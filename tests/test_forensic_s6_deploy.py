"""ممیزی جرم‌شناختی — مرحله ۶، F17: تشخیص لایه‌به‌لایه استقرار؛ سلامت داخلی اثبات HTTPS عمومی نیست.

سرور TLS واقعی محلی (ssl پایتون با گواهی‌های CA آزمون ساخته‌شده با openssl) روی loopback؛ هیچ VPS یا شبکه
عمومی لمس نمی‌شود و اعتبارسنجی TLS در ابزار تشخیص غیرفعال نمی‌شود.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deploy_check", ROOT / "infra/scripts/deploy_check.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)
pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl در دسترس نیست")


def _sh(*args: str, cwd: Path) -> None:
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    _sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
        "-subj", "/CN=Xagent Test CA", cwd=d)
    for name, san in (("domain", "DNS:agent.test"), ("ip", "IP:127.0.0.1")):
        (d / f"{name}.ext").write_text(f"subjectAltName={san}\n")
        _sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
            "-subj", f"/CN={name}", cwd=d)
        _sh("openssl", "x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", f"{name}.pem", "-days", "2", "-extfile", f"{name}.ext", cwd=d)
    return d


def _server(pki: Path, *, default_cert: bool):
    """شنونده TLS با انتخاب گواهی بر اساس SNI؛ default_cert=False یعنی کلاینت بدون SNI دست‌دهی ناموفق دارد."""
    def ctx(name):
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        c.load_cert_chain(pki / f"{name}.pem", pki / f"{name}.key")
        return c
    domain_ctx, ip_ctx = ctx("domain"), ctx("ip")

    def pick(sock, server_name, _orig):
        if server_name == "agent.test":
            sock.context = domain_ctx
        elif server_name is None and not default_cert:
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        return None
    base = ctx("ip") if default_cert else ctx("domain")
    base.sni_callback = pick
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            try:
                with base.wrap_socket(conn, server_side=True) as s:
                    s.recv(1)
            except (OSError, ssl.SSLError):
                pass
    threading.Thread(target=loop, daemon=True).start()
    _ = ip_ctx
    return port, lambda: (stop.set(), srv.close())


HEALTH_OK = {"ok": True, "db": True, "redis": True, "worker": {"ready": True}}
PS_OK = "\n".join(json.dumps({"Service": s, "State": "running", "Health": "healthy"}) for s in ("api", "worker", "web"))


def test_F17_ip_install_without_sni_and_internal_ca_is_expected_untrusted_not_public_ok(pki):
    port, stop = _server(pki, default_cert=True)
    try:
        env = {"XAGENT_SITE_ADDRESS": "https://127.0.0.1", "XAGENT_TLS": "internal"}
        api, worker = dc.check_api(HEALTH_OK)
        rep = dc.build_report(env, containers=dc.check_containers(PS_OK), api=api, worker=worker, tls_sni=None,
                              tls_no_sni=dc.tls_probe("127.0.0.1", port, None), public=dc.check_public(None))
    finally:
        stop()
    L = rep["layers"]
    assert L["tls_no_sni"]["status"] == "ok" and L["tls_sni"]["status"] == "skipped"
    assert L["trust"]["status"] == "expected_untrusted", L["trust"]
    assert L["public"]["status"] == "not_verified" and rep["public_verified"] is False
    assert "تأیید نشده" in rep["verdict"] and "اثبات" in rep["verdict"], rep["verdict"]


def test_F17_domain_sni_trust_and_missing_no_sni_fallback_are_distinguished(pki):
    port, stop = _server(pki, default_cert=False)  # مانند Caddy بدون default_sni (پیش از اصلاح bc79cc7)
    try:
        sni = dc.tls_probe("127.0.0.1", port, "agent.test", cafile=str(pki / "ca.pem"))
        untrusted = dc.tls_probe("127.0.0.1", port, "agent.test")  # مخزن اعتماد سیستم: CA آزمون معتبر نیست
        no_sni = dc.tls_probe("127.0.0.1", port, None)
    finally:
        stop()
    assert sni["handshake"] and sni["trusted"] and "agent.test" in sni["san"]
    assert untrusted["handshake"] and untrusted["trusted"] is False  # مذاکره موفق ≠ اعتماد
    assert no_sni["handshake"] is False, "دست‌دهی بدون SNI باید شکست بخورد"
    env = {"XAGENT_SITE_ADDRESS": "agent.test", "XAGENT_TLS": "ops@agent.test"}
    api, worker = dc.check_api(HEALTH_OK)
    rep = dc.build_report(env, containers=dc.check_containers(PS_OK), api=api, worker=worker, tls_sni=untrusted,
                          tls_no_sni=no_sni, public=dc.check_public(None))
    assert rep["internal_ok"] and not rep["https_ok"], "سلامت داخلی سالم به‌جای شکست HTTPS گزارش شد"
    assert rep["layers"]["trust"]["status"] == "fail" and rep["layers"]["tls_no_sni"]["status"] == "fail"


def test_F17_container_api_and_worker_layers_are_separate():
    bad_ps = PS_OK + "\n" + json.dumps({"Service": "desktop", "State": "running", "Health": "unhealthy"})
    assert dc.check_containers(bad_ps)["status"] == "fail"
    assert dc.check_containers(None)["status"] == "not_verified"
    api, worker = dc.check_api({"ok": True, "db": True, "redis": True, "worker": {"ready": False, "detail": "x"}})
    assert api["status"] == "ok" and worker["status"] == "degraded"
    api2, worker2 = dc.check_api(None)
    assert api2["status"] == "fail" and worker2["status"] == "not_verified"


def test_F17_public_probe_only_when_configured_and_doctor_uses_layered_check():
    class R:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self.body).encode()
    assert dc.check_public("https://probe.example/x", opener=lambda u, timeout: R({"reachable": True}))["status"] == "ok"
    assert dc.check_public("https://probe.example/x", opener=lambda u, timeout: R({"reachable": False}))["status"] == "fail"
    ctl = (ROOT / "infra/scripts/xagentctl").read_text()
    assert "deploy_check.py" in ctl.split("doctor)", 1)[1].split(";;", 1)[0], "xagent doctor فقط سلامت داخلی را می‌سنجد"
