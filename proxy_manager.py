from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from sqlalchemy import select

from models import Proxy, UserSetting

logger = logging.getLogger(__name__)

# 🔒 один глобальный lock на любые операции с SMTP proxy (smtplib wrap/reset)
_PROXY_LOCK = asyncio.Lock()

# Оригинальный socket-модуль внутри smtplib (чтобы вернуть назад)
import smtplib as _smtplib
_SMTP_SOCKET_ORIG = _smtplib.socket


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

        if not rot_on:
            q = (
                select(Proxy)
                .where(Proxy.user_id == int(user_id))
                .where(Proxy.is_active.is_(True))
                .order_by(Proxy.id.asc())
                .limit(1)
            )
            return (await session.execute(q)).scalars().first()

        # random
        q_all = (
            select(Proxy)
            .where(Proxy.user_id == int(user_id))
            .where(Proxy.is_active.is_(True))
        )
        items = (await session.execute(q_all)).scalars().all()
        if not items:
            return None
        return random.choice(items)
    except Exception:
        logger.exception("choose_proxy_for_user failed")
        return None


# ----------------------------
# Low-level: apply/reset proxy ONLY for smtplib
# ----------------------------
def apply_proxy_to_smtplib(proxy: Proxy) -> None:
    """
    ВАЖНО:
    НЕ трогаем глобальный socket.socket, иначе сломаешь Telegram (aiohttp).
    Мы меняем только smtplib.socket -> socks, через wrapmodule.
    """
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

    # ✅ Только smtplib начинает использовать socks.create_connection (без локального getaddrinfo)
    socks.wrapmodule(smtplib)

    logger.info("SMTP proxy applied (smtplib only): %s://%s:%s rdns=%s", proxy_type, host, port, rdns)


def reset_smtplib_proxy() -> None:
    """
    Возвращает стандартное поведение smtplib (убирает proxy wrapper).
    """
    import smtplib

    try:
        import socks  # type: ignore
        socks.set_default_proxy()
    except Exception:
        pass

    # ✅ возвращаем smtplib.socket как было (НЕ глобальный socket)
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
