"""Единая проверка прокси (добавление, меню, рассылка)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp

from models import Proxy

logger = logging.getLogger(__name__)

_HTTP_TEST_URLS = (
    "http://httpbin.org/ip",
    "http://example.com",
    "http://icanhazip.com",
)
_SOCKS_TEST_URLS = (
    "http://httpbin.org/ip",
    "http://example.com",
)


def normalize_proxy_type(t: str | None) -> str:
    t = (t or "http").strip().lower()
    if t in ("https",):
        return "http"
    if t in ("socks", "sock5", "socksv5"):
        return "socks5"
    if t in ("socks5h",):
        return "socks5h"
    if t in ("socks4", "socks4a"):
        return t
    if t in ("http", "socks5", "socks5h"):
        return t
    if t.startswith("socks"):
        return "socks5"
    return "http"


def proxy_to_dict(proxy: Proxy | dict[str, Any]) -> dict[str, Any]:
    if isinstance(proxy, dict):
        return proxy
    return {
        "host": proxy.host,
        "port": int(proxy.port),
        "username": proxy.username,
        "password": proxy.password,
        "type": proxy.type or "http",
    }


def build_proxy_url(proxy: Proxy | dict[str, Any]) -> str:
    d = proxy_to_dict(proxy)
    proxy_type = normalize_proxy_type(d.get("type"))
    host = d["host"]
    port = int(d["port"])
    user = (d.get("username") or "").strip()
    pwd = (d.get("password") or "").strip()
    if user and pwd:
        return f"{proxy_type}://{user}:{pwd}@{host}:{port}"
    return f"{proxy_type}://{host}:{port}"


async def _test_http_like_proxy(proxy_url: str, *, timeout: int = 10) -> Tuple[bool, str]:
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        last_error = "no response"
        for url in _HTTP_TEST_URLS:
            try:
                async with session.get(url, proxy=proxy_url) as resp:
                    if resp.status < 400:
                        return True, f"HTTP-proxy OK ({resp.status}) via {url}"
                    last_error = f"HTTP status {resp.status} ({url})"
            except Exception as e:
                last_error = f"{type(e).__name__}: {e} ({url})"
                logger.warning("HTTP proxy check failed %s via %s", proxy_url, url)
    return False, last_error


async def _test_socks_proxy(proxy_url: str, *, timeout: int = 10) -> Tuple[bool, str]:
    try:
        from aiohttp_socks import ProxyConnector  # type: ignore
    except ImportError:
        return False, "aiohttp_socks не установлен (pip install aiohttp_socks)"

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    connector = ProxyConnector.from_url(proxy_url)
    async with aiohttp.ClientSession(timeout=timeout_cfg, connector=connector) as session:
        last_error = "no response"
        for url in _SOCKS_TEST_URLS:
            try:
                async with session.get(url) as resp:
                    if resp.status < 400:
                        return True, f"SOCKS OK ({resp.status}) via {url}"
                    last_error = f"HTTP status {resp.status} ({url})"
            except Exception as e:
                last_error = f"{type(e).__name__}: {e} ({url})"
                logger.warning("SOCKS proxy check failed %s via %s", proxy_url, url)
    return False, last_error


def _proxy_row_from_dict(d: dict[str, Any]) -> Proxy:
    p = Proxy(
        host=str(d["host"]),
        port=int(d["port"]),
        username=d.get("username"),
        password=d.get("password"),
        type=d.get("type") or "http",
    )
    return p


async def test_smtp_tunnel(proxy: Proxy | dict[str, Any], *, timeout: int = 12) -> Tuple[bool, str]:
    from proxy_manager import test_smtp_tunnel_sync

    d = proxy_to_dict(proxy)
    row = _proxy_row_from_dict(d)
    return await asyncio.to_thread(test_smtp_tunnel_sync, row, timeout=timeout)


async def test_proxy(proxy: Proxy | dict[str, Any], *, timeout: int = 10) -> Tuple[bool, str]:
    """
    Для рассылки важны оба теста:
    - веб (httpbin) — как раньше;
    - SMTP :587 через PySocks — как при /send.
    """
    d = proxy_to_dict(proxy)
    proxy_type = normalize_proxy_type(d.get("type"))
    proxy_url = build_proxy_url(d)

    if proxy_type in ("http",):
        web_ok, web_info = await _test_http_like_proxy(proxy_url, timeout=timeout)
    elif proxy_type.startswith("socks"):
        web_ok, web_info = await _test_socks_proxy(proxy_url, timeout=timeout)
    else:
        return False, f"Неизвестный тип прокси: {proxy_type}"

    smtp_ok, smtp_info = await test_smtp_tunnel(proxy, timeout=max(timeout, 12))

    if web_ok and smtp_ok:
        return True, f"{web_info} · {smtp_info}"
    if web_ok and not smtp_ok:
        return False, f"Веб OK, но SMTP нет: {smtp_info}"
    if not web_ok and smtp_ok:
        return False, f"SMTP OK, но веб нет: {web_info}"
    return False, f"Веб: {web_info} · SMTP: {smtp_info}"


async def test_proxy_url(proxy_url: str, *, timeout: int = 10) -> Tuple[bool, str]:
    """Проверка по URL (для autocheck_proxies)."""
    p = (proxy_url or "").strip()
    if not p:
        return False, "empty proxy url"
    kind = normalize_proxy_type(urlsplit(p).scheme or "http")
    if kind in ("http",):
        return await _test_http_like_proxy(p, timeout=timeout)
    if kind.startswith("socks"):
        return await _test_socks_proxy(p, timeout=timeout)
    return False, f"Неизвестный тип прокси: {kind}"


async def refresh_proxies_status(
    session,
    user_id: int,
    *,
    concurrency: int = 10,
    timeout: int = 10,
) -> tuple[int, int, int]:
    """
    Проверить все прокси пользователя и обновить is_active / last_error.
    Returns: (ok_count, fail_count, total)
    """
    from sqlalchemy import select as sa_select

    proxies = list(
        (
            await session.execute(sa_select(Proxy).where(Proxy.user_id == int(user_id)))
        ).scalars()
    )
    if not proxies:
        return 0, 0, 0

    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[tuple[Proxy, bool, str]] = []

    async def _one(p: Proxy) -> None:
        async with sem:
            ok, info = await test_proxy(p, timeout=timeout)
        results.append((p, ok, info))

    await asyncio.gather(*[_one(p) for p in proxies])

    ok_n = 0
    fail_n = 0
    for p, ok, info in results:
        row = await session.get(Proxy, int(p.id))
        if not row:
            continue
        row.is_active = bool(ok)
        row.last_error = None if ok else info
        if ok:
            ok_n += 1
        else:
            fail_n += 1

    await session.commit()
    return ok_n, fail_n, len(proxies)
