"""MCP protocol success must not prove an unobserved external effect."""
from types import SimpleNamespace

import pytest

from xagent_agent.validation import checked_result
from xagent_tools import registry


@pytest.mark.parametrize('annotations, verified', [({}, None), ({'readOnlyHint': False}, None),
                                                   ({'readOnlyHint': True}, True)])
async def test_protocol_response_is_not_mutation_receipt(monkeypatch, annotations, verified):
    async def call(*args):
        return True, 'MCP response text'
    monkeypatch.setattr(registry.mcp_client, 'call_tool', call)
    installed = SimpleNamespace(name='synthetic-server', config={}, harness_category='external_tool')
    tool = registry._mcp_tool(installed, {'name': 'op', 'annotations': annotations})
    result = await tool.handler(None, {})
    assert result.verified is verified
    settled = checked_result(tool, {}, result)
    assert settled.ok is (verified is True)
