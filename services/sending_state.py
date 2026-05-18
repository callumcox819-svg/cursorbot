from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class SendingState:
    # --- новый API (используется send.py / stopsend.py) ---
    user_id: int = 0  # telegram id
    is_running: bool = False
    is_stopping: bool = False

    total_targets: int = 0
    sent_count: int = 0
    failed_count: int = 0

    last_status: str = "-"  # NORMAL / FAST
    last_error: str = "-"
    last_failed_to: str = ""  # email получателя последней ошибки
    current_to: str = ""  # сейчас в работе (для /stat)

    # --- метрики аккаунтов для статуса ---
    accounts_total: int = 0
    accounts_active: int = 0

    # --- старый API (на всякий) ---
    running: bool = False
    sent: int = 0
    total: int = 0
    errors: int = 0

    def sync_aliases(self) -> None:
        """Синхронизирует старые и новые поля, чтобы статус/совместимость не ломались."""
        self.running = bool(self.is_running)
        self.sent = int(self.sent_count)
        self.total = int(self.total_targets)
        self.errors = int(self.failed_count)


_STATE: Dict[int, SendingState] = {}  # key = telegram_id


def get_sending_state(telegram_id: int) -> Optional[SendingState]:
    return _STATE.get(telegram_id)


def set_sending_state(telegram_id: int, state: SendingState) -> SendingState:
    state.user_id = telegram_id
    state.sync_aliases()
    _STATE[telegram_id] = state
    return state


# ------------------------------------------------------------
# Совместимость со старым API (если где-то импортируют get_state/set_state)
# ------------------------------------------------------------
get_state = get_sending_state


def set_state(telegram_id: int, **kwargs) -> SendingState:
    st = _STATE.get(telegram_id) or SendingState(user_id=telegram_id)
    for k, v in kwargs.items():
        if hasattr(st, k):
            setattr(st, k, v)
    st.sync_aliases()
    _STATE[telegram_id] = st
    return st


def any_mailing_active() -> bool:
    """Идёт рассылка — IMAP и тяжёлые задачи приостанавливаем, чтобы /start и кнопки не зависали."""
    return bool(active_mailing_telegram_ids())


def active_mailing_telegram_ids() -> frozenset[int]:
    """Telegram ID пользователей, у которых сейчас идёт /send (не останавливается)."""
    return frozenset(
        tid
        for tid, st in _STATE.items()
        if st.is_running and not st.is_stopping
    )
