"""SMTP sending that always runs through the user's proxy."""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount, Proxy
from proxy_manager import ProxySMTPContext, choose_proxy_for_user
from services.sender import send_batch_via_account, send_email_via_account

NO_ACTIVE_PROXY = "PROXY_ERROR|no_active_proxy|No active proxy configured"


async def choose_required_proxy(session: AsyncSession, user_id: int) -> Tuple[Optional[Proxy], Optional[str]]:
    proxy = await choose_proxy_for_user(session, int(user_id))
    if not proxy:
        return None, NO_ACTIVE_PROXY
    return proxy, None


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
    proxy, err = await choose_required_proxy(session, user_id)
    if err:
        return False, err
    async with ProxySMTPContext(proxy):
        return await send_email_via_account(
            account,
            to_email,
            subject,
            body,
            sender_name=sender_name,
            is_html=is_html,
        )


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
        return await send_batch_via_account(account, items, sender_name=sender_name)
