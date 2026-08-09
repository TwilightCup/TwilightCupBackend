FROM python:3.14-slim

# uvloop / orjson 在缺少预编译 wheel 时需要从源码构建
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先拷依赖描述以利用层缓存
COPY pyproject.toml uv.lock ./
COPY README.md ./
COPY src ./src

# 安装运行期依赖（不含 dev）
RUN uv sync --frozen --no-dev --no-install-project

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT [".venv/bin/uvicorn", "twilightcupbackend.main:app", \
            "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop"]
