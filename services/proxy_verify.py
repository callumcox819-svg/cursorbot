"""Проверка SOCKS5 прокси: туннель + SMTP (как при рассылке)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Tuple
from urllib.parse import urlsplit

import aiohttp
from aiohttp_socks import ProxyConnector  # type: ignore

from models import Proxy

logger = logging.getLogger(__name__)

_SOCKS5_SCHEMES = frozenset({"socks5", "socks5h"})


def normalize_proxy_type(t: str | None) -> str:
    t = (t or "socks5").strip().lower()
    if t in ("socks", "sock5", "socksv5"):
        return "socks5"
    if t in ("socks5h",):
        return "socks5h"
    if t in ("socks5",):
        return "socks5"
    if t in ("http", "https"):
        return "http"
    if t.startswith("socks"):
        return "socks5"
    return "socks5"


def proxy_to_dict(proxy: Proxy | dict[str, Any]) -> dict[str, Any]:
    if isinstance(proxy, dict):
        return proxy
    return {
        "host": proxy.host,
        "port": int(proxy.port),
        "username": proxy.username,
        "password": proxy.password,
        "type": proxy.type or "socks5",
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


def is_socks5_type(proxy_type: str) -> bool:
    return normalize_proxy_type(proxy_type) in _SOCKS5_SCHEMES


async def _test_socks5_handshake(proxy_url: str, *, timeout: int = 10) -> Tuple[bool, str]:
    """Быстрая проверка SOCKS5 (httpbin через туннель)."""
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    connector = ProxyConnector.from_url(proxy_url)
    try:
        async with aiohttp.ClientSession(timeout=timeout_cfg, connector=connector) as session:
            async with session.get("http://httpbin.org/ip") as resp:
                if resp.status < 400:
                    return True, f"SOCKS5 OK ({resp.status})"
                return False, f"SOCKS5 HTTP status {resp.status}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _proxy_row_from_dict(d: dict[str, Any]) -> Proxy:
    return Proxy(
        host=str(d["host"]),
        port=int(d["port"]),
        username=d.get("username"),
        password=d.get("password"),
        type=normalize_proxy_type(d.get("type")),
    )


async def test_smtp_tunnel(proxy: Proxy | dict[str, Any], *, timeout: int = 12) -> Tuple[bool, str]:
    from proxy_manager import test_smtp_tunnel_sync

    row = _proxy_row_from_dict(proxy_to_dict(proxy))
    return await asyncio.to_thread(test_smtp_tunnel_sync, row, timeout=timeout)


async def test_proxy(proxy: Proxy | dict[str, Any], *, timeout: int = 10) -> Tuple[bool, str]:
    """
    Только SOCKS5:
    1) SOCKS5 туннель (httpbin)
    2) SMTP :587 через PySocks (как /send)
    """
    d = proxy_to_dict(proxy)
    ptype = normalize_proxy_type(d.get("type"))

    if not is_socks5_type(ptype):
        return False, "Только SOCKS5. HTTP/HTTPS не поддерживаются для рассылки."

    proxy_url = build_proxy_url(d)
    socks_ok, socks_info = await _test_socks5_handshake(proxy_url, timeout=timeout)
    smtp_ok, smtp_info = await test_smtp_tunnel(proxy, timeout=max(timeout, 14))

    if socks_ok and smtp_ok:
        return True, f"{socks_info} · {smtp_info}"
    if socks_ok and not smtp_ok:
        return False, f"SOCKS5 OK, SMTP нет: {smtp_info}"
    if not socks_ok and smtp_ok:
        return False, f"SMTP OK, SOCKS5 нет: {socks_info}"
    return False, f"SOCKS5: {socks_info} · SMTP: {smtp_info}"


async def test_proxy_url(proxy_url: str, *, timeout: int = 10) -> Tuple[bool, str]:
    p = (proxy_url or "").strip()
    scheme = normalize_proxy_type(urlsplit(p).scheme or "socks5")
    if not is_socks5_type(scheme):
        return False, "Только socks5://"
    return await test_proxy(
        {
            "host": urlsplit(p).hostname or "",
            "port": urlsplit(p).port or 1080,
            "username": urlsplit(p).username,
            "password": urlsplit(p).password,
            "type": scheme,
        },
        timeout=timeout,
    )


async def refresh_proxies_status(
    session,
    user_id: int,
    *,
    concurrency: int = 10,
    timeout: int = 10,
) -> tuple[int, int, int]:
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
