"""SMTP sending that always runs through the user's proxy."""
from __future__ import annotations

import logging
import os
import random
from typing import List, Optional, Tuple

from sqlalchemy import or_ as sa_or
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount, Proxy
from proxy_manager import ProxySMTPContext, is_socks5_proxy
from services.sender import (
    is_definite_proxy_failure,
    is_smtp_timeout_error,
    normalize_send_error,
    send_batch_via_account,
    send_email_via_account,
    should_retry_send_with_other_proxy,
)
from services.proxy_manager import ProxyManager

logger = logging.getLogger(__name__)

NO_ACTIVE_PROXY = "PROXY_ERROR|no_active_proxy|No active proxy configured"

# Быстрый ответ в чате (пресет/HTML/текст).
REPLY_SMTP_TIMEOUT_SEC = max(15, min(60, int(os.getenv("REPLY_SMTP_TIMEOUT_SEC", "28"))))
REPLY_SMTP_MAX_PROXIES = max(1, min(6, int(os.getenv("REPLY_SMTP_MAX_PROXIES", "2"))))

# Рассылка /send: несколько SOCKS5, таймаут на каждую попытку.
MAIL_SMTP_TIMEOUT_SEC = max(25, min(90, int(os.getenv("MAIL_SMTP_TIMEOUT_SEC", "55"))))
MAIL_SMTP_MAX_PROXIES = max(1, min(8, int(os.getenv("MAIL_SMTP_MAX_PROXIES", "4"))))

_LAST_OK_PROXY_ID: dict[int, int] = {}


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
    """Все SOCKS5 пользователя; «красные» в UI не исключаем из рассылки."""
    rows = (
        await session.execute(
            sa_select(Proxy)
            .where(Proxy.user_id == int(user_id))
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

    def _pref_key(px: Proxy) -> int:
        if px.is_active is True:
            return 0
        if px.is_active is None:
            return 1
        return 2

    out.sort(key=_pref_key)
    return out


def _order_proxies_for_send(user_id: int, proxies: List[Proxy], *, fast: bool) -> List[Proxy]:
    if not proxies:
        return []
    uid = int(user_id)
    last_id = _LAST_OK_PROXY_ID.get(uid)
    head: List[Proxy] = []
    tail: List[Proxy] = []
    for p in proxies:
        if last_id and int(p.id) == int(last_id):
            head.append(p)
        else:
            tail.append(p)
    random.shuffle(tail)
    order = head + tail
    limit = REPLY_SMTP_MAX_PROXIES if fast else MAIL_SMTP_MAX_PROXIES
    return order[:limit]


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
) -> Tuple[bool, Optional[str], Optional[str]]:
    proxies = await _list_active_socks5_proxies(session, user_id)
    if not proxies:
        return False, NO_ACTIVE_PROXY, None

    order = _order_proxies_for_send(int(user_id), proxies, fast=fast)
    smtp_tmo = REPLY_SMTP_TIMEOUT_SEC if fast else MAIL_SMTP_TIMEOUT_SEC

    last_err: str | None = None
    last_msgid: str | None = None
    tried = 0

    for proxy in order:
        pid = int(proxy.id)
        tried += 1
        logger.info(
            "[SMTP send] try proxy_id=%s %s:%s account=%s -> %s (%s/%s fast=%s)",
            pid,
            proxy.host,
            proxy.port,
            account.email,
            to_email,
            tried,
            len(order),
            fast,
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
            _LAST_OK_PROXY_ID[int(user_id)] = pid
            try:
                await ProxyManager.note_proxy_success(session, pid)
            except Exception:
                pass
            return True, err, msgid

        last_err = err
        last_msgid = msgid
        logger.warning(
            "[SMTP send] fail proxy_id=%s account=%s err=%s",
            pid,
            account.email,
            (err or "")[:200],
        )

        dead = is_definite_proxy_failure(err)
        try:
            await ProxyManager.note_proxy_failure(
                session,
                pid,
                (err or "")[:500],
                deactivate=dead,
                from_mailing=True,
            )
        except Exception:
            pass

        if not should_retry_send_with_other_proxy(err):
            return False, err, last_msgid

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
    """Отправка пачки: неудачные адреса повторяются на следующем SOCKS5 (не «3 из 10»)."""
    n = len(items)
    if n == 0:
        return []

    proxies = await _list_active_socks5_proxies(session, user_id)
    if not proxies:
        return [(False, NO_ACTIVE_PROXY) for _ in items]

    order = _order_proxies_for_send(int(user_id), proxies, fast=False)
    merged: List[Tuple[bool, Optional[str]]] = [(False, NO_ACTIVE_PROXY) for _ in range(n)]
    pending: List[int] = list(range(n))

    for proxy in order:
        if not pending:
            break

        pid = int(proxy.id)
        batch_items = [items[i] for i in pending]
        logger.info(
            "[SMTP batch] proxy_id=%s account=%s pending=%s/%s",
            pid,
            account.email,
            len(batch_items),
            n,
        )

        async with ProxySMTPContext(proxy):
            raw = await send_batch_via_account(
                account,
                batch_items,
                sender_name=sender_name,
                smtp_timeout_sec=MAIL_SMTP_TIMEOUT_SEC,
            )

        new_pending: List[int] = []
        any_ok = False
        for j, idx in enumerate(pending):
            ok, err = raw[j] if j < len(raw) else (False, "BATCH_INDEX_ERROR")
            err_n = normalize_send_error(err)
            merged[idx] = (bool(ok), err_n)
            if ok:
                any_ok = True
            elif should_retry_send_with_other_proxy(err_n):
                new_pending.append(idx)

        if any_ok:
            _LAST_OK_PROXY_ID[int(user_id)] = pid
            try:
                await ProxyManager.note_proxy_success(session, pid)
            except Exception:
                pass

        if not new_pending:
            return merged

        last_err = next((e for o, e in merged if not o and e), None)
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

        if not should_retry_send_with_other_proxy(last_err):
            return merged

        pending = new_pending

    return merged
