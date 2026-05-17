# handlers/mail_templates.py
from __future__ import annotations

import logging
from typing import List

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from sqlalchemy import select

from database import Session
from models import EmailAccount, User

from services.incoming_mail_worker import FULL_META
from services.smtp_proxy_send import send_email_via_account_with_proxy
from handlers.templates import load_templates, TemplateItem
from handlers.incoming_mail import _bg_incoming_smtp, _reply_notify_from_state
from models import IncomingMail

router = Router()
logger = logging.getLogger(__name__)


def _templates_kb(items: List[TemplateItem], acc_id: int, uid: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []

    # UI requirement: this screen is "Отправить пресет" and must show user templates.
    # We display them as numbered presets (as in the reference UX).
    for i, t in enumerate(items[:30]):
        label = (t.title or f"Пресет #{i + 1}").strip()[:40]
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"mail_tmpl_send:{i}:{acc_id}:{uid}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="Скрыть", callback_data=f"mail_tmpl_close:{acc_id}:{uid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _safe_re_subject(subject: str) -> str:
    s = (subject or "").strip()
    if not s:
        return "Re: message"
    sl = s.lower()
    if sl.startswith("re:"):
        return s
    return f"Re: {s}"


def _render_subject_with_offer(subject_template: str, offer_title: str) -> str:
    tpl = (subject_template or "").strip() or "Re: OFFER"
    offer_value = (offer_title or "").strip() or "OFFER"
    out = tpl.replace("{{OFFER}}", offer_value).replace("OFFER", offer_value).strip()
    if not out:
        out = offer_value
    if len(out) > 140:
        out = out[:137] + "…"
    return out


def _parse_uid(uid: str) -> int | None:
    """IncomingMail stores imap_uid as int. UID in callbacks can be like '123' or 'S:123'."""
    try:
        u = (uid or "").strip()
        if u.startswith("S:"):
            u = u.split(":", 1)[1]
        return int(u)
    except Exception:
        return None


async def _load_meta_from_db(acc_id: int, uid: str) -> dict | None:
    """Fallback for callbacks after redeploy (FULL_META is in-memory and gets cleared)."""
    uid_num = _parse_uid(uid)
    if uid_num is None:
        return None

    async with Session() as session:
        m = (
            await session.execute(
                select(IncomingMail)
                .where(IncomingMail.account_id == int(acc_id))
                .where(IncomingMail.imap_uid == int(uid_num))
                .limit(1)
            )
        ).scalars().first()

    if not m:
        return None

    return {
        "from_email": (m.from_email or "").strip(),
        "from_name": (m.from_name or "").strip(),
        "subject": m.subject or "",
        "account_email": (m.account_email or "").strip(),
        "date_str": m.date_str or "",
    }


 


@router.callback_query(F.data.startswith("mail_tmpl_open:"))
async def mail_tmpl_open(callback: CallbackQuery):
    try:
        _, acc_id, uid = (callback.data or "").split(":", 2)
        acc_id = int(acc_id)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    meta = FULL_META.get((acc_id, uid)) or await _load_meta_from_db(acc_id, uid)
    if not meta:
        return await callback.answer("Письмо устарело", show_alert=True)

    items = await load_templates(callback.from_user.id)
    if not items:
        return await callback.answer("Нет шаблонов. Добавь их в ⚡ Шаблоны", show_alert=True)

    text = "Нажмите на пресет для отправки"
    await callback.message.answer(text, reply_markup=_templates_kb(items, acc_id, uid), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("mail_tmpl_close:"))
async def mail_tmpl_close(callback: CallbackQuery):
    await callback.answer("Ок")


@router.callback_query(F.data.startswith("mail_tmpl_send:"))
async def mail_tmpl_send(callback: CallbackQuery, state: FSMContext):
    try:
        _, idx, acc_id, uid = (callback.data or "").split(":", 3)
        idx = int(idx)
        acc_id = int(acc_id)
    except Exception:
        return await callback.answer("Неверный формат", show_alert=True)

    meta = FULL_META.get((acc_id, uid)) or await _load_meta_from_db(acc_id, uid)
    if not meta:
        return await callback.answer("Письмо устарело", show_alert=True)

    to_email = (meta.get("from_email") or "").strip()
    if not to_email:
        return await callback.answer("Не найден email получателя", show_alert=True)

    items = await load_templates(callback.from_user.id)
    if not items:
        return await callback.answer("Нет шаблонов. Добавь их в ⚡ Шаблоны", show_alert=True)

    if idx < 0 or idx >= len(items):
        return await callback.answer("Шаблон не найден", show_alert=True)

    tpl = items[idx]
    body = (tpl.text or "").strip()
    if not body:
        return await callback.answer("Пустой шаблон", show_alert=True)

    subject_orig = (meta.get("subject") or "").strip()
    subject = _safe_re_subject(subject_orig)
    tg_id = callback.from_user.id
    body_copy = body

    async def _send() -> tuple[bool, str | None]:
        async with Session() as session:
            acc = (await session.execute(select(EmailAccount).where(EmailAccount.id == acc_id))).scalars().first()
            if not acc:
                return False, "SMTP аккаунт не найден"
            user = (
                await session.execute(select(User).where(User.telegram_id == int(tg_id)))
            ).scalars().first()
            if not user:
                return False, "Пользователь не найден"
            out_subject = subject
            try:
                from services.user_settings import get_user_setting

                subj_insert = str(await get_user_setting(session, user, "subj_insert") or "").strip().lower() in {
                    "1", "true", "yes", "on",
                }
                if subj_insert:
                    uid_num = _parse_uid(uid)
                    offer_title = ""
                    if uid_num is not None:
                        mrow = (
                            await session.execute(
                                select(IncomingMail)
                                .where(IncomingMail.account_id == int(acc_id))
                                .where(IncomingMail.imap_uid == int(uid_num))
                                .limit(1)
                            )
                        ).scalars().first()
                        if mrow and getattr(mrow, "resolved_offer_id", None):
                            from models import Offer

                            off = (
                                await session.execute(
                                    select(Offer).where(Offer.id == int(mrow.resolved_offer_id)).limit(1)
                                )
                            ).scalars().first()
                            if off:
                                offer_title = (off.title or "").strip()
                    tpl = str(await get_user_setting(session, user, "subject_template") or "Re: OFFER")
                    out_subject = _render_subject_with_offer(tpl, offer_title)
            except Exception:
                pass
            return await send_email_via_account_with_proxy(
                session,
                int(user.id),
                acc,
                to_email,
                out_subject,
                body_copy,
                sender_name=getattr(user, "sender_name", None),
            )

    data = await state.get_data()
    notify = _reply_notify_from_state(
        data,
        body_text=body,
        is_preset=True,
        extra_cleanup=[callback.message.message_id],
    )
    await state.clear()
    if not await _bg_incoming_smtp(callback, tg_id, _send, notify=notify):
        return
    logger.info("MAIL_TEMPLATE queued to=%s acc_id=%s uid=%s idx=%s", to_email, acc_id, uid, idx)
