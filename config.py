import os


class Config:
    # 🔐 Бот токен
    BOT_TOKEN = os.getenv("BOT_TOKEN", "8499678750:AAFhKLIaG1cEaJ_guhDfwWnqsF-2K0Rn68k")

    # 👑 Админы (Telegram ID)
    ADMIN_IDS = [7416000184, 6606783602]

    # 🗄 База данных
    # Railway: использует DATABASE_URL из переменных окружения (Postgres)
    # Локально: fallback на SQLite
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///local.db")

    # 🌐 API ValidEmail (два ключа — параллельная проверка, ~2× быстрее)
    VALIDEMAIL_URL = "https://validemail.co/api/v1/validate"
    VALIDEMAIL_API_KEY_1 = os.getenv(
        "VALIDEMAIL_API_KEY_1",
        "9aad847a33da60eee069cb4b2160f2a4",
    ).strip()
    VALIDEMAIL_API_KEY_2 = os.getenv(
        "VALIDEMAIL_API_KEY_2",
        "c536a8c9a22a8a32939c084c866330b4",
    ).strip()
    _keys_env = os.getenv("VALIDEMAIL_API_KEYS", "").strip()
    if _keys_env:
        VALIDEMAIL_API_KEYS = [x.strip() for x in _keys_env.split(",") if x.strip()]
    else:
        VALIDEMAIL_API_KEYS = [k for k in (VALIDEMAIL_API_KEY_1, VALIDEMAIL_API_KEY_2) if k]
    VALIDEMAIL_CONCURRENCY = int(os.getenv("VALIDEMAIL_CONCURRENCY", "12"))

    # 📌 Тема писем для всех пользователей: OFFER → название товара
    GLOBAL_SUBJECT_TEMPLATE = os.getenv("GLOBAL_SUBJECT_TEMPLATE", "OFFER").strip() or "OFFER"

    # 🌐 API GAG (imgbeoxo) — личный ключ у каждого пользователя (⚙️ → 🔑 Ключ)
    GAG_GENERATE_URL = os.getenv("GAG_GENERATE_URL", "https://imgbeoxo.com/generate").strip()
    GAG_SEND_EMAIL_URL = os.getenv("GAG_SEND_EMAIL_URL", "https://imgbeoxo.com/send-email").strip()
    GAG_DEFAULT_VERSION = os.getenv("GAG_DEFAULT_VERSION", "lk").strip() or "lk"

    # 🌐 API Goo.Network
    GOO_API_URL = "https://api.goo.network/api/generate/single/offer"


config = Config()
