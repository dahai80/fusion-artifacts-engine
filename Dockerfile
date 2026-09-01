# fusion-artifacts-engine container image
# 设计依据: architecture/fusion-deploy-container-multinode-0901.md §6 Phase 2 item 3
# 引用: docker-compose.business.yml fusion-artifacts-engine: build: ../fusion-artifacts-engine
#
# Artifacts CRUD 中间件 (stdlib http.server + SQLite WAL), 端口 11451。
# 不调用 MLX 推理 (本引擎是 artifact 平台, 非 MLX 客户端), 故不设 FUSION_MLX_URL。
# 调用方若需跨容器访问 MLX, 由各自服务自带 MLX client env, 与本镜像无关。

FROM python:3.12-slim

# 阿里云 PyPI 镜像 (monorepo 约定, 国内构建加速)
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖 (利用 layer cache: pyproject 变动少)
COPY pyproject.toml ./
COPY fusion_artifacts_engine ./fusion_artifacts_engine

# 安装包 + otel 可选依赖 (生产不含 test/ruff)
RUN pip install --no-cache-dir -e ".[otel]"

# 数据卷: SQLite meta.db + content/{art_id}/v{num}.{ext}
RUN mkdir -p /data
VOLUME ["/data"]
ENV FUSION_ARTIFACTS_STORAGE_ROOT=/data \
    FUSION_ARTIFACTS_HOST=0.0.0.0 \
    FUSION_ARTIFACTS_PORT=11451

EXPOSE 11451

# 健康探针: /healthz (liveness, 无鉴权, 永返 200)
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:11451/healthz',timeout=2).status==200 else 1)"

CMD ["fusion-artifacts-engine", "start", "--host", "0.0.0.0", "--port", "11451"]
