"""Проверка SOCKS5 прокси: туннель + SMTP (как при рассылке)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Tuple
from urllib.parse import urlsplit

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


def _test_socks5_connect_sync(d: dict[str, Any], *, timeout: int = 12) -> Tuple[bool, str]:
    """Быстрая проверка SOCKS5 через PySocks (тот же стек, что и рассылка)."""
    import socks

    host = (d.get("host") or "").strip()
    port = int(d.get("port") or 0)
    if not host or not port:
        return False, "host/port пустые"

    username = (d.get("username") or "").strip() or None
    password = (d.get("password") or "").strip() or None

    targets = (("smtp.gmail.com", 587), ("httpbin.org", 80))
    last_err = ""

    for thost, tport in targets:
        s = socks.socksocket()
        try:
            s.set_proxy(
                socks.SOCKS5,
                host,
                port,
                username=username,
                password=password,
                rdns=True,
            )
            s.settimeout(float(timeout))
            s.connect((thost, int(tport)))
            return True, f"SOCKS5 OK -> {thost}:{tport}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        finally:
            try:
                s.close()
            except Exception:
                pass

    return False, last_err or "SOCKS5 connect failed"


async def _test_socks5_handshake(proxy: Proxy | dict[str, Any], *, timeout: int = 12) -> Tuple[bool, str]:
    d = proxy_to_dict(proxy)
    return await asyncio.to_thread(_test_socks5_connect_sync, d, timeout=timeout)


def _proxy_row_from_dict(d: dict[str, Any]) -> Proxy:
    return Proxy(
        host=str(d["host"]),
        port=int(d["port"]),
        username=d.get("username"),
        password=d.get("password"),
        type=normalize_proxy_type(d.get("type")),
    )


async def test_smtp_tunnel(proxy: Proxy | dict[str, Any], *, timeout: int = 20) -> Tuple[bool, str]:
    from proxy_manager import test_smtp_tunnel_async

    row = _proxy_row_from_dict(proxy_to_dict(proxy))
    return await test_smtp_tunnel_async(row, timeout=timeout)


async def test_proxy(proxy: Proxy | dict[str, Any], *, timeout: int = 20) -> Tuple[bool, str]:
    """
    Только SOCKS5. Для рассылки решающий тест — SMTP :587 (как /send).
    SOCKS5-handshake — дополнительная диагностика (PySocks, не aiohttp).
    """
    d = proxy_to_dict(proxy)
    ptype = normalize_proxy_type(d.get("type"))

    if not is_socks5_type(ptype):
        return False, "Только SOCKS5. HTTP/HTTPS не поддерживаются для рассылки."

    smtp_timeout = max(12, int(timeout))
    socks_timeout = max(8, min(smtp_timeout, 15))

    smtp_ok, smtp_info = await test_smtp_tunnel(proxy, timeout=smtp_timeout)
    socks_ok, socks_info = await _test_socks5_handshake(proxy, timeout=socks_timeout)

    # Рассылка идёт через PySocks→SMTP — если SMTP OK, прокси рабочий.
    if smtp_ok:
        if socks_ok:
            return True, f"{smtp_info} · {socks_info}"
        return True, smtp_info

    if socks_ok:
        return False, f"SOCKS подключается, SMTP нет: {smtp_info}"
    return False, f"SOCKS: {socks_info} · SMTP: {smtp_info}"


async def test_proxy_url(proxy_url: str, *, timeout: int = 20) -> Tuple[bool, str]:
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
    timeout: int = 20,
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

    per_proxy_timeout = max(12, int(timeout))

    async def _one(p: Proxy) -> None:
        async with sem:
            try:
                ok, info = await asyncio.wait_for(
                    test_proxy(p, timeout=per_proxy_timeout),
                    timeout=per_proxy_timeout * 2 + 10,
                )
            except asyncio.TimeoutError:
                ok, info = False, "Timeout: проверка прокси заняла слишком долго"
            except Exception as e:
                ok, info = False, f"{type(e).__name__}: {e}"
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
