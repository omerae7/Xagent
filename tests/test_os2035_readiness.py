"""Daemon liveness is distinct from graphical/browser readiness."""
# ruff: noqa: F811 (imported pytest fixtures)
import httpx
from test_desktop_bridge import agentd, runnerd  # noqa: F401


def test_desktop_readiness_fails_without_graphical_browser(agentd):
    assert httpx.get(agentd+'/health').json()['ok'] is True
    r = httpx.get(agentd+'/ready')
    assert r.status_code == 503
    assert r.json()['desktop_ready'] is False
    assert r.json()['browser_cdp'] is False


def test_headless_runner_does_not_require_a_graphical_browser(runnerd):
    assert httpx.get(runnerd+'/ready').status_code == 200
