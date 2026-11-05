#!/usr/bin/env python3
"""xagent-hostd — کارگزار فرمان میزبان X Agent (Host Command Broker).

اصول امنیتی:
- فقط سوکت یونیکس (بدون پورت شبکه) در $XAGENT_HOME/run/hostd؛ فقط کانتینر API آن را (فقط‌خواندنی) mount می‌کند.
- احراز هویت سه‌لایه: uid فرایند متصل (SO_PEERCRED)، امضای HMAC هر درخواست با کلید مشتق از XAGENT_SECRET_KEY،
  و برای سطح ۳ «مجوز امضاشده» (grant) که فقط پس از تأیید کارفرما صادر می‌شود، یک‌بارمصرف و با زمان انقضا.
- هیچ متن آزادی اجرا نمی‌شود: فقط عملیات ساختاریافته از Allowlist با آرگومان‌های اعتبارسنجی‌شده و بدون Shell.
- محدودیت زمان، حجم خروجی، تعداد اجرای هم‌زمان و قفل انحصاری برای عملیات تغییردهنده.
- اسرار (.env و الگوهای کلید) از خروجی حذف می‌شوند؛ هر عملیات در گزارش حسابرسی میزبان ثبت می‌شود.
- هر عملیات در فرایند جدا (Session مستقل) اجرا می‌شود و با خرابی/ری‌استارت این سرویس از بین نمی‌رود.

اجرا: systemd (xagent-hostd.socket ← xagent-hostd.service) یا برای آزمون: xagent-hostd --config conf.json
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

VERSION = "1"
MAX_OUTPUT = 512 * 1024
MAX_REQUEST = 64 * 1024
CLOCK_SKEW = 120

# ───────────────────────── پیکربندی ─────────────────────────


def load_config(path: str | None) -> dict:
    home = "/opt/xagent"
    if os.path.exists("/etc/xagent.conf"):
        for line in open("/etc/xagent.conf", encoding="utf-8"):
            if line.startswith("XAGENT_HOME="):
                home = line.split("=", 1)[1].strip()
    cfg = {
        "home": home,
        "run_dir": f"{home}/run/hostd",
        "env_file": f"{home}/app/.env",
        "app_dir": f"{home}/app",
        "allowed_uids": [0, 1001],
        "max_concurrent": 3,
        "bin": {"docker": "/usr/bin/docker", "xagentctl": "/usr/local/lib/xagent/xagentctl",
                "journalctl": "/usr/bin/journalctl", "systemctl": "/usr/bin/systemctl"},
        "audit_log": "/var/log/xagent-hostd.log",
    }
    if path:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        bins = {**cfg["bin"], **user.pop("bin", {})}
        cfg.update(user)
        cfg["bin"] = bins
    cfg["jobs_dir"] = os.path.join(cfg["run_dir"], "jobs")
    cfg["socket"] = cfg.get("socket") or os.path.join(cfg["run_dir"], "hostd.sock")
    return cfg


def read_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def derive(secret: str, label: bytes) -> bytes:
    return hmac.new(secret.encode(), label, hashlib.sha256).digest()


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# ───────────────────────── پاک‌سازی اسرار ─────────────────────────
_PATTERNS = [
    (re.compile(r"\b(sk-(?:ant-|proj-|or-v1-|svcacct-)?[A-Za-z0-9_\-]{8,})"), "••••"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "••••"),
    (re.compile(r"\b(?:xai|gsk|pplx|nvapi|hf)[-_][A-Za-z0-9_\-]{12,}"), "••••"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"), "••••"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 ••••"),
    (re.compile(r"(?i)((?:api[_-]?key|password|passwd|secret|token)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{6,}"), r"\1••••"),
    (re.compile(r"([a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s/]+@"), r"\1••••@"),
]
_SECRET_KEY = re.compile(r"(SECRET|KEY|TOKEN|PASSWORD|PASS|HASH|API)", re.I)


class Redactor:
    def __init__(self, env: dict[str, str]):
        self.values = sorted({v for k, v in env.items() if _SECRET_KEY.search(k) and len(v) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for v in self.values:
            if v in text:
                text = text.replace(v, "••••")
        for rx, rep in _PATTERNS:
            text = rx.sub(rep, text)
        return text


# ───────────────────────── اعتبارسنجی آرگومان‌ها ─────────────────────────
SERVICES_APP = ["api", "worker", "web", "runner", "desktop"]
SERVICES_DATA = ["postgres", "redis"]
SERVICES_ALL = SERVICES_APP + SERVICES_DATA
UNITS = ["docker", "containerd", "xagent-hostd", "xagent-updater", "ssh", "cron"]
RE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,99}$")
RE_HOST = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9.\-]{1,253}(?<!-)$")
RE_PKG = re.compile(r"^[a-z0-9][a-z0-9+.\-]{1,62}$")
RE_URL = re.compile(r"^https?://[A-Za-z0-9.\-]{1,253}(:\d{1,5})?(/[A-Za-z0-9._~%!$&'()*+,;=:@/\-]*)?(\?[A-Za-z0-9._~%!$&'()*+,;=:@/?\-]*)?$")


class BadArgs(ValueError):
    pass


def a_enum(v, choices, name):
    if v not in choices:
        raise BadArgs(f"{name} must be one of {choices}")
    return v


def a_int(v, lo, hi, name, default=None):
    if v is None:
        if default is None:
            raise BadArgs(f"{name} required")
        return default
    if isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):
        raise BadArgs(f"{name} must be integer in [{lo},{hi}]")
    return v


def a_re(v, rx, name, required=True):
    if v is None and not required:
        return None
    if not isinstance(v, str) or not rx.match(v) or v.startswith("-"):
        raise BadArgs(f"invalid {name}")
    return v


# ───────────────────────── عملیات ─────────────────────────
# هر عملیات: (سطح، شرح، تابع ساخت گام‌ها، کلیدهای مجاز آرگومان، زمان مجاز)
# گام: ("exec", argv, timeout) | ("builtin", name, args) | ("wait_healthy", service, timeout)


def dc(cfg) -> list[str]:
    app = cfg["app_dir"]
    return [cfg["bin"]["docker"], "compose", "--project-directory", app, "-f", f"{app}/compose.yaml", "--env-file", f"{app}/.env"]


def op_table(cfg) -> dict:
    X = cfg["bin"]["xagentctl"]
    J = cfg["bin"]["journalctl"]
    S = cfg["bin"]["systemctl"]
    D = cfg["bin"]["docker"]

    def ex(*argv, t=60):
        return ("exec", [str(a) for a in argv], t)

    T = {
        # ── سطح ۱: مشاهده و تشخیص ──
        "system.status": (1, "وضعیت CPU، حافظه، دیسک، شبکه و بار سیستم", lambda a: [("builtin", "system_status", {})], [], 30),
        "services.status": (1, "وضعیت کانتینرها و سرویس‌های systemd", lambda a: [
            ex(*dc(cfg), "ps", "--all", "--format", "table {{.Service}}\t{{.State}}\t{{.Status}}\t{{.Image}}"),
            ("builtin", "units_status", {})], [], 60),
        "logs.service": (1, "لاگ یک سرویس Docker", lambda a: [ex(
            *dc(cfg), "logs", "--no-color", "--tail", a_int(a.get("lines"), 1, 1000, "lines", 200),
            a_enum(a.get("service"), SERVICES_ALL, "service"))], ["service", "lines"], 60),
        "logs.unit": (1, "لاگ سرویس systemd", lambda a: [ex(
            J, "-u", a_enum(a.get("unit"), UNITS, "unit"), "-n", a_int(a.get("lines"), 1, 1000, "lines", 200),
            "--no-pager", "-o", "short-iso")], ["unit", "lines"], 60),
        "logs.update": (1, "گزارش آخرین به‌روزرسانی", lambda a: [("builtin", "update_log", {})], [], 15),
        "xagent.status": (1, "xagent status", lambda a: [ex(X, "status")], [], 90),
        "xagent.doctor": (1, "xagent doctor", lambda a: [ex(X, "doctor")], [], 90),
        "xagent.version": (1, "xagent version", lambda a: [ex(X, "version")], [], 30),
        "update.check": (1, "بررسی نسخه جدید و Preflight (Dry Run، بدون هیچ تغییری)", lambda a: [ex(
            X, "update", "--dry-run", *(["--ref", a_re(a["ref"], RE_REF, "ref")] if a.get("ref") else []), t=600)],
            ["ref"], 600),
        "diag.disk": (1, "df -h", lambda a: [ex("df", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay")], [], 30),
        "diag.memory": (1, "free -m", lambda a: [ex("free", "-m")], [], 30),
        "diag.processes": (1, "پرمصرف‌ترین فرایندها", lambda a: [("builtin", "top_processes", {})], [], 30),
        "diag.ports": (1, "پورت‌های در حال گوش‌دادن", lambda a: [ex("ss", "-tlnp")], [], 30),
        "diag.network": (1, "آدرس‌ها و مسیرهای شبکه", lambda a: [ex("ip", "-br", "addr"), ex("ip", "route")], [], 30),
        "diag.docker_stats": (1, "مصرف منابع کانتینرها", lambda a: [ex(D, "stats", "--no-stream")], [], 60),
        "diag.docker_df": (1, "فضای Docker", lambda a: [ex(D, "system", "df")], [], 60),
        "diag.failed_units": (1, "سرویس‌های ناموفق systemd", lambda a: [ex(S, "--failed", "--no-pager")], [], 30),
        "diag.errors": (1, "خطاهای اخیر سیستم", lambda a: [ex(J, "-p", "err", "-n", "100", "--no-pager")], [], 30),
        "diag.kernel": (1, "هشدارهای کرنل", lambda a: [ex("dmesg", "-T", "--level=err,warn")], [], 30),
        "diag.os": (1, "سیستم‌عامل و کرنل", lambda a: [ex("uname", "-a"), ("builtin", "os_release", {})], [], 15),
        "diag.time": (1, "زمان و همگام‌سازی", lambda a: [ex("timedatectl")], [], 15),
        "diag.dns": (1, "حل نام دامنه", lambda a: [ex("getent", "ahosts", a_re(a.get("host"), RE_HOST, "host"))], ["host"], 20),
        "diag.ping": (1, "ping", lambda a: [ex("ping", "-c", "3", "-W", "2", a_re(a.get("host"), RE_HOST, "host"))], ["host"], 20),
        "diag.http": (1, "بررسی پاسخ HTTP یک نشانی عمومی", lambda a: [ex(
            "curl", "-q", "--noproxy", "*", "-sS", "-o", "/dev/null", "--proto", "=http,https",
            "--max-redirs", "0", "--max-time", "10", "-w", "HTTP %{http_code} in %{time_total}s\n",
            *_public_http_args(a.get("url")))], ["url"], 20),
        # ── سطح ۲: اصلاح کم‌ریسک (خودکار مشروط، با سقف تعداد) ──
        "service.restart": (2, "Restart سرویس برنامه و بررسی سلامت", lambda a: [
            ex(*dc(cfg), "restart", a_enum(a.get("service"), SERVICES_APP, "service"), t=120),
            ("wait_healthy", a["service"], 150)], ["service"], 300),
        "xagent.backup": (2, "پشتیبان‌گیری دیتابیس", lambda a: [ex(X, "backup", t=1800)], [], 1800),
        "docker.prune": (2, "پاک‌سازی ایمیج‌های بلااستفاده", lambda a: [
            ex(D, "image", "prune", "-f"), ex(D, "builder", "prune", "-f", "--filter", "until=168h")], [], 600),
        "logs.vacuum": (2, "کوچک‌کردن لاگ‌های systemd", lambda a: [ex(J, "--vacuum-size=500M")], [], 120),
        # ── سطح ۳: فقط با تأیید کارفرما ──
        "xagent.update": (3, "به‌روزرسانی هسته (xagent update)", lambda a: [ex(
            X, "update", "--yes", *(["--ref", a_re(a["ref"], RE_REF, "ref")] if a.get("ref") else []), t=3600)],
            ["ref"], 3600),
        "xagent.rollback": (3, "بازگشت به نسخه قبلی (xagent rollback)", lambda a: [ex(X, "rollback", "--yes", t=1800)], [], 1800),
        "service.restart_data": (3, "Restart دیتابیس یا Redis", lambda a: [
            ex(*dc(cfg), "restart", a_enum(a.get("service"), SERVICES_DATA, "service"), t=180),
            ("wait_healthy", a["service"], 180)], ["service"], 400),
        "stack.restart": (3, "Restart همه سرویس‌ها", lambda a: [ex(*dc(cfg), "restart", t=600)], [], 700),
        "system.apt_install": (3, "نصب بسته سیستمی", lambda a: [
            ex("apt-get", "update", t=600),
            ex("apt-get", "install", "-y", "--no-install-recommends",
               *[a_re(p, RE_PKG, "package") for p in _pkg_list(a.get("packages"))], t=1800)], ["packages"], 2400),
        "system.security_updates": (3, "نصب به‌روزرسانی‌های امنیتی سیستم‌عامل", lambda a: [
            ex("apt-get", "update", t=600), ex("unattended-upgrade", "-v", t=3000)], [], 3600),
        "firewall.allow": (3, "باز کردن پورت در فایروال (ufw)", lambda a: [ex(
            "ufw", "allow", f"{a_int(a.get('port'), 1, 65535, 'port')}/{a_enum(a.get('proto', 'tcp'), ['tcp', 'udp'], 'proto')}")],
            ["port", "proto"], 60),
        "firewall.deny": (3, "بستن پورت در فایروال (ufw)", lambda a: [ex(
            "ufw", "deny", f"{a_int(a.get('port'), 1, 65535, 'port')}/{a_enum(a.get('proto', 'tcp'), ['tcp', 'udp'], 'proto')}")],
            ["port", "proto"], 60),
        "system.reboot": (3, "راه‌اندازی مجدد سرور (با یک دقیقه تأخیر)", lambda a: [ex("shutdown", "-r", "+1", "X Agent: approved reboot")],
                          [], 30),
        "exec.approved": (3, "اجرای یک فرمان خارج از Allowlist (فقط با تأیید، یک‌بار، محدود به زمان)", lambda a: [
            ("exec_approved", _argv(a.get("argv")), {"run_as": a_enum(a.get("run_as", "nobody"), ["nobody", "root"], "run_as"),
                                                    "timeout": a_int(a.get("timeout"), 1, 600, "timeout", 120)})],
            ["argv", "run_as", "timeout"], 660),
    }
    return T


def _public_url(v):
    """فقط نام دامنه عمومی؛ آدرس IP، localhost و نام‌های داخلی رد می‌شوند (جلوگیری از کاوش شبکه داخلی)."""
    a_re(v, RE_URL, "url")
    host = re.sub(r"^https?://", "", v).split("/")[0].split("?")[0].split(":")[0].lower()
    if (re.fullmatch(r"[0-9.]+", host) or "." not in host or host == "localhost"
            or host.endswith((".local", ".internal", ".localhost", ".lan"))):
        raise BadArgs("only public domain names are allowed")
    return v


def _public_http_args(v):
    """Resolve the destination once and pin curl to a vetted public IP.

    Curl does not follow redirects (no -L), cannot use configured proxies (-q,
    --noproxy), and keeps the original hostname for TLS SNI and Host validation.
    Reject mixed DNS records, including any private answer, rather than picking
    only the public records and overlooking a DNS-rebinding candidate.
    """
    url = _public_url(v)
    parsed = urlsplit(url)
    host = parsed.hostname
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise BadArgs("invalid HTTP port") from exc
    if not host or not 1 <= port <= 65535:
        raise BadArgs("invalid HTTP destination")
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise BadArgs("destination DNS resolution failed") from exc
    valid = []
    for family, _type, _proto, _canon, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise BadArgs("invalid DNS address") from exc
        if (not ip.is_global or getattr(ip, "ipv4_mapped", None)
                or getattr(ip, "sixtofour", None) or getattr(ip, "teredo", None)):
            raise BadArgs("destination resolves to a non-public IP")
        valid.append(ip)
    if not valid:
        raise BadArgs("no public destination address")
    address = str(valid[0])
    if valid[0].version == 6:
        address = f"[{address}]"
    return ["--resolve", f"{host}:{port}:{address}", url]


def _pkg_list(v):
    if not isinstance(v, list) or not (1 <= len(v) <= 5):
        raise BadArgs("packages must be a list of 1..5 names")
    return v


def _argv(v):
    if not isinstance(v, list) or not (1 <= len(v) <= 32):
        raise BadArgs("argv must be a list of 1..32 strings")
    for x in v:
        if not isinstance(x, str) or not x or len(x) > 512 or any(ord(c) < 32 for c in x):
            raise BadArgs("invalid argv element")
    return v


def build_steps(cfg, op: str, args: dict):
    T = op_table(cfg)
    if op not in T:
        raise BadArgs(f"unknown op: {op}")
    level, _desc, fn, allowed, tmax = T[op]
    extra = set(args) - set(allowed)
    if extra:
        raise BadArgs(f"unexpected args: {sorted(extra)}")
    return level, fn(args), tmax


def catalog(cfg) -> list[dict]:
    return [{"op": k, "level": v[0], "description": v[1], "args": v[3], "timeout": v[4]} for k, v in op_table(cfg).items()]


# ───────────────────────── عملیات داخلی (بدون فرایند خارجی) ─────────────────────────


def _cpu_sample():
    with open("/proc/stat") as f:
        p = [int(x) for x in f.readline().split()[1:]]
    return sum(p), p[3] + (p[4] if len(p) > 4 else 0)


def builtin(cfg, name: str, write):
    if name == "system_status":
        t1, i1 = _cpu_sample()
        time.sleep(0.5)
        t2, i2 = _cpu_sample()
        cpu = 100.0 * (1 - (i2 - i1) / max(1, t2 - t1))
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
        load = open("/proc/loadavg").read().split()[:3]
        up = float(open("/proc/uptime").read().split()[0])
        write(f"CPU: {cpu:.1f}%  ({os.cpu_count()} cores)  Load: {' '.join(load)}\n")
        used = mem["MemTotal"] - mem.get("MemAvailable", mem.get("MemFree", 0))
        write(f"RAM: {used / 2**30:.2f} / {mem['MemTotal'] / 2**30:.2f} GiB ({100 * used / mem['MemTotal']:.0f}%)\n")
        sw_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
        write(f"Swap: {sw_used / 2**30:.2f} / {mem.get('SwapTotal', 0) / 2**30:.2f} GiB\n")
        for path in ("/", cfg["home"]):
            try:
                st = os.statvfs(path)
                tot, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
                write(f"Disk {path}: {(tot - free) / 2**30:.1f} / {tot / 2**30:.1f} GiB ({100 * (tot - free) / max(1, tot):.0f}%)\n")
            except OSError:
                pass
        try:
            for line in open("/proc/net/dev").read().splitlines()[2:]:
                iface, data = line.split(":", 1)
                iface = iface.strip()
                if iface == "lo" or iface.startswith(("veth", "br-", "docker")):
                    continue
                f = data.split()
                write(f"Net {iface}: rx {int(f[0]) / 2**20:.1f} MiB, tx {int(f[8]) / 2**20:.1f} MiB\n")
        except OSError:
            pass
        write(f"Uptime: {int(up // 86400)}d {int(up % 86400 // 3600)}h {int(up % 3600 // 60)}m\n")
        return 0
    if name == "units_status":
        sc = cfg["bin"]["systemctl"]
        if not shutil.which(sc) and not os.path.exists(sc):
            write("systemd: در دسترس نیست\n")
            return 0
        for u in ("docker", "xagent-hostd.socket", "xagent-updater.path"):
            r = subprocess.run([sc, "is-active", u], capture_output=True, text=True, timeout=10)
            write(f"{u}: {r.stdout.strip() or r.stderr.strip()}\n")
        return 0
    if name == "update_log":
        path = os.path.join(cfg["home"], "data", "control", "update.log")
        try:
            data = open(path, encoding="utf-8", errors="replace").read()[-60000:]
        except OSError:
            data = "گزارش به‌روزرسانی موجود نیست.\n"
        write(re.sub(r"\x1b\[[0-9;]*m", "", data))
        return 0
    if name == "top_processes":
        r = subprocess.run(["ps", "-eo", "pid,user,%cpu,%mem,etime,comm", "--sort=-%cpu"], capture_output=True, text=True,
                           timeout=15)
        write("\n".join(r.stdout.splitlines()[:30]) + "\n")
        return r.returncode
    if name == "os_release":
        try:
            write(open("/etc/os-release").read())
        except OSError:
            pass
        return 0
    write(f"unknown builtin {name}\n")
    return 2


# ───────────────────────── اجراکننده کار (فرایند جدا) ─────────────────────────


def _jobs_fd(cfg) -> int:
    """پوشه کارها: باید متعلق به همین کاربر و غیرقابل‌نوشتن برای دیگران باشد (ضد حمله پیوند نمادین)."""
    fd = os.open(cfg["jobs_dir"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        os.close(fd)
        raise PermissionError("jobs dir ownership/mode is unsafe")
    return fd


def _write_json(dfd: int, name: str, obj: dict) -> None:
    tmp = f".{name}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)


def _read_json(dfd: int, name: str) -> dict | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError:
            return None


def run_job(cfg: dict, job: str, spec: dict) -> None:
    """در فرایند جدا: گام‌ها را اجرا، خروجی پاک‌سازی‌شده را با سقف حجم بنویس و وضعیت نهایی را ثبت کن."""
    dfd = _jobs_fd(cfg)
    redact = Redactor(read_env(cfg["env_file"]))
    out_fd = os.open(f"{job}.out", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
    state = {**spec, "status": "running", "pid": os.getpid(), "started_at": time.time(), "bytes": 0, "truncated": False}
    _write_json(dfd, f"{job}.json", state)
    written = 0
    lock = threading.Lock()

    def write(text: str) -> None:
        nonlocal written
        data = redact(text).encode("utf-8", "replace")
        with lock:
            if written >= MAX_OUTPUT:
                state["truncated"] = True
                return
            data = data[: MAX_OUTPUT - written]
            os.write(out_fd, data)
            written += len(data)
            if written >= MAX_OUTPUT:
                state["truncated"] = True
                os.write(out_fd, "\n[… خروجی به سقف مجاز رسید و کوتاه شد]\n".encode())

    child: dict = {"p": None}
    cancelled = {"v": False}

    def on_term(_s, _f):
        cancelled["v"] = True
        p = child["p"]
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    signal.signal(signal.SIGTERM, on_term)
    deadline = time.monotonic() + int(spec.get("timeout", 60))
    code = 0
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
           "DEBIAN_FRONTEND": "noninteractive", "HOME": "/root" if os.geteuid() == 0 else os.environ.get("HOME", "/tmp")}
    if cfg.get("xagent_conf"):
        env["XAGENT_CONF"] = cfg["xagent_conf"]
    try:
        _, steps, _ = build_steps(cfg, spec["op"], spec.get("args") or {})
        for step in steps:
            if cancelled["v"]:
                code = 130
                break
            kind = step[0]
            if kind == "builtin":
                code = builtin(cfg, step[1], write)
            elif kind == "wait_healthy":
                code = _wait_healthy(cfg, step[1], min(step[2], max(1, deadline - time.monotonic())), write, cancelled)
            elif kind in ("exec", "exec_approved"):
                argv = list(step[1])
                limit = step[2] if kind == "exec" else step[2]["timeout"]
                if kind == "exec_approved" and step[2]["run_as"] == "nobody":
                    argv = ["runuser", "-u", "nobody", "--", *argv]
                write(f"$ {' '.join(argv)}\n")
                remaining = max(1, min(limit, deadline - time.monotonic()))
                try:
                    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         env=env, cwd="/", close_fds=True)
                except FileNotFoundError:
                    write(f"برنامه یافت نشد: {argv[0]}\n")
                    code = 127
                    break
                child["p"] = p
                timer = threading.Timer(remaining, lambda pp=p: pp.poll() is None and pp.kill())
                timer.start()
                for raw in iter(p.stdout.readline, b""):
                    write(raw.decode("utf-8", "replace"))
                p.wait()
                timer.cancel()
                code = p.returncode
                if code == -9 and not cancelled["v"]:
                    write(f"\n[زمان مجاز ({int(remaining)} ثانیه) تمام شد و فرایند متوقف شد]\n")
                    state["status"] = "timeout"
                    code = 124
            if code != 0:
                break
    except BadArgs as e:
        write(f"آرگومان نامعتبر: {e}\n")
        code = 2
    except Exception as e:  # noqa: BLE001
        write(f"خطای اجرای کار: {type(e).__name__}: {e}\n")
        code = 1
    status = "cancelled" if cancelled["v"] else state.get("status") if state.get("status") == "timeout" else (
        "succeeded" if code == 0 else "failed")
    state.update({"status": status, "exit_code": code, "finished_at": time.time(), "bytes": written})
    os.close(out_fd)
    _write_json(dfd, f"{job}.json", state)
    os.close(dfd)


def _wait_healthy(cfg, service: str, timeout: float, write, cancelled) -> int:
    write(f"⏳ انتظار برای سالم شدن «{service}»…\n")
    end = time.monotonic() + timeout
    last = ""
    while time.monotonic() < end and not cancelled["v"]:
        r = subprocess.run([*dc(cfg), "ps", "--format", "json", service], capture_output=True, text=True, timeout=30)
        try:
            rows = [json.loads(x) for x in r.stdout.splitlines() if x.strip().startswith("{")]
            if not rows and r.stdout.strip().startswith("["):
                rows = json.loads(r.stdout)
        except ValueError:
            rows = []
        if rows:
            st = rows[0]
            health = (st.get("Health") or "").lower()
            state = (st.get("State") or "").lower()
            last = f"{state}/{health or '-'}"
            if state == "running" and health in ("", "healthy"):
                write(f"✓ «{service}» سالم است ({last})\n")
                return 0
        time.sleep(2)
    write(f"✗ «{service}» در زمان مجاز سالم نشد (آخرین وضعیت: {last or 'نامشخص'})\n")
    return 1


# ───────────────────────── سرور ─────────────────────────


class Broker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.nonces: dict[str, float] = {}
        self.lock = threading.Lock()
        self._env_stamp = None
        self._keys = (b"", b"")
        self.used_grants_path = os.path.join(cfg["run_dir"], "used-grants")

    def keys(self) -> tuple[bytes, bytes]:
        # Missing/unreadable .env must NEVER authenticate against an empty key.
        # Check on every request: a removed file must invalidate cached keys.
        try:
            stat = os.stat(self.cfg["env_file"])
        except OSError as exc:
            self._keys = (b"", b"")
            self._env_stamp = None
            raise PermissionError("XAGENT_SECRET_KEY configuration unavailable") from exc
        stamp = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if stamp != self._env_stamp:
            secret = read_env(self.cfg["env_file"]).get("XAGENT_SECRET_KEY", "")
            if len(secret) < 16:
                self._keys = (b"", b"")
                self._env_stamp = None
                raise PermissionError("XAGENT_SECRET_KEY not configured")
            self._keys = (derive(secret, b"xagent-hostd-request-v1"), derive(secret, b"xagent-hostd-grant-v1"))
            self._env_stamp = stamp
        if not all(self._keys):
            raise PermissionError("XAGENT_SECRET_KEY not loaded")
        return self._keys

    def audit(self, entry: dict) -> None:
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            fd = os.open(self.cfg["audit_log"], os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ── احراز هویت ──
    def verify(self, req: dict) -> None:
        k_req, _ = self.keys()
        mac = req.pop("mac", "")
        if not isinstance(mac, str) or not hmac.compare_digest(hmac.new(k_req, canonical(req), hashlib.sha256).hexdigest(), mac):
            raise PermissionError("invalid request signature")
        ts = req.get("ts")
        if not isinstance(ts, int | float) or abs(time.time() - ts) > CLOCK_SKEW:
            raise PermissionError("stale request")
        nonce = req.get("nonce")
        if not isinstance(nonce, str) or len(nonce) < 16:
            raise PermissionError("missing nonce")
        with self.lock:
            now = time.time()
            for n, t in list(self.nonces.items()):
                if now - t > CLOCK_SKEW * 2:
                    del self.nonces[n]
            if nonce in self.nonces:
                raise PermissionError("replayed request")
            self.nonces[nonce] = now

    def verify_grant(self, op: str, args: dict, grant) -> None:
        _, k_grant = self.keys()
        if not isinstance(grant, dict):
            raise PermissionError("owner approval grant required for level-3 operation")
        g = dict(grant)
        gmac = g.pop("mac", "")
        if not hmac.compare_digest(hmac.new(k_grant, canonical(g), hashlib.sha256).hexdigest(), str(gmac)):
            raise PermissionError("invalid approval grant")
        if g.get("op") != op or g.get("args_hash") != hashlib.sha256(canonical(args)).hexdigest():
            raise PermissionError("grant does not match this operation")
        if not isinstance(g.get("exp"), int | float) or g["exp"] < time.time():
            raise PermissionError("approval grant expired")
        nonce = str(g.get("nonce", ""))
        if len(nonce) < 16:
            raise PermissionError("invalid grant nonce")
        with self.lock:
            used = set()
            try:
                used = set(open(self.used_grants_path, encoding="utf-8").read().split())
            except OSError:
                pass
            if nonce in used:
                raise PermissionError("approval grant already used")
            fd = os.open(self.used_grants_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(nonce + "\n")

    # ── کارها ──
    def jobs(self) -> list[dict]:
        dfd = _jobs_fd(self.cfg)
        try:
            out = []
            for name in os.listdir(dfd):
                if name.endswith(".json") and not name.startswith("."):
                    st = _read_json(dfd, name)
                    if st:
                        st["job"] = name[:-5]
                        out.append(st)
            return out
        finally:
            os.close(dfd)

    def reconcile(self) -> None:
        """کارهایی که فرایندشان مرده ولی وضعیت running دارند «گم‌شده» علامت می‌خورند."""
        dfd = _jobs_fd(self.cfg)
        try:
            for name in os.listdir(dfd):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                st = _read_json(dfd, name)
                if st and st.get("status") in ("running", "starting") and not _alive(st.get("pid")):
                    if time.time() - float(st.get("started_at", st.get("created_at", 0)) or 0) > 5:
                        st.update({"status": "lost", "finished_at": time.time(),
                                   "error": "فرایند اجرای کار پیش از پایان از بین رفت (خرابی/ری‌استارت میزبان)"})
                        _write_json(dfd, name, st)
        finally:
            os.close(dfd)

    def handle(self, req: dict, peer_uid: int) -> dict:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "version": VERSION}
        self.verify(req)
        if op == "catalog":
            return {"ok": True, "ops": catalog(self.cfg)}
        if op == "job.status":
            self.reconcile()
            job = str(req.get("args", {}).get("job", ""))
            if not re.fullmatch(r"[a-f0-9]{16,32}", job):
                return {"ok": False, "error": "invalid job id"}
            st = next((j for j in self.jobs() if j["job"] == job), None)
            return {"ok": st is not None, "job": st, "error": None if st else "job not found"}
        if op in ("job.cancel", "jobs.cancel_all"):
            targets = self.jobs()
            if op == "job.cancel":
                targets = [j for j in targets if j["job"] == str(req.get("args", {}).get("job"))]
            n = 0
            for j in targets:
                if j.get("status") == "running" and _alive(j.get("pid")):
                    try:
                        os.killpg(int(j["pid"]), signal.SIGTERM)
                        n += 1
                    except OSError:
                        pass
            self.audit({"event": "cancel", "op": op, "count": n, "actor": req.get("actor"), "uid": peer_uid})
            return {"ok": True, "cancelled": n}
        if op != "run":
            return {"ok": False, "error": "unknown request"}
        body = req.get("args") or {}
        target, args = body.get("op"), body.get("args") or {}
        if not isinstance(target, str) or not isinstance(args, dict):
            return {"ok": False, "error": "invalid run request"}
        try:
            level, _steps, tmax = build_steps(self.cfg, target, args)
        except BadArgs as e:
            self.audit({"event": "rejected", "op": target, "reason": str(e), "actor": req.get("actor"), "uid": peer_uid})
            return {"ok": False, "error": f"rejected: {e}", "code": "rejected"}
        if level >= 3:
            try:
                self.verify_grant(target, args, body.get("grant"))
            except PermissionError as e:
                self.audit({"event": "denied", "op": target, "reason": str(e), "actor": req.get("actor"), "uid": peer_uid})
                return {"ok": False, "error": str(e), "code": "approval_required"}
        with self.lock:
            self.reconcile()
            running = [j for j in self.jobs() if j.get("status") in ("running", "starting") and _alive(j.get("pid"))]
            if len(running) >= int(self.cfg["max_concurrent"]):
                return {"ok": False, "error": "too many concurrent operations", "code": "busy"}
            if level >= 2 and any(int(j.get("level", 1)) >= 2 for j in running):
                return {"ok": False, "error": "another modifying operation is running", "code": "busy"}
            job = uuid.uuid4().hex
            spec = {"op": target, "args": args, "level": level, "timeout": tmax, "actor": str(req.get("actor", ""))[:64],
                    "created_at": time.time(), "status": "starting"}
            dfd = _jobs_fd(self.cfg)
            try:
                _write_json(dfd, f"{job}.json", spec)
            finally:
                os.close(dfd)
            cmd = [sys.executable, os.path.abspath(__file__), "--run-job", job]
            if self.cfg.get("_config_path"):
                cmd += ["--config", self.cfg["_config_path"]]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True)
            p.stdin.write(canonical(spec))
            p.stdin.close()
            threading.Thread(target=p.wait, daemon=True).start()  # جلوگیری از فرایند zombie
            spec.update({"status": "running", "pid": p.pid})
        self.audit({"event": "started", "job": job, "op": target, "args": args, "level": level,
                    "actor": req.get("actor"), "uid": peer_uid})
        return {"ok": True, "job": job, "level": level, "status": "running"}


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _peer_uid(conn: socket.socket) -> int:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def serve(cfg: dict, listener: socket.socket) -> None:
    broker = Broker(cfg)
    broker.reconcile()
    allowed = {int(u) for u in cfg["allowed_uids"]}
    idle_exit = float(cfg.get("idle_exit_seconds", 0) or 0)
    listener.settimeout(5.0)
    last = time.monotonic()

    def client(conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(15)
                uid = _peer_uid(conn)
                if uid not in allowed:
                    conn.sendall(b'{"ok":false,"error":"peer not allowed"}\n')
                    broker.audit({"event": "peer_denied", "uid": uid})
                    return
                buf = b""
                while not buf.endswith(b"\n") and len(buf) < MAX_REQUEST:
                    chunk = conn.recv(8192)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.decode("utf-8"))
                if not isinstance(req, dict):
                    raise ValueError("bad request")
                resp = broker.handle(req, uid)
            except PermissionError as e:
                resp = {"ok": False, "error": str(e), "code": "unauthorized"}
            except Exception as e:  # noqa: BLE001
                resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            try:
                conn.sendall(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
            except OSError:
                pass

    while True:
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            if idle_exit and time.monotonic() - last > idle_exit:
                busy = any(j.get("status") == "running" and _alive(j.get("pid")) for j in broker.jobs())
                if not busy:
                    return  # فعال‌سازی سوکت systemd در اتصال بعدی دوباره اجرا می‌کند
            continue
        last = time.monotonic()
        threading.Thread(target=client, args=(conn,), daemon=True).start()


def prepare_dirs(cfg: dict) -> None:
    os.makedirs(cfg["run_dir"], exist_ok=True)
    os.makedirs(cfg["jobs_dir"], exist_ok=True)
    os.chmod(cfg["jobs_dir"], 0o755)


def main() -> None:
    ap = argparse.ArgumentParser(description="X Agent host command broker")
    ap.add_argument("--config")
    ap.add_argument("--run-job")
    ap.add_argument("--catalog", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config)
    cfg["_config_path"] = os.path.abspath(a.config) if a.config else None
    if a.catalog:
        print(json.dumps(catalog(cfg), ensure_ascii=False, indent=1))
        return
    if a.run_job:
        if not re.fullmatch(r"[a-f0-9]{32}", a.run_job):
            sys.exit(2)
        spec = json.loads(sys.stdin.buffer.read(MAX_REQUEST).decode())
        run_job(cfg, a.run_job, spec)
        return
    prepare_dirs(cfg)
    if os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid()):
        listener = socket.socket(fileno=3)  # فعال‌سازی سوکت systemd
    else:
        try:
            os.unlink(cfg["socket"])
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o117)
        # اتصال با نام نسبی در پوشه سوکت: محدودیت ۱۰۸ نویسه مسیر AF_UNIX برای مسیرهای نصب طولانی
        cwd = os.getcwd()
        os.chdir(os.path.dirname(cfg["socket"]))
        try:
            listener.bind(os.path.basename(cfg["socket"]))
        finally:
            os.chdir(cwd)
        os.umask(old)
        if cfg.get("socket_gid") is not None:
            os.chown(cfg["socket"], -1, int(cfg["socket_gid"]))
        listener.listen(16)
    serve(cfg, listener)


if __name__ == "__main__":
    main()
