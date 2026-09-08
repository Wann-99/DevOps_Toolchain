# DevOps Toolchain

DevOps Toolchain 是面向药房自动化现场的数据查询与运维工具，提供数据加载、药品查询与库位编辑、下单、工单状态跟踪、日志查看、服务控制和现场配置管理。

## 主要功能

- 登录与权限：账号密码登录，区分管理员与普通用户两级角色；文件管理与终端限管理员使用。
- 仪表板：查看当前工单、门店任务列表、子任务进度和实时事件，并处置当前测试工单；子任务名称与库位以 Broker 带回的下单信息为准，日志只提供处理状态与用时。
- 数据加载：一键解析设备 `config_pnp` 目录加载数据，也支持本机路径手工指定、ZIP 数据包或单独配置文件导入；统一复制到 `data/current`，显示进度并备份上一份数据。
- 数据查询：按药品、库位等条件查询和导出，仅库位、货架属性、挡板高度支持编辑；Knowledge、工具、不可处理、闭环吸取配置只读。
- 药品下单：选择药品或扫码下单，最多保留当前单和一张等待单，并查看工单状态和结构化失败详情。
- 测试下单：生成、宽松 CSV 导入（不要求行在候选数据中存在）、导出和提交测试药品列表，按批次统计订单量。
- 订单操作：逐项调用 Broker 接口处置工单任务与门店业务配置，并查看原始响应；写操作仅测试模式可用。
- 日志查询：通过 SSE 实时跟随 `docker logs -f`，并执行启动、停止、重启操作；历史日志损坏时自动跳过并继续获取新日志。
- 地图导航：连接思岚底盘，查看实时地图与雷达，按停留点自动生成虚拟轨道并执行轨道优先巡逻。
- 文件管理：左右卡片分别展示独立操作的宿主机文件目录与交互终端；目录互不跟随，断开后清空终端。Docker 部署通过本机连接进程访问宿主机，使用启动部署脚本的系统账号。管理员可预览文本与图片、上传文件/目录、下载文件/目录 ZIP；可连接已部署的浏览器远程桌面。
- 设置：维护工作模式、下单接口、虚拟键盘和飞书表单配置。

完整功能操作见 [软件使用说明](docs/manual/使用手册.md)。

## 账号与权限

系统要求登录后使用，未登录访问页面会跳转登录页，接口返回 401。

| 角色 | 默认账号 | 权限 |
| --- | --- | --- |
| 管理员 | `admin / noematrix` | 全部操作 |
| 普通用户 | `nvidia / nvidia` | 禁止下列编辑操作，以及文件管理与终端 |

普通用户禁止的操作：

文件管理和交互终端另限管理员使用，包括文件列表、预览与下载，避免通过文件或 Shell 绕过现有权限。该功能无需先加载业务数据。

1. 库位的编辑保存（数据查询页「编辑/保存」）；
2. 设置页的配置保存（下单接口、虚拟键盘、ETM、飞书表单），**工作模式切换除外**——切换后对应模式的配置自动加载，不受权限影响；获取 Token 属下单凭据刷新，普通用户可用；
3. 数据加载页的「导入」方式（本机路径与包加载不受限）。

账号文件为部署目录下的 `config/users.json`（容器内 `/app/users.json`）。新增账号或修改密码：编辑该文件，写入明文 `password` 字段（不要手填 `salt`/`password_hash`），无需重启，首次登录时系统自动迁移为加盐哈希。会话有效期 12 小时（滑动续期），容器重启后需重新登录。

## 运行架构

项目将运行环境与源码分开交付：

| 交付物 | 内容 | 更新时机 |
| --- | --- | --- |
| `knowledge_shelf_query_runtime:v1.1.1` | ARM64 Python 3.12、Pillow（失败日志截图渲染）、Docker CLI、Docker Compose | 运行环境变化时 |
| `knowledge_shelf_query_<版本>.bin` | Python 源码、页面模板和静态资源 | 源码变化时 |

日常更新只替换 `.bin` 文件，无需重新构建或拉取运行镜像。

## 环境要求

- 本地源码运行：Python 3.12。
- 容器部署：Linux ARM64/AArch64、Docker Engine、Docker Compose。
- 宿主机文件与终端：宿主机提供 Python 3.8 或更高版本，仅使用标准库；网页服务的 Python 3.12 仍由运行镜像提供。
- 业务数据：`config_pnp` 目录和包含多个场景目录的 `templates` 根目录。

Python 固定为 3.12，是因为项目当前仍使用该版本提供的标准库 `cgi` 模块。

## 本地运行

```bash
KNOWLEDGE_DIR=/path/to/model/templates \
CONFIG_PNP_DIR=/path/to/config_pnp \
bash start.sh
```

浏览器访问 `http://127.0.0.1:8765`，登录后使用（默认账号见「账号与权限」）。

允许同一可信网络的其他设备通过 IP 访问时，使用 `KSQ_HOST=0.0.0.0 KSQ_PORT=8765 bash start.sh`。直接运行源码/应用包时，文件与终端使用该进程的系统账号。标准 Docker 部署由宿主机连接进程处理文件与终端，默认使用启动部署脚本的账号；通过 `sudo` 启动时使用 `sudo` 前的原账号。网页会显示实际主机和账号，从该账号的主目录打开。文件管理与终端始终独立，访问范围受宿主机账号权限限制。

首次升级宿主机连接功能，需要同步完整部署包中的 `start.sh`、`host-files.sh`、`docker-compose.yml` 和 `.bin`；仅替换 `.bin` 不会增加本机连接挂载。部署脚本自动启动宿主机连接，再启动容器。连接通过权限受限的 `host-files/host.sock` 转发，不额外开放网络端口；连接不可用时明确报错，不回退到容器文件系统。可用 `bash start.sh host-files status` 检查账号和主目录，用 `bash start.sh host-files start` 恢复连接。宿主机重启后需重新执行启动脚本；需要开机自启时，将 `host-files.sh serve` 交给宿主机现有的服务管理器。详见部署说明。

终端文本直接显示在访问设备浏览器内。Docker 部署首次使用图形程序时，在宿主机部署目录执行 `bash start.sh desktop install`、`bash start.sh restart`，然后重新连接网页终端。程序窗口显示在页面“远程桌面”中，使用同一 IP、端口和管理员登录；宿主机无需连接显示器。最小化窗口显示在桌面底部任务栏，也可用 `Alt+Tab` 恢复。安装命令仅支持 Debian/Ubuntu，会安装 TigerVNC、noVNC 等系统组件。已有独立桌面服务仍可通过 `KSQ_DESKTOP_URL` 配置入口。详见 [文件管理与显示转发](docs/manual/部署操作文档.md#十文件管理与显示转发)。

服务自身的运行日志独立写入 `logs/knowledge_shelf_query.log`，同时输出到标准错误。源码和部署包均可执行 `bash start.sh runtime-logs` 查看最近日志并持续跟随。日志按 5 MiB 轮转并保留最近 3 个文件，记录启动信息、HTTP 请求、客户端错误和未处理异常堆栈。它与页面「日志查询」中的机器人服务日志不是同一份日志。

数据源路径按「命令行显式参数 ＞ `config_pnp/config.py` ＞ 内置默认」的优先级确定。`--config-pnp` 指向设备 `config_pnp` 目录后，启动和重新加载时会解析其中的 `config.py`（仅 AST 解析，不执行代码），自动定位库位表、不可处理列表、工具映射和闭环吸取列表；`sku-shelves*.csv` 和 `etm_sku_locations_cache*.csv` 均可识别。在「数据加载」页手工填写的源路径同样优先于 `config.py`。

「本机路径」右上角可选择「本地 / 云」。默认本地，所有文件按路径加载；云模式仅由后台从 `http://127.0.0.1:12005/api/v1/sku/locations` 下载 SKU 库位 CSV，其余文件仍使用本地路径。云模式库位路径为空且不可填写，切回本地恢复原路径。手动加载、一键加载均使用所选模式，查询页重新加载沿用上次成功加载的模式；下载或校验失败保留当前数据，不回退到本地库位表。包加载和文件导入不受该开关影响。

本机路径、一键加载、ZIP、导入和重新加载统一使用应用目录下的 `data/current/knowledge` 与 `data/current/config_pnp` 工作副本，不修改 Knowledge/config_pnp 源目录。新副本复制、解析和校验成功后，上一份数据移至 `data/backups/YYYYMMDD_HHMMSS_ffffff`（`ffffff` 为六位微秒），再切换当前数据；失败时保留原有数据。页面展示上传、复制、解析、备份切换等进度，无法计算百分比的阶段显示等待进度。

每天按服务本地时区在 `00:00` 清理此前的备份，保留最新一份备份和 `data/current`；启动时补清理过期备份，当天生成的备份继续保留。库位编辑仅保存到工作副本，重新加载本机路径会重新复制源文件，覆盖工作副本中的库位修改；需要保留修改时先导出。

`KNOWLEDGE_DIR` 指向宿主机的 `model/templates` 根目录，标准容器将其挂载为 `/data/knowledge`；默认实际读取 `/data/knowledge/knowledge`。页面中的 Knowledge 路径以该根目录为基准，可填写 `knowledge`、场景目录 `pnp_percept/templates_260827` 或其 `.../knowledge` 子目录；填写场景目录时会自动定位 `knowledge` 子目录，不再依赖 VfmApp 的 `config.yaml`。更换挂载根目录时更新该变量并重建容器；只切换根下场景目录时直接在页面填写并加载即可。

旧部署若仍把 `KNOWLEDGE_DIR` 写成 `.../model/templates/knowledge`，启动脚本会在父目录确为 `templates` 时自动提升到新的根目录并给出提示。

## 构建应用包

版本号是应用包的唯一版本来源：

```bash
python3 deploy/build_app_bin.py v1.3.1
```

输出文件：

```text
deploy/dist/knowledge_shelf_query_v1.3.1.bin
```

应用启动时会读取 `.bin` 内的构建元数据，侧栏显示的版本号与构建参数自动保持一致。

生成包含启动脚本和 Compose 配置的完整部署包：

```bash
bash deploy/make_package.sh v1.3.1
```

## ARM64 部署

运行镜像：

```text
hub.noematrix.cn/pharmacy/knowledge_shelf_query_runtime:v1.1.1
```

首次部署：

```bash
tar xzf ksq_deploy_v1.3.1.tar.gz
cd ksq_deploy_v1.3.1
./start.sh
```

直接执行会检查同名旧容器：存在时先停止并移除，再启动当前部署包；不存在时直接启动。

部署目录的 `data/` 通过 `./data:/app/data` 持久化工作副本和备份，启动脚本会自动创建。升级到该目录结构时，必须同步新版 `start.sh` 和 `docker-compose.yml`（或使用完整部署包）；仅替换 `.bin` 不会添加数据挂载。

后续源码更新：

```bash
bash start.sh update /path/to/knowledge_shelf_query_v1.3.2.bin
```

版本检查和回滚：

```bash
bash start.sh version
bash start.sh rollback
```

`update` 更新应用包时会先将四个运行状态文件（`test_order_state.json`、`dashboard_active_order.json`、`order_config.json`、`order_config.prod.json`）备份到 `config/.backup/`，再重置为干净初始值；应用版本变化后的首次启动也会由 `.bin` 内置逻辑自动执行同样的重置，不依赖设备上 `start.sh` 的版本。`dashboard_settings.json` 与 `users.json` 始终保留。也可随时手动重置：

```bash
bash start.sh reset-state
```

完整的在线、离线部署说明见 [部署与更新操作文档](docs/manual/部署操作文档.md)。

## 配置文件

仓库只保存无密钥示例：

- `dashboard_settings.example.json`
- `order_config.example.json`
- `order_config.prod.example.json`
- `devOps/.env.example`

飞书表单规则维护在 `ksq/feishu/rules.json`。新增表单规则时复制一个规则节点，修改规则 ID、显示名称、飞书字段别名和选项映射，然后重新生成部署包。完整部署包会将它复制为 `config/feishu_rules.json` 并挂载到容器 `/app/feishu_rules.json`；现场直接替换该文件后执行 `bash start.sh restart` 即可生效。规则文件在服务启动时严格校验，格式错误会阻止服务启动并在启动日志中说明原因。

部署包的 `config/` 初始为干净值（空状态 + 默认账号），由 `deploy/make_package.sh` 写入、首次启动时 `start.sh` 补齐；运行时配置、账号文件 `users.json`、密码、Token、生成的 `.bin` 和部署压缩包均被 `.gitignore` 排除，不应提交到仓库。

## 后续更新流程

每次发布使用同一个版本号完成构建、验证和 Git 标记：

```bash
VERSION=v1.3.2
python3 deploy/build_app_bin.py "$VERSION"
bash deploy/make_package.sh "$VERSION"

git add -A
git commit -m "release: $VERSION"
git tag -a "$VERSION" -m "DevOps Toolchain $VERSION"
git push origin main --follow-tags
```

源码功能或部署方式变化时，同一提交中同步修改本 README 及 `docs/manual/` 下对应文档。运行镜像未变化时，不需要修改 `Dockerfile` 或运行镜像版本。
