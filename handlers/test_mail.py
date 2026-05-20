import asyncio
import html
import pathlib
import random
import re
from typing import List

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from database import async_session
from handlers.first_sms import pick_random_first_sms
from handlers.templates import pick_random_smart_preset
from models import EmailAccount, Offer, OfferEmail, User
from services.smtp_block_control import is_smtp_account_block_error, mark_account_smtp_blocked
from services.smtp_delivery_verify import verify_message_in_sent
from services.smtp_proxy_send import send_email_via_account_with_proxy
from services.user_json_store import load_json_blob, save_json_blob
from utils.bg_jobs import is_running as bg_is_running, start as bg_start

router = Router()

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TEST_MAIL_BLOB = "test_mail_recipients"
MAX_TEST_RECIPIENTS = 20

class TestMailStates(StatesGroup):
    waiting_add = State()
    waiting_oneoff = State()


def _canon_email(addr: str) -> str:
    return (addr or "").strip().lower()


def _parse_emails(text: str) -> List[str]:
    raw = (text or "").replace(";", "\n").replace(",", "\n")
    out: List[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        em = _canon_email(line)
        if not em or not EMAIL_RE.match(em):
            continue
        if em in seen:
            continue
        seen.add(em)
        out.append(em)
    return out


async def _load_recipients(tg_id: int) -> List[str]:
    data = await load_json_blob(int(tg_id), TEST_MAIL_BLOB, default=[])
    out: List[str] = []
    seen: set[str] = set()
    for item in data if isinstance(data, list) else []:
        em = _canon_email(str(item))
        if em and EMAIL_RE.match(em) and em not in seen:
            seen.add(em)
            out.append(em)
    return out


async def _save_recipients(tg_id: int, emails: List[str]) -> None:
    clean: List[str] = []
    seen: set[str] = set()
    for em in emails:
        c = _canon_email(em)
        if c and EMAIL_RE.match(c) and c not in seen:
            seen.add(c)
            clean.append(c)
    await save_json_blob(int(tg_id), TEST_MAIL_BLOB, clean[:MAX_TEST_RECIPIENTS])


def _menu_text(emails: List[str]) -> str:
    if not emails:
        return (
            "🧪 <b>Тест маил</b>\n\n"
            "Список получателей пуст.\n"
            "Нажмите «➕ Добавить email» — можно сразу несколько строк."
        )
    lines = "\n".join(f"{i + 1}. <code>{html.escape(em)}</code>" for i, em in enumerate(emails))
    return (
        "🧪 <b>Тест маил</b>\n\n"
        f"<b>Сохранённые адреса ({len(emails)}):</b>\n{lines}\n\n"
        "Отправка как в /send: тема <code>OFFER</code> → название товара, умный пресет, свой аккаунт на каждое письмо."
    )


def _menu_kb(emails: List[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if emails:
        rows.append(
            [InlineKeyboardButton(text="▶️ Отправить на все", callback_data="tm_send:all")]
        )
        for i, em in enumerate(emails[:10]):
            label = em if len(em) <= 28 else em[:25] + "…"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📤 {label}",
                        callback_data=f"tm_send:{i}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="➕ Добавить email", callback_data="tm_add")])
    if emails:
        rows.append([InlineKeyboardButton(text="🗑 Очистить список", callback_data="tm_clear")])
    rows.append([InlineKeyboardButton(text="✏️ Разовый email", callback_data="tm_oneoff")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_menu(message: Message, tg_id: int, *, edit: bool = False) -> None:
    emails = await _load_recipients(tg_id)
    text = _menu_text(emails)
    kb = _menu_kb(emails)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _pick_send_context(tg_id: int) -> tuple[int, EmailAccount, str, str, str] | None:
    async with async_session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == int(tg_id)))
        ).scalars().first()
        if not user:
            return None

        user_id = int(user.id)
        accs = (
            await session.execute(select(EmailAccount).where(EmailAccount.user_id == user_id))
        ).scalars().all()
        accs = [a for a in accs if getattr(a, "status", "") == "active"]
        if not accs:
            return None

        title_row = (
            await session.execute(
                select(Offer.title).where(Offer.title.is_not(None)).order_by(func.random()).limit(1)
            )
        ).first()
        offer_title = title_row[0] if title_row and title_row[0] else ""

    from services.subject_offer import subject_for_offer

    subject = subject_for_offer(offer_title or "")
    body = await pick_random_smart_preset(tg_id, offer_title)
    if not (body or "").strip():
        body = await pick_random_first_sms(tg_id, offer_title)
    if not (body or "").strip():
        return None

    account = random.choice(accs)
    return user_id, account, subject, body, offer_title


async def _send_test_one(
    *,
    bot,
    chat_id: int,
    tg_id: int,
    to_email: str,
    user_id: int,
    account: EmailAccount,
    subject: str,
    body: str,
    offer_title: str,
) -> tuple[bool, str]:
    acc_email = account.email or ""
    try:
        async with async_session() as session_proxy:
            ok, err, msgid = await send_email_via_account_with_proxy(
                session_proxy,
                user_id,
                account,
                to_email,
                subject,
                body,
            )
    except Exception as e:
        return False, f"{acc_email}: {e}"

    if not ok:
        err_s = err or "unknown"
        if is_smtp_account_block_error(err_s):
            async with async_session() as session_blk:
                await mark_account_smtp_blocked(
                    session_blk,
                    account,
                    err_s,
                    db_user_id=user_id,
                    bot=bot,
                    chat_id=int(chat_id),
                )
        return False, f"{acc_email} → {to_email}: {err_s}"

    imap_note = ""
    try:
        await asyncio.sleep(3)
        verified, verify_msg = await verify_message_in_sent(
            account.email,
            account.password or "",
            subject=subject,
            to_email=to_email,
            message_id=msgid,
        )
        if verified:
            imap_note = f" ({verify_msg})"
    except Exception:
        pass

    async with async_session() as session2:
        raw_link = await pick_random_raw_link(session2)
        if raw_link:
            test_offer = Offer(
                user_id=user_id,
                title="TEST MAIL",
                link=raw_link,
                price=None,
                photo=None,
                person_name="TEST",
            )
            session2.add(test_offer)
            await session2.flush()
            session2.add(OfferEmail(offer_id=test_offer.id, email=to_email))
            await session2.commit()

    return True, f"✅ <code>{to_email}</code> ← <code>{acc_email}</code>{imap_note}"


async def _run_test_batch(
    *,
    bot,
    chat_id: int,
    tg_id: int,
    targets: List[str],
    status_message: Message,
) -> None:
    ok_n = 0
    fail_n = 0
    lines: List[str] = []
    last_subject = ""

    for i, to_email in enumerate(targets):
        if i > 0:
            await asyncio.sleep(4)
        ctx = await _pick_send_context(tg_id)
        if not ctx:
            fail_n += 1
            lines.append(f"❌ {to_email}: нет аккаунта/шаблона")
            continue
        user_id, account, subject, body, offer_title = ctx
        last_subject = subject
        ok, line = await _send_test_one(
            bot=bot,
            chat_id=chat_id,
            tg_id=tg_id,
            to_email=to_email,
            user_id=user_id,
            account=account,
            subject=subject,
            body=body,
            offer_title=offer_title,
        )
        if ok:
            ok_n += 1
        else:
            fail_n += 1
        lines.append(line)
        try:
            await status_message.edit_text(
                f"⏳ Тест {i + 1}/{len(targets)}…\n\n" + "\n".join(lines[-5:]),
                parse_mode="HTML",
            )
        except Exception:
            pass

    summary = (
        f"<b>Тест завершён</b> — OK: {ok_n}, ошибок: {fail_n}\n"
        f"Тема (как в рассылке): <code>{html.escape(last_subject or '—')}</code>\n\n"
        + "\n".join(lines)
    )
    try:
        await status_message.edit_text(summary, parse_mode="HTML")
    except Exception:
        await bot.send_message(chat_id, summary, parse_mode="HTML")


@router.message(F.text == "🧪 Тест маил")
async def test_mail_start(message: Message, state: FSMContext):
    from services.bot_roles import user_is_admin

    if not await user_is_admin(message.from_user.id):
        return
    await state.clear()
    await _show_menu(message, int(message.from_user.id))


@router.callback_query(F.data == "tm_add")
async def cb_test_mail_add(callback: CallbackQuery, state: FSMContext):
    from services.bot_roles import user_is_admin

    if not await user_is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await state.set_state(TestMailStates.waiting_add)
    await callback.message.answer(
        "➕ Введите один или несколько email (каждый с новой строки или через запятую).\n"
        "«-» — отмена.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "tm_oneoff")
async def cb_test_mail_oneoff(callback: CallbackQuery, state: FSMContext):
    from services.bot_roles import user_is_admin

    if not await user_is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await state.set_state(TestMailStates.waiting_oneoff)
    await callback.message.answer(
        "✏️ Разовый тест — email(ы) без сохранения в список.\n"
        "Несколько адресов: каждый с новой строки.\n«-» — отмена.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "tm_clear")
async def cb_test_mail_clear(callback: CallbackQuery, state: FSMContext):
    from services.bot_roles import user_is_admin

    if not await user_is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    tg_id = int(callback.from_user.id)
    await _save_recipients(tg_id, [])
    await state.clear()
    await callback.answer("Список очищен")
    await _show_menu(callback.message, tg_id, edit=True)


@router.callback_query(F.data.startswith("tm_send:"))
async def cb_test_mail_send(callback: CallbackQuery, state: FSMContext):
    from services.bot_roles import user_is_admin

    if not await user_is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)

    tg_id = int(callback.from_user.id)
    key = (callback.data or "").split(":", 1)[1]
    emails = await _load_recipients(tg_id)
    if not emails:
        return await callback.answer("Список пуст — добавьте email", show_alert=True)

    if key == "all":
        targets = list(emails)
    else:
        try:
            idx = int(key)
            targets = [emails[idx]]
        except (ValueError, IndexError):
            return await callback.answer("Неверный адрес", show_alert=True)

    sender_emails = {_canon_email(a.email) for a in await _load_active_accounts(tg_id)}
    targets = [t for t in targets if _canon_email(t) not in sender_emails]
    if not targets:
        return await callback.answer(
            "Нельзя слать на тот же ящик, что и единственный аккаунт отправителя.",
            show_alert=True,
        )

    if bg_is_running(tg_id, "test_mail"):
        return await callback.answer("⏳ Тест уже идёт…", show_alert=True)

    await state.clear()
    await callback.answer("⏳ Отправляю…")
    status = await callback.message.answer(
        f"⏳ Тест на {len(targets)} адр…",
        parse_mode="HTML",
    )

    async def _job() -> None:
        await _run_test_batch(
            bot=callback.bot,
            chat_id=int(callback.message.chat.id),
            tg_id=tg_id,
            targets=targets,
            status_message=status,
        )

    if not bg_start(tg_id, "test_mail", _job()):
        await callback.answer("⏳ Тест уже идёт…", show_alert=True)


async def _load_active_accounts(tg_id: int) -> List[EmailAccount]:
    async with async_session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == int(tg_id)))
        ).scalars().first()
        if not user:
            return []
        accs = (
            await session.execute(
                select(EmailAccount).where(EmailAccount.user_id == int(user.id))
            )
        ).scalars().all()
        return [a for a in accs if getattr(a, "status", "") == "active"]


@router.message(TestMailStates.waiting_add)
async def test_mail_add_emails(message: Message, state: FSMContext):
    from services.bot_roles import user_is_admin

    if not await user_is_admin(message.from_user.id):
        await state.clear()
        return

    text = (message.text or "").strip()
    if text in {"-", "cancel"} or text.lower() == "cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return await _show_menu(message, int(message.from_user.id))

    new_emails = _parse_emails(text)
    if not new_emails:
        return await message.answer("❌ Не нашёл валидных email. Попробуйте снова или «-».")

    tg_id = int(message.from_user.id)
    current = await _load_recipients(tg_id)
    merged = list(current)
    seen = set(current)
    added = 0
    for em in new_emails:
        if em in seen:
            continue
        if len(merged) >= MAX_TEST_RECIPIENTS:
            break
        merged.append(em)
        seen.add(em)
        added += 1

    await _save_recipients(tg_id, merged)
    await state.clear()
    await message.answer(f"✅ Добавлено: {added}. Всего в списке: {len(merged)}.")
    await _show_menu(message, tg_id)


@router.message(TestMailStates.waiting_oneoff)
async def test_mail_oneoff(message: Message, state: FSMContext):
    from services.bot_roles import user_is_admin

    if not await user_is_admin(message.from_user.id):
        await state.clear()
        return

    text = (message.text or "").strip()
    if text in {"-", "cancel"} or text.lower() == "cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return

    targets = _parse_emails(text)
    if not targets:
        return await message.answer("❌ Не нашёл валидных email.")

    tg_id = int(message.from_user.id)
    sender_emails = {_canon_email(a.email) for a in await _load_active_accounts(tg_id)}
    targets = [t for t in targets if _canon_email(t) not in sender_emails]
    if not targets:
        await state.clear()
        return await message.answer("❌ Получатель совпадает с аккаунтом отправителя.")

    if bg_is_running(tg_id, "test_mail"):
        return await message.answer("⏳ Тест уже идёт…")

    await state.clear()
    status = await message.answer(
        f"⏳ Разовый тест на {len(targets)} адр…",
        parse_mode="HTML",
    )

    async def _job() -> None:
        await _run_test_batch(
            bot=message.bot,
            chat_id=int(message.chat.id),
            tg_id=tg_id,
            targets=targets,
            status_message=status,
        )

    if not bg_start(tg_id, "test_mail", _job()):
        await message.answer("⏳ Тест уже идёт…")


def _is_valid_ad_link(url: str) -> bool:
    if not url:
        return False
    u = url.lower().strip()
    if "kleinanzeigen.de" in u:
        return True
    if "ebay." in u and ".de" in u:
        return True
    return False


async def pick_random_raw_link(session):
    row = (
        await session.execute(
            select(Offer.link).where(Offer.link.is_not(None)).order_by(func.random()).limit(50)
        )
    ).all()
    for (candidate,) in row:
        if _is_valid_ad_link(candidate):
            return candidate

    p = pathlib.Path("data/test_links.txt")
    if not p.exists():
        return None
    links = [
        x.strip()
        for x in p.read_text(encoding="utf-8").splitlines()
        if x.strip() and _is_valid_ad_link(x.strip())
    ]
    return random.choice(links) if links else None


@router.message(Command("preview_imap"))
async def preview_imap_card(message: Message) -> None:
    """Демо-карточка входящего письма (тот же UI, что у IMAP)."""
    from services.incoming_mail_worker import build_kb, render_mail_text_chunks

    async with async_session() as session:
        user = (
            await session.execute(
                select(User).where(User.telegram_id == int(message.from_user.id)).limit(1)
            )
        ).scalars().first()
        if not user:
            return await message.answer("Сначала /start")

        acc = (
            await session.execute(
                select(EmailAccount)
                .where(EmailAccount.user_id == int(user.id))
                .where(EmailAccount.status.in_(["active", "enabled"]))
                .limit(1)
            )
        ).scalars().first()
        inbox = (getattr(user, "sender_name", None) or "").strip() or "Demo User"
        account_email = (getattr(acc, "email", None) or "demo@gmail.com") if acc else "demo@gmail.com"

    demo_body = (
        "Sie haben wohl eine falsche Mail adresse, ich habe nichts zum Verkauf ausgeschrieben\n\n"
        "--------\n"
        "Gesendet: Mittwoch, 25. März 2026 um 14:45\n"
        "Von: Maria Johansen <demo@gmail.com>\n"
    )
    chunks = render_mail_text_chunks(
        account_email=account_email,
        inbox_label=inbox,
        from_name="lara.wolf",
        from_email="lara.wolf@gmx.ch",
        subject="Aw: Johann Jakob Couchtisch, Messing-Glas",
        body=demo_body,
        offer_id=12345,
        link_id="210743034",
        service_label="ricardo.ch",
        product_title="Johann Jakob Couchtisch, Messing-Glas",
    )
    kb = build_kb(0, "preview", mail_id=None)
    await message.answer(
        "ℹ️ <b>Демо-карточка</b> (не реальное IMAP-письмо). Разворот текста — стрелкой в блоке «Текст». Кнопки «Перевести» — на живых письмах с ID в БД.",
        parse_mode="HTML",
    )
    await message.answer(chunks[0], reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
