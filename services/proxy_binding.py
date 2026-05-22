"""Постоянная привязка SOCKS5-прокси к почтовому ящику (без ротации)."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount, Proxy
from proxy_manager import is_socks5_proxy

logger = logging.getLogger(__name__)

NO_ACTIVE_PROXY = "PROXY_ERROR|no_active_proxy|No active proxy configured"
PROXY_WAIT_REPLACEMENT = (
    "PROXY_ERROR|dead_proxy|Прокси отключён — добавьте живой SOCKS5 и снова /send"
)
PROXY_NOT_ASSIGNED = (
    "PROXY_ERROR|no_proxy_assigned|Нет свободного SOCKS5 для привязки к ящику"
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


async def count_accounts_per_proxy(session: AsyncSession, user_id: int) -> dict[int, int]:
    rows = (
        await session.execute(
            select(EmailAccount.proxy_id, func.count(EmailAccount.id))
            .where(EmailAccount.user_id == int(user_id))
            .where(EmailAccount.proxy_id.isnot(None))
            .group_by(EmailAccount.proxy_id)
        )
    ).all()
    return {int(pid): int(cnt) for pid, cnt in rows if pid is not None}


async def pick_least_loaded_proxy(
    session: AsyncSession,
    user_id: int,
    *,
    exclude_ids: set[int] | None = None,
) -> Optional[Proxy]:
    """Прокси с минимумом привязанных ящиков — чтобы все 3–4 использовались."""
    proxies = await list_sendable_proxies(session, user_id)
    skip = exclude_ids or set()
    proxies = [p for p in proxies if int(p.id) not in skip]
    if not proxies:
        return None

    counts = await count_accounts_per_proxy(session, user_id)
    return min(proxies, key=lambda p: (counts.get(int(p.id), 0), int(p.id)))


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


async def assign_proxy_to_account(
    session: AsyncSession,
    account: EmailAccount,
    *,
    force_new: bool = False,
) -> Optional[Proxy]:
    """Привязать ящик к прокси с наименьшей нагрузкой."""
    uid = int(account.user_id)
    if not force_new and account.proxy_id:
        bound = await get_proxy_row(session, int(account.proxy_id), uid)
        if bound:
            return bound
        account.proxy_id = None

    proxy = await pick_least_loaded_proxy(session, uid)
    if not proxy:
        return None

    account.proxy_id = int(proxy.id)
    if (account.status or "").strip().lower() == "proxy_error":
        account.status = "active"
        account.last_error = None
    await session.commit()
    logger.info(
        "proxy bind account_id=%s email=%s -> proxy_id=%s %s:%s",
        account.id,
        account.email,
        proxy.id,
        proxy.host,
        proxy.port,
    )
    return proxy


async def ensure_all_accounts_assigned(session: AsyncSession, user_id: int) -> int:
    """Привязать только новые active-ящики без proxy_id (не трогаем proxy_error — ждут новый SOCKS5)."""
    rows = (
        await session.execute(
            select(EmailAccount)
            .where(EmailAccount.user_id == int(user_id))
            .where(EmailAccount.proxy_id.is_(None))
            .where(EmailAccount.status == "active")
            .order_by(EmailAccount.id.asc())
        )
    ).scalars().all()
    n = 0
    for acc in rows:
        if await assign_proxy_to_account(session, acc):
            n += 1
    return n


async def assign_waiting_accounts(session: AsyncSession, user_id: int) -> int:
    """После добавления нового прокси — ящики без привязки (в т.ч. proxy_error)."""
    rows = (
        await session.execute(
            select(EmailAccount)
            .where(EmailAccount.user_id == int(user_id))
            .where(EmailAccount.proxy_id.is_(None))
            .order_by(EmailAccount.id.asc())
        )
    ).scalars().all()
    n = 0
    for acc in rows:
        if await assign_proxy_to_account(session, acc):
            n += 1
    return n


def is_mailing_proxy_failure(err: str | None) -> bool:
    """Срыв /send из‑за прокси/SMTP-туннеля — снимаем прокси с очереди рассылки."""
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


async def eject_proxy_after_mailing_failure(
    session: AsyncSession,
    *,
    account: EmailAccount,
    proxy: Proxy | None,
    err: str | None,
) -> Optional[Proxy]:
    """
    Прокси 🔴 + отвязка ящиков; этот ящик сразу на другой SOCKS5 (если есть).
    """
    if not proxy or not is_mailing_proxy_failure(err):
        return None

    from services.proxy_manager import ProxyManager

    pid = int(proxy.id)
    err_txt = (err or "mailing proxy failure")[:500]
    await ProxyManager.note_proxy_failure(
        session, pid, err_txt, deactivate=True, from_mailing=True
    )
    new_proxy = await assign_proxy_to_account(session, account, force_new=True)
    logger.warning(
        "mailing eject proxy_id=%s %s:%s account=%s err=%s -> %s",
        pid,
        proxy.host,
        proxy.port,
        account.email,
        err_txt[:120],
        f"proxy_id={new_proxy.id}" if new_proxy else "NO_REPLACEMENT",
    )
    return new_proxy


async def detach_accounts_from_proxy(
    session: AsyncSession,
    proxy_id: int,
    *,
    reason: str = "",
) -> int:
    """Прокси умер — открепить ящики, не перекидывать на другой IP."""
    err = (reason or "Прокси недоступен")[:500]
    res = await session.execute(
        update(EmailAccount)
        .where(EmailAccount.proxy_id == int(proxy_id))
        .values(
            proxy_id=None,
            status="proxy_error",
            last_error=err,
        )
    )
    await session.commit()
    n = int(res.rowcount or 0)
    if n:
        logger.warning("proxy detach proxy_id=%s accounts=%s", proxy_id, n)
    return n


async def resolve_proxy_for_account(
    session: AsyncSession,
    account: EmailAccount,
) -> tuple[Optional[Proxy], Optional[str]]:
    """
    Прокси для SMTP/IMAP этого ящика.
    (proxy, None) | (None, error_code_message)
    """
    proxies = await list_sendable_proxies(session, int(account.user_id))
    if not proxies:
        return None, NO_ACTIVE_PROXY

    if account.proxy_id:
        bound = await get_proxy_row(session, int(account.proxy_id), int(account.user_id))
        if bound:
            return bound, None
        account.proxy_id = None
        await session.commit()

    proxy = await assign_proxy_to_account(session, account)
    if proxy:
        return proxy, None
    return None, PROXY_NOT_ASSIGNED
