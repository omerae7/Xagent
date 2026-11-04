"""Lossless private-memory downgrade/upgrade on a disposable database.

Never migrates or erases the shared pytest database, let alone production.
This verifies facts, experiences, immutable episodes and their scopes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = os.pathsep.join(str(ROOT / x) for x in (
    "packages/core", "packages/agent_core", "packages/memory",
    "packages/tools", "packages/crm", "services/api", "services/worker"))


@pytest.mark.asyncio
async def test_private_memory_scope_roundtrips_without_exposing_rows(database):
    db = "xa_private_mig_" + uuid.uuid4().hex[:10]
    source_dsn = os.environ["XAGENT_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    admin = await asyncpg.connect(source_dsn, database="postgres")
    try:
        await admin.execute(f"CREATE DATABASE {db} OWNER xagent")
        from urllib.parse import urlsplit, urlunsplit
        u = urlsplit(source_dsn)
        target = urlunsplit((u.scheme, u.netloc, "/" + db, u.query, u.fragment))
        env = {**os.environ, "PYTHONPATH": PYTHONPATH,
               "XAGENT_DATABASE_URL": target.replace("postgresql://", "postgresql+asyncpg://")}
        cmd = [sys.executable, "-m", "alembic", "-c", str(ROOT / "services/api/alembic.ini")]

        def migrate(*args):
            p = subprocess.run([*cmd, *args], env=env, capture_output=True, text=True, timeout=180)
            assert p.returncode == 0, f"alembic failed: {p.stdout[-3000:]} {p.stderr[-3000:]}"

        migrate("upgrade", "0003")
        con = await asyncpg.connect(target)
        ids = [uuid.uuid4() for _ in range(3)]
        try:
            await con.execute("""
                INSERT INTO semantic_facts (id, section, key, content, source, status, version, scope)
                VALUES ($1, 'agent', 'secret_scope', 'PRIVATE-FACT', 'test', 'active', 1, 'employee:test')
            """, ids[0])
            await con.execute("""
                INSERT INTO experiences (id, signature, title, approach, tool_sequence,
                    outcome, status, tokens_used, duration_ms, steps, success_count, failure_count, scope)
                VALUES ($1, 'secret', 'PRIVATE-EXPERIENCE', 'private', ARRAY[]::varchar[],
                    'verified_success', 'active', 1, 1, 1, 1, 0, 'employee:test')
            """, ids[1])
            await con.execute("""
                INSERT INTO episodes (id, kind, title, summary, tools, tokens_in, tokens_out,
                    cost_usd, duration_ms, status, meta, scope)
                VALUES ($1, 'routine', 'PRIVATE-EPISODE', 'secret', ARRAY[]::varchar[],
                    1, 1, 0, 1, 'success', '{}'::jsonb, 'employee:test')
            """, ids[2])
        finally:
            await con.close()

        migrate("downgrade", "0002")
        con = await asyncpg.connect(target)
        try:
            for table, rid in zip(("semantic_facts", "experiences", "episodes"), ids):
                assert await con.fetchval(f"SELECT count(*) FROM {table} WHERE id=$1", rid) == 0
                assert await con.fetchval(
                    f"SELECT count(*) FROM xagent_private_{table}_archive WHERE id=$1", rid) == 1
        finally:
            await con.close()

        migrate("upgrade", "0003")
        con = await asyncpg.connect(target)
        try:
            for table, rid in zip(("semantic_facts", "experiences", "episodes"), ids):
                assert await con.fetchval(f"SELECT scope FROM {table} WHERE id=$1", rid) == "employee:test"
                assert await con.fetchval(
                    f"SELECT to_regclass('public.xagent_private_{table}_archive')") is None
            assert await con.fetchval("SELECT content FROM semantic_facts WHERE id=$1", ids[0]) == "PRIVATE-FACT"
            assert await con.fetchval("SELECT title FROM episodes WHERE id=$1", ids[2]) == "PRIVATE-EPISODE"
        finally:
            await con.close()
        migrate("upgrade", "0004")
    finally:
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1", db)
            await admin.execute(f"DROP DATABASE IF EXISTS {db}")
        finally:
            await admin.close()
