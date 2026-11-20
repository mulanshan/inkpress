from __future__ import annotations

from urllib.parse import urlparse

import httpx

from inkpress.logging import get_logger

logger = get_logger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
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
    "cw.com.tw",
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
    "hupu.com",
    "weibo.com",
    "sina.com",
    "qq.com",
    "163.com",
    "ifeng.com",
    "solidot.org",
    "freebuf.com",
)


def should_use_proxy(url: str, mode: str, proxy_url: str) -> bool:
    if not proxy_url:
        return False
    if mode == "proxy":
        return True
    if mode == "direct":
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host or host in ("localhost", "127.0.0.1", "rsshub", "newsnow", "byparr"):
        return False
    if host.startswith(("192.168.", "10.", "172.")):
        return False
    if any(host == d or host.endswith("." + d) or host.endswith(d) for d in OVERSEAS_HINTS):
        return True
    if any(host.endswith(d) for d in DOMESTIC_HINTS):
        return False
    return False


async def fetch_via_byparr(
    url: str,
    byparr_base_url: str,
    *,
    timeout: float = 45.0,
) -> tuple[str, str]:
    """Use FlareSolverr-compatible Byparr headless browser to bypass Cloudflare/JS walls."""
    endpoint = f"{byparr_base_url.rstrip('/')}/v1"
    payload = {"cmd": "request.get", "url": url, "maxTimeout": int(timeout * 1000)}
    async with httpx.AsyncClient(timeout=timeout + 10.0, trust_env=False) as client:
        resp = await client.post(endpoint, json=payload)
        resp.raise_for_status()
        data = resp.json()
    solution = data.get("solution") or {}
    html = str(solution.get("response") or "")
    final_url = str(solution.get("url") or url)
    if not html:
        raise RuntimeError(f"Byparr returned empty response for {url}")
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


async def smart_fetch_text(
    url: str,
    *,
    use_proxy: str = "auto",
    outbound_proxy: str = "",
    byparr_base_url: str = "",
    force_byparr: bool = False,
    timeout: float = 20.0,
    verify_ssl: bool = True,
) -> tuple[str, str]:
    """Fetch HTML/XML with smart proxy selection and automatic Byparr fallback."""
    if force_byparr and byparr_base_url:
        return await fetch_via_byparr(url, byparr_base_url)

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
                follow_redirects=True,
                verify=verify_ssl,
                trust_env=False,
            ) as client:
                resp = await client.get(url, headers=headers)
                text = resp.text
                if _looks_like_cloudflare_challenge(text, resp.status_code):
                    if byparr_base_url:
                        return await fetch_via_byparr(url, byparr_base_url)
                    resp.raise_for_status()
                resp.raise_for_status()
                return text, str(resp.url)
        except Exception as exc:
            last_exc = exc
            logger.debug("fetch_attempt_failed", url=url, proxy=bool(attempt_proxy), err=str(exc))

    if byparr_base_url:
        try:
            return await fetch_via_byparr(url, byparr_base_url)
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
) -> tuple[bytes, str]:
    """Fetch binary image/media bytes with proxy routing and referer anti-hotlink header."""
    proxy = outbound_proxy if should_use_proxy(url, use_proxy, outbound_proxy) else None
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if referer:
        headers["Referer"] = referer
    async with httpx.AsyncClient(
        proxy=proxy,
        timeout=timeout,
        follow_redirects=True,
        verify=False,
        trust_env=False,
    ) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return resp.content, content_type
