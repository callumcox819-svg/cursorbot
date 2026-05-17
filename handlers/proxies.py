from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional, List, Tuple, Dict

import aiohttp
from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from database import Session
from models import User, Proxy
from services.proxy_verify import test_proxy, refresh_proxies_status
from utils.bg_jobs import is_running as bg_is_running, start as bg_start

router = Router()
logger = logging.getLogger(__name__)

# Один фоновый прогон «Проверить все» на пользователя (повторные клики не вешают бота)
_proxy_bulk_check_tasks: dict[int, asyncio.Task] = {}


# ======================
#  FSM состояния
# ======================

class ProxyAddStates(StatesGroup):
    waiting_for_list = State()


# ======================
#  Helpers
# ======================

_HOST_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_HOST_IPV6_RE = re.compile(r"^\[?[0-9a-fA-F:]+\]?$")  # rough
_HOST_HAS_DOT_RE = re.compile(r"\.")  # domain usually has dot
_HOST_HAS_DIGIT_RE = re.compile(r"\d")
_BAD_HOST_WORDS = {
    "тип", "типпрокси", "прокси", "host", "хост", "порт", "port",
    "логин", "login", "user", "username", "пароль", "password", "pass",
}

def _is_probable_host(host: str) -> bool:
    """Чтобы не принимать 'Порт' как host."""
    if not host:
        return False
    h = host.strip()
    hl = h.lower()

    # явно мусорные слова
    if hl in _BAD_HOST_WORDS:
        return False

    # IPv4
    if _HOST_IPV4_RE.match(h):
        # грубо проверим октеты
        try:
            parts = [int(x) for x in h.split(".")]
            if all(0 <= p <= 255 for p in parts):
                return True
        except Exception:
            return False

    # IPv6
    if ":" in h and _HOST_IPV6_RE.match(h):
        return True

    # домены: обычно есть точка или цифра (многие прокси: proxy123.domain.com)
    if _HOST_HAS_DOT_RE.search(h) or _HOST_HAS_DIGIT_RE.search(h):
        return True

    return False


def _normalize_proxy_type(t: str | None) -> str:
    """Только SOCKS5 для рассылки."""
    t = (t or "socks5").strip().lower()
    if t in ("socks", "sock5", "socksv5"):
        return "socks5"
    if t in ("socks5h",):
        return "socks5h"
    if t in ("socks5",):
        return "socks5"
    if t in ("http", "https"):
        return "http"
    if t.startswith("socks"):
        return "socks5"
    return "socks5"


def _reject_non_socks5(parsed: dict) -> Optional[str]:
    pt = _normalize_proxy_type(parsed.get("type"))
    if pt in ("http", "https"):
        return "Поддерживается только SOCKS5. HTTP/HTTPS прокси не подходят для рассылки."
    parsed["type"] = "socks5"
    return None


def _strip_comments(s: str) -> str:
    """убираем комментарии типа '... # comment'"""
    if not s:
        return ""
    s = s.strip().strip('"').strip("'")
    # режем по # если это не часть пароля/логина (в прокси почти не встречается)
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    return s


# ======================
#  Парсер строки/блока прокси
# ======================

def parse_proxy_string(raw: str) -> Optional[dict]:
    """
    Поддерживаемые форматы (и ещё куча вариаций):

      URL-формы:
        - http://user:pass@ip:port
        - https://user:pass@ip:port
        - socks5://user:pass@ip:port
        - socks5://ip:port

      Классика:
        - ip:port
        - ip:port:user:pass
        - ip:port:user:pass:socks5

      Браузерные:
        - user:pass@ip:port
        - ip:port@user:pass

    Важно: мы НЕ хотим принимать строки типа "Порт: 10811" как прокси.
    """
    raw = _strip_comments(raw)
    if not raw:
        return None

    # ---------- 1) URL формат ----------
    if "://" in raw:
        from urllib.parse import urlsplit
        try:
            u = urlsplit(raw)
            scheme = _normalize_proxy_type(u.scheme)
            host = u.hostname
            port = u.port
            user = u.username
            pwd = u.password
            if not host or not port or not _is_probable_host(host):
                return None
            return {
                "host": host,
                "port": int(port),
                "username": user,
                "password": pwd,
                "type": scheme,
            }
        except Exception as e:
            logger.warning("URL proxy parse failed for '%s': %s", raw, e)
            return None

    # ---------- 2) user:pass@host:port ----------
    if "@" in raw:
        # A) user:pass@host:port(:type?)  или user:pass@host:port|type
        try:
            left, right = raw.rsplit("@", 1)
            # возможен суффикс :type после port
            proto = None

            # right может быть host:port или host:port:type
            rparts = right.split(":")
            if len(rparts) >= 2:
                host = rparts[0].strip()
                port_s = rparts[1].strip()
                if len(rparts) >= 3:
                    proto = rparts[2].strip()
                if not _is_probable_host(host):
                    raise ValueError("bad host")
                port_i = int(port_s)

                if ":" in left:
                    user, pwd = left.split(":", 1)
                else:
                    user, pwd = left, ""

                return {
                    "host": host,
                    "port": port_i,
                    "username": user or None,
                    "password": pwd or None,
                    "type": _normalize_proxy_type(proto or "socks5"),
                }
        except Exception:
            pass

        # B) host:port@user:pass
        try:
            hostport, creds = raw.split("@", 1)
            if ":" not in hostport or ":" not in creds:
                raise ValueError("not host:port@user:pass")
            host, port_s = hostport.split(":", 1)
            user, pwd = creds.split(":", 1)
            if not _is_probable_host(host):
                raise ValueError("bad host")
            return {
                "host": host.strip(),
                "port": int(port_s.strip()),
                "username": user.strip() or None,
                "password": pwd.strip() or None,
                "type": "socks5",
            }
        except Exception:
            pass

    # ---------- 3) через ':' ----------
    parts = raw.split(":")
    parts = [p.strip() for p in parts if p is not None]

    # ip:port
    if len(parts) == 2:
        host, port = parts
        if not _is_probable_host(host):
            return None
        try:
            port_i = int(port)
        except ValueError:
            return None
        return {
            "host": host,
            "port": port_i,
            "username": None,
            "password": None,
            "type": "socks5",
        }

    # ip:port:user:pass
    if len(parts) == 4:
        host, port, user, pwd = parts
        if not _is_probable_host(host):
            return None
        try:
            port_i = int(port)
        except ValueError:
            return None
        return {
            "host": host,
            "port": port_i,
            "username": user or None,
            "password": pwd or None,
            "type": "socks5",
        }

    # ip:port:user:pass[:type] — пароль может содержать ':'
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        if not _is_probable_host(host):
            return None
        try:
            port_i = int(port)
        except ValueError:
            return None
        tail = parts[3:]
        proto = None
        if len(tail) >= 2 and _normalize_proxy_type(tail[-1]) in ("socks5", "socks5h", "http", "https"):
            proto = tail[-1]
            pwd = ":".join(tail[:-1])
        else:
            pwd = ":".join(tail)
        return {
            "host": host,
            "port": port_i,
            "username": user or None,
            "password": pwd or None,
            "type": _normalize_proxy_type(proto or "socks5"),
        }

    return None


def parse_proxy_block(text: str) -> Optional[dict]:
    """
    Парсит "карточку" вида:
      Тип прокси: socks5
      Хост: 109.104.153.100
      Порт: 10811
      Логин: user
      Пароль: pass

    Также понимает:
      type=...
      host=...
      port=...
      user=...
      pass=...
    """
    if not text:
        return None

    raw = text.strip()
    if not raw:
        return None

    # Если блок — это просто одна строка, пусть обработает parse_proxy_string
    if "\n" not in raw:
        return parse_proxy_string(raw)

    kv: Dict[str, str] = {}
    for line in raw.splitlines():
        l = line.strip()
        if not l:
            continue

        # позволяем "ключ: значение" и "ключ = значение"
        if ":" in l:
            k, v = l.split(":", 1)
        elif "=" in l:
            k, v = l.split("=", 1)
        else:
            # если это не key:value, возможно это обычная строка прокси — попробуем позже
            continue

        k = (k or "").strip().lower()
        v = (v or "").strip()
        if not v:
            continue

        # нормализуем ключи
        if "тип" in k or k in ("type", "scheme", "proto", "protocol"):
            kv["type"] = v
        elif "хост" in k or k in ("host", "ip", "addr", "address"):
            kv["host"] = v
        elif "порт" in k or k in ("port",):
            kv["port"] = v
        elif "логин" in k or "user" in k or k in ("username",):
            kv["username"] = v
        elif "пароль" in k or "pass" in k:
            kv["password"] = v

    # Если похоже на карточку
    if "host" in kv and "port" in kv:
        host = kv.get("host", "").strip()
        if not _is_probable_host(host):
            return None
        try:
            port_i = int(str(kv.get("port", "")).strip())
        except Exception:
            return None

        return {
            "host": host,
            "port": port_i,
            "username": (kv.get("username") or "").strip() or None,
            "password": (kv.get("password") or "").strip() or None,
            "type": _normalize_proxy_type(kv.get("type")),
        }

    # Иначе попробуем найти строку прокси внутри блока (если человек вставил лишний текст)
    for line in raw.splitlines():
        p = parse_proxy_string(line.strip())
        if p:
            return p

    return None


# ======================
#  Меню
# ======================

def proxies_menu(proxies: List[Proxy]) -> InlineKeyboardMarkup:
    rows = []

    for p in proxies:
        status = "🟢" if p.is_active else "🔴"
        ptype = (p.type or "socks5").lower()
        text = f"{status} {ptype} {p.host}:{p.port}"

        rows.append([
            InlineKeyboardButton(text=text, callback_data=f"proxy_info:{p.id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"proxy_del:{p.id}"),
            InlineKeyboardButton(text="🔄", callback_data=f"proxy_test:{p.id}"),
        ])

    rows.append([InlineKeyboardButton(text="➕ Добавить прокси", callback_data="proxy_add_menu")])
    if proxies:
        rows.append([InlineKeyboardButton(text="🔍 Проверить прокси", callback_data="proxies_check_all")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_back")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ======================
#  Рендер меню
# ======================

async def render_proxy_menu(message_or_cb, telegram_id: int):
    async with Session() as session:
        res_user = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = res_user.scalar_one_or_none()

        if not user:
            proxies: List[Proxy] = []
        else:
            result = await session.execute(
                select(Proxy).where(Proxy.user_id == user.id)
            )
            proxies = list(result.scalars())

    text = (
        "🧩 <b>Твои прокси</b>\n\n"
        f"Всего: {len(proxies)}\n"
        f"Рабочих (SOCKS5+SMTP): {sum(1 for p in proxies if p.is_active)}\n"
        f"Плохих: {sum(1 for p in proxies if not p.is_active)}\n"
        f"<i>Только SOCKS5 · проверка: туннель + smtp.gmail.com:587</i>"
    )

    kb = proxies_menu(proxies)

    if isinstance(message_or_cb, Message):
        await message_or_cb.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ======================
#  Открыть список прокси
# ======================

@router.callback_query(F.data == "settings_proxies")
async def open_proxies(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    await render_proxy_menu(callback, callback.from_user.id)


# ======================
#  Добавить прокси — меню
# ======================

@router.callback_query(F.data == "proxy_add_menu")
async def proxy_add_menu(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    await state.set_state(ProxyAddStates.waiting_for_list)
    await callback.message.edit_text(
        "📝 <b>Только SOCKS5</b> (HTTP не поддерживается).\n"
        "Пришли список прокси (по одному на строку) ИЛИ карточкой.\n\n"
        "<b>Примеры:</b>\n"
        "<code>socks5://user:pass@109.104.153.100:10811</code>\n"
        "<code>109.104.153.100:10811:user:pass:socks5</code>\n"
        "<code>user:pass@109.104.153.100:10811</code>\n\n"
        "<b>Или так (карточкой):</b>\n"
        "<code>Тип прокси: socks5\nХост: 109.104.153.100\nПорт: 10811\nЛогин: user\nПароль: pass</code>\n\n"
        "Каждый прокси будет проверен.\n",
        parse_mode="HTML",
    )


# ======================
#  Обработка добавления прокси
# ======================

@router.message(ProxyAddStates.waiting_for_list)
async def proxy_add_process(message: Message, state: FSMContext):
    raw_text = (message.text or "").strip()
    if not raw_text:
        await message.answer("❌ Пусто. Пришли прокси строками или карточкой.")
        return

    telegram_id = message.from_user.id

    # 1) Разбиваем на блоки по пустым строкам (чтобы поддержать несколько карточек)
    blocks: list[str] = []
    cur: list[str] = []
    for ln in raw_text.splitlines():
        if ln.strip() == "":
            if cur:
                blocks.append("\n".join(cur).strip())
                cur = []
        else:
            cur.append(ln.strip())
    if cur:
        blocks.append("\n".join(cur).strip())

    # 2) Если это НЕ карточки — оставим как "строка на прокси"
    # Если блок один и он без ключевых слов — будем парсить построчно.
    def _has_kv_keywords(t: str) -> bool:
        s = t.lower()
        return any(k in s for k in ("тип прокси", "хост", "порт", "логин", "пароль", "username", "password", "type="))

    parsed_items: list[tuple[str, Optional[dict]]] = []

    if len(blocks) == 1 and not _has_kv_keywords(blocks[0]):
        # обычный режим: каждая строка отдельный прокси
        for line in [l.strip() for l in raw_text.splitlines() if l.strip()]:
            parsed_items.append((line, parse_proxy_string(line)))
    else:
        # режим блоков: каждый блок либо карточка, либо одиночная строка
        for b in blocks:
            if _has_kv_keywords(b):
                parsed_items.append((b, parse_proxy_block(b)))
            else:
                # если в блоке несколько строк, попробуем каждую
                if "\n" in b:
                    for line in [l.strip() for l in b.splitlines() if l.strip()]:
                        parsed_items.append((line, parse_proxy_string(line)))
                else:
                    parsed_items.append((b, parse_proxy_string(b)))

    if bg_is_running(telegram_id, "proxy_add"):
        return await message.answer("⏳ Добавление прокси уже идёт. Подождите завершения.")

    status_msg = await message.answer(
        f"⏳ Проверяю <b>{len(parsed_items)}</b> прокси…\n"
        "<i>Не отправляйте новый список, пока идёт проверка.</i>",
        parse_mode="HTML",
    )

    async def _job() -> None:
        await _proxy_add_work(message, state, telegram_id, parsed_items, status_msg)

    if not bg_start(telegram_id, "proxy_add", _job()):
        return await message.answer("⏳ Добавление прокси уже идёт. Подождите завершения.")


async def _proxy_add_work(
    message: Message,
    state: FSMContext,
    telegram_id: int,
    parsed_items: list[tuple[str, Optional[dict]]],
    status_msg: Message,
) -> None:
    ok_count = 0
    fail_count = 0
    details: List[str] = []

    async with Session() as session:
        res_user = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = res_user.scalar_one_or_none()
        if not user:
            user = User(telegram_id=telegram_id)
            session.add(user)
            await session.commit()
            await session.refresh(user)

        for original_text, parsed in parsed_items:
            if not parsed:
                fail_count += 1
                # показываем кратко (чтобы не залить чат огромным блоком)
                preview = original_text.replace("\n", " / ")
                if len(preview) > 120:
                    preview = preview[:120] + "…"
                details.append(f"❌ `{preview}` — неправильный формат")
                continue

            err = _reject_non_socks5(parsed)
            if err:
                fail_count += 1
                preview = original_text.replace("\n", " / ")
                if len(preview) > 120:
                    preview = preview[:120] + "…"
                details.append(f"❌ `{preview}` — {err}")
                continue

            try:
                ok, info = await asyncio.wait_for(test_proxy(parsed, timeout=22), timeout=55)
            except asyncio.TimeoutError:
                ok, info = False, "Timeout: проверка прокси заняла слишком долго"
            except Exception as e:
                ok, info = False, f"{type(e).__name__}: {e}"

            proxy = Proxy(
                user_id=user.id,
                host=parsed["host"],
                port=parsed["port"],
                username=parsed.get("username"),
                password=parsed.get("password"),
                type="socks5",
                is_active=ok,
                last_error=None if ok else info,
            )

            try:
                session.add(proxy)
                await session.commit()
                ok_count += 1
                preview = original_text.replace("\n", " / ")
                if len(preview) > 120:
                    preview = preview[:120] + "…"
                details.append(f"✅ `{preview}` — {info}")
            except Exception as e:
                logger.exception("Error saving proxy")
                fail_count += 1
                preview = original_text.replace("\n", " / ")
                if len(preview) > 120:
                    preview = preview[:120] + "…"
                details.append(f"❌ `{preview}` — ошибка сохранения: {e}")

    summary = (
        "Готово.\n\n"
        f"Успешно добавлено: {ok_count}\n"
        f"Ошибок: {fail_count}\n\n" +
        "\n".join(details[:50])  # ограничим, чтобы не словить лимиты
    )
    if len(details) > 50:
        summary += f"\n…и ещё {len(details) - 50} строк"

    try:
        await status_msg.edit_text(summary[:4000], parse_mode="Markdown")
    except Exception:
        await message.answer(summary, parse_mode="Markdown")
    await state.clear()
    await render_proxy_menu(message, telegram_id)


# ======================
#  Клик по прокси (инфо)
# ======================

@router.callback_query(F.data.startswith("proxy_info:"))
async def proxy_info(callback: CallbackQuery):
    proxy_id = int(callback.data.split(":")[1])

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    async with Session() as session:
        proxy = await session.get(Proxy, proxy_id)

    if not proxy:
        return

    err = proxy.last_error or "-"

    text = (
        "🧩 <b>Прокси</b>\n\n"
        f"Host: <code>{proxy.host}</code>\n"
        f"Port: <code>{proxy.port}</code>\n"
        f"Type: <code>{proxy.type}</code>\n"
        f"Username: <code>{proxy.username}</code>\n"
        f"Password: <code>{proxy.password}</code>\n"
        f"Статус: {'🟢 Рабочий' if proxy.is_active else '🔴 Ошибка'}\n"
        f"Ошибка: <code>{err}</code>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить", callback_data=f"proxy_test:{proxy.id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings_proxies")],
        ]
    )

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ======================
#  Удаление прокси
# ======================

@router.callback_query(F.data.startswith("proxy_del:"))
async def proxy_delete(callback: CallbackQuery):
    proxy_id = int(callback.data.split(":")[1])

    try:
        await callback.answer("Удаляю…")
    except TelegramBadRequest:
        pass

    async with Session() as session:
        proxy = await session.get(Proxy, proxy_id)
        if proxy:
            await session.delete(proxy)
            await session.commit()

    await render_proxy_menu(callback, callback.from_user.id)


# ======================
#  Ручной тест прокси
# ======================

@router.callback_query(F.data == "proxies_check_all")
async def proxies_check_all(callback: CallbackQuery) -> None:
    telegram_id = callback.from_user.id

    existing = _proxy_bulk_check_tasks.get(telegram_id)
    if existing and not existing.done():
        try:
            await callback.answer("⏳ Проверка уже идёт, подождите…", show_alert=True)
        except TelegramBadRequest:
            pass
        return

    try:
        await callback.answer("⏳ Запускаю проверку…", show_alert=False)
    except TelegramBadRequest:
        pass

    async with Session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one_or_none()
        if not user:
            try:
                await callback.message.edit_text("❌ Пользователь не найден.")
            except TelegramBadRequest:
                pass
            return

        proxies = list(
            (
                await session.execute(select(Proxy).where(Proxy.user_id == user.id))
            ).scalars()
        )

    if not proxies:
        await render_proxy_menu(callback, telegram_id)
        return

    try:
        await callback.message.edit_text(
            f"⏳ <b>Проверяю {len(proxies)} прокси…</b>\n\n"
            "<i>SOCKS5 (httpbin) + SMTP smtp.gmail.com:587\n"
            "Не нажимайте кнопку повторно — займёт до ~30 сек.</i>",
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass

    async def _run_bulk_check() -> None:
        concurrency = max(1, min(3, int(os.getenv("PROXY_CHECK_CONCURRENCY", "2"))))
        check_timeout = max(12, min(25, int(os.getenv("PROXY_CHECK_TIMEOUT", "22"))))
        ok_n = fail_n = 0
        try:
            async with Session() as session:
                user = (
                    await session.execute(select(User).where(User.telegram_id == telegram_id))
                ).scalar_one_or_none()
                if not user:
                    return
                ok_n, fail_n, _ = await refresh_proxies_status(
                    session,
                    int(user.id),
                    concurrency=concurrency,
                    timeout=check_timeout,
                )

            try:
                await callback.message.edit_text(
                    "✅ <b>Проверка завершена</b>\n\n"
                    f"🟢 рабочих: <b>{ok_n}</b> · 🔴 неактивных: <b>{fail_n}</b>\n"
                    "<i>Статус обновлён в списке ниже.</i>",
                    parse_mode="HTML",
                )
            except TelegramBadRequest:
                pass

            await render_proxy_menu(callback, telegram_id)
        except Exception:
            logger.exception("bulk proxy check failed for user %s", telegram_id)
            try:
                await callback.message.edit_text(
                    "❌ Ошибка при проверке прокси. Попробуйте позже или проверьте один прокси кнопкой 🔄.",
                    parse_mode="HTML",
                )
            except TelegramBadRequest:
                pass
        finally:
            _proxy_bulk_check_tasks.pop(telegram_id, None)

    task = asyncio.create_task(_run_bulk_check())
    _proxy_bulk_check_tasks[telegram_id] = task


@router.callback_query(F.data.startswith("proxy_test:"))
async def proxy_test(callback: CallbackQuery):
    try:
        await callback.answer("⏳ Тестирую прокси…", show_alert=False)
    except TelegramBadRequest:
        pass

    proxy_id = int(callback.data.split(":")[1])
    telegram_id = callback.from_user.id
    job_key = f"proxy_test:{proxy_id}"

    if bg_is_running(telegram_id, job_key):
        try:
            await callback.answer("⏳ Этот прокси уже проверяется…", show_alert=True)
        except TelegramBadRequest:
            pass
        return

    async with Session() as session:
        proxy = await session.get(Proxy, proxy_id)

    if not proxy:
        return

    check_timeout = max(12, min(25, int(os.getenv("PROXY_CHECK_TIMEOUT", "22"))))

    async def run() -> None:
        try:
            ok, info = await asyncio.wait_for(
                test_proxy(proxy, timeout=check_timeout),
                timeout=check_timeout * 2 + 12,
            )
        except asyncio.TimeoutError:
            ok, info = False, "Timeout: проверка заняла слишком долго"
        except Exception as e:
            ok, info = False, f"{type(e).__name__}: {e}"

        async with Session() as session2:
            proxy_db = await session2.get(Proxy, proxy_id)
            if proxy_db:
                proxy_db.is_active = ok
                proxy_db.last_error = None if ok else info
                await session2.commit()

        status_text = (
            f"✅ Прокси работает\n<code>{info}</code>"
            if ok
            else f"❌ Прокси не работает\n<code>{info}</code>"
        )
        try:
            await callback.bot.send_message(
                callback.message.chat.id,
                status_text,
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            await render_proxy_menu(callback, telegram_id)
        except Exception:
            logger.exception("render_proxy_menu failed")

    if not bg_start(telegram_id, job_key, run()):
        try:
            await callback.answer("⏳ Этот прокси уже проверяется…", show_alert=True)
        except TelegramBadRequest:
            pass
