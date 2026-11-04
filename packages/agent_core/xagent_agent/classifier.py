"""مسیریاب سبک و قطعی (Lightweight Classifier/Router).

بدون فراخوانی LLM تشخیص می‌دهد درخواست:
- فرمان قطعی است (توقف/ادامه/Undo/وضعیت) → بدون مدل اجرا می‌شود
- ساده و کم‌ریسک است → مستقیم به مدل کارمند (بدون هدف‌سنجی و برنامه‌ریزی جداگانه)
- چندمرحله‌ای، مبهم یا حساس است → مدیر وارد عمل می‌شود
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Literal

from xagent_core.text import normalize_fa

from .intent import parse_intent

Complexity = Literal["trivial", "simple", "complex"]


@dataclass
class Route:
    complexity: Complexity = "simple"
    risk: Literal["low", "high"] = "low"
    path: Literal["command", "worker", "manager"] = "worker"
    command: str | None = None
    needs_vision: bool = False
    needs_audio: bool = False
    tool_groups: list[str] = field(default_factory=list)
    memory_layers: list[str] = field(default_factory=lambda: ["behavioral", "semantic", "working"])
    reasons: list[str] = field(default_factory=list)
    # مأموریت زمان‌دار تشخیص‌داده‌شده (قطعی، بدون LLM) — مبنای انتخاب ابزار و پیش‌فرض‌های روتین
    intent: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_COMMANDS: list[tuple[str, re.Pattern[str]]] = [
    ("stop_all", re.compile(r"^(/stop_?all|/stop|توقف (همه|کامل|اضطراری)|همه (کارها )?رو? متوقف کن|stop all)$")),
    ("resume_all", re.compile(r"^(/resume_?all|/resume|ادامه (همه|کارها)|resume all)$")),
    ("undo", re.compile(r"^(/undo|undo|آندو|برگردون|بازگردانی|یادگیری (قبلی|آخر) ?رو? (پاک|برگردون|حذف) کن)(\s+\d+)?$")),
    ("status", re.compile(r"^(/status|وضعیت|وضعیت سیستم|status)$")),
    ("help", re.compile(r"^(/help|راهنما|help)$")),
]

_TRIVIAL = re.compile(
    r"^(سلام|درود|ممنون|مرسی|متشکرم|خسته نباشی|خداحافظ|شب بخیر|صبح بخیر|عالی|اوکی|باشه|ok|thanks|thank you|hi|hello)[\s!.؟?]*$"
)
_COMPLEX_MARKERS = [
    r"و (بعد|سپس)",
    r"\bسپس\b",
    r"مرحله",
    r"برنامه ?ریزی",
    r"تحلیل",
    r"گزارش",
    r"مقایسه",
    r"بررسی کن",
    r"پیگیری",
    r"هر (روز|هفته|ساعت|ماه)",
    r"روتین",
    r"خودکار",
    r"جست ?و ?جو",
    r"تحقیق",
    r"\bresearch\b",
    r"\banaly[sz]e\b",
    r"\bplan\b",
    r"step by step",
]
_RISK_MARKERS = {
    "payment": r"(پرداخت|واریز|انتقال (پول|وجه)|خرید|کارت به کارت|\bpay\b|\bpayment\b)",
    "external_message": r"(پیام (بده|بفرست)|ارسال کن|بفرست (به|برای)|ایمیل (بزن|بفرست)|send (a )?message|\bemail\b)",
    "file_modify": r"(حذف کن|پاک کن|delete|remove|rm -rf)",
    "login": r"(لاگین|وارد (حساب|اکانت)|ورود به|\blogin\b|sign in|رمز عبور|password)",
    "install_tool": r"(نصب کن|install|گیت ?هاب|github\.com)",
    "code_exec": r"(اجرا کن|ترمینال|دستور|اسکریپت|\bbash\b|\bshell\b|python)",
}
_TOOL_GROUP_MARKERS = {
    "tasks": r"(تسک|کار(ها)?ی?|وظیفه|یادآوری|todo|task|ددلاین|مهلت)",
    "routines": r"(روتین|هر (روز|هفته|ساعت|ماه|دقیقه)|زمان ?بندی|cron|routine|schedule)",
    "crm": r"(همکار|مخاطب|مشتری|پروژه|crm|contact)",
    "telegram": r"(تلگرام|telegram|پیام|چت)",
    "web": r"(سایت|وب|لینک|http|www\.|جست ?و ?جو|اینترنت|خبر|قیمت|بگرد|سرچ|پیدا کن|بررسی آنلاین|نرخ ارز|دلار|یورو|بورس)",
    "browser": r"(مرورگر|کروم|browser|chrome|صفحه|کلیک)",
    "desktop": r"(دسکتاپ|صفحه نمایش|اسکرین ?شات|screenshot|desktop)",
    "files": r"(فایل|پوشه|سند|file|folder|csv|pdf|excel)",
    "memory": r"(یادت|حافظه|قبلا|دفعه (قبل|پیش)|سابقه|تاریخچه|history|remember)",
    "episodes": r"(دیروز|هفته (پیش|گذشته)|ماه (پیش|گذشته)|چه کارهایی|گزارش کار|سابقه)",
    # اجرای کد/محاسبه با Python در Sandbox (ابزار shell_exec؛ دسته Harness «اجرای کد» همچنان اعمال می‌شود)
    "system": r"(نصب|اجرا کن|ترمینال|اسکریپت|shell|bash|پایتون|python|\bscript\b|\brun\b[^.؟?!]{0,30}\bcode\b|"
              r"(کد|برنامه)[^.؟?!]{0,30}(اجرا|بنویس و اجرا))",
    # کار کدنویسی روی مخزن → اجراکننده بیرونی (فقط اگر کارفرما Cursor را تنظیم کرده باشد بارگذاری می‌شود)
    "external": r"(\bباگ|\bbug\b|ریپو|\brepo\b|مخزن|pull request|\bpr\b|ریفکتور|refactor|کامیت|\bcommit\b|"
                r"کرسر|\bcursor\b|(رفع|اصلاح|فیکس|درست کردن|بنویس|پیاده ?سازی)[^.؟?!]{0,40}(کد|تست|فانکشن|تابع|ماژول)|"
                r"(\bکد|تست|فانکشن|تابع|ماژول)[^.؟?!]{0,40}(رفع|اصلاح|فیکس|درست کن|بنویس|پیاده ?سازی))",
    # مرز کلمه لازم است: «ram» داخل «telegram» و «رم» داخل «خبرم» نباید ابزار سرور را فعال کند
    "host": r"(سرور|\bvps\b|میزبان|ری ?استارت|\brestart\b|\bلاگ|\blogs?\b|دیسک|\bdisk\b|\bرم\b|\bram\b|\bcpu\b|"
            r"پردازنده|حافظه سرور|docker|داکر|کانتینر|systemd|سرویس|آپدیت|به ?روز ?رسانی|\bupdate\b|رول ?بک|"
            r"rollback|بکاپ|پشتیبان|backup|نسخه|ترمینال|لینوکس|linux|پورت|فایروال|uptime|doctor|xagent)",
    "admin": r"(تنظیم|مدل|کارمند|ابزار|خطا|ارور|مشکل|عیب|اصلاح|درست کن|ارائه ?دهنده|مسیریابی|api|کلید|خودت|سیستم|"
             r"وضعیت|سلامت|بهینه|هزینه|فعال کن|غیرفعال|کشف|آزمون|تست کن|config|setting|model|employee|debug|fix)",
}


def detect_command(text: str) -> tuple[str, str] | None:
    t = normalize_fa(text).strip()
    for name, pat in _COMMANDS:
        m = pat.match(t)
        if m:
            arg = ""
            nums = re.findall(r"\d+", t)
            if nums:
                arg = nums[-1]
            return name, arg
    return None


def classify(text: str, has_images: bool = False, has_audio: bool = False, trigger: str = "chat") -> Route:
    r = Route()
    t = normalize_fa(text)
    cmd = detect_command(text)
    if cmd:
        r.path, r.command, r.complexity = "command", cmd[0], "trivial"
        r.reasons.append(f"فرمان قطعی: {cmd[0]}")
        r.memory_layers = []
        return r
    r.needs_vision = has_images
    r.needs_audio = has_audio
    for group, pat in _TOOL_GROUP_MARKERS.items():
        if re.search(pat, t):
            r.tool_groups.append(group)
    # تشخیص مأموریت زمان‌دار: «هر دقیقه»، «دقیقه‌ای یک بار»، «تا ۱۰ دقیقه آینده»، «every 2 minutes» …
    intent = parse_intent(text)
    if intent.one_shot and not intent.recurring and trigger not in ("routine", "event"):
        if "tasks" not in r.tool_groups:
            r.tool_groups.append("tasks")
        r.intent = intent.to_dict()
        r.reasons.append("مأموریت زمان‌دار: " + intent.describe())
    if intent.recurring and trigger not in ("routine", "event"):
        for g in ("routines", *[sv for sv in intent.services if sv in ("telegram", "web", "tasks")]):
            if g not in r.tool_groups:
                r.tool_groups.append(g)
        r.intent = intent.to_dict()
        r.reasons.append("مأموریت زمان‌دار: " + intent.describe())
    if "admin" in r.tool_groups:
        r.reasons.append("درخواست اداره/عیب‌یابی سیستم")
    risky = [cat for cat, pat in _RISK_MARKERS.items() if re.search(pat, t)]
    if risky:
        r.risk = "high"
        r.reasons.append("عملیات حساس: " + "، ".join(risky))
    if _TRIVIAL.match(t) and not has_images:
        r.complexity, r.path = "trivial", "worker"
        r.memory_layers = ["behavioral", "working"]
        r.reasons.append("پیام کوتاه احوال‌پرسی")
        return r
    score = 0
    score += sum(1 for p in _COMPLEX_MARKERS if re.search(p, t))
    if len(t) > 350:
        score += 1
    if len(t) > 900:
        score += 1
    if t.count("\n") >= 3 or len(re.findall(r"[\.!؟\?]", t)) >= 4:
        score += 1
    if len(r.tool_groups) >= 3:
        score += 1
    if trigger in ("routine", "event"):
        score += 1
        r.memory_layers.append("experiential")
    if "admin" in r.tool_groups and trigger in ("chat", "voice", "telegram"):
        score += 2
    if score >= 2 or r.risk == "high":
        r.complexity, r.path = "complex", "manager"
        r.reasons.append(f"امتیاز پیچیدگی {score}")
        if "experiential" not in r.memory_layers:
            r.memory_layers.append("experiential")
    else:
        r.complexity, r.path = "simple", "worker"
        r.reasons.append(f"کار ساده (امتیاز {score})")
    return r
