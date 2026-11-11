# local-danmaku-service

自托管的 **Emby 弹幕缓存服务**：本地部署、按需匹配、按需缓存，最终把弹幕生成为 `.ass` 外挂字幕交给 Emby 原生加载。

> **当前版本 v0.6.3**
> ✅ 阶段一「媒体库扫描」已完成 —— Emby 连通、按库分页扫描、条目索引、卡片墙控制台、运行日志、定时任务
> ⏳ 阶段二「弹弹play 匹配 / 弹幕拉取 / `.ass` 生成」尚未开始

---

## 它解决什么问题

Emby 本身不带弹幕。常见做法是搭一个弹幕转发服务，但那样需要播放器插件、要把媒体流和弹幕接口都暴露出去。本项目换个思路：

1. **扫描** Emby 媒体库，把剧集与电影建成一张索引表（已完成）
2. **匹配** 每一条用「剧名 + 季 + 集」去弹弹play 查对应的剧集 ID（待实现）
3. **生成** 按需拉取弹幕，转成 `.ass` 文件写回媒体目录，Emby 当作外挂字幕直接加载（待实现）

好处是每集只调用一次 API、不预扫全库拉取、也不对外转发任何接口。

---

## 功能现状

| 模块 | 状态 | 说明 |
|---|---|---|
| Emby 连通与配置 | ✅ | 网页填写地址与 API Key，存本地数据库，支持 IP / 主机名 / 域名 / 反代子路径 |
| 媒体库同步 | ✅ | 自动识别「电影 / 剧集 / 混合」三类库，非媒体库（合集、音乐、家庭视频）自动忽略 |
| 分页扫描 | ✅ | 全量（发现删除）/ 增量（仅近期变更），先探总量再分页，进度从第一秒就是真的 |
| 进度与取消 | ✅ | 按库分项进度，单库失败不影响其他库，可随时取消 |
| 条目索引 | ✅ | `sha1(path|size|runTimeTicks)` 做差量，文件被替换才重新标记为待匹配 |
| 卡片墙控制台 | ✅ | 一部剧一张卡：2:3 海报 + 元信息 + **按季分行**的单集方块（状态着色） |
| 单集抽屉 | ✅ | 集数多的剧卡片只展示前几季，点「查看全部 N 集」展开完整列表 |
| 运行日志 | ✅ | 内存环形缓冲 + 增量接口，级别筛选、复制、下载 |
| 定时任务 | ✅ | 增量 / 全量两套 cron，可在设置页开关与修改 |
| 命令行 | ✅ | 不开网页也能健康检查、同步库、扫描、看统计 |
| 弹弹play 匹配 | ⏳ | 待实现 |
| 弹幕拉取与 `.ass` 生成 | ⏳ | 待实现 |

---

## 快速开始

需要 Python 3.11+。

**Windows**

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run.py
```

**Linux / macOS**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

打开 <http://127.0.0.1:8000>，会进入三步引导：

1. 填 Emby 地址 + API Key —— 支持 `192.168.1.10:8096`、`nas.local:8096`、`https://emby.example.com`，不写协议会自动补 `http://`
2. 勾选要监控的媒体库 —— **同步后默认全部未勾选**，需要手动选
3. 选择是否立即全量扫一次，建立索引

> API Key 在 **Emby 后台 → 设置 → 高级 → API 密钥** 生成。
>
> 地址与密钥只保存在本机 `data/app.db`，不上传、也不需要改配置文件。
> 服务启动时**不会自动扫描，也不会自动请求 Emby** —— 没配好之前一个请求都不发。
> 想重看引导：设置页 →「完全重置」（会连扫描数据一起清掉）。

---

## 界面

**控制台**是左右两栏：

- 左侧：扫描面板（模式选择、进度、按库分项）+ 媒体库内容卡片墙（库筛选 / 搜索 / 分页）
- 右侧：入库统计 + 运行日志（跟随滚动，日志自己内部滚动）

卡片按「一部剧一卡」组织。同一部剧的多个季**分行显示**，季标签 `S1` `S2`，特别篇记作 `SP`，缺季号的条目记为 `—` 并排在最后。每季最多展示 16 个方块，多出的折成 `+N`；超过 3 季会折成一行摘要，避免 97 集的剧把卡片撑爆。

方块颜色对应匹配状态：灰=待匹配、蓝=已匹配、绿=已缓存、红=失败。

**设置页**管 Emby 连接、媒体库勾选、每页条数、定时任务，以及「完全重置 / 清空扫描数据」两个危险操作（二次确认）。

---

## 配置

Emby 地址、API Key、每页条数、定时任务都在网页「设置」里改，存进数据库，**优先级高于 `.env`**。

`.env` 只放进程级参数，也可以给 Emby 两个字段填兜底默认值：

| 键 | 说明 |
|---|---|
| `APP_HOST` / `APP_PORT` | 默认 `127.0.0.1:8000`，只监听本机 |
| `DB_PATH` | 默认 `./data/app.db` |
| `REQUEST_TIMEOUT` | 请求 Emby 的超时秒数，默认 30 |
| `PAGE_SIZE` | 扫描分页大小，默认 500 |
| `SCHEDULER_ENABLED` | 是否启用定时扫描 |
| `SCAN_CRON_INCREMENTAL` / `SCAN_CRON_FULL` | 增量 / 全量 cron，默认每天 03:00、每周日 03:00 |
| `EMBY_BASE_URL` / `EMBY_API_KEY` | 可选兜底，正常不用填 |

复制 `.env.example` 为 `.env` 再改。`.env` 不进版本库。

---

## 命令行

不开网页也能用：

```bash
python -m app.cli health                  # 测试 Emby 连接（读已保存的配置）
python -m app.cli libraries --refresh     # 同步库列表并打印
python -m app.cli scan --mode full        # 扫所有已勾选的库
python -m app.cli scan --mode full --all  # 忽略勾选，扫全部可扫库
python -m app.cli scan --library 1589     # 只扫指定库
python -m app.cli scan --mode incremental # 只拉近期变更
python -m app.cli stats                   # 入库统计
```

---

## API

服务同时是一套干净的 JSON API，方便二次开发或接入脚本。接口文档见 `/docs`（FastAPI 自动生成）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings` | 读取配置（密钥不返回明文） |
| PUT | `/api/settings` | 更新配置，只传需要改的字段 |
| GET | `/api/settings/status` | 引导阶段：`unconfigured` / `no_libraries` / `no_selection` / `ready` |
| POST | `/api/settings/test` | 用传入参数测试 Emby 连通（不落库，短超时） |
| POST | `/api/settings/reset` | 完全重置（配置 + 扫描数据） |
| POST | `/api/settings/purge` | 只清空扫描数据 |
| POST | `/api/libraries/refresh` | 同步 Emby 库列表并探量 |
| GET | `/api/libraries` | 库列表（只含媒体库） |
| PATCH | `/api/libraries/{id}` | 启用 / 停用某个库 |
| POST | `/api/scan` | 启动扫描 `{mode: "full"\|"incremental"}` |
| GET | `/api/scan/current` | 当前是否有任务在跑 |
| GET | `/api/scan/runs` | 历史扫描记录 |
| POST | `/api/scan/{id}/cancel` | 取消 |
| GET | `/api/media/groups` | 卡片墙数据：按剧聚合 + 分页，参数 `page` `page_size` `q` `library_id` |
| GET | `/api/media` | 平铺的条目列表 |
| GET | `/api/media/cover/{id}` | 服务端代理 Emby 海报（不暴露 API Key） |
| GET | `/api/media/stats` | 入库统计 |
| GET | `/api/logs?after=<seq>` | 增量运行日志，前端只拉新条目 |

---

## 设计要点

- **扫描只做发现**：不匹配、不算 MD5、不碰弹幕。匹配是独立阶段，可以反复重跑。
- **删除标记只在 full 模式做**，且严格限定在本次扫描过的库作用域内；增量扫描不删任何东西。
- **先探总量再分页**：进度条从第一秒就是真的，不会中途倒退。
- **单库失败不影响其他库**：`scan_run_library` 逐库记录状态与错误。
- **单进程单 worker**：任务状态与 SQLite 都在进程内，多 worker 会重复跑。SQLite 已开 WAL。
- **日志只放内存**：运维信息写进业务库会越滚越大，扫描失败原因在 `scan_run.error` 里已有一份。代价是重启后日志清空。
- **海报走服务端代理**：`<img>` 不直接指向 Emby，API Key 不进前端。
- **静态资源带 `?v=`**：改前端必须 bump `app/__init__.py` 的 `__version__`，否则浏览器缓存命中旧文件。

---

## 数据表

| 表 | 用途 |
|---|---|
| `library` | 媒体库（Emby ItemId 为主键，改名不影响） |
| `media_item` | 条目索引，含匹配 / 弹幕状态的占位字段 |
| `series_meta` | 剧集元信息（TMDB / TVDB / IMDB ID），用于把同名剧正确合并成一张卡 |
| `scan_run` | 每次扫描的汇总与进度 |
| `scan_run_library` | 每次扫描中每个库的分项进度 |
| `app_setting` | 网页设置，覆盖 `.env` |

---

## 目录结构

```
app/
├── main.py          FastAPI 入口、生命周期、路由挂载
├── config.py        .env 配置
├── runtime.py       DB 配置覆盖 .env、Emby 客户端工厂
├── db.py            异步引擎、建表与迁移
├── models.py        SQLAlchemy 模型
├── logs.py          内存日志缓冲 + 标准 logging 桥
├── library_sync.py  同步 Emby 库列表
├── emby/client.py   Emby API 封装
├── jobs/scanner.py  扫描任务
├── jobs/scheduler.py 定时任务
├── api/             REST 接口
└── web/             Jinja2 单页 + 原生 JS/CSS
```

---

## 已知限制

- 卡片高度由海报决定，只有 1–3 集的短剧右侧会留白（预留作后期的单集配置区）。
- 日志不持久化，重启即清空。
- 增量扫描只更新，不删除条目；要发现删除必须跑一次全量。
- Emby 返回的是容器内路径，本机不可读，因此阶段二只能走元数据匹配，不能用文件 MD5。
- 目前只支持单个 Emby 实例。

---

## 合规与边界

弹弹play 开放 API 自 2026-11 起收紧了接入政策，明令禁止「批量下载弹幕」。本项目的立场：

- **按需缓存**：用户点哪一集才拉哪一集，不预扫全库。
- **生成字幕后交付**：产物是 `.ass` 文件，不做对外转发端点，不二次分发 API。
- **不内置任何凭证**：AppId / AppSecret 由使用者自行申请并填入本地配置。

使用前请自行确认所在地区的版权要求，以及弹弹play 的接入条款。

---

## 路线图

- [ ] 弹弹play 签名鉴权与剧集匹配（`/api/v2/search/episodes`）
- [ ] 单集抽屉里接入「匹配 / 重匹配 / 手动指定剧集」
- [ ] 弹幕拉取与 `.ass` 生成，写回媒体目录
- [ ] 批量操作（按库、按季、按状态）
- [ ] 卡片密度切换（海报墙 / 紧凑列表）
