#!/usr/bin/env python3
"""
osu! Top Beatmaps Downloader
============================

下载 osu! 上架谱面中按游玩次数降序的前 N 张谱面（默认 10000）。

工作流程:
  1) 用 osu! 官方 API v2 (/beatmapsets/search) 按 plays_desc 翻页获取 beatmapset ID
     列表。结果保存到磁盘以支持断点续传。
  2) 通过镜像站（catboy.best -> nerinyan.moe -> beatconnect.io）下载 .osz 文件。
  3) 完整的进度保存 / 失败重试 / 多镜像回退 / 并发限制。

需要:
  - Python 3.10+
  - 第三方库:  requests
  - 一个 osu! OAuth 应用，提供 client_id 和 client_secret
    在 https://osu.ppy.sh/home/account/edit -> "OAuth" -> "New OAuth Application" 创建
    Application Callback URL 留空即可，scopes 用默认 public

用法:
  1) 复制 config.example.json 为 config.json，填写 client_id / client_secret / 下载目录
  2) python osu_top_downloader.py
  3) 中途可随时 Ctrl+C，再次运行会从断点继续
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ----------------------------- 配置 ----------------------------- #


@dataclass
class Config:
    client_id: str
    client_secret: str
    download_dir: Path
    target_count: int = 10000
    mode: int = 0  # 0=osu, 1=taiko, 2=catch, 3=mania
    # 同时下载多少张谱面。镜像站对并发很敏感，3-5 是安全值。
    download_concurrency: int = 4
    # 每次下载之间的最小间隔（秒），用于善待镜像站
    download_delay: float = 0.5
    # 单个文件下载失败时最多重试次数（每个镜像独立计数）
    max_retries_per_mirror: int = 2
    # 跳过已经存在的 .osz 文件
    skip_existing: bool = True
    # 镜像站列表，按顺序尝试。第一个失败用第二个，以此类推。
    mirrors: list[str] = field(
        default_factory=lambda: [
            "https://catboy.best/d/{id}",
            "https://api.nerinyan.moe/d/{id}",
            "https://beatconnect.io/b/{id}",
        ]
    )
    # ID 列表持久化文件
    id_list_file: str = "beatmapset_ids.json"
    # 失败列表文件
    failed_file: str = "failed.json"
    # 日志文件
    log_file: str = "downloader.log"

    @staticmethod
    def load(path: Path) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        raw["download_dir"] = Path(raw["download_dir"]).expanduser().resolve()
        return Config(**raw)


# ----------------------------- 日志 ----------------------------- #


def setup_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("osu_dl")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ----------------------------- HTTP 会话 ----------------------------- #


def make_session() -> requests.Session:
    """创建一个带自动重试的 requests session。"""
    sess = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    sess.headers.update({
        "User-Agent": "osu-top-downloader/1.0 (educational personal use)",
    })
    return sess


# ----------------------------- osu! OAuth ----------------------------- #


class OsuOAuth:
    """osu! API v2 OAuth client_credentials flow，自动刷新 token。"""

    TOKEN_URL = "https://osu.ppy.sh/oauth/token"

    def __init__(self, client_id: str, client_secret: str, sess: requests.Session,
                 logger: logging.Logger):
        self.client_id = client_id
        self.client_secret = client_secret
        self.sess = sess
        self.logger = logger
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            self._refresh()
            return self._token  # type: ignore

    def _refresh(self) -> None:
        self.logger.info("正在获取 osu! API access token ...")
        resp = self.sess.post(
            self.TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
                "scope": "public",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"获取 access token 失败: HTTP {resp.status_code}: {resp.text}"
            )
        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 86400))
        self.logger.info("Access token 获取成功，有效期约 %d 秒。", data.get("expires_in", 0))


# ----------------------------- 第一步：抓取 ID 列表 ----------------------------- #


def fetch_top_beatmapset_ids(cfg: Config, oauth: OsuOAuth, sess: requests.Session,
                             logger: logging.Logger) -> list[int]:
    """
    按 plays_desc 翻页抓取 ranked 谱面 ID。
    支持断点续传：如果磁盘上已有 id_list_file，则直接加载，并补足到 target_count。
    """
    id_list_path = cfg.download_dir / cfg.id_list_file
    state: dict[str, Any] = {
        "ids": [],
        "cursor_string": None,
        "complete": False,
    }
    if id_list_path.exists():
        try:
            with open(id_list_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            logger.info("加载已保存的 ID 列表：%d 个，cursor=%s, complete=%s",
                        len(state["ids"]), state.get("cursor_string"), state.get("complete"))
        except Exception as e:
            logger.warning("加载 ID 列表失败，重新抓取: %s", e)
            state = {"ids": [], "cursor_string": None, "complete": False}

    if state["complete"] or len(state["ids"]) >= cfg.target_count:
        ids = state["ids"][:cfg.target_count]
        logger.info("ID 列表已足够，共 %d 个，跳过抓取阶段。", len(ids))
        return ids

    logger.info("开始抓取 ID 列表，目标 %d 个 ...", cfg.target_count)
    seen = set(state["ids"])
    cursor_string = state.get("cursor_string")
    page_num = 0
    consecutive_empty = 0

    while len(state["ids"]) < cfg.target_count:
        page_num += 1
        params: dict[str, Any] = {
            "m": cfg.mode,
            "s": "ranked",
            "sort": "plays_desc",
        }
        if cursor_string:
            params["cursor_string"] = cursor_string

        try:
            resp = sess.get(
                "https://osu.ppy.sh/api/v2/beatmapsets/search",
                params=params,
                headers={"Authorization": f"Bearer {oauth.token()}"},
                timeout=60,
            )
        except requests.RequestException as e:
            logger.warning("第 %d 页请求异常，10 秒后重试: %s", page_num, e)
            time.sleep(10)
            continue

        if resp.status_code == 401:
            # token 过期或无效，强制刷新
            logger.info("Token 过期，刷新中 ...")
            oauth._refresh()  # noqa
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "30"))
            logger.warning("被限流，等待 %d 秒。", wait)
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            logger.error("第 %d 页 HTTP %d: %s", page_num, resp.status_code, resp.text[:200])
            time.sleep(5)
            continue

        data = resp.json()
        sets = data.get("beatmapsets", [])
        new_in_page = 0
        for s in sets:
            sid = s.get("id")
            if sid is None or sid in seen:
                continue
            seen.add(sid)
            state["ids"].append(sid)
            new_in_page += 1
            if len(state["ids"]) >= cfg.target_count:
                break

        cursor_string = data.get("cursor_string")
        state["cursor_string"] = cursor_string

        logger.info("第 %d 页：返回 %d 个，新增 %d 个，累计 %d / %d",
                    page_num, len(sets), new_in_page, len(state["ids"]), cfg.target_count)

        # 每 5 页保存一次进度
        if page_num % 5 == 0:
            _save_json_atomic(id_list_path, state)

        # 终止条件
        if not sets or cursor_string is None:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                logger.info("API 未返回更多结果，结束翻页（实际拿到 %d 个）。", len(state["ids"]))
                break
        else:
            consecutive_empty = 0

        # 善待 osu! API：每页之间睡一下
        time.sleep(0.8 + random.random() * 0.4)

    state["complete"] = True
    _save_json_atomic(id_list_path, state)
    ids = state["ids"][:cfg.target_count]
    logger.info("ID 抓取完毕，共 %d 个（目标 %d）。", len(ids), cfg.target_count)
    return ids


def _save_json_atomic(path: Path, data: Any) -> None:
    """原子写入 JSON：先写到 .tmp 再重命名。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ----------------------------- 第二步：下载 .osz ----------------------------- #


@dataclass
class DownloadResult:
    beatmapset_id: int
    success: bool
    path: Optional[Path] = None
    error: Optional[str] = None
    mirror_used: Optional[str] = None


def download_one(beatmapset_id: int, cfg: Config, sess: requests.Session,
                 logger: logging.Logger, stop_event: threading.Event) -> DownloadResult:
    """下载单个 beatmapset。按顺序尝试每个镜像。"""
    target = cfg.download_dir / f"{beatmapset_id}.osz"

    if cfg.skip_existing and target.exists() and target.stat().st_size > 0:
        return DownloadResult(beatmapset_id, True, target, mirror_used="(已存在)")

    last_err = "未尝试任何镜像"
    for mirror_template in cfg.mirrors:
        if stop_event.is_set():
            return DownloadResult(beatmapset_id, False, error="用户中止")

        url = mirror_template.format(id=beatmapset_id)
        for attempt in range(cfg.max_retries_per_mirror + 1):
            if stop_event.is_set():
                return DownloadResult(beatmapset_id, False, error="用户中止")
            try:
                with sess.get(url, stream=True, timeout=(15, 120),
                              allow_redirects=True) as r:
                    if r.status_code == 404:
                        last_err = f"{url} -> 404"
                        break  # 404 时换镜像比重试更有意义
                    if r.status_code == 429:
                        wait = int(r.headers.get("Retry-After", "20"))
                        logger.warning("镜像限流: %s, 等待 %ds", url, wait)
                        if stop_event.wait(wait):
                            return DownloadResult(beatmapset_id, False, error="用户中止")
                        continue
                    if r.status_code != 200:
                        last_err = f"{url} -> HTTP {r.status_code}"
                        time.sleep(1.5 ** attempt)
                        continue

                    ctype = r.headers.get("Content-Type", "")
                    # 镜像站正常返回 application/x-osu-archive 或 application/octet-stream
                    if "html" in ctype.lower():
                        last_err = f"{url} -> 返回 HTML（可能维护中）"
                        break

                    tmp = target.with_suffix(".osz.part")
                    bytes_written = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=64 * 1024):
                            if stop_event.is_set():
                                f.close()
                                tmp.unlink(missing_ok=True)
                                return DownloadResult(beatmapset_id, False, error="用户中止")
                            if chunk:
                                f.write(chunk)
                                bytes_written += len(chunk)

                    if bytes_written < 1024:
                        last_err = f"{url} -> 文件过小 ({bytes_written}B)"
                        tmp.unlink(missing_ok=True)
                        time.sleep(1.5 ** attempt)
                        continue

                    tmp.replace(target)
                    return DownloadResult(beatmapset_id, True, target, mirror_used=url)
            except requests.RequestException as e:
                last_err = f"{url} -> {type(e).__name__}: {e}"
                time.sleep(1.5 ** attempt)

        # 该镜像彻底失败，下一个
        logger.debug("镜像失败 [%s]: %s", beatmapset_id, last_err)

    return DownloadResult(beatmapset_id, False, error=last_err)


def download_all(ids: list[int], cfg: Config, sess: requests.Session,
                 logger: logging.Logger, stop_event: threading.Event) -> None:
    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    failed_path = cfg.download_dir / cfg.failed_file

    # 加载历史失败记录（仅作信息展示，不强制重试，避免无限循环）
    historical_failed: dict[str, str] = {}
    if failed_path.exists():
        try:
            with open(failed_path, "r", encoding="utf-8") as f:
                historical_failed = json.load(f)
        except Exception:
            historical_failed = {}

    failed_lock = threading.Lock()
    new_failed: dict[str, str] = {}

    total = len(ids)
    counter = {"done": 0, "ok": 0, "skipped": 0, "failed": 0}
    counter_lock = threading.Lock()

    delay_sema = threading.Semaphore(cfg.download_concurrency)

    def worker(bid: int) -> DownloadResult:
        # 简单的全局速率控制：每次任务前等一下
        time.sleep(cfg.download_delay)
        with delay_sema:
            return download_one(bid, cfg, sess, logger, stop_event)

    logger.info("开始下载 %d 张谱面到 %s（并发 %d）",
                total, cfg.download_dir, cfg.download_concurrency)

    last_save = time.time()
    with ThreadPoolExecutor(max_workers=cfg.download_concurrency) as pool:
        futures = {pool.submit(worker, bid): bid for bid in ids}
        try:
            for fut in as_completed(futures):
                bid = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = DownloadResult(bid, False, error=f"未捕获异常: {e}")

                with counter_lock:
                    counter["done"] += 1
                    if res.success:
                        if res.mirror_used == "(已存在)":
                            counter["skipped"] += 1
                        else:
                            counter["ok"] += 1
                    else:
                        counter["failed"] += 1
                        with failed_lock:
                            new_failed[str(bid)] = res.error or "unknown"

                done = counter["done"]
                if res.success:
                    if done % 25 == 0 or done == total:
                        logger.info(
                            "[%d/%d] OK=%d 跳过=%d 失败=%d   最近: %d (%s)",
                            done, total, counter["ok"], counter["skipped"],
                            counter["failed"], bid, res.mirror_used,
                        )
                else:
                    logger.warning("[%d/%d] 失败 %d: %s",
                                   done, total, bid, res.error)

                # 每 60 秒保存一次失败列表
                if time.time() - last_save > 60:
                    _save_json_atomic(failed_path, new_failed)
                    last_save = time.time()

                if stop_event.is_set():
                    # 取消所有还没开始的任务
                    for f in futures:
                        f.cancel()
                    break
        finally:
            _save_json_atomic(failed_path, new_failed)

    logger.info("下载阶段结束。新下载=%d 跳过=%d 失败=%d / 共 %d",
                counter["ok"], counter["skipped"], counter["failed"], total)
    if new_failed:
        logger.info("失败列表已保存到 %s（共 %d 项），可重新运行脚本以重试。",
                    failed_path, len(new_failed))


# ----------------------------- main ----------------------------- #


def main():
    parser = argparse.ArgumentParser(description="osu! Top Beatmaps Downloader")
    parser.add_argument("--config", default="config.json",
                        help="配置文件路径（默认 ./config.json）")
    parser.add_argument("--ids-only", action="store_true",
                        help="只抓取 ID 列表，不下载")
    args = parser.parse_args()

    cfg_path = Path(args.config).expanduser().resolve()
    if not cfg_path.exists():
        print(f"配置文件不存在: {cfg_path}", file=sys.stderr)
        print("请基于 config.example.json 复制一份并填写。", file=sys.stderr)
        sys.exit(1)

    cfg = Config.load(cfg_path)
    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(cfg.download_dir / cfg.log_file)
    logger.info("配置: target=%d, mode=%d, dir=%s, concurrency=%d",
                cfg.target_count, cfg.mode, cfg.download_dir, cfg.download_concurrency)

    # Ctrl+C 优雅退出
    stop_event = threading.Event()

    def handle_sig(signum, frame):
        logger.warning("收到信号 %s，准备优雅退出（进行中的下载会完成）...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    sess = make_session()
    oauth = OsuOAuth(cfg.client_id, cfg.client_secret, sess, logger)

    try:
        ids = fetch_top_beatmapset_ids(cfg, oauth, sess, logger)
    except Exception as e:
        logger.exception("抓取 ID 列表失败: %s", e)
        sys.exit(2)

    if args.ids_only:
        logger.info("--ids-only 已设置，结束。")
        return

    download_all(ids, cfg, sess, logger, stop_event)


if __name__ == "__main__":
    main()
