#!/usr/bin/env python3
"""تشخیص لایه‌به‌لایه سلامت استقرار X Agent (فقط کتابخانه استاندارد؛ روی میزبان اجرا می‌شود).

لایه‌ها جدا گزارش می‌شوند و هیچ لایه داخلی «اثبات HTTPS عمومی» حساب نمی‌شود:
  containers  وضعیت/سلامت کانتینرها (docker compose ps)
  api         سلامت داخلی API (/api/health: پایگاه داده و Redis)
  worker      آمادگی عملکردی Worker (گزارش حلقه‌ها)
  tls_sni     دست‌دهی HTTPS با SNI دامنه (نصب دامنه‌دار)
  tls_no_sni  دست‌دهی HTTPS بدون SNI روی IP (کلاینت‌هایی که SNI نمی‌فرستند)
  trust       سیاست اعتماد گواهی: دامنه → باید عمومی معتبر باشد؛ IP/داخلی → «نامعتبر مورد انتظار»، نه «سالم»
  public      دسترس‌پذیری عمومی فقط با کاوشگر بیرونی (XAGENT_EXTERNAL_PROBE_URL)؛ بدون آن «تأیید نشده»
اعتبارسنجی TLS هرگز غیرفعال نمی‌شود؛ دست‌دهی بدون تأیید فقط برای خواندن گواهی و جدا از «اعتماد» است.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import ssl
import subprocess
import sys
import urllib.request
from typing import Any
from urllib.parse import urlparse

OK, FAIL, DEGRADED, NOT_VERIFIED, EXPECTED_UNTRUSTED, SKIPPED = (
    "ok", "fail", "degraded", "not_verified", "expected_untrusted", "skipped")


def read_env(path: str | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if path:
        try:
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k] = v
        except OSError:
            pass
    return env


def site_host(site: str) -> str:
    s = site.strip()
    if "://" not in s:
        s = "https://" + s
    return (urlparse(s).hostname or "").strip("[]")


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def check_containers(ps_output: str | None) -> dict[str, Any]:
    """خروجی `docker compose ps --format json` (یک JSON در هر خط یا آرایه)."""
    if ps_output is None:
        return {"status": NOT_VERIFIED, "detail": "docker compose در دسترس نیست"}
    rows: list[dict] = []
    text = ps_output.strip()
    try:
        data = json.loads(text) if text.startswith("[") else [json.loads(x) for x in text.splitlines() if x.strip()]
        rows = data if isinstance(data, list) else [data]
    except ValueError:
        return {"status": FAIL, "detail": "خروجی وضعیت کانتینرها قابل خواندن نیست"}
    bad = [{"service": r.get("Service"), "state": r.get("State"), "health": r.get("Health")} for r in rows
           if r.get("State") != "running" or r.get("Health") not in ("", None, "healthy")]
    return {"status": OK if rows and not bad else FAIL, "services": len(rows), "problems": bad}


def check_api(health: dict | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if health is None:
        return ({"status": FAIL, "detail": "API داخلی پاسخ نداد"},
                {"status": NOT_VERIFIED, "detail": "بدون پاسخ API، آمادگی Worker معلوم نیست"})
    api = {"status": OK if health.get("ok") else FAIL, "db": health.get("db"), "redis": health.get("redis")}
    w = health.get("worker") or {}
    if w.get("ready") is True:
        worker = {"status": OK}
    elif w.get("ready") is False:
        worker = {"status": DEGRADED, "detail": w.get("detail") or "حلقه‌های Worker آماده نیستند",
                  "degraded": w.get("degraded", [])}
    else:
        worker = {"status": NOT_VERIFIED, "detail": "گزارش آمادگی Worker موجود نیست"}
    return api, worker


def tls_probe(host: str, port: int, sni: str | None, cafile: str | None = None, timeout: float = 6) -> dict[str, Any]:
    """دست‌دهی واقعی؛ دو گام جدا: (۱) مذاکره و خواندن گواهی، (۲) اعتبارسنجی کامل زنجیره و نام با مخزن اعتماد."""
    out: dict[str, Any] = {"host": host, "port": port, "sni": sni}
    probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE  # فقط خواندن گواهی برای گزارش؛ هرگز ادعای اعتماد نیست
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw, \
                probe.wrap_socket(raw, server_hostname=sni) as conn:
            out["handshake"] = True
            out["version"] = conn.version()
            der = conn.getpeercert(binary_form=True)
            out["cert_sha256"] = __import__("hashlib").sha256(der).hexdigest()[:16] if der else None
    except (OSError, ssl.SSLError) as e:
        out.update(handshake=False, error=f"{type(e).__name__}: {e}"[:200])
        return out
    verify = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw, \
                verify.wrap_socket(raw, server_hostname=sni or host) as conn:
            cert = conn.getpeercert()
            out["trusted"] = True
            out["subject"] = dict(x[0] for x in cert.get("subject", ()))
            out["san"] = [v for k, v in cert.get("subjectAltName", ()) if k in ("DNS", "IP Address")]
    except (OSError, ssl.SSLError, ssl.CertificateError) as e:
        out.update(trusted=False, trust_error=f"{type(e).__name__}: {e}"[:200])
    return out


def check_public(probe_url: str | None, opener=urllib.request.urlopen) -> dict[str, Any]:
    if not probe_url:
        return {"status": NOT_VERIFIED, "detail": "کاوشگر بیرونی تنظیم نشده؛ دسترس‌پذیری عمومی تأیید نشده است"}
    try:
        with opener(probe_url, timeout=10) as r:
            body = json.loads(r.read().decode() or "{}")
        ok = bool(body.get("reachable"))
        return {"status": OK if ok else FAIL, "detail": body.get("detail", ""), "probe": probe_url}
    except Exception as e:  # noqa: BLE001
        return {"status": NOT_VERIFIED, "detail": f"کاوشگر بیرونی پاسخ نداد: {type(e).__name__}"}


def build_report(env: dict[str, str], *, containers: dict, api: dict, worker: dict, tls_sni: dict | None,
                 tls_no_sni: dict | None, public: dict) -> dict[str, Any]:
    host = site_host(env.get("XAGENT_SITE_ADDRESS", ""))
    internal_ca = env.get("XAGENT_TLS", "") == "internal" or is_ip(host)
    layers: dict[str, Any] = {"containers": containers, "api": api, "worker": worker}

    def tls_layer(p: dict | None, label: str) -> dict:
        if p is None:
            return {"status": SKIPPED, "detail": f"{label} برای این نوع نصب کاربرد ندارد"}
        return {**p, "status": OK if p.get("handshake") else FAIL}
    layers["tls_sni"] = tls_layer(tls_sni, "دست‌دهی با SNI دامنه")
    layers["tls_no_sni"] = tls_layer(tls_no_sni, "دست‌دهی بدون SNI")
    trusted = [p.get("trusted") for p in (tls_sni, tls_no_sni) if p and p.get("handshake")]
    if not trusted:
        layers["trust"] = {"status": NOT_VERIFIED, "detail": "دست‌دهی موفقی برای سنجش اعتماد نبود"}
    elif internal_ca:
        layers["trust"] = {"status": EXPECTED_UNTRUSTED if not all(trusted) else OK,
                           "detail": "گواهی داخلی (IP): برای مرورگرها عمومی معتبر نیست — مورد انتظار، نه «سالم عمومی»"}
    else:
        layers["trust"] = {"status": OK if all(trusted) else FAIL,
                           "detail": "نصب دامنه‌دار باید گواهی عمومی معتبر داشته باشد"}
    layers["public"] = public
    internal_ok = all(layers[k]["status"] == OK for k in ("containers", "api", "worker"))
    https_ok = all(layers[k]["status"] in (OK, SKIPPED) for k in ("tls_sni", "tls_no_sni"))
    if public["status"] == OK and https_ok:
        verdict = "دسترس‌پذیری عمومی HTTPS با کاوشگر بیرونی تأیید شد"
    elif internal_ok and https_ok:
        verdict = ("سرویس‌های داخلی و شنونده HTTPS سالم‌اند؛ دسترس‌پذیری عمومی تأیید نشده است "
                   "(سلامت داخلی اثبات کارکرد HTTPS عمومی نیست)")
    else:
        failed = [k for k, v in layers.items() if v["status"] in (FAIL, DEGRADED)]
        verdict = "مشکل در لایه‌ها: " + "، ".join(failed)
    return {"host": host, "mode": "ip_internal_ca" if internal_ca else "domain", "layers": layers,
            "internal_ok": internal_ok, "https_ok": https_ok, "public_verified": public["status"] == OK,
            "verdict": verdict}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="تشخیص لایه‌به‌لایه سلامت استقرار X Agent")
    ap.add_argument("--env", default=None)
    ap.add_argument("--health-json", default=None, help="خروجی /api/health داخلی (اختیاری)")
    ap.add_argument("--ps-json", default=None, help="خروجی docker compose ps --format json (اختیاری)")
    ap.add_argument("--connect", default="127.0.0.1", help="نشانی اتصال به شنونده HTTPS")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--cafile", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    env = read_env(a.env)
    ps = a.ps_json
    if ps is None:
        try:
            ps = subprocess.run(["docker", "compose", "ps", "--format", "json"], capture_output=True, text=True,
                                timeout=20, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            ps = None
    try:
        health = json.loads(a.health_json) if a.health_json else None
    except ValueError:
        health = None
    api, worker = check_api(health)
    host = site_host(env.get("XAGENT_SITE_ADDRESS", "")) or a.connect
    tls_sni = None if is_ip(host) else tls_probe(a.connect, a.port, host, a.cafile)
    tls_no_sni = tls_probe(a.connect, a.port, None, a.cafile)
    rep = build_report(env, containers=check_containers(ps), api=api, worker=worker, tls_sni=tls_sni,
                       tls_no_sni=tls_no_sni, public=check_public(env.get("XAGENT_EXTERNAL_PROBE_URL")))
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        for k, v in rep["layers"].items():
            print(f"{k:12} {v['status']:20} {v.get('detail') or v.get('error') or ''}")
        print("نتیجه:", rep["verdict"])
    return 0 if rep["internal_ok"] and rep["https_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
