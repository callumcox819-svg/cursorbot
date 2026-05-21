# handlers/incoming_mail.py
from __future__ import annotations

import asyncio
import html
import logging
import os
import re

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field

from aiogram import Router, F
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from sqlalchemy import select as sa_select, func, select, update as sa_update

from database import Session
from models import (
    EmailAccount,
    ConversationLink,
    Offer,
    OfferEmail,
    IncomingMail,
    User,
    UserSetting,
    QuickTemplate,
)

from services.users import get_or_create_user
from services.user_settings import get_user_setting
from services.gag_keys import (
    gag_default_version,
    gag_generate_endpoint,
    gag_service_for_api,
    gag_service_for_html_dir,
    gag_service_from_offer_link,
    get_user_gag_api_key,
    is_valid_gag_service,
)
from services.gag_network import generate_gag_url, GAGError
from services.incoming_mail_worker import (
    FULL_META,
    _try_pin,
    build_mail_card_from_mail,
    resolve_offer_for_mail_card,
)
from services.offer_matching import resolve_offer_for_incoming
from services.offer_storage import offer_effective_photo, offer_effective_price, offer_effective_title
from services.smtp_proxy_send import send_email_via_account_with_proxy
from services.translate import translate_to_ru, _strip_html

# Email reply "presets" must use the same storage/UI as ⚡ Шаблоны (handlers/templates.py)
from handlers.templates import load_templates, TemplateItem
from utils.bg_jobs import is_running as bg_is_running, start as bg_start

router = Router()

def _incoming_smtp_wait_sec() -> int:
    from services.smtp_proxy_send import REPLY_SMTP_MAX_PROXIES, REPLY_SMTP_TIMEOUT_SEC

    default = REPLY_SMTP_MAX_PROXIES * REPLY_SMTP_TIMEOUT_SEC + 20
    return max(45, int(os.getenv("INCOMING_SMTP_TIMEOUT_SEC", str(default))))
REPLY_CHOICE_TEXT = "Выберите вариант"

COUNTRY_KEY = "country"
GAG_PROFILE_TITLE_KEY = "gag_profile_title"
GAG_PROFILE_NAME_KEY = "gag_profile_name"
GAG_PROFILE_ADDRESS_KEY = "gag_profile_address"
GAG_SERVICE_KEY = "gag_service"
HTML_NICK_KEY = "html_nick"
HTML_SIGNATURE_KEY = "html_signature"
HTML_SUBJECT_KEY = "html_subject_theme"


@dataclass
class ReplyNotifyCtx:
    anchor_message_id: int
    to_email: str
    account_email: str
    incoming_from: str
    body_text: str
    is_preset: bool = False
    is_html: bool = False
    is_link: bool = False
    inbox_label: str | None = None
    html_attachment: str | None = None
    html_filename: str | None = None
    cleanup_message_ids: list[int] = field(default_factory=list)


def _preview_reply_body(body: str, *, is_html: bool = False, max_len: int = 220) -> str:
    t = (body or "").strip()
    if is_html:
        t = _strip_html(t) or "HTML-письмо"
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_len:
        return t[: max_len - 1] + "…"
    return t


async def _delete_message_safe(bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
    except Exception:
        pass


def _resolve_mail_anchor(
    acc_id: int,
    uid: str,
    meta: dict | None,
    callback_message: Message | None,
) -> int | None:
    """ID карточки входящего в Telegram (для reply_to)."""
    if callback_message and getattr(callback_message, "message_id", None):
        return int(callback_message.message_id)
    m = meta or {}
    anchor = m.get("tg_card_message_id")
    if anchor:
        return int(anchor)
    fm = FULL_META.get((int(acc_id), str(uid))) or {}
    anchor = fm.get("tg_card_message_id")
    if anchor:
        return int(anchor)
    return None


async def _notify_reply_sent(bot, chat_id: int, ctx: ReplyNotifyCtx) -> None:
    for mid in ctx.cleanup_message_ids:
        await _delete_message_safe(bot, chat_id, mid)

    from_acc = _e(ctx.account_email or "—")
    to_addr = _e(ctx.to_email or "—")
    anchor = int(ctx.anchor_message_id)

    if ctx.is_html:
        body_part = "<b>[HTML]</b>"
    else:
        body_part = f"<b>{_e(_preview_reply_body(ctx.body_text, is_html=False))}</b>"

    main = (
        f"⚡️ Ответ {body_part} успешно отправлен на <code>{to_addr}</code> "
        f"с аккаунта <code>{from_acc}</code> ⚡️"
    )

    try:
        await bot.send_message(
            int(chat_id),
            main,
            parse_mode="HTML",
            reply_to_message_id=anchor,
        )
    except Exception:
        await bot.send_message(int(chat_id), main, parse_mode="HTML")

    if ctx.is_html and ctx.html_attachment:
        fname = (ctx.html_filename or "reply.html").strip() or "reply.html"
        try:
            doc = BufferedInputFile(
                ctx.html_attachment.encode("utf-8"),
                filename=fname,
            )
            await bot.send_document(
                int(chat_id),
                doc,
                caption="📄 HTML, который был отправлен",
                reply_to_message_id=anchor,
            )
        except Exception:
            pass

    footer: str | None = None
    if ctx.is_preset:
        footer = "✅ Пресет отправлен"
    elif ctx.is_html:
        footer = "✅ HTML отправлен"
    elif ctx.is_link:
        footer = "✅ Ссылка создана"

    if footer:
        try:
            await bot.send_message(
                int(chat_id),
                footer,
                reply_to_message_id=anchor,
            )
        except Exception:
            await bot.send_message(int(chat_id), footer)


def _reply_notify_from_state(
    data: dict,
    *,
    body_text: str,
    is_preset: bool = False,
    is_html: bool = False,
    extra_cleanup: list[int] | None = None,
    meta: dict | None = None,
    acc_id: int | None = None,
    uid: str | None = None,
) -> ReplyNotifyCtx | None:
    return _reply_notify_build(
        acc_id=int(acc_id or data.get("acc_id") or 0),
        uid=str(uid or data.get("uid") or ""),
        meta=meta or {},
        state_data=data,
        body_text=body_text,
        is_preset=is_preset,
        is_html=is_html,
        extra_cleanup=extra_cleanup,
    )


def _reply_notify_build(
    *,
    acc_id: int,
    uid: str,
    meta: dict,
    state_data: dict,
    body_text: str,
    is_preset: bool = False,
    is_html: bool = False,
    extra_cleanup: list[int] | None = None,
) -> ReplyNotifyCtx | None:
    """Собрать уведомление об ответе: FSM + FULL_META (переживает рестарт/деплой)."""
    anchor = state_data.get("anchor_message_id") or meta.get("tg_card_message_id")
    if not anchor and acc_id and uid:
        anchor = (FULL_META.get((int(acc_id), str(uid))) or {}).get("tg_card_message_id")
    if not anchor:
        return None

    to_email = (
        state_data.get("to_email")
        or meta.get("from_email")
        or (FULL_META.get((int(acc_id), str(uid))) or {}).get("from_email")
        or ""
    )
    account_email = (
        state_data.get("account_email")
        or meta.get("account_email")
        or (FULL_META.get((int(acc_id), str(uid))) or {}).get("account_email")
        or ""
    )

    cleanup: list[int] = []
    ui = state_data.get("ui_message_id")
    if ui:
        cleanup.append(int(ui))
    if extra_cleanup:
        for mid in extra_cleanup:
            if mid and int(mid) not in cleanup:
                cleanup.append(int(mid))

    return ReplyNotifyCtx(
        anchor_message_id=int(anchor),
        to_email=_canon_email(str(to_email)),
        account_email=_canon_email(str(account_email)),
        incoming_from=_canon_email(str(to_email)),
        body_text=body_text,
        is_preset=is_preset,
        is_html=is_html,
        inbox_label=(state_data.get("inbox_label") or "").strip() or None,
        cleanup_message_ids=cleanup,
    )


async def _reply_notify_build_async(
    *,
    acc_id: int,
    uid: str,
    meta: dict,
    state_data: dict,
    body_text: str,
    is_preset: bool = False,
    is_html: bool = False,
    extra_cleanup: list[int] | None = None,
    user_id: int | None = None,
) -> ReplyNotifyCtx | None:
    """Как _reply_notify_build + fallback на ConversationLink.tg_message_id."""
    merged_state = dict(state_data)
    if acc_id and uid:
        try:
            async with Session() as session:
                to_e, subj, acc_e = await _resolve_reply_recipient(
                    session,
                    int(acc_id),
                    str(uid),
                    meta=meta,
                    state_data=state_data,
                )
            if to_e:
                merged_state.setdefault("to_email", to_e)
            if subj:
                merged_state.setdefault("subject", subj)
            if acc_e:
                merged_state.setdefault("account_email", acc_e)
        except Exception:
            pass

    ctx = _reply_notify_build(
        acc_id=acc_id,
        uid=uid,
        meta=meta,
        state_data=merged_state,
        body_text=body_text,
        is_preset=is_preset,
        is_html=is_html,
        extra_cleanup=extra_cleanup,
    )
    if ctx:
        return ctx

    inbox = _canon_email(str(meta.get("account_email") or state_data.get("account_email") or ""))
    contact = _canon_email(str(meta.get("from_email") or state_data.get("to_email") or ""))
    if not user_id or not inbox or not contact:
        return None

    try:
        async with Session() as session:
            conv = await _load_convlink_for_reply(session, user_id=int(user_id), inbox_email=inbox, contact_email=contact)
        if conv and getattr(conv, "tg_message_id", None):
            return ReplyNotifyCtx(
                anchor_message_id=int(conv.tg_message_id),
                to_email=contact,
                account_email=inbox,
                incoming_from=contact,
                body_text=body_text,
                is_preset=is_preset,
                is_html=is_html,
                cleanup_message_ids=list(extra_cleanup or []),
            )
    except Exception:
        pass
    return None


async def _load_convlink_for_reply(session, *, user_id: int, inbox_email: str, contact_email: str):
    return (
        await session.execute(
            sa_select(ConversationLink)
            .where(ConversationLink.user_id == int(user_id))
            .where(func.lower(ConversationLink.account_email) == inbox_email.lower())
            .where(func.lower(ConversationLink.from_email) == contact_email.lower())
            .limit(1)
        )
    ).scalars().first()


async def _bg_incoming_smtp(
    callback: CallbackQuery,
    user_id: int,
    coro_fn,
    *,
    notify: ReplyNotifyCtx | None = None,
    notify_builder=None,
) -> bool:
    """SMTP в фоне — polling не блокируется."""
    try:
        await callback.answer("⏳ Отправляю…", show_alert=False)
    except Exception:
        pass
    if bg_is_running(user_id, "smtp"):
        try:
            await callback.answer("⏳ Отправка уже идёт…", show_alert=True)
        except Exception:
            pass
        return False

    chat_id = callback.message.chat.id
    bot = callback.bot

    async def _job() -> None:
        try:
            ok, err, _msgid = await asyncio.wait_for(
                coro_fn(), timeout=_incoming_smtp_wait_sec() + 10
            )
        except asyncio.TimeoutError:
            ok, err = False, (
                f"Timeout: SMTP отправка > {_incoming_smtp_wait_sec()}с "
                f"(прокси перебираются, подождите или уменьшите число прокси)"
            )
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        if ok:
            ctx = notify
            if notify_builder:
                try:
                    ctx = await notify_builder()
                except Exception:
                    pass
            if ctx:
                await _notify_reply_sent(bot, chat_id, ctx)
            else:
                await bot.send_message(chat_id, "✅ Отправлено.", parse_mode="HTML")
        else:
            err_s = _e(err or "unknown")
            await bot.send_message(chat_id, f"❌ Ошибка SMTP:\n<code>{err_s}</code>", parse_mode="HTML")

    if not bg_start(user_id, "smtp", _job()):
        try:
            await callback.answer("⏳ Отправка уже идёт…", show_alert=True)
        except Exception:
            pass
        return False
    return True


async def _bg_message_smtp(
    message: Message,
    user_id: int,
    coro_fn,
    *,
    notify: ReplyNotifyCtx | None = None,
    notify_builder=None,
) -> bool:
    """SMTP из текстового ответа — в фоне."""
    if bg_is_running(user_id, "smtp"):
        await message.answer("⏳ Отправка уже идёт…")
        return False

    chat_id = message.chat.id
    bot = message.bot

    async def _job() -> None:
        try:
            ok, err, _msgid = await asyncio.wait_for(
                coro_fn(), timeout=_incoming_smtp_wait_sec() + 10
            )
        except asyncio.TimeoutError:
            ok, err = False, (
                f"Timeout: SMTP отправка > {_incoming_smtp_wait_sec()}с "
                f"(прокси перебираются, подождите или уменьшите число прокси)"
            )
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        if ok:
            ctx = notify
            if notify_builder:
                try:
                    ctx = await notify_builder()
                except Exception:
                    pass
            if ctx:
                await _notify_reply_sent(bot, chat_id, ctx)
            else:
                await bot.send_message(chat_id, "✅ Отправлено.", parse_mode="HTML")
        else:
            err_s = _e(err or "unknown")
            await bot.send_message(chat_id, f"❌ Ошибка SMTP:\n<code>{err_s}</code>", parse_mode="HTML")

    if not bg_start(user_id, "smtp", _job()):
        await message.answer("⏳ Отправка уже идёт…")
        return False
    return True


class _MailReplyState(StatesGroup):
    # after clicking "Написать ещё" we show a choice menu (preset / HTML / manual)
    waiting_choice = State()
    waiting_text = State()
    waiting_custom_html = State()


def _kb_reply_choice(acc_id: int, uid: str):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📄 Отправить пресет",
                    callback_data=f"mail_reply_mode:preset:{acc_id}:{uid}",
                ),
                InlineKeyboardButton(
                    text="🧩 Отправить HTML",
                    callback_data=f"mail_reply_mode:html:{acc_id}:{uid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Отмена",
                    callback_data=f"mail_reply_mode:cancel:{acc_id}:{uid}",
                )
            ],
        ]
    )


def _kb_preset_pick(items: list[TemplateItem], acc_id: int, uid: str):
    """Picker for reply-presets.

    IMPORTANT (per TZ): must use the same presets as ⚡ Шаблоны.
    We reuse handlers/templates.py storage and send via the existing mail_tmpl_send handler.
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    rows: list[list[InlineKeyboardButton]] = []

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

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"mail_reply_mode:back:{acc_id}:{uid}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_html_pick(acc_id: int, uid: str):
    """HTML picker (strict TZ): only GO / PUSH / SMS / BACK."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟢 GO", callback_data=f"mail_reply_html:go:{acc_id}:{uid}")],
            [InlineKeyboardButton(text="📣 PUSH", callback_data=f"mail_reply_html:push:{acc_id}:{uid}")],
            [InlineKeyboardButton(text="💬 SMS", callback_data=f"mail_reply_html:sms:{acc_id}:{uid}")],
            [InlineKeyboardButton(text="🔙 BACK", callback_data=f"mail_reply_html:back:{acc_id}:{uid}")],
            [InlineKeyboardButton(text="🚫 Отмена", callback_data=f"mail_reply_mode:back:{acc_id}:{uid}")],
        ]
    )


@router.callback_query(F.data.startswith("mail_hide:"))
async def cb_mail_hide(callback: CallbackQuery):
    """Скрыть карточку письма (UI как в референс-видео).

    Безопасное поведение:
    - пытаемся снять pin (если был)
    - удаляем сообщение с карточкой
    """
    try:
        _, acc_id, uid = (callback.data or "").split(":", 2)
        int(acc_id)
        str(uid)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    try:
        # best-effort unpin + delete
        try:
            await callback.bot.unpin_chat_message(chat_id=callback.message.chat.id, message_id=callback.message.message_id)
        except Exception:
            pass
        await callback.message.delete()
    except Exception:
        # если нельзя удалить — просто убираем клавиатуру
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await callback.answer("Скрыто")


@router.callback_query(F.data.startswith("mail_ignore:"))
async def cb_mail_ignore(callback: CallbackQuery):
    """Отметить письмо как "не отвечать".

    В проекте авто-ответ запускается сразу после получения письма,
    поэтому здесь мы делаем честное и полезное действие:
    - убираем кнопки, чтобы не тыкали случайно
    - помечаем в FULL_META флагом ignored (на будущее/лог)
    """
    try:
        _, acc_id, uid = (callback.data or "").split(":", 2)
        acc_id_i = int(acc_id)
        uid_s = str(uid)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    try:
        meta = FULL_META.get((acc_id_i, uid_s)) or {}
        meta["ignored"] = True
        FULL_META[(acc_id_i, uid_s)] = meta
    except Exception:
        pass

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer("Ок")


def _canon_email(email: str) -> str:
    """Canonicalize email for robust matching.
    - lower/strip
    - for Gmail/Googlemail: remove dots in local-part, strip +tag, normalize domain to gmail.com
    - for others: strip +tag in local-part (common), keep domain
    """
    e = (email or "").strip().lower()
    if "@" not in e:
        return e
    local, domain = e.split("@", 1)
    local = local.strip()
    domain = domain.strip().lower()
    if "+" in local:
        local = local.split("+", 1)[0]
    if domain in ("googlemail.com", "gmail.com"):
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _parse_imap_uid_key(uid: str) -> int | None:
    uid_s = (uid or "").strip()
    if ":" in uid_s:
        uid_s = uid_s.rsplit(":", 1)[-1]
    try:
        return int(uid_s)
    except (TypeError, ValueError):
        return None


async def _load_incoming_mail_for_uid(session, acc_id: int, uid: str) -> IncomingMail | None:
    uid_num = _parse_imap_uid_key(uid)
    if uid_num is None:
        return None
    return (
        await session.execute(
            sa_select(IncomingMail)
            .where(IncomingMail.account_id == int(acc_id))
            .where(IncomingMail.imap_uid == int(uid_num))
            .limit(1)
        )
    ).scalars().first()


async def _resolve_reply_recipient(
    session,
    acc_id: int,
    uid: str,
    *,
    meta: dict | None = None,
    state_data: dict | None = None,
) -> tuple[str, str, str]:
    """
    Email получателя ответа (контакт), тема, наш ящик.
    Источник правды — IncomingMail в Postgres (переживает redeploy).
    """
    meta = meta or {}
    state_data = state_data or {}

    to_email = _canon_email(
        state_data.get("to_email") or meta.get("from_email") or ""
    )
    subject = (state_data.get("subject") or meta.get("subject") or "").strip()
    account_email = _canon_email(
        state_data.get("account_email") or meta.get("account_email") or ""
    )

    mail = await _load_incoming_mail_for_uid(session, acc_id, uid)
    if mail:
        to_email = _canon_email(mail.from_email or "") or to_email
        subject = (mail.subject or "").strip() or subject
        account_email = _canon_email(mail.account_email or "") or account_email

    if not account_email:
        acc = await session.get(EmailAccount, int(acc_id))
        if acc and acc.email:
            account_email = _canon_email(acc.email)

    if to_email:
        fm_key = (int(acc_id), str(uid))
        FULL_META[fm_key] = {
            **(FULL_META.get(fm_key) or {}),
            "from_email": to_email,
            "subject": subject,
            "account_email": account_email,
        }

    return to_email, subject, account_email


def _e(s: str) -> str:
    return html.escape(s or "", quote=False)


def _deploy_build_id() -> str:
    return (os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or "local")[:8]


def _format_aqua_link_error(err: BaseException) -> str:
    msg = str(err).strip()
    low = msg.lower()
    if "connection_lost" in low or "server disconnected" in low:
        return "сеть оборвалась при запросе к GAG — нажми «Создать ссылку» ещё раз"
    if isinstance(err, asyncio.TimeoutError) or "timeout" in low:
        return "таймаут GAG API — попробуй ещё раз через несколько секунд"
    return msg or err.__class__.__name__


def _clean(v: str | None) -> str:
    return (v or "").strip()


def _service_label_for_card(service_code: str) -> str:
    """Human-readable service label for the link card.

    IMPORTANT: used only for UI rendering (per TZ).
    """
    sc = (service_code or "").strip().lower()

    # CH (GAG)
    if sc in {"ricardo_ch", "ricardo.ch"}:
        return "ricardo.ch"
    if sc in {"tutti_ch", "tutti.ch"}:
        return "tutti.ch"
    if sc in {"post_ch", "posta_ch", "post.ch"}:
        return "post.ch"

    # DE (GOO)
    if sc in {"ebay_de", "kleinanzeigen", "kleinanzeigen_de", "kleinanzeigen.de"}:
        # The project uses goo service code "ebay_de" for Kleinanzeigen.
        return "kleinanzeigen.de"

    # FB (inbox)
    if sc in {"facebook", "facebook.com"}:
        return "facebook.com"

    return service_code or "—"


async def _send_generated_link_card_to_chat(
    bot,
    chat_id: int,
    *,
    offer_title: str | None,
    offer_price: str | None,
    photo_url: str | None,
    profile_display: str | None,
    service_code: str,
    link: str,
    offer_id: int | None = None,
    anchor_message_id: int | None = None,
    account_email: str | None = None,
    contact_email: str | None = None,
    inbox_label: str | None = None,
):
    """Карточка GAG-ссылки — reply к исходному письму, те же поля что на карточке письма."""
    service_label = _service_label_for_card(service_code)
    reply_to = int(anchor_message_id) if anchor_message_id else None

    card_text = (
        f"📣 <b>Объявления » {_e(service_label)}</b>\n\n"
        f"📌 <b>Название:</b> {_e((offer_title or '').strip()) or '—'}\n"
        f"💰 <b>Цена:</b> {_e((offer_price or '').strip()) or '—'}\n"
        f"👤 <b>Профиль:</b> <code>{_e((profile_display or '').strip()) or '—'}</code>\n\n"
        f"🔗 <b>Ссылка:</b>\n{_e(link)}"
    )

    price_kb = None
    if offer_id:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        price_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="💶 Цена", callback_data=f"offer_price:{offer_id}")]]
        )

    p = (photo_url or "").strip()
    if not p:
        await bot.send_message(
            chat_id,
            card_text + "\n\n<i>Фото объявления не найдено в БД.</i>",
            parse_mode="HTML",
            reply_markup=price_kb,
            reply_to_message_id=reply_to,
        )
    else:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=p,
                caption=card_text,
                parse_mode="HTML",
                reply_markup=price_kb,
                reply_to_message_id=reply_to,
            )
        except Exception:
            await bot.send_message(
                chat_id,
                card_text + "\n\n<i>Не удалось отправить фото объявления.</i>",
                parse_mode="HTML",
                reply_markup=price_kb,
                reply_to_message_id=reply_to,
            )

    if reply_to:
        try:
            await bot.send_message(
                chat_id,
                "✅ Ссылка создана",
                reply_to_message_id=reply_to,
            )
        except Exception:
            await bot.send_message(chat_id, "✅ Ссылка создана")
        try:
            await _try_pin(bot, chat_id, reply_to)
        except Exception:
            pass


async def _send_generated_link_card(
    *,
    callback: CallbackQuery,
    offer_title: str | None,
    offer_price: str | None,
    photo_url: str | None,
    profile_display: str | None,
    service_code: str,
    link: str,
    offer_id: int | None = None,
    anchor_message_id: int | None = None,
    account_email: str | None = None,
    contact_email: str | None = None,
    inbox_label: str | None = None,
):
    await _send_generated_link_card_to_chat(
        callback.bot,
        int(callback.message.chat.id),
        offer_title=offer_title,
        offer_price=offer_price,
        photo_url=photo_url,
        profile_display=profile_display,
        service_code=service_code,
        link=link,
        offer_id=offer_id,
        anchor_message_id=anchor_message_id,
        account_email=account_email,
        contact_email=contact_email,
        inbox_label=inbox_label,
    )


def _norm_subject_for_match(subject: str) -> str:
    """Normalize email subject for offer matching.

    Keep behavior predictable:
    - strip common reply/forward prefixes (re/aw/fw/fwd)
    - trim
    """
    s = (subject or "").strip()
    if not s:
        return ""
    return re.sub(r"^(re|aw|fw|fwd)\s*:\s*", "", s, flags=re.I).strip()


async def _offer_link_by_subject_or_name(
    session,
    *,
    user_id: int,
    subject: str,
    from_name: str,
) -> str | None:
    """Fallback: try to resolve Offer.link by subject/title or seller name.

    This is the same idea as in cb_create_goo_link (in-memory flow),
    but used for the DB-bound flow too.
    """
    if not user_id:
        return None

    subj = _norm_subject_for_match(subject)
    fn = (from_name or "").strip()

    # 1) by title-ish subject
    if subj:
        row_t = (
            await session.execute(
                sa_select(Offer.link)
                .where(Offer.user_id == int(user_id))
                .where(func.lower(Offer.title).like(f"%{subj.lower()}%"))
                .where(Offer.link.is_not(None))
                .order_by(Offer.id.desc())
                .limit(1)
            )
        ).first()
        if row_t and row_t[0]:
            return str(row_t[0]).strip()

        # Some subjects include extra words like "Verkaufe".
        # If the subject is long, also try a smaller token window.
        try:
            parts = [p for p in re.split(r"\s+", subj) if p]
            if len(parts) >= 4:
                tail = " ".join(parts[-4:]).strip()
                if tail and tail.lower() != subj.lower():
                    row_t2 = (
                        await session.execute(
                            sa_select(Offer.link)
                            .where(Offer.user_id == int(user_id))
                            .where(func.lower(Offer.title).like(f"%{tail.lower()}%"))
                            .where(Offer.link.is_not(None))
                            .order_by(Offer.id.desc())
                            .limit(1)
                        )
                    ).first()
                    if row_t2 and row_t2[0]:
                        return str(row_t2[0]).strip()
        except Exception:
            pass

    # 2) by seller name
    if fn:
        row_n = (
            await session.execute(
                sa_select(Offer.link)
                .where(Offer.user_id == int(user_id))
                .where(func.lower(Offer.person_name).like(f"%{fn.lower()}%"))
                .where(Offer.link.is_not(None))
                .order_by(Offer.id.desc())
                .limit(1)
            )
        ).first()
        if row_n and row_n[0]:
            return str(row_n[0]).strip()

    return None


async def _get_acc_owner_user_id(session, acc_id: int) -> int | None:
    acc = (
        await session.execute(
            sa_select(EmailAccount).where(EmailAccount.id == int(acc_id))
        )
    ).scalars().first()
    return int(acc.user_id) if acc else None


async def _get_convlink(
    session,
    *,
    user_id: int,
    inbox_email: str,
    contact_email: str,
) -> ConversationLink | None:
    """
    Связка диалога для конкретного ящика и контакта.

    В МОДЕЛИ ConversationLink поля:
      - account_email — наш почтовый ящик (куда пришло письмо)
      - from_email    — email отправителя (продавца)

    Здесь:
      inbox_email   -> пишем/сравниваем с account_email
      contact_email -> пишем/сравниваем с from_email
    """
    inbox = (inbox_email or "").strip().lower()
    contact = (contact_email or "").strip().lower()
    if not inbox or not contact:
        return None

    return (
        await session.execute(
            sa_select(ConversationLink)
            .where(ConversationLink.user_id == int(user_id))
            .where(ConversationLink.account_email == inbox)
            .where(ConversationLink.from_email == contact)
            .limit(1)
        )
    ).scalars().first()


async def _upsert_convlink(
    session,
    *,
    user_id: int,
    inbox_email: str,
    contact_email: str,
    ad_url: str | None = None,
    generated_link: str | None = None,
    pinned_offer_id: int | None = None,
) -> None:
    """
    Обновить/создать запись ConversationLink.

    В МОДЕЛИ ConversationLink поля:
      - account_email — наш почтовый ящик (куда пришло письмо)
      - from_email    — email отправителя (продавца)
    """
    inbox = (inbox_email or "").strip().lower()
    contact = (contact_email or "").strip().lower()
    if not inbox or not contact:
        return

    conv = await _get_convlink(
        session,
        user_id=user_id,
        inbox_email=inbox,
        contact_email=contact,
    )
    if not conv:
        conv = ConversationLink(
            user_id=int(user_id),
            account_email=inbox,
            from_email=contact,
            ad_url=(ad_url or "").strip() or None,
            generated_link=(generated_link or "").strip() or None,
        )
        session.add(conv)
    else:
        if ad_url:
            conv.ad_url = (ad_url or "").strip() or conv.ad_url
        if generated_link:
            conv.generated_link = (generated_link or "").strip() or conv.generated_link
        if pinned_offer_id:
            conv.pinned_offer_id = int(pinned_offer_id)
    await session.commit()


async def _resolve_gag_from_incoming_mail(
    session,
    *,
    user_id: int,
    from_email: str,
    subject: str,
    from_name: str = "",
    body_text: str = "",
    ad_url: str | None = None,
    resolved_offer_id: int | None = None,
    inbox_email: str | None = None,
) -> tuple[Offer | None, str]:
    """Один оффер и ad_url строго под это входящее письмо (как на карточке)."""
    from services.offer_matching import resolve_offer_for_aqua_link

    return await resolve_offer_for_aqua_link(
        session,
        user_id=int(user_id),
        from_email=from_email,
        subject=subject,
        from_name=from_name,
        body_text=body_text,
        ad_url=ad_url,
        resolved_offer_id=resolved_offer_id,
        inbox_email=inbox_email,
    )


async def _offer_link_by_sender_email(
    session,
    user_id: int,
    from_email: str,
    *,
    subject: str = "",
) -> str | None:
    """Offer.link по email отправителя; при нескольких лотах — по теме письма."""
    from services.offer_matching import offer_link_for_seller

    link = await offer_link_for_seller(
        session, user_id=int(user_id), from_email=from_email, subject=subject
    )
    if link:
        return link

    if not user_id or not from_email or "@" not in from_email:
        return None

    fe = from_email.strip().lower()
    local, domain = fe.split("@", 1)
    local = local.strip()
    domain = domain.strip().lower()

    # 1) exact
    row = (
        await session.execute(
            sa_select(Offer.link)
            .select_from(OfferEmail)
            .join(Offer, Offer.id == OfferEmail.offer_id)
            .where(Offer.user_id == int(user_id))
            .where(func.lower(OfferEmail.email) == fe)
            .where(Offer.link.is_not(None))
            .order_by(Offer.id.desc())
            .limit(1)
        )
    ).first()
    if row and row[0]:
        return str(row[0]).strip()

    # 1.5) Gmail: точки в local-part могут "исчезать" (gmail игнорирует '.')
    # пример: sorik.hajoyan@gmail.com -> sorikhajoyan@gmail.com
    if domain in ("gmail.com", "googlemail.com"):
        fe_nodot = fe.replace(".", "")
        row_g = (
            await session.execute(
                sa_select(Offer.link)
                .select_from(OfferEmail)
                .join(Offer, Offer.id == OfferEmail.offer_id)
                .where(Offer.user_id == int(user_id))
                # убираем точки у email в БД и у входящего email
                .where(func.replace(func.lower(OfferEmail.email), ".", "") == fe_nodot)
                .where(Offer.link.is_not(None))
                .order_by(Offer.id.desc())
                .limit(1)
            )
        ).first()
        if row_g and row_g[0]:
            return str(row_g[0]).strip()

    # 2) local-part match (first.last@domain -> first.last@ANY)
    # Для single-name у не-gmail почти всегда бесполезно, но тут не режем жестко,
    # чтобы не ломать нестандартные кейсы (и для gmail тоже).
    row2 = (
        await session.execute(
            sa_select(Offer.link)
            .select_from(OfferEmail)
            .join(Offer, Offer.id == OfferEmail.offer_id)
            .where(Offer.user_id == int(user_id))
            .where(func.lower(OfferEmail.email).like(local + "@%"))
            .where(Offer.link.is_not(None))
            .order_by(Offer.id.desc())
            .limit(1)
        )
    ).first()
    if row2 and row2[0]:
        return str(row2[0]).strip()

    # 3) python-side canonical fallback (handles gmail dots/+ and domain mismatches)
    try:
        fe_can = _canon_email(from_email)
        rows = (
            await session.execute(
                sa_select(OfferEmail.email, Offer.link)
                .select_from(OfferEmail)
                .join(Offer, Offer.id == OfferEmail.offer_id)
                .where(Offer.user_id == int(user_id))
                .where(Offer.link.is_not(None))
                .order_by(Offer.id.desc())
                .limit(800)
            )
        ).all()
        for em, lk in rows:
            if not lk:
                continue
            if _canon_email(em or "") == fe_can:
                return str(lk).strip()
    except Exception:
        pass

    return None


async def _offer_by_sender_email(session, user_id: int, from_email: str) -> tuple[Offer | None, int]:
    """Находит Offer по email отправителя.

    Для Gmail/Googlemail сравнение делается без точек в адресе,
    т.к. в реальности отправитель может ответить с варианта без точек.
    """
    fe = (from_email or "").strip().lower()
    if "@" not in fe:
        return None, 0

    domain = fe.split("@", 1)[1].strip().lower()
    if domain in ("gmail.com", "googlemail.com"):
        fe_nd = fe.replace(".", "")
        row = (
            await session.execute(
                select(Offer, func.count(OfferEmail.id))
                .join(OfferEmail, OfferEmail.offer_id == Offer.id)
                .where(Offer.user_id == user_id)
                .where(func.replace(func.lower(OfferEmail.email), ".", "") == fe_nd)
                .group_by(Offer.id)
                .limit(1)
            )
        ).first()
        if row:
            return row[0], int(row[1] or 0)
        return None, 0

    # обычный случай: точное совпадение
    row = (
        await session.execute(
            select(Offer, func.count(OfferEmail.id))
            .join(OfferEmail, OfferEmail.offer_id == Offer.id)
            .where(Offer.user_id == user_id)
            .where(func.lower(OfferEmail.email) == fe)
            .group_by(Offer.id)
            .limit(1)
        )
    ).first()
    if row:
        return row[0], int(row[1] or 0)
    return None, 0


def _extract_translation_from_card(text: str) -> str | None:
    try:
        m = re.search(
            r"(?is)<b>Перевод:</b>\s*<blockquote><code>(.*?)</code></blockquote>",
            text or "",
        )
        if m:
            return html.unescape(m.group(1)).strip()
    except Exception:
        pass
    return None


@router.callback_query(F.data.startswith("mail_translate:"))
async def cb_mail_translate(callback: CallbackQuery) -> None:
    try:
        _, mail_id_s = (callback.data or "").split(":", 1)
        mail_id = int(mail_id_s)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    async with Session() as session:
        mail = (
            await session.execute(
                sa_select(IncomingMail).where(IncomingMail.id == int(mail_id)).limit(1)
            )
        ).scalars().first()
        if not mail:
            return await callback.answer("Письмо не найдено в БД.", show_alert=True)

        body_full = (getattr(mail, "body", None) or "").strip()
        if not body_full:
            return await callback.answer("Нет текста для перевода.", show_alert=True)

        from services.incoming_mail_worker import _clean_mail_body_for_card

        shown = _clean_mail_body_for_card(body_full)
        shown = _strip_html(shown)
        if not shown:
            return await callback.answer("Нет текста для перевода.", show_alert=True)

        uid = callback.from_user.id
        if bg_is_running(uid, "translate"):
            return await callback.answer("⏳ Перевод уже выполняется…", show_alert=True)
        await callback.answer("Перевожу…", show_alert=False)

        mail_id_copy = int(mail.id)
        msg = callback.message
        bot = callback.bot

        async def _translate_job() -> None:
            translated = await translate_to_ru(shown, preserve_blocks=True)
            if not translated:
                try:
                    await bot.send_message(
                        msg.chat.id,
                        "❌ Не удалось перевести. Попробуйте позже.",
                        reply_to_message_id=msg.message_id,
                    )
                except Exception:
                    pass
                return
            async with Session() as session2:
                mail2 = (
                    await session2.execute(
                        sa_select(IncomingMail).where(IncomingMail.id == mail_id_copy).limit(1)
                    )
                ).scalars().first()
                if not mail2:
                    return
                new_text, new_kb = await build_mail_card_from_mail(
                    session2,
                    mail2,
                    translation=translated,
                )
            try:
                await msg.edit_text(
                    new_text,
                    reply_markup=new_kb,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                await bot.send_message(
                    msg.chat.id,
                    new_text,
                    reply_markup=new_kb,
                    parse_mode="HTML",
                    reply_to_message_id=msg.message_id,
                )

        if not bg_start(uid, "translate", _translate_job()):
            return await callback.answer("⏳ Перевод уже выполняется…", show_alert=True)
        return


@router.callback_query(F.data.startswith("mail_translate_stub:"))
async def cb_mail_translate_stub(callback: CallbackQuery) -> None:
    await callback.answer("Письмо устарело — дождитесь нового входящего.", show_alert=True)


@router.callback_query(F.data.startswith("mail_view:"))
async def cb_mail_view_legacy(callback: CallbackQuery) -> None:
    """Старые кнопки «Развернуть» — обновляем карточку на формат со стрелкой в тексте."""
    try:
        _, mail_id_s, _mode = (callback.data or "").split(":", 2)
        mail_id = int(mail_id_s)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    async with Session() as session:
        mail = (
            await session.execute(
                sa_select(IncomingMail).where(IncomingMail.id == int(mail_id)).limit(1)
            )
        ).scalars().first()
        if not mail:
            return await callback.answer("Письмо не найдено.", show_alert=True)
        cur = (callback.message.html_text or callback.message.text or "").strip()
        translation = _extract_translation_from_card(cur)
        new_text, new_kb = await build_mail_card_from_mail(session, mail, translation=translation)

    try:
        await callback.message.edit_text(
            new_text,
            reply_markup=new_kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("goo_mail:"))
async def cb_create_goo_link_from_db(callback: CallbackQuery):
    """Create GAG link for incoming mail stored in DB."""

    try:
        _, mail_id = (callback.data or "").split(":", 1)
        mail_id = int(mail_id)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    uid = callback.from_user.id
    if bg_is_running(uid, "aqua_link"):
        return await callback.answer("⏳ Ссылка уже создаётся…", show_alert=True)
    try:
        await callback.answer("⏳ Создаю ссылку…", show_alert=False)
    except Exception:
        pass

    async def _link_job() -> None:
        try:
            await _create_aqua_link_from_db_work(callback, mail_id)
        except Exception as e:
            logger.exception("aqua_link mail_id=%s failed", mail_id)
            err_msg = _format_aqua_link_error(e)
            try:
                await callback.bot.send_message(
                    int(callback.message.chat.id),
                    f"❌ Ошибка при создании ссылки: {_e(err_msg)[:400]}",
                )
            except Exception:
                pass

    if not bg_start(uid, "aqua_link", _link_job()):
        return await callback.answer("⏳ Ссылка уже создаётся…", show_alert=True)


async def _create_aqua_link_from_db_work(callback: CallbackQuery, mail_id: int) -> None:
    async with Session() as session:
        # Ensure the telegram user is the owner in our DB
        tg_user = await get_or_create_user(session, int(callback.from_user.id))

        mail = (
            await session.execute(
                sa_select(IncomingMail).where(IncomingMail.id == int(mail_id)).limit(1)
            )
        ).scalars().first()

        if not mail:
            return await callback.answer("Письмо не найдено в БД", show_alert=True)

        if int(mail.user_id) != int(tg_user.id):
            return await callback.answer("Нет доступа к этому письму", show_alert=True)

        acc_id = int(mail.account_id)
        inbox_email = _canon_email(mail.account_email or "")
        contact_email = _canon_email(mail.from_email or "")

        subj = (getattr(mail, "subject", "") or "").strip()
        from services.offer_matching import resolve_offer_for_aqua_link

        offer, url = await resolve_offer_for_aqua_link(
            session,
            user_id=int(tg_user.id),
            from_email=contact_email,
            subject=subj,
            from_name=(getattr(mail, "from_name", "") or ""),
            body_text=(getattr(mail, "body", "") or ""),
            ad_url=(getattr(mail, "ad_url", "") or "").strip() or None,
            resolved_offer_id=getattr(mail, "resolved_offer_id", None),
            resolved_offer_email_id=getattr(mail, "resolved_offer_email_id", None),
            inbox_email=inbox_email,
        )
        if offer:
            mail.resolved_offer_id = int(offer.id)
            mail.ad_url = url or mail.ad_url

        reasons: list[str] = []
        if not url:
            subj_hint = (subj or "")[:120]
            if offer:
                reasons.append(
                    f"лот найден (id {offer.id}), но нет ссылки item_link в БД — "
                    "перезалейте JSON с полем item_link"
                )
            else:
                from services.offer_storage import diagnose_subject_match
                from models import MailingSend

                diag = await diagnose_subject_match(
                    session, user_id=int(tg_user.id), subject=subj
                )
                mail_log_n = (
                    await session.execute(
                        sa_select(func.count(MailingSend.id))
                        .where(MailingSend.user_id == int(tg_user.id))
                        .where(func.lower(MailingSend.inbox_email) == inbox_email)
                    )
                ).scalar() or 0
                words = diag.get("words") or []
                w_hint = "+".join(words) if words else "—"
                reasons.append(
                    f"нет Offer в БД по теме «{subj_hint}» (лотов: {diag.get('total', 0)}, "
                    f"с словами {w_hint}: {diag.get('near', 0)})"
                )
                samples = diag.get("samples") or []
                if samples:
                    reasons.append("похожие названия: " + "; ".join(samples[:3]))
                reasons.append(
                    f"журнал рассылки с ящика {inbox_email or '—'}: {int(mail_log_n)} записей "
                    "(нужна хотя бы одна /send после деплоя — пишем offer_id+тему)"
                )
                reasons.append(
                    "или загрузите JSON с этим лотом и прогоните валидацию"
                )

        if not url:
            reason_text = "\n".join([f"• { _e(r) }" for r in (reasons or ["не удалось получить ссылку из БД"])])
            await callback.message.answer(
                "❌ <b>Не нашёл ссылку на объявление (ad_url)</b>\n\n"
                f"<b>from_email:</b> <code>{_e(contact_email) or '—'}</code>\n"
                f"<b>inbox:</b> <code>{_e(inbox_email) or '—'}</code>\n\n"
                "<b>Причины:</b>\n"
                f"{reason_text}\n\n"
                "По ТЗ ссылка берётся только из БД (Offer/OfferEmail/ConversationLink).\n"
                f"<i>build {(_deploy_build_id())}</i>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await callback.answer()
            return

        gag_key = await get_user_gag_api_key(session, tg_user)
        if not gag_key:
            await callback.message.answer(
                "❌ <b>GAG API ключ не установлен</b>\n\n"
                "Открой ⚙️ Настройки → 🔑 Ключ и вставь свой API-ключ команды.",
                parse_mode="HTML",
            )
            await callback.answer()
            return

        user_svc = (await get_user_setting(session, tg_user, GAG_SERVICE_KEY) or "").strip()
        service = gag_service_from_offer_link(url, user_fallback=user_svc) or ""
        if not is_valid_gag_service(service):
            await callback.message.answer(
                "❌ Не удалось определить сервис GAG по ссылке объявления. "
                "Проверьте Offer.link (ricardo/tutti/post) или выберите сервис в 👤 Профиль."
            )
            await callback.answer()
            return

        prof_title = (await get_user_setting(session, tg_user, GAG_PROFILE_TITLE_KEY) or "").strip()
        prof_name = (await get_user_setting(session, tg_user, GAG_PROFILE_NAME_KEY) or "").strip()
        prof_addr = (await get_user_setting(session, tg_user, GAG_PROFILE_ADDRESS_KEY) or "").strip()
        if not (prof_title and prof_name and prof_addr):
            await callback.message.answer(
                "❌ Профиль GAG не заполнен. Открой 👤 Профиль → ➕ Создать профиль."
            )
            await callback.answer()
            return

        offer_id = int(offer.id) if offer else None

        from services.offer_matching import normalized_reply_subject

        offer_title = offer_effective_title(offer) if offer else None
        offer_image = offer_effective_photo(offer) if offer else None
        title = offer_title or normalized_reply_subject(subj) or subj
        price = offer_effective_price(offer) if offer else ""
        if not title:
            await callback.message.answer("❌ Нет названия объявления (title).")
            await callback.answer()
            return

        api_service = gag_service_for_api(service)

        dom_raw = (await get_user_setting(session, tg_user, "gag_domain_slot") or "").strip()
        dom_slot = None
        try:
            if dom_raw and dom_raw.lower() not in {"team", "default", "0"}:
                dom_slot = int(dom_raw)
        except Exception:
            dom_slot = None
        api_domain = (dom_slot + 4) if dom_slot in (1, 2, 3, 4) else None

        user_db_id = int(tg_user.id)
        pinned_offer_id = int(offer.id) if offer else None
        inbox_label = (getattr(tg_user, "sender_name", None) or "").strip()
        mail_uid = str(getattr(mail, "imap_uid", "") or "")
        await session.commit()

    try:
        gag_url = await generate_gag_url(
            endpoint=gag_generate_endpoint(),
            apikey=gag_key,
            title=title,
            price=price,
            service=api_service,
            name=prof_name,
            address=prof_addr,
            image=offer_image,
            domain=api_domain,
            version=gag_default_version(),
        )
    except GAGError as e:
        await callback.message.answer(f"❌ GAG ошибка: {e}")
        await callback.answer()
        return
    except Exception as e:
        await callback.message.answer(f"❌ {_format_aqua_link_error(e)}")
        await callback.answer()
        return

    async with Session() as session:
        mail = (
            await session.execute(
                sa_select(IncomingMail).where(IncomingMail.id == int(mail_id)).limit(1)
            )
        ).scalars().first()
        if not mail:
            await callback.message.answer("❌ Письмо не найдено после генерации ссылки.")
            await callback.answer()
            return

        await _upsert_convlink(
            session,
            user_id=user_db_id,
            inbox_email=inbox_email,
            contact_email=contact_email,
            ad_url=url,
            generated_link=gag_url,
            pinned_offer_id=pinned_offer_id,
        )
        try:
            mail.generated_link = gag_url
        except Exception:
            pass
        await session.commit()

    meta_fm = FULL_META.get((acc_id, mail_uid)) or {}
    anchor = _resolve_mail_anchor(acc_id, mail_uid, meta_fm, callback.message)

    await _send_generated_link_card(
        callback=callback,
        offer_title=offer_title or title,
        offer_price=price,
        photo_url=offer_image,
        profile_display=prof_title,
        service_code=service,
        link=gag_url,
        offer_id=offer_id,
        anchor_message_id=anchor,
        account_email=inbox_email,
        contact_email=contact_email,
        inbox_label=inbox_label or None,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("goo_link:"))
async def cb_create_goo_link(callback: CallbackQuery):
    try:
        _, acc_id, uid = (callback.data or "").split(":", 2)
        acc_id = int(acc_id)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    meta = FULL_META.get((acc_id, uid))
    if not meta:
        return await callback.answer("Письмо устарело", show_alert=True)

    uid_tg = callback.from_user.id
    if bg_is_running(uid_tg, "aqua_link"):
        return await callback.answer("⏳ Ссылка уже создаётся…", show_alert=True)
    try:
        await callback.answer("⏳ Создаю ссылку…", show_alert=False)
    except Exception:
        pass

    async def _link_job() -> None:
        await _create_gag_link_work(callback, acc_id, uid, meta)

    if not bg_start(uid_tg, "aqua_link", _link_job()):
        return await callback.answer("⏳ Ссылка уже создаётся…", show_alert=True)


async def _create_gag_link_work(callback: CallbackQuery, acc_id: int, uid: str, meta: dict) -> None:
    inbox_email = _canon_email((meta.get("account_email") or ""))
    contact_email = _canon_email((meta.get("from_email") or ""))

    async with Session() as session:
        owner_user_id = await _get_acc_owner_user_id(session, acc_id)
        if not owner_user_id:
            return await callback.answer("Аккаунт не найден в БД", show_alert=True)

        mail_pre = (
            await session.execute(
                sa_select(IncomingMail)
                .where(IncomingMail.account_id == int(acc_id))
                .where(IncomingMail.imap_uid == int(uid))
                .where(IncomingMail.user_id == int(owner_user_id))
                .limit(1)
            )
        ).scalars().first()

        subj_pre = (getattr(mail_pre, "subject", "") or meta.get("subject") or "").strip()
        from services.offer_matching import resolve_offer_for_aqua_link

        offer_pre, url = await resolve_offer_for_aqua_link(
            session,
            user_id=int(owner_user_id),
            from_email=contact_email,
            subject=subj_pre,
            from_name=(getattr(mail_pre, "from_name", "") or meta.get("from_name") or "").strip(),
            body_text=(getattr(mail_pre, "body", "") or meta.get("body") or "").strip() if mail_pre else "",
            ad_url=(getattr(mail_pre, "ad_url", "") or "").strip() if mail_pre else None,
            resolved_offer_id=getattr(mail_pre, "resolved_offer_id", None) if mail_pre else None,
            inbox_email=inbox_email,
        )
        if offer_pre and mail_pre:
            mail_pre.resolved_offer_id = int(offer_pre.id)
            mail_pre.ad_url = url or mail_pre.ad_url

        reasons: list[str] = []
        if not url:
            subj_hint = (subj_pre or "")[:120]
            reasons = [
                f"нет Offer в БД по теме «{subj_hint}» и email продавца — "
                "проверьте, что JSON с этим лотом загружен и валидирован"
            ]

        if not url:
            reason_text = "\n".join([f"• { _e(r) }" for r in (reasons or ["не удалось получить ссылку из БД"])])
            await callback.message.answer(
                "❌ <b>Не нашёл ссылку на объявление (ad_url)</b>\n\n"
                f"<b>from_email:</b> <code>{_e(contact_email) or '—'}</code>\n"
                f"<b>inbox:</b> <code>{_e(inbox_email) or '—'}</code>\n\n"
                "<b>Причины:</b>\n"
                f"{reason_text}\n\n"
                "По ТЗ ссылка берётся только из БД (Offer/OfferEmail/ConversationLink).",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await callback.answer()
            return

        user = await get_or_create_user(session, int(callback.from_user.id))

        gag_key = await get_user_gag_api_key(session, user)
        if not gag_key:
            await callback.message.answer(
                "❌ <b>GAG API ключ не установлен</b>\n\n"
                "Открой ⚙️ Настройки → 🔑 Ключ и вставь свой API-ключ команды.",
                parse_mode="HTML",
            )
            return await callback.answer()

        user_svc = (await get_user_setting(session, user, GAG_SERVICE_KEY) or "").strip()
        service = gag_service_from_offer_link(url, user_fallback=user_svc) or ""
        if not is_valid_gag_service(service):
            await callback.message.answer(
                "❌ Не удалось определить сервис GAG по ссылке объявления. "
                "Проверьте Offer.link или выберите сервис в 👤 Профиль."
            )
            return await callback.answer()

        prof_title = (await get_user_setting(session, user, GAG_PROFILE_TITLE_KEY) or "").strip()
        prof_name = (await get_user_setting(session, user, GAG_PROFILE_NAME_KEY) or "").strip()
        prof_addr = (await get_user_setting(session, user, GAG_PROFILE_ADDRESS_KEY) or "").strip()
        if not (prof_title and prof_name and prof_addr):
            await callback.message.answer("❌ Профиль GAG не заполнен. Открой 👤 Профиль → ➕ Создать профиль.")
            return await callback.answer()

        mail = mail_pre
        offer = offer_pre
        offer_id = int(offer.id) if offer else None

        from services.offer_matching import normalized_reply_subject

        offer_title = offer_effective_title(offer) if offer else None
        offer_image = offer_effective_photo(offer) if offer else None
        title = offer_title or normalized_reply_subject(subj_pre) or subj_pre
        price = offer_effective_price(offer) if offer else ""
        if not title:
            await callback.message.answer("❌ Нет названия объявления (title).")
            return await callback.answer()

        api_service = gag_service_for_api(service)

        dom_raw = (await get_user_setting(session, user, "gag_domain_slot") or "").strip()
        dom_slot = None
        try:
            if dom_raw and dom_raw.lower() not in {"team", "default", "0"}:
                dom_slot = int(dom_raw)
        except Exception:
            dom_slot = None
        api_domain = (dom_slot + 4) if dom_slot in (1, 2, 3, 4) else None

        try:
            gag_url = await generate_gag_url(
                endpoint=gag_generate_endpoint(),
                apikey=gag_key,
                title=title,
                price=price,
                service=api_service,
                name=prof_name,
                address=prof_addr,
                image=offer_image,
                domain=api_domain,
                version=gag_default_version(),
            )
        except GAGError as e:
            await callback.message.answer(f"❌ GAG ошибка: {e}")
            return await callback.answer()

        await _upsert_convlink(
            session,
            user_id=int(owner_user_id),
            inbox_email=inbox_email,
            contact_email=contact_email,
            ad_url=url,
            generated_link=gag_url,
            pinned_offer_id=int(offer.id) if offer else None,
        )
        if mail:
            try:
                mail.generated_link = gag_url
            except Exception:
                pass
        await session.commit()

        inbox_label = (getattr(user, "sender_name", None) or "").strip()
        anchor = _resolve_mail_anchor(acc_id, uid, meta, callback.message)

        await _send_generated_link_card(
            callback=callback,
            offer_title=offer_title or title,
            offer_price=price,
            photo_url=offer_image,
            profile_display=prof_title,
            service_code=service,
            link=gag_url,
            offer_id=offer_id,
            anchor_message_id=anchor,
            account_email=inbox_email,
            contact_email=contact_email,
            inbox_label=inbox_label or None,
        )
        await callback.answer("Готово ✅")


@router.callback_query(F.data.startswith("mail_reply:"))
async def cb_mail_reply(callback: CallbackQuery, state: FSMContext):
    try:
        _, acc_id, uid = (callback.data or "").split(":", 2)
        acc_id = int(acc_id)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    meta = FULL_META.get((acc_id, uid)) or {}
    async with Session() as session:
        to_email, subject, account_email = await _resolve_reply_recipient(
            session, acc_id, uid, meta=meta, state_data={}
        )

    if not to_email or "@" not in to_email:
        return await callback.answer(
            "Не вижу email получателя. Откройте карточку письма и нажмите «Написать ещё» снова.",
            show_alert=True,
        )

    inbox_label = ""
    try:
        async with Session() as session:
            user = await get_or_create_user(session, int(callback.from_user.id))
            inbox_label = (getattr(user, "sender_name", None) or "").strip()
    except Exception:
        pass

    fm = dict(FULL_META.get((acc_id, uid)) or meta)
    fm.update(
        {
            "from_email": to_email,
            "subject": subject,
            "account_email": account_email,
            "tg_card_message_id": int(callback.message.message_id),
        }
    )
    FULL_META[(acc_id, uid)] = fm

    await state.set_state(_MailReplyState.waiting_choice)
    ui = await callback.message.answer(
        REPLY_CHOICE_TEXT,
        reply_markup=_kb_reply_choice(acc_id, uid),
    )
    await state.update_data(
        acc_id=acc_id,
        uid=uid,
        to_email=to_email,
        subject=subject,
        account_email=account_email,
        anchor_message_id=int(callback.message.message_id),
        ui_message_id=int(ui.message_id),
        inbox_label=inbox_label,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mail_reply_mode:"))
async def cb_mail_reply_mode(callback: CallbackQuery, state: FSMContext):
    """Choice menu after clicking "Написать ещё"."""
    try:
        _, mode, acc_id, uid = (callback.data or "").split(":", 3)
        acc_id = int(acc_id)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    data = await state.get_data()

    if mode in {"cancel"}:
        await _delete_message_safe(
            callback.bot,
            callback.message.chat.id,
            data.get("ui_message_id") or callback.message.message_id,
        )
        await state.clear()
        return await callback.answer("Отменено")

    if mode in {"back"}:
        await state.set_state(_MailReplyState.waiting_choice)
        try:
            await callback.message.edit_text(
                REPLY_CHOICE_TEXT,
                reply_markup=_kb_reply_choice(acc_id, uid),
            )
        except Exception:
            ui = await callback.message.answer(
                REPLY_CHOICE_TEXT,
                reply_markup=_kb_reply_choice(acc_id, uid),
            )
            await state.update_data(ui_message_id=int(ui.message_id))
        else:
            await state.update_data(ui_message_id=int(callback.message.message_id))
        return await callback.answer()

    # preset picker
    if mode == "preset":
        items = await load_templates(int(callback.from_user.id))
        if not items:
            return await callback.answer("Нет шаблонов. Добавь их в ⚡ Шаблоны", show_alert=True)

        try:
            await callback.message.edit_text(
                "🧾 <b>Ваши шаблоны:</b>\n\nНажмите на пресет для отправки",
                parse_mode="HTML",
                reply_markup=_kb_preset_pick(items, acc_id, uid),
            )
        except Exception:
            ui = await callback.message.answer(
                "🧾 <b>Ваши шаблоны:</b>\n\nНажмите на пресет для отправки",
                parse_mode="HTML",
                reply_markup=_kb_preset_pick(items, acc_id, uid),
            )
            await state.update_data(ui_message_id=int(ui.message_id))
        else:
            await state.update_data(ui_message_id=int(callback.message.message_id))
        return await callback.answer()

    # html picker
    if mode == "html":
        async with Session() as session:
            to_email, subject, account_email = await _resolve_reply_recipient(
                session,
                acc_id,
                uid,
                meta=FULL_META.get((acc_id, uid)),
                state_data=data,
            )
        await state.update_data(
            to_email=to_email,
            subject=subject,
            account_email=account_email,
        )
        html_text = (
            "🧩 <b>HTML</b>\n\n"
            f"Кому: <code>{_e(to_email) or '—'}</code>\n"
            f"От ящика: <code>{_e(account_email) or '—'}</code>\n\n"
            "Выберите шаблон:"
        )
        try:
            await callback.message.edit_text(
                html_text,
                parse_mode="HTML",
                reply_markup=_kb_html_pick(acc_id, uid),
            )
        except Exception:
            ui = await callback.message.answer(
                html_text,
                parse_mode="HTML",
                reply_markup=_kb_html_pick(acc_id, uid),
            )
            await state.update_data(ui_message_id=int(ui.message_id))
        else:
            await state.update_data(ui_message_id=int(callback.message.message_id))
        return await callback.answer()

    return await callback.answer("Ок")


@router.callback_query(F.data.startswith("mail_reply_preset:"))
async def cb_mail_reply_preset_send(callback: CallbackQuery, state: FSMContext):
    try:
        _, acc_id, mail_uid, tid = (callback.data or "").split(":", 3)
        acc_id = int(acc_id)
        tid = int(tid)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    data = await state.get_data()
    async with Session() as session:
        to_email, subject, account_email = await _resolve_reply_recipient(
            session,
            acc_id,
            mail_uid,
            meta=FULL_META.get((acc_id, mail_uid)),
            state_data=data,
        )
    if not to_email or "@" not in to_email:
        return await callback.answer(
            "Не вижу email получателя. Откройте карточку письма снова.",
            show_alert=True,
        )
    await state.update_data(
        to_email=to_email,
        subject=subject,
        account_email=account_email,
    )

    tg_id = int(callback.from_user.id)
    preset_body = ""

    db_user_id: int | None = None
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
        db_user_id = int(user.id)
        tmpl_pre = (
            await session.execute(
                sa_select(QuickTemplate)
                .where(QuickTemplate.user_id == int(user.id))
                .where(QuickTemplate.id == int(tid))
            )
        ).scalar_one_or_none()
        if tmpl_pre:
            preset_body = (tmpl_pre.body or "").strip()

    if not (preset_body or "").strip():
        return await callback.answer("Пресет пустой или не найден", show_alert=True)

    async def _send() -> tuple[bool, str | None]:
        async with Session() as session:
            user = await get_or_create_user(session, tg_id)
            acc = (
                await session.execute(sa_select(EmailAccount).where(EmailAccount.id == int(acc_id)))
            ).scalar_one_or_none()
            if not acc:
                return False, "SMTP аккаунт не найден"
            out_subject = _reply_subject(subject)
            return await send_email_via_account_with_proxy(
                session,
                int(user.id),
                acc,
                to_email,
                out_subject,
                preset_body,
                fast=True,
            )

    meta_fm = FULL_META.get((acc_id, mail_uid)) or {}
    state_snap = dict(data)

    async def _notify_builder() -> ReplyNotifyCtx | None:
        return await _reply_notify_build_async(
            acc_id=acc_id,
            uid=str(mail_uid),
            meta=meta_fm,
            state_data=state_snap,
            body_text=preset_body,
            is_preset=True,
            extra_cleanup=[callback.message.message_id],
            user_id=db_user_id,
        )

    await state.clear()
    if not await _bg_incoming_smtp(callback, tg_id, _send, notify_builder=_notify_builder):
        return


def _reply_subject(subject: str) -> str:
    subj_norm = re.sub(r"^(re|aw|fw|fwd)\s*:\s*", "", (subject or ""), flags=re.I).strip()
    return f"Re: {subj_norm}" if subj_norm else "Re:"


def _html_attachment_filename(subject: str) -> str:
    base = re.sub(r"[^\w\-]+", "_", (subject or "reply")[:60]).strip("_") or "reply"
    return f"{base}.html"


def _html_nick_key_for_service(service: str) -> str:
    service = (service or "").strip()
    return f"html_nick_{service}" if service else HTML_NICK_KEY


async def _offer_title_for_email(session: Session, user_id: int, to_email: str) -> str:
    try:
        canon = _canon_email(to_email)
        off = (
            await session.execute(
                sa_select(Offer)
                .join(OfferEmail, OfferEmail.offer_id == Offer.id)
                .where(Offer.user_id == int(user_id))
                .where(OfferEmail.email == canon)
                .order_by(Offer.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if off:
            return (off.title or "").strip()
    except Exception:
        pass
    return ""


async def _load_html_template_for_user(session: Session, user: User, filename: str) -> tuple[str, str | None]:
    """HTML только из data/HTMLch/<сервис>/ (без fallback на общую папку)."""
    from services.html_templates import load_html_for_user

    html, _subdir, err = await load_html_for_user(
        session, user, gag_service_key=GAG_SERVICE_KEY, filename=filename
    )
    return html, err


def _apply_link(html_text: str, link: str) -> str:
    if not html_text:
        return ""
    if not link:
        return html_text
    return re.sub(r"\{\{\s*LINK\s*\}\}", link, html_text, flags=re.I)


@router.callback_query(F.data.startswith("mail_reply_html:"))
async def cb_mail_reply_html_send(callback: CallbackQuery, state: FSMContext):
    try:
        _, kind, acc_id, uid = (callback.data or "").split(":", 3)
        acc_id = int(acc_id)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    data = await state.get_data()
    async with Session() as session:
        to_email, subject_raw, account_email = await _resolve_reply_recipient(
            session,
            acc_id,
            uid,
            meta=FULL_META.get((acc_id, uid)),
            state_data=data,
        )
    if not to_email or "@" not in to_email:
        return await callback.answer(
            "Не вижу email получателя. Откройте карточку письма и «Написать ещё» → HTML.",
            show_alert=True,
        )
    await state.update_data(
        to_email=to_email,
        subject=subject_raw,
        account_email=account_email,
    )

    if kind == "custom":
        await state.set_state(_MailReplyState.waiting_custom_html)
        await callback.message.answer(
            "Отправьте HTML-разметку текстом или .txt файлом\n\n"
            "Чтобы отменить — отправь <code>-</code>.",
            parse_mode="HTML",
        )
        return await callback.answer()

    file_map = {
        "pro": "confirmation.html",
        "go": "confirmation.html",
        "pickup": "pickup.html",
        "sms": "sms.html",
        "push": "push.html",
        "back": "return.html",
    }
    filename = file_map.get(kind)
    if not filename:
        return await callback.answer("Неизвестный шаблон", show_alert=True)

    mail_uid = uid
    tg_id = int(callback.from_user.id)
    html_kind_label = kind.upper()
    sent_pkg: dict = {}

    async with Session() as session:
        user_pre = await get_or_create_user(session, tg_id)
        service_raw = (await get_user_setting(session, user_pre, GAG_SERVICE_KEY) or "").strip()
        if not is_valid_gag_service(service_raw):
            return await callback.answer(
                "Сначала выберите сервис в 👤 Профиль → 🧭 Выбор сервиса",
                show_alert=True,
            )
        from services.html_templates import html_template_path, service_label_for_path

        sub = gag_service_for_html_dir(service_raw)
        if not html_template_path(service_raw, filename):
            label = service_label_for_path(sub or "")
            return await callback.answer(
                f"Нет шаблона {filename} для {label}",
                show_alert=True,
            )

    async def _send() -> tuple[bool, str | None]:
        async with Session() as session:
            user = await get_or_create_user(session, tg_id)
            acc = (
                await session.execute(sa_select(EmailAccount).where(EmailAccount.id == int(acc_id)))
            ).scalar_one_or_none()
            if not acc:
                return False, "SMTP аккаунт не найден"

            from services.html_reply import (
                build_offer_html_ctx,
                get_html_reply_subject,
                get_html_sender_name,
                prepare_html_body,
                resolve_gag_link_for_reply,
            )

            subject = await get_html_reply_subject(session, user, fallback=_reply_subject(subject_raw))
            sender_name = await get_html_sender_name(session, user)

            html_signature = (
                await session.execute(
                    sa_select(UserSetting.value)
                    .where(UserSetting.user_id == int(user.id))
                    .where(UserSetting.key == HTML_SIGNATURE_KEY)
                )
            ).scalar_one_or_none()

            raw_html, tpl_err = await _load_html_template_for_user(session, user, filename)
            if tpl_err or not raw_html:
                return False, tpl_err or "HTML шаблон не найден"

            from services.placeholders import apply_placeholders

            mail_gen_link = None
            try:
                uid_s = (mail_uid or "").strip()
                if uid_s.startswith("S:"):
                    uid_s = uid_s.split(":", 1)[1]
                uid_num = int(uid_s)
                mail_row = (
                    await session.execute(
                        sa_select(IncomingMail)
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

            mail_subj = (getattr(mail_row, "subject", None) or subject_raw or "").strip()
            mail_body = (getattr(mail_row, "body", None) or "").strip() if mail_row else ""
            mail_from_name = (getattr(mail_row, "from_name", None) or "").strip() if mail_row else ""

            link = await resolve_gag_link_for_reply(
                session,
                int(user.id),
                account_email=account_email,
                seller_email=to_email,
                mail_generated_link=mail_gen_link,
            )
            ctx = await build_offer_html_ctx(
                session,
                int(user.id),
                to_email,
                link=link,
                subject=mail_subj,
                from_name=mail_from_name,
                body_text=mail_body,
            )
            html_body = await prepare_html_body(_apply_link(raw_html, link), session, user)
            if html_signature:
                html_body = html_body.replace("{{SIGNATURE}}", str(html_signature))
            html_body = apply_placeholders(html_body, link=link, ctx=ctx)
            sent_pkg["html"] = html_body
            sent_pkg["subject"] = subject

            return await send_email_via_account_with_proxy(
                session,
                int(user.id),
                acc,
                to_email,
                subject,
                html_body,
                is_html=True,
                sender_name=sender_name,
                fast=True,
            )

    meta_fm = FULL_META.get((acc_id, uid)) or {}
    db_user_id: int | None = None
    try:
        async with Session() as session:
            u = await get_or_create_user(session, tg_id)
            db_user_id = int(u.id)
    except Exception:
        pass

    async def _notify_builder() -> ReplyNotifyCtx | None:
        n = await _reply_notify_build_async(
            acc_id=acc_id,
            uid=uid,
            meta=meta_fm,
            state_data=data,
            body_text=f"HTML ({html_kind_label})",
            is_html=True,
            extra_cleanup=[callback.message.message_id],
            user_id=db_user_id,
        )
        if not n:
            return None
        n.html_attachment = sent_pkg.get("html")
        n.html_filename = _html_attachment_filename(sent_pkg.get("subject") or subject_raw)
        return n

    await state.clear()
    if not await _bg_incoming_smtp(callback, tg_id, _send, notify_builder=_notify_builder):
        return


@router.message(_MailReplyState.waiting_choice)
@router.message(_MailReplyState.waiting_text)
async def mail_reply_text(message: Message, state: FSMContext):
    data = await state.get_data()
    text = (message.text or "").strip()
    if text == "-":
        await state.clear()
        return await message.answer("❌ Отменено.")

    acc_id = int(data.get("acc_id") or 0)
    uid = str(data.get("uid") or "")
    tg_id = int(message.from_user.id)
    body_for_notify = text

    async with Session() as session:
        to_email, subject, _acc_em = await _resolve_reply_recipient(
            session,
            acc_id,
            uid,
            meta=FULL_META.get((acc_id, uid)),
            state_data=data,
        )
    if not to_email or "@" not in to_email:
        return await message.answer(
            "❌ Не вижу email получателя. Откройте карточку письма и «Написать ещё» снова."
        )

    out_subject = _reply_subject(subject)

    async def _send() -> tuple[bool, str | None]:
        async with Session() as session:
            acc = (
                await session.execute(sa_select(EmailAccount).where(EmailAccount.id == acc_id))
            ).scalar_one_or_none()
            if not acc:
                return False, "SMTP аккаунт не найден в БД."
            user = await get_or_create_user(session, tg_id)
            owner_user_id = await _get_acc_owner_user_id(session, acc_id)
            if owner_user_id and int(owner_user_id) != int(user.id):
                return False, "Этот ящик не принадлежит вам."
            return await send_email_via_account_with_proxy(
                session,
                int(user.id),
                acc,
                to_email,
                out_subject,
                text,
                fast=True,
            )

    meta_fm = FULL_META.get((acc_id, uid)) or {}
    state_snap = dict(data)
    db_user_id: int | None = None
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
        db_user_id = int(user.id)

    async def _notify_builder() -> ReplyNotifyCtx | None:
        return await _reply_notify_build_async(
            acc_id=acc_id,
            uid=uid,
            meta=meta_fm,
            state_data=state_snap,
            body_text=body_for_notify,
            user_id=db_user_id,
        )

    await state.clear()
    if await _bg_message_smtp(message, tg_id, _send, notify_builder=_notify_builder):
        try:
            FULL_META[(acc_id, uid)] = {
                **(FULL_META.get((acc_id, uid)) or {}),
                "last_reply": "pending",
            }
        except Exception:
            pass


@router.message(_MailReplyState.waiting_custom_html)
async def mail_reply_custom_html(message: Message, state: FSMContext):
    """Send user-provided HTML as a reply (CUSTOME-like)."""
    data = await state.get_data()
    text = (message.text or "").strip()
    if text == "-":
        await state.clear()
        return await message.answer("❌ Отменено.")

    acc_id = int(data.get("acc_id") or 0)
    mail_uid = str(data.get("uid") or "")
    tg_id = int(message.from_user.id)
    html_text = text
    body_for_notify = _preview_reply_body(html_text, is_html=True)
    sent_pkg: dict = {}

    async with Session() as session:
        to_email, subject_raw, account_email = await _resolve_reply_recipient(
            session,
            acc_id,
            mail_uid,
            meta=FULL_META.get((acc_id, mail_uid)),
            state_data=data,
        )
    if not to_email or "@" not in to_email:
        return await message.answer(
            "❌ Не вижу email получателя. Откройте карточку письма снова."
        )

    async def _send() -> tuple[bool, str | None]:
        async with Session() as session:
            user = await get_or_create_user(session, tg_id)
            acc = (
                await session.execute(sa_select(EmailAccount).where(EmailAccount.id == acc_id))
            ).scalar_one_or_none()
            if not acc:
                return False, "SMTP аккаунт не найден в БД."

            from services.html_reply import (
                build_offer_html_ctx,
                get_html_reply_subject,
                get_html_sender_name,
                prepare_html_body,
                resolve_gag_link_for_reply,
            )

            subject = await get_html_reply_subject(session, user, fallback=_reply_subject(subject_raw))
            sender_name = await get_html_sender_name(session, user)

            html_signature = (
                await session.execute(
                    sa_select(UserSetting.value)
                    .where(UserSetting.user_id == int(user.id))
                    .where(UserSetting.key == HTML_SIGNATURE_KEY)
                )
            ).scalar_one_or_none()

            from services.placeholders import apply_placeholders

            mail_gen_link = None
            try:
                uid_s = (mail_uid or "").strip()
                if uid_s.startswith("S:"):
                    uid_s = uid_s.split(":", 1)[1]
                uid_num = int(uid_s)
                mail_row = (
                    await session.execute(
                        sa_select(IncomingMail)
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

            mail_subj = (getattr(mail_row, "subject", None) or subject_raw or "").strip()
            mail_body = (getattr(mail_row, "body", None) or "").strip() if mail_row else ""
            mail_from_name = (getattr(mail_row, "from_name", None) or "").strip() if mail_row else ""

            link = await resolve_gag_link_for_reply(
                session,
                int(user.id),
                account_email=account_email,
                seller_email=to_email,
                mail_generated_link=mail_gen_link,
            )
            ctx = await build_offer_html_ctx(
                session,
                int(user.id),
                to_email,
                link=link,
                subject=mail_subj,
                from_name=mail_from_name,
                body_text=mail_body,
            )
            html_body = await prepare_html_body(_apply_link(html_text, link), session, user)
            if html_signature:
                html_body = html_body.replace("{{SIGNATURE}}", str(html_signature))
            html_body = apply_placeholders(html_body, link=link, ctx=ctx)
            sent_pkg["html"] = html_body
            sent_pkg["subject"] = subject

            return await send_email_via_account_with_proxy(
                session,
                int(user.id),
                acc,
                to_email,
                subject,
                html_body,
                is_html=True,
                sender_name=sender_name,
                fast=True,
            )

    meta_fm = FULL_META.get((acc_id, mail_uid)) or {}
    db_user_id: int | None = None
    async with Session() as session:
        user = await get_or_create_user(session, tg_id)
        db_user_id = int(user.id)

    async def _notify_builder() -> ReplyNotifyCtx | None:
        n = await _reply_notify_build_async(
            acc_id=acc_id,
            uid=mail_uid,
            meta=meta_fm,
            state_data=data,
            body_text=body_for_notify,
            is_html=True,
            user_id=db_user_id,
        )
        if not n:
            return None
        n.html_attachment = sent_pkg.get("html")
        n.html_filename = _html_attachment_filename(sent_pkg.get("subject") or subject_raw)
        return n

    await state.clear()
    await _bg_message_smtp(message, tg_id, _send, notify_builder=_notify_builder)


@router.callback_query(F.data.startswith("mail_info:"))
async def cb_mail_info(callback: CallbackQuery):
    try:
        _, acc_id, uid = (callback.data or "").split(":", 2)
        acc_id = int(acc_id)
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    # ✅ Основной источник данных — БД (IncomingMail).
    # FULL_META может быть очищен при рестартах, поэтому здесь всегда...
    meta = FULL_META.get((acc_id, uid)) or {}
    inbox_email = _canon_email(meta.get("account_email") or "")
    contact_email = _canon_email(meta.get("from_email") or "")
    subject = (meta.get("subject") or "").strip()
    ad_url = (meta.get("ad_url") or "").strip()
    gen_link = (meta.get("generated_link") or "").strip()

    mail_db_id: int | None = None
    mail_body: str = ""
    mail_date: str = ""
    mail_from_name: str = ""
    resolved_offer_id: int | None = None

    offer_title_full = offer_price = offer_link_full = offer_photo = offer_person = ""

    conv_ad = conv_gen = ""
    offer_title = offer_link = ""
    offer_emails_cnt = 0
    offer_link_by_email = ""
    offer_emails_cnt_by_email = 0

    async with Session() as session:
        owner_user_id = await _get_acc_owner_user_id(session, acc_id)
        if owner_user_id:
            # 0) Ищем письмо в БД
            mail = (
                await session.execute(
                    sa_select(IncomingMail)
                    .where(IncomingMail.account_id == int(acc_id))
                    .where(IncomingMail.imap_uid == int(uid))
                    .limit(1)
                )
            ).scalars().first()
            if mail:
                mail_db_id = int(getattr(mail, "id", 0) or 0) or None
                inbox_email = _canon_email(getattr(mail, "account_email", "") or "") or inbox_email
                contact_email = _canon_email(getattr(mail, "from_email", "") or "") or contact_email
                mail_from_name = (getattr(mail, "from_name", "") or "")
                subject = (getattr(mail, "subject", "") or "").strip() or subject
                mail_date = (getattr(mail, "date_str", "") or "")
                mail_body = (getattr(mail, "body", "") or "")
                ad_url = (getattr(mail, "ad_url", "") or "").strip() or ad_url
                gen_link = (getattr(mail, "generated_link", "") or "").strip() or gen_link
                resolved_offer_id = int(getattr(mail, "resolved_offer_id", 0) or 0) or None

            conv = await _get_convlink(
                session,
                user_id=owner_user_id,
                inbox_email=inbox_email,
                contact_email=contact_email,
            )
            if conv:
                conv_ad = (conv.ad_url or "").strip()
                conv_gen = (conv.generated_link or "").strip()

            # 0.1) Если письмо связано с Offer — показываем полные данные из Offer
            if resolved_offer_id:
                off = (
                    await session.execute(
                        sa_select(Offer).where(Offer.id == int(resolved_offer_id)).where(Offer.user_id == int(owner_user_id)).limit(1)
                    )
                ).scalars().first()
                if off:
                    offer_title_full = (getattr(off, "title", "") or "")
                    offer_price = (getattr(off, "price", "") or "")
                    offer_link_full = (getattr(off, "link", "") or "")
                    offer_photo = (getattr(off, "photo", "") or "")
                    offer_person = (getattr(off, "person_name", "") or "")

            if subject:
                subj_norm = re.sub(
                    r"^(re|aw|fw|fwd)\s*:\s*",
                    "",
                    subject,
                    flags=re.I,
                ).strip()
                m = re.search(r"\bOFFER\s*:\s*(.+)$", subj_norm, flags=re.I)
                offer_title = (m.group(1).strip() if m else subj_norm)

                row = (
                    await session.execute(
                        sa_select(Offer.id, Offer.link)
                        .where(Offer.user_id == int(owner_user_id))
                        .where(func.lower(Offer.title).like(f"%{offer_title.lower()}%"))
                        .order_by(Offer.id.desc())
                        .limit(1)
                    )
                ).first()
                if row:
                    oid, olink = row
                    offer_link = (olink or "").strip()
                    offer_emails_cnt = (
                        await session.execute(
                            sa_select(func.count(OfferEmail.id)).where(
                                OfferEmail.offer_id == int(oid)
                            )
                        )
                    ).scalar_one() or 0

            # если по теме ничего не нашлось — ищем по email отправителя
            if contact_email:
                offer_obj, cnt = await _offer_by_sender_email(
                    session, int(owner_user_id), contact_email
                )
                if offer_obj is not None:
                    offer_link_by_email = (
                        getattr(offer_obj, "link", "") or ""
                    ).strip()
                    offer_emails_cnt_by_email = int(cnt or 0)

    # Показываем "фул данные" по письму (основное из IncomingMail + привязанные сущности).
    # Дизайн не меняем — просто текст.
    body_preview = (mail_body or "").strip()
    if body_preview and len(body_preview) > 1800:
        body_preview = body_preview[:1800] + "…"

    text = (
        "ℹ️ <b>Информация по письму</b>\n\n"
        f"<b>Mail ID:</b> <code>{mail_db_id or '—'}</code>\n"
        f"<b>Inbox:</b> <code>{_e(inbox_email) or '—'}</code>\n"
        f"<b>From:</b> <code>{_e(contact_email) or '—'}</code>\n"
        f"<b>From name:</b> <code>{_e(mail_from_name) or '—'}</code>\n"
        f"<b>Subject:</b> <code>{_e(subject) or '—'}</code>\n\n"
        f"<b>Date:</b> <code>{_e(mail_date) or '—'}</code>\n\n"
        f"<b>Meta ad_url:</b> <code>{_e(ad_url) or '—'}</code>\n"
        f"<b>Meta generated_link:</b> <code>{_e(gen_link) or '—'}</code>\n"
        f"<b>DB conv ad_url:</b> <code>{_e(conv_ad) or '—'}</code>\n"
        f"<b>DB conv generated_link:</b> <code>{_e(conv_gen) or '—'}</code>\n\n"
        f"<b>Resolved offer_id:</b> <code>{resolved_offer_id or '—'}</code>\n"
        f"<b>Offer.title:</b> <code>{_e(offer_title_full) or '—'}</code>\n"
        f"<b>Offer.price:</b> <code>{_e(offer_price) or '—'}</code>\n"
        f"<b>Offer.link:</b> <code>{_e(offer_link_full) or '—'}</code>\n"
        f"<b>Offer.photo:</b> <code>{_e(offer_photo) or '—'}</code>\n"
        f"<b>Offer.person_name:</b> <code>{_e(offer_person) or '—'}</code>\n\n"
        f"<b>Offer title from subject:</b> <code>{_e(offer_title) or '—'}</code>\n"
        f"<b>Offer.link (by title):</b> <code>{_e(offer_link) or '—'}</code>\n"
        f"<b>Offer emails count:</b> <code>{offer_emails_cnt}</code>\n"
        f"<b>Offer.link (by sender email):</b> <code>{_e(offer_link_by_email) or '—'}</code>\n"
        f"<b>Offer emails count (by sender email):</b> <code>{offer_emails_cnt_by_email}</code>\n"
        + ("\n<b>Body preview:</b>\n<blockquote><code>" + _e(body_preview) + "</code></blockquote>" if body_preview else "")
    )
    await callback.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer()


# =========================
# Offer price edit (кнопка "Цена" на pinned карточке)
# =========================
# Offer price → пересоздать GAG-ссылку (в API GAG нет смены цены)
# =========================


def _format_gag_price_from_input(text: str, *, previous: str | None = None) -> str | None:
    """Парсинг цены для GAG: по умолчанию CHF (как в офферах Швейцарии)."""
    raw = (text or "").strip().replace(",", ".")
    if not raw:
        return None
    m = re.search(r"([\d.]+)", raw)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    if num < 0:
        return None

    prev = (previous or "").upper()
    suffix_m = re.search(r"([A-Za-z]{2,4}|€)\s*$", raw.strip())
    if suffix_m:
        suf = suffix_m.group(1).upper()
        if suf in {"€", "EUR"}:
            return f"{num:.2f} EUR"
        if suf == "CHF":
            return f"{num:.2f} CHF"
    if "EUR" in prev or "€" in (previous or ""):
        return f"{num:.2f} EUR"
    return f"{num:.2f} CHF"


async def _resolve_offer_dialog_emails(session, user_id: int, offer_id: int) -> tuple[str, str]:
    """inbox (наш ящик) и contact (продавец) по OfferEmail + ConversationLink."""
    emails = (
        await session.execute(
            sa_select(OfferEmail.email).where(OfferEmail.offer_id == int(offer_id)).limit(8)
        )
    ).scalars().all()
    for em in emails:
        contact = _canon_email(str(em or ""))
        if not contact:
            continue
        conv = (
            await session.execute(
                sa_select(ConversationLink)
                .where(ConversationLink.user_id == int(user_id))
                .where(func.lower(ConversationLink.from_email) == contact)
                .order_by(ConversationLink.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if conv and (conv.account_email or "").strip():
            return _canon_email(conv.account_email or ""), contact
    return "", ""


async def _gag_generate_link_for_offer(session, user: User, offer: Offer, price: str) -> str:
    gag_key = await get_user_gag_api_key(session, user)
    if not gag_key:
        raise GAGError("GAG API ключ не установлен")

    user_svc = (await get_user_setting(session, user, GAG_SERVICE_KEY) or "").strip()
    service = gag_service_from_offer_link((offer.link or "").strip(), user_fallback=user_svc) or ""
    if not is_valid_gag_service(service):
        raise GAGError("Не выбран сервис GAG")

    prof_name = (await get_user_setting(session, user, GAG_PROFILE_NAME_KEY) or "").strip()
    prof_addr = (await get_user_setting(session, user, GAG_PROFILE_ADDRESS_KEY) or "").strip()
    if not (prof_name and prof_addr):
        raise GAGError("Профиль GAG не заполнен")

    title = offer_effective_title(offer)
    if not title:
        raise GAGError("Нет названия объявления (title)")

    p = (price or "").strip() or offer_effective_price(offer)
    if not p:
        raise GAGError("Нет цены")

    offer_image = offer_effective_photo(offer) or None
    api_service = gag_service_for_api(service)

    dom_raw = (await get_user_setting(session, user, "gag_domain_slot") or "").strip()
    dom_slot = None
    try:
        if dom_raw and dom_raw.lower() not in {"team", "default", "0"}:
            dom_slot = int(dom_raw)
    except Exception:
        dom_slot = None
    api_domain = (dom_slot + 4) if dom_slot in (1, 2, 3, 4) else None

    return await generate_gag_url(
        endpoint=gag_generate_endpoint(),
        apikey=gag_key,
        title=title,
        price=p,
        service=api_service,
        name=prof_name,
        address=prof_addr,
        image=offer_image,
        domain=api_domain,
        version=gag_default_version(),
    )


async def _regenerate_gag_link_after_price(
    bot,
    chat_id: int,
    tg_user_id: int,
    *,
    offer_id: int,
    new_price: str,
    anchor_message_id: int | None,
    inbox_email: str,
    contact_email: str,
) -> tuple[bool, str]:
    card: dict = {}
    async with Session() as session:
        user = await get_or_create_user(session, int(tg_user_id))
        offer = (
            await session.execute(
                sa_select(Offer)
                .where(Offer.id == int(offer_id))
                .where(Offer.user_id == int(user.id))
                .limit(1)
            )
        ).scalars().first()
        if not offer:
            return False, "Оффер не найден"

        offer.price = new_price
        await session.flush()

        try:
            gag_url = await _gag_generate_link_for_offer(session, user, offer, new_price)
        except GAGError as e:
            await session.rollback()
            return False, str(e)

        ad_url = (offer.link or "").strip()
        if inbox_email and contact_email:
            await _upsert_convlink(
                session,
                user_id=int(user.id),
                inbox_email=inbox_email,
                contact_email=contact_email,
                ad_url=ad_url or None,
                generated_link=gag_url,
                pinned_offer_id=int(offer.id),
            )

        await session.execute(
            sa_update(IncomingMail)
            .where(IncomingMail.user_id == int(user.id))
            .where(IncomingMail.resolved_offer_id == int(offer.id))
            .values(generated_link=gag_url)
        )
        await session.commit()

        card = {
            "offer_title": offer_effective_title(offer) or None,
            "offer_price": new_price,
            "photo_url": offer_effective_photo(offer) or None,
            "profile_display": (
                await get_user_setting(session, user, GAG_PROFILE_TITLE_KEY) or ""
            ).strip()
            or None,
            "service_code": gag_service_from_offer_link(
                (offer.link or "").strip(),
                user_fallback=(await get_user_setting(session, user, GAG_SERVICE_KEY) or "").strip(),
            )
            or "",
            "link": gag_url,
            "offer_id": int(offer.id),
            "inbox_label": (getattr(user, "sender_name", None) or "").strip() or None,
        }

    await _send_generated_link_card_to_chat(
        bot,
        int(chat_id),
        offer_title=card["offer_title"],
        offer_price=card["offer_price"],
        photo_url=card["photo_url"],
        profile_display=card["profile_display"],
        service_code=card["service_code"],
        link=card["link"],
        offer_id=card["offer_id"],
        anchor_message_id=anchor_message_id,
        account_email=inbox_email or None,
        contact_email=contact_email or None,
        inbox_label=card["inbox_label"],
    )
    return True, new_price


class _OfferPriceState(StatesGroup):
    waiting_price = State()


@router.callback_query(F.data.startswith("offer_price:"))
async def cb_offer_price(callback: CallbackQuery, state: FSMContext):
    try:
        offer_id = int((callback.data or "").split(":", 1)[1])
    except Exception:
        return await callback.answer("Неверный ID", show_alert=True)

    anchor: int | None = None
    rt = callback.message.reply_to_message if callback.message else None
    if rt and getattr(rt, "message_id", None):
        anchor = int(rt.message_id)

    inbox_email = contact_email = ""
    async with Session() as session:
        user = await get_or_create_user(session, int(callback.from_user.id))
        offer = (
            await session.execute(
                sa_select(Offer)
                .where(Offer.id == int(offer_id))
                .where(Offer.user_id == int(user.id))
                .limit(1)
            )
        ).scalars().first()
        if not offer:
            return await callback.answer("Оффер не найден", show_alert=True)
        current = (offer.price or "—").strip() or "—"
        inbox_email, contact_email = await _resolve_offer_dialog_emails(session, int(user.id), int(offer_id))
        if not anchor and inbox_email and contact_email:
            conv = await _load_convlink_for_reply(
                session,
                user_id=int(user.id),
                inbox_email=inbox_email,
                contact_email=contact_email,
            )
            if conv and getattr(conv, "tg_message_id", None):
                anchor = int(conv.tg_message_id)

    await state.clear()
    await state.set_state(_OfferPriceState.waiting_price)
    await state.update_data(
        offer_id=offer_id,
        chat_id=int(callback.message.chat.id),
        anchor_message_id=anchor,
        inbox_email=inbox_email,
        contact_email=contact_email,
    )

    await callback.message.answer(
        "💶 <b>Цена</b>\n\n"
        f"Текущая цена: <code>{_e(current)}</code>\n\n"
        "Отправь новую цену (например: <code>500</code> или <code>500.00 CHF</code>).\n"
        "Бот пересоздаст GAG-ссылку и отправит её к письму.\n\n"
        "Чтобы отменить — отправь <code>-</code>.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(_OfferPriceState.waiting_price)
async def offer_price_set(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "-":
        await state.clear()
        return await message.answer("❌ Отменено.")

    data = await state.get_data()
    offer_id = int(data.get("offer_id") or 0)
    if not offer_id:
        await state.clear()
        return await message.answer("❌ Нет offer_id.")

    prev_price = ""
    async with Session() as session:
        offer = (
            await session.execute(sa_select(Offer).where(Offer.id == offer_id).limit(1))
        ).scalars().first()
        if offer:
            prev_price = (offer.price or "").strip()

    new_price = _format_gag_price_from_input(text, previous=prev_price)
    if not new_price:
        return await message.answer(
            "❌ Введи число (пример: <code>500</code> или <code>500.00 CHF</code>) или <code>-</code> для отмены.",
            parse_mode="HTML",
        )

    chat_id = int(data.get("chat_id") or message.chat.id)
    anchor = data.get("anchor_message_id")
    anchor_id = int(anchor) if anchor else None
    inbox_email = _canon_email(str(data.get("inbox_email") or ""))
    contact_email = _canon_email(str(data.get("contact_email") or ""))
    tg_id = int(message.from_user.id)

    await state.clear()

    if bg_is_running(tg_id, "gag_link"):
        return await message.answer("⏳ Ссылка уже создаётся… подождите.")

    await message.answer("⏳ Пересоздаю ссылку с новой ценой…")

    bot = message.bot

    async def _job() -> None:
        ok, info = await _regenerate_gag_link_after_price(
            bot,
            chat_id,
            tg_id,
            offer_id=offer_id,
            new_price=new_price,
            anchor_message_id=anchor_id,
            inbox_email=inbox_email,
            contact_email=contact_email,
        )
        if ok:
            await bot.send_message(
                chat_id,
                f"✅ Цена обновлена: <code>{_e(info)}</code>\nНовая ссылка прикреплена к письму.",
                parse_mode="HTML",
                reply_to_message_id=anchor_id,
            )
        else:
            await bot.send_message(
                chat_id,
                f"❌ Не удалось пересоздать ссылку:\n<code>{_e(info)}</code>",
                parse_mode="HTML",
            )

    if not bg_start(tg_id, "gag_link", _job()):
        await message.answer("⏳ Ссылка уже создаётся…")
