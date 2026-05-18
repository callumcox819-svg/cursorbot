"""Рассылка /send: SMTP+NOOP как в обычном софте; IMAP Sent — только по флагу."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount
from services.sender import normalize_send_error, should_retry_send_with_other_proxy
from services.smtp_delivery_verify import verify_message_in_sent
from services.smtp_proxy_send import (
    MAIL_SMTP_MAX_PROXIES,
    MAIL_SMTP_TIMEOUT_SEC,
    send_email_via_account_with_proxy,
)

logger = logging.getLogger(__name__)

# Как в типичном софте: успех = SMTP 250 + NOOP (в sender.py). IMAP — опционально.
MAIL_VERIFY_SENT = os.getenv("MAIL_VERIFY_SENT", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
MAIL_VERIFY_SENT_DELAY_SEC = max(2, min(8, int(os.getenv("MAIL_VERIFY_SENT_DELAY_SEC", "3"))))
MAIL_SEND_RETRIES = max(1, min(3, int(os.getenv("MAIL_SEND_RETRIES", "2"))))
MAIL_SEND_RETRY_PAUSE_SEC = max(
    1.0, min(8.0, float(os.getenv("MAIL_SEND_RETRY_PAUSE_SEC", "2")))
)


def mailing_send_overall_timeout_sec() -> int:
    """Лимит на одно письмо: прокси × таймаут × попытки (без 10-минутных зависаний)."""
    per = MAIL_SMTP_MAX_PROXIES * MAIL_SMTP_TIMEOUT_SEC + 20
    raw = per * MAIL_SEND_RETRIES + MAIL_SEND_RETRIES * MAIL_SEND_RETRY_PAUSE_SEC + 15
    return max(60, min(240, int(os.getenv("SEND_ONE_TIMEOUT", str(int(raw))))))


def _retry_after_failure(err: str | None) -> bool:
    return should_retry_send_with_other_proxy(err)


async def send_mailing_one(
    session: AsyncSession,
    user_id: int,
    account: EmailAccount,
    to_email: str,
    subject: str,
    body: str,
    sender_name: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    last_err: Optional[str] = None
    last_msgid: Optional[str] = None

    for attempt in range(1, MAIL_SEND_RETRIES + 1):
        ok, err, msgid = await send_email_via_account_with_proxy(
            session,
            int(user_id),
            account,
            to_email,
            subject,
            body,
            sender_name=sender_name,
        )
        err = normalize_send_error(err)
        last_err = err
        last_msgid = msgid

        if not ok:
            if _retry_after_failure(err) and attempt < MAIL_SEND_RETRIES:
                await asyncio.sleep(MAIL_SEND_RETRY_PAUSE_SEC)
                continue
            return False, err, msgid

        if MAIL_VERIFY_SENT:
            await asyncio.sleep(MAIL_VERIFY_SENT_DELAY_SEC)
            try:
                verified, verify_msg = await verify_message_in_sent(
                    account.email,
                    account.password or "",
                    subject=subject,
                    to_email=to_email,
                    message_id=msgid,
                )
            except Exception as e:
                verified, verify_msg = False, str(e)
            if not verified:
                last_err = normalize_send_error(
                    f"SMTP_ACCEPTED_NOT_IN_SENT|verify|{verify_msg or 'not in Sent'}"
                )
                if attempt < MAIL_SEND_RETRIES:
                    await asyncio.sleep(MAIL_SEND_RETRY_PAUSE_SEC)
                    continue
                return False, last_err, msgid

        return True, None, msgid

    return False, last_err or "UNKNOWN", last_msgid


# совместимость
send_mailing_one_verified = send_mailing_one
