"""Capability readiness requires a CDP response, not just an HTTP 200 page."""
import httpx
import pytest

from xagent_agent import manifest
from xagent_tools.builtin import computer


@pytest.mark.parametrize('payload,expected', [
    (None, False), ({'ok': True}, False), ({'Browser': 'Chrome/test'}, False),
    ({'Browser': 'Chrome/test', 'Protocol-Version': '1.3', 'webSocketDebuggerUrl': 'ws://probe.test/devtools/browser/id'}, True),
])
async def test_probe_validates_actual_cdp_shape(monkeypatch, payload, expected):
    real = httpx.AsyncClient
    def reply(request):
        return httpx.Response(200, json=payload) if payload is not None else httpx.Response(200, text='<html>Sign in</html>')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real(transport=httpx.MockTransport(reply), **kw))
    monkeypatch.setattr(computer, '_cdp_url', lambda: 'http://probe.test:9222')
    result = await manifest.probe_browser()
    assert result['ok'] is expected
