import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

TEST_URL = "https://api.ipify.org?format=json"


async def _tcp_probe(host: str, port: int, timeout: int = 3) -> Tuple[bool, Optional[str]]:
    try:
        fut = asyncio.open_connection(host, int(port))
        r, w = await asyncio.wait_for(fut, timeout=timeout)
        try:
            w.close()
            if hasattr(w, "wait_closed"):
                await w.wait_closed()
        except Exception:
            pass
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


@dataclass
class ProxyCheckResult:
    proxy: str
    ok: bool
    kind: str
    error: Optional[str] = None
    ip: Optional[str] = None


def _detect_kind(proxy: str) -> str:
    p = (proxy or "").strip().lower()
    if p.startswith("http://") or p.startswith("https://"):
        return "http"
    if (
        p.startswith("socks5://")
        or p.startswith("socks5h://")
        or p.startswith("socks4://")
        or p.startswith("socks4a://")
    ):
        return "socks5"
    return "unknown"


async def _check_http_proxy(proxy_url: str, timeout: int = 10) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        from urllib.parse import urlsplit

        u = urlsplit(proxy_url)
        if not u.hostname or not u.port:
            return False, "Invalid proxy URL", None

        ok_tcp, err_tcp = await _tcp_probe(u.hostname, int(u.port), timeout=min(3, int(timeout)))
        if not ok_tcp:
            return False, f"TCP connect failed: {err_tcp}", None

        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.get(TEST_URL, proxy=proxy_url) as resp:
                if resp.status != 200:
                    return False, f"HTTP status {resp.status}", None
                data = await resp.json()
                return True, None, str(data.get("ip") or "")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None


async def _check_socks_proxy(proxy_url: str, timeout: int = 10) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        from urllib.parse import urlsplit

        u = urlsplit(proxy_url)
        if not u.hostname or not u.port:
            return False, "Invalid proxy URL", None

        ok_tcp, err_tcp = await _tcp_probe(u.hostname, int(u.port), timeout=min(3, int(timeout)))
        if not ok_tcp:
            return False, f"TCP connect failed: {err_tcp}", None

        from aiohttp_socks import ProxyConnector  # type: ignore
    except Exception:
        return False, "aiohttp_socks not installed (pip install aiohttp_socks)", None

    try:
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        connector = ProxyConnector.from_url(proxy_url)
        async with aiohttp.ClientSession(timeout=timeout_cfg, connector=connector) as session:
            async with session.get(TEST_URL) as resp:
                if resp.status != 200:
                    return False, f"HTTP status {resp.status}", None
                data = await resp.json()
                return True, None, str(data.get("ip") or "")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None


async def test_proxy(proxy: str, timeout: int = 10) -> ProxyCheckResult:
    proxy = (proxy or "").strip()
    kind = _detect_kind(proxy)

    if kind == "unknown":
        return ProxyCheckResult(
            proxy=proxy,
            ok=False,
            kind="unknown",
            error="Proxy must start with http:// or socks5://",
        )

    if kind == "http":
        ok, err, ip = await _check_http_proxy(proxy, timeout=timeout)
        return ProxyCheckResult(proxy=proxy, ok=ok, kind="http", error=err, ip=ip)

    ok, err, ip = await _check_socks_proxy(proxy, timeout=timeout)
    return ProxyCheckResult(proxy=proxy, ok=ok, kind="socks5", error=err, ip=ip)


async def autocheck_proxies(
    proxies: List[str],
    concurrency: int = 20,
    timeout: int = 10,
) -> List[ProxyCheckResult]:
    sem = asyncio.Semaphore(max(1, concurrency))
    results: List[ProxyCheckResult] = []

    async def one(p: str):
        async with sem:
            res = await test_proxy(p, timeout=timeout)
            results.append(res)

    tasks = [asyncio.create_task(one(p)) for p in proxies]
    await asyncio.gather(*tasks, return_exceptions=True)
    return results


# ============================================================
# 🔒 КРИТИЧНО: ALIAS ДЛЯ СТАРОГО КОДА (НИЧЕГО НЕ ЛОМАЕТ)
# ============================================================
# Старые хендлеры импортируют check_proxy
# Новый код использует test_proxy
# Alias сохраняет полную обратную совместимость
check_proxy = test_proxy
