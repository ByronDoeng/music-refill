# 架构与开发指南

本文面向接手开发的人。目标是用最少时间理解边界、找到修改点，并知道哪些行为不能破坏。使用方式见 [README](../README.md)，接口字段见 [API](API.md)，已知限制见 [STATUS](STATUS.md)。

## 总体结构

```text
music_refill/
├── __main__.py        CLI：serve / launch / stop / scan / doctor
├── launcher.py        静默启动、健康检查、PID、打开浏览器
├── config.py          config.toml → dataclass；统一校验与路径解析
├── state.py           SQLite：扫描、缺口、上传记录、持久任务队列
├── netease/           登录、WEAPI、歌单/云盘读取、云盘上传
├── core/              缺口判定、候选打分、下载、标签与封面
├── services/          日志、重试、上传后校验
├── sources/           音源协议、模板与动态加载器
└── web/
    ├── app.py         FastAPI 路由、匹配任务、下载 worker
    └── static/        原生 HTML/CSS/JS 单页
sources_local/         音源适配器（默认带 kuwo、migu）
tests/                 接口、配置、队列、加载器回归
data/                  运行数据（gitignored）
```

模块边界也是失败边界：一个音源失败只影响自己的候选；一首歌下载/上传失败不阻塞其它任务；音频文件与队列记录分开处理。

## 请求与任务流

```text
浏览器
  │
  ├─ /api/scan ──────────────> gap_finder：播放地址终审 + 云盘判重
  │                              └─ 缺口清单（可导出 markdown）
  │
  ├─ /api/match/start ───────> 后台线程逐首搜索 / 打分
  │                              └─ /api/match/results 增量读取
  │
  ├─ /api/source-search ─────> 并发音源搜索，首批先返回
  │                              └─ /api/source-search/results 合并慢源
  │
  └─ /api/download ──────────> jobs(SQLite) → 内存队列 → worker
                                    下载 → 大小时校验 → 标签/封面
                                    → upload/check 秒传或 NOS 上传
                                    → info/v2 → pub/v2 → 云盘校验
```

`/api/tasks` 是所有页面共用的只读快照，驱动顶部任务栏；它不触发后台行为。匹配与搜索会话只保存在内存中，下载任务才持久化。

## 前后端职责

- 后端只暴露本地 JSON API，业务判断集中在 `core/`、`netease/` 与 `web/app.py`。
- 前端不推断音源结果，只负责展示候选、保留用户选择、按接口契约提交。
- 视图由 `setView()` 统一切换：`dashboard → playlist/gaps/match` 或 `search`。
- 音源版本选择是悬浮层，不改变表格行高；音质选项来自该版本的实际可用音质。
- 任务栏固定在顶部，使用 `/api/tasks` 每秒轮询，与当前页面无关。

## 音源适配器

协议定义在 `music_refill/sources/base.py`，适配器由 `loader.py` 从目录动态加载：

```text
load_sources(dirs, order, workdir, probe, enabled, disabled)
  ├─ 跳过下划线开头的模板文件
  ├─ 按 dirs 顺序叠加；后加载的同名源覆盖前者
  ├─ 每个 .py 独立加载；先注册后抛错也会回滚，不影响其它源
  ├─ enabled/disabled 按注册名过滤，状态接口与搜索使用同一集合
  ├─ 按 order 排优先级，未列出的排后
  ├─ 可选并发探活；probe=False 时直接视为可用
  └─ 重建注册表，供搜索/匹配读取
```

目录来源：

1. 默认 `sources_local/`（仓库自带酷我和咪咕）；
2. `config.toml [sources].dirs` 可叠加多个绝对路径或项目内相对路径；
3. 后面的目录覆盖同名注册名，因此只替换一个源时，新建一个只含该文件的目录即可；
4. `enabled` / `disabled` 可按注册名启停，不必搬动文件；
5. 旧配置 `[sources].dir` 仍可用，等价于单目录的 `dirs`。

替换示例（保留内置源，只覆盖酷我）：

```toml
[sources]
dirs = ["sources_local", "my_sources"]
enabled = ["kuwo", "migu"]
disabled = []
order = ["kuwo", "migu"]
```

`my_sources/kuwo.py` 只需注册名为 `kuwo` 的同一个协议实现，其它源继续从
`sources_local/` 加载。改完文件后调用 `POST /api/sources/reload`，不需要重启进程；
已在执行的任务继续使用旧实例，新任务使用新集合。

新增源的最小接口：

```python
class MySource:
    name = "my_source"
    quality_ceiling = "320"

    def health_check(self) -> bool: ...

    def search(self, title: str, artist: str = "", limit: int = 5) -> list[SourceTrack]: ...
```

性能扩展（可选）：

- `quick_search()` 只返回元信息，不解析下载地址；
- `resolve_many()` 并发补全链接并探测文件；
- `source_id` 与 `source_meta` 可保存适配器私有字段，但必须 JSON 序列化。

未实现快路径的适配器仍可直接使用 `search()`；实现快路径的源会在搜索补歌里明显更快。

## 匹配与音质

- 查询变体按“标题、标题 + 歌手、标题 + 主歌手”生成，最多三个。
- 不同音源并行，同一音源内部按顺序执行；标题精确命中后提前停止。
- 打分以标题为主，歌手与时长辅助；Live/翻唱/伴奏相对原曲降权。
- 候选按“来源 + 标题 + 歌手 + 专辑 + 时长”分组，组内保留可用音质。
- 前端和后端都按“精确 → 最近更高 → 最近更低”选音质。
- 入队时把选中的 `quality` 和 `track` 写入任务 payload；worker 不再读取全局默认音质。

## 持久化与恢复

`state.py` 用 SQLite 保存扫描、缺口、上传记录和任务。启动时：

- `running` 任务改回 `queued`；
- 首次确认登录后恢复 `queued` 任务；
- 队列记录先落库，再进入内存队列；
- 失败任务只由用户点击重试，不会无限自动重跑。

表结构通过幂等 `ALTER TABLE` 迁移，旧数据库可直接沿用。

## 安全边界

- 服务默认只绑定 `127.0.0.1`，不适合直接暴露到网络；没有远程鉴权。
- 报告下载只允许 `data/reports/*.md`，并拒绝路径分隔符。
- 下载队列只接受 `http/https` URL。
- Cookie 只写入 `data/cookies.json`，日志不打印 Cookie 值。
- 浏览器登录使用独立 profile 与本机 CDP，不读取日常浏览器 profile。

## 开发与验证

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m music_refill doctor
.venv/bin/python -m music_refill serve
```

测试通过 `MUSIC_REFILL_DATA_DIR` 使用临时目录，不触碰真实账号数据。

改动前建议确认：

- 队列/上传改动必须跑 `tests/test_api_smoke.py`；
- 新配置项同步 `config.py`、`config.example.toml` 和 README；
- 新接口同步 `docs/API.md`；
- 新音源至少验证 `health_check()`、`search()` 和一次真实入队；
- 修改前端后检查 `node --check music_refill/web/static/app.js`。

## 交接清单

1. 不要提交 `data/`、`config.toml` 或任何 Cookie/数据库/下载文件。
2. `sources_local/` 是可运行依赖，不是本地私货；新增或替换适配器时保持单文件可回退。
3. 音源接口失效时，先在页面和 `/api/sources` 看健康状态，再查 `data/logs/music_refill.log`。
4. 匹配结果只存在内存；服务重启后需要重新扫描或搜索。
5. 下载任务可续跑，但账号未登录时不会启动。
