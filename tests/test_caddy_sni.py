"""Host derivation for old .env files and configurable IP/domain deployments."""
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(("site", "override", "expected"), [
    ("https://192.0.2.10", "", "192.0.2.10"),
    ("https://192.0.2.10:443", "", "192.0.2.10"),
    ("agent.example.com", "", "agent.example.com"),
    ("https://agent.example.com:443", "", "agent.example.com"),
    ("https://[2001:db8::1]:443", "", "2001:db8::1"),
    ("https://192.0.2.10", "override.example.com", "override.example.com"),
    ("", "", "localhost"),
])
def test_default_sni_host(site, override, expected):
    script = Path(__file__).resolve().parents[1] / "infra/caddy/entrypoint.sh"
    result = subprocess.run(["sh", str(script), "sh", "-c", 'printf %s "$XAGENT_DEFAULT_SNI"'],
                            env={**os.environ, "XAGENT_SITE_ADDRESS": site, "XAGENT_DEFAULT_SNI": override},
                            check=True, capture_output=True, text=True)
    assert result.stdout == expected
