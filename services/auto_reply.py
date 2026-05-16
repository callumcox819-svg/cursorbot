from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import select as sa_select

from database import Session
from models import EmailAccount, User
from services.user_settings import get_user_setting
from services.smtp_proxy_send import send_email_via_account_with_proxy


AUTO_CFG_KEY = "auto_reply_cfg"


def _norm_subject(subject: str) -> str:
    s = (subject or "").strip()
    s = re.sub(r"^(re|aw|fw|fwd)\s*:\s*", "", s, flags=re.I).strip()
    return s


def _load_cfg(raw: Optional[str]) -> dict:
    """
    cfg json пример:
    {
      "enabled": true,
      "text": "Hallo! Hier ist der Link: {LINK}"
    }
    """
    if not raw:
        return {"enabled": False, "text": ""}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"enabled": False, "text": ""}


def _render_text(template: str, meta: dict[str, Any]) -> str:
    link = (meta.get("generated_link") or meta.get("ad_url") or "").strip()
    from_name = (meta.get("from_name") or "").strip()
    subject = (meta.get("subject") or "").strip()

    out = template or ""
    out = out.replace("{LINK}", link)
    out = out.replace("{FROM_NAME}", from_name)
    out = out.replace("{SUBJECT}", subject)
    return out.strip()


async def try_auto_reply_for_incoming(
    *,
    account_id: int,
    imap_uid: str,
    meta: dict[str, Any],
) -> tuple[bool, str | None]:
    """
    Делает авто-ответ на входящее письмо, если включено.
    Возвращает (sent, error).
    """

    # meta минимум:
    # from_email, from_name, subject, account_email, ad_url?, generated_link?
    to_email = (meta.get("from_email") or "").strip().lower()
    subject_in = (meta.get("subject") or "").strip()

    if not to_email or "@" not in to_email:
        return False, None  # некому отвечать

    async with Session() as session:
        acc = (
            await session.execute(
                sa_select(EmailAccount).where(EmailAccount.id == int(account_id))
            )
        ).scalars().first()
        if not acc:
            return False, "SMTP account not found"

        user = (
            await session.execute(
                sa_select(User).where(User.id == int(acc.user_id))
            )
        ).scalars().first()
        if not user:
            return False, "User not found"

        raw_cfg = await get_user_setting(session, user, AUTO_CFG_KEY)
        cfg = _load_cfg(raw_cfg)

        if not bool(cfg.get("enabled", False)):
            return False, None  # авто-ответ выключен

        template = (cfg.get("text") or "").strip()
        if not template:
            return False, "Auto-reply enabled but template is empty"

        body = _render_text(template, meta)
        if not body:
            return False, "Rendered auto-reply is empty"

        out_subject = _norm_subject(subject_in)
        out_subject = f"Re: {out_subject}" if out_subject else "Re:"

        sender_name = getattr(user, "sender_name", None) or None

        ok, err = await send_email_via_account_with_proxy(
            session,
            int(user.id),
            acc,
            to_email,
            out_subject,
            body,
            sender_name=sender_name,
        )
        if not ok:
            return False, err or "SMTP send failed"

    return True, None
