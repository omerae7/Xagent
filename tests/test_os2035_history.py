"""Stable chat history pagination, including messages with the same timestamp."""

import uuid

import pytest

from xagent_core.db import session_scope, utcnow
from xagent_core.models import Conversation, Message

pytestmark = pytest.mark.asyncio


async def test_equal_timestamp_history_is_not_lost_between_pages(app_client):
    async with session_scope() as s:
        c = Conversation(title="Synthetic pagination")
        s.add(c)
        await s.flush()
        cid = c.id
        now = utcnow()
        ids = []
        for i in range(65):
            m = Message(
                id=uuid.UUID(int=i + 1),
                conversation_id=cid,
                role="user",
                content=f"History {i}",
                created_at=now,
            )
            s.add(m)
            ids.append(str(m.id))
    first = await app_client.get(f"/api/conversations/{cid}/messages?limit=60")
    assert first.status_code == 200
    first = first.json()
    before = first["items"][0]["id"]
    second = await app_client.get(f"/api/conversations/{cid}/messages?limit=60&before={before}")
    actual = [m["id"] for m in second.json()["items"] + first["items"]]
    assert actual == ids, f"{len(actual)} of 65 messages preserved"
    assert first["has_more"] is True and second.json()["has_more"] is False


async def test_history_cursor_must_belong_to_the_requested_conversation(app_client):
    async with session_scope() as s:
        a, b = Conversation(title="A"), Conversation(title="B")
        s.add_all([a, b])
        await s.flush()
        m = Message(conversation_id=b.id, role="user", content="Separate conversation")
        s.add(m)
        await s.flush()
        aid, mid = a.id, m.id
    r = await app_client.get(f"/api/conversations/{aid}/messages?before={mid}")
    assert r.status_code == 404
