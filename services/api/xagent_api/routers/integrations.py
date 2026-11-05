"""یکپارچه‌سازی‌ها: تلگرام شخصی (ورود MTProto)، ابزارها و نصاب GitHub، دسکتاپ زنده و صدا (STT/TTS)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_agent.model_select import select_models
from xagent_agent.usage_ledger import begin_aux_attempt, finish_aux_attempt
from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.config import get_settings
from xagent_core.db import get_sessionmaker
from xagent_core.models import InstalledTool, SettingKV, ToolVersion, User
from xagent_core.redact import redact_obj, scrub, seal_secrets
from xagent_core.security import decrypt_secret, encrypt_secret
from xagent_core.settings_store import AISettings, TelegramSettings, get_setting, set_setting
from xagent_tools.registry import BUILTIN, GROUP_LABELS
from xagent_tools.sandbox import DesktopUnavailable, get_sandbox

from ..deps import current_user, get_session, owner_user, parse_uuid, resolve_user, ser

router = APIRouter(prefix="/api", tags=["integrations"])


# ───────────── تلگرام ─────────────
class TgStart(BaseModel):
    api_id: int
    api_hash: str = Field(min_length=10)
    phone: str = Field(min_length=6)


class TgVerify(BaseModel):
    code: str | None = None
    password: str | None = None


def _enc(v: str) -> str:
    return base64.b64encode(encrypt_secret(v) or b"").decode()


def _dec(v: str | None) -> str | None:
    if not v:
        return None
    return decrypt_secret(base64.b64decode(v))


def _client(session_str: str, api_id: int, api_hash: str):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(session_str), api_id, api_hash, device_model="X Agent",
                          system_version="Linux", app_version="1.0", lang_code="fa", system_lang_code="fa")


@router.get("/telegram/status")
async def tg_status(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    cfg = await get_setting(session, "telegram", TelegramSettings)
    raw = await bus.get_redis().get("xagent:telegram:status")
    return {"settings": cfg.model_dump(), "live": json.loads(raw) if raw else {"connected": False}}


@router.post("/telegram/login/start")
async def tg_start(body: TgStart, user: User = Depends(owner_user)):
    client = _client("", body.api_id, body.api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(body.phone)
        state = {"session": client.session.save(), "hash": sent.phone_code_hash, "api_id": body.api_id,
                 "api_hash": body.api_hash, "phone": body.phone}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"ارسال کد ناموفق: {e}") from e
    finally:
        await client.disconnect()
    await bus.get_redis().set("xagent:telegram:login", _enc(json.dumps(state)), ex=600)
    return {"ok": True, "message": "کد تایید به تلگرام شما ارسال شد."}


@router.post("/telegram/login/verify")
async def tg_verify(body: TgVerify, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    from telethon.errors import SessionPasswordNeededError

    raw = await bus.get_redis().get("xagent:telegram:login")
    if not raw:
        raise HTTPException(400, "مهلت ورود تمام شده؛ دوباره شروع کنید")
    st = json.loads(_dec(raw) or "{}")
    client = _client(st["session"], st["api_id"], st["api_hash"])
    await client.connect()
    try:
        try:
            if body.code:
                await client.sign_in(st["phone"], body.code, phone_code_hash=st["hash"])
            elif body.password:
                await client.sign_in(password=body.password)
        except SessionPasswordNeededError:
            if not body.password:
                st["session"] = client.session.save()
                await bus.get_redis().set("xagent:telegram:login", _enc(json.dumps(st)), ex=600)
                return {"ok": False, "need_password": True, "message": "رمز دومرحله‌ای حساب را وارد کنید."}
            await client.sign_in(password=body.password)
        me = await client.get_me()
        if me is None:
            raise HTTPException(400, "ورود ناموفق بود")
        secret = {"session": _enc(client.session.save()), "api_hash": _enc(st["api_hash"])}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"ورود ناموفق: {e}") from e
    finally:
        await client.disconnect()
    row = await session.get(SettingKV, "telegram_secret")
    if row is None:
        session.add(SettingKV(key="telegram_secret", value=secret))
    else:
        row.value = secret
    cfg = await get_setting(session, "telegram", TelegramSettings)
    cfg.enabled, cfg.api_id, cfg.api_hash_set, cfg.phone = True, st["api_id"], True, st["phone"]
    cfg.owner_chat = cfg.owner_chat or "me"
    await set_setting(session, "telegram", cfg)
    await bus.get_redis().delete("xagent:telegram:login")
    await audit(session, user.username, "telegram.login", st["phone"][-4:])
    await session.commit()
    await bus.publish("settings.changed", {"key": "telegram"})
    name = " ".join(filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")]))
    return {"ok": True, "message": f"حساب «{name}» متصل شد."}


@router.post("/telegram/logout")
async def tg_logout(user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    row = await session.get(SettingKV, "telegram_secret")
    if row is not None:
        await session.delete(row)
    cfg = await get_setting(session, "telegram", TelegramSettings)
    cfg.enabled, cfg.api_hash_set = False, False
    await set_setting(session, "telegram", cfg)
    await audit(session, user.username, "telegram.logout")
    await session.commit()
    await bus.publish("settings.changed", {"key": "telegram"})
    return {"ok": True}


# ───────────── ابزارها ─────────────
class InstallIn(BaseModel):
    source: str = Field(min_length=4, max_length=512)
    ref: str | None = None
    name: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


@router.get("/tools")
async def tools(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    installed = (await session.execute(select(InstalledTool).order_by(InstalledTool.created_at.desc()))).scalars().all()
    builtin = [{"name": t.name, "label": t.label, "group": t.group, "group_label": GROUP_LABELS.get(t.group, t.group),
                "category": t.category, "description": t.description} for t in BUILTIN]
    return {"builtin": builtin, "installed": [tool_out(t, {
        "tool_count": len(t.tool_schemas or []), "tool_names": [s["name"] for s in (t.tool_schemas or [])][:30]})
        for t in installed]}


def tool_out(t: InstalledTool, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """خروجی امن ابزار: پیکربندی (env/headers) هرگز، نشانی منبع و خطا بدون اعتبارنامه."""
    return ser(t, exclude={"config"}, extra={**(extra or {}), "source_url": scrub(t.source_url) if t.source_url else None,
                                             "last_error": scrub(t.last_error) if t.last_error else None})


@router.post("/tools/install")
async def install(body: InstallIn, user: User = Depends(owner_user)):
    from xagent_tools.installer import parse_source

    try:
        parse_source(body.source)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    payload = {**body.model_dump(exclude={"env"}), "actor": user.username,
               "env_sealed": seal_secrets({"env": body.env})["env"] if body.env else {}}  # اسرار در صف Redis رمزشده
    await bus.get_redis().xadd("xagent:control", {"cmd": "install", "payload": json.dumps(payload)})
    return {"ok": True, "message": "نصب در محیط ایزوله آغاز شد؛ نتیجه در اعلان‌ها نمایش داده می‌شود."}


@router.post("/tools/{tid}/{action}")
async def tool_action(tid: str, action: str, body: dict[str, Any] | None = None, user: User = Depends(owner_user),
                      session: AsyncSession = Depends(get_session)):
    t = await session.get(InstalledTool, parse_uuid(tid))
    if t is None:
        raise HTTPException(404, "ابزار یافت نشد")
    body = body or {}
    if action in ("enable", "disable"):
        from xagent_memory.revisions import record_change, snapshot

        before = snapshot("tool", t)
        t.enabled = action == "enable"
        t.status = "ready" if t.enabled and t.active_version_id else ("disabled" if not t.enabled else t.status)
        await record_change(session, "tool", t, "status", before, f"{action} توسط کارفرما", actor="owner", origin="panel")
        await bus.publish("tool.changed", {"id": str(t.id), "status": t.status})
        return tool_out(t)
    if action in ("update", "rollback"):
        await bus.get_redis().xadd("xagent:control", {"cmd": action, "payload": json.dumps(
            {"tool_id": str(t.id), "ref": body.get("ref"), "version_id": body.get("version_id"), "actor": user.username})})
        return {"ok": True, "message": "درخواست ثبت شد"}
    raise HTTPException(400, "اقدام نامعتبر")


@router.get("/tools/{tid}/versions")
async def tool_versions(tid: str, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(ToolVersion).where(ToolVersion.tool_id == parse_uuid(tid))
                                  .order_by(ToolVersion.created_at.desc()))).scalars()
    return {"items": [ser(v, extra={"manifest": redact_obj(v.manifest), "path": v.path,
                                    "test_output": scrub(v.test_output) if v.test_output else None}) for v in rows]}


@router.delete("/tools/{tid}")
async def delete_tool(tid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    t = await session.get(InstalledTool, parse_uuid(tid))
    if t is None:
        raise HTTPException(404, "ابزار یافت نشد")
    await audit(session, user.username, "tool.delete", t.name)
    await session.delete(t)
    return {"ok": True}


# ───────────── دسکتاپ زنده ─────────────
class TakeoverIn(BaseModel):
    on: bool


@router.get("/desktop/status")
async def desktop_status(_: User = Depends(current_user)):
    sandbox = get_sandbox()
    # In lite deployments tools use an isolated runner, but there is no desktop.
    if sandbox.kind == "runner":
        return {"health": {"ok": False, "kind": "disabled", "message": "دسکتاپ در پروفایل Lite غیرفعال است"}, "takeover": False}
    health = await sandbox.health()
    return {"health": health, "takeover": await bus.is_takeover()}


@router.post("/desktop/takeover")
async def takeover(body: TakeoverIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    await bus.set_takeover(body.on, user.username)
    # اطلاع به دیمن دسکتاپ (لایه دوم اعمال قطعی Takeover)
    sb = get_sandbox()
    if hasattr(sb, "_post"):
        with contextlib.suppress(Exception):
            await sb._post("/takeover", {"on": body.on})
    await audit(session, user.username, "desktop.takeover" if body.on else "desktop.release")
    await bus.publish("desktop.takeover", {"on": body.on, "by": user.username})
    return {"takeover": body.on}


@router.get("/desktop/screenshot")
async def desktop_screenshot(_: User = Depends(current_user)):
    try:
        png = await get_sandbox().screenshot()
    except DesktopUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.websocket("/desktop/vnc")
async def vnc_proxy(ws: WebSocket, mode: str = "view"):
    """پروکسی WebSocket احرازشده به noVNC داخلی؛ حالت کنترل فقط هنگام Takeover فعال است."""
    import websockets

    async with get_sessionmaker()() as session:
        user = await resolve_user(session, ws.cookies.get("xa_token"))
    if user is None:
        await ws.close(code=4401)
        return
    if mode == "control" and (user.role != "owner" or not await bus.is_takeover()):
        await ws.close(code=4403)
        return
    s = get_settings()
    upstream_url = s.desktop_vnc_ws_control if mode == "control" else s.desktop_vnc_ws_view
    proto = "binary" if "binary" in (ws.headers.get("sec-websocket-protocol") or "") else None
    await ws.accept(subprotocol=proto)
    try:
        async with websockets.connect(upstream_url, subprotocols=["binary"], max_size=None,
                                      additional_headers={"Authorization": f"Bearer {s.desktop_token}"}) as up:
            async def client_to_up():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes") is not None:
                        await up.send(msg["bytes"])
                    elif msg.get("text") is not None:
                        await up.send(msg["text"])

            async def up_to_client():
                async for data in up:
                    if isinstance(data, bytes):
                        await ws.send_bytes(data)
                    else:
                        await ws.send_text(data)

            async def watch_takeover():
                # با پایان Takeover، اتصال کنترلی بسته می‌شود
                while mode == "control":
                    await asyncio.sleep(2)
                    if not await bus.is_takeover():
                        break
                else:
                    await asyncio.Event().wait()

            tasks = [asyncio.create_task(c()) for c in (client_to_up, up_to_client, watch_takeover)]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except (WebSocketDisconnect, OSError, websockets.WebSocketException):
        pass
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


# ───────────── صدا ─────────────
class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/voice/stt")
async def stt(file: UploadFile = File(...), _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    chain = (await select_models(session, "stt", "worker", need_cap="stt")).models
    if not chain:
        raise HTTPException(400, "مدل گفتار به متن (STT) تنظیم نشده است")
    audio = await file.read()
    if len(audio) > 25 * 1024 * 1024:
        raise HTTPException(413, "فایل صوتی بسیار بزرگ است")
    last = None
    for m in chain:
        aid = await begin_aux_attempt(m, purpose="stt", input_hint=max(256, len(audio) // 1000),
                                      output_hint=2048)
        try:
            text = await m.provider.transcribe(m.model_name, audio, file.content_type or "audio/webm")
            await finish_aux_attempt(aid, model=m, unit_details={
                "type": "audio_input", "bytes": len(audio), "mime": file.content_type or "audio/webm",
                "output_text_characters": len(text),
            })
            return {"text": text.strip(), "model": m.label}
        except Exception as e:  # noqa: BLE001
            from xagent_agent.providers.base import ProviderError
            await finish_aux_attempt(aid, model=m, error=e,
                known_rejected=isinstance(e, ProviderError) and e.status in (400, 401, 403, 404, 422),
                unit_details={"type": "audio_input", "bytes": len(audio)})
            last = e
    raise HTTPException(502, f"تبدیل گفتار ناموفق: {scrub(str(last))}")


@router.post("/voice/tts")
async def tts(body: TTSIn, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    chain = (await select_models(session, "tts", "worker", need_cap="tts")).models
    if not chain:
        raise HTTPException(400, "مدل متن به گفتار (TTS) تنظیم نشده است")
    ai = await get_setting(session, "ai", AISettings)
    last = None
    for m in chain:
        aid = await begin_aux_attempt(m, purpose="tts", input_hint=max(256, len(body.text) // 2),
                                      output_hint=max(1024, len(body.text)))
        try:
            audio, mime = await m.provider.speak(m.model_name, body.text, ai.tts_voice)
            await finish_aux_attempt(aid, model=m, unit_details={
                "type": "audio_output", "input_text_characters": len(body.text),
                "output_bytes": len(audio), "mime": mime,
            })
            return Response(audio, media_type=mime, headers={"Cache-Control": "no-store"})
        except Exception as e:  # noqa: BLE001
            from xagent_agent.providers.base import ProviderError
            await finish_aux_attempt(aid, model=m, error=e,
                known_rejected=isinstance(e, ProviderError) and e.status in (400, 401, 403, 404, 422),
                unit_details={"type": "audio_output", "input_text_characters": len(body.text)})
            last = e
    raise HTTPException(502, f"تولید صدا ناموفق: {scrub(str(last))}")
