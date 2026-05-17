"""Проверка, что письмо реально попало в «Отправленные» (IMAP), а не только принято SMTP."""

from __future__ import annotations

import imaplib
import re
from email import message_from_bytes
from email.header import decode_header
from typing import Optional


def _decode_hdr(val: str | bytes | None) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        parts = decode_header(val)
        out = []
        for frag, enc in parts:
            if isinstance(frag, bytes):
                out.append(frag.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(str(frag))
        return "".join(out)
    return str(val)


def _find_sent_mailbox(M: imaplib.IMAP4_SSL) -> Optional[str]:
    typ, data = M.list()
    if typ != "OK" or not data:
        return None
    fallback: Optional[str] = None
    for raw in data:
        if not raw:
            continue
        line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
        low = line.lower()
        m = re.findall(r'"([^"]+)"', line)
        name = (m[-1] if m else "").strip() or line.split()[-1].strip('"')
        if not name:
            continue
        if "\\sent" in low or "[gmail]/sent" in low.replace(" ", ""):
            return name
        if "sent" in name.lower() and "draft" not in name.lower():
            fallback = name
    return fallback or "Sent"


def verify_message_in_sent_sync(
    account_email: str,
    password: str,
    *,
    subject: str,
    to_email: str | None = None,
    message_id: str | None = None,
    lookback: int = 30,
) -> tuple[bool, str]:
    """
    Ищем письмо в Sent у отправителя (IMAP без прокси).
    False = SMTP мог «принять», но в ящике отправителя письма нет.
    """
    email = (account_email or "").strip()
    pwd = (password or "").strip()
    subj_needle = (subject or "").strip()
    to_needle = (to_email or "").strip().lower()
    msgid_needle = (message_id or "").strip().strip("<>")

    if not email or not pwd:
        return False, "Нет email/пароля для IMAP-проверки"

    domain = email.rsplit("@", 1)[-1].lower()
    _imap = {
        "gmail.com": "imap.gmail.com",
        "googlemail.com": "imap.gmail.com",
        "gmx.com": "imap.gmx.com",
        "gmx.net": "imap.gmx.com",
        "gmx.de": "imap.gmx.net",
        "web.de": "imap.web.de",
        "icloud.com": "imap.mail.me.com",
        "outlook.com": "outlook.office365.com",
        "hotmail.com": "outlook.office365.com",
        "live.com": "outlook.office365.com",
    }
    host = _imap.get(domain, f"imap.{domain}")
    M: imaplib.IMAP4_SSL | None = None
    try:
        M = imaplib.IMAP4_SSL(host, 993)
        M.login(email, pwd)
        sent_box = _find_sent_mailbox(M)
        if not sent_box:
            return False, "Не найдена папка «Отправленные» по IMAP"

        typ, _ = M.select(sent_box)
        if typ != "OK":
            return False, f"Не удалось открыть {sent_box}"

        typ, data = M.search(None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return False, "Папка «Отправленные» пуста — письмо не ушло"

        uids = data[0].split()
        for uid in reversed(uids[-max(5, lookback) :]):
            typ, msgdata = M.fetch(
                uid,
                "(BODY.PEEK[HEADER.FIELDS (SUBJECT TO MESSAGE-ID FROM)])",
            )
            if typ != "OK" or not msgdata:
                continue
            raw = msgdata[0][1] if isinstance(msgdata[0], tuple) else msgdata[0]
            if not raw:
                continue
            hdr = message_from_bytes(raw if isinstance(raw, bytes) else str(raw).encode())
            subj = _decode_hdr(hdr.get("Subject"))
            to_h = _decode_hdr(hdr.get("To")).lower()
            mid = _decode_hdr(hdr.get("Message-ID")).strip("<>")

            subj_ok = bool(subj_needle) and subj_needle in subj
            to_ok = (not to_needle) or to_needle in to_h
            mid_ok = (not msgid_needle) or msgid_needle in mid

            if subj_ok and to_ok and (mid_ok or not msgid_needle):
                return True, f"Найдено в «Отправленные» ({sent_box})"

        return False, (
            "SMTP не вернул ошибку, но в «Отправленных» у отправителя письма нет — "
            "отправка через прокси, скорее всего, не сработала."
        )
    except imaplib.IMAP4.error as e:
        return False, f"IMAP: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


async def verify_message_in_sent(
    account_email: str,
    password: str,
    *,
    subject: str,
    to_email: str | None = None,
    message_id: str | None = None,
) -> tuple[bool, str]:
    import asyncio

    from proxy_manager import database_socket_guard

    async with database_socket_guard():
        return await asyncio.to_thread(
            verify_message_in_sent_sync,
            account_email,
            password,
            subject=subject,
            to_email=to_email,
            message_id=message_id,
        )
