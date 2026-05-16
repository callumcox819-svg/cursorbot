from __future__ import annotations

from config import config


def get_admin_ids() -> set[int]:
    """
    Берём ADMIN_IDS из config и приводим всё к int.
    Поддерживает варианты:
      ADMIN_IDS = 123
      ADMIN_IDS = [123]
      ADMIN_IDS = ["123", "456"]
    """
    ids = getattr(config, "ADMIN_IDS", [])

    if isinstance(ids, (int, str)):
        ids = [ids]

    admin_ids: set[int] = set()
    for v in ids:
        try:
            admin_ids.add(int(v))
        except Exception:
            continue

    return admin_ids


def is_admin(user_id: int) -> bool:
    """
    Простая и надёжная проверка: есть ли user_id в ADMIN_IDS.
    """
    try:
        uid = int(user_id)
    except Exception:
        return False

    return uid in get_admin_ids()
