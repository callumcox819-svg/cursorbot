from aiogram import Router
from aiogram.types import Message
from aiogram.filters import CommandStart

from keyboards.main_menu import main_menu_kb
from database import Session
from services.users import get_or_create_user
from config import config

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, int(message.from_user.id))
        admin_ids = set(getattr(config, "ADMIN_IDS", []) or [])
        if int(message.from_user.id) not in admin_ids and not getattr(user, "access_granted", False):
            await message.answer("⛔ У тебя нет доступа к использованию этого бота. Обратись к администратору.")
            return

    text = (
        "👋 Привет! Это бот для рассылки и работы с email.\n\n"
        "Основные команды:\n"
        "/send — запустить рассылку\n"
        "/stopsend — остановить рассылку\n"
        "/status — статус рассылки\n\n"
        "Чтобы начать валидацию email — просто пришли сюда JSON-файл с объявлениями.\n\n"
        "Также ты можешь открыть ⚙️ Настройки и добавить аккаунты, домены, прокси и API-ключи."
    )
    await message.answer(text, reply_markup=main_menu_kb(message.from_user.id))
