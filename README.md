# osu-auto-download

按游玩次数批量下载 osu! Ranked 谱面集，并将 `.osz` 文件整理为 osu!stable 的目录结构，供 osu!lazer 批量导入。

## 功能

- 从 osu! 官方 API v2 获取按游玩次数降序排列的谱面集。
- 从多个镜像站下载 `.osz`，支持并发、失败重试和镜像切换。
- 缓存谱面集 ID，重复运行时跳过已下载文件。
- 批量解压为 `Songs/<谱面集 ID>/`，支持并行解压和跳过已完成目录。

## 安装与配置

需要 Python 3.10 或更新版本。

```sh
git clone https://github.com/assass1nlike/osu-auto-download.git
cd osu-auto-download
python -m pip install -r requirements.txt
```

复制 `config.example.json` 为 `config.json`。在 Windows PowerShell 中：

```powershell
Copy-Item config.example.json config.json
```

在 [osu! 账号设置](https://osu.ppy.sh/home/account/edit) 中创建 OAuth 应用，将 Client ID 和 Client Secret 填入 `config.json`。脚本使用 `client_credentials` 流程和 `public` scope。

`config.json` 包含本地凭据，已加入 `.gitignore`；分享配置时使用示例文件。

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `download_dir` | `./downloads` | 下载目录；相对路径以运行命令的目录为基准 |
| `target_count` | `10000` | 谱面集数量，一个谱面集可能包含多个难度 |
| `mode` | `0` | `0` osu!、`1` taiko、`2` catch、`3` mania |
| `download_concurrency` | `4` | 同时下载的任务数 |
| `download_delay` | `0.5` | 每个下载任务开始前的等待秒数，并非全局请求间隔 |
| `max_retries_per_mirror` | `2` | 每个镜像首次尝试失败后的最大重试次数 |
| `skip_existing` | `true` | 跳过已有的非空 `.osz` 文件 |
| `mirrors` | 见示例配置 | 按顺序尝试的下载地址，`{id}` 替换为谱面集 ID |

## 下载谱面

```sh
python osu_top_downloader.py --config config.json
```

只获取谱面集 ID，不下载文件：

```sh
python osu_top_downloader.py --config config.json --ids-only
```

下载目录中会生成：

```text
downloads/
  123456.osz
  beatmapset_ids.json
  failed.json
  downloader.log
```

重新运行相同命令即可跳过已下载文件、再次尝试失败项。已有文件仅按非空判断，不校验压缩包完整性；损坏文件需删除后重下。

ID 列表会被复用。更改模式、增加目标数量或需要刷新热门排序时，先删除下载目录中的 `beatmapset_ids.json`，或换用新的下载目录。

## 整理并导入 osu!lazer

```sh
python build_fake_stable.py --src ./downloads --dst ./fake_stable --workers 4
```

输出结构：

```text
fake_stable/
  osu!.cfg
  Songs/
    123456/
      map.osu
      audio.mp3
      ...
  build_fake_stable.log
```

在 osu!lazer 设置中找到 **Run setup wizard**，选择定位 osu!stable 安装目录，将路径指向 `fake_stable`，然后导入谱面。入口名称和位置可能随 lazer 版本变化。

原始 `.osz` 会保留；解压内容和 lazer 导入后的副本需要额外磁盘空间。重复运行时，含有 `.osu` 文件的目标目录会被跳过。解压失败项会写入目标目录的 `extract_failed.json`。

其他选项可通过以下命令查看：

```sh
python osu_top_downloader.py --help
python build_fake_stable.py --help
```

镜像站由第三方运营，可用性和限速策略可能变化；遇到限流时降低并发或调整镜像配置。
