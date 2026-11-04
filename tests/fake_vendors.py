"""سرورهای جعلی محلی xAI، Kimi Open Platform، Kimi Code و Cursor Cloud Agents برای تست — بدون کلید واقعی.

ساختار پاسخ‌ها از مستندات رسمی هر سرویس گرفته شده است؛ هیچ درخواستی به اینترنت ارسال نمی‌شود.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Vendors:
    def __init__(self) -> None:
        self.port = _port()
        self.requests: list[dict[str, Any]] = []
        self.mode: dict[str, str] = {}  # مسیر → ok | 401 | 429 | slow | 500
        # وضعیت Cursor
        self.agents: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.cursor_posts = 0

    def url(self, prefix: str) -> str:
        return f"http://127.0.0.1:{self.port}/{prefix}"

    def reset(self) -> None:
        self.requests.clear()
        self.mode.clear()
        self.agents.clear()
        self.runs.clear()
        self.cursor_posts = 0

    def bodies(self, path_part: str) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if path_part in r["path"]]

    def auth_of(self, path_part: str) -> set[str]:
        return {r["auth"] for r in self.requests if path_part in r["path"]}


def _usage(prompt: int = 120, completion: int = 30, **extra: Any) -> dict[str, Any]:
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion, **extra}


def _chat_reply(body: dict[str, Any], *, reasoning: str | None = None, usage_extra: dict | None = None) -> dict:
    msgs = body.get("messages") or []
    has_tool_result = any(m.get("role") == "tool" for m in msgs)
    msg: dict[str, Any] = {"role": "assistant", "content": None}
    if body.get("tools") and not has_tool_result:
        msg["tool_calls"] = [{"id": "call_1", "type": "function",
                              "function": {"name": body["tools"][0]["function"]["name"],
                                           "arguments": json.dumps({"q": "x"})}}]
        finish = "tool_calls"
    else:
        msg["content"] = "پاسخ نهایی" if has_tool_result else "OK"
        finish = "stop"
    if reasoning:
        msg["reasoning_content"] = reasoning
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:8]}", "object": "chat.completion", "model": body.get("model"),
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": _usage(**(usage_extra or {}))}


def build_app(v: Vendors) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def record(request: Request, call_next):
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        v.requests.append({"path": request.url.path, "method": request.method, "body": body,
                           "auth": request.headers.get("authorization", "")})
        return await call_next(request)

    async def _gate(path: str) -> JSONResponse | None:
        mode = v.mode.get(path, "ok")
        if mode == "401":
            return JSONResponse({"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}}, 401)
        if mode == "429":
            return JSONResponse({"error": {"message": "rate limit"}}, 429)
        if mode == "500":
            return JSONResponse({"error": {"message": "upstream"}}, 500)
        if mode == "slow":
            await asyncio.sleep(3)
        return None

    # ───────── xAI (docs.x.ai): ‎/v1/language-models و ‎/v1/models ─────────
    @app.get("/xai/v1/language-models")
    async def xai_lm():
        if (g := await _gate("xai-models")):
            return g
        return {"models": [
            {"id": "grok-test", "fingerprint": "fp", "created": 1, "object": "model", "owned_by": "xai",
             "version": "1", "aliases": ["grok-test-latest"], "input_modalities": ["text", "image"],
             "output_modalities": ["text"], "prompt_text_token_price": 20000,
             "cached_prompt_text_token_price": 5000, "completion_text_token_price": 100000,
             "prompt_image_token_price": 20000, "search_price": 0},
            {"id": "grok-mini-test", "fingerprint": "fp2", "created": 1, "object": "model", "owned_by": "xai",
             "version": "1", "aliases": [], "input_modalities": ["text"], "output_modalities": ["text"],
             "prompt_text_token_price": 3000, "cached_prompt_text_token_price": 750,
             "completion_text_token_price": 5000},
            {"id": "grok-noprice", "object": "model", "owned_by": "xai", "input_modalities": ["text"],
             "output_modalities": ["text"], "prompt_text_token_price": None, "completion_text_token_price": None},
        ]}

    @app.get("/xai/v1/models")
    async def xai_models():
        if (g := await _gate("xai-models")):
            return g
        return {"object": "list", "data": [
            {"id": "grok-test", "object": "model", "owned_by": "xai", "context_length": 256000,
             "capabilities": {}, "long_context_threshold": 128000,
             "prompt_text_token_price_long_context": 40000, "completion_text_token_price_long_context": 200000,
             "cached_prompt_text_token_price_long_context": 10000},
            {"id": "grok-mini-test", "object": "model", "owned_by": "xai", "context_length": 131072,
             "capabilities": {"reasoning_effort": ["low", "high"], "default_reasoning_effort": "low"},
             "long_context_threshold": 0, "prompt_text_token_price_long_context": 0,
             "completion_text_token_price_long_context": 0},
            {"id": "grok-noprice", "object": "model", "owned_by": "xai"},
        ]}

    @app.post("/xai/v1/chat/completions")
    async def xai_chat(request: Request):
        if (g := await _gate("xai-chat")):
            return g
        body = await request.json()
        return _chat_reply(body, usage_extra={"prompt_tokens_details": {"cached_tokens": 20},
                                              "completion_tokens_details": {"reasoning_tokens": 7}})

    # ───────── Kimi Open Platform (سازگار با OpenAI) ─────────
    @app.get("/kimi/v1/models")
    async def kimi_models():
        if (g := await _gate("kimi-models")):
            return g
        return {"object": "list", "data": [
            {"id": "kimi-test-vision", "object": "model", "owned_by": "moonshot", "context_length": 262144,
             "supports_image_in": True, "supports_video_in": False, "supports_reasoning": False},
            {"id": "kimi-test-thinking", "object": "model", "owned_by": "moonshot", "context_length": 262144,
             "supports_image_in": False, "supports_reasoning": True},
            {"id": "kimi-plain", "object": "model", "owned_by": "moonshot"},
        ]}

    @app.post("/kimi/v1/chat/completions")
    async def kimi_chat(request: Request):
        if (g := await _gate("kimi-chat")):
            return g
        body = await request.json()
        thinking = (body.get("thinking") or {}).get("type") == "enabled" or "thinking" in body.get("model", "")
        if (body.get("thinking") or {}).get("type") == "enabled" and body.get("tool_choice") not in (None, "auto", "none"):
            return JSONResponse({"error": {"message": "tool_choice must be auto or none when thinking"}}, 400)
        return _chat_reply(body, reasoning="استدلال داخلی مدل" if thinking else None,
                           usage_extra={"cached_tokens": 40})

    # ───────── Kimi Code (مسیر مستقل) ─────────
    @app.post("/kimicode/coding/v1/chat/completions")
    async def kimicode_chat(request: Request):
        if (g := await _gate("kimicode-chat")):
            return g
        return _chat_reply(await request.json())

    # (بدون ‎/models: کشف باید به فهرست مستند برگردد)

    # ───────── Cursor Cloud Agents (cursor.com/docs/cloud-agent/api/endpoints) ─────────
    def _cursor_auth(request: Request) -> JSONResponse | None:
        if not request.headers.get("authorization"):
            return JSONResponse({"error": {"code": "unauthorized", "message": "missing key"}}, 401)
        return None

    @app.get("/cursor/v1/me")
    async def cursor_me(request: Request):
        if (g := _cursor_auth(request) or await _gate("cursor-me")):
            return g
        return {"apiKeyName": "test-key", "createdAt": "2026-01-01T00:00:00Z", "userId": 1,
                "userEmail": "owner@example.test", "userFirstName": "T", "userLastName": "U"}

    @app.get("/cursor/v1/models")
    async def cursor_models(request: Request):
        if (g := _cursor_auth(request) or await _gate("cursor-models")):
            return g
        return {"items": [{"id": "composer-test", "displayName": "Composer Test", "description": "", "aliases": [],
                           "parameters": [], "variants": []}]}

    @app.post("/cursor/v1/agents")
    async def cursor_create(request: Request):
        if (g := _cursor_auth(request) or await _gate("cursor-create")):
            return g
        body = await request.json()
        aid = body.get("agentId") or f"bc-{uuid.uuid4()}"
        if aid in v.agents:
            return JSONResponse({"error": {"code": "agent_id_conflict", "message": "exists"}}, 409)
        v.cursor_posts += 1
        rid = f"run-{uuid.uuid4().hex[:10]}"
        now = "2026-01-01T00:00:00Z"
        agent = {"id": aid, "name": body.get("name") or "agent", "status": "ACTIVE", "env": body.get("env"),
                 "repos": body.get("repos"), "workOnCurrentBranch": body.get("workOnCurrentBranch", False),
                 "autoCreatePR": body.get("autoCreatePR", False), "url": f"https://cursor.com/agents/{aid}",
                 "createdAt": now, "updatedAt": now, "latestRunId": rid}
        run = {"id": rid, "agentId": aid, "status": "CREATING", "createdAt": now, "updatedAt": now}
        v.agents[aid] = agent
        v.runs[rid] = run
        if v.mode.get("cursor-create-lost") == "1":
            v.mode["cursor-create-lost"] = "done"
            await asyncio.sleep(3)  # پاسخ گم می‌شود (Timeout سمت کلاینت) ولی Agent ساخته شده است
        return {"agent": agent, "run": run}

    @app.get("/cursor/v1/agents/{aid}")
    async def cursor_agent(aid: str, request: Request):
        if (g := _cursor_auth(request)):
            return g
        if aid not in v.agents:
            return JSONResponse({"error": {"code": "agent_not_found", "message": "nf"}}, 404)
        return v.agents[aid]

    @app.get("/cursor/v1/agents/{aid}/runs/{rid}")
    async def cursor_run(aid: str, rid: str, request: Request):
        if (g := _cursor_auth(request) or await _gate("cursor-run")):
            return g
        run = v.runs.get(rid)
        if run is None or run["agentId"] != aid:
            return JSONResponse({"error": {"code": "run_not_found", "message": "nf"}}, 404)
        return run

    @app.post("/cursor/v1/agents/{aid}/runs/{rid}/cancel")
    async def cursor_cancel(aid: str, rid: str, request: Request):
        if (g := _cursor_auth(request)):
            return g
        run = v.runs.get(rid)
        if run is None:
            return JSONResponse({"error": {"code": "run_not_found", "message": "nf"}}, 404)
        if run["status"] in ("FINISHED", "ERROR", "CANCELLED", "EXPIRED"):
            return JSONResponse({"error": {"code": "run_not_cancellable", "message": "terminal"}}, 409)
        run["status"] = "CANCELLED"
        return {"id": rid}

    return app


def start(v: Vendors) -> Any:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(build_app(v), host="127.0.0.1", port=v.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", v.port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    return server


def finish_run(v: Vendors, rid: str, status: str = "FINISHED", pr_url: str | None = None, branch: str = "cursor/fix-1",
               repo: str = "https://github.com/acme/app") -> None:
    run = v.runs[rid]
    run["status"] = status
    run["durationMs"] = 1234
    run["result"] = "Fixed the failing test and opened a PR." if status == "FINISHED" else "error"
    run["git"] = {"branches": [{"repoUrl": repo, "branch": branch, "prUrl": pr_url}]}
