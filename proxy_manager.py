from __future__ import annotations

import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy import select, or_

from models import Proxy, UserSetting

logger = logging.getLogger(__name__)


class _ReentrantAsyncLock:
    """Один event loop: рассылка держит lock в Session и снова в ProxySMTPContext."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        if self._depth > 1:
            self._depth -= 1
            return
        self._owner = None
        self._depth = 0
        self._lock.release()

    async def __aenter__(self) -> "_ReentrantAsyncLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


_PROXY_LOCK = _ReentrantAsyncLock()
_DB_SOCKET_LOCK = _ReentrantAsyncLock()
# round-robin по активным прокси (на user_id из БД)
_RR_INDEX: dict[int, int] = {}

import socket as _stdlib_socket
import smtplib as _smtplib

_SMTP_SOCKET_ORIG = _smtplib.socket
_SOCKET_GETADDRINFO_ORIG = None
_SMTP_TEST_HOST = (os.getenv("SMTP_TEST_HOST") or "smtp.gmail.com").strip()
_SMTP_TEST_PORT = int(os.getenv("SMTP_TEST_PORT") or "587")

SOCKS5_TYPES = frozenset({"socks5", "socks5h"})


def is_socks5_proxy(proxy: Proxy) -> bool:
    t = (getattr(proxy, "proxy_type", None) or proxy.type or "socks5").lower().strip()
    return t in SOCKS5_TYPES or t.startswith("socks5")


async def choose_proxy_for_user(
    session,
    user_id: int,
    *,
    exclude_ids: set[int] | None = None,
) -> Optional[Proxy]:
    """
    Возвращает один активный SOCKS5 прокси пользователя.
    """
    try:
        rot = (
            await session.execute(
                select(UserSetting.value)
                .where(UserSetting.user_id == int(user_id))
                .where(UserSetting.key == "proxy_rotation")
                .limit(1)
            )
        ).scalar_one_or_none()
        rot_on = str(rot or "0").strip().lower() in {"1", "true", "yes", "on", "y"}

        active_cond = or_(Proxy.is_active.is_(True), Proxy.is_active.is_(None))

        def _smtp_eligible(p: Proxy) -> bool:
            if not is_socks5_proxy(p):
                t = (getattr(p, "type", None) or "").strip().lower()
                # В БД default=http, хотя прокси SOCKS5 — не отбрасываем пустой/мусорный type
                if t in ("http", "https"):
                    return False
                if t and not t.startswith("socks"):
                    return False
            return True

        all_rows = list(
            (
                await session.execute(
                    select(Proxy)
                    .where(Proxy.user_id == int(user_id))
                    .order_by(Proxy.id.asc())
                )
            ).scalars().all()
        )
        skip = exclude_ids or set()
        eligible = [p for p in all_rows if _smtp_eligible(p) and int(p.id) not in skip]
        preferred = [p for p in eligible if p.is_active is not False]
        items = preferred if preferred else eligible
        if not items:
            logger.warning(
                "no SMTP proxy for user_id=%s total=%s eligible=%s",
                user_id,
                len(all_rows),
                sum(1 for p in all_rows if _smtp_eligible(p)),
            )
            return None
        if len(items) == 1:
            return items[0]

        uid = int(user_id)
        if rot_on:
            chosen = random.choice(items)
        else:
            # Раньше без ротации всегда брался только первый id — новые прокси не использовались.
            idx = _RR_INDEX.get(uid, 0) % len(items)
            _RR_INDEX[uid] = idx + 1
            chosen = items[idx]

        logger.info(
            "SMTP proxy selected user_id=%s proxy_id=%s %s:%s rot=%s",
            uid,
            chosen.id,
            chosen.host,
            chosen.port,
            "random" if rot_on else "rr",
        )
        return chosen
    except Exception:
        logger.exception("choose_proxy_for_user failed")
        return None


def apply_proxy_to_smtplib(proxy: Proxy) -> None:
    """Только SOCKS5 → PySocks → smtplib."""
    global _SOCKET_GETADDRINFO_ORIG

    import socks
    import smtplib

    if not is_socks5_proxy(proxy):
        raise ValueError(
            f"Поддерживается только SOCKS5, получен: {(proxy.type or '?')!r}"
        )

    host = (proxy.host or "").strip()
    port = int(proxy.port or 0)
    if not host or not port:
        raise ValueError("Proxy host/port is empty")

    username = (proxy.username or "").strip() or None
    password = (proxy.password or "").strip() or None

    socks.set_default_proxy(
        socks.SOCKS5,
        host,
        port,
        username=username,
        password=password,
        rdns=True,
    )

    if _SOCKET_GETADDRINFO_ORIG is None:
        _SOCKET_GETADDRINFO_ORIG = _stdlib_socket.getaddrinfo

    def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        return _SOCKET_GETADDRINFO_ORIG(
            host,
            port,
            _stdlib_socket.AF_INET,
            type or _stdlib_socket.SOCK_STREAM,
            proto,
            flags,
        )

    _stdlib_socket.getaddrinfo = _getaddrinfo_ipv4  # type: ignore[assignment]

    socks.wrapmodule(smtplib)
    if hasattr(smtplib.socket, "getaddrinfo"):
        smtplib.socket.getaddrinfo = _getaddrinfo_ipv4  # type: ignore[attr-defined]

    logger.info("SMTP SOCKS5 applied: %s:%s rdns=True", host, port)


async def test_smtp_tunnel_async(proxy: Proxy, *, timeout: int = 20) -> tuple[bool, str]:
    """SMTP-проверка под lock — без гонок при параллельных тестах."""
    async with _PROXY_LOCK:
        return await asyncio.to_thread(test_smtp_tunnel_sync, proxy, timeout=timeout)


def test_smtp_tunnel_sync(proxy: Proxy, *, timeout: int = 20) -> tuple[bool, str]:
    """Проверка как при рассылке: SOCKS5 → SMTP :587."""
    if not is_socks5_proxy(proxy):
        return False, "Только SOCKS5 прокси"

    apply_proxy_to_smtplib(proxy)
    try:
        s = _smtplib.SMTP(_SMTP_TEST_HOST, _SMTP_TEST_PORT, timeout=timeout)
        try:
            s.ehlo()
            s.starttls()
            s.ehlo()
        finally:
            try:
                s.close()
            except Exception:
                pass
        return True, f"SMTP+STARTTLS OK ({_SMTP_TEST_HOST}:{_SMTP_TEST_PORT})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        reset_smtplib_proxy()


async def _reset_socks_under_lock() -> None:
    """Короткий lock только на сброс PySocks (миллисекунды), не на весь запрос к Postgres."""
    import asyncio

    lock_wait = float(os.getenv("DB_SOCKET_LOCK_TIMEOUT_SEC", "10"))
    try:
        await asyncio.wait_for(_DB_SOCKET_LOCK.acquire(), timeout=lock_wait)
    except asyncio.TimeoutError:
        import logging

        logging.getLogger(__name__).error(
            "DB socket lock timeout (%.0fs) — reset без lock", lock_wait
        )
        reset_smtplib_proxy()
        return

    try:
        reset_smtplib_proxy()
    finally:
        _DB_SOCKET_LOCK.release()


@asynccontextmanager
async def database_socket_guard():
    """
    Перед/после работы с Postgres: сбросить PySocks-патч.
    Lock НЕ держится на время yield — иначе IMAP блокирует /start и все кнопки.
    """
    await _reset_socks_under_lock()
    try:
        yield
    finally:
        await _reset_socks_under_lock()


def reset_smtplib_proxy() -> None:
    global _SOCKET_GETADDRINFO_ORIG

    import smtplib

    try:
        import socks  # type: ignore
        socks.set_default_proxy()
    except Exception:
        pass

    if _SOCKET_GETADDRINFO_ORIG is not None:
        _stdlib_socket.getaddrinfo = _SOCKET_GETADDRINFO_ORIG  # type: ignore[assignment]

    smtplib.socket = _SMTP_SOCKET_ORIG
    logger.info("SMTP proxy reset (smtplib only)")


class ProxySMTPContext:
    """async with ProxySMTPContext(proxy): ... SMTP send ..."""

    def __init__(self, proxy: Proxy):
        self.proxy = proxy
        self._guard_token = None

    async def __aenter__(self):
        from services.smtp_proxy_guard import smtp_proxy_guard_enter

        await _PROXY_LOCK.acquire()
        try:
            self._guard_token = smtp_proxy_guard_enter()
            apply_proxy_to_smtplib(self.proxy)
        except Exception:
            _PROXY_LOCK.release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        from services.smtp_proxy_guard import smtp_proxy_guard_exit

        try:
            try:
                reset_smtplib_proxy()
            except Exception:
                pass
        finally:
            if self._guard_token is not None:
                try:
                    smtp_proxy_guard_exit(self._guard_token)
                except Exception:
                    pass
                self._guard_token = None
            try:
                _PROXY_LOCK.release()
            except Exception:
                pass
        return False
