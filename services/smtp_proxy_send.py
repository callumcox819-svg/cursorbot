"""SMTP через SOCKS5: один прокси навсегда привязан к ящику (без ротации)."""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount, Proxy
from proxy_manager import ProxySMTPContext
from services.proxy_binding import (
    NO_ACTIVE_PROXY,
    eject_proxy_after_mailing_failure,
    is_mailing_proxy_failure,
    resolve_proxy_for_account,
)
from services.sender import (
    is_definite_proxy_failure,
    is_smtp_timeout_error,
    normalize_send_error,
    send_batch_via_account,
    send_email_via_account,
)
from services.proxy_manager import ProxyManager

logger = logging.getLogger(__name__)

REPLY_SMTP_TIMEOUT_SEC = max(15, min(60, int(os.getenv("REPLY_SMTP_TIMEOUT_SEC", "28"))))
MAIL_SMTP_TIMEOUT_SEC = max(20, min(60, int(os.getenv("MAIL_SMTP_TIMEOUT_SEC", "35"))))
MAIL_MAILING_TIMEOUT_SEC = max(15, min(45, int(os.getenv("MAIL_MAILING_TIMEOUT_SEC", "22"))))

# Совместимость (preflight / старые логи)
MAIL_SMTP_MAX_PROXIES = 1
MAIL_MAILING_MAX_PROXIES = 1
REPLY_SMTP_MAX_PROXIES = 1


async def choose_required_proxy(
    session: AsyncSession,
    user_id: int,
    *,
    account: EmailAccount | None = None,
    exclude_ids: set[int] | None = None,
) -> Tuple[Optional[Proxy], Optional[str]]:
    if account is not None:
        return await resolve_proxy_for_account(session, account)
    from services.proxy_binding import pick_least_loaded_proxy

    proxy = await pick_least_loaded_proxy(session, int(user_id), exclude_ids=exclude_ids)
    if proxy:
        return proxy, None
    if exclude_ids:
        return None, None
    return None, NO_ACTIVE_PROXY


async def _list_active_socks5_proxies(session: AsyncSession, user_id: int) -> List[Proxy]:
    from services.proxy_binding import list_sendable_proxies

    return await list_sendable_proxies(session, user_id)


async def send_email_via_account_with_proxy(
    session: AsyncSession,
    user_id: int,
    account: EmailAccount,
    to_email: str,
    subject: str,
    body: str,
    sender_name: Optional[str] = None,
    is_html: Optional[bool] = None,
    *,
    fast: bool = False,
    mailing: bool = False,
) -> Tuple[bool, Optional[str], Optional[str]]:
    proxy, pick_err = await resolve_proxy_for_account(session, account)
    if pick_err:
        return False, pick_err, None
    if not proxy:
        return False, NO_ACTIVE_PROXY, None

    smtp_tmo = (
        MAIL_MAILING_TIMEOUT_SEC
        if mailing
        else (REPLY_SMTP_TIMEOUT_SEC if fast else MAIL_SMTP_TIMEOUT_SEC)
    )
    pid = int(proxy.id)

    logger.info(
        "[SMTP send] bound proxy_id=%s %s:%s account=%s -> %s",
        pid,
        proxy.host,
        proxy.port,
        account.email,
        to_email,
    )

    async with ProxySMTPContext(proxy):
        ok, err, msgid = await send_email_via_account(
            account,
            to_email,
            subject,
            body,
            sender_name=sender_name,
            is_html=is_html,
            smtp_timeout_sec=smtp_tmo,
        )
    err = normalize_send_error(err)

    if ok:
        try:
            await ProxyManager.note_proxy_success(session, pid)
        except Exception:
            pass
        return True, err, msgid

    logger.warning(
        "[SMTP send] fail bound proxy_id=%s account=%s err=%s",
        pid,
        account.email,
        (err or "")[:200],
    )
    if mailing and is_mailing_proxy_failure(err):
        await eject_proxy_after_mailing_failure(
            session, account=account, proxy=proxy, err=err
        )
    else:
        dead = is_definite_proxy_failure(err)
        try:
            await ProxyManager.note_proxy_failure(
                session,
                pid,
                (err or "")[:500],
                deactivate=dead,
                from_mailing=mailing,
            )
        except Exception:
            pass

    if is_smtp_timeout_error(err):
        hint = (
            f"Прокси {proxy.host}:{proxy.port} снят с рассылки "
            f"({err or 'timeout'}). Ящик переключён на другой SOCKS5, если есть."
        )
        return False, f"SMTP_TIMEOUT|bound_proxy|{hint}", msgid
    return False, err or NO_ACTIVE_PROXY, msgid


async def send_batch_via_account_with_proxy(
    session: AsyncSession,
    user_id: int,
    account: EmailAccount,
    items: list[tuple[str, str, str]],
    sender_name: Optional[str] = None,
    *,
    mailing: bool = True,
) -> List[Tuple[bool, Optional[str]]]:
    n = len(items)
    if n == 0:
        return []

    proxy, pick_err = await resolve_proxy_for_account(session, account)
    if pick_err or not proxy:
        err = pick_err or NO_ACTIVE_PROXY
        return [(False, err) for _ in items]

    smtp_tmo = MAIL_MAILING_TIMEOUT_SEC if mailing else MAIL_SMTP_TIMEOUT_SEC
    pid = int(proxy.id)

    logger.info(
        "[SMTP batch] bound proxy_id=%s account=%s count=%s",
        pid,
        account.email,
        n,
    )

    async with ProxySMTPContext(proxy):
        raw = await send_batch_via_account(
            account,
            items,
            sender_name=sender_name,
            smtp_timeout_sec=smtp_tmo,
        )

    merged: List[Tuple[bool, Optional[str]]] = []
    any_ok = False
    last_err: str | None = None
    for j in range(n):
        ok, err = raw[j] if j < len(raw) else (False, "BATCH_INDEX_ERROR")
        err_n = normalize_send_error(err)
        merged.append((bool(ok), err_n))
        if ok:
            any_ok = True
        else:
            last_err = err_n

    if any_ok:
        try:
            await ProxyManager.note_proxy_success(session, pid)
        except Exception:
            pass
        return merged

    if mailing and is_mailing_proxy_failure(last_err):
        await eject_proxy_after_mailing_failure(
            session, account=account, proxy=proxy, err=last_err
        )
    else:
        dead = is_definite_proxy_failure(last_err)
        try:
            await ProxyManager.note_proxy_failure(
                session,
                pid,
                (last_err or "batch fail")[:500],
                deactivate=dead,
                from_mailing=True,
            )
        except Exception:
            pass
    return merged
