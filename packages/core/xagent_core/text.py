"""ابزارهای متن فارسی: نرمال‌سازی، تشخیص و برش امن."""

from __future__ import annotations

import re
import unicodedata

_ARABIC_TO_PERSIAN = str.maketrans(
    {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ة": "ه",
        "ۀ": "ه",
        "أ": "ا",
        "إ": "ا",
        "ٱ": "ا",
        "ؤ": "و",
        "٠": "0",
        "١": "1",
        "٢": "2",
        "٣": "3",
        "٤": "4",
        "٥": "5",
        "٦": "6",
        "٧": "7",
        "٨": "8",
        "٩": "9",
        "۰": "0",
        "۱": "1",
        "۲": "2",
        "۳": "3",
        "۴": "4",
        "۵": "5",
        "۶": "6",
        "۷": "7",
        "۸": "8",
        "۹": "9",
    }
)
_DIACRITICS = re.compile(r"[ً-ٰٟـ]")
_SPACES = re.compile(r"[\s‌‏‎]+")


def normalize_fa(text: str) -> str:
    """نرمال‌سازی برای جست‌وجو و مقایسه: یکسان‌سازی حروف عربی/فارسی، ارقام، حذف اعراب و نیم‌فاصله."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).translate(_ARABIC_TO_PERSIAN)
    t = _DIACRITICS.sub("", t)
    t = _SPACES.sub(" ", t)
    return t.strip().lower()


_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_digits(value: object) -> str:
    return str(value).translate(_FA_DIGITS)


def truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def estimate_tokens(text: str) -> int:
    """تخمین سریع و محافظه‌کارانه تعداد توکن (بدون وابستگی به tokenizer).

    متن فارسی در اغلب tokenizerها پرهزینه‌تر از انگلیسی است؛ برای حروف غیرلاتین ضریب بیشتری لحاظ می‌شود.
    """
    if not text:
        return 0
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ascii_count = len(text) - non_ascii
    return int(ascii_count / 4 + non_ascii / 2.2) + 1
