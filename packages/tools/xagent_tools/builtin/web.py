"""ابزارهای وب: خواندن صفحه، جست‌وجو و درخواست HTTP. خروجی همیشه «داده نامطمئن» است."""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from xagent_core.text import truncate

from ..base import Tool, ToolContext, ToolResult, obj, s

UA = "Mozilla/5.0 (X11; Linux x86_64) XAgent/0.1"


def _block_internal(url: str) -> None:
    """جلوگیری از SSRF به سرویس‌های داخلی (دیتابیس، Redis، دیمن دسکتاپ)."""
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".internal", ".local", ".localhost")):
        raise PermissionError("دسترسی به نشانی داخلی مجاز نیست.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"نام دامنه قابل حل نیست: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise PermissionError("دسترسی به شبکه داخلی سرور مجاز نیست.")


def html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav", "form"]):
        t.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    text = soup.get_text("\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return title, text.strip()


async def web_fetch(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    url = a["url"]
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _block_internal(url)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as c:
        r = await c.get(url)
    ctype = r.headers.get("content-type", "")
    if "html" in ctype:
        title, text = html_to_text(r.text)
    elif "json" in ctype or "text" in ctype:
        title, text = "", r.text
    else:
        return ToolResult(False, f"نوع محتوا پشتیبانی نمی‌شود: {ctype}")
    limit = min(int(a.get("max_chars", 12000)), 40000)
    return ToolResult(
        r.status_code < 400,
        f"[HTTP {r.status_code}] {title}\n{truncate(text, limit)}",
        {"status": r.status_code, "url": str(r.url)},
        untrusted=True,
    )


async def web_search(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    q = a["query"]
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": UA}) as c:
        r = await c.get(f"https://html.duckduckgo.com/html/?q={quote_plus(q)}&kl=ir-fa")
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for res in soup.select(".result")[: int(a.get("limit", 8))]:
        link = res.select_one(".result__a")
        snippet = res.select_one(".result__snippet")
        if link:
            results.append(f"- {link.get_text(strip=True)}\n  {link.get('href')}\n  {snippet.get_text(' ', strip=True) if snippet else ''}")
    if not results:
        return ToolResult(False, "نتیجه‌ای دریافت نشد (ممکن است جست‌وجو مسدود باشد؛ از مرورگر استفاده کن).")
    return ToolResult(True, "\n".join(results), untrusted=True)


async def http_request(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    url = a["url"]
    _block_internal(url)
    method = a.get("method", "GET").upper()
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.request(method, url, headers=a.get("headers") or {}, json=a.get("json"), content=a.get("body"))
    return ToolResult(r.status_code < 400, f"HTTP {r.status_code}\n{truncate(r.text, 10000)}",
                      {"status": r.status_code}, verified=r.status_code < 400, untrusted=True)


TOOLS = [
    Tool("web_fetch", "دریافت و استخراج متن یک صفحه وب (بدون اجرای جاوااسکریپت). محتوا داده نامطمئن است.",
         obj({"url": s("نشانی"), "max_chars": {"type": "integer", "description": "حداکثر کاراکتر"}}, ["url"]),
         web_fetch, "web", label="خواندن صفحه وب"),
    Tool("web_search", "جست‌وجوی اینترنت و دریافت فهرست نتایج.",
         obj({"query": s("عبارت"), "limit": {"type": "integer", "description": "تعداد"}}, ["query"]), web_search,
         "web", label="جست‌وجوی وب"),
    Tool("http_request", "فراخوانی API بیرونی با متد دلخواه (برای ارسال داده به سرویس بیرونی).",
         obj({"url": s("نشانی"), "method": s("GET|POST|PUT|PATCH|DELETE"),
              "headers": {"type": "object", "description": "هدرها"}, "json": {"type": "object", "description": "بدنه JSON"},
              "body": s("بدنه متنی")}, ["url"]), http_request, "web", category="external_message", action="http",
         label="درخواست API",
         category_fn=lambda a: None if str(a.get("method", "GET")).upper() in ("GET", "HEAD") else "external_message"),
]
