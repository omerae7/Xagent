"""ساخت صفحه آزمون برای اجزای واقعی پنل (React) با esbuild همان مخزن و اجرای آن در Chromium واقعی."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "apps" / "web"
ESBUILD = WEB / "node_modules" / ".bin" / "esbuild"


def available() -> bool:
    return ESBUILD.exists() and (WEB / "node_modules" / "react-dom").exists()


def build(entry_source: str) -> tuple[str, str]:
    """entry_source: کد TSX ورودی؛ خروجی (مسیر پوشه، نام bundle). مسیرهای import مطلق به src پنل‌اند."""
    out = tempfile.mkdtemp(prefix="xa-web-harness-")
    entry = Path(out) / "entry.tsx"
    entry.write_text(entry_source.replace("@web/", str(WEB / "src") + "/"))
    env = {**os.environ, "NODE_PATH": str(WEB / "node_modules")}
    proc = subprocess.run([str(ESBUILD), str(entry), "--bundle", "--format=iife", "--jsx=automatic",
                           f"--outfile={out}/bundle.js", "--define:process.env.NODE_ENV=\"production\"",
                           "--log-level=error"], capture_output=True, text=True, env=env, timeout=120)
    if proc.returncode != 0:
        shutil.rmtree(out, ignore_errors=True)
        raise RuntimeError(f"esbuild failed: {proc.stderr[-2000:]}")
    (Path(out) / "index.html").write_text(
        '<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"></head>'
        '<body><div id="root"></div><script src="/bundle.js"></script></body></html>')
    return out, "bundle.js"


def routes_for(folder: str) -> dict:
    files = {p.name: p.read_text() for p in Path(folder).iterdir() if p.is_file()}
    return {"/": (200, {}, files["index.html"]),
            "/bundle.js": (200, {"Content-Type": "application/javascript"}, files["bundle.js"])}


def payload_script(payloads: list[str]) -> str:
    return f"window.__PAYLOADS = {json.dumps(payloads, ensure_ascii=False)};"
