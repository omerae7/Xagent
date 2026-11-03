# کامپیوتر اختصاصی ایجنت: لینوکس XFCE + Xvfb + x11vnc + noVNC + Chromium (پروفایل پایدار) + Telegram Desktop
# هیچ رمز یا کلیدی از سیستم اصلی وارد این کانتینر نمی‌شود؛ فقط از شبکه داخلی و پشت احراز هویت پنل قابل دسترسی است.
FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 DISPLAY=:1 HOME=/home/agent SCREEN_GEOMETRY=1600x900x24

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc xfce4 xfce4-terminal xfce4-taskmanager dbus-x11 x11-xserver-utils \
        novnc websockify supervisor socat xdotool imagemagick \
        chromium fonts-noto-core fonts-noto-color-emoji fonts-dejavu-core \
        python3 python3-aiohttp python3-pip python3-venv git curl ca-certificates nodejs npm tini procps \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --break-system-packages --no-cache-dir uv==0.4.30 \
    && useradd -m -u 1000 -s /bin/bash agent \
    && mkdir -p /home/agent/work /home/agent/tools /var/log/xagent \
    && chown -R agent:agent /home/agent /var/log/xagent

# بسته‌های اختیاری (اگر در مخزن معماری فعلی موجود نباشند، ساخت متوقف نمی‌شود)
RUN apt-get update \
    && (apt-get install -y --no-install-recommends telegram-desktop || echo "⚠ telegram-desktop در این معماری موجود نیست") \
    && (apt-get install -y --no-install-recommends fonts-vazirmatn || apt-get install -y --no-install-recommends fonts-farsiweb || true) \
    && rm -rf /var/lib/apt/lists/*

COPY agentd.py /opt/xagent/agentd.py
COPY supervisord.conf /etc/supervisor/conf.d/xagent.conf
COPY start-chromium.sh /opt/xagent/start-chromium.sh
RUN chmod +x /opt/xagent/*.sh

VOLUME ["/home/agent"]
EXPOSE 7000 6080 6081 9222
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
