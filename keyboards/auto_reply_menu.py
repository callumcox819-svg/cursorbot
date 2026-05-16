from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def auto_reply_menu(cfg: dict, line_idx: int = 0) -> InlineKeyboardMarkup:
    enabled = bool(cfg.get("enabled", False))
    auto_create_link = bool(cfg.get("auto_create_link", False))
    auto_send = bool(cfg.get("auto_send", False))

    lines = cfg.get("lines") or []
    if not isinstance(lines, list) or not lines:
        lines = [
            {
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
        ]

    line_idx = max(0, min(line_idx, len(lines) - 1))
    ln = lines[line_idx] if isinstance(lines[line_idx], dict) else {}

    def onoff(v: bool) -> str:
        return "🟢 ВКЛ" if v else "🔴 ВЫКЛ"

    kw = (ln.get("keywords") or "").strip()
    sw = (ln.get("stopwords") or "").strip()
    html_template = (ln.get("html_template") or "pickup.html").strip()
    reply_mode = (ln.get("reply_mode") or "html").lower()
    line_enabled = bool(ln.get("enabled", True))
    gen_link_enabled = bool(ln.get("gen_link_enabled", True))

    t = ln.get("timings") or {}
    gen_t = int((t or {}).get("gen_link", 0) or 0)
    tpl_t = int((t or {}).get("send_template", 0) or 0)
    rep_t = int((t or {}).get("send_reply", 0) or 0)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️", callback_data="auto_line_prev"),
                InlineKeyboardButton(text=f"Линия {line_idx + 1}/{len(lines)}", callback_data="auto_noop"),
                InlineKeyboardButton(text="➡️", callback_data="auto_line_next"),
            ],

            [InlineKeyboardButton(text=f"Авто-ответ: {onoff(enabled)}", callback_data="auto_toggle_enabled")],
            [InlineKeyboardButton(text=f"Авто-создание ссылки: {onoff(auto_create_link)}", callback_data="auto_toggle_link")],
            [InlineKeyboardButton(text=f"Авто-отправка: {onoff(auto_send)}", callback_data="auto_toggle_send")],

            [InlineKeyboardButton(text=f"Линия активна: {onoff(line_enabled)}", callback_data="auto_toggle_line_enabled")],
            [InlineKeyboardButton(text=f"Генерация ссылки (эта линия): {onoff(gen_link_enabled)}", callback_data="auto_toggle_line_gen")],

            [
                InlineKeyboardButton(text=f"Кейворды: {('✅' if kw else '—')}", callback_data="auto_edit_keywords"),
                InlineKeyboardButton(text=f"Стоп-слова: {('⛔' if sw else '—')}", callback_data="auto_edit_stopwords"),
            ],

            # ✅ выбор HTML для текущей линии
            [InlineKeyboardButton(text=f"HTML: {html_template}", callback_data="auto_pick_html")],

            [InlineKeyboardButton(text="⚡ Шаблон (из «Шаблоны»)", callback_data="auto_pick_template")],

            [
                InlineKeyboardButton(text=f"⏱ Тайминг: ссылка {gen_t}s", callback_data="auto_timing_gen"),
                InlineKeyboardButton(text=f"⏱ Тайминг: шаблон {tpl_t}s", callback_data="auto_timing_tpl"),
            ],
            [InlineKeyboardButton(text=f"⏱ Тайминг: ответ {rep_t}s", callback_data="auto_timing_reply")],

            [
                InlineKeyboardButton(text="➕ Добавить линию", callback_data="auto_line_add"),
                InlineKeyboardButton(text="➖ Удалить линию", callback_data="auto_line_del"),
            ],

            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_open")],
        ]
    )
