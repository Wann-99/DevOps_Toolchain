# DevOps Toolchain

DevOps Toolchain 是面向药房自动化现场的数据查询与运维工具，提供数据加载、药品查询与编辑、下单、工单状态跟踪、日志查看、服务控制和现场配置管理。

## 主要功能

- 登录与权限：账号密码登录，区分管理员与普通用户两级角色，普通用户仅禁止编辑类操作。
- 仪表板：查看当前工单、门店任务列表、子任务进度和实时事件，并处置当前测试工单。
- 数据加载：一键解析设备 `config_pnp` 目录加载数据，也支持本机路径手工指定、ZIP 数据包或单独配置文件导入。
- 数据查询：按药品、库位等条件查询，编辑并导出业务数据。
- 药品下单：选择药品或扫码下单，最多保留当前单和一张等待单，并查看工单状态和结构化失败详情。
- 测试下单：生成、宽松 CSV 导入、导出和提交测试药品列表，按批次统计订单量。
- 日志查询：通过 SSE 实时跟随 `docker logs -f`，并执行启动、停止、重启操作；历史日志损坏时自动跳过并继续获取新日志。
- 设置：维护工作模式、下单接口、虚拟键盘和飞书表单配置。

完整功能操作见 [软件使用说明](docs/manual/使用手册.md)。

## 账号与权限

系统要求登录后使用，未登录访问页面会跳转登录页，接口返回 401。

| 角色 | 默认账号 | 权限 |
| --- | --- | --- |
| 管理员 | `admin / noematrix` | 全部操作 |
| 普通用户 | `nvidia / nvidia` | 仅禁止三类编辑操作（见下），其余正常 |

普通用户禁止的操作：

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

日常更新只替换约 200 KiB 的 `.bin` 文件，无需重新构建或拉取运行镜像。

## 环境要求

- 本地源码运行：Python 3.12。
- 容器部署：Linux ARM64/AArch64、Docker Engine、Docker Compose。
- 业务数据：`config_pnp` 目录和 `knowledge` 目录。

Python 固定为 3.12，是因为项目当前仍使用该版本提供的标准库 `cgi` 模块。

## 本地运行

```bash
KNOWLEDGE_DIR=/path/to/knowledge \
CONFIG_PNP_DIR=/path/to/config_pnp \
bash start.sh
```

浏览器访问 `http://127.0.0.1:8765`，登录后使用（默认账号见「账号与权限」）。

数据文件路径按「命令行显式参数 ＞ `config_pnp/config.py` ＞ 内置默认」的优先级确定。`--config-pnp` 指向设备 `config_pnp` 目录后，启动和重新加载时会解析其中的 `config.py`（仅 AST 解析，不执行代码），自动定位库位表、不可处理列表、工具映射和闭环吸取列表；库位表按日期命名（如 `sku-shelves_20260812.csv`）也能识别，无需随文件改名调整配置。在「数据加载」页手工填写或「导入」写入的路径同样优先于 `config.py`。

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
bash start.sh start
```

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
