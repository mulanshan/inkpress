from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse
from weakref import WeakKeyDictionary

import httpx

from inkpress.logging import get_logger

logger = get_logger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

MAX_TEXT_BYTES = 15 * 1024 * 1024  # 15 MB
MAX_BINARY_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_REDIRECTS = 5
CHALLENGE_PEEK_BYTES = 65536

_HTTP_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([a-zA-Z0-9._-]+)['\"]?", re.IGNORECASE)
_META_CHARSET_RE = re.compile(
    r"<meta[^>]+charset\s*=\s*['\"]?\s*([a-zA-Z0-9._-]+)",
    re.IGNORECASE,
)
_XML_ENCODING_RE = re.compile(
    r"<\?xml[^>]+encoding\s*=\s*['\"]([a-zA-Z0-9._-]+)['\"]",
    re.IGNORECASE,
)

# Common domains typically needing outbound proxy in mainland residential networks
OVERSEAS_HINTS = (
    "github.com",
    "githubusercontent.com",
    "ycombinator.com",
    "medium.com",
    "substack.com",
    "economist.com",
    "nytimes.com",
    "reuters.com",
    "bloomberg.com",
    "bbc.com",
    "bbc.co.uk",
    "theguardian.com",
    "technologyreview.com",
    "producthunt.com",
    "simonwillison.net",
    "x.com",
    "twitter.com",
    "reddit.com",
    "wikipedia.org",
    "zaobao.com",
    "zaochenbao.com",
    "cw.com.tw",
    "v2ex.com",
    "mktnews.net",
    "fastbull.com",
    "googleapis.com",
    "imgur.com",
    "huggingface.co",
    "twimg.com",
    "substackcdn.com",
    "redd.it",
)

DOMESTIC_HINTS = (
    ".cn",
    "dapenti.com",
    "sspai.com",
    "zhihu.com",
    "thepaper.cn",
    "36kr.com",
    "ithome.com",
    "wallstreetcn.com",
    "cls.cn",
    "xueqiu.com",
    "ruanyifeng.com",
    "juejin.cn",
    "douban.com",
    "doubanio.com",
    "toutiaoimg.com",
    "byteimg.com",
    "hdslb.com",
    "zhimg.com",
    "hupu.com",
    "weibo.com",
    "sina.com",
    "qq.com",
    "qpic.cn",
    "163.com",
    "ifeng.com",
    "solidot.org",
    "freebuf.com",
)

# Anti-hotlink CDNs returning 403/418 when fetched without an expected origin Referer
IMAGE_DOMAIN_DEFAULT_REFERERS: tuple[tuple[str, str], ...] = (
    ("doubanio.com", "https://movie.douban.com/"),
    ("sinaimg.cn", "https://weibo.com/"),
    ("zhimg.com", "https://www.zhihu.com/"),
    ("hdslb.com", "https://www.bilibili.com/"),
    ("sspai.com", "https://sspai.com/"),
    ("ithome.com", "https://www.ithome.com/"),
    ("thepaper.cn", "https://www.thepaper.cn/"),
    ("36krcdn.com", "https://36kr.com/"),
    ("toutiaoimg.com", "https://www.toutiao.com/"),
    ("byteimg.com", "https://www.toutiao.com/"),
    ("qpic.cn", "https://mp.weixin.qq.com/"),
    ("dapenti.com", "https://www.dapenti.com/"),
    ("penti.org", "https://www.dapenti.com/"),
)


def infer_default_image_referer(url: str) -> str:
    """Return a known anti-hotlink Referer for well-known image CDNs, or empty string."""
    host = (urlparse((url or "").strip()).hostname or "").strip().lower().rstrip(".")
    if not host:
        return ""
    for domain, ref in IMAGE_DOMAIN_DEFAULT_REFERERS:
        if _host_matches_domain(host, domain):
            return ref
    return ""


def _host_matches_domain(host: str, domain: str) -> bool:
    if domain.startswith("."):
        return host.endswith(domain)
    return host == domain or host.endswith("." + domain)


_GFW_FORGED_TEREDO_AAAA_NET = ipaddress.IPv6Network("2001::/96")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


async def assert_safe_public_url(
    url: str,
    *,
    allow_internal_hosts: set[str] | None = None,
) -> None:
    """Validate URL scheme and block private/loopback/link-local/multicast/reserved IPs (SSRF guard)."""
    parsed = urlparse((url or "").strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {scheme!r}")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("URL missing hostname")

    allowed = {h.strip().lower().rstrip(".") for h in (allow_internal_hosts or set()) if h.strip()}
    if host in allowed:
        return

    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise ValueError(f"Blocked internal hostname: {host}")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if _is_blocked_ip(literal_ip):
            raise ValueError(f"Blocked non-public IP address: {host}")
        return

    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        addr_infos = await asyncio.to_thread(socket.getaddrinfo, host, port)
    except OSError as exc:
        raise ValueError(f"Unable to resolve hostname {host!r}: {exc}") from exc

    if not addr_infos:
        raise ValueError(f"DNS returned no addresses for {host!r}")

    resolved_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in addr_infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        try:
            resolved_ips.append(ipaddress.ip_address(raw_ip))
        except ValueError as exc:
            raise ValueError(f"Invalid resolved IP {raw_ip!r} for {host!r}") from exc

    has_public_ip = any(not _is_blocked_ip(ip) for ip in resolved_ips)
    for resolved_ip in resolved_ips:
        if _is_blocked_ip(resolved_ip):
            # Upstream ISP DNS in mainland China frequently injects forged 2001::/96 Teredo AAAA
            # records alongside valid public A records on domains like cw.com.tw / v2ex.com.
            if (
                has_public_ip
                and isinstance(resolved_ip, ipaddress.IPv6Address)
                and resolved_ip.ipv4_mapped is None
                and resolved_ip in _GFW_FORGED_TEREDO_AAAA_NET
            ):
                continue
            raise ValueError(f"Blocked hostname {host!r} resolving to non-public IP {resolved_ip}")


async def is_safe_public_url(
    url: str,
    *,
    allow_internal_hosts: set[str] | None = None,
) -> bool:
    """Boolean convenience wrapper around assert_safe_public_url."""
    try:
        await assert_safe_public_url(url, allow_internal_hosts=allow_internal_hosts)
        return True
    except ValueError:
        return False


def should_use_proxy(url: str, mode: str, proxy_url: str) -> bool:
    if not proxy_url:
        return False
    if mode == "proxy":
        return True
    if mode == "direct":
        return False
    host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
    if not host or host in ("localhost", "127.0.0.1", "::1", "rsshub", "newsnow", "byparr"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    ):
        return False
    if any(_host_matches_domain(host, d) for d in OVERSEAS_HINTS):
        return True
    if any(_host_matches_domain(host, d) for d in DOMESTIC_HINTS):
        return False
    return False


def _normalize_charset(charset: str) -> str:
    cleaned = charset.strip().strip("\"'").lower()
    if cleaned in ("gb2312", "gbk", "gb_2312", "gb_2312-80", "x-gbk", "cp936", "ms936"):
        return "gb18030"
    if cleaned in ("utf8", "utf-8"):
        return "utf-8"
    if cleaned in ("big5", "big-5", "csbig5"):
        return "big5"
    return cleaned


def decode_response_bytes(raw: bytes, content_type: str = "") -> str:
    """Decode HTML/XML response bytes cleanly without falling into httpx's latin-1 trap.

    Order: HTTP charset -> HTML <meta charset> / XML encoding -> strict utf-8 ->
    strict gb18030 -> strict big5 -> utf-8 replace.
    """
    if not raw:
        return ""

    candidates: list[str] = []

    http_match = _HTTP_CHARSET_RE.search(content_type or "")
    if http_match:
        candidates.append(_normalize_charset(http_match.group(1)))

    head_ascii = raw[:4096].decode("ascii", errors="ignore")
    meta_match = _META_CHARSET_RE.search(head_ascii) or _XML_ENCODING_RE.search(head_ascii)
    if meta_match:
        candidates.append(_normalize_charset(meta_match.group(1)))

    utf8_codec = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    candidates.extend([utf8_codec, "gb18030", "big5"])

    seen: set[str] = set()
    for enc in candidates:
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            return raw.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("utf-8", errors="replace")


async def _read_response_limited(resp: httpx.Response, max_bytes: int, url: str) -> bytes:
    raw_len = resp.headers.get("content-length")
    if raw_len:
        try:
            declared = int(raw_len.strip())
            if declared > max_bytes:
                raise ValueError(
                    f"Response from {url} exceeds {max_bytes} bytes limit (Content-Length: {declared})"
                )
        except ValueError as exc:
            if "exceeds" in str(exc):
                raise

    buf = bytearray()
    async for chunk in resp.aiter_bytes(chunk_size=65536):
        if not chunk:
            continue
        if len(buf) + len(chunk) > max_bytes:
            raise ValueError(f"Response from {url} exceeded {max_bytes} bytes limit")
        buf.extend(chunk)
    return bytes(buf)


_BYPARR_SEMAPHORES: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    WeakKeyDictionary()
)


def _get_byparr_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _BYPARR_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(1)
        _BYPARR_SEMAPHORES[loop] = sem
    return sem


async def fetch_via_byparr(
    url: str,
    byparr_base_url: str,
    *,
    timeout: float = 45.0,
    allow_internal_hosts: set[str] | None = None,
) -> tuple[str, str]:
    """Use FlareSolverr-compatible Byparr headless browser to bypass Cloudflare/JS walls."""
    await assert_safe_public_url(url, allow_internal_hosts=allow_internal_hosts)
    endpoint = f"{byparr_base_url.rstrip('/')}/v1"
    payload = {"cmd": "request.get", "url": url, "maxTimeout": int(timeout * 1000)}
    async with (
        _get_byparr_semaphore(),
        httpx.AsyncClient(timeout=timeout + 10.0, trust_env=False) as client,
    ):
        resp = await client.post(endpoint, json=payload)
        resp.raise_for_status()
        data = resp.json()
    solution = data.get("solution") or {}
    html = str(solution.get("response") or "")
    final_url = str(solution.get("url") or url)
    if not html:
        raise RuntimeError(f"Byparr returned empty response for {url}")
    if len(html.encode("utf-8", errors="ignore")) > MAX_TEXT_BYTES:
        raise ValueError(f"Byparr response for {url} exceeded {MAX_TEXT_BYTES} bytes limit")
    logger.info("byparr_fetch_ok", url=url, bytes=len(html))
    return html, final_url


def _looks_like_cloudflare_challenge(html: str, status_code: int) -> bool:
    if status_code in (403, 429, 503):
        lower = html[:3000].lower()
        return any(
            marker in lower
            for marker in (
                "attention required! | cloudflare",
                "just a moment...",
                "cf-browser-verification",
                "enable javascript and cookies to continue",
                "datadome",
            )
        )
    return False


def _should_fallback_to_byparr(exc: Exception | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (403, 429, 503)
    return isinstance(exc, httpx.TimeoutException)


async def smart_fetch_text(
    url: str,
    *,
    use_proxy: str = "auto",
    outbound_proxy: str = "",
    byparr_base_url: str = "",
    force_byparr: bool = False,
    timeout: float = 20.0,
    verify_ssl: bool = True,
    max_bytes: int = MAX_TEXT_BYTES,
    allow_internal_hosts: set[str] | None = None,
) -> tuple[str, str]:
    """Fetch HTML/XML with SSRF validation, charset detection, size cap, proxy routing, and selective Byparr fallback."""
    await assert_safe_public_url(url, allow_internal_hosts=allow_internal_hosts)

    if force_byparr and byparr_base_url:
        return await fetch_via_byparr(
            url, byparr_base_url, allow_internal_hosts=allow_internal_hosts
        )

    proxy = outbound_proxy if should_use_proxy(url, use_proxy, outbound_proxy) else None
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    last_exc: Exception | None = None
    attempts: list[str | None] = [proxy]
    if outbound_proxy and proxy is None and use_proxy == "auto":
        attempts.append(outbound_proxy)

    for attempt_proxy in attempts:
        try:
            async with httpx.AsyncClient(
                proxy=attempt_proxy,
                timeout=timeout,
                follow_redirects=False,
                verify=verify_ssl,
                trust_env=False,
            ) as client:
                current_url = url
                for hop in range(MAX_REDIRECTS + 1):
                    if hop > 0:
                        await assert_safe_public_url(
                            current_url, allow_internal_hosts=allow_internal_hosts
                        )
                    async with client.stream("GET", current_url, headers=headers) as resp:
                        if resp.is_redirect and resp.headers.get("location"):
                            if hop >= MAX_REDIRECTS:
                                raise httpx.TooManyRedirects(
                                    f"Exceeded {MAX_REDIRECTS} redirects for {url}",
                                    request=resp.request,
                                )
                            current_url = urljoin(str(resp.url), resp.headers["location"])
                            continue

                        if resp.status_code in (403, 429, 503):
                            peek_bytes = await _read_response_limited(
                                resp, min(max_bytes, CHALLENGE_PEEK_BYTES), str(resp.url)
                            )
                            peek_text = decode_response_bytes(
                                peek_bytes, resp.headers.get("content-type", "")
                            )
                            if _looks_like_cloudflare_challenge(peek_text, resp.status_code):
                                if byparr_base_url:
                                    return await fetch_via_byparr(
                                        str(resp.url),
                                        byparr_base_url,
                                        allow_internal_hosts=allow_internal_hosts,
                                    )
                                resp.raise_for_status()
                            resp.raise_for_status()

                        resp.raise_for_status()
                        raw = await _read_response_limited(resp, max_bytes, str(resp.url))
                        text = decode_response_bytes(raw, resp.headers.get("content-type", ""))
                        return text, str(resp.url)
        except ValueError:
            # SSRF violation or response size limit exceeded — never retry or bypass via Byparr
            raise
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            logger.debug("fetch_attempt_failed", url=url, proxy=bool(attempt_proxy), err=str(exc))
            if exc.response.status_code in (404, 410):
                break
        except Exception as exc:
            last_exc = exc
            logger.debug("fetch_attempt_failed", url=url, proxy=bool(attempt_proxy), err=str(exc))

    if byparr_base_url and _should_fallback_to_byparr(last_exc):
        try:
            return await fetch_via_byparr(
                url, byparr_base_url, allow_internal_hosts=allow_internal_hosts
            )
        except Exception as byparr_exc:
            logger.warning("byparr_fallback_failed", url=url, err=str(byparr_exc))

    raise RuntimeError(f"Failed to fetch {url}: {last_exc}") from last_exc


async def smart_fetch_bytes(
    url: str,
    *,
    use_proxy: str = "auto",
    outbound_proxy: str = "",
    referer: str = "",
    timeout: float = 15.0,
    max_bytes: int = MAX_BINARY_BYTES,
    allow_internal_hosts: set[str] | None = None,
) -> tuple[bytes, str]:
    """Fetch binary image/media bytes with SSRF guard, size cap, proxy routing, and referer header."""
    await assert_safe_public_url(url, allow_internal_hosts=allow_internal_hosts)

    proxy = outbound_proxy if should_use_proxy(url, use_proxy, outbound_proxy) else None
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if referer:
        headers["Referer"] = referer

    host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
    is_domestic = any(_host_matches_domain(host, d) for d in DOMESTIC_HINTS)
    attempts: list[str | None] = [proxy]
    if outbound_proxy and proxy is None and use_proxy == "auto" and not is_domestic:
        attempts.append(outbound_proxy)

    last_exc: Exception | None = None
    for attempt_proxy in attempts:
        try:
            async with httpx.AsyncClient(
                proxy=attempt_proxy,
                timeout=timeout,
                follow_redirects=False,
                verify=False,
                trust_env=False,
            ) as client:
                current_url = url
                for hop in range(MAX_REDIRECTS + 1):
                    if hop > 0:
                        await assert_safe_public_url(
                            current_url, allow_internal_hosts=allow_internal_hosts
                        )
                    async with client.stream("GET", current_url, headers=headers) as resp:
                        if resp.is_redirect and resp.headers.get("location"):
                            if hop >= MAX_REDIRECTS:
                                raise httpx.TooManyRedirects(
                                    f"Exceeded {MAX_REDIRECTS} redirects for {url}",
                                    request=resp.request,
                                )
                            current_url = urljoin(str(resp.url), resp.headers["location"])
                            continue
                        resp.raise_for_status()
                        content_type = resp.headers.get("content-type", "application/octet-stream")
                        raw = await _read_response_limited(resp, max_bytes, str(resp.url))
                        return raw, content_type
                raise RuntimeError(f"Failed to fetch binary {url}: too many redirects")
        except ValueError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403, 404, 410, 418):
                raise
            last_exc = exc
        except Exception as exc:
            last_exc = exc

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Failed to fetch binary {url}")
