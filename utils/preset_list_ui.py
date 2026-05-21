"""Общий экран списка текстовых пресетов (копируемые <code>, кнопки как в боте)."""

from __future__ import annotations

from html import escape
from typing import List

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

FOOTER_VARIABLES = "<b>Переменная:</b> <code>OFFER</code> / <code>{{OFFER}}</code>"
FOOTER_SPINTAX = "<b>Спинтаксис:</b> <code>{a|b|c}</code>"

NOTE_SMART_PRESETS = (
    "<i>Только эти тексты идут в рассылку /send и тест-почту. 🧾 Пресеты — для ответа на входящее письмо.</i>"
)
NOTE_REGULAR_PRESETS = (
    "<i>Название — на кнопках при быстром ответе на письмо. Текст — уходит получателю.</i>"
)

REGULAR_PRESETS_EMPTY_HINT = (
    "Пока нет пресетов.\n"
    "Нажми «➕ Добавить пресет»: сначала имя для кнопки, затем текст письма."
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


def render_named_presets_page(
    header_html: str,
    items: list[tuple[str, str]],
    *,
    empty_hint: str | None = None,
    footer_note: str | None = None,
    max_show: int = 40,
) -> str:
    """Пресеты с названием (ответы на письма): название + текст в списке."""
    if not items:
        hint = empty_hint or REGULAR_PRESETS_EMPTY_HINT
        out = f"{header_html}\n\n{hint}"
        if footer_note:
            out += f"\n\n{footer_note}"
        return out

    lines: List[str] = [header_html, ""]
    for i, (title, body) in enumerate(items[:max_show], start=1):
        name = escape((title or "").strip())
        txt = escape((body or "").strip().replace("\n", " "))
        if len(txt) > 500:
            txt = txt[:497] + "…"
        lines.append(f"<b>Пресет #{i}</b> · <u>{name}</u>\n<code>{txt}</code>\n")
    if len(items) > max_show:
        lines.append(f"…и ещё {len(items) - max_show}")
    if footer_note:
        lines.append(f"\n{footer_note}")
    return "\n".join(lines)


def named_presets_pick_kb(
    items: list[tuple[str, str]],
    action: str,
    back_cb: str,
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for i, (title, _) in enumerate(items[:40]):
        label = (title or f"Пресет #{i + 1}").strip()[:40]
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{action}:{i}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def text_presets_pick_kb(count: int, action: str, back_cb: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(min(count, 40)):
        rows.append([InlineKeyboardButton(text=f"Пресет #{i + 1}", callback_data=f"{action}:{i}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
