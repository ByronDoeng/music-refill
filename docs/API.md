# 本地 API 契约

服务只监听 `127.0.0.1`（`config.toml [server]`），所有接口都是本地 JSON；
没有鉴权中间件，**不要**把端口暴露到公网或局域网。

通用约定：

- 成功：HTTP 200 + JSON；失败：HTTP 4xx/5xx + `{"detail": "面向用户的中文说明"}`。
- 错误码：`400` 参数不合法；`401` 未登录/cookie 失效；`404` 资源不存在；
  `408` 浏览器登录超时；`409` 已有同类任务在跑；`502` 上游（网易/音源）失败。
- 所有写操作只在用户点击后触发；`/api/status`、`/api/history`、`/api/search` 不改动账号数据。
- `/api/source-search` 会创建短期内存会话，用于合并慢音源结果，不会写数据库。

## 登录

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 登录态、当前配置、后台队列状态 |
| POST | `/api/login/browser` | 启动本机 Chrome/Edge，登录后自动取 cookie（阻塞直到成功/超时） |
| POST | `/api/login/cookie` | 手动注入 Cookie 串，body `{"raw": "MUSIC_U=...; ..."}` |
| POST | `/api/logout` | 清空会话与 `data/cookies.json` |
| POST | `/api/login/qr/create` | 生成扫码二维码（受网易 8821 风控影响） |
| GET | `/api/login/qr/check?unikey=` | 轮询扫码状态；`801/802/803/800/8821` |

`/api/status` 返回示例：

```json
{
  "logged_in": true,
  "user_id": 123,
  "nickname": "example-user",
  "config": {"quality": "flac", "limit": 50, "sleep_min": 3.0, "sleep_max": 10.0, "upload_attempts": 2},
  "queue_len": 0,
  "running": false
}
```

## 歌单与扫描

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/playlists` | 歌单列表 + 默认勾选项 |
| POST | `/api/scan` | body `{"playlist_ids": [..], "include_liked": true}`，返回缺口清单 |
| GET | `/api/report/{filename}` | 下载 `data/reports/*.md`，拒绝路径穿越 |

`/api/scan` 的 `gap[]` 字段：

```text
id, name, artists[], album, duration_ms, fee, reason,
playlists[], in_cloud, cloud_info, processed
```

- `in_cloud=true`：云盘里已有同名文件（未匹配曲库），默认不勾选；
- `processed=true`：仅供排障/统计参考；当前前端不再展示“已补过”标签，也不据此取消勾选。

## 音源匹配

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sources` | 各音源健康状态、优先级、来源文件；目录由 `[sources].dirs` 指定 |
| POST | `/api/sources/reload` | 重新扫描音源目录；改适配器后无需重启进程 |
| POST | `/api/sources/probe` | 重新加载并探活当前集合；排查上游接口是否可达 |
| POST | `/api/match/start` | body `{"songs": [gap_item, ...], "origin": "gap"\|"search"}`，后台匹配 |
| GET | `/api/match/results?offset=&limit=` | 增量拉取匹配结果（`limit<=50`） |

`/api/sources` 返回：

```json
{
  "sources": {"kuwo": {"healthy": true, "error": "", "priority": 0, "path": ".../kuwo.py"}},
  "dirs": [".../sources_local"],
  "enabled": [],
  "disabled": [],
  "order": ["kuwo", "migu"]
}
```

`enabled` 为空表示全部启用；`disabled` 永远排除。两者按注册名匹配，不按文件名。
`/api/sources/reload` 返回同样的结构，外加 `"reloaded": true`。

`/api/match/results` 额外返回 `errors[]`：没有候选的歌曲会以
`{"name", "artist", "detail"}` 报告（通常是音源慢/无下载地址），不影响已匹配结果。
单首等待上限是 `scan.match_source_timeout_s`（默认 30 秒）；超时只取消未完成的查询，
已完成音源结果仍会返回，并推荐用户换关键词重试。

匹配结果：

```json
{"song": {...}, "versions": [{"source": "kuwo", "title": "...", "artist": "..." }], "auto": true}
```

`versions[]` 是新的确认单位，字段：

```text
source, title, artist, album, duration_s, score,
available_qualities: ["flac", "320", "128"],
qualities: [{quality, label, bitrate, track: SourceTrack}]
```

同一来源、同标题/歌手/专辑/时长的不同音质会合并成一个版本；`track` 是可直接
提交给 `/api/download` 的下载参数。`candidates` 字段保留兼容，但前端不再依赖。

音质回退顺序由前端 `pickQuality()` 与后端 `choose_quality()` 共用同一规则：
精确匹配 → 最接近的更高一档 → 最接近的更低一档；仍无可用音质则禁止入队。

## 下载 / 上传

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/download` | body `{"items": [{"song": {...}, "track": {...}, "quality": "320"}], "origin": "gap"\|"search"}`，单批 ≤ `limit`；返回 `job_ids` |
| GET | `/api/download/progress` | 当前阶段、字节进度、成功/失败、校验结果 |
| POST | `/api/downloads/clear` | 清空 `data/downloads/` |
| POST | `/api/jobs/retry?limit=` | 把最近失败任务重新入队（显式触发，不自动重跑） |

`/api/download/progress` 的 `current` 阶段：
`下载 → 打标 → 上传云盘 → 校验云盘`；失败重试时显示“XX失败，等待重试”。

结果对象：

```text
song_id, name, status(ok|failed), quality, cloud_song_id,
matched_library, instant, verified, playable, attempt, detail
```

- `matched_library=true`：网易云盘文件匹配到了曲库条目；
- `verified=true`：云盘列表里确实查到了该文件；
- `playable=true/false/null`：已匹配曲库时再查播放 URL；未匹配文件为 `null`。
- `origin` / `quality`：任务来源与逐首实际音质；写入任务 payload，worker 按它上传，
  不受后续修改全局默认音质影响。

## 搜索补歌（直接搜音源）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/source-search` | body `{"keyword": "晴天 周杰伦", "limit": 20}`；并发搜各健康音源，只读 |
| POST | `/api/source-search/results` | body `{"token": "..."}`；拉取首批之后完成的慢音源结果 |

返回 `{"keyword", "sources", "versions", "errors", "token", "pending"}`。
`pending>0` 表示还有音源在跑，前端用它轮询 `/api/source-search/results`；该接口返回
`{"keyword", "versions", "errors", "pending", "done", "changed"}`，`versions` 是**全量**
合并后的列表（不是增量），前端按版本 key 重映射已勾选项即可。

`versions[]` 与匹配接口同构，
另外带 `canonical`（尽力匹配到的网易曲库原版，可能为 `null`）和
`suggested_song`（入队时可直接使用的 song 对象；匹配不到曲库时使用负数稳定 id）。

搜索补歌**不自动入队**：前端默认全不选，用户勾选候选和音质后才调用
`/api/download`。这与旧的 `/api/quick-add` 自动高分入队路径不同。

查询规则：先清理空白/零宽字符，再按“标题、标题+歌手、标题+主歌手”生成最多
3 个变体；音源之间并行、同一音源内串行，标题精确命中即跳过后续变体。
首批最多等 `scan.source_search_first_batch_s`（默认 3 秒），到点先返回已完成音源；
慢音源继续在后台跑，最多到 `scan.source_search_timeout_s`（默认 30 秒）。
超时仍返回已完成候选并在 `errors[]` 说明。

音源侧实现约定：`quick_search()` 只取元信息（`resolved=false`），`resolve_many()`
并发补下载地址并探测真实类型；未实现这两个方法的旧适配器仍可只用 `search()`。
候选可带 `source_id` / `source_meta`（解析下载地址所需的私有字段），必须可 JSON 序列化。

## 兼容：按歌名快速补歌

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/search` | body `{"keyword": "晴天 周杰伦", "limit": 20}`，只读搜索网易曲库 |
| POST | `/api/quick-add` | body `{"songs": [search_song, ...], "min_score": 1.15}`；启动后台匹配并立即返回 |
| GET | `/api/quick-add/status` | 轮询后台快速补歌进度与结果 |

返回：

```json
{"keyword": "...", "songs": [{"id": 1, "name": "...", "artists": "周杰伦", "album": "...",
 "duration_ms": 269000, "fee": 1, "in_cloud": false, "cloud_hit": null, "cloud_info": null}]}
```

前端新流程已改为 `/api/source-search` + 人工勾选 + `/api/download`；
`/api/quick-add` 作为旧路径保留，不再被当前页面调用。没有绕过候选校验的
“裸上传”接口：任何路径最终都走 `/api/download` 的同一 worker。

`/api/quick-add` 是“按名字搜歌 → 选中 → 直接加入网盘”的快速路径：

- 请求立即返回 `{"started": true, "total": N, "threshold": 1.15}`，匹配在线程中执行；
- 前端每 1.5 秒轮询 `/api/quick-add/status`，不会长时间占住 HTTP 请求；
- 只有最高分 ≥ 阈值才直接入队；实际阈值不低于 `downloader.AUTO_CONFIRM_THRESHOLD`（0.8），
  调用方不能把明显低分的候选强制直传；
- 低于阈值进入 `needs_review`（含候选），前端回落到人工确认；无候选进入 `skipped`；
- 单次 ≤ `scan.limit` 首；启动时若内存队列积压超过 `scan.limit * 6` 返回 400。

状态返回：

```json
{"running": false, "total": 1, "done": 1, "threshold": 1.15, "queued": 1,
 "job_ids": [7], "needs_review": [], "skipped": [], "errors": []}
```

阈值宁高勿低：错歌进了云盘，清理成本远高于多确认一次。

`origin="search"` 的任务不关联扫描批次（`scan_id=0`），但成功记录仍写入 `uploads`，
因此会进入 `/api/history` 统计；失败任务可通过 `/api/jobs/retry` 重新入队。

`/api/download` 返回 `{"queued": 1, "queue_len": 1, "job_ids": [42]}`；任务先写 SQLite
再进内存队列，服务重启后由启动线程确认登录态再续跑。`job_ids` 会随 payload 一起
写入 `jobs.payload_json`，worker 用它在 `running/ok/failed` 间更新状态。

## 交接与排障

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/history?limit=` | 去重后的成功/失败统计、最近上传、最近任务、当前配置 |
| GET | `/api/tasks` | 全局任务栏快照：当前阶段、队列、成功/失败、最近结果、匹配进度 |

`/api/tasks` 是只读聚合接口，前端每秒轮询；它不触发任何后台行为。

`/api/status` 额外暴露 `persisted_queued`：SQLite 里 `queued` 的任务数，用于区分
“内存队列”与“重启待恢复”。正常情况下两者在入队后会一致；若不一致，先查
`data/logs/music_refill.log` 与 `/api/history`。

命令行：

```bash
python -m music_refill serve     # 启动 GUI
python -m music_refill launch    # 静默启动并打开网页
python -m music_refill stop      # 停止 launch 启动的服务
python -m music_refill scan      # 终端输出缺口 markdown
python -m music_refill doctor    # 依赖/浏览器/音源/数据目录自检
```
