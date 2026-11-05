"""Fail-closed evidence checks; model prose is never proof of an external effect."""
from __future__ import annotations

import re
from typing import Any

from xagent_tools.base import Tool, ToolResult

# Ordinary UI interactions return observations, not proof of a send or purchase.
# The caller separately enforces verification for escalated external-effect categories.
OBSERVATION_TOOLS = {"browser_open", "browser_click", "browser_type", "desktop_input"}
# Discovery lists tabs or loads tools; it never shows requested page content.
DISCOVERY_TOOLS = {"tools_load", "browser_tabs"}
# Browser work counts as inspection only when some browser result actually observed page content
# (DOM text/elements or a real vision analysis). Navigation alone (URL reached) is not inspection.
BROWSER_TOOLS = {"browser_tabs", "browser_open", "browser_read", "browser_click", "browser_type", "browser_screenshot"}
# Asking *about* tabs (plural/listing), not «open X in a new tab».
_TABS = re.compile(r"(?<!\w)tabs(?!\w)|(?<!\w)تب(\u200c|\s)?ها", re.I)
_COMPLETION = re.compile(
    r"\b(done|sent|created|saved|deleted|executed|completed|scheduled|updated)\b|"
    r"(انجام|ارسال|ثبت|ساخته|ذخیره|حذف|اجرا|زمان‌بندی)\s*(شد|کردم|شده)", re.I,
)


def requires_verification(tool: Tool, args: dict) -> bool:
    return tool.category_for(args) is not None and tool.name not in OBSERVATION_TOOLS


def checked_result(tool: Tool, args: dict, result: Any, *, verification_required: bool | None = None) -> ToolResult:
    """Keep the ToolResult interface; reject absent/malformed results and unknown effects."""
    if (not isinstance(result, ToolResult) or type(result.ok) is not bool
            or not isinstance(result.output, str)
            or not isinstance(result.data, dict)
            or (result.verified is not None and type(result.verified) is not bool)):
        return ToolResult(False, "نتیجه معتبر ابزار دریافت نشد؛ انجام عملیات تأیید نشده است.", verified=False)
    if result.ok and not result.output.strip() and not result.data and not result.images:
        return ToolResult(False, "ابزار هیچ نتیجه‌ای برای بررسی برنگرداند؛ تکمیل درخواست تأیید نشد.", verified=False)
    needs_verification = requires_verification(tool, args) if verification_required is None else verification_required
    if any(result.data.get(key) is True for key in ("uncertain", "outcome_uncertain", "replay_blocked")):
        # Native outbox results and older adapters used `uncertain`; all paths
        # must share the durable execution guard before another model/tool call.
        return ToolResult(False, result.output, {**result.data, "outcome_uncertain": True},
                          verified=False, untrusted=result.untrusted, images=result.images)
    if result.ok and (result.verified is False or (needs_verification and result.verified is not True)):
        return ToolResult(False, "اثر عملیات تأیید نشده است؛ ممکن است انجام شده باشد. "
                          "بدون بررسی نتیجه، عملیات را تکرار نکن.\n" + result.output,
                          {**result.data, "outcome_uncertain": bool(needs_verification)},
                          verified=result.verified, untrusted=result.untrusted)
    return result


def execution_required(run: Any, state: dict, answer: str) -> bool:
    """Conservative guard for executable work, including claims on otherwise text-only turns."""
    if run.task_id or run.routine_id:
        return True
    # The router selects broad groups by substring (e.g. «وب» in «دوباره»).
    # A short greeting does not request an external effect despite that routing hint.
    if re.fullmatch(r"(سلام|درود|hello|hi)(\s+(دوباره|مجدد|again))?[\s!.؟?]*", run.input, re.I):
        return bool(_COMPLETION.search(answer))
    if (state.get("route") or {}).get("tool_groups") or (state.get("plan") or {}).get("tool_groups"):
        return True
    if any(s.get("tool_hint") not in (None, "", "auto", "none")
           for s in (state.get("plan") or {}).get("steps", [])):
        return True
    return bool(state.get("tools_used") or _COMPLETION.search(answer))


def evidence_issues(state: dict, required: bool, request: str = "") -> list[str]:
    evidence = state.get("evidence") or []
    if not isinstance(evidence, list):
        return ["ساختار شواهد اجرا معتبر نیست"]
    # A retry may resolve the *same* operation; unrelated successes must not hide failures.
    latest = {}
    for idx, e in enumerate(evidence):
        if not isinstance(e, dict) or not isinstance(e.get("tool"), str) or type(e.get("ok")) is not bool:
            return ["نتیجه ابزار در شواهد اجرا ناقص یا نامعتبر است"]
        latest[(e["tool"], e.get("signature", idx))] = e
    issues = []
    observations = [i for i, e in enumerate(evidence) if e["ok"] is True and e.get("verified") is not False
                    and e.get("requires_verification") is False and e["tool"] not in DISCOVERY_TOOLS]
    for e in latest.values():
        # A failed read may be recovered by an alternative observation; it remains
        # in the evidence and requires a model criterion check, never a relabel to success.
        if (e["ok"] is False and e.get("requires_verification") is False
                and any(i > evidence.index(e) for i in observations)):
            continue
        if e["ok"] is not True or e.get("verified") is False:
            issues.append(f"نتیجه موفق برای ابزار {e['tool']} تأیید نشد")
        elif e.get("requires_verification", True) and e.get("verified") is not True:
            issues.append(f"اثر عملیات {e['tool']} تأیید نشده است")
    # Tool-call results must be backed by handler evidence; tools_load is discovery only.
    from .stoploss import LoopController

    records = {(e.get("call_id"), e["tool"], e.get("signature")) for e in evidence}
    for m in state.get("transcript", []):
        if m.get("role") == "assistant":
            for call in m.get("tool_calls") or []:
                signature = LoopController.signature(call.get("name", ""), call.get("arguments") or {})
                if call.get("name") != "tools_load" and (call.get("id"), call.get("name"), signature) not in records:
                    issues.append("فراخوانی ابزار بدون نتیجه ثبت‌شده وجود دارد")
    if required and not evidence:
        issues.append("هیچ شاهدی از اجرای ابزار برای انجام درخواست وجود ندارد")
    browser = [e for e in evidence if e["tool"] in BROWSER_TOOLS]
    # Listing tabs is itself the objective only when the request asks about tabs.
    inspects = any(e["tool"] not in DISCOVERY_TOOLS for e in browser) or bool(browser) and not _TABS.search(request)
    if inspects and not any(e["ok"] is True and e.get("content_observed") is True for e in browser):
        issues.append("مرورگر استفاده شد ولی محتوای قابل استفاده صفحه مشاهده نشد؛ ناوبری یا فهرست تب‌ها "
                      "بررسی سایت نیست (ناوبری فقط تلاش‌شده/جزئی است)")
    # Optional plans are not objective requirements. The transcript guard above
    # requires every attempted call's result; objective_report independently checks
    # effects explicitly requested by the user. Neither trusts planner tool hints.
    return issues


def evaluation_verdict(text: str, criteria: list[str], evidence: list[dict], required: bool) -> dict:
    from .llm import parse_json

    verdict = parse_json(text)
    invalid = {"passed": False, "issues": ["پاسخ ارزیاب ناقص یا بدون شواهد معتبر برای معیارهاست"]}
    if (not isinstance(verdict, dict) or type(verdict.get("passed")) is not bool
            or not isinstance(verdict.get("issues", []), list)
            or any(not isinstance(i, str) for i in verdict.get("issues", []))):
        return invalid
    if verdict["passed"] is False:
        return verdict
    checks = verdict.get("criteria")
    if not isinstance(checks, list) or len(checks) != len(criteria):
        return invalid
    eligible = {f"e{i}" for i, e in enumerate(evidence) if e.get("ok") is True and e.get("verified") is not False
                and (not e.get("requires_verification", True) or e.get("verified") is True)}
    indices = []
    for check in checks:
        if not isinstance(check, dict):
            return invalid
        idx, refs = check.get("index"), check.get("evidence")
        if (type(idx) is not int or check.get("passed") is not True or not isinstance(refs, list)
                or any(not isinstance(ref, str) or ref not in eligible for ref in refs)
                or required and not refs):
            return invalid
        indices.append(idx)
    return verdict if sorted(indices) == list(range(len(criteria))) else invalid


# ── هدف عملیاتی هر درخواست (F07): هر معیار اجباری به شاهد مرتبط خودش نیاز دارد ──
# (هدف، الگوی درخواست، ابزارهایی که اثرشان آن هدف را برآورده می‌کند)
_SEND = re.compile(r"(پیام|message|متن)[^.؟?!،]{0,40}(بده|بفرست|ارسال کن|بنویس برای|send)|"
                   r"(بفرست|ارسال کن|send)\b[^.؟?!]{0,40}(به|to)\b|(جواب|پاسخ) (بده|بفرست)|\breply to\b", re.I)
_FILE = re.compile(r"(فایل|file|سند|یادداشت)[^.؟?!،]{0,40}(بساز|بنویس|ذخیره کن|ایجاد کن|create|write|save)|"
                   r"(بساز|بنویس|ذخیره کن|create|write|save)[^.؟?!،]{0,30}(فایل|file)", re.I)
_TASK = re.compile(r"(تسک|وظیفه|task|یادآور)[^.؟?!،]{0,40}(بساز|ایجاد کن|ثبت کن|اضافه کن|بگذار|create|add)|"
                   r"(بساز|ایجاد کن|ثبت کن|اضافه کن|create|add)[^.؟?!،]{0,30}(تسک|وظیفه|task)", re.I)
_CONDITIONAL = re.compile(r"(^|\s)(اگر|در صورت|درصورتی|if|when|whenever)\s", re.I)
_TO_OWNER = re.compile(r"به من|برای من|خودم|\bme\b", re.I)
OBJECTIVES: list[tuple[str, re.Pattern[str], frozenset[str]]] = [
    ("file_written", _FILE, frozenset({"file_write"})),
    ("task_created", _TASK, frozenset({"task_create", "routine_create"})),
    ("message_sent", _SEND, frozenset({"telegram_send"})),
]
DIRECT_TRIGGERS = {"chat", "voice", "telegram", "delegate"}


def required_objectives(run: Any, state: dict) -> list[str]:
    """اهداف عملیاتی صریح درخواست مستقیم. درخواست شرطی/زمان‌دار و اجرای روتین/رویداد هدف قطعی تحمیل نمی‌کنند
    (ارسال در آن‌ها به داده بستگی دارد)؛ مأموریت زمان‌دار هدفش ساخت روتین است."""
    if getattr(run, "trigger", "chat") not in DIRECT_TRIGGERS or getattr(run, "routine_id", None):
        return []
    text = str(getattr(run, "input", "") or "")
    if (state.get("route") or {}).get("intent"):
        return ["task_created"]  # مأموریت زمان‌دار: اثر لازم ساخت روتین/تسک است، نه ارسال فوری
    if _CONDITIONAL.search(text):
        return []
    return [name for name, rx, _ in OBJECTIVES if rx.search(text)]


def objective_report(run: Any, state: dict) -> dict:
    """گزارش هدف: فراخوانی ≠ مشاهده ≠ اثر تأییدشده ≠ تکمیل هدف. status:
    not_required | completed | partial | not_completed | unknown (اثر بیرونی نامعلوم؛ تکرار کورکورانه نشود)."""
    required = required_objectives(run, state)
    if not required:
        return {"status": "not_required", "required": [], "completed": [], "missing": [], "unknown": []}
    evidence = [e for e in (state.get("evidence") or []) if isinstance(e, dict)]
    tools = {name: t for name, _, t in OBJECTIVES}
    to_owner = bool(_TO_OWNER.search(str(getattr(run, "input", "") or "")))
    completed, missing, unknown = [], [], []
    for obj in required:
        allowed = set(tools[obj]) | ({"notify_owner"} if obj == "message_sent" and to_owner else set())
        rel = [e for e in evidence if e.get("tool") in allowed]
        if any(e.get("ok") is True and (e.get("verified") is True or e.get("tool") == "notify_owner") for e in rel):
            completed.append(obj)
        elif any(e.get("outcome") == "UNKNOWN_EFFECT" or
                 e.get("ok") is False and e.get("verified") is None and e.get("requires_verification") for e in rel):
            unknown.append(obj)  # شاید انجام شده باشد؛ بدون بررسی تکرار نمی‌شود
        else:
            missing.append(obj)
    status = ("completed" if not missing and not unknown else
              "unknown" if unknown and not missing else "partial" if completed else "not_completed")
    labels = {"message_sent": "ارسال پیام", "file_written": "ساخت/نوشتن فایل", "task_created": "ساخت تسک/روتین"}
    issues = [f"هدف «{labels[o]}» با شاهد مرتبط و تأییدشده انجام نشده است" for o in missing]
    issues += [f"نتیجه بیرونی «{labels[o]}» نامعلوم است؛ بدون بررسی تکرار نشود" for o in unknown]
    return {"status": status, "required": required, "completed": completed, "missing": missing, "unknown": unknown,
            "issues": issues}
