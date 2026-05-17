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
from utils.bg_jobs import is_running as bg_is_running, start as bg_start

router = Router()

_SMTP_TIMEOUT = 25
logger = logging.getLogger(__name__)

HTML_DIR = Path("data/html")
HTML_CH_DIR = Path("data/HTMLch")
GAG_SERVICE_KEY = "gag_service"


async def _get_html_dir(session: Session, tg_user_id: int) -> Path:
    user = await get_or_create_user(session, tg_user_id)
    service = (await get_user_setting(session, user, GAG_SERVICE_KEY) or "").strip()
    if service:
        sub = HTML_CH_DIR / service
        if sub.exists():
            return sub
    return HTML_CH_DIR if HTML_CH_DIR.exists() else HTML_DIR


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

        html_dir = await _get_html_dir(session, callback.from_user.id)
        files = sorted([p.name for p in html_dir.glob("*.html")])
        if not files:
            return await callback.answer("Нет HTML-шаблонов", show_alert=True)

    await callback.message.answer(
        "🧩 <b>CUSTOME</b>\n\nВыбери HTML-шаблон — я отправлю ответ на почту отправителя этого письма.",
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
            from services.html_reply import get_html_reply_subject, get_html_sender_name, prepare_html_body

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
            html_dir = await _get_html_dir(session, tg_id)
            file_path = html_dir / filename_copy
            if not file_path.exists():
                file_path = HTML_DIR / filename_copy
            if not file_path.exists():
                return False, "HTML шаблон не найден"
            raw_html = file_path.read_text(encoding="utf-8", errors="ignore")
            html_body = prepare_html_body(raw_html, session, user)
            if html_signature:
                html_body = html_body.replace("{{SIGNATURE}}", str(html_signature))
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
            from services.html_reply import get_html_reply_subject, get_html_sender_name, prepare_html_body

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
            html_dir = await _get_html_dir(session, tg_id)
            file_path = html_dir / filename_copy
            if not file_path.exists():
                file_path = HTML_DIR / filename_copy
            if not file_path.exists():
                return False, "HTML шаблон не найден"
            raw_html = file_path.read_text(encoding="utf-8", errors="ignore")
            html_body = prepare_html_body(raw_html, session, user)
            if html_signature:
                html_body = html_body.replace("{{SIGNATURE}}", html_signature)
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
