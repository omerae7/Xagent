"""Regression coverage for current-message de-duplication and isolated runner configuration."""
from xagent_core.db import session_scope
from xagent_core.models import Conversation, Message, Run
from xagent_memory.working import load_window


async def test_history_excludes_only_the_current_run(database):
    async with session_scope() as session:
        conv = Conversation(title="voice context regression", channel="voice")
        session.add(conv)
        await session.flush()
        previous = Run(conversation_id=conv.id, trigger="voice", input="سلام")
        current = Run(conversation_id=conv.id, trigger="voice", input="سلام")
        session.add_all([previous, current])
        await session.flush()
        session.add_all([
            Message(conversation_id=conv.id, run_id=previous.id, role="user", content="سلام"),
            Message(conversation_id=conv.id, run_id=current.id, role="user", content="سلام"),
        ])
        await session.flush()
        _, history = await load_window(session, conv.id, exclude_run_id=current.id)
        assert len(history) == 1
        assert history[0].run_id == previous.id


def test_isolated_runner_has_no_worker_or_desktop_mounts():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yaml").read_text()
    runner = compose.split("\n  runner:\n", 1)[1].split("\n  web:\n", 1)[0]
    assert "runner-home:/home/agent" in runner
    assert "desktop-home" not in runner
    assert "pgdata" not in runner
    assert "/data:/data" not in runner
    assert "RUNNER_MODE" in runner


def test_initial_password_never_in_bootstrap_log():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    source = (root / "services/api/xagent_api/bootstrap.py").read_text()
    assert 's.owner_username, password)' not in source


def test_desktop_network_does_not_include_db_or_redis_network():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yaml").read_text()
    desktop = compose.split("\n  desktop:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    assert "networks: [desktop_link]" in desktop
    assert "networks: [internal, public]" not in desktop
