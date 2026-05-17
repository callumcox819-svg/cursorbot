import random
import pathlib
import re
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from config import config
from database import async_session
from models import EmailAccount, Offer, OfferEmail, User
from sqlalchemy import select, func
from services.smtp_proxy_send import send_email_via_account_with_proxy
from handlers.templates import pick_random_smart_preset
from handlers.first_sms import pick_random_first_sms

router = Router()

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class TestMailStates(StatesGroup):
    waiting_email = State()


TEST_SUBJECTS = [
    "Test message – Order update",
    "Test message – Please confirm",
    "Test message – Action required",
    "Test message – Status notification",
    "Test message – Delivery check",
]


@router.message(F.text == "🧪 Тест маил")
async def test_mail_start(message: Message, state: FSMContext):
    from services.bot_roles import user_is_admin

    if not await user_is_admin(message.from_user.id):
        return
    await state.set_state(TestMailStates.waiting_email)
    await message.answer("🧪 Введите email для теста (или '-' чтобы отменить):")


@router.message(TestMailStates.waiting_email)
async def test_mail_send(message: Message, state: FSMContext):
    from services.bot_roles import user_is_admin

    if not await user_is_admin(message.from_user.id):
        await state.clear()
        return

    text = (message.text or "").strip()
    if text == "-" or text.lower() == "cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return

    if not EMAIL_RE.match(text):
        await message.answer("❌ Это не похоже на email. Попробуйте ещё раз или отправьте '-' чтобы отменить.")
        return

    to_email = text.lower()
    tg_id = int(message.from_user.id)

    # 1) берём юзера+аккаунт из БД
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )).scalars().first()
        if not user:
            await message.answer("❌ Пользователь не найден в БД. Напишите /start и попробуйте ещё раз.")
            await state.clear()
            return

        user_id = int(user.id)

        accs = (await session.execute(
            select(EmailAccount).where(EmailAccount.user_id == user_id)
        )).scalars().all()
        accs = [a for a in accs if getattr(a, "status", "") == "active"]
        if not accs:
            await message.answer("❌ Нет активных email-аккаунтов для отправки. Добавьте аккаунт в 📮 Аккаунты.")
            await state.clear()
            return
        account = random.choice(accs)

        title_row = (await session.execute(
            select(Offer.title).where(Offer.title.is_not(None)).order_by(func.random()).limit(1)
        )).first()
        offer_title = title_row[0] if title_row and title_row[0] else ""

    subject = random.choice(TEST_SUBJECTS)
    body = await pick_random_smart_preset(message.from_user.id, offer_title)
    if not (body or "").strip():
        body = await pick_random_first_sms(message.from_user.id, offer_title)

    try:
        async with async_session() as session_proxy:
            ok, err = await send_email_via_account_with_proxy(
                session_proxy,
                user_id,
                account,
                to_email,
                subject,
                body,
            )
        if ok:
            await message.answer(
                f"✅ Тестовое письмо отправлено на: {to_email}\n"
                f"Тема: {subject}\n"
                f"От: {account.email}"
            )

            # 2) отдельная сессия БД для записи
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

        else:
            await message.answer(f"❌ [TESTMAIL v3] Ошибка отправки (От: {account.email}): {err}")
    except Exception as e:
        await message.answer(f"❌ [TESTMAIL v3] Ошибка отправки (От: {account.email}): {e}")
    finally:
        await state.clear()


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
    row = (await session.execute(
        select(Offer.link).where(Offer.link.is_not(None)).order_by(func.random()).limit(50)
    )).all()
    for (candidate,) in row:
        if _is_valid_ad_link(candidate):
            return candidate

    p = pathlib.Path("data/test_links.txt")
    if not p.exists():
        return None
    links = [x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip() and _is_valid_ad_link(x.strip())]
    return random.choice(links) if links else None


@router.message(Command("preview_imap"))
async def preview_imap_card(message: Message) -> None:
    """Демо-карточка входящего письма (тот же UI, что у IMAP)."""
    from services.incoming_mail_worker import render_mail_text_chunks, build_kb

    async with async_session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == int(message.from_user.id)).limit(1))
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
