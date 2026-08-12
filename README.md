# DevOps Toolchain

DevOps Toolchain 是面向药房自动化现场的数据查询与运维工具，提供数据加载、药品查询与编辑、下单、工单状态跟踪、日志查看、服务控制和现场配置管理。

## 主要功能

- 仪表板：查看当前工单、子任务进度、实时事件和人工确认状态。
- 数据加载：从设备目录、ZIP 数据包或单独配置文件加载数据。
- 数据查询：按药品、库位等条件查询，编辑并导出业务数据。
- 药品下单：选择药品或扫码下单，查看和取消工单。
- 测试下单：生成、导入、导出和提交测试药品列表。
- 日志查询：查看相关容器日志并执行启动、停止、重启操作。
- 设置：维护工作模式、下单接口、虚拟键盘和飞书表单配置。

完整功能操作见 [软件使用说明](docs/manual/使用手册.md)。

## 运行架构

项目将运行环境与源码分开交付：

| 交付物 | 内容 | 更新时机 |
| --- | --- | --- |
| `knowledge_shelf_query_runtime:v1.1.0` | ARM64 Python 3.12、Docker CLI、Docker Compose | 运行环境变化时 |
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
SHELVES_FILE=/path/to/config_pnp/sku-shelves.csv \
UNAVAILABLE_FILE=/path/to/config_pnp/unavailabel_obj.json \
TOOL_MAPPING_FILE=/path/to/config_pnp/obj_tool_mapping.json \
PICK_STRATEGY_FILE=/path/to/config_pnp/pick_strategy_obj.json \
bash start.sh
```

浏览器访问 `http://127.0.0.1:8765`。

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
hub.noematrix.cn/pharmacy/knowledge_shelf_query_runtime:v1.1.0
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

完整的在线、离线部署说明见 [部署与更新操作文档](docs/manual/部署操作文档.md)。

## 配置文件

仓库只保存无密钥示例：

- `dashboard_settings.example.json`
- `order_config.example.json`
- `order_config.prod.example.json`
- `devOps/.env.example`

运行时配置、密码、Token、生成的 `.bin` 和部署压缩包均被 `.gitignore` 排除，不应提交到仓库。

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
