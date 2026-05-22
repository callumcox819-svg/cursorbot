"""SOCKS5 для SMTP: общий пул на пользователя, без привязки к ящику."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount, Proxy
from proxy_manager import is_socks5_proxy

logger = logging.getLogger(__name__)

NO_ACTIVE_PROXY = "PROXY_ERROR|no_active_proxy|No active proxy configured"
PROXY_WAIT_REPLACEMENT = (
    "PROXY_ERROR|dead_proxy|Нет живых SOCKS5 — добавьте прокси и снова /send"
)
PROXY_NOT_ASSIGNED = PROXY_WAIT_REPLACEMENT

_rr_lock = asyncio.Lock()
_rr_index: dict[int, int] = {}
# Временно исключены из пула только на текущую рассылку (не трогаем is_active в БД)
_mailing_excluded: dict[int, set[int]] = {}
_mailing_fail_streak: dict[tuple[int, int], int] = {}
MAILING_PROXY_MAX_TIMEOUT_STREAK = max(
    2, min(8, int(__import__("os").getenv("MAILING_PROXY_MAX_TIMEOUT_STREAK", "4")))
)


def _smtp_eligible_proxy_row(p: Proxy) -> bool:
    if not is_socks5_proxy(p):
        t = (getattr(p, "type", None) or "").strip().lower()
        if t in ("http", "https"):
            return False
        if t and not t.startswith("socks"):
            return False
    return True


async def list_sendable_proxies(session: AsyncSession, user_id: int) -> list[Proxy]:
    rows = (
        await session.execute(
            select(Proxy).where(Proxy.user_id == int(user_id)).order_by(Proxy.id.asc())
        )
    ).scalars().all()
    out: list[Proxy] = []
    for p in rows:
        if not _smtp_eligible_proxy_row(p):
            continue
        if p.is_active is False:
            continue
        out.append(p)
    out.sort(key=lambda x: (0 if x.is_active is True else 1, int(x.id)))
    return out


async def get_proxy_row(session: AsyncSession, proxy_id: int, user_id: int) -> Optional[Proxy]:
    p = (
        await session.execute(
            select(Proxy)
            .where(Proxy.id == int(proxy_id))
            .where(Proxy.user_id == int(user_id))
            .limit(1)
        )
    ).scalars().first()
    if not p or not _smtp_eligible_proxy_row(p):
        return None
    if p.is_active is False:
        return None
    return p


def reset_mailing_proxy_round_robin(user_id: int) -> None:
    uid = int(user_id)
    _rr_index[uid] = 0
    _mailing_excluded.pop(uid, None)
    keys = [k for k in _mailing_fail_streak if k[0] == uid]
    for k in keys:
        _mailing_fail_streak.pop(k, None)


def get_mailing_excluded_proxy_ids(user_id: int) -> set[int]:
    return set(_mailing_excluded.get(int(user_id), set()))


def exclude_proxy_for_mailing_session(user_id: int, proxy_id: int) -> None:
    _mailing_excluded.setdefault(int(user_id), set()).add(int(proxy_id))


async def revive_proxies_after_transient_mailing_errors(
    session: AsyncSession,
    user_id: int,
) -> int:
    """Снять ложный 🔴 после таймаутов рассылки — прокси снова в пуле."""
    from services.proxy_verify import MAILING_PROXY_DEAD_PREFIX

    res = await session.execute(
        update(Proxy)
        .where(Proxy.user_id == int(user_id))
        .where(Proxy.is_active.is_(False))
        .where(Proxy.last_error.ilike(f"{MAILING_PROXY_DEAD_PREFIX}%"))
        .values(is_active=None, last_error=None)
    )
    await session.commit()
    return int(res.rowcount or 0)


async def pick_mailing_proxy(
    session: AsyncSession,
    user_id: int,
    *,
    exclude_ids: set[int] | None = None,
) -> Optional[Proxy]:
    """Следующий живой SOCKS5 из пула (round-robin), без привязки к ящику."""
    uid = int(user_id)
    proxies = await list_sendable_proxies(session, uid)
    skip = get_mailing_excluded_proxy_ids(uid) | set(exclude_ids or ())
    available = [p for p in proxies if int(p.id) not in skip]
    if not available:
        return None

    uid = int(user_id)
    async with _rr_lock:
        idx = _rr_index.get(uid, 0) % len(available)
        _rr_index[uid] = idx + 1
    return available[idx]


async def clear_legacy_account_proxy_state(session: AsyncSession, user_id: int) -> int:
    """Сброс proxy_error / proxy_id; smtp_blocked и 🔴 по паролю не трогаем."""
    from services.account_status import heal_accounts_mislabeled_by_proxy

    n = await heal_accounts_mislabeled_by_proxy(session, user_id)
    if n:
        logger.info("healed proxy-mislabeled accounts=%s user_id=%s", n, user_id)
    return n


# Совместимость: больше не привязываем ящики к прокси
async def assign_proxy_to_account(
    session: AsyncSession,
    account: EmailAccount,
    *,
    force_new: bool = False,
) -> Optional[Proxy]:
    del session, account, force_new
    return None


async def ensure_all_accounts_assigned(session: AsyncSession, user_id: int) -> int:
    return await clear_legacy_account_proxy_state(session, user_id)


async def assign_waiting_accounts(session: AsyncSession, user_id: int) -> int:
    return await clear_legacy_account_proxy_state(session, user_id)


def is_mailing_proxy_failure(err: str | None) -> bool:
    """Срыв /send из‑за прокси/SMTP-туннеля — убираем прокси из пула рассылки."""
    from services.sender import (
        is_definite_proxy_failure,
        is_proxy_error_marker,
        is_smtp_timeout_error,
        is_transient_connection_error,
        normalize_send_error,
        should_retry_send_with_other_proxy,
    )

    if should_retry_send_with_other_proxy(err):
        return True
    if is_definite_proxy_failure(err) or is_smtp_timeout_error(err):
        return True
    if is_transient_connection_error(err):
        return True
    if is_proxy_error_marker(err):
        return True
    s = (normalize_send_error(err) or "").lower()
    if "bound_proxy" in s or "serverdisconnected" in s.replace(" ", ""):
        return True
    if "unexpectedly closed" in s or "connection closed" in s:
        return True
    return False


async def deactivate_proxy_from_mailing(
    session: AsyncSession,
    proxy: Proxy | None,
    err: str | None,
) -> bool:
    """
    Таймаут/обрыв → временно skip в этой рассылке.
    🔴 в БД только при явной смерти SOCKS5 или N таймаутов подряд.
    """
    if not proxy or not is_mailing_proxy_failure(err):
        return False

    from services.proxy_manager import ProxyManager
    from services.sender import is_definite_proxy_failure, is_smtp_timeout_error

    uid = int(proxy.user_id)
    pid = int(proxy.id)
    err_txt = (err or "mailing proxy failure")[:500]
    exclude_proxy_for_mailing_session(uid, pid)

    hard_dead = is_definite_proxy_failure(err)
    if not hard_dead and is_smtp_timeout_error(err):
        key = (uid, pid)
        streak = _mailing_fail_streak.get(key, 0) + 1
        _mailing_fail_streak[key] = streak
        hard_dead = streak >= MAILING_PROXY_MAX_TIMEOUT_STREAK
    elif not hard_dead:
        key = (uid, pid)
        _mailing_fail_streak[key] = _mailing_fail_streak.get(key, 0) + 1

    if hard_dead:
        await ProxyManager.note_proxy_failure(
            session, pid, err_txt, deactivate=True, from_mailing=True
        )
        logger.warning(
            "mailing proxy DEAD proxy_id=%s %s:%s err=%s",
            pid,
            proxy.host,
            proxy.port,
            err_txt[:120],
        )
    else:
        await ProxyManager.note_proxy_failure(
            session, pid, err_txt, deactivate=False, from_mailing=True
        )
        logger.info(
            "mailing proxy skip (session) proxy_id=%s %s:%s err=%s",
            pid,
            proxy.host,
            proxy.port,
            err_txt[:120],
        )
    return True


async def eject_proxy_after_mailing_failure(
    session: AsyncSession,
    *,
    account: EmailAccount,
    proxy: Proxy | None,
    err: str | None,
) -> Optional[Proxy]:
    """Совместимость: снять прокси с пула и вернуть следующий для retry."""
    del account
    if not await deactivate_proxy_from_mailing(session, proxy, err):
        return None
    exclude = {int(proxy.id)} if proxy else set()
    return await pick_mailing_proxy(session, int(proxy.user_id), exclude_ids=exclude)


async def detach_accounts_from_proxy(
    session: AsyncSession,
    proxy_id: int,
    *,
    reason: str = "",
) -> int:
    """Только сброс устаревшего proxy_id (статус ящика не меняем)."""
    del reason
    res = await session.execute(
        update(EmailAccount)
        .where(EmailAccount.proxy_id == int(proxy_id))
        .values(proxy_id=None)
    )
    await session.commit()
    return int(res.rowcount or 0)


async def resolve_proxy_for_account(
    session: AsyncSession,
    account: EmailAccount,
    *,
    exclude_ids: set[int] | None = None,
) -> tuple[Optional[Proxy], Optional[str]]:
    """Прокси из пула для SMTP (рассылка и ответы)."""
    uid = int(account.user_id)
    proxy = await pick_mailing_proxy(session, uid, exclude_ids=exclude_ids)
    if proxy:
        return proxy, None
    if exclude_ids:
        return None, NO_ACTIVE_PROXY
    if not await list_sendable_proxies(session, uid):
        return None, NO_ACTIVE_PROXY
    return None, PROXY_NOT_ASSIGNED


async def pick_least_loaded_proxy(
    session: AsyncSession,
    user_id: int,
    *,
    exclude_ids: set[int] | None = None,
) -> Optional[Proxy]:
    return await pick_mailing_proxy(session, user_id, exclude_ids=exclude_ids)
