from aiogram import Router
from aiogram.types import Message, ReplyKeyboardRemove
from aiogram.filters import CommandStart

from keyboards.main_menu import main_menu_kb_for
from database import Session
from services.users import get_or_create_user
from services.bot_access import deny_access_message, user_has_bot_access

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with Session() as session:
        user = await get_or_create_user(session, int(message.from_user.id))
        if getattr(user, "is_banned", False):
            await message.answer(
                "⛔ Вы заблокированы администратором.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

    if not await user_has_bot_access(message.from_user.id):
        await deny_access_message(message)
        return

    text = (
        "👋 Привет! Это бот для рассылки и работы с email.\n\n"
        "Основные команды:\n"
        "/send — запустить рассылку\n"
        "/stop — остановить рассылку\n"
        "/stat — статус рассылки\n\n"
        "Чтобы начать валидацию email — просто пришли сюда JSON-файл с объявлениями.\n\n"
        "Также ты можешь открыть ⚙️ Настройки и добавить аккаунты, домены, прокси и API-ключи."
    )
    await message.answer(text, reply_markup=await main_menu_kb_for(message.from_user.id))
