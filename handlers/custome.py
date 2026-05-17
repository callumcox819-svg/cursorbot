# handlers/custome.py
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from pathlib import Path

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from database import Session
from models import EmailAccount, Offer, OfferEmail, UserSetting, IncomingMail
from services.smtp_proxy_send import send_email_via_account_with_proxy
from services.users import get_or_create_user
from services.user_settings import get_user_setting
from services.gag_keys import gag_service_for_html_dir, is_valid_gag_service
from services.html_templates import (
    list_html_templates_for_service,
    load_html_for_user,
    service_label_for_path,
)
from utils.bg_jobs import is_running as bg_is_running, start as bg_start

router = Router()

_SMTP_TIMEOUT = 25
logger = logging.getLogger(__name__)

HTML_DIR = Path("data/html")
HTML_CH_DIR = Path("data/HTMLch")
GAG_SERVICE_KEY = "gag_service"


HTML_NICK_KEY = "html_nick"
HTML_SIGNATURE_KEY = "html_signature"
HTML_SUBJECT_KEY = "html_subject_theme"


def _html_nick_key_for_service(service: str) -> str:
    service = (service or "").strip()
    return f"html_nick_{service}" if service else HTML_NICK_KEY


def _parse_uid(uid: str) -> int | None:
    try:
        u = (uid or "").strip()
        if u.startswith("S:"):
            u = u.split(":", 1)[1]
        return int(u)
    except Exception:
        return None


async def _load_incoming_meta(session: Session, acc_id: int, uid: str) -> dict | None:
    """Load incoming mail meta from DB so buttons keep working after redeploy."""
    uid_num = _parse_uid(uid)
    if uid_num is None:
        return None
    m = (
        await session.execute(
            select(IncomingMail)
            .where(IncomingMail.account_id == int(acc_id))
            .where(IncomingMail.imap_uid == int(uid_num))
            .order_by(IncomingMail.id.desc())
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


def _html_list_kb(files: list[str], acc_id: int, uid: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for fn in files[:40]:
        rows.append([InlineKeyboardButton(text=f"📄 {fn}", callback_data=f"cust_send_imap:{acc_id}:{uid}:{fn}")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data=f"cust_close:{acc_id}:{uid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("cust_open:"))
async def cust_open(callback: CallbackQuery):
    """Open custom HTML reply picker for an incoming email."""
    try:
        _, acc_id, uid = (callback.data or "").split(":", 2)
        acc_id = int(acc_id)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    async with Session() as session:
        meta = await _load_incoming_meta(session, acc_id, uid)
        if not meta:
            return await callback.answer("Письмо устарело", show_alert=True)

        user = await get_or_create_user(session, callback.from_user.id)
        raw_svc = (await get_user_setting(session, user, GAG_SERVICE_KEY) or "").strip()
        if not is_valid_gag_service(raw_svc):
            return await callback.answer(
                "Сначала выберите сервис: 👤 Профиль → 🧭 Выбор сервиса",
                show_alert=True,
            )
        files = list_html_templates_for_service(raw_svc)
        if not files:
            sub = gag_service_for_html_dir(raw_svc) or "?"
            return await callback.answer(
                f"Нет HTML в папке {sub}/",
                show_alert=True,
            )
        svc_label = service_label_for_path(gag_service_for_html_dir(raw_svc) or "")

    await callback.message.answer(
        f"🧩 <b>CUSTOME</b> · {svc_label}\n\nВыбери HTML-шаблон — ответ уйдёт на почту отправителя письма.",
        reply_markup=_html_list_kb(files, acc_id, uid),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cust_close:"))
async def cust_close(callback: CallbackQuery):
    await callback.answer("Ок")


async def _bg_cust_smtp(callback: CallbackQuery, coro_fn) -> bool:
    uid = callback.from_user.id
    try:
        await callback.answer("⏳ Отправляю…", show_alert=False)
    except Exception:
        pass
    if bg_is_running(uid, "smtp"):
        try:
            await callback.answer("⏳ Отправка уже идёт…", show_alert=True)
        except Exception:
            pass
        return False

    bot = callback.bot
    chat_id = callback.message.chat.id

    async def _job() -> None:
        try:
            ok, err = await asyncio.wait_for(coro_fn(), timeout=_SMTP_TIMEOUT + 10)
        except asyncio.TimeoutError:
            ok, err = False, "Timeout SMTP"
        except Exception as e:
            ok, err = False, str(e)
        if ok:
            await bot.send_message(chat_id, "✅ Отправлено.", parse_mode="HTML")
        else:
            await bot.send_message(chat_id, f"❌ Ошибка: <code>{_e(err)}</code>", parse_mode="HTML")

    if not bg_start(uid, "smtp", _job()):
        try:
            await callback.answer("⏳ Отправка уже идёт…", show_alert=True)
        except Exception:
            pass
        return False
    return True


@router.callback_query(F.data.startswith("cust_send_imap:"))
async def cust_send_imap(callback: CallbackQuery):
    try:
        _, acc_id, uid, filename = (callback.data or "").split(":", 3)
        acc_id = int(acc_id)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    tg_id = callback.from_user.id
    mail_uid = uid
    filename_copy = filename

    async def _send() -> tuple[bool, str | None]:
        async with Session() as session:
            meta = await _load_incoming_meta(session, acc_id, mail_uid)
            if not meta:
                return False, "Письмо устарело"
            to_email = (meta.get("from_email") or "").strip()
            if not to_email:
                return False, "Не найден email получателя"
            account = (
                await session.execute(select(EmailAccount).where(EmailAccount.id == int(acc_id)))
            ).scalars().first()
            if not account:
                return False, "Аккаунт не найден"
            user = await get_or_create_user(session, tg_id)
            from services.html_reply import (
                build_offer_html_ctx,
                get_html_reply_subject,
                get_html_sender_name,
                prepare_html_body,
                resolve_gag_link_for_reply,
            )
            from services.placeholders import apply_placeholders

            subject = await get_html_reply_subject(
                session,
                user,
                fallback=_normalize_subject({"subject": meta.get("subject") or ""}),
            )
            sender_name = await get_html_sender_name(session, user)
            html_signature = (
                await session.execute(
                    select(UserSetting.value)
                    .where(UserSetting.user_id == int(user.id))
                    .where(UserSetting.key == HTML_SIGNATURE_KEY)
                )
            ).scalar_one_or_none()
            raw_html, _sub, tpl_err = await load_html_for_user(
                session, user, gag_service_key=GAG_SERVICE_KEY, filename=filename_copy
            )
            if tpl_err or not raw_html:
                return False, tpl_err or "HTML шаблон не найден"
            account_email = (meta.get("account_email") or account.email or "").strip().lower()
            mail_gen_link = None
            try:
                uid_s = (mail_uid or "").strip()
                if uid_s.startswith("S:"):
                    uid_s = uid_s.split(":", 1)[1]
                uid_num = int(uid_s)
                mail_row = (
                    await session.execute(
                        select(IncomingMail)
                        .where(IncomingMail.account_id == int(acc_id))
                        .where(IncomingMail.imap_uid == int(uid_num))
                        .order_by(IncomingMail.id.desc())
                        .limit(1)
                    )
                ).scalars().first()
                if mail_row and mail_row.generated_link:
                    mail_gen_link = str(mail_row.generated_link)
            except Exception:
                pass
            link = await resolve_gag_link_for_reply(
                session,
                int(user.id),
                account_email=account_email,
                seller_email=to_email,
                mail_generated_link=mail_gen_link,
            )
            ctx = await build_offer_html_ctx(session, int(user.id), to_email, link=link)
            html_body = prepare_html_body(raw_html, session, user)
            if html_signature:
                html_body = html_body.replace("{{SIGNATURE}}", str(html_signature))
            html_body = apply_placeholders(html_body, link=link, ctx=ctx)
            return await send_email_via_account_with_proxy(
                session,
                int(user.id),
                account,
                to_email,
                subject,
                html_body,
                is_html=True,
                sender_name=sender_name,
            )

    if not await _bg_cust_smtp(callback, _send):
        return


def _e(s: str) -> str:
    return html.escape(s or "", quote=False)


def _normalize_subject(meta: dict) -> str:
    subject = (meta.get("subject") or "").strip()
    if not subject:
        return "Re:"
    subject = re.sub(r"^(re|aw|fw|fwd)\s*:\s*", "", subject, flags=re.I).strip()
    return f"Re: {subject}" if subject else "Re:"


@router.callback_query(F.data.startswith("cust_send_html:"))
async def cust_send_html(callback: CallbackQuery):
    try:
        _, offer_email_id, filename = (callback.data or "").split(":", 2)
        offer_email_id = int(offer_email_id)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    tg_id = callback.from_user.id
    filename_copy = filename

    async def _send() -> tuple[bool, str | None]:
        async with Session() as session:
            offer_email = (
                await session.execute(
                    select(OfferEmail).where(OfferEmail.id == offer_email_id)
                )
            ).scalars().first()
            if not offer_email:
                return False, "Email не найден"
            account = (
                await session.execute(
                    select(EmailAccount).where(EmailAccount.id == offer_email.account_id)
                )
            ).scalars().first()
            if not account:
                return False, "Аккаунт не найден"
            from models import User
            from services.html_reply import (
                build_offer_html_ctx,
                get_html_reply_subject,
                get_html_sender_name,
                prepare_html_body,
                resolve_gag_link_for_reply,
            )
            from services.placeholders import apply_placeholders

            user = (
                await session.execute(select(User).where(User.id == int(offer_email.user_id)))
            ).scalars().first()
            if not user:
                return False, "Пользователь не найден"
            sender_name = await get_html_sender_name(session, user)
            html_signature = (
                await session.execute(
                    select(UserSetting.value)
                    .where(UserSetting.user_id == offer_email.user_id)
                    .where(UserSetting.key == HTML_SIGNATURE_KEY)
                )
            ).scalar_one_or_none()
            meta = {}
            try:
                meta = json.loads(offer_email.meta or "{}")
            except Exception:
                pass
            subject = await get_html_reply_subject(session, user, fallback=_normalize_subject(meta))
            raw_html, _sub, tpl_err = await load_html_for_user(
                session, user, gag_service_key=GAG_SERVICE_KEY, filename=filename_copy
            )
            if tpl_err or not raw_html:
                return False, tpl_err or "HTML шаблон не найден"
            to_email = (offer_email.email or "").strip().lower()
            link = await resolve_gag_link_for_reply(
                session,
                int(user.id),
                account_email=(account.email or "").strip().lower(),
                seller_email=to_email,
                mail_generated_link=(meta.get("generated_link") if isinstance(meta, dict) else None),
            )
            ctx = await build_offer_html_ctx(session, int(user.id), to_email, link=link)
            html_body = prepare_html_body(raw_html, session, user)
            if html_signature:
                html_body = html_body.replace("{{SIGNATURE}}", html_signature)
            html_body = apply_placeholders(html_body, link=link, ctx=ctx)
            return await send_email_via_account_with_proxy(
                session,
                int(user.id),
                account,
                offer_email.email,
                subject,
                html_body,
                is_html=True,
                sender_name=sender_name,
            )

    if not await _bg_cust_smtp(callback, _send):
        return
