# Dedicated sandbox for untrusted CLI/MCP tools. Does NOT mount browser profiles or worker data.
FROM debian:bookworm-slim
ENV HOME=/home/agent LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-aiohttp python3-pip python3-venv git curl ca-certificates nodejs npm tini procps \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --break-system-packages --no-cache-dir uv==0.4.30 \
    && useradd -m -u 1000 -s /bin/bash agent \
    && mkdir -p /home/agent/work /home/agent/tools \
    && chown -R agent:agent /home/agent
COPY infra/desktop/agentd.py /opt/xagent/agentd.py
USER agent
EXPOSE 7000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/python3", "/opt/xagent/agentd.py"]
