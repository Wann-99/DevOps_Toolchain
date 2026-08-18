# Stable runtime only. Application code is mounted as knowledge_shelf_query.bin.
# Python 3.12 is pinned because the application still uses the stdlib cgi module,
# which was removed in Python 3.13.
FROM docker:27.5.1-cli AS docker-cli

FROM python:3.12.13-slim-bookworm

LABEL org.opencontainers.image.title="knowledge_shelf_query runtime" \
      org.opencontainers.image.description="Python and Docker CLI runtime without application source" \
      org.opencontainers.image.version="1.1.1"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

WORKDIR /app

RUN mkdir -p /opt/ksq

# Pillow：失败工单的日志截图渲染（飞书附件）依赖它。
RUN pip3 install --no-cache-dir pillow==10.4.0

# 文泉驿正黑：截图里的中文靠它渲染（slim 基础镜像不带任何字体）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# The dashboard manages sibling containers through the host Docker socket.
# Keep the matching CLI and Compose plugin in the runtime image so deployment
# does not depend on host binary paths.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose \
    /usr/local/libexec/docker/cli-plugins/docker-compose

EXPOSE 8765

# /api/status 需要登录（401 会误报 unhealthy）；/api/health 是公开端点。
HEALTHCHECK --interval=10s --timeout=3s --start-period=60s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=2)"]

# CMD 旧参数（由 config_pnp 目录驱动，不再逐项指定）:
#   --shelves /data/config_pnp/sku-shelves.csv
#   --unavailable /data/config_pnp/unavailabel_obj.json
#   --tool-mapping /data/config_pnp/obj_tool_mapping.json
#   --pick-strategy /data/config_pnp/pick_strategy_obj.json
CMD ["python3", "/opt/ksq/knowledge_shelf_query.bin", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--config-pnp", "/data/config_pnp", \
     "--knowledge", "/data/knowledge"]
