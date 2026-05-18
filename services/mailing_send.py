"""Рассылка /send: повторы SMTP и опциональная проверка IMAP Sent."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailAccount
from services.sender import normalize_send_error, should_retry_send_with_other_proxy
from services.smtp_delivery_verify import verify_message_in_sent
from services.smtp_proxy_send import send_email_via_account_with_proxy

logger = logging.getLogger(__name__)

MAIL_VERIFY_SENT = os.getenv("MAIL_VERIFY_SENT", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
MAIL_VERIFY_SENT_DELAY_SEC = max(2, min(12, int(os.getenv("MAIL_VERIFY_SENT_DELAY_SEC", "4"))))
MAIL_SEND_RETRIES = max(1, min(5, int(os.getenv("MAIL_SEND_RETRIES", "3"))))
MAIL_SEND_RETRY_PAUSE_SEC = max(
    1.0, min(15.0, float(os.getenv("MAIL_SEND_RETRY_PAUSE_SEC", "3")))
)


def mailing_send_overall_timeout_sec() -> int:
    """Верхний предел wait_for на одно письмо (все попытки + IMAP)."""
    from services.smtp_proxy_send import MAIL_SMTP_MAX_PROXIES, MAIL_SMTP_TIMEOUT_SEC

    per_smtp = MAIL_SMTP_MAX_PROXIES * MAIL_SMTP_TIMEOUT_SEC + 25
    extra = MAIL_SEND_RETRIES * (
        (MAIL_VERIFY_SENT_DELAY_SEC + 8 if MAIL_VERIFY_SENT else 0)
        + MAIL_SEND_RETRY_PAUSE_SEC
    )
    raw = int(os.getenv("SEND_ONE_TIMEOUT", str(per_smtp * MAIL_SEND_RETRIES + extra + 20)))
    # Одно письмо не должно «висеть» в /stat без счётчиков дольше ~6 мин.
    return max(90, min(360, raw))


def _retry_after_failure(err: str | None) -> bool:
    if should_retry_send_with_other_proxy(err):
        return True
    s = (err or "").upper()
    return "SMTP_ACCEPTED_NOT_IN_SENT" in s or "NOT_IN_SENT" in s


async def send_mailing_one_verified(
    session: AsyncSession,
    user_id: int,
    account: EmailAccount,
    to_email: str,
    subject: str,
    body: str,
    sender_name: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    До MAIL_SEND_RETRIES попыток: SMTP через SOCKS5 (смена прокси внутри) → при успехе IMAP Sent.
    Успех только если письмо найдено в «Отправленных» (или MAIL_VERIFY_SENT=0).
    """
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
                logger.info(
                    "[mailing retry] smtp fail %s/%s %s -> %s: %s",
                    attempt,
                    MAIL_SEND_RETRIES,
                    account.email,
                    to_email,
                    (err or "")[:160],
                )
                await asyncio.sleep(MAIL_SEND_RETRY_PAUSE_SEC)
                continue
            return False, err, msgid

        if not MAIL_VERIFY_SENT:
            return True, None, msgid

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

        if verified:
            return True, None, msgid

        last_err = normalize_send_error(
            f"SMTP_ACCEPTED_NOT_IN_SENT|verify|{verify_msg or 'not in Sent'}"
        )
        logger.warning(
            "[mailing retry] not in Sent %s/%s %s -> %s",
            attempt,
            MAIL_SEND_RETRIES,
            account.email,
            to_email,
        )
        if attempt < MAIL_SEND_RETRIES:
            await asyncio.sleep(MAIL_SEND_RETRY_PAUSE_SEC)
            continue
        return False, last_err, msgid

    return False, last_err or "UNKNOWN", last_msgid
