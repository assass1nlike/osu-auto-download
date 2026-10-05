#!/usr/bin/env python3
"""
Build a fake osu!stable directory from a folder of .osz files.
============================================================

Why this exists
---------------
osu!lazer's setup wizard can import beatmaps from an osu!stable installation,
i.e. a directory shaped like:

    osu!stable/
        Songs/
            123 Artist - Title/
                map.osu
                audio.mp3
                ...
            456 Other - Map/
                ...

This script takes a directory full of .osz files (the output of
osu_top_downloader.py) and produces exactly that shape. After it runs,
you point lazer's setup wizard at the produced directory and it will import
every beatmap in one go — no per-file clicking, no command line tricks.

Usage
-----
    python build_fake_stable.py --src D:/osu_beatmaps --dst D:/fake_stable

Then in lazer:
    Settings -> Debug (or General, depending on version) -> Run setup wizard
        -> "Locate osu!(stable) installation" -> point to D:/fake_stable
        -> Import beatmaps.

Notes
-----
- A .osz file is just a renamed .zip. We unzip each one into Songs/<id>/.
- We do NOT delete the original .osz files. If you want to reclaim disk space
  after import succeeds, delete them yourself.
- Disk usage roughly doubles during the process: original .osz files plus
  unzipped contents. Make sure you have headroom.
- This is idempotent: re-running skips folders that already exist and look
  complete. Safe to interrupt with Ctrl+C and resume.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


# --------------------------------------------------------------------------- #
# logging                                                                     #
# --------------------------------------------------------------------------- #


def setup_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("build_fake_stable")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# --------------------------------------------------------------------------- #
# extraction                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class ExtractResult:
    osz_name: str
    success: bool
    target: str | None = None
    error: str | None = None
    skipped: bool = False


def _looks_complete(folder: Path) -> bool:
    """A folder is 'complete' if it contains at least one .osu file."""
    if not folder.is_dir():
        return False
    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".osu":
                return True
    except OSError:
        return False
    return False


def _extract_one(osz_path_str: str, songs_dir_str: str) -> ExtractResult:
    """
    Top-level function (module-level) so it works with ProcessPoolExecutor.
    Extracts a single .osz into songs_dir/<osz_stem>/.
    """
    osz_path = Path(osz_path_str)
    songs_dir = Path(songs_dir_str)
    target = songs_dir / osz_path.stem

    if _looks_complete(target):
        return ExtractResult(osz_path.name, True, str(target), skipped=True)

    # Fresh extraction: clear any partial directory first
    if target.exists():
        try:
            shutil.rmtree(target)
        except OSError as e:
            return ExtractResult(osz_path.name, False, error=f"rmtree failed: {e}")

    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists():
        try:
            shutil.rmtree(tmp)
        except OSError:
            pass

    try:
        tmp.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        return ExtractResult(osz_path.name, False, error=f"mkdir failed: {e}")

    try:
        with zipfile.ZipFile(osz_path, "r") as zf:
            # Defensively reject zip entries that try to escape the target dir
            # (e.g. paths containing ".." or absolute paths).
            tmp_resolved = tmp.resolve()
            for member in zf.infolist():
                # ZipFile.extract handles forward slashes; resolve target path
                # and check it stays inside tmp_resolved.
                dest = (tmp / member.filename).resolve()
                try:
                    dest.relative_to(tmp_resolved)
                except ValueError:
                    raise RuntimeError(
                        f"unsafe zip entry: {member.filename!r}"
                    )
            zf.extractall(tmp)
    except (zipfile.BadZipFile, RuntimeError, OSError) as e:
        # Clean up the half-written tmp dir
        try:
            shutil.rmtree(tmp)
        except OSError:
            pass
        return ExtractResult(osz_path.name, False, error=f"extract failed: {e}")

    # Atomic-ish rename. On Windows this fails if target exists; we cleared it.
    try:
        tmp.rename(target)
    except OSError as e:
        try:
            shutil.rmtree(tmp)
        except OSError:
            pass
        return ExtractResult(osz_path.name, False, error=f"rename failed: {e}")

    if not _looks_complete(target):
        return ExtractResult(
            osz_path.name, False, error="extracted but no .osu file found"
        )
    return ExtractResult(osz_path.name, True, str(target))


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #


def build(
    src: Path,
    dst: Path,
    workers: int,
    logger: logging.Logger,
    create_marker: bool = True,
) -> None:
    if not src.is_dir():
        logger.error("source dir does not exist: %s", src)
        sys.exit(1)

    songs_dir = dst / "Songs"
    songs_dir.mkdir(parents=True, exist_ok=True)

    if create_marker:
        # Lazer's setup wizard sometimes wants to see a stable-ish marker file.
        # Creating an empty osu!.<user>.cfg makes the directory look more like a
        # real stable install. Harmless if not needed.
        marker = dst / "osu!.cfg"
        if not marker.exists():
            try:
                marker.write_text(
                    "# placeholder so lazer setup wizard is happy\n",
                    encoding="utf-8",
                )
            except OSError as e:
                logger.warning("could not write marker file: %s", e)

    osz_files = sorted(p for p in src.glob("*.osz") if p.is_file())
    if not osz_files:
        logger.error("no .osz files found in %s", src)
        sys.exit(1)

    failed_path = dst / "extract_failed.json"
    failed: dict[str, str] = {}

    logger.info(
        "extracting %d .osz files from %s into %s (workers=%d)",
        len(osz_files), src, songs_dir, workers,
    )

    counters = {"ok": 0, "skipped": 0, "failed": 0}
    started = time.time()
    last_log = started

    if workers <= 1:
        # Single-process path: easier debugging, identical correctness
        for i, osz in enumerate(osz_files, 1):
            res = _extract_one(str(osz), str(songs_dir))
            if res.success:
                if res.skipped:
                    counters["skipped"] += 1
                else:
                    counters["ok"] += 1
            else:
                counters["failed"] += 1
                failed[res.osz_name] = res.error or "unknown"
                logger.warning("FAIL %s: %s", res.osz_name, res.error)

            if time.time() - last_log > 5 or i == len(osz_files):
                logger.info(
                    "[%d/%d] ok=%d skipped=%d failed=%d",
                    i, len(osz_files),
                    counters["ok"], counters["skipped"], counters["failed"],
                )
                last_log = time.time()
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_extract_one, str(osz), str(songs_dir)): osz
                for osz in osz_files
            }
            done = 0
            for fut in as_completed(futures):
                done += 1
                try:
                    res = fut.result()
                except Exception as e:  # pragma: no cover
                    osz = futures[fut]
                    res = ExtractResult(osz.name, False, error=f"worker crashed: {e}")

                if res.success:
                    if res.skipped:
                        counters["skipped"] += 1
                    else:
                        counters["ok"] += 1
                else:
                    counters["failed"] += 1
                    failed[res.osz_name] = res.error or "unknown"
                    logger.warning("FAIL %s: %s", res.osz_name, res.error)

                if time.time() - last_log > 5 or done == len(osz_files):
                    logger.info(
                        "[%d/%d] ok=%d skipped=%d failed=%d",
                        done, len(osz_files),
                        counters["ok"], counters["skipped"], counters["failed"],
                    )
                    last_log = time.time()

    if failed:
        failed_path.write_text(
            json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("wrote failure list to %s (%d entries)", failed_path, len(failed))

    elapsed = time.time() - started
    logger.info(
        "done in %.0fs. extracted=%d skipped=%d failed=%d",
        elapsed, counters["ok"], counters["skipped"], counters["failed"],
    )
    logger.info("now point osu!lazer's setup wizard at: %s", dst)
    logger.info(
        "  Settings -> Maintenance / Debug -> 'Run setup wizard' "
        "-> 'Locate osu!(stable) installation' -> select the directory above."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Unpack .osz files into a fake osu!stable directory "
                    "for lazer setup wizard import."
    )
    parser.add_argument("--src", required=True, type=Path,
                        help="directory containing .osz files")
    parser.add_argument("--dst", required=True, type=Path,
                        help="output directory; a Songs/ subdir will be created here")
    parser.add_argument("--workers", type=int, default=4,
                        help="parallel extraction workers (default 4)")
    parser.add_argument("--no-marker", action="store_true",
                        help="don't create osu!.cfg marker file in --dst")
    parser.add_argument("--log", default=None,
                        help="log file path (default: <dst>/build_fake_stable.log)")
    args = parser.parse_args()

    src = args.src.expanduser().resolve()
    dst = args.dst.expanduser().resolve()
    log_path = Path(args.log).expanduser().resolve() if args.log \
        else dst / "build_fake_stable.log"

    logger = setup_logging(log_path)
    logger.info("src=%s", src)
    logger.info("dst=%s", dst)

    build(src, dst, args.workers, logger, create_marker=not args.no_marker)


if __name__ == "__main__":
    main()
