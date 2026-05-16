import io
import json
import logging
from typing import Any

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from keyboards.main_menu import main_menu_kb

# ✅ ВАЖНО: вот этот импорт должен существовать в проекте
# services/validemail_validator.py должен содержать функцию validate_offers(...)
from services.validemail_validator import validate_offers

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("validate"))
async def validate_hint(message: Message) -> None:
    await message.answer(
        "Пришли JSON-файл документом — я запущу валидацию.",
        reply_markup=main_menu_kb(message.from_user.id),
    )


@router.message(lambda m: m.document is not None and (m.document.file_name or "").lower().endswith(".json"))
async def handle_json_document(message: Message) -> None:
    """
    Принимаем JSON документ, читаем, валидируем через validemail,
    результат возвращаем файлом.
    """
    await message.answer("📥 Файл принят, запускаю обработку...", reply_markup=main_menu_kb(message.from_user.id))

    doc = message.document
    if not doc:
        return

    # Aiogram 3: правильное скачивание файла
    try:
        file = await message.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await message.bot.download_file(file.file_path, destination=buf)
        raw = buf.getvalue()
    except Exception as e:
        logger.exception("Download error")
        await message.answer(f"❌ Ошибка скачивания файла:\n<code>{e}</code>", parse_mode="HTML")
        return

    # Декодируем + парсим JSON
    try:
        try:
            text = raw.decode("utf-8")
        except Exception:
            text = raw.decode("latin-1")
        data = json.loads(text)
    except Exception as e:
        await message.answer(f"❌ Ошибка чтения JSON:\n<code>{e}</code>", parse_mode="HTML")
        return

    # Приводим к списку items
    items: list[dict[str, Any]] = []
    if isinstance(data, list):
        items = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        if isinstance(data.get("items"), list):
            items = [x for x in data["items"] if isinstance(x, dict)]
        else:
            # ищем первый list в значениях
            for v in data.values():
                if isinstance(v, list):
                    items = [x for x in v if isinstance(x, dict)]
                    break

    if not items:
        await message.answer("❌ В JSON не найден массив записей (items).")
        return

    # ✅ Валидация (логика внутри services.validemail_validator)
    try:
        result = await validate_offers(
            telegram_id=message.from_user.id,
            offers=items,
            bot=message.bot,
            chat_id=message.chat.id,
        )
    except Exception as e:
        logger.exception("validate_offers failed")
        await message.answer(f"❌ Ошибка при валидации:\n<code>{e}</code>", parse_mode="HTML")
        return

    # result должен быть dict с:
    # - "stats" (summary)
    # - "output_json_bytes" (готовый файл)
    summary = result.get("summary_text") or "✅ Валидация завершена."
    out_bytes = result.get("output_json_bytes")
    out_name = result.get("output_filename") or f"validated_{message.from_user.id}.json"

    await message.answer(summary)

    if out_bytes:
        await message.answer_document(
            document=(out_name, out_bytes),
            caption="📎 Файл с результатами валидации.",
        )
