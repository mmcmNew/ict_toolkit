"""
telegram_bot.py - Интерактивный Telegram-бот для управления и мониторинга ICT Toolkit.

Позволяет прямо из Telegram:
- 📊 /stats     - Полная статистика торговли (Winrate, Net PnL, комиссии, Net R) по обоим рынкам
- 📌 /positions - Активные открытые позиции на Bitget и Мосбирже (вход, SL, TP, причина входа)
- 💰 /balance   - Текущий баланс и свободные средства (USDT на Bitget, RUB в Т-Банке)
- 🕒 /status    - Торговые сессии, Киллзоны, расписание биржи и статус ботов
- ❓ /help      - Список команд и меню кнопок на клавиатуре телефона
"""

import os
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import ssl
import time
import json
import html
import urllib.request
import urllib.error
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timezone, timedelta

import config as cfg
from telegram_notifier import get_telegram_credentials
import pending_signals as ps

DATA_DIR = getattr(cfg, "DATA_DIR", "data")
BITGET_TRADES_PATH = getattr(cfg, "TRADE_LOG_FILE", os.path.join(DATA_DIR, "live_trade_log.json"))
TBANK_POSITIONS_PATH = getattr(cfg, "TBANK_ACTIVE_POSITIONS_FILE", os.path.join(DATA_DIR, "tbank_active_positions.json"))
TBANK_TRADES_PATH = getattr(cfg, "TBANK_TRADE_LOG_FILE", os.path.join(DATA_DIR, "tbank_trade_log.json"))

ROOT_BITGET_TRADES_PATH = "live_trade_log.json"
ROOT_TBANK_POSITIONS_PATH = "tbank_active_positions.json"
ROOT_TBANK_TRADES_PATH = "tbank_trade_log.json"


def load_json_data(primary_path: str, fallback_path: str, default=None):
    """Загружает JSON из primary_path с фоллбэком на fallback_path."""
    path = primary_path
    if not os.path.exists(path) and os.path.exists(fallback_path):
        path = fallback_path
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default if default is not None else {}
    return default if default is not None else {}


def get_opener():
    """Создает HTTP opener с поддержкой TELEGRAM_PROXY и SSL context."""
    token, chat_id, proxy, base_url = get_telegram_credentials()
    if proxy:
        proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        ssl_ctx = ssl._create_unverified_context()
        return urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ssl_ctx))
    return urllib.request.build_opener()


def send_bot_reply(text: str, reply_markup: dict = None) -> bool:
    """Отправляет ответ пользователю в Telegram с опциональной клавиатурой."""
    token, chat_id, proxy, base_url = get_telegram_credentials()
    if not token or not chat_id:
        return False

    url = f"{base_url}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "ICT-Toolkit-Bot/1.0"},
            method="POST",
        )
        opener = get_opener()
        with opener.open(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"⚠️ [Telegram Bot] Ошибка отправки ответа: {e}")
        return False


def get_main_keyboard():
    """Постоянное меню кнопок внизу экрана мобильного приложения Telegram."""
    return {
        "keyboard": [
            [{"text": "📊 Статистика"}, {"text": "📌 Позиции"}],
            [{"text": "💰 Баланс"}, {"text": "🕒 Статус сессий"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def format_stats_report() -> str:
    """Формирует отчет по истории сделок и PnL по обоим рынкам."""
    lines = [
        "📊 <b>СВОДНАЯ СТАТИСТИКА ТОРГОВЛИ (ICT TOOLKIT)</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    # 1. Статистика Bitget Crypto
    bg_trades = load_json_data(BITGET_TRADES_PATH, ROOT_BITGET_TRADES_PATH, default=[])

    bg_closed = [t for t in bg_trades if t.get("status") == "CLOSED"]
    bg_open = [t for t in bg_trades if t.get("status") == "OPEN"]

    lines.append("🪙 <b>Bitget USDT-M Фьючерсы:</b>")
    lines.append(f"  • Всего сделок: <code>{len(bg_trades)}</code> (Открыто: <code>{len(bg_open)}</code> | Закрыто: <code>{len(bg_closed)}</code>)")

    if bg_closed:
        wins = [t for t in bg_closed if t.get("net_pnl", 0) > 0]
        losses = [t for t in bg_closed if t.get("net_pnl", 0) <= 0]
        wr = (len(wins) / len(bg_closed)) * 100.0 if bg_closed else 0.0
        total_net = sum(t.get("net_pnl", 0) for t in bg_closed)
        total_fees = sum(t.get("total_fee", 0) for t in bg_closed)
        total_r = sum(t.get("net_r", 0) for t in bg_closed)
        pnl_sign = "+" if total_net > 0 else ""

        lines.append(f"  • Винрейт: <b>{wr:.1f}%</b> ({len(wins)}W / {len(losses)}L)")
        lines.append(f"  • Чистый PnL: <b>{pnl_sign}{total_net:,.2f} USDT</b>")
        lines.append(f"  • Накопленный Net R: <b>{total_r:+.2f}R</b>")
        lines.append(f"  • Уплачено комиссий: <code>${total_fees:.3f} USDT</code>")
    else:
        lines.append("  • Закрытых сделок пока нет.")

    lines.append("")

    # 2. Статистика T-Bank MOEX
    tb_trades = load_json_data(TBANK_TRADES_PATH, ROOT_TBANK_TRADES_PATH, default=[])

    tb_closed = [t for t in tb_trades if t.get("status") == "CLOSED"]
    lines.append("🇷🇺 <b>Т-Банк (Акции Мосбиржи):</b>")
    lines.append(f"  • Закрытых сделок: <code>{len(tb_closed)}</code>")

    if tb_closed:
        tb_wins = [t for t in tb_closed if t.get("net_pnl_rub", 0) > 0]
        tb_losses = [t for t in tb_closed if t.get("net_pnl_rub", 0) <= 0]
        tb_wr = (len(tb_wins) / len(tb_closed)) * 100.0 if tb_closed else 0.0
        tb_net = sum(t.get("net_pnl_rub", 0) for t in tb_closed)
        tb_r = sum(t.get("net_r", 0) for t in tb_closed)
        pnl_sign_tb = "+" if tb_net > 0 else ""

        lines.append(f"  • Винрейт: <b>{tb_wr:.1f}%</b> ({len(tb_wins)}W / {len(tb_losses)}L)")
        lines.append(f"  • Чистый PnL: <b>{pnl_sign_tb}{tb_net:,.2f} RUB</b>")
        lines.append(f"  • Накопленный Net R: <b>{tb_r:+.2f}R</b>")
    else:
        lines.append("  • Закрытых сделок пока нет.")

    # Последние сделки
    if bg_closed:
        lines.append("\n<b>Последние закрытые сделки (Bitget):</b>")
        for t in bg_closed[-3:]:
            pnl_s = "+" if t.get("net_pnl", 0) > 0 else ""
            lines.append(
                f"  ▫️ <code>{t['symbol']}</code> ({t.get('direction', '')}): "
                f"<b>{pnl_s}{t.get('net_pnl', 0):,.2f} USD</b> ({t.get('net_r', 0):+.2f}R) | {t.get('exit_reason', '')[:20]}"
            )

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_positions_report() -> str:
    """Формирует отчет по активным открытым позициям."""
    lines = [
        "📌 <b>АКТИВНЫЕ ОТКРЫТЫЕ ПОЗИЦИИ</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    total_open = 0

    # 1. Bitget
    bg_trades = load_json_data(BITGET_TRADES_PATH, ROOT_BITGET_TRADES_PATH, default=[])

    bg_open = [t for t in bg_trades if t.get("status") == "OPEN"]
    lines.append(f"🪙 <b>Bitget Фьючерсы ({len(bg_open)}/5):</b>")
    if bg_open:
        for t in bg_open:
            total_open += 1
            sr = t.get("setup_reason")
            sr_text = ""
            if isinstance(sr, dict) and sr.get("bias_desc"):
                sr_text = f"\n     ↳ <i>Сетап: {sr.get('bias_desc')} свип @ {sr.get('sweep_price', 0):.2f} (FVG [{sr.get('fvg_bottom', 0):.2f}-{sr.get('fvg_top', 0):.2f}])</i>"
            lines.append(
                f"  • <b>{t['symbol']}</b> (<code>{t['direction']}</code>)\n"
                f"    💵 Вход: <code>{t['entry_price']:,.4f}</code> | Объем: <code>{t['amount']}</code>\n"
                f"    🛑 SL: <code>{t['stop_loss']:,.4f}</code> | 🎯 TP: <code>{t['take_profit']:,.4f}</code>"
                f"{sr_text}"
            )
    else:
        lines.append("  <i>Открытых позиций по крипте сейчас нет.</i>")

    lines.append("")

    # 2. T-Bank
    tb_pos = load_json_data(TBANK_POSITIONS_PATH, ROOT_TBANK_POSITIONS_PATH, default={})

    lines.append(f"🇷🇺 <b>Т-Банк Мосбиржа ({len(tb_pos)}/5):</b>")
    if tb_pos:
        for ticker, pos in tb_pos.items():
            total_open += 1
            sr = pos.get("setup_reason")
            sr_text = ""
            if isinstance(sr, dict) and sr.get("bias_desc"):
                sr_text = f"\n     ↳ <i>Сетап: {sr.get('bias_desc')} свип @ {sr.get('sweep_price', 0):.2f}</i>"
            lines.append(
                f"  • <b>{ticker}</b> (<code>{pos.get('dir', 'LONG')}</code>)\n"
                f"    💵 Вход: <code>{pos.get('entry_price', 0):,.2f} RUB</code> | Лотов: <code>{pos.get('remaining_lots', 0)}/{pos.get('total_lots', 0)}</code>\n"
                f"    🛑 Текущий стоп: <code>{pos.get('current_stop', 0):,.2f} RUB</code> | 🎯 TP1: <code>{pos.get('tp1_price', 0):,.2f} RUB</code>"
                f"{sr_text}"
            )
    else:
        lines.append("  <i>Открытых позиций по акциям сейчас нет.</i>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    if total_open == 0:
        lines.append("💡 <i>Боты непрерывно сканируют рынок на предмет ликвидности и FVG. При появлении валидного входа вы сразу получите уведомление.</i>")

    return "\n".join(lines)


def format_balance_report() -> str:
    """Формирует отчет по балансам счетов."""
    lines = [
        "💰 <b>ТЕКУЩИЕ БАЛАНСЫ СЧЕТОВ</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Bitget баланс
    try:
        import ccxt
        apiKey = getattr(cfg, "BITGET_API_KEY", "")
        secret = getattr(cfg, "BITGET_API_SECRET", "")
        password = getattr(cfg, "BITGET_API_PASSWORD", "")
        if apiKey and secret and password:
            exchange = ccxt.bitget({
                "apiKey": apiKey,
                "secret": secret,
                "password": password,
                "options": {"defaultType": "swap"},
                "enableRateLimit": True,
            })
            bal = exchange.fetch_balance()
            usdt = bal.get("USDT", {})
            free_u = float(usdt.get("free", bal.get("free", {}).get("USDT", 0.0) or 0.0))
            total_u = float(usdt.get("total", bal.get("total", {}).get("USDT", 0.0) or 0.0))
            lines.append("🪙 <b>Bitget (USDT-M Фьючерсы):</b>")
            lines.append(f"  • Свободно для сделок: <b>{free_u:,.2f} USDT</b>")
            lines.append(f"  • Общий капитал (Equity): <b>{total_u:,.2f} USDT</b>")
        else:
            lines.append("🪙 <b>Bitget:</b> <i>API-ключи не настроены</i>")
    except Exception as e:
        lines.append(f"🪙 <b>Bitget:</b> <i>Не удалось получить баланс: {e}</i>")

    lines.append("")

    # T-Bank баланс
    try:
        try:
            from t_tech.invest import Client
        except ImportError:
            from tinkoff.invest import Client
        tb_token = getattr(cfg, "TBANK_TOKEN", "") or os.environ.get("TBANK_TOKEN", "")
        account_id = getattr(cfg, "TBANK_ACCOUNT_ID", "")
        sandbox = getattr(cfg, "TBANK_SANDBOX", False)
        if tb_token and account_id:
            with Client(tb_token) as client:
                pos = client.operations.get_positions(account_id=account_id)
                rub_val = 0.0
                for m in pos.money:
                    if m.currency.lower() == "rub":
                        rub_val = float(m.units + m.nano / 1e9)
                lines.append("🇷🇺 <b>Т-Банк (Брокерский счет):</b>")
                lines.append(f"  • Свободно рублей: <b>{rub_val:,.2f} RUB</b>")
                lines.append(f"  • ID счета: <code>{account_id}</code>")
        else:
            lines.append("🇷🇺 <b>Т-Банк:</b> <i>Токен или ID счёта не указан</i>")
    except Exception as e:
        lines.append(f"🇷🇺 <b>Т-Банк:</b> <i>Не удалось получить баланс: {e}</i>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_status_report() -> str:
    """Формирует отчет о сессиях, расписании и статусе работы."""
    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc + timedelta(hours=3)

    # Определение сессии
    hour_utc = now_utc.hour
    if 0 <= hour_utc < 6:
        session_name = "🌏 Asian Session (Азиатская сессия)"
    elif 7 <= hour_utc <= 10:
        session_name = "🇬🇧 London Killzone (Лондонское окно)"
    elif 12 <= hour_utc <= 15:
        session_name = "🇺🇸 New York Killzone (Нью-Йоркское окно)"
    else:
        session_name = "🌐 Вне основных Киллзон (Межсессионный мониторинг)"

    # Статус Мосбиржи
    is_weekend = now_msk.weekday() >= 5
    m_min = now_msk.hour * 60 + now_msk.minute
    moex_open = False
    if not is_weekend:
        if (10 * 60 <= m_min <= 18 * 60 + 40) or (19 * 60 + 5 <= m_min <= 23 * 60 + 50):
            moex_open = True

    moex_status = "🟢 Торги идут (Открыта)" if moex_open else "🔴 Торги закрыты"

    lines = [
        "🕒 <b>СТАТУС СЕССИЙ И РАСПИСАНИЕ БИРЖ</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"⏱ <b>Время MSK:</b> <code>{now_msk.strftime('%Y-%m-%d %H:%M:%S')}</code>",
        f"⏱ <b>Время UTC:</b> <code>{now_utc.strftime('%H:%M:%S')}</code>",
        "",
        f"🎯 <b>Текущая сессия:</b> {session_name}",
        f"🏛 <b>Мосбиржа (MOEX):</b> {moex_status}",
        "  • <i>Основная сессия: 10:00 — 18:40 МСК</i>",
        "  • <i>Вечерняя сессия: 19:05 — 23:50 МСК</i>",
        "",
        "🤖 <b>Фоновые демоны торговли:</b>",
        "  • Bitget Crypto: 🟢 <b>Активен</b> (5 пар, Asian Range, риск 1.0%)",
        "  • T-Bank MOEX:   🟢 <b>Активен</b> (SBER, ROSN, T, GAZP, риск 1.0%)",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def format_help_message() -> str:
    """Справка по доступным командам бота."""
    return (
        "🤖 <b>ICT TOOLKIT: КОМАНДЫ БОТА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Используйте кнопки на клавиатуре внизу или команды:\n\n"
        "📊 <b>/stats</b> — Полная статистика сделок, винрейт, Net PnL, Net R и комиссии биржи\n"
        "📌 <b>/positions</b> — Все текущие открытые позиции с ценами входа, SL, TP и сетапом\n"
        "💰 <b>/balance</b> — Баланс USDT на Bitget и рублей на счете Т-Банка\n"
        "🕒 <b>/status</b> — Текущая торговая сессия (Asia/London/NY) и расписание биржи\n"
        "❓ <b>/help</b> — Показать это справочное сообщение\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Уведомления о сделках, тейках 1.5R и марже приходят автоматически!</i>"
    )


def handle_command(text: str) -> tuple[str, dict]:
    """Обрабатывает входящую текстовую команду и возвращает ответ с кнопками."""
    cmd = text.strip().lower()
    keyboard = get_main_keyboard()

    if cmd in ("/start", "/help", "помощь", "❓ помощь", "help"):
        return format_help_message(), keyboard
    elif cmd in ("/stats", "статистика", "📊 статистика", "stats"):
        return format_stats_report(), keyboard
    elif cmd in ("/positions", "позиции", "📌 позиции", "pos"):
        return format_positions_report(), keyboard
    elif cmd in ("/balance", "баланс", "💰 баланс", "bal"):
        return format_balance_report(), keyboard
    elif cmd in ("/status", "статус", "🕒 статус сессий", "status"):
        return format_status_report(), keyboard
    else:
        return (
            f"Неизвестная команда: <code>{html.escape(text)}</code>\n\n"
            f"Используйте кнопки внизу экрана или введите /stats, /positions, /balance, /status",
            keyboard,
        )


def answer_callback_query(callback_query_id: str, text: str = ""):
    """Отправляет всплывающее уведомление в Telegram клиенте при нажатии кнопки."""
    token, chat_id, proxy, base_url = get_telegram_credentials()
    url = f"{base_url}/bot{token}/answerCallbackQuery"
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        requests.post(
            url,
            json={"callback_query_id": callback_query_id, "text": text},
            proxies=proxies,
            verify=False,
            timeout=8,
        )
    except Exception as e:
        print(f"⚠️ Ошибка answerCallbackQuery: {e}")


def edit_message_caption(message_id: int, new_caption: str, reply_markup: dict = None):
    """Обновляет подпись к отправленному фото (убирает кнопки или меняет статус)."""
    token, chat_id, proxy, base_url = get_telegram_credentials()
    url = f"{base_url}/bot{token}/editMessageCaption"
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if reply_markup is None:
        reply_markup = {"inline_keyboard": []}
    try:
        requests.post(
            url,
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": new_caption,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            },
            proxies=proxies,
            verify=False,
            timeout=8,
        )
    except Exception as e:
        print(f"⚠️ Ошибка editMessageCaption: {e}")


def handle_callback_query(cb_id: str, data: str, msg_id: int, orig_caption: str):
    """Обрабатывает нажатия инлайн-кнопок подтверждения или отклонения сделки."""
    print(f"🔘 Нажата инлайн-кнопка: '{data}' (Msg ID: {msg_id})")
    if data.startswith("confirm:"):
        sig_id = data.split(":", 1)[1]
        ok = ps.approve_signal(sig_id)
        if ok:
            answer_callback_query(cb_id, "✅ Сделка подтверждена! Ордер отправлен на биржу.")
            new_caption = (
                orig_caption + "\n\n"
                "⏳ <b>СТАТУС: ПОДТВЕРЖДЕНО ПОЛЬЗОВАТЕЛЕМ</b>\n"
                "<i>Ордер передан торговому роботу на исполнение...</i>"
            )
            if msg_id:
                edit_message_caption(msg_id, new_caption)
        else:
            answer_callback_query(cb_id, "⚠️ Сигнал уже обработан или истек (TTL).")

    elif data.startswith("reject:"):
        sig_id = data.split(":", 1)[1]
        ps.reject_signal(sig_id)
        answer_callback_query(cb_id, "❌ Сетап отклонен.")
        new_caption = (
            orig_caption + "\n\n"
            "❌ <b>СТАТУС: СЕТАП ОТКЛОНЕН</b>\n"
            "<i>Сделка отменена пользователем.</i>"
        )
        if msg_id:
            edit_message_caption(msg_id, new_caption)

    elif data.startswith("sample_"):
        answer_callback_query(cb_id, "Это демонстрация кнопок.")


def run_bot_listener():
    """Основной цикл polling для получения команд из Telegram."""
    token, chat_id, proxy, base_url = get_telegram_credentials()
    if not token or not chat_id:
        print("❌ Ошибка: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы в .env файле!")
        sys.exit(1)

    print("=" * 70)
    print("      ЗАПУСК ИНТЕРАКТИВНОГО ТЕЛЕГРАМ-БОТА ICT TOOLKIT")
    print("=" * 70)
    print(f"Token:    {'[УКАЗАН: ' + token[:8] + '...]'}")
    print(f"Chat ID:  {chat_id}")
    print(f"Proxy:    {proxy if proxy else '[Прямое подключение]'}")
    print(f"Base URL: {base_url}")
    print("Слушатель запущен. Ожидание команд пользователя (/stats, /positions)...")
    print("=" * 70 + "\n")

    opener = get_opener()
    offset = None

    while True:
        try:
            # Очистка устаревших сигналов
            ps.clean_stale_signals(ttl_sec=600)

            url = f"{base_url}/bot{token}/getUpdates?timeout=20"
            if offset is not None:
                url += f"&offset={offset}"

            req = urllib.request.Request(
                url,
                headers={"User-Agent": "ICT-Toolkit-Bot/1.0"},
                method="GET",
            )
            with opener.open(req, timeout=25) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("ok"):
                        updates = data.get("result", [])
                        for u in updates:
                            update_id = u.get("update_id")
                            offset = update_id + 1

                            # 1. Проверяем callback_query (нажатия инлайн-кнопок)
                            cb = u.get("callback_query")
                            if cb:
                                cb_id = cb.get("id")
                                cb_data = cb.get("data", "")
                                user_id = str(cb.get("from", {}).get("id", ""))
                                cb_chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                                msg_id = cb.get("message", {}).get("message_id")
                                orig_caption = cb.get("message", {}).get("caption", "")

                                if user_id == chat_id or cb_chat_id == chat_id:
                                    handle_callback_query(cb_id, cb_data, msg_id, orig_caption)
                                else:
                                    print(f"⚠️ Отклонен callback от неавторизованного пользователя: user={user_id}, chat={cb_chat_id}")
                                continue

                            # 2. Обычные текстовые сообщения
                            msg = u.get("message", {})
                            msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                            msg_text = msg.get("text", "")

                            # Безопасность: отвечаем только владельцу бота!
                            if msg_chat_id != chat_id:
                                print(f"⚠️ Отклонен запрос от неавторизованного пользователя: {msg_chat_id}")
                                continue

                            if not msg_text:
                                continue

                            user_name = msg.get("from", {}).get("first_name", "User")
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] Команда от {user_name}: '{msg_text}'")

                            reply_text, reply_kb = handle_command(msg_text)
                            send_bot_reply(reply_text, reply_markup=reply_kb)

        except urllib.error.URLError as e:
            time.sleep(3)
        except Exception as e:
            print(f"⚠️ [Telegram Listener] Ошибка в цикле polling: {e}")
            time.sleep(3)


if __name__ == "__main__":
    run_bot_listener()

