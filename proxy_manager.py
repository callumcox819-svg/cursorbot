from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from sqlalchemy import select, or_

from models import Proxy, UserSetting

logger = logging.getLogger(__name__)

# 🔒 один глобальный lock на любые операции с SMTP proxy (smtplib wrap/reset)
_PROXY_LOCK = asyncio.Lock()

# Оригинальный socket-модуль внутри smtplib (чтобы вернуть назад)
import socket as _stdlib_socket
import smtplib as _smtplib

_SMTP_SOCKET_ORIG = _smtplib.socket
_SMTP_CONNECT_ORIG = None
_SOCKET_GETADDRINFO_ORIG = None


# ----------------------------
# Choose proxy
# ----------------------------
async def choose_proxy_for_user(session, user_id: int) -> Optional[Proxy]:
    """
    Возвращает один активный прокси пользователя.

    Ротация управляется настройкой user_settings:
      key = "proxy_rotation"
        - "0" (по умолчанию) => первый активный прокси
        - "1" => случайный активный прокси на каждую отправку

    ВАЖНО: user_id тут = users.id (DB), НЕ telegram_id.
    """
    try:
        # rotation flag
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

        if not rot_on:
            q = (
                select(Proxy)
                .where(Proxy.user_id == int(user_id))
                .where(active_cond)
                .order_by(Proxy.id.asc())
                .limit(1)
            )
            return (await session.execute(q)).scalars().first()

        # random
        q_all = (
            select(Proxy)
            .where(Proxy.user_id == int(user_id))
            .where(active_cond)
        )
        items = (await session.execute(q_all)).scalars().all()
        if not items:
            return None
        return random.choice(items)
    except Exception:
        logger.exception("choose_proxy_for_user failed")
        return None


def _looks_like_ipv6(host: str) -> bool:
    h = (host or "").strip()
    if not h or h.startswith("."):
        return False
    if h.count(":") >= 2:
        return True
    if ":" in h and "." not in h:
        return True
    return False


def _ensure_smtp_host_ipv4(host: str) -> str:
    """PySocks не умеет IPv6 — для SMTP только IPv4."""
    h = (host or "").strip()
    if not h:
        return h
    if _looks_like_ipv6(h):
        raise OSError(f"PySocks doesn't support IPv6: ({h!r},)")
    parts = h.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return h
    infos = _stdlib_socket.getaddrinfo(h, None, _stdlib_socket.AF_INET, _stdlib_socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"No IPv4 address for SMTP host {h!r}")
    return str(infos[0][4][0])


# ----------------------------
# Low-level: apply/reset proxy ONLY for smtplib
# ----------------------------
def apply_proxy_to_smtplib(proxy: Proxy) -> None:
    """
    ВАЖНО:
    НЕ трогаем глобальный socket.socket, иначе сломаешь Telegram (aiohttp).
    Мы меняем только smtplib.socket -> socks, через wrapmodule.
    """
    global _SMTP_CONNECT_ORIG, _SOCKET_GETADDRINFO_ORIG

    import socks  # PySocks
    import smtplib

    host = (proxy.host or "").strip()
    port = int(proxy.port or 0)
    if not host or not port:
        raise ValueError("Proxy host/port is empty")

    proxy_type = (getattr(proxy, "proxy_type", None) or proxy.type or "socks5").lower().strip()
    username = (proxy.username or "").strip() or None
    password = (proxy.password or "").strip() or None

    if proxy_type in ("socks5", "socks5h"):
        ptype = socks.SOCKS5
        rdns = True  # ✅ важно: DNS через прокси (лечит gaierror при плохом DNS)
    elif proxy_type in ("socks4", "socks4a"):
        ptype = socks.SOCKS4
        rdns = True
    elif proxy_type in ("http", "https"):
        ptype = socks.HTTP
        # ✅ ВАЖНО: если rdns=False, PySocks делает локальный DNS-resolve.
        # На Railway/Ubuntu он часто возвращает IPv6 (например для smtp.gmail.com),
        # а PySocks не поддерживает IPv6 и падает с ошибкой:
        #   "PySocks doesn't support IPv6: ('2a00:...')"
        # Поэтому для SMTP включаем rdns=True даже для HTTP(S) прокси.
        rdns = True
        rdns = True
    else:
        ptype = socks.SOCKS5
        rdns = True

    socks.set_default_proxy(
        ptype,
        host,
        port,
        username=username,
        password=password,
        rdns=rdns,
    )

  # Только smtplib → PySocks; DNS только IPv4 (Railway иначе даёт AAAA → падение).
    if _SMTP_CONNECT_ORIG is None:
        _SMTP_CONNECT_ORIG = smtplib.SMTP.connect

    def _smtp_connect_ipv4(self, host="", port=0, source_address=None):
        target = (host or getattr(self, "host", "") or "").strip()
        if target:
            target = _ensure_smtp_host_ipv4(target)
        if source_address is not None:
            return _SMTP_CONNECT_ORIG(self, target, port, source_address)
        return _SMTP_CONNECT_ORIG(self, target, port)

    smtplib.SMTP.connect = _smtp_connect_ipv4  # type: ignore[method-assign]

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

    logger.info("SMTP proxy applied (smtplib only): %s://%s:%s rdns=%s", proxy_type, host, port, rdns)


def reset_smtplib_proxy() -> None:
    """
    Возвращает стандартное поведение smtplib (убирает proxy wrapper).
    """
    global _SMTP_CONNECT_ORIG, _SOCKET_GETADDRINFO_ORIG

    import smtplib

    try:
        import socks  # type: ignore
        socks.set_default_proxy()
    except Exception:
        pass

    if _SMTP_CONNECT_ORIG is not None:
        smtplib.SMTP.connect = _SMTP_CONNECT_ORIG  # type: ignore[method-assign]

    if _SOCKET_GETADDRINFO_ORIG is not None:
        _stdlib_socket.getaddrinfo = _SOCKET_GETADDRINFO_ORIG  # type: ignore[assignment]

    smtplib.socket = _SMTP_SOCKET_ORIG

    logger.info("SMTP proxy reset (smtplib only)")


# ----------------------------
# ✅ Async context: safe apply/reset with lock
# ----------------------------
class ProxySMTPContext:
    """
    async with ProxySMTPContext(proxy):
        ... SMTP send ...
    """
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
