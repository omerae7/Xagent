"""درخواست HTTP امن برای ابزارهای وب (ضد SSRF).

لایه‌ها:
۱. فقط http/https؛ نام‌های داخلی (localhost، *.internal، *.local، …) رد می‌شوند.
۲. نام دامنه «یک بار» حل می‌شود و همه آدرس‌ها باید عمومی (global) باشند؛ IPv4-mapped، CGNAT، Link-local،
   Multicast، Unspecified و شبکه‌های خصوصی رد می‌شوند.
۳. اتصال دقیقاً به همان IP بررسی‌شده انجام می‌شود (Pinning) و Host/SNI نام اصلی را نگه می‌دارد؛
   بنابراین DNS rebinding بین بررسی و اتصال بی‌اثر است و گواهی TLS همچنان برای نام دامنه بررسی می‌شود.
۴. Redirect ها دستی دنبال می‌شوند و هر گام دوباره از همین مسیر عبور می‌کند؛ هدرهای احراز هویت در تغییر میزبان حذف می‌شوند.
۵. پس از اتصال، آدرس واقعی طرف مقابل دوباره بررسی می‌شود (دفاع در عمق).
۶. پروکسی محیطی نادیده گرفته می‌شود (trust_env=False) تا مسیر اتصال قابل کنترل بماند؛ پروکسی صریح فقط با
   XAGENT_WEB_PROXY (در این حالت Pinning ممکن نیست ولی هر گام همچنان بررسی می‌شود).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64) XAgent/0.1"
BLOCKED_SUFFIXES = (".internal", ".local", ".localhost", ".lan", ".home.arpa", ".intranet", ".corp")
REDIRECTS = {301, 302, 303, 307, 308}
SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-api-key"}


class UnsafeDestination(PermissionError):
    """مقصد درخواست به شبکه داخلی یا آدرس غیرمجاز اشاره می‌کند."""


def validate_ip(value: str):
    """آدرس باید عمومی باشد؛ در غیر این صورت UnsafeDestination."""
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return validate_ip(str(ip.ipv4_mapped))
        if ip.sixtofour is not None:
            validate_ip(str(ip.sixtofour))
        if ip.teredo is not None:
            validate_ip(str(ip.teredo[1]))
    if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        raise UnsafeDestination(f"دسترسی به آدرس داخلی/غیرعمومی ({value}) مجاز نیست.")
    return ip


def _check_host(host: str) -> str:
    h = (host or "").strip("[]").rstrip(".").lower()
    if not h or h == "localhost" or h.endswith(BLOCKED_SUFFIXES) or "." not in h and ":" not in h and not h.isdigit():
        raise UnsafeDestination(f"دسترسی به نشانی داخلی ({host}) مجاز نیست.")
    return h


async def resolve_public(host: str, port: int) -> list[str]:
    """یک بار حل نام و بررسی همه آدرس‌ها؛ خروجی فهرست IP های مجاز."""
    h = _check_host(host)
    try:
        validate_ip(h)
        return [h]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(None, lambda: socket.getaddrinfo(h, port, type=socket.SOCK_STREAM))
    except socket.gaierror as e:
        raise ValueError(f"نام دامنه قابل حل نیست: {host}") from e
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            validate_ip(ip)
            ips.append(ip)
    if not ips:
        raise ValueError(f"نام دامنه آدرسی ندارد: {host}")
    return ips


@dataclass
class SafeResponse:
    status_code: int
    url: str
    headers: httpx.Headers
    content: bytes
    truncated: bool = False

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        charset = "utf-8"
        for part in ctype.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset" and v:
                charset = v.strip('"')
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self):
        import json

        return json.loads(self.text)


async def safe_request(method: str, url: str, *, headers: dict | None = None, json=None, content=None,
                       timeout: float = 30.0, max_bytes: int = 5_000_000, max_redirects: int = 5,
                       verify=True) -> SafeResponse:
    method = method.upper()
    hdrs = {"User-Agent": UA, **(headers or {})}
    hdrs = {k: v for k, v in hdrs.items() if k.lower() != "host"}
    proxy = os.environ.get("XAGENT_WEB_PROXY") or None
    current = url
    for _hop in range(max_redirects + 1):
        u = httpx.URL(current)
        if u.scheme not in ("http", "https"):
            raise UnsafeDestination("فقط نشانی‌های http و https مجازند.")
        if u.userinfo:
            raise UnsafeDestination("نشانی دارای نام کاربری/رمز پذیرفته نمی‌شود.")
        host = u.raw_host.decode("ascii")
        port = u.port or (443 if u.scheme == "https" else 80)
        ips = await resolve_public(host, port)
        req_headers = {**hdrs, "Host": host if u.port is None else f"{host}:{u.port}"}
        ext: dict = {}
        if proxy:
            target = u  # با پروکسی صریح Pinning ممکن نیست؛ هر گام همچنان بررسی شده است
        else:
            target = u.copy_with(host=ips[0])
            if u.scheme == "https":
                ext["sni_hostname"] = host
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout, proxy=proxy,
                                     verify=verify) as client:
            req = client.build_request(method, target, headers=req_headers, json=json, content=content, extensions=ext)
            r = await client.send(req, stream=True)
            try:
                if not proxy:
                    stream = r.extensions.get("network_stream")
                    peer = stream.get_extra_info("server_addr") if stream is not None else None
                    if peer:
                        validate_ip(peer[0])
                if r.status_code in REDIRECTS and r.headers.get("location"):
                    nxt = urljoin(str(u), r.headers["location"])
                    if r.status_code == 303 or (r.status_code in (301, 302) and method not in ("GET", "HEAD")):
                        method, json, content = "GET", None, None
                    if httpx.URL(nxt).host != u.host:
                        hdrs = {k: v for k, v in hdrs.items() if k.lower() not in SENSITIVE_HEADERS}
                    current = nxt
                    continue
                buf = bytearray()
                truncated = False
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        truncated = True
                        del buf[max_bytes:]
                        break
                return SafeResponse(r.status_code, str(u), r.headers, bytes(buf), truncated)
            finally:
                await r.aclose()
    raise UnsafeDestination(f"تعداد تغییر مسیرها بیش از {max_redirects} است.")
