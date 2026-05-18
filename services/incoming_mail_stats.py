"""Статистика и разбор входящих писем в БД (для /imap_diag)."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models import ConversationLink, IncomingMail
from services.incoming_mail_worker import (
    _is_google_system_mail,
    _is_smtp_block_bounce,
)


_AUTO_REPLY_RE = re.compile(
    r"(out of office|auto[\s-]?reply|autoreply|abwesenheit|urlaub|vacation|"
    r"nicht im (büro|buero)|away from|i am away|automatic reply|réponse automatique)",
    re.I,
)

_PLATFORM_DOMAIN_RE = re.compile(
    r"(tutti\.ch|ricardo\.ch|anibis\.|facebook\.com|marketplace|post\.ch|gmx\.(net|de)|"
    r"mail\.gmail\.com)",
    re.I,
)


def _is_auto_reply(subject: str, body: str) -> bool:
    blob = f"{subject or ''}\n{body or ''}"[:2000]
    return bool(_AUTO_REPLY_RE.search(blob))


def _is_platform_sender(from_email: str) -> bool:
    f = (from_email or "").strip().lower()
    if not f or "@" not in f:
        return False
    domain = f.split("@", 1)[1]
    return bool(_PLATFORM_DOMAIN_RE.search(domain))


def classify_incoming_row(row: IncomingMail) -> str:
    """Одна категория на письмо (приоритет сверху вниз)."""
    fe = row.from_email or ""
    fn = row.from_name or ""
    subj = row.subject or ""
    body = row.body or ""

    if _is_smtp_block_bounce(fe, subj, body):
        return "bounce"
    if _is_google_system_mail(fe, fn, subj):
        return "google"
    if _is_auto_reply(subj, body):
        return "auto_reply"
    if _is_platform_sender(fe):
        return "platform"
    if row.resolved_offer_email_id:
        return "seller_matched"
    if row.resolved_offer_id:
        return "offer_title_only"
    return "unmatched"


_CATEGORY_LABELS = {
    "seller_matched": "🟢 Продавец (email в базе) — ближе всего к «живому»",
    "offer_title_only": "📦 Только оффер/тема (email отправителя не совпал)",
    "platform": "🏪 Платформа / сервис (tutti, ricardo, gmx…)",
    "google": "📧 Google / системное (в TG обычно нет карточки)",
    "bounce": "↩️ Отбой / block (mailer-daemon)",
    "auto_reply": "🤖 Автоответ (out of office)",
    "unmatched": "❓ Без привязки к офферу (возможная потеря)",
}


async def build_incoming_breakdown(session: AsyncSession, db_user_id: int) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(IncomingMail)
            .where(IncomingMail.user_id == int(db_user_id))
            .order_by(IncomingMail.id.desc())
        )
    ).scalars().all()

    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = {k: [] for k in _CATEGORY_LABELS}

    for row in rows:
        cat = classify_incoming_row(row)
        counts[cat] += 1
        if len(samples[cat]) < 5:
            fe = (row.from_email or "—")[:40]
            subj = (row.subject or "—").replace("\n", " ")[:55]
            samples[cat].append(f"<code>{fe}</code> — {subj}")

    tg_dialogs = (
        await session.execute(
            select(func.count(ConversationLink.id)).where(
                ConversationLink.user_id == int(db_user_id),
                ConversationLink.tg_message_id.isnot(None),
            )
        )
    ).scalar() or 0

    seller_matched = int(counts.get("seller_matched", 0))

    return {
        "total": len(rows),
        "counts": dict(counts),
        "samples": samples,
        "tg_dialogs": int(tg_dialogs),
        "likely_live": seller_matched,
        "offer_title_only": int(counts.get("offer_title_only", 0)),
    }


def format_incoming_breakdown_html(data: dict[str, Any]) -> str:
    total = int(data.get("total") or 0)
    if total <= 0:
        return "\n<b>Разбор входящих:</b> в БД пока 0 писем."

    lines = [
        f"\n<b>Разбор входящих в БД ({total}):</b>",
        f"≈ «живых» (email продавца в базе): <b>{data.get('likely_live', 0)}</b>",
        f"Диалогов с карточкой в TG (anchor): <b>{data.get('tg_dialogs', 0)}</b>",
        "",
    ]

    order = (
        "seller_matched",
        "offer_title_only",
        "unmatched",
        "platform",
        "google",
        "bounce",
        "auto_reply",
    )
    counts: dict[str, int] = data.get("counts") or {}
    for key in order:
        n = int(counts.get(key, 0))
        if n <= 0:
            continue
        lines.append(f"{_CATEGORY_LABELS[key]}: <b>{n}</b>")
        for sample in (data.get("samples") or {}).get(key, [])[:3]:
            lines.append(f"  • {sample}")

    unmatched = int(counts.get("unmatched", 0))
    if unmatched:
        lines.append(
            "\n<i>«Без привязки» — ответ мог прийти, но бот не связал с оффером "
            "(другой адрес, нет Re: в теме). Их стоит проверить вручную в почте.</i>"
        )

    return "\n".join(lines)
