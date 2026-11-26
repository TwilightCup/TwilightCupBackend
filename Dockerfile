FROM python:3.14-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52 AS deps
WORKDIR /build
COPY runtime-requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/python -m pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: --index-url https://mirrors.aliyun.com/pypi/simple/ -r runtime-requirements.txt && /opt/venv/bin/python -m pip check

FROM python:3.14-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52
ENV PYTHONPATH=/app/src PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY src/ ./src/
COPY locales/ ./locales/
COPY start.py ./start.py
RUN mkdir logs && chown 65532:65532 logs
USER 65532:65532
EXPOSE 8100
ENTRYPOINT ["/opt/venv/bin/python", "/app/start.py"]
