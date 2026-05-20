"""Тема и имя отправителя для HTML-ответов (отдельно от рассылки с OFFER)."""

from __future__ import annotations

from models import User
from services.html_spoof import apply_nick_to_html, get_spoof_display_name
from services.user_settings import get_user_setting

# как в handlers/settings.py → html_theme_menu
HTML_THEME_KEY = "html_theme"


async def get_html_reply_subject(session, user: User, *, fallback: str = "") -> str:
    """
    Тема для HTML: задаёт пользователь в настройках (📌 Тема HTML).
    Не путать с глобальным OFFER для массовой рассылки.
    """
    subj = (await get_user_setting(session, user, HTML_THEME_KEY) or "").strip()
    if subj:
        return subj[:140] if len(subj) > 140 else subj
    fb = (fallback or "").strip()
    return fb[:140] if len(fb) > 140 else (fb or "Message")


async def get_html_sender_name(session, user: User) -> str | None:
    """
    HTML: при 🟢 Спуфинг — имя из «👤 Имя для спуфинга».
    Иначе — имя отправителя из аккаунтов (user.sender_name), как при рассылке.
    """
    spoof = await get_spoof_display_name(session, user)
    if spoof:
        return spoof
    name = (getattr(user, "sender_name", None) or "").strip()
    return name or None


async def prepare_html_body(html: str, session, user: User) -> str:
    nick = await get_spoof_display_name(session, user)
    return apply_nick_to_html(html, nick)


def _canon_email(email: str) -> str:
    return (email or "").strip().lower()


def _format_chf_price(price: str) -> str:
    p = (price or "").strip()
    if not p:
        return ""
    if p.upper().startswith("CHF"):
        return p
    return f"CHF {p}"


async def build_offer_html_ctx(
    session,
    user_id: int,
    seller_email: str,
    *,
    link: str = "",
    subject: str = "",
    from_name: str = "",
    body_text: str = "",
) -> dict[str, str]:
    """Контекст для HTML: оффер строго по теме письма (как карточка/GAG), не последний по email."""
    from services.incoming_mail_worker import resolve_offer_for_mail_card
    from services.offer_matching import normalized_reply_subject, subject_is_informative
    from services.offer_storage import offer_effective_photo, offer_effective_price, offer_effective_title

    title = ""
    price = ""
    photo = ""
    subj_norm = normalized_reply_subject(subject)
    try:
        off = await resolve_offer_for_mail_card(
            session,
            user_id=int(user_id),
            from_email=_canon_email(seller_email),
            resolved_offer_id=None,
            subject=subject or "",
            from_name=from_name or "",
            body_text=body_text or "",
        )
        if off:
            title = (offer_effective_title(off) or "").strip()
            price = _format_chf_price(offer_effective_price(off, default=""))
            photo = (offer_effective_photo(off) or "").strip()
        elif subject_is_informative(subject) and subj_norm:
            title = subj_norm
    except Exception:
        pass

    return {
        "ITEM_TITLE": title,
        "PRICE": price,
        "IMAGE_URL": photo,
        "SELLER_EMAIL": _canon_email(seller_email),
        "LINK": (link or "").strip(),
    }


async def resolve_gag_link_for_reply(
    session,
    user_id: int,
    *,
    account_email: str,
    seller_email: str,
    mail_generated_link: str | None = None,
) -> str:
    """GAG-ссылка из ConversationLink или из письма после «Создать ссылку»."""
    from sqlalchemy import select

    from models import ConversationLink

    link = (mail_generated_link or "").strip()
    if link:
        return link

    inbox = _canon_email(account_email)
    seller = _canon_email(seller_email)
    if inbox and seller:
        conv = (
            await session.execute(
                select(ConversationLink)
                .where(ConversationLink.user_id == int(user_id))
                .where(ConversationLink.account_email == inbox)
                .where(ConversationLink.from_email == seller)
            )
        ).scalar_one_or_none()
        if conv and conv.generated_link:
            return str(conv.generated_link).strip()
    return ""
