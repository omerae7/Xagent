# ایمیج مشترک API و Worker (پایتون ۳.۱۲، چندمعماری amd64/arm64)
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app/packages/core:/app/packages/agent_core:/app/packages/memory:/app/packages/tools:/app/packages/crm:/app/services/api:/app/services/worker

# git و node فقط برای حالت سبک (بدون کانتینر دسکتاپ) که ابزارها در Worker نصب می‌شوند
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git tini tzdata postgresql-client nodejs npm util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv==0.4.30

ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
COPY pyproject.toml README.md ./
RUN UV_INDEX_URL="$PIP_INDEX_URL" uv pip install --system -r pyproject.toml

COPY packages ./packages
COPY services ./services
COPY infra/docker/entrypoint.sh /usr/local/bin/xagent-entrypoint
RUN chmod +x /usr/local/bin/xagent-entrypoint \
    && useradd -m -u 1001 -s /bin/bash xagent \
    && mkdir -p /data && chown -R xagent:xagent /data /app

USER xagent
VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/tini", "--", "xagent-entrypoint"]
CMD ["api"]
