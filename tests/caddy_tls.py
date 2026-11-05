"""Real disposable Caddy HTTPS regression. Run: python tests/caddy_tls.py --image IMAGE.

Uses loopback ports, the actual image entrypoint/Caddyfile/SPA, and a tiny HTTP
fixture for upstream routing. Never contacts a VPS or uses deployment volumes.
Internal CA trust is tested explicitly; it is NOT public certificate trust.
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def request(port, host, path, *, sni=None, context=None):
    if context is None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # handshake-only probe, not a trust claim
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=sni) as conn:
            assert conn.version() in ("TLSv1.2", "TLSv1.3")
            conn.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            parts = []
            while data := conn.recv(65536):
                parts.append(data)
            return b"".join(parts)


def scenario(image, site, host, override=""):
    name = "xagent-sni-test-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="xagent-sni-") as folder:
        root = Path(folder)
        (root / "api").mkdir()
        (root / "api/health").write_text('{"ok":true,"source":"sni-regression"}')
        (root / "Caddyfile").write_text(
            "{\n admin off\n}\n:8000 {\n bind 127.0.0.1\n root * /tmp/backend\n file_server\n}\n")
        try:
            docker("run", "-d", "--name", name, "--add-host", "api:127.0.0.1",
                   "-e", "NO_PROXY=api,127.0.0.1,localhost",
                   "-p", "127.0.0.1::443", "-e", f"XAGENT_SITE_ADDRESS={site}",
                   "-e", "XAGENT_TLS=internal", "-e", f"XAGENT_DEFAULT_SNI={override}",
                   "--mount", f"type=bind,src={root},dst=/tmp/backend,readonly", image)
            port = int(docker("port", name, "443/tcp").rsplit(":", 1)[1])
            docker("exec", "-d", name, "caddy", "run", "--config", "/tmp/backend/Caddyfile", "--adapter", "caddyfile")
            # Validation/adaptation runs through the same host-deriving entrypoint.
            docker("exec", name, "/usr/local/bin/xagent-web-entrypoint", "caddy", "validate",
                   "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
            adapted = json.loads(docker("exec", name, "/usr/local/bin/xagent-web-entrypoint", "caddy", "adapt",
                                        "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"))
            policies = [p for server in adapted["apps"]["http"]["servers"].values()
                        for p in server.get("tls_connection_policies", [])]
            assert any(p.get("default_sni") == (override or host) for p in policies)
            for _ in range(100):
                try:
                    page = request(port, host, "/")  # server_hostname=None: NO client SNI
                    if b'id="root"' in page:
                        break
                except (OSError, ssl.SSLError):
                    pass
                time.sleep(.1)
            else:
                raise AssertionError(docker("logs", name))
            assert b"200 OK" in page
            assert b'id="root"' in request(port, host, "/chat/test")
            api = request(port, host, "/api/health")
            assert b"200 OK" in api and b'"source":"sni-regression"' in api, api
            assert b"404 Not Found" in request(port, host, "/api/internal/private")
            assert b"Cache-Control: no-cache" in request(port, host, "/sw.js")
            if not host.replace(".", "").isdigit():
                assert b"200 OK" in request(port, host, "/", sni=host)
            try:
                request(port, host, "/", sni=host, context=ssl.create_default_context())
            except ssl.SSLCertVerificationError:
                pass
            else:
                raise AssertionError("Fresh internal CA unexpectedly trusted by system roots")
            ca = docker("exec", name, "cat", "/data/caddy/pki/authorities/local/root.crt")
            trusted = ssl.create_default_context(cadata=ca)
            assert b"200 OK" in request(port, host, "/", sni=host, context=trusted)
            # Restart with the same disposable CA storage; startup fallback must persist.
            docker("restart", name)
            # Docker may allocate a new host port for an ephemeral publication.
            port = int(docker("port", name, "443/tcp").rsplit(":", 1)[1])
            for _ in range(100):
                try:
                    if b'id="root"' in request(port, host, "/"):
                        break
                except OSError:
                    pass
                time.sleep(.1)
            else:
                raise AssertionError("No-SNI HTTPS failed after restart: " + docker("logs", name))
            print(json.dumps({"site": site, "override": override, "no_sni_tls_and_routing": "passed",
                              "system_ca_trust": "rejected as expected", "explicit_ca_trust": "passed",
                              "restart": "passed"}))
        finally:
            # Only this uniquely named disposable test container and its anonymous volumes.
            subprocess.run(["docker", "rm", "-fv", name], check=False, capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    image = parser.parse_args().image
    scenario(image, "https://127.0.0.1", "127.0.0.1")
    scenario(image, "agent.test", "agent.test")
    scenario(image, "https://127.0.0.1", "127.0.0.1", "127.0.0.1")
