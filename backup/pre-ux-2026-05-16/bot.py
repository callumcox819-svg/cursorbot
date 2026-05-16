import asyncio
import importlib
import logging
import os
import pkgutil
from typing import List, Tuple

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import config
from database import init_db

logger = logging.getLogger(__name__)


def _discover_handler_modules(package_name: str = "handlers") -> List[str]:
    pkg = importlib.import_module(package_name)
    module_names: List[str] = [package_name]

    if hasattr(pkg, "__path__"):
        for m in pkgutil.walk_packages(pkg.__path__, prefix=f"{package_name}."):
            module_names.append(m.name)

    module_names = list(dict.fromkeys(module_names))
    module_names_sorted = sorted(module_names)

    catchall = f"{package_name}.catchall_debug"
    if catchall in module_names_sorted:
        module_names_sorted = [x for x in module_names_sorted if x != catchall] + [catchall]

    return module_names_sorted


def _extract_routers(module, module_name: str) -> List[Router]:
    routers: List[Router] = []

    r = getattr(module, "router", None)
    if isinstance(r, Router):
        logger.info("Подключаю router из %s", module_name)
        routers.append(r)

    rs = getattr(module, "routers", None)
    if isinstance(rs, (list, tuple)):
        for x in rs:
            if isinstance(x, Router):
                logger.info("Подключаю router из %s (routers[])", module_name)
                routers.append(x)

    return routers


def _load_all_routers(package_name: str = "handlers") -> List[Tuple[str, Router]]:
    routers: List[Tuple[str, Router]] = []

    for mod_name in _discover_handler_modules(package_name):
        mod = importlib.import_module(mod_name)
        for r in _extract_routers(mod, mod_name):
            routers.append((mod_name, r))

    unique = []
    seen = set()
    for name, r in routers:
        if id(r) not in seen:
            unique.append((name, r))
            seen.add(id(r))

    return unique


async def _on_startup(bot: Bot) -> None:
    poll_seconds = int(os.getenv("INCOMING_MAIL_POLL_SECONDS", "20"))
    from services.incoming_mail_worker import start_incoming_mail_worker
    start_incoming_mail_worker(bot, poll_seconds=poll_seconds)
    logger.info("✅ Incoming mail worker стартовал (poll=%ss)", poll_seconds)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    await init_db()
    logger.info("✅ БД инициализирована (таблицы созданы при необходимости)")

    dp = Dispatcher()
    dp.startup.register(_on_startup)

    for mod_name, r in _load_all_routers("handlers"):
        dp.include_router(r)

    logger.info("✅ Bot started. Launching polling...")

    try:
        # ВАЖНО: убрать webhook
        await bot.delete_webhook(drop_pending_updates=True)

        # ❗❗❗ НЕ ИСПОЛЬЗУЕМ allowed_updates
        await dp.start_polling(bot)

    finally:
        await bot.session.close()
        logger.info("Bot stopped, session closed.")


if __name__ == "__main__":
    asyncio.run(main())
