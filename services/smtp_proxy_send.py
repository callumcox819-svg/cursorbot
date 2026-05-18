"""SMTP sending that always runs through the user's proxy."""
from __future__ import annotations

import logging
import random
from typing import List, Optional, Tuple

from sqlalchemy import or_ as sa_or
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount, Proxy
from proxy_manager import ProxySMTPContext, is_socks5_proxy
from services.sender import (
    is_definite_proxy_failure,
    is_proxy_error_marker,
    is_smtp_timeout_error,
    normalize_send_error,
    send_batch_via_account,
    send_email_via_account,
    should_retry_send_with_other_proxy,
)
from services.proxy_manager import ProxyManager

logger = logging.getLogger(__name__)

NO_ACTIVE_PROXY = "PROXY_ERROR|no_active_proxy|No active proxy configured"


async def choose_required_proxy(
    session: AsyncSession,
    user_id: int,
    *,
    exclude_ids: set[int] | None = None,
) -> Tuple[Optional[Proxy], Optional[str]]:
    """
    (proxy, None) — ок.
    (None, NO_ACTIVE_PROXY) — в БД нет ни одного активного SOCKS5.
    (None, None) — все доступные прокси уже пробовали в этом send (не «мёртвые»).
    """
    from proxy_manager import choose_proxy_for_user

    proxy = await choose_proxy_for_user(session, int(user_id), exclude_ids=exclude_ids)
    if proxy:
        return proxy, None
    if exclude_ids:
        return None, None
    return None, NO_ACTIVE_PROXY


async def _list_active_socks5_proxies(session: AsyncSession, user_id: int) -> List[Proxy]:
    active_cond = sa_or(Proxy.is_active.is_(True), Proxy.is_active.is_(None))
    rows = (
        await session.execute(
            sa_select(Proxy)
            .where(Proxy.user_id == int(user_id))
            .where(active_cond)
            .order_by(Proxy.id)
        )
    ).scalars().all()
    out: List[Proxy] = []
    for p in rows:
        if not is_socks5_proxy(p):
            t = (getattr(p, "type", None) or "").strip().lower()
            if t in ("http", "https"):
                continue
            if t and not t.startswith("socks"):
                continue
        out.append(p)
    return out


async def send_email_via_account_with_proxy(
    session: AsyncSession,
    user_id: int,
    account: EmailAccount,
    to_email: str,
    subject: str,
    body: str,
    sender_name: Optional[str] = None,
    is_html: Optional[bool] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    proxies = await _list_active_socks5_proxies(session, user_id)
    if not proxies:
        return False, NO_ACTIVE_PROXY, None

    order = list(proxies)
    random.shuffle(order)

    last_err: str | None = None
    last_msgid: str | None = None
    tried = 0

    for proxy in order:
        pid = int(proxy.id)
        tried += 1
        logger.info(
            "[SMTP send] try proxy_id=%s %s:%s account=%s -> %s (%s/%s)",
            pid,
            proxy.host,
            proxy.port,
            account.email,
            to_email,
            tried,
            len(order),
        )
        async with ProxySMTPContext(proxy):
            ok, err, msgid = await send_email_via_account(
                account,
                to_email,
                subject,
                body,
                sender_name=sender_name,
                is_html=is_html,
            )
        err = normalize_send_error(err)
        if ok:
            return True, err, msgid

        last_err = err
        last_msgid = msgid
        logger.warning(
            "[SMTP send] fail proxy_id=%s account=%s err=%s",
            pid,
            account.email,
            (err or "")[:200],
        )

        if not should_retry_send_with_other_proxy(err):
            return False, err, last_msgid

        try:
            await ProxyManager.note_proxy_failure(
                session,
                pid,
                (err or "")[:500],
                deactivate=is_definite_proxy_failure(err),
            )
        except Exception:
            pass

    hint = (
        f"Ни один из {tried} SOCKS5 не достучался до Gmail SMTP "
        f"(последняя: {last_err or 'timeout'}). "
        f"«Прокси» → проверить — нужно SMTP+STARTTLS OK."
    )
    if is_smtp_timeout_error(last_err):
        return False, f"SMTP_TIMEOUT|all_proxies|{hint}", last_msgid
    return False, last_err or NO_ACTIVE_PROXY, last_msgid


async def send_batch_via_account_with_proxy(
    session: AsyncSession,
    user_id: int,
    account: EmailAccount,
    items: list[tuple[str, str, str]],
    sender_name: Optional[str] = None,
) -> List[Tuple[bool, Optional[str]]]:
    proxy, err = await choose_required_proxy(session, user_id)
    if err:
        return [(False, err) for _ in items]
    async with ProxySMTPContext(proxy):
        results = await send_batch_via_account(account, items, sender_name=sender_name)
    out: list[tuple[bool, str | None]] = []
    proxy_failed = False
    for ok, err in results:
        err_n = normalize_send_error(err)
        if not ok and is_proxy_error_marker(err_n):
            proxy_failed = True
        out.append((bool(ok), err_n))
    if proxy_failed:
        try:
            await ProxyManager.note_proxy_failure(
                session,
                int(proxy.id),
                "PROXY_ERROR batch",
                deactivate=any(is_definite_proxy_failure(e) for _, e in out if e),
            )
        except Exception:
            pass
    return out
