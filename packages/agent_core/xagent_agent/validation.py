"""Fail-closed evidence checks; model prose is never proof of an external effect."""
from __future__ import annotations

import re
from typing import Any

from xagent_tools.base import Tool, ToolResult

# Ordinary UI interactions return observations, not proof of a send or purchase.
# The caller separately enforces verification for escalated external-effect categories.
OBSERVATION_TOOLS = {"browser_open", "browser_click", "browser_type", "desktop_input"}
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
    if result.ok and (result.verified is False or (needs_verification and result.verified is not True)):
        return ToolResult(False, "اثر عملیات تأیید نشده است؛ ممکن است انجام شده باشد. "
                          "بدون بررسی نتیجه، عملیات را تکرار نکن.\n" + result.output,
                          verified=result.verified, untrusted=result.untrusted)
    return result


def execution_required(run: Any, state: dict, answer: str) -> bool:
    """Conservative guard for executable work, including claims on otherwise text-only turns."""
    if run.task_id or run.routine_id or run.employee_id:
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


def evidence_issues(state: dict, required: bool) -> list[str]:
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
                    and e.get("requires_verification") is False]
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
    completed_tools = {e["tool"] for e in latest.values() if e["ok"] is True and e.get("verified") is not False
                       and (not e.get("requires_verification", True) or e.get("verified") is True)}
    for step in (state.get("plan") or {}).get("steps", []):
        hint = step.get("tool_hint")
        if hint not in (None, "", "auto", "none") and hint not in completed_tools:
            issues.append(f"معیار اجرای گام برنامه با ابزار {hint} تأیید نشده است")
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
