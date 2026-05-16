import re
import unicodedata

# Служебные слова, которые не считаем фамилиями
STOPWORDS = {"de", "da", "von", "van", "der", "den", "di", "la", "le"}


def _normalize_text(text: str) -> str:
    """
    Удаляем диакритику, приводим к нижнему регистру, убираем лишние пробелы.
    Пример: "María de Silva" -> "maria de silva"
    """
    if not text:
        return ""

    # NFKD + фильтр диакритики
    nfkd = unicodedata.normalize("NFKD", text)
    without_accents = "".join(
        ch for ch in nfkd if not unicodedata.combining(ch)
    )

    cleaned = without_accents.lower()
    # дефисы превращаем в пробел, чтобы "Maria-Hansen" стало "Maria Hansen"
    cleaned = cleaned.replace("-", " ")
    # убираем всё, кроме букв и пробелов
    cleaned = re.sub(r"[^a-z\s]", " ", cleaned)
    # схлопываем пробелы
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def generate_login_from_name(raw_name: str) -> str | None:
    """
    Из имени продавца делаем логин для почты.

    Требования:
    - минимум 2 «нормальных» слова (имя + фамилия).
    - слова короче 3 символов или из STOPWORDS отбрасываем.
    - 'Maria Johansen'          -> 'maria.johansen'
      'Maria-Hansen Johansen'   -> 'maria.hansen'
      'Maria de Johansen'       -> 'maria.johansen'
      'ivan n'                  -> None
      'arina k'                 -> None
      'evgeniy s'               -> None
    """

    norm = _normalize_text(raw_name)
    if not norm:
        return None

    parts = norm.split()

    # фильтруем: длина >= 3 и не служебные слова
    tokens = [p for p in parts if len(p) >= 3 and p not in STOPWORDS]

    # нужно минимум два нормальных токена: имя + фамилия
    if len(tokens) < 2:
        return None

    # логин делаем по первым двум
    first, last = tokens[0], tokens[1]
    return f"{first}.{last}"
