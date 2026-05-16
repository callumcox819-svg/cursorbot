# services/smtp_sender.py
from __future__ import annotations

from typing import Optional, Tuple
from models import EmailAccount
from database import Session
from services.smtp_proxy_send import send_email_via_account_with_proxy


async def send_email_via_smtp(
    *,
    from_email: str,
    password: str,
    to_email: str,
    subject: str,
    sender_name: Optional[str] = None,
    body: str,
    provider: str = "gmail",
    **kwargs,
) -> Tuple[bool, Optional[str]]:
    """
    Совместимость с handlers/send.py.
    В проекте нет smtp_host/smtp_port, поэтому берём provider и используем sender.py
    """
    acc = EmailAccount(email=from_email, password=password, provider=provider)
    user_id = int(kwargs.get("user_id") or 0)
    if not user_id:
        return False, "PROXY_ERROR|no_user|user_id required for SMTP send"
    async with Session() as session:
        ok, err = await send_email_via_account_with_proxy(
            session, user_id, acc, to_email, subject, body, sender_name=sender_name
        )
    return ok, err
