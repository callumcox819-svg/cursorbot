from __future__ import annotations

import json
from sqlalchemy import select

from database import Session
from models import AppSetting

from services.users import get_or_create_user
from services.user_settings import get_user_setting, set_user_setting


VALIDEMAIL_KEY_SETTING = "validemail_api_key"

# GAG (imageoxo) global key stored in app_settings
GAG_KEY_SETTING = "gag_api_key"

# timings stored per-user (same approach as handlers/settings.py)
TIMING_KEY = "timings_json"


async def get_setting(session: Session, key: str) -> str | None:
    res = await session.execute(select(AppSetting).where(AppSetting.key == key))
    row = res.scalar_one_or_none()
    if not row:
        return None
    return (row.value or "").strip() or None


async def set_setting(session: Session, key: str, value: str | None) -> None:
    k = (key or "").strip()
    if not k:
        return
    res = await session.execute(select(AppSetting).where(AppSetting.key == k))
    row = res.scalar_one_or_none()
    if row is None:
        row = AppSetting(key=k, value=(value or None))
        session.add(row)
    else:
        row.value = (value or None)
    await session.commit()


async def get_validemail_api_key(session: Session) -> str | None:
    from services.validemail_keys import keys_from_config

    keys = keys_from_config()
    return keys[0] if keys else None


async def set_validemail_api_key(session: Session, api_key: str | None) -> None:
    """Не используется: ключи ValidEmail задаются в config.py."""
    return


async def get_gag_api_key(session: Session) -> str | None:
    """Устарело: GAG-ключ хранится per-user (gag_user_api_key)."""
    return None


async def set_gag_api_key(session: Session, api_key: str | None) -> None:
    """Не используется: ключ задаётся пользователем в ⚙️ → 🔑 Ключ."""
    return


def _timing_default() -> dict:
    # default values (как в handlers/settings.py)
    # + совместимость: дублируем min/max в min_delay/max_delay
    return {
        "min": 1,
        "max": 5,
        "min_delay": 2,
        "max_delay": 4,
        "batch_size": 1,
    }


async def load_timing(session: Session, tg_user_id: int) -> dict:
    """
    Нужна, потому что handlers/send.py импортирует:
      from services.settings import load_timing
    """
    user = await get_or_create_user(session, tg_user_id)
    raw = await get_user_setting(session, user, TIMING_KEY)
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                base = _timing_default()
                base.update(d)

                # нормализация
                base["min"] = int(base.get("min", 1))
                base["max"] = int(base.get("max", 5))
                # совместимость с send.py, который может ожидать min_delay/max_delay/batch_size
                base["min_delay"] = float(base.get("min_delay", base["min"]))
                base["max_delay"] = float(base.get("max_delay", base["max"]))
                base["batch_size"] = 1  # рассылка всегда 1 ящик → 1 письмо

                return base
        except Exception:
            pass
    return _timing_default()


async def save_timing(session: Session, tg_user_id: int, timing: dict) -> None:
    """
    Парная функция, чтобы не было рассинхрона API.
    """
    user = await get_or_create_user(session, tg_user_id)
    payload = {
        "min": int(timing.get("min", 1)),
        "max": int(timing.get("max", 5)),
        "min_delay": float(timing.get("min_delay", timing.get("min", 1))),
        "max_delay": float(timing.get("max_delay", timing.get("max", 5))),
        "batch_size": 1,
    }
    await set_user_setting(session, user, TIMING_KEY, json.dumps(payload, ensure_ascii=False))
