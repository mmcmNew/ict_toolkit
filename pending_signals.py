"""
pending_signals.py - Управление очередью подтверждения сделок (IPC) между Telegram и ботами.

Обеспечивает надежный обмен командами через persistent JSON-файл `pending_signals.json`:
- Бот находит сетап вне Киллзоны -> регистрирует сигнал со статусом PENDING.
- Пользователь в Telegram нажимает [✅ Открыть] -> статус меняется на APPROVED.
- Торговый бот мгновенно считывает APPROVED сигнал и отправляет ордер на биржу.
- При нажатии [❌ Пропустить] или истечении TTL (10 мин) сигнал безопасно закрывается.
"""

import os
import json
import time
import tempfile
from datetime import datetime, timezone

try:
    import config as cfg
    SIGNALS_FILE = getattr(cfg, "PENDING_SIGNALS_FILE", os.path.join("data", "pending_signals.json"))
except Exception:
    SIGNALS_FILE = os.path.join("data", "pending_signals.json")

ROOT_SIGNALS_FILE = "pending_signals.json"


def load_pending_signals() -> dict:
    """Загружает текущую очередь сигналов с защитой от блокировки файла."""
    file_to_read = SIGNALS_FILE
    if not os.path.exists(file_to_read) and os.path.exists(ROOT_SIGNALS_FILE):
        file_to_read = ROOT_SIGNALS_FILE

    for attempt in range(3):
        if not os.path.exists(file_to_read):
            return {}
        try:
            with open(file_to_read, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Миграция из корня в целевой файл в data/ при чтении
            if file_to_read != SIGNALS_FILE and not os.path.exists(SIGNALS_FILE):
                try:
                    save_pending_signals(data)
                except Exception:
                    pass
            return data
        except Exception:
            if attempt < 2:
                time.sleep(0.05)
            else:
                return {}
    return {}


def save_pending_signals(signals: dict):
    """Атомарно сохраняет очередь сигналов через временный файл."""
    target_dir = os.path.dirname(os.path.abspath(SIGNALS_FILE)) or "."
    os.makedirs(target_dir, exist_ok=True)
    temp_file = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=target_dir, delete=False, encoding="utf-8", suffix=".tmp") as f:
            temp_file = f.name
            json.dump(signals, f, indent=2, default=str)
        os.replace(temp_file, SIGNALS_FILE)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения pending signals: {e}")
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass


def create_pending_signal(
    market: str,
    symbol: str,
    direction: int,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    pos_calc: dict,
    setup_reason: dict,
    message_id: int = None,
) -> str:
    """Создает новый отложенный сигнал со статусом PENDING."""
    clean_sym = symbol.replace("/", "_").replace(":", "_")
    sig_id = f"{clean_sym}_{int(time.time())}"

    signals = load_pending_signals()
    signals[sig_id] = {
        "id": sig_id,
        "market": market.lower(),
        "symbol": symbol,
        "direction": direction,
        "entry_price": float(entry_price),
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "pos_calc": pos_calc,
        "setup_reason": setup_reason,
        "status": "PENDING",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_ts": time.time(),
        "message_id": message_id,
    }
    save_pending_signals(signals)
    return sig_id


def set_signal_message_id(sig_id: str, message_id: int):
    """Привязывает message_id сообщения Telegram к сигналу."""
    signals = load_pending_signals()
    if sig_id in signals:
        signals[sig_id]["message_id"] = message_id
        save_pending_signals(signals)


def approve_signal(sig_id: str) -> bool:
    """Пользователь одобрил сделку в Telegram."""
    signals = load_pending_signals()
    if sig_id in signals:
        if signals[sig_id].get("status") == "PENDING":
            signals[sig_id]["status"] = "APPROVED"
            signals[sig_id]["approved_ts"] = time.time()
            signals[sig_id]["approved_at"] = datetime.now(timezone.utc).isoformat()
            save_pending_signals(signals)
            return True
    return False


def reject_signal(sig_id: str) -> bool:
    """Пользователь отклонил сделку в Telegram."""
    signals = load_pending_signals()
    if sig_id in signals:
        signals[sig_id]["status"] = "REJECTED"
        signals[sig_id]["rejected_ts"] = time.time()
        signals[sig_id]["rejected_at"] = datetime.now(timezone.utc).isoformat()
        save_pending_signals(signals)
        return True
    return False


def mark_executed(sig_id: str, exec_info: dict = None) -> bool:
    """Торговый бот исполнил подтвержденный ордер."""
    signals = load_pending_signals()
    if sig_id in signals:
        signals[sig_id]["status"] = "EXECUTED"
        signals[sig_id]["executed_ts"] = time.time()
        signals[sig_id]["executed_at"] = datetime.now(timezone.utc).isoformat()
        if exec_info:
            signals[sig_id]["execution_info"] = exec_info
        save_pending_signals(signals)
        return True
    return False


def mark_failed(sig_id: str, error_msg: str) -> bool:
    """Ошибка выставления ордера на бирже."""
    signals = load_pending_signals()
    if sig_id in signals:
        signals[sig_id]["status"] = "FAILED"
        signals[sig_id]["error"] = str(error_msg)
        save_pending_signals(signals)
        return True
    return False


def get_approved_signals(market: str = None) -> list:
    """Возвращает список сигналов, ожидающих немедленного исполнения роботом."""
    signals = load_pending_signals()
    now = time.time()
    approved = []
    for s in signals.values():
        if s.get("status") == "APPROVED":
            # Игнорируем одобрения старше 3 минут, чтобы не входить с сильным опозданием
            if now - s.get("approved_ts", now) < 180:
                if market is None or market.lower() in s.get("market", "").lower():
                    approved.append(s)
    return approved


def clean_stale_signals(ttl_sec: int = 600):
    """Переводит зависшие PENDING сигналы старше ttl_sec в статус EXPIRED."""
    signals = load_pending_signals()
    now = time.time()
    changed = False
    for s in signals.values():
        if s.get("status") == "PENDING" and (now - s.get("created_ts", now) > ttl_sec):
            s["status"] = "EXPIRED"
            s["expired_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
    if changed:
        save_pending_signals(signals)
