"""
telegram_notifier.py - Модуль Telegram-уведомлений для торговых ботов ICT Toolkit.

Поддерживает:
- Уведомления об открытии новых позиций (с деталями сетапа, SL, TP, риска)
- Уведомления о взятии частичной прибыли (1.5R) и переводе стопа в безубыток
- Уведомления о закрытии позиций (TP, SL, Manual) с итоговым Net PnL и Net R
- Предупреждения о нехватке маржи / баланса при обнаружении сетапа
- Уведомления о запуске/остановке ботов
- Тестирование связи с Telegram (--test)

Работает через стандартную библиотеку urllib (без внешних зависимостей),
вызовы защищены тайм-аутом 5с и не блокируют торговый процесс при сбоях сети.
"""

import os
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import html
import json
import ssl
import urllib.request
import urllib.error
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timezone

try:
    import config as cfg
except ImportError:
    cfg = None


def get_telegram_credentials():
    """Получает токен бота, chat_id, proxy и base_url из config или переменных окружения."""
    token = getattr(cfg, "TELEGRAM_BOT_TOKEN", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(cfg, "TELEGRAM_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
    proxy = getattr(cfg, "TELEGRAM_PROXY", "") or os.environ.get("TELEGRAM_PROXY", "") or os.environ.get("HTTPS_PROXY", "")
    base_url = (getattr(cfg, "TELEGRAM_BASE_URL", "") or os.environ.get("TELEGRAM_BASE_URL", "")).strip().rstrip("/")
    if not base_url:
        base_url = "https://api.telegram.org"
    return token.strip(), str(chat_id).strip(), proxy.strip(), base_url


def send_telegram_message(text: str, silent: bool = False) -> bool:
    """
    Отправляет сообщение в Telegram через HTTP Bot API или Reverse Proxy.
    Возвращает True при успешной отправке, False при ошибке или если ключи не заданы.
    Никогда не вызывает исключений во внешнем коде.
    """
    token, chat_id, proxy, base_url = get_telegram_credentials()
    if not token or not chat_id:
        return False

    url = f"{base_url}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "ICT-Toolkit-Bot/1.0"},
            method="POST",
        )

        if proxy:
            proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            ssl_ctx = ssl._create_unverified_context()
            opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ssl_ctx))
            resp_ctx = opener.open(req, timeout=10)
        else:
            resp_ctx = urllib.request.urlopen(req, timeout=10)

        with resp_ctx as resp:
            if resp.status == 200:
                return True
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            pass
        print(f"⚠️ [Telegram API Error] HTTP {e.code}: {e.reason} ({err_body})")
    except urllib.error.URLError as e:
        if "timed out" in str(e).lower():
            print(f"⚠️ [Telegram Warning] Таймаут соединения с {base_url}. (Блокировка РКН на прямые адреса Telegram в РФ)")
            print("💡 Решение: включите VPN/прокси и укажите TELEGRAM_PROXY=http://127.0.0.1:ПОРТ или используйте TELEGRAM_BASE_URL.")
        else:
            print(f"⚠️ [Telegram Warning] Ошибка сети: {e}")
    except Exception as e:
        print(f"⚠️ [Telegram Warning] Не удалось отправить сообщение: {e}")

    return False


def send_telegram_photo(
    photo_bytes: bytes,
    caption: str,
    reply_markup: dict = None,
) -> dict:
    """
    Отправляет изображение (скриншот графика) с подписью и инлайн-клавиатурой.
    Возвращает dict ответа Telegram или None.
    """
    token, chat_id, proxy, base_url = get_telegram_credentials()
    if not token or not chat_id:
        return None

    url = f"{base_url}/bot{token}/sendPhoto"
    proxies = {"http": proxy, "https": proxy} if proxy else None

    data = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)

    files = {
        "photo": ("chart.png", photo_bytes, "image/png"),
    }

    env_ssl = os.environ.get("SSL_VERIFY") or os.environ.get("SSL_TBANK_VERIFY")
    if env_ssl is not None:
        ssl_verify = env_ssl.lower() in ("true", "1", "yes")
    else:
        ssl_verify = getattr(cfg, "SSL_VERIFY", True) if cfg else True

    try:
        resp = requests.post(url, data=data, files=files, proxies=proxies, verify=ssl_verify, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("result")
        else:
            print(f"⚠️ [Telegram Photo Error] {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"⚠️ [Telegram Warning] Не удалось отправить фото: {e}")

    return None


def notify_setup_proposal(
    market: str,
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    amount_str: str,
    risk_str: str,
    setup_reason: dict = None,
    chart_bytes: bytes = None,
    sig_id: str = "",
    is_off_session: bool = False,
) -> int:
    """
    Отправляет предложение по сделке со скриншотом 3-TF графика и кнопками [Открыть] / [Пропустить].
    Возвращает message_id отправленного сообщения.
    """
    dir_emoji = "🟢" if str(direction).upper() in ("LONG", "1") else "🔴"
    dir_name = "LONG" if str(direction).upper() in ("LONG", "1") else "SHORT"
    stop_pct = abs(entry_price - stop_loss) / max(entry_price, 0.0001) * 100
    tp_pct = abs(take_profit - entry_price) / max(entry_price, 0.0001) * 100

    sr_lines = []
    if setup_reason and isinstance(setup_reason, dict):
        bias = setup_reason.get("bias_desc", "")
        sweep_p = setup_reason.get("sweep_price", 0.0)
        fvg_bot = setup_reason.get("fvg_bottom", 0.0)
        fvg_top = setup_reason.get("fvg_top", 0.0)
        lvl_name = setup_reason.get("level_name")
        if lvl_name:
            sr_lines.append(f"📍 <b>Снят уровень:</b> {html.escape(lvl_name)}")
        if bias:
            sr_lines.append(f"💡 <b>Сетап:</b> {bias} свип @ {sweep_p:,.4f}")
        choch_conf = setup_reason.get("choch_confirmed")
        choch_tp = setup_reason.get("choch_type")
        choch_lvl = setup_reason.get("choch_level")
        if choch_conf and choch_tp:
            lvl_str = f" @ {choch_lvl:,.4f}" if choch_lvl else ""
            sr_lines.append(f"🔄 <b>Структурный слом (MSS):</b> {html.escape(str(choch_tp))}{lvl_str} ✅")
        if fvg_bot and fvg_top:
            sr_lines.append(f"📐 <b>FVG зона:</b> [{fvg_bot:,.4f} — {fvg_top:,.4f}]")
        ai_score = setup_reason.get("ai_score")
        ai_rec = setup_reason.get("ai_recommendation")
        if ai_score is not None:
            rec_str = f" ({ai_rec})" if ai_rec else ""
            sr_lines.append(f"🧠 <b>AI Оценка:</b> <code>{ai_score}/10</code>{rec_str}")
        strengths = setup_reason.get("ai_strengths", [])
        if strengths and isinstance(strengths, list):
            sr_lines.append("  ✅ <i>" + html.escape("; ".join(strengths[:2])) + "</i>")
        risks = setup_reason.get("ai_risks", [])
        if risks and isinstance(risks, list):
            sr_lines.append("  ⚠️ <i>" + html.escape("; ".join(risks[:2])) + "</i>")
        ai_reason = setup_reason.get("ai_reasoning")
        if ai_reason:
            sr_lines.append(f"  💬 <i>{html.escape(ai_reason)}</i>")

    setup_block = ("\n" + "\n".join(sr_lines)) if sr_lines else ""
    header = "💡 <b>ПРЕДЛОЖЕНИЕ СДЕЛКИ (ВНЕ КИЛЛЗОНЫ)</b>" if is_off_session else "⚡ <b>ПРЕДЛОЖЕНИЕ СДЕЛКИ (ТРЕБУЕТСЯ ПОДТВЕРЖДЕНИЕ)</b>"

    caption = (
        f"{header} [{html.escape(market)}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_emoji} <b>Инструмент:</b> <code>{html.escape(symbol)}</code> (<b>{dir_name}</b>)\n"
        f"💵 <b>Цена входа:</b> <code>{entry_price:,.4f}</code>\n"
        f"🛑 <b>Stop-Loss:</b> <code>{stop_loss:,.4f}</code> (-{stop_pct:.2f}%)\n"
        f"🎯 <b>Take-Profit:</b> <code>{take_profit:,.4f}</code> (+{tp_pct:.2f}%, 1.5R)\n"
        f"📊 <b>Объем:</b> {html.escape(amount_str)}\n"
        f"🛡️ <b>Риск:</b> {html.escape(risk_str)}"
        f"{setup_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>График 3-TF: 1H (Контекст/Тренд), 15M (Свип), 5M (Вход/FVG).</i>"
    )

    reply_markup = None
    if sig_id:
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": f"✅ Открыть {dir_name}", "callback_data": f"confirm:{sig_id}"},
                    {"text": "❌ Пропустить", "callback_data": f"reject:{sig_id}"},
                ]
            ]
        }

    if chart_bytes:
        res = send_telegram_photo(photo_bytes=chart_bytes, caption=caption, reply_markup=reply_markup)
        if res and "message_id" in res:
            return res["message_id"]

    # Fallback на текстовое сообщение при сбое генерации изображения
    send_telegram_message(caption)
    return None


def notify_bot_started(bot_name: str, mode: str, symbols: list, risk_pct: float, max_pos: int) -> bool:
    """Уведомление о старте торгового бота."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    syms_str = ", ".join(html.escape(s) for s in symbols)
    msg = (
        f"🤖 <b>БОТ ЗАПУЩЕН НА РЫНКЕ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Платформа:</b> {html.escape(bot_name)}\n"
        f"⚡ <b>Режим:</b> <code>{html.escape(mode.upper())}</code>\n"
        f"🎯 <b>Инструменты:</b> {syms_str}\n"
        f"🛡️ <b>Риск на сделку:</b> <code>{risk_pct:.1f}%</code>\n"
        f"📦 <b>Макс. позиций:</b> <code>{max_pos}</code>\n"
        f"🕒 <b>Время старта:</b> {now_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Система мониторит свипы ликвидности и FVG паттерны.</i>"
    )
    return send_telegram_message(msg)


def notify_trade_opened(
    market: str,
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    amount_str: str,
    risk_str: str,
    setup_reason: dict = None,
) -> bool:
    """Уведомление об открытии новой позиции."""
    dir_emoji = "🟢" if direction.upper() == "LONG" else "🔴"
    stop_pct = abs(entry_price - stop_loss) / entry_price * 100
    tp_pct = abs(take_profit - entry_price) / entry_price * 100

    sr_lines = []
    if setup_reason and isinstance(setup_reason, dict):
        bias = setup_reason.get("bias_desc", "")
        sweep_p = setup_reason.get("sweep_price", 0.0)
        fvg_bot = setup_reason.get("fvg_bottom", 0.0)
        fvg_top = setup_reason.get("fvg_top", 0.0)
        lvl_name = setup_reason.get("level_name")
        if lvl_name:
            sr_lines.append(f"📍 <b>Снят уровень:</b> {html.escape(lvl_name)}")
        if bias:
            sr_lines.append(f"💡 <b>Сетап:</b> {bias} свип @ {sweep_p:,.4f}")
        choch_conf = setup_reason.get("choch_confirmed")
        choch_tp = setup_reason.get("choch_type")
        choch_lvl = setup_reason.get("choch_level")
        if choch_conf and choch_tp:
            lvl_str = f" @ {choch_lvl:,.4f}" if choch_lvl else ""
            sr_lines.append(f"🔄 <b>Структурный слом (MSS):</b> {html.escape(str(choch_tp))}{lvl_str} ✅")
        if fvg_bot and fvg_top:
            sr_lines.append(f"📐 <b>FVG зона:</b> [{fvg_bot:,.4f} — {fvg_top:,.4f}]")
        ai_score = setup_reason.get("ai_score")
        ai_rec = setup_reason.get("ai_recommendation")
        if ai_score is not None:
            rec_str = f" ({ai_rec})" if ai_rec else ""
            sr_lines.append(f"🧠 <b>AI Оценка:</b> <code>{ai_score}/10</code>{rec_str}")
        strengths = setup_reason.get("ai_strengths", [])
        if strengths and isinstance(strengths, list):
            sr_lines.append("  ✅ <i>" + html.escape("; ".join(strengths[:2])) + "</i>")
        risks = setup_reason.get("ai_risks", [])
        if risks and isinstance(risks, list):
            sr_lines.append("  ⚠️ <i>" + html.escape("; ".join(risks[:2])) + "</i>")
        ai_reason = setup_reason.get("ai_reasoning")
        if ai_reason:
            sr_lines.append(f"  💬 <i>{html.escape(ai_reason)}</i>")

    setup_block = ("\n" + "\n".join(sr_lines)) if sr_lines else ""

    msg = (
        f"🚀 <b>НОВАЯ СДЕЛКА ОТКРЫТА</b> [{html.escape(market)}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_emoji} <b>Инструмент:</b> <code>{html.escape(symbol)}</code> (<b>{direction.upper()}</b>)\n"
        f"💵 <b>Цена входа:</b> <code>{entry_price:,.4f}</code>\n"
        f"🛑 <b>Stop-Loss:</b> <code>{stop_loss:,.4f}</code> (-{stop_pct:.2f}%)\n"
        f"🎯 <b>Take-Profit:</b> <code>{take_profit:,.4f}</code> (+{tp_pct:.2f}%, 1.5R)\n"
        f"📊 <b>Объем:</b> {html.escape(amount_str)}\n"
        f"🛡️ <b>Риск:</b> {html.escape(risk_str)}"
        f"{setup_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    return send_telegram_message(msg)


def notify_partial_take(
    market: str,
    symbol: str,
    direction: str,
    fill_price: float,
    closed_str: str,
    remaining_str: str,
    be_stop: float,
) -> bool:
    """Уведомление о взятии частичного тейка (1.5R) и переводе в безубыток."""
    msg = (
        f"🎯 <b>ЧАСТИЧНЫЙ ТЕЙК (1.5R) ДОСТИГНУТ!</b> [{html.escape(market)}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Инструмент:</b> <code>{html.escape(symbol)}</code> ({direction.upper()})\n"
        f"💵 <b>Цена фиксации:</b> <code>{fill_price:,.4f}</code>\n"
        f"📦 <b>Зафиксировано:</b> {html.escape(closed_str)}\n"
        f"📌 <b>Остаток в позиции:</b> {html.escape(remaining_str)}\n"
        f"🛡️ <b>Стоп перенесён в безубыток:</b> <code>{be_stop:,.4f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Позиция переведена в безрисковый трейлинг остатка.</i>"
    )
    return send_telegram_message(msg)


def notify_trade_closed(
    market: str,
    symbol: str,
    direction: str,
    exit_price: float,
    exit_reason: str,
    net_pnl: float,
    currency: str,
    net_r: float = None,
    total_fees: float = None,
) -> bool:
    """Уведомление о полном закрытии сделки (TP, SL, ручное)."""
    is_win = net_pnl > 0
    header_emoji = "🎉" if is_win else "🛑"
    pnl_sign = "+" if net_pnl > 0 else ""
    reason_label = {
        "TAKE_PROFIT": "Тейк-профит (1.5R)",
        "STOP_LOSS": "Стоп-лосс",
        "MANUAL": "Ручное закрытие",
        "TRAILING": "Трейлинг-стоп",
    }.get(exit_reason, exit_reason)

    r_str = f" ({net_r:+.2f}R)" if net_r is not None else ""
    fee_str = f"\n💳 <b>Комиссии биржи:</b> <code>{total_fees:.3f} {html.escape(currency)}</code>" if total_fees else ""

    msg = (
        f"{header_emoji} <b>СДЕЛКА ЗАКРЫТА: {html.escape(reason_label.upper())}</b> [{html.escape(market)}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Инструмент:</b> <code>{html.escape(symbol)}</code> ({direction.upper()})\n"
        f"🏁 <b>Цена выхода:</b> <code>{exit_price:,.4f}</code>\n"
        f"💵 <b>Чистый PnL:</b> <code>{pnl_sign}{net_pnl:,.2f} {html.escape(currency)}</code><b>{r_str}</b>"
        f"{fee_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    return send_telegram_message(msg)


def notify_sweep_detected(
    market: str,
    symbol: str,
    level_type: str,
    level_name: str,
    level_price: float,
    trigger_price: float,
    direction: int,
    bias_desc: str = "",
    in_killzone: bool = True,
    penetration_pct: float = 0.0,
    hunt_window_min: int = 45,
) -> bool:
    """Уведомление о проколе (свипе) уровня ликвидности и переходе в режим охоты."""
    dir_str = "LONG (Поиск отскока вверх)" if direction == 1 else "SHORT (Поиск отскока вниз)"
    dir_emoji = "🟢" if direction == 1 else "🔴"
    kz_str = "🟢 Да (Активная сессия)" if in_killzone else "🟡 Вне киллзоны (Контроль)"

    msg = (
        f"⚡ <b>СВИП ЛИКВИДНОСТИ ОБНАРУЖЕН!</b> [{html.escape(market)}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_emoji} <b>Инструмент:</b> <code>{html.escape(symbol)}</code>\n"
        f"📍 <b>Снят уровень:</b> <b>{html.escape(level_name)}</b>\n"
        f"🎯 <b>Цена уровня:</b> <code>{level_price:,.4f}</code>\n"
        f"⚡ <b>Цена прокола:</b> <code>{trigger_price:,.4f}</code> ({penetration_pct:+.2f}%)\n"
        f"🧭 <b>Ожидаемый вход:</b> <b>{dir_str}</b>\n"
        f"🕒 <b>Внутри Killzone:</b> {kz_str}\n"
        f"🏹 <b>Статус:</b> <i>Включен режим Охоты за FVG (окно {hunt_window_min} мин)</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Бот ожидает Displacement + FVG и перед входом запросит оценку у ИИ.</i>"
    )
    return send_telegram_message(msg)


def notify_ai_rejection(
    market: str,
    symbol: str,
    direction: str,
    price: float,
    ai_score: int,
    recommendation: str,
    risk_factors: list = None,
    reasoning: str = "",
) -> bool:
    """Уведомление об отклонении потенциальной сделки нейросетью Google Gemini."""
    dir_emoji = "🟢" if direction.upper() == "LONG" else "🔴"
    rf_lines = []
    if risk_factors and isinstance(risk_factors, list):
        for rf in risk_factors[:3]:
            rf_lines.append(f"  • {html.escape(str(rf))}")
    rf_block = ("\n⚠️ <b>Факторы риска:</b>\n" + "\n".join(rf_lines)) if rf_lines else ""
    reason_str = f"\n💬 <i>{html.escape(reasoning)}</i>" if reasoning else ""

    msg = (
        f"🛑 <b>СЕТАП ОТКЛОНЕН ИИ (Google Gemini)</b> [{html.escape(market)}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_emoji} <b>Инструмент:</b> <code>{html.escape(symbol)}</code> (<b>{direction.upper()}</b> @ {price:,.4f})\n"
        f"🧠 <b>Оценка ИИ:</b> <code>{ai_score}/10</code> (<b>{html.escape(recommendation)}</b>)"
        f"{rf_block}"
        f"{reason_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Сделка отменена риск-офицером ИИ для защиты депозита от ловушки.</i>"
    )
    return send_telegram_message(msg)


def notify_margin_warning(
    market: str,
    symbol: str,
    direction: str,
    price: float,
    required_amount: float,
    available_amount: float,
    currency: str,
    hint: str = "",
) -> bool:
    """Предупреждение о пропуске сетапа из-за нехватки свободных средств."""
    missing = max(0.0, required_amount - available_amount)
    hint_line = f"\n💡 <i>{html.escape(hint)}</i>" if hint else ""

    msg = (
        f"⚠️ <b>НЕДОСТАТОЧНО СРЕДСТВ ДЛЯ ВХОДА!</b> [{html.escape(market)}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Сетап:</b> <code>{html.escape(symbol)}</code> ({direction.upper()} @ {price:,.4f})\n"
        f"💳 <b>Требуется обеспечения:</b> <code>~{required_amount:,.2f} {html.escape(currency)}</code>\n"
        f"💰 <b>Доступно на счёте:</b> <code>{available_amount:,.2f} {html.escape(currency)}</code>\n"
        f"🔻 <b>Не хватает:</b> <code>~{missing:,.2f} {html.escape(currency)}</code>"
        f"{hint_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Сетап пропущен, чтобы не превышать лимиты маржи.</i>"
    )
    return send_telegram_message(msg)


if __name__ == "__main__":
    if "--test" in sys.argv:
        token, chat_id, proxy, base_url = get_telegram_credentials()
        print(f"Тестирование Telegram отправки...")
        print(f"  Token:    {'[УКАЗАН: ' + token[:8] + '...]' if token else '[НЕ ЗАДАН]'}")
        print(f"  Chat ID:  {chat_id if chat_id else '[НЕ ЗАДАН]'}")
        print(f"  Proxy:    {proxy if proxy else '[Прямое подключение]'}")
        print(f"  Base URL: {base_url}")

        if not token or not chat_id:
            print("\n❌ ОШИБКА: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы в .env файле!")
            print("Укажите их в файле .env:")
            print("  TELEGRAM_BOT_TOKEN=1234567890:ABC-DEF...")
            print("  TELEGRAM_CHAT_ID=123456789")
            sys.exit(1)

        test_msg = (
            "🔔 <b>ICT Toolkit: Тестовое уведомление</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Связь с ботом успешно настроена!\n"
            "Вы будете получать уведомления об:\n"
            " • Открытии сделок (с указанием сетапа и AI)\n"
            " • Взятии тейков 1.5R и переносе в безубыток\n"
            " • Закрытии сделок и PnL\n"
            " • Нехватке баланса / маржи на сделку"
        )
        ok = send_telegram_message(test_msg)
        if ok:
            print("\n🎉 Сообщение успешно доставлено в Telegram!")
        else:
            print("\n❌ Не удалось отправить тестовое сообщение. Проверьте правильность токена и Chat ID.")
    else:
        print("Использование: python telegram_notifier.py --test")
