"""ممیزی جرم‌شناختی — مرحله ۳، F10: مهار مسیر فایل خروجی بزرگ ابزار (شناسه فراخوانی کنترل‌شده توسط ارائه‌دهنده)."""

from __future__ import annotations

import uuid
from pathlib import Path

HOSTILE = ["../../../escape-f10", "/tmp/abs-f10", "a/b\\c-f10", "..", ".", "‮evil-f10", "nul\x00byte-f10",
           "CON", "x" * 400, "../" * 30 + "deep-f10", "~/home-f10"]


def _big(tag: str) -> str:
    return (tag + " ") * 6000


def test_F10_hostile_call_ids_stay_inside_run_artifact_directory(tmp_path, monkeypatch):
    from xagent_agent import pipeline
    from xagent_core.config import get_settings

    ws = get_settings().workspace_dir
    run_id = uuid.uuid4()
    run_dir = (ws / "runs" / str(run_id)).resolve()
    before_outside = {p for p in Path("/tmp").glob("*f10*")}
    for cid in HOSTILE:
        out = pipeline.bound_tool_output(run_id, cid, _big(f"T{HOSTILE.index(cid)}"))
        ref = out.split("متن کامل: ", 1)[1].split(" ", 1)[0]
        assert ref != "(ذخیره", f"خروجی شناسه {cid!r} ذخیره نشد"
        target = (ws / ref).resolve()
        assert target.is_relative_to(run_dir), f"مسیر خارج از پوشه اجرا: {cid!r} → {target}"
        assert target.read_text().startswith(f"T{HOSTILE.index(cid)} ")
    assert {p for p in Path("/tmp").glob("*f10*")} == before_outside, "فایلی خارج از فضای کاری نوشته شد"
    assert not (ws.parent / "escape-f10").exists() and not (ws / "escape-f10").exists()


def test_F10_repeated_identifier_preserves_both_artifacts():
    from xagent_agent import pipeline
    from xagent_core.config import get_settings

    run_id = uuid.uuid4()
    a = pipeline.bound_tool_output(run_id, "call_1", _big("FIRST"))
    b = pipeline.bound_tool_output(run_id, "call_1", _big("SECOND"))
    ra, rb = (x.split("متن کامل: ", 1)[1].split(" ", 1)[0] for x in (a, b))
    ws = get_settings().workspace_dir
    assert ra != rb and (ws / ra).read_text().startswith("FIRST") and (ws / rb).read_text().startswith("SECOND")


async def test_F10_reference_is_readable_by_file_read_and_legacy_refs_still_work():
    from types import SimpleNamespace

    from xagent_agent import pipeline
    from xagent_core.config import get_settings
    from xagent_tools.builtin import core

    run_id = uuid.uuid4()
    ref = pipeline.bound_tool_output(run_id, "call_ok", _big("READ")).split("متن کامل: ", 1)[1].split(" ", 1)[0]
    ctx = SimpleNamespace(extra={}, run_id=run_id, session=None, sandbox=None, trusted_owner=True)
    res = await core.file_read(ctx, {"path": ref})
    assert res.ok and "READ" in res.output
    legacy = get_settings().workspace_dir / "runs" / str(run_id) / "call_legacy.txt"  # قالب نام پیش از اصلاح
    legacy.write_text("LEGACY")
    old = await core.file_read(ctx, {"path": f"runs/{run_id}/call_legacy.txt"})
    assert old.ok and "LEGACY" in old.output
