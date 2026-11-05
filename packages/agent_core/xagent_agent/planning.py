"""Model plans are bounded advice, never authorization or objective truth."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

GROUPS = {"core", "tasks", "routines", "crm", "telegram", "web", "browser", "desktop", "files", "memory",
          "episodes", "system", "installed", "external", "admin", "host"}
LAYERS = {"behavioral", "semantic", "experiential", "episodic", "working"}


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    title: str = Field(min_length=1, max_length=500)
    tool_hint: str = "auto"


def _names(value: Any, allowed: set[str]) -> list[str]:
    values = value if isinstance(value, list) else re.split(r"[,،\s]+", value) if isinstance(value, str) else []
    return sorted({v for v in values if isinstance(v, str) and v in allowed})


def normalize_plan(raw: Any, catalog: dict[str, str]) -> dict:
    """Normalize old checkpoints and provider output. A group cannot become a tool ID.

    Invalid hints are removed; their recognized groups may inform tool discovery.
    No step becomes a mandatory effect just because the model suggested it.
    """
    raw = raw if isinstance(raw, dict) else {}
    groups = set(_names(raw.get("tool_groups"), GROUPS))
    steps = []
    for item in (raw.get("steps") if isinstance(raw.get("steps"), list) else [])[:12]:
        try:
            step = PlanStep.model_validate(item)
        except ValidationError:
            continue
        hint = step.tool_hint.strip()
        if hint in catalog:
            groups.add(catalog[hint])
        elif hint not in {"auto", "none"}:
            groups.update(_names(hint, GROUPS))
            hint = "auto"
        steps.append({"title": step.title.strip(), "tool_hint": hint})
    return {"steps": steps or [{"title": "اجرای درخواست و بررسی نتیجه", "tool_hint": "auto"}],
            "tool_groups": sorted(groups & GROUPS), "memory_layers": _names(raw.get("memory_layers"), LAYERS),
            "notes": raw.get("notes", "")[:1000] if isinstance(raw.get("notes", ""), str) else ""}


def normalize_goal(raw: Any, request: str, *, scheduled_creation: bool = False) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    criteria = raw.get("success_criteria")
    criteria = [c.strip()[:500] for c in criteria[:12] if isinstance(c, str) and c.strip()] if isinstance(criteria, list) else []
    return {"goal": raw.get("goal")[:500] if isinstance(raw.get("goal"), str) else request[:500],
            "success_criteria": ["روتین یا تسک با زمان‌بندی و محدوده درخواستی ذخیره و بازخوانی شده باشد"]
            if scheduled_creation else criteria or ["انجام درخواست و گزارش نتیجه واقعی"],
            "ambiguity": raw.get("ambiguity", "") if isinstance(raw.get("ambiguity", ""), str) else "",
            "risk": "high" if raw.get("risk") == "high" else "low"}
