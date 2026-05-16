from __future__ import annotations

import asyncio
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Tuple


SMTP_BY_PROVIDER = {
    "gmail": ("smtp.gmail.com", 587),
    "gmx": ("mail.gmx.net", 587),
    "icloud": ("smtp.mail.me.com", 587),
    "outlook": ("smtp.office365.com", 587),
    "yahoo": ("smtp.mail.yahoo.com", 587),
}


def _smtp_host_port(provider: str, email_addr: str) -> Tuple[str, int]:
    provider = (provider or "").lower().strip()
    if provider in SMTP_BY_PROVIDER:
        return SMTP_BY_PROVIDER[provider]
    # fallback: по домену
    domain = email_addr.split("@")[-1].strip() if "@" in email_addr else ""
    if domain:
        return f"smtp.{domain}", 587
    return "smtp.gmail.com", 587


def _send_smtp_blocking(
    from_email: str,
    password: str,
    to_email: str,
    subject: str,
    body: str,
    provider: str,
    sender_name: str | None = None,
):
    host, port = _smtp_host_port(provider, from_email)

    msg = MIMEText(body or "", "plain", "utf-8")
    msg["Subject"] = subject or ""
    # ✅ ВАЖНО: имя отправителя для plain-text писем
    msg["From"] = formataddr((sender_name, from_email)) if sender_name else from_email
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    server = smtplib.SMTP(host, port, timeout=25)
    server.ehlo()
    server.starttls()
    server.ehlo()
    server.login(from_email, (password or "").strip())

    # envelope-from всегда должен быть голый email
    refused = server.sendmail(from_email, [to_email], msg.as_string())
    if refused:
        raise RuntimeError(f"SMTP refused recipients: {refused}")
    server.quit()


async def send_reply_via_smtp(
    *,
    from_email: str,
    password: str,
    to_email: str,
    subject: str,
    text: str,
    provider: str,
    sender_name: str | None = None,
):
    await asyncio.to_thread(
        _send_smtp_blocking,
        from_email,
        password,
        to_email,
        subject,
        text,
        provider,
        sender_name,
    )


def _send_html_smtp_blocking_checked(
    from_email: str,
    password: str,
    to_email: str,
    subject: str,
    html_body: str,
    provider: str,
    sender_name: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Send HTML email and return (ok, error, message_id).

    Uses multipart/alternative (plain + html), UTF-8.
    We return message-id so caller can display it for debugging.
    """
    from email.message import EmailMessage

    host, port = _smtp_host_port(provider, from_email)

    msg = EmailMessage()
    msg["Subject"] = subject or ""
    msg["From"] = formataddr((sender_name, from_email)) if sender_name else from_email
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    msgid = make_msgid()
    msg["Message-ID"] = msgid

    # Plain fallback (keep it simple/ASCII)
    msg.set_content("Please open this email in an HTML-capable client.")
    msg.add_alternative(html_body or "", subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(from_email, (password or "").strip())

            # envelope-from всегда должен быть голый email
            refused = server.send_message(msg)
            if refused:
                return False, f"SMTP refused recipients: {refused}", msgid

        return True, None, msgid
    except Exception as e:
        return False, str(e), msgid


def _send_html_smtp_blocking(
    from_email: str,
    password: str,
    to_email: str,
    subject: str,
    html_body: str,
    provider: str,
    sender_name: str | None = None,
):
    """Backward-compatible wrapper.

    Raises on failure (old behavior), so existing callers that rely on exceptions keep working.
    """
    ok, err, _msgid = _send_html_smtp_blocking_checked(
        from_email,
        password,
        to_email,
        subject,
        html_body,
        provider,
        sender_name,
    )
    if not ok:
        raise RuntimeError(err or "SMTP send failed")


async def send_html_reply_via_smtp_checked(
    *,
    from_email: str,
    password: str,
    to_email: str,
    subject: str,
    html_body: str,
    provider: str,
    sender_name: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Send an HTML email and return (ok, error, message_id)."""
    return await asyncio.to_thread(
        _send_html_smtp_blocking_checked,
        from_email,
        password,
        to_email,
        subject,
        html_body,
        provider,
        sender_name,
    )


async def send_html_reply_via_smtp(
    *,
    from_email: str,
    password: str,
    to_email: str,
    subject: str,
    html_body: str,
    provider: str,
    sender_name: str | None = None,
):
    """Send an HTML email (used for custom templates)
    - keeps old exception behavior.
    """
    ok, err, _msgid = await send_html_reply_via_smtp_checked(
        from_email=from_email,
        password=password,
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        provider=provider,
        sender_name=sender_name,
    )
    if not ok:
        raise RuntimeError(err or "SMTP send failed")
