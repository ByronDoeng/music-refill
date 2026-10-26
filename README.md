# music-refill

把听不了的歌，补进网易云音乐云盘。

扫描歌单找出缺口，按歌名匹配可下载音源，人工确认后自动完成 **下载 → 打标 → 上传 → 校验**。本机运行，只监听 `127.0.0.1`。

[快速开始](#快速开始) · [工作原理](#工作原理) · [配置](#配置) · [开发文档](docs/architecture.md) · [API](docs/API.md) · [已知限制](docs/STATUS.md)

## 功能

- **歌单补歌**：扫描歌单，先看缺口清单；云盘疑似已有会优先标注，再由你决定补哪些。
- **搜索补歌**：输入“歌名 + 歌手”直接搜音源，候选分页展示，勾选后加入下载队列。
- **可插拔音源**：自带酷我、咪咕适配器；可叠加多个目录、单独覆盖或停用某个源。
- **人工把关**：匹配结果按页确认，逐首选择版本与音质，默认不自动入队。
- **后台队列**：全局任务栏持续显示匹配、下载、上传和校验进度；任务持久化，重启可续跑。
- **本机浏览器登录**：复用 Chrome / Edge / Chromium 的登录态，不附带浏览器内核。

## 快速开始

需要 **Python 3.11+**；登录需要本机安装 Chrome、Edge 或 Chromium。

Windows：
```bat
start.bat
```
Linux / macOS：
```bash
./start.sh
```

首次启动会自动创建 `.venv` 并安装依赖。服务就绪后自动打开 [http://127.0.0.1:18123](http://127.0.0.1:18123)。

停止服务：
```bat
stop.bat
```
```bash
./stop.sh
```

也可以直接使用命令行：
```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows 用 .venv\Scripts\pip
.venv/bin/python -m music_refill launch     # 静默启动并打开网页
.venv/bin/python -m music_refill serve      # 前台运行，便于看日志
.venv/bin/python -m music_refill doctor     # 环境与音源自检
```

## 工作原理

```text
登录 → 选择歌单 / 搜索歌名
     → 找缺口或音源候选
     → 人工确认版本与音质
     → SQLite 任务队列
     → 下载 → 写入标签 → 上传云盘 → 校验
```

- 缺口判定以播放地址为准，`fee/privilege` 只用于解释原因。
- 匹配优先比较歌名，歌手和时长作为辅助；Live、翻唱等变体会降权。
- 每首歌的音质在候选版本内选择，按“精确 → 更近的更高档 → 更近的更低档”回退。
- 音源搜索按“标题 → 标题 + 歌手 → 标题 + 主歌手”生成查询；多源并行，命中标题后提前停止。
- 下载任务写入 SQLite 后再进入后台队列，服务重启不会丢队列。

## 添加或替换音源

适配器放在 `sources_local/`，每个文件独立注册一个音源。主程序只依赖 `music_refill/sources/base.py` 的协议，不关心具体实现。

1. 复制 `music_refill/sources/_template.py` 为 `sources_local/your_source.py`。
2. 实现 `name`、`quality_ceiling`、`health_check()`、`search()`。
3. 调用 `POST /api/sources/reload`，或重启服务，即可在 `/api/sources` 看到新源。

需要组合或切换时，在 `config.toml` 指定目录：
```toml
[sources]
dirs = ["sources_local", "my_sources"]   # 后者覆盖同名源
enabled = []                             # 空 = 全部启用
disabled = []                            # 临时停用某个源
order = ["kuwo", "migu"]
```

只替换酷我时，新建 `my_sources/kuwo.py` 即可，不需要复制咪咕；改完调用
`POST /api/sources/reload` 让新集合生效，无需重启进程。

适配器搜索失败只会影响自己的候选，不会拖垮其它音源。要实现更快的“先列元信息、再解析下载地址”，可额外提供 `quick_search()` 和 `resolve_many()`；不实现也能工作。

## 配置

复制 `config.example.toml` 为 `config.toml`（本地生效，不入库）。常用项：

```toml
[server]
host = "127.0.0.1"
port = 18123

[scan]
quality = "flac"                 # 默认音质：flac | 320 | 128
limit = 50                       # 单批入队上限
source_search_timeout_s = 30     # 音源搜索总上限
source_search_first_batch_s = 3  # 先返回已完成音源，慢源随后合并

[sources]
dirs = ["sources_local"]
enabled = []
disabled = []
order = ["kuwo", "migu"]

[browser]
executable = ""                  # 留空自动查找 Chrome/Edge
debug_port = 9222
```

完整字段见 `config.example.toml`。

## 数据与安全

- 运行数据放在 `data/`：Cookie、SQLite、日志、下载缓存、报告。`data/` 和 `config.toml` 已加入 `.gitignore`。
- 服务默认只监听 `127.0.0.1`，没有远程鉴权；不要把端口暴露到局域网或公网。
- 登录 Cookie 只保存在本机；日志不输出 Cookie 内容。
- “清除本地缓存”只删除 `data/downloads/`，不会删除云盘歌曲。

## 开发

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试使用临时数据目录，不会读写真实账号数据。

## 目录

```text
music_refill/     核心程序：网易接口、缺口判定、匹配、下载上传、Web API
  netease/        登录、会话、云盘上传
  core/           缺口、打分、下载与标签
  sources/        音源协议与动态加载器
  web/            FastAPI + 单页前端
sources_local/    自带音源适配器
docs/             架构、API、状态与已知限制
tests/            配置、队列、接口与加载器测试
```

## 声明

仅供个人学习与研究使用。请遵守音乐平台服务条款与版权要求，支持正版。使用产生的账号与内容风险由使用者自行承担。
