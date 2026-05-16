from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message

from database import Session
from services.users import get_or_create_user
from services.user_settings import get_user_setting, set_user_setting
from keyboards.auto_reply_menu import auto_reply_menu

router = Router()

AUTO_KEY = "auto_reply_cfg"
HTML_DIR = Path("data/html")
DATA_DIR = Path("data")

# pending edits per user: {"mode": "keywords|stopwords|timing", "key": "..."}
_PENDING: Dict[int, Dict[str, Any]] = {}

_INT_RE = re.compile(r"^\s*(\d{1,3})\s*$")


def _default_line() -> dict:
    return {
        "enabled": True,
        "keywords": "",
        "stopwords": "",
        "price_add": 0,
        "reply_mode": "html",
        "html_template": "pickup.html",
        "template_idx": None,
        "gen_link_enabled": True,
        "timings": {"gen_link": 0, "send_template": 0, "send_reply": 0},
    }


def _default_cfg() -> dict:
    return {
        "enabled": False,
        "auto_create_link": False,
        "auto_send": False,
        "current_line": 0,
        "lines": [_default_line()],
    }


def _load_cfg(raw: Optional[str]) -> dict:
    if not raw:
        return _default_cfg()
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            base = _default_cfg()
            base.update(d)
            if not isinstance(base.get("lines"), list) or not base["lines"]:
                base["lines"] = [_default_line()]
            return base
    except Exception:
        pass
    return _default_cfg()


async def _get_cfg(tg_id: int) -> dict:
    async with Session() as session:
        u = await get_or_create_user(session, tg_id)
        raw = await get_user_setting(session, u, AUTO_KEY)
        return _load_cfg(raw)


async def _save_cfg(tg_id: int, cfg: dict) -> None:
    async with Session() as session:
        u = await get_or_create_user(session, tg_id)
        await set_user_setting(session, u, AUTO_KEY, json.dumps(cfg, ensure_ascii=False))


def _cur_line(cfg: dict) -> int:
    try:
        idx = int(cfg.get("current_line", 0))
    except Exception:
        idx = 0
    lines = cfg.get("lines") or []
    if not isinstance(lines, list) or not lines:
        return 0
    return max(0, min(idx, len(lines) - 1))


async def _render(callback: CallbackQuery, cfg: dict) -> None:
    idx = _cur_line(cfg)
    text = (
        "🤖 <b>Авто-ответ</b>\n\n"
        "⚠️ ВАЖНО: авто-отправка работает только если найдены кейворды.\n"
        "Если кейвордов нет/не найдено — бот молчит.\n"
    )
    await callback.message.edit_text(text, reply_markup=auto_reply_menu(cfg, idx), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


# ✅ ЛОВИМ ОБЕ КНОПКИ (settings_auto иногда приходит как settings_auto_reply)
@router.callback_query(F.data.in_({"settings_auto", "settings_auto_reply"}))
async def open_auto(callback: CallbackQuery):
    cfg = await _get_cfg(callback.from_user.id)
    await _render(callback, cfg)


@router.callback_query(F.data == "auto_noop")
async def auto_noop(callback: CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass


# ===== GLOBAL toggles =====
@router.callback_query(F.data == "auto_toggle_enabled")
async def auto_toggle_enabled(callback: CallbackQuery):
    cfg = await _get_cfg(callback.from_user.id)
    cfg["enabled"] = not bool(cfg.get("enabled", False))
    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)


@router.callback_query(F.data == "auto_toggle_link")
async def auto_toggle_link(callback: CallbackQuery):
    cfg = await _get_cfg(callback.from_user.id)
    cfg["auto_create_link"] = not bool(cfg.get("auto_create_link", False))
    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)


@router.callback_query(F.data == "auto_toggle_send")
async def auto_toggle_send(callback: CallbackQuery):
    cfg = await _get_cfg(callback.from_user.id)
    cfg["auto_send"] = not bool(cfg.get("auto_send", False))
    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)


# ===== line navigation =====
@router.callback_query(F.data == "auto_line_prev")
async def auto_line_prev(callback: CallbackQuery):
    cfg = await _get_cfg(callback.from_user.id)
    lines = cfg.get("lines") or []
    if not isinstance(lines, list) or not lines:
        cfg["lines"] = [_default_line()]
        lines = cfg["lines"]
    idx = _cur_line(cfg)
    cfg["current_line"] = max(0, idx - 1)
    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)


@router.callback_query(F.data == "auto_line_next")
async def auto_line_next(callback: CallbackQuery):
    cfg = await _get_cfg(callback.from_user.id)
    lines = cfg.get("lines") or []
    if not isinstance(lines, list) or not lines:
        cfg["lines"] = [_default_line()]
        lines = cfg["lines"]
    idx = _cur_line(cfg)
    cfg["current_line"] = min(len(lines) - 1, idx + 1)
    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)


# ===== line toggles =====
@router.callback_query(F.data == "auto_toggle_line_enabled")
async def auto_toggle_line_enabled(callback: CallbackQuery):
    cfg = await _get_cfg(callback.from_user.id)
    idx = _cur_line(cfg)
    lines = cfg.get("lines") or []
    if not isinstance(lines, list) or not lines:
        await _render(callback, cfg)
        return
    ln = lines[idx]
    if isinstance(ln, dict):
        ln["enabled"] = not bool(ln.get("enabled", True))
    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)


@router.callback_query(F.data == "auto_toggle_line_gen")
async def auto_toggle_line_gen(callback: CallbackQuery):
    cfg = await _get_cfg(callback.from_user.id)
    idx = _cur_line(cfg)
    lines = cfg.get("lines") or []
    if not isinstance(lines, list) or not lines:
        await _render(callback, cfg)
        return
    ln = lines[idx]
    if isinstance(ln, dict):
        ln["gen_link_enabled"] = not bool(ln.get("gen_link_enabled", True))
    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)


# ===== edit keywords / stopwords =====
@router.callback_query(F.data == "auto_edit_keywords")
async def auto_edit_keywords(callback: CallbackQuery):
    _PENDING[callback.from_user.id] = {"mode": "keywords"}
    await callback.message.answer(
        "✏️ Отправь новые <b>кейворды</b> через запятую.\nНапример: <code>ja, noch da</code>",
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data == "auto_edit_stopwords")
async def auto_edit_stopwords(callback: CallbackQuery):
    _PENDING[callback.from_user.id] = {"mode": "stopwords"}
    await callback.message.answer(
        "✏️ Отправь новые <b>стоп-слова</b> через запятую.\nНапример: <code>spam, test</code>",
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


# ===== timings =====
def _ask_seconds(text: str) -> str:
    return text + "\n\nОтправь число секунд (0..120). Например: <code>3</code>"


@router.callback_query(F.data == "auto_timing_gen")
async def auto_timing_gen(callback: CallbackQuery):
    _PENDING[callback.from_user.id] = {"mode": "timing", "key": "gen_link"}
    await callback.message.answer(_ask_seconds("⏱ Тайминг для <b>создания ссылки</b>."), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data == "auto_timing_tpl")
async def auto_timing_tpl(callback: CallbackQuery):
    _PENDING[callback.from_user.id] = {"mode": "timing", "key": "send_template"}
    await callback.message.answer(_ask_seconds("⏱ Тайминг для <b>отправки шаблона</b>."), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data == "auto_timing_reply")
async def auto_timing_reply(callback: CallbackQuery):
    _PENDING[callback.from_user.id] = {"mode": "timing", "key": "send_reply"}
    await callback.message.answer(_ask_seconds("⏱ Тайминг для <b>отправки ответа</b>."), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


def _auto_reply_pending(message: Message) -> bool:
    return message.from_user.id in _PENDING


@router.message(F.func(_auto_reply_pending))
async def _pending_text_input(message: Message):
    pending = _PENDING.get(message.from_user.id)
    if not pending:
        return

    mode = pending.get("mode")
    txt = (message.text or "").strip()

    cfg = await _get_cfg(message.from_user.id)
    idx = _cur_line(cfg)
    lines = cfg.get("lines") or []
    if not isinstance(lines, list) or not lines:
        lines = [_default_line()]
        cfg["lines"] = lines

    if not isinstance(lines[idx], dict):
        lines[idx] = _default_line()
    ln = lines[idx]

    if mode in ("keywords", "stopwords"):
        ln[mode] = txt
        await _save_cfg(message.from_user.id, cfg)
        _PENDING.pop(message.from_user.id, None)
        await message.answer("✅ Сохранено.")
        await message.answer(
            "🤖 <b>Авто-ответ</b>\n\n⚠️ ВАЖНО: авто-отправка работает только если найдены кейворды.",
            reply_markup=auto_reply_menu(cfg, _cur_line(cfg)),
            parse_mode="HTML",
        )
        return

    if mode == "timing":
        m = _INT_RE.match(txt)
        if not m:
            await message.answer("❌ Нужна цифра. Например: <code>3</code>", parse_mode="HTML")
            return
        val = int(m.group(1))
        if val < 0 or val > 120:
            await message.answer("❌ Диапазон 0..120", parse_mode="HTML")
            return

        key = str(pending.get("key") or "")
        t = ln.get("timings")
        if not isinstance(t, dict):
            t = {"gen_link": 0, "send_template": 0, "send_reply": 0}
            ln["timings"] = t
        t[key] = val

        await _save_cfg(message.from_user.id, cfg)
        _PENDING.pop(message.from_user.id, None)
        await message.answer("✅ Тайминг сохранён.")
        await message.answer(
            "🤖 <b>Авто-ответ</b>\n\n⚠️ ВАЖНО: авто-отправка работает только если найдены кейворды.",
            reply_markup=auto_reply_menu(cfg, _cur_line(cfg)),
            parse_mode="HTML",
        )
        return

    _PENDING.pop(message.from_user.id, None)


# ===== add/del lines =====
@router.callback_query(F.data == "auto_line_add")
async def auto_line_add(callback: CallbackQuery):
    cfg = await _get_cfg(callback.from_user.id)
    lines = cfg.get("lines") or []
    if not isinstance(lines, list):
        lines = []
    lines.append(_default_line())
    cfg["lines"] = lines
    cfg["current_line"] = len(lines) - 1
    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)


@router.callback_query(F.data == "auto_line_del")
async def auto_line_del(callback: CallbackQuery):
    cfg = await _get_cfg(callback.from_user.id)
    lines = cfg.get("lines") or []
    if not isinstance(lines, list) or len(lines) <= 1:
        try:
            await callback.answer("Нельзя удалить последнюю линию", show_alert=True)
        except Exception:
            pass
        await _render(callback, cfg)
        return

    idx = _cur_line(cfg)
    lines.pop(idx)
    cfg["lines"] = lines
    cfg["current_line"] = max(0, min(idx, len(lines) - 1))
    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)


# ---------- HTML picker for line ----------
def _html_list_kb(files: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for f in files[:30]:
        rows.append([InlineKeyboardButton(text=f"📄 {f}", callback_data=f"auto_set_html:{f}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_auto")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "auto_pick_html")
async def auto_pick_html(callback: CallbackQuery):
    files = []
    try:
        if HTML_DIR.exists():
            files = sorted([x.name for x in HTML_DIR.iterdir() if x.is_file() and x.suffix.lower() == ".html"])
    except Exception:
        files = []
    if not files:
        await callback.answer("Не нашёл html в data/html", show_alert=True)
        return

    await callback.message.edit_text("📄 Выбери HTML для этой линии:", reply_markup=_html_list_kb(files))
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("auto_set_html:"))
async def auto_set_html(callback: CallbackQuery):
    _, fname = (callback.data or "").split(":", 1)
    fname = (fname or "").strip()

    cfg = await _get_cfg(callback.from_user.id)
    idx = _cur_line(cfg)
    lines = cfg.get("lines") or []
    if isinstance(lines, list) and lines and isinstance(lines[idx], dict):
        lines[idx]["html_template"] = fname
    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)


# ---------- TEMPLATE picker for line ----------
@dataclass
class _Tpl:
    title: str
    text: str


async def _load_templates(tg_id: int) -> list[_Tpl]:
    from handlers.templates import load_templates

    return [_Tpl(title=t.title, text=t.text) for t in await load_templates(int(tg_id))]


def _templates_pick_kb(items: list[_Tpl]) -> InlineKeyboardMarkup:
    rows = []
    for i, t in enumerate(items[:30]):
        rows.append([InlineKeyboardButton(text=f"⚡ {t.title}", callback_data=f"auto_set_template:{i}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_auto")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "auto_pick_template")
async def auto_pick_template(callback: CallbackQuery):
    items = await _load_templates(callback.from_user.id)
    if not items:
        await callback.answer("Нет шаблонов. Добавь их в меню «⚡ Шаблоны».", show_alert=True)
        return

    await callback.message.edit_text(
        "⚡ Выбери шаблон для этой линии:",
        reply_markup=_templates_pick_kb(items),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("auto_set_template:"))
async def auto_set_template(callback: CallbackQuery):
    _, s_idx = (callback.data or "").split(":", 1)
    try:
        tpl_idx = int(s_idx)
    except Exception:
        await callback.answer("Некорректный шаблон", show_alert=True)
        return

    items = await _load_templates(callback.from_user.id)
    if tpl_idx < 0 or tpl_idx >= len(items):
        await callback.answer("Шаблон не найден", show_alert=True)
        return

    cfg = await _get_cfg(callback.from_user.id)
    line_idx = _cur_line(cfg)
    lines = cfg.get("lines") or []
    if isinstance(lines, list) and lines and isinstance(lines[line_idx], dict):
        lines[line_idx]["template_idx"] = tpl_idx

    await _save_cfg(callback.from_user.id, cfg)
    await _render(callback, cfg)
