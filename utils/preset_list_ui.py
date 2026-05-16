"""Общий экран списка текстовых пресетов (копируемые <code>, кнопки как в боте)."""

from __future__ import annotations

from html import escape
from typing import List

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

FOOTER_VARIABLES = "<b>Переменная:</b> <code>OFFER</code> / <code>{{OFFER}}</code>"
FOOTER_SPINTAX = "<b>Спинтаксис:</b> <code>{a|b|c}</code>"

NOTE_SMART_PRESETS = (
    "<i>При рассылке объединяются тексты отсюда и из меню пресетов с названием (умные шаблоны).</i>"
)
NOTE_REGULAR_PRESETS = (
    "<i>При рассылке объединяются тексты отсюда и из «Умных пресетов».</i>"
)


def render_text_presets_page(
    header_html: str,
    texts: List[str],
    *,
    empty_hint: str | None = None,
    footer_note: str | None = None,
    max_show: int = 40,
) -> str:
    if not texts:
        hint = empty_hint or "Пока нет пресетов.\nНажми «➕ Добавить пресет» и отправь текст одним сообщением."
        return f"{header_html}\n\n{hint}\n\n{FOOTER_VARIABLES}\n{FOOTER_SPINTAX}"

    lines: List[str] = [header_html, ""]
    for i, raw in enumerate(texts[:max_show], start=1):
        txt = escape((raw or "").strip().replace("\n", " "))
        if len(txt) > 500:
            txt = txt[:497] + "…"
        lines.append(f"<b>Пресет #{i}</b>\n<code>{txt}</code>\n")
    if len(texts) > max_show:
        lines.append(f"…и ещё {len(texts) - max_show}")
    lines.append("")
    lines.append(FOOTER_VARIABLES)
    lines.append(FOOTER_SPINTAX)
    if footer_note:
        lines.append(footer_note)
    return "\n".join(lines)


def text_presets_manage_kb(
    *,
    add_cb: str,
    edit_cb: str,
    del_cb: str,
    del_all_cb: str,
    back_cb: str,
    hide_cb: str,
    has_any: bool,
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="➕ Добавить пресет", callback_data=add_cb),
            InlineKeyboardButton(text="✏️ Изменить пресет", callback_data=edit_cb),
        ],
    ]
    if has_any:
        rows.append(
            [
                InlineKeyboardButton(text="🗑 Удалить пресет", callback_data=del_cb),
                InlineKeyboardButton(text="🗑 Удалить все", callback_data=del_all_cb),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb),
            InlineKeyboardButton(text="♻️ Скрыть", callback_data=hide_cb),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def text_presets_pick_kb(count: int, action: str, back_cb: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(min(count, 40)):
        rows.append([InlineKeyboardButton(text=f"Пресет #{i + 1}", callback_data=f"{action}:{i}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
