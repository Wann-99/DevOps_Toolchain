# 分层架构与验证

## 请求流程

应用由 `ksq/cli.py` 读取启动参数、初始化路径和版本状态，再启动单进程 Uvicorn。`ksq/web/app.py:create_app()` 组装 FastAPI 路由和生命周期。

```text
HTTP / WebSocket
  → web/app.py、web/asgi.py、web/routes/*
  → order/service.py、dashboard/service.py、data/service.py、robot/service.py
  → 配置/状态存储、Broker/底盘客户端、文件系统和 Docker
```

接口层保留现有 URL、参数校验、登录/角色限制和响应格式。同步业务放在线程池执行；multipart 使用框架解析，SSE 和文件下载使用流式响应，桌面 WebSocket 通过异步 Unix socket 转发。

`web/handlers.py` 保留公共认证和响应适配，并提供 `QueryHandler` 供原有 HTTP 合约检查使用；实际启动入口已使用 FastAPI。业务模块不依赖 `ksq.web`、FastAPI 或 Starlette，该边界由 `tests/test_architecture.py` 检查。

## 目录职责

- `web/routes/`：订单、仪表板、数据、日志、测试订单、地图、建图与桌面接口。
- `order/service.py`、`order/active.py`、`order/test_service.py`：Broker 操作、活动订单规则和测试订单业务；`model.py` 保存纯数据处理。
- `order/config.py`、`order/store.py`、`order/cache.py`、`order/broker.py`：配置读写、唯一活动订单状态、Token/详情缓存和 Broker HTTP 客户端。
- `dashboard/service.py`：状态聚合与后台监听；`parsing.py`：纯日志解析；`settings.py`：设置及键盘环境文件读写；`cache.py`：快照缓存。
- `data/service.py`、`data/loader.py`、`data/imports.py`：加载、导入和失败回滚；`storage.py`、`workspace.py`、`state.py`：工作副本、备份、编辑和内存状态；`paths.py`、`progress.py`：路径和加载进度。
- `robot/service.py`、`mapping.py`：地图导航与建图业务；`client.py`、`store.py`：底盘 HTTP 访问及设置/停留点持久化；`logs.py`、`keyboard.py`：Docker 日志/服务和键盘交互。
- `web/files_api.py`、`web/host_files.py`、`web/desktop.py`：宿主机文件、终端与桌面连接边界。这些模块继续兼容仅有标准库的 Python 3.8+ 宿主机进程，避免要求现场宿主机安装网页依赖。

项目仍使用已有 JSON/CSV 文件和原子写入，没有引入数据库、ORM、通用 Repository 基类或依赖注入框架。

## 状态和生命周期

- 活动订单及其锁只由 `order/store.py` 持有；单次只允许一个未完成订单，保留现有兼容响应字段。
- 数据集及其锁由 `data/state.py` 持有；Token 与 Broker 详情缓存由 `order/cache.py` 持有，仪表板缓存由 `dashboard/cache.py` 持有。
- Uvicorn 固定 `workers=1`，保持会话、订单和设备操作的进程内互斥。不要直接增加 worker；需要扩容时先迁移共享状态和锁。
- FastAPI lifespan 负责启动备份清理与仪表板监听，退出时关闭终端、停止监听、Docker 日志跟随和备份清理。
- SSE、文件和上游 HTTP 流在结束或客户端断开时关闭资源。桌面连接校验登录会话和同源要求，退出登录后断开连接。
- 工作模式、权限、生产写操作限制、备份保留策略均沿用原有规则；架构调整不解决上游 Broker 账号本身的授权问题。

## 安装和检查

网页运行环境为 Python 3.12。安装开发依赖后执行：

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests
python -m tests.test_files_terminal
python -m tests.test_host_files
node --test tests/test_*.js
```

新增 FastAPI 检查覆盖登录/权限、生产订单模式、错误响应、重复文件上传、SSE、流断开清理、二进制文件传输、生命周期，以及 Unix socket 上 WebSocket 双向传输和会话撤销。业务网络与设备操作使用隔离替身；文件与终端检查只使用临时目录和本地测试进程。

应用包和本地运行镜像可分别构建：

```bash
python deploy/build_app_bin.py <应用版本> --output /tmp/knowledge_shelf_query.bin
docker build -t knowledge-shelf-query-runtime:local .
docker run --rm --network none \
  -v /tmp/knowledge_shelf_query.bin:/opt/ksq/knowledge_shelf_query.bin:ro \
  knowledge-shelf-query-runtime:local \
  python3 /opt/ksq/knowledge_shelf_query.bin --help
```

## 部署兼容性

本次迁移需要 `knowledge_shelf_query_runtime:v1.2.0`，其中包含 `requirements.txt` 固定的运行依赖。必须先构建发布或离线导入新运行镜像，并同步完整部署包的启动脚本、Compose 和 `.bin`。只在旧镜像上替换 `.bin` 会因缺少 FastAPI 等依赖启动失败。

后续运行依赖未变化时，仍只需替换 `.bin`。跨本次迁移回滚时应恢复旧部署包、运行镜像标签和应用包，保留现场配置/数据备份；原有版本切换状态重置机制继续生效。

本机验证不替代 ARM64 设备、真实机器人、Docker 服务控制及图形桌面的现场验收。具体部署步骤见 [部署操作文档](manual/部署操作文档.md)。

## 本次本地验收记录

- 557 项 Python 单元/接口测试、9 项前端检查通过。
- 文件/PTY 与宿主机 Unix socket 集成检查通过，同时在实际 Uvicorn/FastAPI 网络入口复查通过。
- x86_64 运行镜像构建、应用包完整性与镜像内启动检查通过；ARM64 Python 3.12 所需二进制依赖包均可下载。
- 未执行 ARM64 镜像运行、真实机器人或图形桌面验收；本机没有 ARM64 执行环境，也未安装虚拟桌面系统组件。未推送镜像或部署生产。
- 静态检查无新增未定义名称；全仓库仍有 5 项迁移前已有的未使用导入/局部变量提示，未做无关清理。
