import asyncio
import importlib
import logging
import os
import pkgutil
import sys
from pathlib import Path
from typing import List, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent
from aiogram.exceptions import TelegramConflictError

from config import config
from database import init_db

logger = logging.getLogger(__name__)

_PID_FILE = Path(__file__).resolve().parent / ".happy88_bot.pid"

# settings/send раньше тяжёлых роутеров; catchall — всегда последним
_ROUTER_BOOT_ORDER: Tuple[str, ...] = (
    "handlers.start",
    "handlers.settings",
    "handlers.send",
    "handlers.stopsend",
)


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


def _sort_routers(routers: List[Tuple[str, Router]]) -> List[Tuple[str, Router]]:
    priority = {name: i for i, name in enumerate(_ROUTER_BOOT_ORDER)}
    catchall = "handlers.catchall_debug"

    def key(item: Tuple[str, Router]) -> Tuple[int, str]:
        mod_name, _ = item
        if mod_name == catchall:
            return (10_000, mod_name)
        if mod_name in priority:
            return (priority[mod_name], mod_name)
        return (100, mod_name)

    return sorted(routers, key=key)


def _bind_priority_dispatcher_handlers(dp: Dispatcher) -> None:
    """
    Главное reply-меню и inline «Настройки» на корне Dispatcher — первыми в очереди.
    """
    from aiogram.filters import Command
    from aiogram.types import Message

    from handlers.accounts import open_accounts_from_settings, quick_gmail_from_main_menu
    from handlers.api_keys import goo_show_key, goo_show_profile
    from handlers.first_sms import firstsms_open
    from handlers.proxies import open_proxies
    from handlers.send import send_cmd
    from handlers.settings import (
        _force_settings_menu,
        match_settings_menu_text,
        open_settings_menu,
        priority_menu,
        ref_hide,
        ref_toggle,
        settings_open_cb,
        settings_timings,
        spoof_name_menu,
    )
    from handlers.stopsend import cmd_stopsend
    from handlers.status import cmd_statussend
    from handlers.templates import presets_menu

    async def _dp_settings_message(message: Message, state: FSMContext) -> None:
        logger.info("⚙️ settings (dispatcher) tg=%s", message.from_user.id)
        await open_settings_menu(message, state)

    dp.message.register(
        _dp_settings_message,
        F.func(lambda m: match_settings_menu_text(getattr(m, "text", None))),
    )

    dp.message.register(send_cmd, Command("send"))
    dp.message.register(send_cmd, F.text == "▶️ Запустить рассылку")
    dp.message.register(cmd_stopsend, Command("stop", "stopsend"))
    dp.message.register(
        cmd_stopsend,
        F.text.in_({"⏹ Остановить рассылку", "/stop", "/stopsend"}),
    )
    dp.message.register(cmd_statussend, Command("stat", "status", "statussend"))
    dp.message.register(cmd_statussend, F.text == "📊 Статус рассылки")
    dp.message.register(
        quick_gmail_from_main_menu,
        F.text.in_({"⚡ Быстрое добавление", "⚡ Быстрое добавление (Gmail)"}),
    )

    _deprecated = frozenset({"settings_menu", "goo:settings", "goo_settings", "settings_main"})
    bindings = (
        (settings_open_cb, F.data == "settings_open"),
        (priority_menu, F.data == "priority_menu"),
        (presets_menu, F.data == "presets_menu"),
        (firstsms_open, F.data == "firstsms_open"),
        (spoof_name_menu, F.data == "spoof_name_menu"),
        (open_accounts_from_settings, F.data == "settings_accounts"),
        (open_proxies, F.data == "settings_proxies"),
        (settings_timings, F.data == "settings_timings"),
        (goo_show_key, F.data.in_({"goo_show:key", "gag_show:key"})),
        (goo_show_profile, F.data.in_({"goo_show:profile", "gag_show:profile"})),
        (ref_hide, F.data == "ref_hide"),
        (_force_settings_menu, F.data.in_(_deprecated)),
        (ref_toggle, F.data.startswith("ref_toggle:")),
    )
    for cb, flt in bindings:
        dp.callback_query.register(cb, flt)

    logger.info(
        "Привязано к Dispatcher: reply-меню (send/stop/stat/настройки/быстрое) + %d callback настроек",
        len(bindings),
    )


def _load_all_routers(package_name: str = "handlers") -> List[Tuple[str, Router]]:
    routers: List[Tuple[str, Router]] = []

    for mod_name in _discover_handler_modules(package_name):
        mod = importlib.import_module(mod_name)
        for r in _extract_routers(mod, mod_name):
            routers.append((mod_name, r))

    unique: List[Tuple[str, Router]] = []
    seen: set[int] = set()
    for name, r in routers:
        if id(r) not in seen:
            unique.append((name, r))
            seen.add(id(r))

    return _sort_routers(unique)


def _acquire_single_instance_lock() -> None:
    """Не даём запустить второй bot.py с тем же токеном на этом ПК."""
    try:
        import psutil
    except ImportError:
        logger.warning("psutil не установлен — защита от второго экземпляра отключена")
        return

    my_pid = os.getpid()
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text(encoding="utf-8").strip())
        except ValueError:
            old_pid = 0
        if old_pid and old_pid != my_pid and psutil.pid_exists(old_pid):
            try:
                proc = psutil.Process(old_pid)
                cmd = " ".join(proc.cmdline())
                if "bot.py" in cmd:
                    logger.error(
                        "Уже запущен бот (PID %s). Остановите его или удалите %s",
                        old_pid,
                        _PID_FILE,
                    )
                    sys.exit(1)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    _PID_FILE.write_text(str(my_pid), encoding="utf-8")


def _release_single_instance_lock() -> None:
    try:
        if _PID_FILE.exists() and _PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


async def _on_startup(bot: Bot) -> None:
    wh = await bot.get_webhook_info()
    logger.info(
        "Telegram webhook: url=%r pending_updates=%s",
        wh.url or "",
        getattr(wh, "pending_update_count", "?"),
    )
    if wh.url:
        logger.warning("Активен webhook %s — удаляю, нужен polling", wh.url)
        await bot.delete_webhook(drop_pending_updates=False)

    # IMAP по умолчанию ВЫКЛ — пока не задано ENABLE_INCOMING_MAIL=1 (не блокирует кнопки).
    if os.getenv("ENABLE_INCOMING_MAIL", "").strip() not in {"1", "true", "yes", "on"}:
        logger.warning(
            "IMAP worker ВЫКЛЮЧЕН. Чтобы включить входящую почту: Railway → ENABLE_INCOMING_MAIL=1"
        )
        return

    delay = int(os.getenv("INCOMING_MAIL_START_DELAY_SEC", "90"))
    poll_seconds = int(os.getenv("INCOMING_MAIL_POLL_SECONDS", "30"))

    async def _start_imap_delayed() -> None:
        if delay > 0:
            logger.info("IMAP worker стартует через %ss", delay)
            await asyncio.sleep(delay)
        from services.incoming_mail_worker import start_incoming_mail_worker

        start_incoming_mail_worker(bot, poll_seconds=poll_seconds)
        logger.info("✅ Incoming mail worker стартовал (poll=%ss)", poll_seconds)

    asyncio.create_task(_start_imap_delayed())


async def _polling_heartbeat() -> None:
    """Пульс в логах: polling жив, даже если нет сообщений пользователя."""
    n = 0
    while True:
        await asyncio.sleep(30)
        n += 1
        logger.info("💓 polling alive #%d (если нет 📩 MSG — апдейты не доходят / другой экземпляр бота)", n)


async def _on_error(event: ErrorEvent) -> None:
    exc = event.exception
    if isinstance(exc, TelegramConflictError):
        logger.critical(
            "⚠️ TELEGRAM CONFLICT: два процесса polling на одном BOT_TOKEN! "
            "Останови локальный bot.py и оставь только Railway (1 реплика)."
        )
    logger.exception("Необработанная ошибка апдейта: %s", exc)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    _acquire_single_instance_lock()

    http_timeout = float(os.getenv("TELEGRAM_HTTP_TIMEOUT_SEC", "35"))
    session = AiohttpSession(timeout=http_timeout)

    bot = Bot(
        token=config.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    await init_db()
    from database import engine as _db_engine

    logger.info(
        "✅ БД готова (%s). Пользователи/аккаунты/офферы — в БД; пресеты — в Postgres при DATABASE_URL.",
        _db_engine.dialect.name,
    )

    dp = Dispatcher()
    dp.startup.register(_on_startup)
    dp.errors.register(_on_error)

    from middlewares.bot_access import BotAccessMiddleware
    from middlewares.callback_ack import CallbackAckMiddleware
    from middlewares.update_log import CallbackLogMiddleware, MessageLogMiddleware

    # Лог до access — видно, дошёл ли апдейт от Telegram.
    dp.message.middleware(MessageLogMiddleware())
    dp.callback_query.middleware(CallbackLogMiddleware())
    dp.message.middleware(BotAccessMiddleware())
    dp.callback_query.middleware(BotAccessMiddleware())
    dp.callback_query.middleware(CallbackAckMiddleware())

    _bind_priority_dispatcher_handlers(dp)

    for mod_name, r in _load_all_routers("handlers"):
        dp.include_router(r)

    allowed = sorted(
        {
            "message",
            "edited_message",
            "callback_query",
            "my_chat_member",
            "chat_member",
        }
    )

    me = await bot.get_me()
    token_hint = (config.BOT_TOKEN or "")[:12] + "…" if config.BOT_TOKEN else "ПУСТО"
    logger.info(
        "✅ Bot @%s (id=%s) token=%s. Polling… Только 1 процесс на токен!",
        me.username,
        me.id,
        token_hint,
    )
    logger.info("allowed_updates=%s", allowed)
    if not os.getenv("BOT_TOKEN", "").strip():
        logger.warning("BOT_TOKEN не задан в env — используется значение из config.py (опасно для prod)")

    asyncio.create_task(_polling_heartbeat())

    drop_pending = os.getenv("DROP_PENDING_UPDATES", "").strip() in {"1", "true", "yes"}
    if drop_pending:
        logger.warning("DROP_PENDING_UPDATES=1 — старые сообщения в очереди Telegram будут сброшены")

    try:
        await bot.delete_webhook(drop_pending_updates=drop_pending)
        await dp.start_polling(bot, allowed_updates=allowed, drop_pending_updates=drop_pending)
    finally:
        _release_single_instance_lock()
        await bot.session.close()
        logger.info("Bot stopped, session closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _release_single_instance_lock()
