import random
from typing import Optional

from sqlalchemy import select, update, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from models import Proxy


class ProxyManager:
    """
    Работа с пользовательскими прокси:
    - добавление / удаление
    - выбор случайного активного прокси
    - парсинг строки вида host:port:login:pass
    """

    @staticmethod
    def parse_proxy_string(proxy_str: str):
        """
        Ожидаемый формат:
        proxy.loma.host:38174:login:password
        """
        parts = proxy_str.strip().split(":")
        if len(parts) < 2:
            raise ValueError("Неверный формат прокси. Нужно host:port[:login:password]")

        host = parts[0]
        port = int(parts[1])
        username = parts[2] if len(parts) >= 3 else None
        password = ":".join(parts[3:]) if len(parts) >= 4 else None

        return host, port, username, password

    @staticmethod
    async def add_proxy(
        session: AsyncSession,
        user_id: int,
        proxy_str: str,
        proxy_type: str = "http",
    ) -> Proxy:
        host, port, username, password = ProxyManager.parse_proxy_string(proxy_str)

        proxy = Proxy(
            user_id=user_id,
            host=host,
            port=port,
            username=username,
            password=password,
            type=proxy_type,
            is_active=True,
        )
        session.add(proxy)
        await session.commit()
        await session.refresh(proxy)
        return proxy

    @staticmethod
    async def delete_proxy(session: AsyncSession, user_id: int, proxy_id: int) -> None:
        await session.execute(
            delete(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == user_id)
        )
        await session.commit()

    @staticmethod
    async def list_proxies(session: AsyncSession, user_id: int) -> list[Proxy]:
        result = await session.execute(
            select(Proxy).where(Proxy.user_id == user_id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_random_active_proxy(
        session: AsyncSession, user_id: int
    ) -> Optional[Proxy]:
        result = await session.execute(
            select(Proxy).where(
                Proxy.user_id == user_id,
                or_(Proxy.is_active.is_(True), Proxy.is_active.is_(None)),
            )
        )
        proxies = list(result.scalars().all())
        if not proxies:
            return None
        return random.choice(proxies)

    @staticmethod
    async def set_proxy_error(
        session: AsyncSession, proxy_id: int, error: str
    ) -> None:
        await ProxyManager.note_proxy_failure(
            session, proxy_id, error, deactivate=True
        )

    @staticmethod
    async def note_proxy_failure(
        session: AsyncSession,
        proxy_id: int,
        error: str,
        *,
        deactivate: bool = False,
    ) -> None:
        """Записать ошибку; отключать прокси только при явном сбое SOCKS (не SMTP timeout)."""
        values: dict = {"last_error": (error or "")[:500]}
        if deactivate:
            values["is_active"] = False
        await session.execute(
            update(Proxy).where(Proxy.id == proxy_id).values(**values)
        )
        await session.commit()


# ============================================================
# 🔒 ВАЖНО: совместимость импорта для рассылки
# ============================================================
# send.py импортирует:
#   from services.proxy_manager import choose_proxy_for_user, ProxySMTPContext
#
# Но реальная реализация у тебя находится в корневом proxy_manager.py.
# Мы НЕ пишем новую логику — просто переэкспортируем существующую.
try:
    from proxy_manager import choose_proxy_for_user, ProxySMTPContext  # noqa: F401
except Exception:
    # Если по какой-то причине корневой proxy_manager.py недоступен,
    # оставим "пустые" заглушки, чтобы импорт не валил приложение.
    # (Логика рассылки всё равно не сможет использовать прокси без реальной реализации.)
    async def choose_proxy_for_user(session, user_id: int) -> Optional[Proxy]:  # type: ignore
        return None

    class ProxySMTPContext:  # type: ignore
        def __init__(self, proxy: Proxy):
            self.proxy = proxy

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False
