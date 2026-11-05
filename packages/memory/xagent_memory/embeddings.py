"""جاسازی متن (Embedding).

دو حالت:
1. `local_hash`: جاسازی محلی قطعی بر پایه Feature Hashing روی n-gramهای حرفی و کلمه‌ای متن نرمال‌شده.
   بدون هزینه، بدون اینترنت و بدون GPU — روی هر سخت‌افزاری اجرا می‌شود و برای شباهت واژگانی فارسی خوب است.
2. `model`: جاسازی از طریق API مدل تعریف‌شده در تنظیمات (مثلاً text-embedding-3-small یا Gemini).

ابعاد همیشه EMBED_DIM است؛ بردار کوتاه‌تر با صفر پر می‌شود (شباهت کسینوسی تغییر نمی‌کند).
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Protocol

from xagent_core.models import EMBED_DIM
from xagent_core.text import normalize_fa

LOCAL_MODEL_NAME = "local-hash-v1"


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _h(token: str) -> tuple[int, float]:
    d = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    v = struct.unpack("<Q", d)[0]
    return v % EMBED_DIM, (1.0 if (v >> 63) & 1 else -1.0)


def hash_embed(text: str) -> list[float]:
    t = normalize_fa(text)
    vec = [0.0] * EMBED_DIM
    if not t:
        return vec
    words = [w for w in t.split(" ") if w]
    feats: list[tuple[str, float]] = []
    for w in words:
        feats.append((f"w:{w}", 1.0))
        padded = f"#{w}#"
        for n in (3, 4):
            for i in range(max(1, len(padded) - n + 1)):
                feats.append((f"c{n}:{padded[i:i + n]}", 0.5))
    for a, b in zip(words, words[1:], strict=False):
        feats.append((f"b:{a}_{b}", 0.7))
    for tok, w in feats:
        idx, sign = _h(tok)
        vec[idx] += sign * w
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class HashEmbedder:
    name = LOCAL_MODEL_NAME

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hash_embed(t) for t in texts]


def fit_dim(vec: list[float]) -> list[float]:
    if len(vec) == EMBED_DIM:
        return vec
    if len(vec) > EMBED_DIM:
        v = vec[:EMBED_DIM]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]
    return vec + [0.0] * (EMBED_DIM - len(vec))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


async def get_embedder(session) -> Embedder:  # noqa: ANN001
    """انتخاب جاسازی بر اساس تنظیمات؛ در صورت هر خطا به جاسازی محلی برمی‌گردد."""
    from xagent_core.settings_store import AISettings, get_setting

    ai = await get_setting(session, "ai", AISettings)
    if not ai.embedding_model_id:
        return HashEmbedder()
    try:
        from xagent_agent.providers.registry import ModelEmbedder, load_model

        bound = await load_model(session, ai.embedding_model_id)
        if bound is None:
            return HashEmbedder()
        return ModelEmbedder(bound)
    except Exception:
        return HashEmbedder()
