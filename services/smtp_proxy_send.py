"""SMTP через SOCKS5: пул активных прокси, без привязки к ящику."""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import List, Optional, Set, Tuple, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount, Proxy
from proxy_manager import ProxySMTPContext
from services.proxy_binding import (
    NO_ACTIVE_PROXY,
    deactivate_proxy_from_mailing,
    is_mailing_proxy_failure,
    list_sendable_proxies,
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

MAIL_SMTP_MAX_PROXIES = max(1, min(10, int(os.getenv("MAIL_SMTP_MAX_PROXIES", "5"))))
MAIL_MAILING_MAX_PROXIES = max(1, min(10, int(os.getenv("MAIL_MAILING_MAX_PROXIES", "5"))))
REPLY_SMTP_MAX_PROXIES = MAIL_SMTP_MAX_PROXIES

T = TypeVar("T")


def _mailing_proxy_attempt_cap(n_sendable: int, *, mailing: bool) -> int:
    cap = MAIL_MAILING_MAX_PROXIES if mailing else MAIL_SMTP_MAX_PROXIES
    return max(1, min(cap, max(1, n_sendable)))


async def choose_required_proxy(
    session: AsyncSession,
    user_id: int,
    *,
    account: EmailAccount | None = None,
    exclude_ids: set[int] | None = None,
) -> Tuple[Optional[Proxy], Optional[str]]:
    del account
    from services.proxy_binding import pick_mailing_proxy

    proxy = await pick_mailing_proxy(session, int(user_id), exclude_ids=exclude_ids)
    if proxy:
        return proxy, None
    return None, NO_ACTIVE_PROXY


async def _list_active_socks5_proxies(session: AsyncSession, user_id: int) -> List[Proxy]:
    return await list_sendable_proxies(session, user_id)


async def _send_with_proxy_pool(
    session: AsyncSession,
    user_id: int,
    account: EmailAccount,
    *,
    mailing: bool,
    run_on_proxy: Callable[[Proxy], Awaitable[T]],
    fail_result: Callable[[str], T],
) -> T:
    exclude: Set[int] = set()
    sendable = await list_sendable_proxies(session, int(user_id))
    max_tries = _mailing_proxy_attempt_cap(len(sendable), mailing=mailing)
    last_err: Optional[str] = None

    for _ in range(max_tries):
        proxy, pick_err = await resolve_proxy_for_account(
            session, account, exclude_ids=exclude
        )
        if pick_err or not proxy:
            return fail_result(pick_err or NO_ACTIVE_PROXY)

        pid = int(proxy.id)
        logger.info(
            "[SMTP %s] pool proxy_id=%s %s:%s account=%s",
            "mailing" if mailing else "send",
            pid,
            proxy.host,
            proxy.port,
            account.email,
        )

        async with ProxySMTPContext(proxy):
            result = await run_on_proxy(proxy)

        if _result_ok(result):
            try:
                await ProxyManager.note_proxy_success(session, pid)
            except Exception:
                pass
            return result

        last_err = _result_err(result)
        logger.warning(
            "[SMTP fail] proxy_id=%s account=%s err=%s",
            pid,
            account.email,
            (last_err or "")[:200],
        )

        if mailing and is_mailing_proxy_failure(last_err):
            await deactivate_proxy_from_mailing(session, proxy, last_err)
            exclude.add(pid)
            continue

        dead = is_definite_proxy_failure(last_err)
        try:
            await ProxyManager.note_proxy_failure(
                session,
                pid,
                (last_err or "")[:500],
                deactivate=dead,
                from_mailing=mailing,
            )
        except Exception:
            pass
        if mailing and dead:
            exclude.add(pid)
            continue
        break

    return fail_result(last_err or NO_ACTIVE_PROXY)


def _result_ok(result) -> bool:
    if isinstance(result, list):
        return any(ok for ok, _ in result)
    ok, _err, _mid = result
    return bool(ok)


def _result_err(result) -> Optional[str]:
    if isinstance(result, list):
        for ok, err in result:
            if not ok:
                return normalize_send_error(err)
        return None
    _ok, err, _mid = result
    return normalize_send_error(err)


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
    smtp_tmo = (
        MAIL_MAILING_TIMEOUT_SEC
        if mailing
        else (REPLY_SMTP_TIMEOUT_SEC if fast else MAIL_SMTP_TIMEOUT_SEC)
    )

    async def run(_proxy: Proxy):
        return await send_email_via_account(
            account,
            to_email,
            subject,
            body,
            sender_name=sender_name,
            is_html=is_html,
            smtp_timeout_sec=smtp_tmo,
        )

    def fail(err: str) -> Tuple[bool, Optional[str], Optional[str]]:
        e = normalize_send_error(err)
        if is_smtp_timeout_error(e):
            hint = "Все SOCKS5 из пула недоступны. Добавьте прокси в «Прокси»."
            return False, f"SMTP_TIMEOUT|pool|{hint}", None
        return False, e or NO_ACTIVE_PROXY, None

    out = await _send_with_proxy_pool(
        session,
        int(user_id),
        account,
        mailing=mailing,
        run_on_proxy=run,
        fail_result=fail,
    )
    ok, err, msgid = out
    return bool(ok), normalize_send_error(err), msgid


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

    smtp_tmo = MAIL_MAILING_TIMEOUT_SEC if mailing else MAIL_SMTP_TIMEOUT_SEC

    async def run(_proxy: Proxy) -> List[Tuple[bool, Optional[str]]]:
        raw = await send_batch_via_account(
            account,
            items,
            sender_name=sender_name,
            smtp_timeout_sec=smtp_tmo,
        )
        merged: List[Tuple[bool, Optional[str]]] = []
        for j in range(n):
            ok, err = raw[j] if j < len(raw) else (False, "BATCH_INDEX_ERROR")
            merged.append((bool(ok), normalize_send_error(err)))
        return merged

    def fail(err: str) -> List[Tuple[bool, Optional[str]]]:
        return [(False, normalize_send_error(err) or NO_ACTIVE_PROXY) for _ in items]

    return await _send_with_proxy_pool(
        session,
        int(user_id),
        account,
        mailing=mailing,
        run_on_proxy=run,
        fail_result=fail,
    )
