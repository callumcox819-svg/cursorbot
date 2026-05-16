def accounts_menu_kb(accounts: List[EmailAccount]) -> InlineKeyboardMarkup:
    """
    Меню почтовых аккаунтов в настройках.

    Нажатие по почте НЕ удаляет аккаунт.
    Удаление только по кнопке 🗑.
    """
    rows = []

    for acc in accounts:
        emoji = "🟢" if acc.is_active and acc.status == "active" else "🔴"
        text = f"{emoji} {acc.email}"

        # Левая кнопка — просто инфа / клик без удаления
        email_btn = InlineKeyboardButton(
            text=text,
            callback_data=f"acc_info:{acc.id}",   # <<< ВАЖНО: НЕ acc_del
        )

        # Правая кнопка — именно удаление
        delete_btn = InlineKeyboardButton(
            text="🗑",
            callback_data=f"acc_del:{acc.id}",
        )

        rows.append([email_btn, delete_btn])

    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить аккаунт",
                callback_data="accounts_add_menu",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="settings_back",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)
