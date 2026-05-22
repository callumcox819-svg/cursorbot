"""Проверка SOCKS5 прокси: туннель + SMTP (как при рассылке)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Tuple
from urllib.parse import urlsplit

from models import Proxy

logger = logging.getLogger(__name__)

MAILING_PROXY_DEAD_PREFIX = "[mailing]"

PROXY_CHECK_RETRIES = max(1, min(4, int(os.getenv("PROXY_CHECK_RETRIES", "2"))))
PROXY_CHECK_RETRY_PAUSE_SEC = max(
    0.5, min(5.0, float(os.getenv("PROXY_CHECK_RETRY_PAUSE_SEC", "2")))
)

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

    # Только SMTP :587 — как рассылка. httpbin часто блокируют, не используем для статуса.
    thost, tport = "smtp.gmail.com", 587
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
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            s.close()
        except Exception:
            pass


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


def classify_proxy_check_result(ok: bool, info: str) -> bool | None:
    """
    Результат ручной/фоновой проверки: True = OK, None = неясно/ошибка сети.
    Никогда False — «мёртвый» (🔴) только после реальной ошибки туннеля при рассылке.
    """
    if ok:
        return True
    return None


def is_mailing_marked_dead(last_error: str | None) -> bool:
    return (last_error or "").strip().startswith(MAILING_PROXY_DEAD_PREFIX)


def check_error_worth_retry(info: str) -> bool:
    t = (info or "").lower()
    return any(
        x in t
        for x in (
            "timeout",
            "timed out",
            "заняла слишком",
            "туннель до smtp",
            "ehlo",
            "connection reset",
            "unexpectedly closed",
            "temporarily",
        )
    )


def apply_proxy_check_to_row(row: Proxy, ok: bool, info: str) -> None:
    """Проверка не отключает прокси — только 🟢 или оставляем/🟡."""
    classified = classify_proxy_check_result(ok, info)
    if classified is True:
        row.is_active = True
        row.last_error = None
        return
    if row.is_active is not False:
        row.is_active = None
    row.last_error = (info or "")[:500] if not ok else None


def heal_proxy_rows_from_stale_check_markers(proxies: list[Proxy]) -> None:
    """Снять старые 🔴, выставленные проверкой до правила «только рассылка»."""
    for row in proxies:
        if row.is_active is False and not is_mailing_marked_dead(row.last_error):
            row.is_active = None


async def test_proxy_socks_only(
    proxy: Proxy | dict[str, Any], *, timeout: int = 15
) -> Tuple[bool, str]:
    """Достаточно для 🟢 в меню: SOCKS5 до smtp.gmail.com:587 (без EHLO всей рассылки)."""
    d = proxy_to_dict(proxy)
    if not is_socks5_type(normalize_proxy_type(d.get("type"))):
        return False, "Только SOCKS5."
    return await _test_socks5_handshake(proxy, timeout=max(10, int(timeout)))


async def _test_proxy_once(proxy: Proxy | dict[str, Any], *, timeout: int = 20) -> Tuple[bool, str]:
    """Одна попытка: SMTP+STARTTLS как при /send."""
    d = proxy_to_dict(proxy)
    ptype = normalize_proxy_type(d.get("type"))

    if not is_socks5_type(ptype):
        return False, "Только SOCKS5. HTTP/HTTPS не поддерживаются для рассылки."

    smtp_timeout = max(20, int(timeout))
    smtp_ok, smtp_info = await test_smtp_tunnel(proxy, timeout=smtp_timeout)
    if smtp_ok:
        return True, smtp_info

    socks_timeout = max(12, min(smtp_timeout, 20))
    socks_ok, socks_info = await _test_socks5_handshake(proxy, timeout=socks_timeout)
    if socks_ok:
        return False, f"Туннель до SMTP есть, но EHLO/STARTTLS не прошёл: {smtp_info}"
    return False, f"SMTP: {smtp_info} · туннель: {socks_info}"


async def test_proxy(
    proxy: Proxy | dict[str, Any], *, timeout: int = 20, retries: int | None = None
) -> Tuple[bool, str]:
    """Проверка с повторами — меньше ложных 🟡 из-за лага сети/Railway."""
    attempts = max(1, int(retries if retries is not None else PROXY_CHECK_RETRIES))
    last_info = ""
    for attempt in range(1, attempts + 1):
        ok, info = await _test_proxy_once(proxy, timeout=timeout)
        if ok:
            return True, info
        last_info = info or ""
        if attempt < attempts and check_error_worth_retry(last_info):
            logger.info(
                "proxy check retry %s/%s: %s",
                attempt,
                attempts,
                last_info[:120],
            )
            await asyncio.sleep(PROXY_CHECK_RETRY_PAUSE_SEC)
            continue
        break
    return False, last_info


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
    lite: bool = False,
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
                if lite:
                    ok, info = await asyncio.wait_for(
                        test_proxy_socks_only(p, timeout=per_proxy_timeout),
                        timeout=per_proxy_timeout + 8,
                    )
                else:
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
        apply_proxy_check_to_row(row, ok, info or "")
        if ok:
            ok_n += 1
        else:
            fail_n += 1

    all_rows = list(
        (await session.execute(sa_select(Proxy).where(Proxy.user_id == int(user_id)))).scalars()
    )
    heal_proxy_rows_from_stale_check_markers(all_rows)

    await session.commit()
    return ok_n, fail_n, len(proxies)
