"""زیرساخت تست: PostgreSQL و Redis واقعی + سرور شبیه‌ساز مدل سازگار با OpenAI."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(tempfile.mkdtemp(prefix="xagent-test-"))
os.environ.update(
    {
        "XAGENT_ENV": "test",
        "XAGENT_DATABASE_URL": os.environ.get(
            "XAGENT_TEST_DATABASE_URL", "postgresql+asyncpg://xagent:xagent@localhost:5432/xagent_test"
        ),
        "XAGENT_REDIS_URL": os.environ.get("XAGENT_TEST_REDIS_URL", "redis://localhost:6379/15"),
        "XAGENT_DATA_DIR": str(DATA),
        "XAGENT_SECRET_KEY": "test-secret-key-for-xagent-0123456789abcdef",
        "XAGENT_OWNER_USERNAME": "admin",
        "XAGENT_OWNER_PASSWORD": "Test-Password-123",
        "XAGENT_COOKIE_SECURE": "false",
        "XAGENT_SANDBOX": "local",
        "XAGENT_PROFILE": "standard",
    }
)
PYPATH = [str(ROOT / p) for p in ("packages/core", "packages/agent_core", "packages/memory", "packages/tools",
                                   "packages/crm", "services/api", "services/worker")]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ───────────── سرور شبیه‌ساز مدل ─────────────
class MockLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.scenarios: dict[str, list[tuple[str, dict]]] = {}
        self.handlers: list = []
        self.port = _free_port()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def respond(self, body: dict) -> dict:
        self.calls.append(body)
        msgs = body.get("messages", [])
        tools = {t["function"]["name"] for t in body.get("tools", [])}
        last = msgs[-1] if msgs else {"content": ""}
        content = last.get("content") if isinstance(last.get("content"), str) else json.dumps(last.get("content"))
        user_msgs = [m for m in msgs if m["role"] == "user" and isinstance(m.get("content"), str)]
        first_user = next((m["content"] for m in user_msgs if "<" not in m["content"][:1]), "")
        task_text = " ".join(m["content"] for m in user_msgs)
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        prompt_tokens = max(1, len(json.dumps(msgs, ensure_ascii=False)) // 4)

        def reply(text: str | None = None, call: tuple[str, dict] | None = None) -> dict:
            message: dict = {"role": "assistant", "content": text}
            finish = "stop"
            if call:
                message["tool_calls"] = [{"id": f"call_{len(self.calls)}", "type": "function",
                                          "function": {"name": call[0], "arguments": json.dumps(call[1], ensure_ascii=False)}}]
                finish = "tool_calls"
            return {
                "id": f"mock-{len(self.calls)}", "model": body.get("model"),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 25,
                          "prompt_tokens_details": {"cached_tokens": prompt_tokens // 2}},
            }

        # سناریوهای اختصاصی هر تست: (نام ابزار، آرگومان) یا متن پاسخ
        for handler in list(self.handlers):
            out = handler(body)
            if out is not None:
                return reply(None, out) if isinstance(out, tuple) else reply(out)

        # آزمون عملی قابلیت‌ها (probe)
        if "report_status" in tools:
            return reply(None, ("report_status", {"status": "ok"}))
        last_user = next((m for m in reversed(msgs) if m["role"] == "user"), {})
        if isinstance(last_user.get("content"), list) and any(
                isinstance(p, dict) and p.get("type") == "image_url" for p in last_user["content"]):
            return reply("red")
        # سناریوهای چندمرحله‌ای: اولین گامی که ابزارش هنوز صدا زده نشده اجرا می‌شود
        called = [c["function"]["name"] for m in msgs if m["role"] == "assistant" for c in (m.get("tool_calls") or [])]
        for trigger, steps in self.scenarios.items():
            if trigger in task_text and tools:
                for name, args in steps:
                    if name not in called or (name == "tools_load" and name not in called):
                        if name != "tools_load" and name not in tools and "tools_load" in tools and "tools_load" not in called:
                            return reply(None, ("tools_load", {"groups": ["admin"]}))
                        return reply(None, (name, args))
                return reply("انجام شد: " + str(tool_msgs[-1].get("content", "") if tool_msgs else "")[:400])
        if body.get("response_format", {}).get("type") == "json_object":
            if "هدف واقعی" in content:
                return reply(json.dumps({"goal": "انجام درخواست کارفرما", "success_criteria": ["نتیجه واقعی ثبت شود"],
                                         "deliverable": "گزارش", "ambiguity": "", "risk": "low"}, ensure_ascii=False))
            if "برنامه فشرده" in content:
                return reply(json.dumps({"steps": [{"title": "اجرای ابزار", "tool_hint": "auto"}],
                                         "memory_layers": ["semantic"], "tool_groups": ["tasks", "files", "telegram"]},
                                        ensure_ascii=False))
            if "ارزیابی کن" in content:
                return reply(json.dumps({"passed": True, "score": 9, "issues": []}))
            return reply("{}")

        if tools and "حلقه" in task_text:
            return reply(None, ("web_fetch", {"url": "http://localhost/forbidden"}))
        if tools and not tool_msgs:
            if "تسک" in task_text and "task_create" in tools:
                return reply(None, ("task_create", {"title": "تماس با مشتری", "priority": "high",
                                                    "due": "1405/01/15 10:00"}))
            if "پیام" in task_text and "telegram_send" in tools:
                return reply(None, ("telegram_send", {"chat": "@ali", "text": "سلام"}))
            if "فایل" in task_text and "file_write" in tools:
                return reply(None, ("file_write", {"path": "notes/a.txt", "content": "سلام دنیا"}))
            if "اکو" in task_text:
                name = next((t for t in tools if t.startswith("mcp__")), None)
                if name:
                    return reply(None, (name, {"text": "درود"}))
        if tool_msgs:
            return reply("انجام شد: " + str(tool_msgs[-1].get("content", ""))[:120])
        return reply(f"سلام! من X Agent هستم. ({first_user[:20]})")


@pytest.fixture(scope="session")
def mock_llm():
    import uvicorn

    llm = MockLLM()
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return llm.respond(await request.json())

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": "mock-model"}, {"id": "mock-discovered-1"},
                                            {"id": "text-embedding-mock"}]}

    @app.post("/fail/v1/chat/completions")
    async def fail(request: Request):
        llm.calls.append({"failed": True})
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": {"message": "Incorrect API key provided", "type": "invalid_request_error",
                                       "code": "invalid_api_key"}}, status_code=401)

    # ارائه‌دهنده بدرفتار: کلید ارسالی را عیناً در پیام خطا تکرار می‌کند (آزمون نشت اسرار)
    @app.post("/echo/v1/chat/completions")
    async def echo_chat(request: Request):
        from fastapi.responses import JSONResponse

        key = request.headers.get("authorization", "").split(" ")[-1]
        return JSONResponse({"error": {"message": f"Invalid credentials {key} for https://api.test/v1?key={key}",
                                       "code": "invalid_api_key"}}, status_code=401)

    @app.post("/echo/v1/audio/transcriptions")
    async def echo_stt(request: Request):
        from fastapi.responses import JSONResponse

        key = request.headers.get("authorization", "").split(" ")[-1]
        return JSONResponse({"error": f"upstream rejected token {key}"}, status_code=500)

    config = uvicorn.Config(app, host="127.0.0.1", port=llm.port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", llm.port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield llm
    server.should_exit = True


@pytest.fixture(scope="session", autouse=True)
def database():
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(PYPATH)}
    alembic = [sys.executable, "-m", "alembic", "-c", str(ROOT / "services/api/alembic.ini")]
    subprocess.run([*alembic, "downgrade", "base"], check=True, env=env, capture_output=True)
    subprocess.run([*alembic, "upgrade", "head"], check=True, env=env, capture_output=True)
    import redis

    redis.Redis.from_url(os.environ["XAGENT_REDIS_URL"]).flushdb()
    yield


@pytest.fixture(scope="session")
async def app_client(database):
    import httpx

    from xagent_api.bootstrap import bootstrap
    from xagent_api.main import app

    await bootstrap()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 headers={"X-XAgent": "1"}) as client:
        r = await client.post("/api/auth/login", json={"username": "admin", "password": "Test-Password-123"})
        assert r.status_code == 200, r.text
        client.cookies.set("xa_token", r.json()["token"])
        yield client


@pytest.fixture(scope="session")
async def configured_model(app_client, mock_llm):
    r = await app_client.post("/api/settings/ai/providers",
                              json={"kind": "custom", "name": "Mock", "base_url": mock_llm.base_url, "api_key": "sk-test-123456"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    r = await app_client.post("/api/settings/ai/models", json={
        "provider_id": pid, "model_name": "mock-model", "display_name": "مدل آزمایشی",
        "capabilities": {"tools": True}, "price_in": 1.0, "price_out": 2.0, "price_cache_read": 0.1,
    })
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    r = await app_client.get("/api/settings")
    ai = r.json()["settings"]["ai"]
    ai["smart"]["manager_model_ids"] = [mid]
    ai["smart"]["worker_model_ids"] = [mid]
    r = await app_client.put("/api/settings/ai", json=ai)
    assert r.status_code == 200, r.text
    return mid


@pytest.fixture(scope="session")
def runner():
    from xagent_agent.pipeline import AgentRunner

    return AgentRunner("test-worker")
