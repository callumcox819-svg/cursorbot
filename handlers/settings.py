# handlers/settings.py
from __future__ import annotations

import json
import logging
import re
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest

from database import Session
from services.users import get_or_create_user
from services.user_settings import get_user_setting, set_user_setting
from keyboards.main_menu import main_menu_kb
from utils.callback_safe import callback_answer_safe

class SpoofNameState(StatesGroup):
    waiting_name = State()

router = Router()

# =========================
# Утилиты
# =========================

async def _safe_send(target, *args, **kwargs):
    """Safely await a coroutine OR call an async function with args/kwargs."""
    try:
        coro = target(*args, **kwargs) if callable(target) else target
        return await coro
    except TelegramBadRequest:
        return None
    except Exception:
        logger.exception("_safe_send")
        return None


async def _cq_edit_text(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str = "HTML",
) -> None:
    """Правка inline-меню: через bot.edit_message_text (не message.edit_text через _safe_send)."""
    msg = callback.message
    if msg is None:
        return
    try:
        await callback.bot.edit_message_text(
            text,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramBadRequest:
        pass


logger = logging.getLogger(__name__)

SETTINGS_MENU_TEXT = "Настройки"


def match_settings_menu_text(text: str | None) -> bool:
    """Кнопка «⚙️ Настройки» с главной клавиатуры (устойчиво к вариантам emoji)."""
    t = (text or "").strip().casefold().replace("\ufe0f", "")
    return "настройки" in t


async def open_settings_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        SETTINGS_MENU_TEXT,
        reply_markup=await _settings_menu_kb_for_user(message.from_user.id),
        parse_mode="HTML",
    )


# =========================
# HTML Nick
# =========================

SUBJECT_TEMPLATE_KEY = "subject_template"
HTML_THEME_KEY = "html_theme"
PROXY_ROTATION_KEY = "proxy_rotation"

HTMLNICK_KEY = "html_nick"
COUNTRY_KEY = "country"
TEAM_KEY = "team"

# GAG (CH) profile settings
# Stores: None (team default) or "1".."4" (personal domain slots mapped to API 5..8)
GAG_DOMAIN_SLOT_KEY = "gag_domain_slot"

async def load_html_nick(session: Session, tg_user_id: int) -> str | None:
    user = await get_or_create_user(session, tg_user_id)
    val = await get_user_setting(session, user, HTMLNICK_KEY)
    return (val or "").strip() or None

async def save_html_nick(session: Session, tg_user_id: int, value: str | None) -> None:
    user = await get_or_create_user(session, tg_user_id)
    v = (value or "").strip() or None
    await set_user_setting(session, user, HTMLNICK_KEY, v)


# =========================
# FSM for simple inputs (nick, timings)
# =========================


class _SettingsInput(StatesGroup):
    html_nick = State()
    subject_template = State()
    priority = State()
    html_theme = State()
    timings = State()


def settings_menu_kb(flags: dict[str, bool]) -> InlineKeyboardMarkup:
    """Главное меню настроек: Швейцария + GAG (без goo_*, тем, fast, html_mailer)."""
    def dot(on: bool, label: str) -> str:
        return ("🟢 " if on else "🔴 ") + label

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Приоритет\nотправки", callback_data="priority_menu"),
                InlineKeyboardButton(text="🧾 Пресеты", callback_data="presets_menu"),
            ],
            [
                InlineKeyboardButton(text=dot(flags.get("smart_mode", False), "Умный режим"), callback_data="ref_toggle:smart_mode"),
                InlineKeyboardButton(text="📄 Умные пресеты", callback_data="smart_presets_menu"),
            ],
            [
                InlineKeyboardButton(text=dot(flags.get("spoofing", False), "Спуфинг"), callback_data="ref_toggle:spoofing"),
                InlineKeyboardButton(text="👤 Имя для\nспуфинга", callback_data="spoof_name_menu"),
            ],
            [
                InlineKeyboardButton(text=dot(flags.get("block_control", False), "Контроль\nблокировок"), callback_data="ref_toggle:block_control"),
            ],
            [
                InlineKeyboardButton(text="📧 E-mail", callback_data="settings_accounts"),
                InlineKeyboardButton(text="🌐 Прокси", callback_data="settings_proxies"),
            ],
            [
                InlineKeyboardButton(text="🧮 Интервал", callback_data="settings_timings"),
            ],
            [
                InlineKeyboardButton(text=dot(flags.get("proxy_rotation", False), "Ротация"), callback_data="ref_toggle:proxy_rotation"),
                InlineKeyboardButton(text="🔑 Ключ", callback_data="gag_show:key"),
            ],
            [
                InlineKeyboardButton(text="🧾 Профиль", callback_data="gag_show:profile"),
                InlineKeyboardButton(text="🍀 Скрыть", callback_data="ref_hide"),
            ],
        ]
    )


async def _settings_menu_kb_for_user(tg_user_id: int) -> InlineKeyboardMarkup:
    """Return settings menu keyboard with current toggle states (per-user)."""
    async with Session() as session:
        user = await get_or_create_user(session, tg_user_id)

        async def _b(key: str, default: bool = False) -> bool:
            v = await get_user_setting(session, user, key)
            if v is None:
                return default
            s = str(v).strip().lower()
            return s in {"1", "true", "yes", "on", "y"}

        flags = {
            "smart_mode": await _b("smart_mode", False),
            "spoofing": await _b("spoofing", False),
            "block_control": await _b("block_control", False),
            "proxy_rotation": await _b("proxy_rotation", False),
        }

    return settings_menu_kb(flags)


@router.message(F.func(lambda m: match_settings_menu_text(getattr(m, "text", None))))
async def settings_open(message: Message, state: FSMContext) -> None:
    await open_settings_menu(message, state)


@router.callback_query(F.data == "spoof_name_menu")
async def spoof_name_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Меню установки имени (смены ника) для HTML, привязанное к выбранному сервису профиля."""
    await state.clear()
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        country = (await get_user_setting(session, user, COUNTRY_KEY) or "CH").strip().upper() or "CH"
        if country != "CH":
            return await callback.answer("Доступно только для Швейцарии", show_alert=True)

        service = (await get_user_setting(session, user, GAG_SERVICE_KEY) or "").strip()
        if not service:
            return await callback.answer("Сначала выберите сервис в профиле", show_alert=True)

        key = _html_nick_key_for_service(service)
        cur = (await get_user_setting(session, user, key) or "").strip()
        html_subj = (await get_user_setting(session, user, HTML_THEME_KEY) or "").strip() or "— не задано —"

    label = _service_label(service)
    cur_line = cur if cur else "— не задано —"
    text = (
        f"👤 <b>HTML: имя и тема</b>\n"
        f"Сервис: <b>{label}</b>\n"
        f"Имя отправителя (при 🟢 Спуфинг): <b>{cur_line}</b>\n\n"
        f"Используется только при отправке <b>HTML</b>.\n"
        f"Рассылка — отдельно: имя из «📧 E-mail», тема <code>OFFER</code>."
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Установить имя ({label})", callback_data="spoof_name_set")],
            [InlineKeyboardButton(text="📌 Тема для HTML", callback_data="html_theme_menu")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
        ]
    )
    text += f"\n\n📌 <b>Тема для HTML:</b> <code>{html_subj}</code>"
    await _safe_send(callback.message.edit_text, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "spoof_name_set")
async def spoof_name_set(callback: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        service = (await get_user_setting(session, user, GAG_SERVICE_KEY) or "").strip()
        if not service:
            return await callback.answer("Сначала выберите сервис в профиле", show_alert=True)
    await state.set_state(SpoofNameState.waiting_name)
    await state.update_data(service=service)
    await _safe_send(
        callback.message.edit_text,
        "Введите имя для спуфинга (смены ника) для HTML:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Отмена", callback_data="spoof_name_menu")]]),
    )
    await callback.answer()


@router.message(SpoofNameState.waiting_name)
async def spoof_name_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    service = (data.get("service") or "").strip()
    name = (message.text or "").strip()
    if not name:
        return await message.answer("Введите имя текстом.")
    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        key = _html_nick_key_for_service(service)
        await set_user_setting(session, user, key, name)
    await state.clear()
    # Переоткрываем меню, чтобы сразу было видно текущее значение
    fake_cb = type("obj", (), {"from_user": message.from_user, "message": message, "answer": (lambda *a, **k: None)})()
    await spoof_name_menu(fake_cb, state)


@router.callback_query(F.data == "settings_open")
async def settings_open_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback_answer_safe(callback)
    kb = await _settings_menu_kb_for_user(callback.from_user.id)
    await _cq_edit_text(callback, "Настройки", reply_markup=kb)


@router.callback_query(F.data == "settings_countries")
async def settings_countries(callback: CallbackQuery, state: FSMContext):
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        cur = (await get_user_setting(session, user, COUNTRY_KEY) or "CH").strip().upper() or "CH"

    def mark(code: str, name: str) -> str:
        return ("✅ " if cur == code else "") + name

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=mark("DE", "ГЕРМАНИЯ"), callback_data="country_set:DE")],
            [InlineKeyboardButton(text=mark("NO", "НОРВЕГИЯ"), callback_data="country_set:NO")],
            [InlineKeyboardButton(text=mark("CH", "ШВЕЙЦАРИЯ"), callback_data="country_set:CH")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
        ]
    )
    await callback.message.edit_text("🌍 <b>СТРАНЫ</b>\n\nВыберите страну:", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# =========================
# Missing callbacks from settings menu (Domains / Sender name / Templates / Timings / HTML nick)
# =========================


@router.callback_query(F.data == "settings_domains")
async def settings_domains(callback: CallbackQuery) -> None:
    """Open domains menu. We don't touch domains logic, only show its existing inline menu."""
    from handlers.domains import domains_menu_kb

    await callback.message.edit_text(
        "🌐 <b>Управление доменами</b>\n\nВыбери действие:",
        reply_markup=domains_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "sender_name_menu")
async def sender_name_menu(callback: CallbackQuery) -> None:
    """Show current sender name and provide existing 'sender_name_set' action."""
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        current = (getattr(user, "sender_name", None) or "—").strip() or "—"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Установить", callback_data="sender_name_set")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
        ]
    )
    await callback.message.edit_text(
        "📝 <b>Имя отправителя</b>\n\n"
        f"Текущее имя: <code>{current}</code>\n\n"
        "Нажми «Установить», чтобы задать другое.",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "settings_templates")
async def settings_templates(callback: CallbackQuery, state: FSMContext) -> None:
    """Open presets list (same UI as умные пресеты)."""
    from handlers.templates import presets_menu

    await presets_menu(callback, state)


@router.callback_query(F.data == "html_nick_menu")
async def html_nick_menu(callback: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        cur = await load_html_nick(session, callback.from_user.id)

    await state.clear()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Установить", callback_data="html_nick_set")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
        ]
    )

    await callback.message.edit_text(
        "📝 <b>Смена ника</b>\n\n"
        f"Текущий ник: <code>{cur or '—'}</code>\n\n"
        "Нажми «Установить», чтобы задать другой.",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "html_nick_set")
async def html_nick_set_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask user to send a new nick (HTML nick) after pressing 'Установить'."""
    await state.clear()
    await state.set_state(_SettingsInput.html_nick)

    await callback.message.edit_text(
        "📝 <b>Смена ника</b>\n\n"
        "Отправь новый ник одним сообщением (или «-», чтобы очистить).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")]]
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(_SettingsInput.html_nick)
async def html_nick_set(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ Пустое значение. Отправь ещё раз.")
        return
    if value == "-":
        value = ""
    async with Session() as session:
        await save_html_nick(session, message.from_user.id, value)
    await state.clear()
    await message.answer("✅ Сохранено.")


# =========================
# Timings (ONLY UI change: menu + "Изменить тайминг" button)
# =========================

@router.callback_query(F.data == "settings_timings")
async def settings_timings(callback: CallbackQuery, state: FSMContext) -> None:
    """Show timings menu (no immediate input)."""
    from services.settings import load_timing

    async with Session() as session:
        timing = await load_timing(session, callback.from_user.id)

    await state.clear()

    await callback.message.edit_text(
        "⏱ <b>Тайминги рассылки</b>\n\n"
        "Текущий диапазон:\n"
        f"MIN: <code>{timing.get('min')}</code> сек\n"
        f"MAX: <code>{timing.get('max')}</code> сек\n\n"
        " ",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Изменить тайминг", callback_data="timings_edit")],
                [InlineKeyboardButton(text=" ", callback_data="timings_fast_toggle")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
            ]
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "timings_edit")
async def timings_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask user to input MIN MAX after pressing 'Изменить тайминг'."""
    await state.clear()
    await state.set_state(_SettingsInput.timings)

    await callback.message.edit_text(
        "⏱ <b>Тайминги рассылки</b>\n\n"
        "Отправь двумя числами: <code>MIN MAX</code> (пример: <code>1 5</code>).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_timings")],
            ]
        ),
        parse_mode="HTML",
    )
    await callback.answer()




@router.message(_SettingsInput.timings)
async def timings_set(message: Message, state: FSMContext) -> None:
    from services.settings import load_timing, save_timing

    text = (message.text or "").strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)$", text)
    if not m:
        await message.answer("❌ Формат: MIN MAX (например: 1 5)")
        return
    mn = float(m.group(1))
    mx = float(m.group(2))
    if mn <= 0 or mx <= 0 or mx < mn:
        await message.answer("❌ Неверные значения. Нужно: 0 < MIN <= MAX")
        return

    async with Session() as session:
        cur = await load_timing(session, message.from_user.id)
        cur["min"] = int(mn) if mn.is_integer() else mn
        cur["max"] = int(mx) if mx.is_integer() else mx
        cur["min_delay"] = mn
        cur["max_delay"] = mx
        await save_timing(session, message.from_user.id, cur)

    await state.clear()
    await message.answer("✅ Сохранено.")


@router.callback_query(F.data.startswith("country_set:"))
async def country_set(callback: CallbackQuery, state: FSMContext):
    try:
        _, code = (callback.data or "").split(":", 1)
        code = (code or "").strip().upper()
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    if code not in ("DE", "NO", "CH"):
        return await callback.answer("Неизвестная страна", show_alert=True)

    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        await set_user_setting(session, user, COUNTRY_KEY, code)

    await settings_countries(callback, state)


@router.callback_query(F.data == "settings_back")
async def settings_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text(
            "Настройки",
            reply_markup=await _settings_menu_kb_for_user(callback.from_user.id),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer()


# --- GAG (Switzerland) profile & service ---
GAG_PROFILE_TITLE_KEY = "gag_profile_title"
GAG_PROFILE_NAME_KEY = "gag_profile_name"
GAG_PROFILE_ADDRESS_KEY = "gag_profile_address"
GAG_SERVICE_KEY = "gag_service"  # tutti_ch / post_ch



# Spoof name per CH service (used for HTML sending)
def _service_label(code: str) -> str:
    code = (code or "").strip()
    return {
        "tutti_ch": "ТУТТИ",
        "post_ch": "ПОСТ",
        "ricardo_ch": "Ricardo.ch",
    }.get(code, code or "—")


def _html_nick_key_for_service(service: str) -> str:
    service = (service or "").strip()
    return f"html_nick_{service}" if service else HTMLNICK_KEY


    
class GAGProfileState(StatesGroup):
    title = State()
    name = State()
    address = State()


@router.callback_query(F.data == "gag_profile_create")
async def gag_profile_create(callback: CallbackQuery, state: FSMContext) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        country = (await get_user_setting(session, user, COUNTRY_KEY) or "CH").strip().upper() or "CH"
        if country != "CH":
            return await callback.answer("Доступно только для Швейцарии", show_alert=True)

        cur_title = (await get_user_setting(session, user, GAG_PROFILE_TITLE_KEY) or "—").strip() or "—"
        cur_name = (await get_user_setting(session, user, GAG_PROFILE_NAME_KEY) or "—").strip() or "—"
        cur_addr = (await get_user_setting(session, user, GAG_PROFILE_ADDRESS_KEY) or "—").strip() or "—"

    await state.clear()
    await state.set_state(GAGProfileState.title)
    await callback.message.edit_text(
        "➕ <b>Создать профиль</b>\n\n"
        f"Текущее:\n• Название: <code>{cur_title}</code>\n• Имя: <code>{cur_name}</code>\n• Адрес: <code>{cur_addr}</code>\n\n"
        "Отправь <b>НАЗВАНИЕ ПРОФИЛЯ</b> одним сообщением.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")]]),
    )
    await callback.answer()


@router.message(GAGProfileState.title)
async def gag_profile_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ Название пустое. Отправь название профиля текстом.")
        return
    await state.update_data(title=title)
    await state.set_state(GAGProfileState.name)
    await message.answer("Отправь <b>ИМЯ ПОКУПАТЕЛЯ</b> одним сообщением.", parse_mode="HTML")


@router.message(GAGProfileState.name)
async def gag_profile_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Имя пустое. Отправь имя текстом.")
        return
    await state.update_data(name=name)
    await state.set_state(GAGProfileState.address)
    await message.answer("Отправь <b>АДРЕСС ПОКУПАТЕЛЯ</b> одним сообщением.", parse_mode="HTML")


@router.message(GAGProfileState.address)
async def gag_profile_address(message: Message, state: FSMContext) -> None:
    addr = (message.text or "").strip()
    if not addr:
        await message.answer("❌ Адрес пустой. Отправь адрес текстом.")
        return
    data = await state.get_data()
    title = (data.get("title") or "").strip()
    name = (data.get("name") or "").strip()

    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        await set_user_setting(session, user, GAG_PROFILE_TITLE_KEY, title)
        await set_user_setting(session, user, GAG_PROFILE_NAME_KEY, name)
        await set_user_setting(session, user, GAG_PROFILE_ADDRESS_KEY, addr)

    await state.clear()
    await message.answer("✅ Профиль сохранён.", reply_markup=main_menu_kb(message.from_user.id))


@router.callback_query(F.data == "gag_service_menu")
async def gag_service_menu(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        country = (await get_user_setting(session, user, COUNTRY_KEY) or "CH").strip().upper() or "CH"
        if country != "CH":
            return await callback.answer("Доступно только для Швейцарии", show_alert=True)
        cur = (await get_user_setting(session, user, GAG_SERVICE_KEY) or "").strip()

    def mark(service: str, label: str) -> str:
        return ("🟩 " if cur == service else "⬜️ ") + label

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=mark("tutti_ch", "ТУТТИ"), callback_data="gag_service_set:tutti_ch")],
            [InlineKeyboardButton(text=mark("post_ch", "ПОСТ"), callback_data="gag_service_set:post_ch")],
            [InlineKeyboardButton(text=mark("ricardo_ch", "Ricardo.ch"), callback_data="gag_service_set:ricardo_ch")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
        ]
    )
    await callback.message.edit_text(
        "🧭 <b>Выбор сервиса</b>\n\nВыбери сервис генерации ссылок:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("gag_service_set:"))
async def gag_service_set(callback: CallbackQuery) -> None:
    try:
        _, service = (callback.data or "").split(":", 1)
        service = (service or "").strip()
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    if service not in ("tutti_ch", "post_ch", "ricardo_ch"):
        return await callback.answer("Неизвестный сервис", show_alert=True)

    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        await set_user_setting(session, user, GAG_SERVICE_KEY, service)

    await gag_service_menu(callback)


# =========================
# GAG Domain slot (CH only)
# =========================


def _parse_gag_slot(raw: str | None) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.lower() in {"team", "0", "default"}:
        return None
    try:
        v = int(s)
    except Exception:
        return None
    return v if v in (1, 2, 3, 4) else None


@router.callback_query(F.data == "gag_domain_menu")
async def gag_domain_menu(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        country = (await get_user_setting(session, user, COUNTRY_KEY) or "CH").strip().upper() or "CH"
        if country != "CH":
            return await callback.answer("Доступно только для Швейцарии", show_alert=True)
        cur_slot = _parse_gag_slot(await get_user_setting(session, user, GAG_DOMAIN_SLOT_KEY))

    def mark_slot(slot: int) -> str:
        return ("✅ " if cur_slot == slot else "⬜️ ") + f"Домен {slot}"

    team_mark = "✅ Домен команды" if cur_slot is None else "⬜️ Домен команды"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=team_mark, callback_data="gag_domain_team")],
            [InlineKeyboardButton(text=mark_slot(1), callback_data="gag_domain_set:1")],
            [InlineKeyboardButton(text=mark_slot(2), callback_data="gag_domain_set:2")],
            [InlineKeyboardButton(text=mark_slot(3), callback_data="gag_domain_set:3")],
            [InlineKeyboardButton(text=mark_slot(4), callback_data="gag_domain_set:4")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="goo_show:profile")],
        ]
    )

    text = (
        "🌐 <b>Домен GAG</b>\n\n"
        "Выбери номер домена.\n"
        "• <b>Домен команды</b> — стандартный (по умолчанию)\n"
        "• <b>Домен 1–4</b> — личные домены (в API используются слоты 5–8: 1→5, 2→6, 3→7, 4→8)"
    )

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        # Avoid crashing on repeated taps (message is not modified)
        if "message is not modified" not in str(e).lower():
            raise
    await callback.answer()


@router.callback_query(F.data == "gag_domain_team")
async def gag_domain_team(callback: CallbackQuery) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        country = (await get_user_setting(session, user, COUNTRY_KEY) or "CH").strip().upper() or "CH"
        if country != "CH":
            return await callback.answer("Доступно только для Швейцарии", show_alert=True)
        # Team domain = default => store empty/None
        await set_user_setting(session, user, GAG_DOMAIN_SLOT_KEY, None)

    await gag_domain_menu(callback)


@router.callback_query(F.data.startswith("gag_domain_set:"))
async def gag_domain_set(callback: CallbackQuery) -> None:
    try:
        _, raw = (callback.data or "").split(":", 1)
        slot = int((raw or "").strip())
    except Exception:
        return await callback.answer("Неверные данные", show_alert=True)

    if slot not in (1, 2, 3, 4):
        return await callback.answer("Неизвестный домен", show_alert=True)

    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        country = (await get_user_setting(session, user, COUNTRY_KEY) or "CH").strip().upper() or "CH"
        if country != "CH":
            return await callback.answer("Доступно только для Швейцарии", show_alert=True)
        await set_user_setting(session, user, GAG_DOMAIN_SLOT_KEY, str(slot))

    await gag_domain_menu(callback)


# First SMS button was not inserted automatically


# =========================
# Reference menu toggles / stubs (1v1 UI)
# =========================

_REF_TOGGLE_KEYS = {
    "check_send": "check_send",
    "subj_insert": "subj_insert",
    "smart_mode": "smart_mode",
    "spoofing": "spoofing",
    "html_mailer": "html_mailer",
    "saver": "saver",
    "card": "card",
    "block_control": "block_control",
    "fast_send": "fast_send",
    "proxy_rotation": "proxy_rotation",
        "auto_reply_enabled": "auto_reply_enabled",
}


def _simple_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")]])


@router.callback_query(F.data.startswith("ref_toggle:"))
async def ref_toggle(callback: CallbackQuery):
    key = (callback.data or "").split(":", 1)[1].strip()
    db_key = _REF_TOGGLE_KEYS.get(key)
    if not db_key:
        return await callback.answer()

    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        cur = await get_user_setting(session, user, db_key)
        cur_s = str(cur or "").strip().lower()
        cur_b = cur_s in {"1", "true", "yes", "on", "y"}
        new_b = not cur_b
        await set_user_setting(session, user, db_key, "1" if new_b else "0")

    await callback_answer_safe(callback, "Готово ✅")
    kb = await _settings_menu_kb_for_user(callback.from_user.id)
    await _cq_edit_text(callback, "Настройки", reply_markup=kb)


@router.callback_query(F.data == "ref_hide")
async def ref_hide(callback: CallbackQuery):
    # Try delete message, else just remove buttons
    try:
        await callback.message.delete()
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("ref_open:"))
async def ref_open(callback: CallbackQuery, state: FSMContext):
    """Helper screens for reference menu items that are not full modules in this repo."""
    screen = (callback.data or "").split(":", 1)[1].strip()
    if screen == "commands":
        await state.clear()
        async with Session() as session:
            user = await get_or_create_user(session, callback.from_user.id)
            country = (await get_user_setting(session, user, COUNTRY_KEY) or "CH").strip().upper() or "CH"
            team = (await get_user_setting(session, user, TEAM_KEY) or "").strip().upper()

        if country == "CH":
            options = [("GAG", "GAG")]
        else:
            options = [("AQUA", "AQUA"), ("NURPP", "NURPP"), ("TSUM", "TSUM")]

        rows = []
        for code, title in options:
            mark = "✅ " if team == code else ""
            rows.append([InlineKeyboardButton(text=f"{mark}{title}", callback_data=f"team_pick:{code}")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")])

        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = (
            "⌨️ <b>Команды</b>\n\n"
            f"Страна: <b>{country}</b>\n"
            f"Команда: <b>{team or 'не выбрана'}</b>\n\n"
            "Выберите команду:"
        )
        await callback.message.edit_text(msg, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return
    if screen in {"themes", "themes_html"}:
        await state.clear()
        title = "📌 <b>Темы</b>" if screen == "themes" else "🏷 <b>Тема для HTML</b>"
        text = (
            f"{title}\n\n"
            "В этом проекте темы/шаблоны управляются через «🧾 Пресеты» и «🤖 Авто-ответ».\n"
            "Если нужно — добавлю отдельный менеджер тем 1в1 (лист/добавить/удалить/выбрать)."
        )
        await callback.message.edit_text(text, reply_markup=_simple_back_kb(), parse_mode="HTML")
        await callback.answer()
        return

    if screen == "smart_presets":
        await callback.answer()
        from handlers.templates import smart_presets_menu

        await smart_presets_menu(callback, state)
        return

    if screen in {"cases", "scenario_name", "rotation"}:
        await state.clear()
        labels = {
            "cases": "🟢 <b>Сценарии</b>",
            "scenario_name": "🧾 <b>Имя для сценариев</b>",
            "rotation": "🔄 <b>Ротация</b>",
        }
        text = (
            f"{labels.get(screen, 'ℹ️')}\n\n"
            "Этот раздел в твоём проекте пока не был реализован как отдельный экран.\n"
            "Если хочешь 1в1 — напиши, какие именно действия там должны быть (по видео), и я добавлю."
        )
        await callback.message.edit_text(text, reply_markup=_simple_back_kb(), parse_mode="HTML")
        await callback.answer()
        return

    # unknown
    await callback.answer("OK")


# =========================
# Команды (как на видео) — просто экран с командами + Назад
# =========================

@router.callback_query(F.data == "ref_open:commands")
async def ref_open_commands(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "⌨️ <b>Команды</b>\n\n"
        "/send — запустить рассылку\n"
        "/stop — остановить рассылку\n"
        "/status — статус\n\n"
        "Также: просто пришли JSON/TXT с объявлениями — бот провалидирует и сохранит в БД."
    )
    await _safe_send(callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")]]),
        parse_mode="HTML",
    ))
    await callback.answer()

# =========================
# Темы (OFFER)
# =========================

@router.callback_query(F.data == "themes_menu")
async def themes_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        cur = (await get_user_setting(session, user, SUBJECT_TEMPLATE_KEY) or "").strip()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="themes_edit")],
        [InlineKeyboardButton(text="🗑 Очистить", callback_data="themes_clear")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
    ])
    cur_show = cur if cur else "—"
    txt = (
        "📌 <b>Темы</b>\n\n"
        "Шаблон темы письма (поддерживает <code>OFFER</code>):\n"
        f"<code>{cur_show}</code>\n\n"
        "Пример: <code>OFFER | Antwort</code>"
    )
    await _safe_send(callback.message.edit_text(txt, reply_markup=kb, parse_mode="HTML"))
    await callback.answer()

@router.callback_query(F.data == "themes_edit")
async def themes_edit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(_SettingsInput.subject_template)
    await _safe_send(callback.message.edit_text(
        "📌 <b>Темы</b>\n\n"
        "Отправь шаблон темы. Используй <code>OFFER</code>.\n"
        "Чтобы удалить — отправь <code>-</code>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="themes_menu")]]),
        parse_mode="HTML",
    ))
    await callback.answer()

@router.message(_SettingsInput.subject_template)
async def themes_set(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if val == "-":
        val = ""
    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        await set_user_setting(session, user, SUBJECT_TEMPLATE_KEY, val)
    await state.clear()

    # Показываем экран "Темы" сразу после сохранения, чтобы было видно — установлено или нет.
    cur_show = val if val else "—"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="themes_edit")],
        [InlineKeyboardButton(text="🗑 Очистить", callback_data="themes_clear")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
    ])
    txt = (
        "📌 <b>Темы</b>\n\n"
        "Шаблон темы письма (поддерживает <code>OFFER</code>):\n"
        f"<code>{cur_show}</code>\n\n"
        "Пример: <code>OFFER | Antwort</code>"
    )
    await message.answer(txt, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "themes_clear")
async def themes_clear(callback: CallbackQuery, state: FSMContext):
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        await set_user_setting(session, user, SUBJECT_TEMPLATE_KEY, "")
    await callback.answer("Очищено ✅")
    await themes_menu(callback, state)

# =========================
# Тема для HTML (реально сохраняем html_theme)
# =========================

@router.callback_query(F.data == "html_theme_menu")
async def html_theme_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        cur = (await get_user_setting(session, user, HTML_THEME_KEY) or "").strip()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="html_theme_edit")],
        [InlineKeyboardButton(text="🗑 Очистить", callback_data="html_theme_clear")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="spoof_name_menu")],
    ])
    cur_show = cur if cur else "—"
    txt = (
        "📌 <b>Тема для HTML</b>\n\n"
        "Используется только при отправке <b>HTML</b> (не при массовой рассылке).\n"
        "Рассылка использует глобальный <code>OFFER</code> → название товара.\n\n"
        f"Текущее значение:\n<code>{cur_show}</code>"
    )
    await _safe_send(callback.message.edit_text(txt, reply_markup=kb, parse_mode="HTML"))
    await callback.answer()

@router.callback_query(F.data == "html_theme_edit")
async def html_theme_edit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(_SettingsInput.html_theme)
    await _safe_send(callback.message.edit_text(
        "🧾 <b>Тема для HTML</b>\n\nОтправь тему одной строкой.\n"
        "Чтобы удалить — отправь <code>-</code>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="spoof_name_menu")]]),
        parse_mode="HTML",
    ))
    await callback.answer()

@router.message(_SettingsInput.html_theme)
async def html_theme_set(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if val == "-":
        val = ""
    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        await set_user_setting(session, user, HTML_THEME_KEY, val)
    await state.clear()
    await message.answer(
        "✅ Тема для HTML сохранена.\nПример: <code>Your item sold</code>",
        reply_markup=await _settings_menu_kb_for_user(message.from_user.id),
    )

@router.callback_query(F.data == "html_theme_clear")
async def html_theme_clear(callback: CallbackQuery, state: FSMContext):
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        await set_user_setting(session, user, HTML_THEME_KEY, "")
    await callback.answer("Очищено ✅")
    await html_theme_menu(callback, state)

# =========================
# Ротация прокси — экран + переключатель proxy_rotation (без "0 действий")
# =========================

@router.callback_query(F.data == "proxy_rotation_menu")
async def proxy_rotation_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        v = await get_user_setting(session, user, PROXY_ROTATION_KEY)
        cur = str(v or "").strip().lower() in {"1","true","yes","on"}
    status = "✅" if cur else "❌"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔄 Ротация: {status}", callback_data="ref_toggle:proxy_rotation")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
    ])
    await _safe_send(callback.message.edit_text(
        "🔄 <b>Ротация</b>\n\n"
        "ВКЛ — прокси меняются между отправками.\n"
        "ВЫКЛ — один прокси.",
        reply_markup=kb,
        parse_mode="HTML",
    ))
    await callback.answer()

# =========================
# Приоритет доменов — сохраняем список доменов по порядку
# =========================

DOMAIN_PRIORITY_KEY = "domain_priority"

@router.callback_query(F.data == "priority_menu")
async def priority_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        raw = await get_user_setting(session, user, DOMAIN_PRIORITY_KEY)
        try:
            items = json.loads(raw) if raw else []
        except Exception:
            items = []
    if not isinstance(items, list):
        items = []

    if items:
        lst = "\n".join([f"{i+1}. <code>{d}</code>" for i, d in enumerate(items)])
    else:
        lst = "—"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить приоритет", callback_data="priority_edit")],
        [InlineKeyboardButton(text="🗑 Сбросить приоритет", callback_data="priority_reset")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
    ])
    await _safe_send(callback.message.edit_text(
        "📊 <b>Приоритет отправки</b>\n\n"
        "Домен №1 валидируется первым, потом №2 и т.д.\n\n"
        f"<b>Текущий приоритет:</b>\n{lst}",
        reply_markup=kb,
        parse_mode="HTML",
    ))
    await callback.answer()

@router.callback_query(F.data == "priority_edit")
async def priority_edit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(_SettingsInput.priority)
    await _safe_send(callback.message.edit_text(
        "📊 <b>Приоритет отправки</b>\n\n"
        "Отправь домены списком (каждый с новой строки).\n"
        "Пример:\n<code>gmx.de\ngmail.com\n...</code>\n\n"
        "Чтобы очистить — отправь <code>-</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="priority_menu")]]),
        parse_mode="HTML",
    ))
    await callback.answer()

@router.message(_SettingsInput.priority)
async def priority_set(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if txt == "-":
        items = []
    else:
        items = [re.sub(r"^https?://", "", x.strip().lower()) for x in txt.splitlines() if x.strip()]
    async with Session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        await set_user_setting(session, user, DOMAIN_PRIORITY_KEY, json.dumps(items))
    await state.clear()
    await message.answer("✅ Сохранено.", reply_markup=await _settings_menu_kb_for_user(message.from_user.id))

@router.callback_query(F.data == "priority_reset")
async def priority_reset(callback: CallbackQuery, state: FSMContext):
    async with Session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        await set_user_setting(session, user, DOMAIN_PRIORITY_KEY, json.dumps([]))
    await callback.answer("Сброшено ✅")
    await priority_menu(callback, state)

# =========================
# Ловим любые старые "назад" из старого меню, чтобы оно больше не всплывало
# =========================

@router.callback_query(F.data.in_({"settings_menu", "goo:settings", "goo_settings", "settings_main"}))
async def _force_settings_menu(callback: CallbackQuery, state: FSMContext):
    await settings_open_cb(callback, state)
