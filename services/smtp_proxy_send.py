"""SMTP sending that always runs through the user's proxy."""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount, Proxy
from proxy_manager import ProxySMTPContext, choose_proxy_for_user
from services.sender import (
    is_definite_proxy_failure,
    normalize_send_error,
    send_batch_via_account,
    send_email_via_account,
    should_retry_send_with_other_proxy,
)
from services.proxy_manager import ProxyManager

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
    proxy = await choose_proxy_for_user(session, int(user_id), exclude_ids=exclude_ids)
    if proxy:
        return proxy, None
    if exclude_ids:
        return None, None
    return None, NO_ACTIVE_PROXY


async def send_email_via_account_with_proxy(
    session: AsyncSession,
    user_id: int,
    account: EmailAccount,
    to_email: str,
    subject: str,
    body: str,
    sender_name: Optional[str] = None,
    is_html: Optional[bool] = None,
) -> Tuple[bool, Optional[str]]:
    last_err: str | None = None
    tried_ids: set[int] = set()

    while True:
        proxy, pick_err = await choose_required_proxy(session, user_id, exclude_ids=tried_ids)
        if pick_err:
            return False, pick_err
        if not proxy:
            break

        pid = int(proxy.id)
        tried_ids.add(pid)

        async with ProxySMTPContext(proxy):
            ok, err = await send_email_via_account(
                account,
                to_email,
                subject,
                body,
                sender_name=sender_name,
                is_html=is_html,
            )
        err = normalize_send_error(err)
        if ok:
            return True, err

        last_err = err
        if not should_retry_send_with_other_proxy(err):
            return False, err

        deactivate = is_definite_proxy_failure(err)
        try:
            await ProxyManager.note_proxy_failure(
                session, pid, (err or "")[:500], deactivate=deactivate
            )
        except Exception:
            pass
        continue

    if last_err:
        return False, last_err
    return False, NO_ACTIVE_PROXY


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
