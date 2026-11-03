"""تست‌های واحد: متن فارسی، مسیریاب، Harness، کنترلر حلقه، زمان‌بندی و جاسازی."""

from __future__ import annotations

from datetime import UTC, datetime

from xagent_agent.classifier import classify, detect_command
from xagent_agent.harness import Decision, evaluate, redact
from xagent_agent.stoploss import LoopController
from xagent_core.models import HarnessRule, Routine
from xagent_core.text import estimate_tokens, normalize_fa
from xagent_core.timeutil import jalali_str, parse_jalali_date
from xagent_memory.embeddings import cosine, hash_embed
from xagent_memory.revisions import is_regression
from xagent_tools.schedule import compute_next_run, validate_routine


def test_normalize_fa():
    assert normalize_fa("علي‌رضا  كيست؟ ۱۲۳") == "علی رضا کیست؟ 123"
    assert estimate_tokens("سلام دنیا") > 0


def test_jalali_roundtrip():
    dt = parse_jalali_date("1404/01/01 12:00", "Asia/Tehran")
    assert dt.tzinfo is not None
    assert jalali_str(dt, "Asia/Tehran", persian_digits=False) == "1404/01/01 12:00"


def test_commands_detected_without_llm():
    assert detect_command("/stop")[0] == "stop_all"
    assert detect_command("توقف همه")[0] == "stop_all"
    assert detect_command("آندو 3") == ("undo", "3")
    assert detect_command("وضعیت")[0] == "status"
    assert detect_command("یه تسک بساز") is None


def test_classifier_routes():
    assert classify("سلام").complexity == "trivial"
    r = classify("یه تسک برای فردا بساز")
    assert r.path == "worker" and "tasks" in r.tool_groups
    r = classify("هر روز ساعت ۹ قیمت دلار را بررسی کن، گزارش بنویس و بعد به علی پیام بده")
    assert r.path == "manager" and r.risk == "high"
    assert classify("لطفاً پرداخت فاکتور را انجام بده").risk == "high"


def _rule(mode: str, conditions: dict | None = None, text: str = "") -> HarnessRule:
    return HarnessRule(category="x", mode=mode, rule_text=text, conditions=conditions or {})


def test_harness_modes():
    assert evaluate(_rule("auto"), "external_message", "send", {}).action == "allow"
    assert evaluate(_rule("ask"), "external_message", "send", {}).action == "ask"
    assert evaluate(_rule("forbidden"), "external_message", "send", {}).action == "deny"
    # پرداخت هرگز خودکار نمی‌شود
    assert evaluate(_rule("auto"), "payment", "pay", {}).action == "ask"


def test_harness_conditional():
    rule = _rule("conditional", {"allow": ["example.com", "*.work.ir"], "deny": ["gmail.com", "bank*"]})
    assert evaluate(rule, "login", "type", {"url": "https://app.work.ir/login"}).action == "allow"
    assert evaluate(rule, "login", "type", {"url": "https://mail.gmail.com"}).action == "ask"
    assert evaluate(rule, "login", "type", {"url": "https://other.org"}).action == "ask"
    files = _rule("conditional", {"ask_actions": ["delete"]})
    assert evaluate(files, "file_modify", "write", {"path": "a.txt"}).action == "allow"
    assert evaluate(files, "file_modify", "delete", {"path": "a.txt"}).action == "ask"
    assert isinstance(evaluate(None, "login", "x", {}), Decision)


def test_redact_hides_secrets():
    out = redact({"password": "123", "api_key": "k", "text": "x" * 3000, "nested": {"token": "t"}})
    assert out["password"] == "••••" and out["api_key"] == "••••" and out["nested"]["token"] == "••••"
    assert len(out["text"]) <= 1500


def test_loop_controller_escalates_then_aborts():
    lc = LoopController(max_repeat=2, max_consecutive_errors=3)
    signals = [lc.observe("web_fetch", {"url": "x"}, False) for _ in range(6)]
    assert "change_strategy" in signals or "escalate" in signals
    assert signals[-1] == "abort"
    lc2 = LoopController()
    assert lc2.observe("a", {"x": 1}, True) == "ok"
    assert lc2.observe("b", {"x": 1}, True) == "ok"


def test_regression_detection():
    assert is_regression({"avg_tokens": 1000, "fail_rate": 0.0}, {"runs": 2, "failures": 0, "tokens": 4000})
    assert is_regression({"fail_rate": 0.0}, {"runs": 2, "failures": 2, "tokens": 10})
    assert is_regression({"avg_tokens": 1000, "fail_rate": 0.0}, {"runs": 3, "failures": 0, "tokens": 3000}) is None


def test_hash_embedding_similarity():
    a = hash_embed("گزارش فروش هفتگی را برای مدیر بفرست")
    b = hash_embed("گزارش فروش هفتگی برای مدیر ارسال شود")
    c = hash_embed("قیمت بیت کوین امروز چند است")
    assert len(a) == 768
    assert cosine(a, b) > cosine(a, c)


def test_routine_schedule():
    r = Routine(name="t", instruction="x", trigger_type="cron", cron_expr="0 9 * * *", timezone="Asia/Tehran")
    assert validate_routine(r) is None
    nxt = compute_next_run(r, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    # ۹ صبح تهران = ۰۵:۳۰ UTC
    assert (nxt.hour, nxt.minute) == (5, 30)
    bad = Routine(name="t", instruction="x", trigger_type="interval", interval_seconds=10)
    assert validate_routine(bad)


def test_token_regression_requires_comparable_baseline_and_two_runs():
    # Regression tokens must be compared only within a comparable workload.
    assert is_regression({"avg_tokens": 100}, {"runs": 3, "tokens": 1500, "failures": 0}) is None
    assert is_regression({"avg_tokens": 100, "comparison_scope": "routine"},
                         {"runs": 1, "tokens": 500, "failures": 0}) is None
    assert is_regression({"avg_tokens": 100, "comparison_scope": "routine"},
                         {"runs": 2, "tokens": 500, "failures": 0}) is not None
    assert is_regression({}, {"runs": 3, "tokens": 300, "failures": 2}) is not None
