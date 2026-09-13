"""
ai_circuit_breaker.py - Модуль ИИ-оценки тренда и защитного отключения (Circuit Breaker).

Реализует две ключевые защитные функции:
1. Оценка тренда в начале дня/сессии через Google Gemini:
   - Анализ 1H структуры рынка и волатильности.
   - Определение режима: TRENDING (торговать) vs CHOPPY_RANGE (пропуск).
2. Дневной Circuit Breaker (при 2 убытках подряд за день на паре):
   - Если пара ловит 2 убытка подряд за текущий день, торговля по ней останавливается.
   - Нейросеть Gemini анализирует контекст дня: есть ли тренд или рынок вошел в боковой распил.
   - При вердикте DISABLE_FOR_DAY пара блокируется до 00:00 UTC следующего дня.
   - Пользователю в Telegram отправляется срочное уведомление с вердиктом ИИ.
"""

import os
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import json
import html
from datetime import datetime, timezone, timedelta
import pandas as pd

import config as cfg
from telegram_notifier import send_telegram_message
from trend_filter import compute_adx

DATA_DIR = getattr(cfg, "DATA_DIR", "data")
DAILY_STATS_FILE = os.path.join(DATA_DIR, "daily_pair_stats.json")
DISABLED_PAIRS_FILE = os.path.join(DATA_DIR, "disabled_pairs.json")


def _get_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _get_tomorrow_midnight_utc() -> str:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat()


def is_pair_disabled_today(symbol: str) -> tuple[bool, str]:
    """
    Проверяет, заблокирован ли инструмент на сегодня из-за серии убытков или решения ИИ.
    """
    clean_sym = symbol.strip().upper()
    if not os.path.exists(DISABLED_PAIRS_FILE):
        return False, ""

    try:
        with open(DISABLED_PAIRS_FILE, "r", encoding="utf-8") as f:
            disabled = json.load(f)

        item = disabled.get(clean_sym)
        if not item:
            # Проверяем базовый тикер (например DOGE/USDT для DOGE/USDT:USDT)
            base = clean_sym.split(":")[0]
            item = disabled.get(base)

        if item:
            until_str = item.get("disabled_until", "")
            if until_str:
                until_dt = datetime.fromisoformat(until_str)
                now_utc = datetime.now(timezone.utc)
                if until_dt.tzinfo is None:
                    until_dt = until_dt.replace(tzinfo=timezone.utc)
                if now_utc < until_dt:
                    return True, item.get("reason", "Disabled by AI Circuit Breaker")
                else:
                    # Срок блокировки истек - удаляем
                    del disabled[clean_sym]
                    with open(DISABLED_PAIRS_FILE, "w", encoding="utf-8") as f:
                        json.dump(disabled, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Ошибка чтения {DISABLED_PAIRS_FILE}: {e}")

    return False, ""


def evaluate_circuit_breaker_ai(symbol: str, consecutive_losses: int, df_1h: pd.DataFrame = None) -> dict:
    """
    Запрашивает у Gemini анализ рыночного режима при серии убытков.
    """
    api_key = getattr(cfg, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    model_name = getattr(cfg, "GEMINI_MODEL", "gemini-3.5-flash")

    # Сводка недавних свечей 1H
    recent_info = "N/A"
    if df_1h is not None and len(df_1h) >= 10:
        tail = df_1h.tail(6)
        recent_info = tail[["open", "high", "low", "close"]].to_string()

    prompt = f"""
Ты — институциональный риск-офицер торгового фонда.
Инструмент {symbol} сегодня получил {consecutive_losses} УБЫТОЧНЫХ СДЕЛКИ ПОДРЯД.

Последние 6 часов свечей 1H:
{recent_info}

Твоя задача — защитить депозит от бокового распила и ложных пробоев во флэте.
Определи:
1. Присутствует ли на инструменте сильный направленный тренд (Displacement / Higher Highs / Lower Lows)?
2. Или рынок перешел в боковой распил (Chop / Range / Fakeouts)?
3. Нужно ли ОТКЛЮЧИТЬ торговлю по этому инструменту до конца сегодняшнего дня?

Требования к ответу (строго JSON):
{{
  "action": "DISABLE_FOR_DAY" или "ALLOW_CONTINUE",
  "regime": "CHOPPY_RANGE" или "TRENDING",
  "reason": "Краткое обоснование в 2 предложениях для трейдера"
}}
"""
    if api_key:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                )
            )
            data = json.loads(resp.text)
            return data
        except Exception as e:
            print(f"⚠️ Ошибка вызова Gemini в Circuit Breaker: {e}")

    # Fallback при отсутствии API-ключа:
    # 2 убытка подряд в один день - строгое отключение (консервативный риск-менеджмент)
    return {
        "action": "DISABLE_FOR_DAY",
        "regime": "CHOPPY_RANGE",
        "reason": f"Зафиксировано {consecutive_losses} убытка подряд за день. Включена защитная блокировка до завтра для сохранения депозита."
    }


def record_trade_and_check_circuit_breaker(symbol: str, outcome: str, r_net: float,
                                           exit_time: str = None, df_1h: pd.DataFrame = None) -> dict:
    """
    Фиксирует результат сделки в статистике дня и проверяет срабатывание Circuit Breaker.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    today = _get_today_str()
    clean_sym = symbol.strip().upper()
    base_sym = clean_sym.split(":")[0]

    stats = {}
    if os.path.exists(DAILY_STATS_FILE):
        try:
            with open(DAILY_STATS_FILE, "r", encoding="utf-8") as f:
                stats = json.load(f)
        except Exception:
            stats = {}

    if today not in stats:
        stats[today] = {}
    if base_sym not in stats[today]:
        stats[today][base_sym] = {
            "trades": [],
            "consecutive_losses": 0,
            "status": "ACTIVE",
            "total_r": 0.0,
        }

    pair_data = stats[today][base_sym]
    pair_data["trades"].append({
        "time": exit_time or datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "r": round(r_net, 3),
    })
    pair_data["total_r"] = round(pair_data["total_r"] + r_net, 3)

    is_loss = r_net <= 0.001
    if is_loss:
        pair_data["consecutive_losses"] += 1
    else:
        pair_data["consecutive_losses"] = 0

    cons_losses = pair_data["consecutive_losses"]
    circuit_triggered = False
    ai_verdict = None

    if cons_losses >= 2 and pair_data["status"] != "DISABLED":
        print(f"\n🚨 [Circuit Breaker Triggered] {base_sym}: {cons_losses} убытка подряд за сегодня! Запрос ИИ-анализа...")
        ai_verdict = evaluate_circuit_breaker_ai(base_sym, cons_losses, df_1h=df_1h)
        action = ai_verdict.get("action", "DISABLE_FOR_DAY")
        reason = ai_verdict.get("reason", "2 consecutive losses on choppy market")

        if action == "DISABLE_FOR_DAY":
            circuit_triggered = True
            pair_data["status"] = "DISABLED"
            pair_data["disabled_until"] = _get_tomorrow_midnight_utc()
            pair_data["disable_reason"] = reason

            # Сохраняем в disabled_pairs.json
            disabled_dict = {}
            if os.path.exists(DISABLED_PAIRS_FILE):
                try:
                    with open(DISABLED_PAIRS_FILE, "r", encoding="utf-8") as f:
                        disabled_dict = json.load(f)
                except Exception:
                    disabled_dict = {}

            disabled_dict[base_sym] = {
                "disabled_until": _get_tomorrow_midnight_utc(),
                "reason": reason,
                "triggered_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(DISABLED_PAIRS_FILE, "w", encoding="utf-8") as f:
                json.dump(disabled_dict, f, indent=4, ensure_ascii=False)

            # Отправка срочного алерта в Telegram
            tg_msg = (
                f"🚨 <b>ИИ CIRCUIT BREAKER: {base_sym} ОТКЛЮЧЕН ДО ЗАВТРА</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"Зафиксировано <b>{cons_losses} убытка подряд</b> за сегодня (Итог: {pair_data['total_r']:+.2f}R).\n\n"
                f"🤖 <b>Решение ИИ:</b> ОСТАНОВИТЬ ТОРГОВЛЮ\n"
                f"💡 <b>Причина:</b> <i>{reason}</i>\n\n"
                f"<i>Защита депозита активирована. Новые ордера по {base_sym} заблокированы до 00:00 UTC.</i>"
            )
            send_telegram_message(tg_msg)

    # Сохраняем обновленную дневную статистику
    try:
        with open(DAILY_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Ошибка записи {DAILY_STATS_FILE}: {e}")

    return {
        "consecutive_losses": cons_losses,
        "circuit_triggered": circuit_triggered,
        "status": pair_data["status"],
        "ai_verdict": ai_verdict,
    }


def fetch_1h_for_symbol(symbol: str, exchange=None, market: str = "crypto") -> pd.DataFrame:
    """
    Загружает свечи 1H для инструмента:
    - Через ccxt (биржевой fetch_ohlcv на 50 баров)
    - Либо через локальный кэш/историю data_sources.get_data.
    """
    clean_sym = symbol.strip().upper()
    base_sym = clean_sym.split(":")[0]

    # 1. Если передан ccxt exchange (например в live_trade.py)
    if exchange is not None and market == "crypto":
        try:
            swap_sym = symbol if ":" in symbol else f"{symbol}:USDT"
            bars = exchange.fetch_ohlcv(swap_sym, timeframe="1h", limit=50)
            if bars and len(bars) >= 15:
                df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df.set_index("timestamp", inplace=True)
                return df
        except Exception:
            pass

    # 2. Fallback через data_sources
    try:
        from data_sources import get_data
        start_date = (pd.Timestamp.now(timezone.utc) - pd.Timedelta(days=7)).tz_localize(None)
        df_1m = get_data(cfg, symbol=base_sym, start_date=start_date, use_cache=True)
        if len(df_1m) >= 60:
            from strategy import resample
            df_1h = resample(df_1m, "1h")
            return df_1h
    except Exception:
        pass

    return pd.DataFrame()


def evaluate_pre_trading_universe_ai(symbols: list[str], exchange=None, market: str = "crypto",
                                     df_map: dict = None, send_tg: bool = True) -> dict:
    """
    Институциональная пре-маркет оценка пула инструментов через Gemini:
    1. Запрашивает 1H свечи по каждому тикеру.
    2. Вычисляет ADX(14), EMA 20/50, размах волатильности за 24ч.
    3. Отправляет сводку в Gemini с запросом классификации (TRENDING vs CHOPPY_RANGE).
    4. При вердикте SKIP_FOR_DAY инструмент временно отключается до 00:00 UTC.
    5. Отправляет подробный отчет в Telegram и выводит сводку в консоль.
    """
    if not symbols:
        return {"results": {}, "allowed_symbols": [], "skipped_symbols": []}

    print(f"\n🧠 [AI Pre-Trade] Оценка рыночного режима по корзине {market.upper()} ({len(symbols)} пар)...")

    symbol_data = {}
    for s in symbols:
        clean = s.strip().upper()
        base = clean.split(":")[0]

        df_1h = None
        if df_map and base in df_map:
            df_1h = df_map[base]
        elif df_map and clean in df_map:
            df_1h = df_map[clean]
        else:
            df_1h = fetch_1h_for_symbol(s, exchange, market)

        if df_1h is None or len(df_1h) < 15:
            symbol_data[clean] = {"adx": 20.0, "ema_bias": "NEUTRAL", "range_24h": 2.0, "price": 0.0, "df": None}
            continue

        adx_s = compute_adx(df_1h, period=14)
        curr_adx = round(float(adx_s.iloc[-1]), 1)
        close = df_1h["close"]
        ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        curr_p = float(close.iloc[-1])

        tail_24 = df_1h.tail(24)
        h24 = tail_24["high"].max()
        l24 = tail_24["low"].min()
        range_pct = round(((h24 - l24) / curr_p) * 100.0, 1) if curr_p > 0 else 0.0
        ema_bias = "BULLISH" if ema20 >= ema50 else "BEARISH"

        recent_bars = tail_24.tail(6)[["open", "high", "low", "close"]].to_dict(orient="records")

        symbol_data[clean] = {
            "adx": curr_adx,
            "ema_bias": ema_bias,
            "range_24h": range_pct,
            "price": curr_p,
            "recent_bars": recent_bars,
            "df": df_1h,
        }

    # Формируем сводный промпт для Gemini
    api_key = getattr(cfg, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    model_name = getattr(cfg, "GEMINI_MODEL", "gemini-3.5-flash")

    verdicts = {}
    used_ai = False

    if api_key and symbol_data:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)

            symbols_desc = []
            for sym, d in symbol_data.items():
                symbols_desc.append(
                    f"Инструмент: {sym} | Цена: {d['price']} | 1H ADX: {d['adx']} | 1H Тренд: {d['ema_bias']} | 24ч диапазон: {d['range_24h']}%\n"
                    f"  Последние свечи 1H: {json.dumps(d.get('recent_bars', []))}"
                )
            all_desc = "\n\n".join(symbols_desc)

            prompt = f"""
Ты — главный квант-аналитик и директор по рискам институционального фонда.
Перед началом торговой сессии оцени готовность каждого инструмента к внутридневной трендовой торговле (ICT Breakout / FVG Expansion) на рынке {market.upper()}.

Данные по инструментам:
{all_desc}

Твоя задача — защитить депозит от бокового распила и ложных пробоев во флэте.
Критерии:
1. "ALLOW_TRADE": инструмент находится в выраженном направленном тренде (импульсные движения, ADX >= 20, понятная структура).
2. "SKIP_FOR_DAY": инструмент зажат в узком боковике, слабой компрессии или пиле (ADX < 20, перекрытие свечей, отсутствие дисплейсмента).

Верни строго JSON со словарем по каждому инструменту в формате:
{{
  "<SYMBOL>": {{
    "action": "ALLOW_TRADE" или "SKIP_FOR_DAY",
    "regime": "TRENDING" или "CHOPPY_RANGE",
    "confidence": число от 1 до 10,
    "bias": "LONG" или "SHORT" или "NEUTRAL",
    "reason": "Краткое обоснование в 1 предложении"
  }}
}}
"""
            models_to_try = [model_name, "gemini-3.5-flash", "gemini-flash-latest"]
            for m_name in models_to_try:
                try:
                    resp = client.models.generate_content(
                        model=m_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.2,
                        )
                    )
                    parsed = json.loads(resp.text)
                    if isinstance(parsed, dict):
                        verdicts = parsed
                        used_ai = True
                        model_name = m_name
                        break
                except Exception as model_err:
                    print(f"⚠️ Ошибка Gemini ({m_name}): {model_err}")
        except Exception as e:
            print(f"⚠️ Ошибка вызова Gemini в AI Pre-Trade: {e}. Применен квант-фоллбэк.")

    # Fallback / санитизация вердиктов
    final_results = {}
    allowed = []
    skipped = []

    # Чтение существующих заблокированных пар
    disabled_dict = {}
    if os.path.exists(DISABLED_PAIRS_FILE):
        try:
            with open(DISABLED_PAIRS_FILE, "r", encoding="utf-8") as f:
                disabled_dict = json.load(f)
        except Exception:
            disabled_dict = {}

    for s in symbols:
        clean = s.strip().upper()
        base = clean.split(":")[0]
        v = verdicts.get(clean) or verdicts.get(base) or verdicts.get(s)

        adx_val = symbol_data.get(clean, {}).get("adx", 20.0)
        ema_b = symbol_data.get(clean, {}).get("ema_bias", "NEUTRAL")

        if not v or not isinstance(v, dict):
            # Квантовое правило: если ADX >= 20 -> Трендовый режим, иначе Флэт
            is_trend = adx_val >= 20.0
            v = {
                "action": "ALLOW_TRADE" if is_trend else "SKIP_FOR_DAY",
                "regime": "TRENDING" if is_trend else "CHOPPY_RANGE",
                "confidence": 7 if is_trend else 5,
                "bias": "LONG" if ema_b == "BULLISH" else ("SHORT" if ema_b == "BEARISH" else "NEUTRAL"),
                "reason": f"1H ADX {adx_val:.1f} >= 20.0 (направленный импульс)" if is_trend else f"1H ADX {adx_val:.1f} < 20.0 (боковой флэт / затухание)",
            }

        act = v.get("action", "ALLOW_TRADE")
        reason = v.get("reason", "")
        v["adx"] = adx_val
        final_results[clean] = v

        if act == "SKIP_FOR_DAY":
            skipped.append(clean)
            disabled_dict[base] = {
                "disabled_until": _get_tomorrow_midnight_utc(),
                "reason": f"AI Pre-trade: {reason}",
                "triggered_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            allowed.append(clean)
            if base in disabled_dict and "AI Pre-trade" in disabled_dict[base].get("reason", ""):
                del disabled_dict[base]

    # Сохраняем обновленные блокировки
    try:
        with open(DISABLED_PAIRS_FILE, "w", encoding="utf-8") as f:
            json.dump(disabled_dict, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Ошибка записи {DISABLED_PAIRS_FILE}: {e}")

    # Вывод брифинга в консоль
    print("\n" + "=" * 75)
    print(f"   🤖 ИИ-ОЦЕНКА ВСЕЛЕННОЙ ПЕРЕД СТАРТОМ ({market.upper()}) | Источник: {'Google Gemini ' + model_name if used_ai else 'Quant ADX Engine'}")
    print("=" * 75)
    for sym, res in final_results.items():
        mark = "🟢 ТОРГОВАТЬ" if res["action"] == "ALLOW_TRADE" else "⏸️ ПРОПУСК (ФЛЭТ)"
        print(f"   • {sym:<12} | {mark:<18} | ADX: {res.get('adx', 0):<4} | Оценка: {res.get('confidence', '-')}/10")
        print(f"     ↳ {res.get('reason', '')}")
    print("=" * 75)
    print(f"   ✅ Активно: {len(allowed)} | ⏸️ Отключено: {len(skipped)}\n")

    # Формирование и отправка Telegram-сообщения
    if send_tg:
        lines = [
            f"🧠 <b>ИИ-ОЦЕНКА РЫНКА ПЕРЕД СТАРТОМ ({market.upper()})</b>",
            f"<i>Анализатор: {'Google Gemini ' + model_name if used_ai else 'Квант-модуль ADX/EMA'}</i>",
            "━━━━━━━━━━━━━━━━━━━━━",
        ]
        for sym, res in final_results.items():
            base_s = sym.split(":")[0]
            if res["action"] == "ALLOW_TRADE":
                lines.append(f"🟢 <b>{base_s}</b>: ТОРГОВАТЬ (ADX {res.get('adx', 0):.1f}) | {res.get('confidence', 8)}/10")
            else:
                lines.append(f"⏸️ <b>{base_s}</b>: <s>ПРОПУСК (ФЛЭТ)</s> (ADX {res.get('adx', 0):.1f})")
            lines.append(f"   ↳ <i>{res.get('reason', '')}</i>\n")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"<b>Итог:</b> допущено <b>{len(allowed)}</b> из {len(symbols)} инструментов.")
        send_telegram_message("\n".join(lines))

    return {
        "results": final_results,
        "allowed_symbols": allowed,
        "skipped_symbols": skipped,
        "used_ai": used_ai,
    }


def fetch_live_price(symbol: str, exchange=None) -> float:
    """Безопасно получает текущую цену последней сделки с биржи."""
    clean_sym = symbol.strip().upper()
    swap_sym = clean_sym if ":" in clean_sym else f"{clean_sym}:USDT"

    # 1. Если передан exchange
    if exchange is not None:
        try:
            ticker = exchange.fetch_ticker(swap_sym)
            if ticker and ticker.get("last"):
                return float(ticker["last"])
        except Exception:
            pass

    # 2. Быстрый запрос через ccxt bitget с таймаутом 5 сек
    try:
        import ccxt
        ex = ccxt.bitget({
            "options": {"defaultType": "swap"},
            "timeout": 5000,
            "enableRateLimit": False,
        })
        ticker = ex.fetch_ticker(swap_sym)
        if ticker and ticker.get("last"):
            return float(ticker["last"])
    except Exception:
        pass

    return 0.0


def get_open_positions_detailed(exchange=None) -> list[dict]:
    """
    Загружает и рассчитывает детальные метрики по всем открытым позициям (Bitget + T-Bank).
    """
    open_positions = []

    # 1. Bitget
    bg_file = getattr(cfg, "TRADE_LOG_FILE", os.path.join(DATA_DIR, "live_trade_log.json"))
    if not os.path.exists(bg_file) and os.path.exists("live_trade_log.json"):
        bg_file = "live_trade_log.json"

    if os.path.exists(bg_file):
        try:
            with open(bg_file, "r", encoding="utf-8") as f:
                trades = json.load(f)
            for t in trades:
                if t.get("status") == "OPEN":
                    pos = dict(t)
                    pos["market"] = "crypto"
                    sym = pos.get("symbol", "")
                    entry = float(pos.get("entry_price", 0.0))
                    sl = float(pos.get("stop_loss", pos.get("current_stop", entry * 0.99)))
                    tp = float(pos.get("take_profit", pos.get("tp2_price", entry * 1.02)))
                    tp1 = float(pos.get("tp1_price", entry + (entry - sl)))
                    direction = pos.get("direction", "LONG").upper()
                    amount = float(pos.get("remaining_amount", pos.get("amount", 0.0)))

                    live_p = fetch_live_price(sym, exchange=exchange)
                    if live_p <= 0.0:
                        live_p = entry

                    risk_per_unit = abs(entry - sl)
                    if direction == "LONG":
                        diff = live_p - entry
                        fl_r = diff / risk_per_unit if risk_per_unit > 0 else 0.0
                        fl_pnl = diff * amount
                        dist_sl = ((live_p - sl) / live_p) * 100.0 if live_p > 0 else 0.0
                        dist_tp = ((tp - live_p) / live_p) * 100.0 if live_p > 0 else 0.0
                    else:
                        diff = entry - live_p
                        fl_r = diff / risk_per_unit if risk_per_unit > 0 else 0.0
                        fl_pnl = diff * amount
                        dist_sl = ((sl - live_p) / live_p) * 100.0 if live_p > 0 else 0.0
                        dist_tp = ((live_p - tp) / live_p) * 100.0 if live_p > 0 else 0.0

                    pos["current_price"] = live_p
                    pos["floating_r"] = round(fl_r, 2)
                    pos["floating_pnl"] = round(fl_pnl, 4)
                    pos["currency"] = "USDT"
                    pos["dist_sl_pct"] = round(dist_sl, 2)
                    pos["dist_tp_pct"] = round(dist_tp, 2)
                    open_positions.append(pos)
        except Exception as e:
            print(f"⚠️ Ошибка чтения позиций Bitget: {e}")

    # 2. T-Bank
    tb_file = getattr(cfg, "TBANK_ACTIVE_POSITIONS_FILE", os.path.join(DATA_DIR, "tbank_active_positions.json"))
    if not os.path.exists(tb_file) and os.path.exists("tbank_active_positions.json"):
        tb_file = "tbank_active_positions.json"

    if os.path.exists(tb_file):
        try:
            with open(tb_file, "r", encoding="utf-8") as f:
                tb_pos = json.load(f)
            for ticker, p in tb_pos.items():
                pos = dict(p)
                pos["symbol"] = ticker
                pos["market"] = "moex"
                direction = pos.get("dir", "LONG").upper()
                pos["direction"] = direction
                entry = float(pos.get("entry_price", 0.0))
                sl = float(pos.get("current_stop", pos.get("stop_loss", entry * 0.99)))
                tp = float(pos.get("tp2_price", pos.get("take_profit", entry * 1.02)))
                tp1 = float(pos.get("tp1_price", entry * 1.01))
                lots = float(pos.get("remaining_lots", pos.get("total_lots", 1)))

                live_p = entry
                risk_per_unit = abs(entry - sl)
                diff = live_p - entry if direction == "LONG" else entry - live_p
                fl_r = diff / risk_per_unit if risk_per_unit > 0 else 0.0
                fl_pnl = diff * lots

                pos["current_price"] = live_p
                pos["floating_r"] = round(fl_r, 2)
                pos["floating_pnl"] = round(fl_pnl, 2)
                pos["currency"] = "RUB"
                pos["dist_sl_pct"] = round(abs(live_p - sl) / live_p * 100.0, 2) if live_p > 0 else 0.0
                pos["dist_tp_pct"] = round(abs(tp - live_p) / live_p * 100.0, 2) if live_p > 0 else 0.0
                open_positions.append(pos)
        except Exception as e:
            print(f"⚠️ Ошибка чтения позиций Т-Банк: {e}")

    return open_positions


def get_active_basket_data(exchange=None) -> dict:
    """
    Собирает сводные квант-метрики по текущему набору активных инструментов (Crypto + MOEX).
    """
    univ_file = os.path.join(DATA_DIR, "active_universe.json")
    active_crypto = list(getattr(cfg, "ALTS_SYMBOLS", ["DOGE/USDT", "ADA/USDT", "BNB/USDT", "SOL/USDT"]))
    active_moex = list(getattr(cfg, "TBANK_TICKERS", ["SBER", "GAZP", "LKOH", "ROSN", "YDEX"]))

    if os.path.exists(univ_file):
        try:
            with open(univ_file, "r", encoding="utf-8") as f:
                u_data = json.load(f)
            if u_data.get("crypto"):
                active_crypto = u_data["crypto"]
            if u_data.get("moex"):
                active_moex = u_data["moex"]
        except Exception:
            pass

    basket_data = {}

    # Анализ крипто-инструментов
    for sym in active_crypto:
        clean = sym.strip().upper()
        base = clean.split(":")[0]
        df_1h = fetch_1h_for_symbol(clean, exchange=exchange, market="crypto")

        cur_p = 0.0
        adx_val = 15.0
        ema_b = "NEUTRAL"
        rng24 = 2.0

        if df_1h is not None and len(df_1h) >= 15:
            adx_s = compute_adx(df_1h, period=14)
            adx_val = round(float(adx_s.iloc[-1]), 1)
            close = df_1h["close"]
            ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
            ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
            cur_p = float(close.iloc[-1])
            tail24 = df_1h.tail(24)
            rng24 = round(((tail24["high"].max() - tail24["low"].min()) / cur_p) * 100.0, 1) if cur_p > 0 else 2.0
            ema_b = "BULLISH" if ema20 >= ema50 else "BEARISH"

        is_dis, dis_reason = is_pair_disabled_today(base)
        basket_data[clean] = {
            "market": "crypto",
            "price": cur_p,
            "adx": adx_val,
            "ema_bias": ema_b,
            "range_24h": rng24,
            "is_disabled": is_dis,
            "disable_reason": dis_reason,
        }

    # Анализ MOEX инструментов
    for ticker in active_moex:
        clean_t = ticker.strip().upper()
        df_1h = fetch_1h_for_symbol(clean_t, exchange=None, market="moex")

        cur_p = 0.0
        adx_val = 18.0
        ema_b = "NEUTRAL"
        rng24 = 1.5

        if df_1h is not None and len(df_1h) >= 15:
            adx_s = compute_adx(df_1h, period=14)
            adx_val = round(float(adx_s.iloc[-1]), 1)
            close = df_1h["close"]
            ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
            ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
            cur_p = float(close.iloc[-1])
            tail24 = df_1h.tail(24)
            rng24 = round(((tail24["high"].max() - tail24["low"].min()) / cur_p) * 100.0, 1) if cur_p > 0 else 1.5
            ema_b = "BULLISH" if ema20 >= ema50 else "BEARISH"

        is_dis, dis_reason = is_pair_disabled_today(clean_t)
        basket_data[clean_t] = {
            "market": "moex",
            "price": cur_p,
            "adx": adx_val,
            "ema_bias": ema_b,
            "range_24h": rng24,
            "is_disabled": is_dis,
            "disable_reason": dis_reason,
        }

    return basket_data


def format_ai_portfolio_audit_report(open_positions: list, basket_data: dict, ai_data: dict,
                                     used_ai: bool = True, model_name: str = "gemini-3.6-flash") -> str:
    """Форматирует сводный отчет ИИ-аудита позиций и корзины с Telegram HTML-разметкой."""
    engine_title = f"Google Gemini {model_name}" if used_ai else "Квант-ядро ADX/Risk Officer"
    lines = [
        "🔮 <b>ИИ-АУДИТ ПОРТФЕЛЯ И КОРЗИНЫ АКТИВОВ</b>",
        f"<i>Анализатор: {engine_title}</i>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Раздел 1: Открытые позиции
    pos_audits = {p.get("symbol"): p for p in ai_data.get("positions_audit", [])}
    lines.append(f"💼 <b>ОТКРЫТЫЕ ПОЗИЦИИ ({len(open_positions)}):</b>")

    if not open_positions:
        lines.append("  • <i>Активных открытых позиций нет. Капитал на 100% свободен. Бот ожидает валидный FVG-сетап.</i>\n")
    else:
        for pos in open_positions:
            sym = pos.get("symbol", "N/A")
            direction = pos.get("direction", "LONG")
            entry = pos.get("entry_price", 0.0)
            cur_p = pos.get("current_price", entry)
            fl_r = pos.get("floating_r", 0.0)
            fl_pnl = pos.get("floating_pnl", 0.0)
            curr = pos.get("currency", "USDT")
            sl = pos.get("stop_loss", pos.get("current_stop", 0.0))
            tp = pos.get("take_profit", pos.get("tp2_price", 0.0))
            tp1 = pos.get("tp1_price", 0.0)

            # Бейдж статуса
            if fl_r >= 0.5:
                res_badge = f"<b>{fl_r:+.2f}R</b> ({fl_pnl:+.3f} {curr}) 🟢"
            elif fl_r <= -0.5:
                res_badge = f"<b>{fl_r:+.2f}R</b> ({fl_pnl:+.3f} {curr}) 🔴"
            else:
                res_badge = f"<b>{fl_r:+.2f}R</b> ({fl_pnl:+.3f} {curr}) 🟡"

            audit_item = pos_audits.get(sym, {})
            rec = audit_item.get("recommendation", "HOLD")
            rationale = audit_item.get("rationale", "")
            action = audit_item.get("suggested_action", "")

            rec_emoji = {
                "HOLD": "🛡️ <b>УДЕРЖИВАТЬ (HOLD)</b>",
                "TIGHTEN_STOP": "⚡ <b>ПОДТЯНУТЬ СТОП (TIGHTEN)</b>",
                "TAKE_EARLY_PROFIT": "🎯 <b>ФИКСИРОВАТЬ ПРИБЫЛЬ</b>",
                "WATCH": "👀 <b>ПОВЫШЕННОЕ ВНИМАНИЕ</b>",
            }.get(rec, f"<b>{rec}</b>")

            lines.append(f"• <b>{sym}</b> (<code>{direction}</code>)")
            lines.append(f"  💵 Вход: <code>{entry:,.4f}</code> | Текущая: <code>{cur_p:,.4f}</code>")
            lines.append(f"  📈 Плавающий результат: {res_badge}")
            lines.append(f"  🛑 SL: <code>{sl:,.4f}</code> | 🎯 TP: <code>{tp:,.4f}</code> (TP1: <code>{tp1:,.4f}</code>)")
            lines.append(f"  🤖 <b>Решение ИИ:</b> {rec_emoji}")
            if action:
                lines.append(f"  🎯 <b>Действие:</b> <i>{html.escape(action)}</i>")
            if rationale:
                lines.append(f"  💡 <i>{html.escape(rationale)}</i>")
            lines.append("")

    # Раздел 2: Здоровье активной корзины
    lines.append("🪙 <b>ЗДОРОВЬЕ АКТИВНОЙ КОРЗИНЫ:</b>")
    b_health = ai_data.get("basket_health", {})

    for sym, d in basket_data.items():
        base_s = sym.split(":")[0]
        h_item = b_health.get(sym) or b_health.get(base_s) or {}
        score = h_item.get("quality_score", 7)
        advice = h_item.get("advice", "KEEP")
        comment = h_item.get("comment", "")
        adx = d.get("adx", 0.0)
        ema_b = d.get("ema_bias", "NEUTRAL")
        is_dis = d.get("is_disabled", False)

        if advice == "KEEP" and not is_dis:
            adv_badge = "🟢 <b>АКТИВЕН (В ТОПЕ)</b>"
        elif is_dis:
            adv_badge = "⏸️ <s>БЛОКИРОВКА (CIRCUIT)</s>"
        else:
            adv_badge = "🟡 <b>НА ПАУЗЕ (ФЛЭТ)</b>"

        lines.append(f"• <b>{base_s}</b>: {adv_badge} | Оценка: <b>{score}/10</b>")
        dis_info = f" | <i>Блок до 00:00 UTC</i>" if is_dis else ""
        lines.append(f"  ↳ 1H ADX: <code>{adx:.1f}</code> | Тренд: <code>{ema_b}</code>{dis_info}")
        if comment:
            lines.append(f"  ↳ <i>{html.escape(comment)}</i>")
    lines.append("")

    # Раздел 3: Вердикт Риск-Офицера
    lines.append("🛡️ <b>ВЕРДИКТ РИСК-ОФИЦЕРА:</b>")
    ro = ai_data.get("risk_officer_verdict", {})
    stance = ro.get("market_stance", "DEFENSIVE")
    stance_badge = {
        "DEFENSIVE": "🛡️ <b>ЗАЩИТНЫЙ (DEFENSIVE)</b>",
        "NEUTRAL": "⚖️ <b>НЕЙТРАЛЬНЫЙ (BALANCED)</b>",
        "AGGRESSIVE": "🚀 <b>АКТИВНЫЙ ТРЕНД (OPPORTUNISTIC)</b>",
    }.get(stance, f"<b>{stance}</b>")

    lines.append(f"• <b>Режим рынка:</b> {stance_badge}")
    if ro.get("macro_summary"):
        lines.append(f"• <b>Анализ фона:</b> <i>{html.escape(ro['macro_summary'])}</i>")
    if ro.get("portfolio_advice"):
        lines.append(f"• <b>Директива:</b> <b>{html.escape(ro['portfolio_advice'])}</b>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def evaluate_portfolio_and_positions_ai(exchange=None, send_tg: bool = False) -> tuple[str, dict]:
    """
    Комплексный ИИ-аудит текущих открытых позиций и активного набора инструментов через Gemini:
    1. Проверяет активные сделки (Bitget + MOEX): live-цена, плавающий R, PnL, запас хода.
    2. Оценивает здоровье корзины: 1H ADX, 1H EMA, Circuit Breaker блокировки.
    3. Генерирует рекомендации институционального риск-офицера.
    4. Формирует профессиональный отчет для Telegram и возвращает (text, ai_data).
    """
    open_positions = get_open_positions_detailed(exchange=exchange)
    basket_data = get_active_basket_data(exchange=exchange)

    api_key = getattr(cfg, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    model_name = getattr(cfg, "GEMINI_MODEL", "gemini-3.5-flash")

    pos_summary = []
    for p in open_positions:
        pos_summary.append({
            "symbol": p.get("symbol"),
            "direction": p.get("direction"),
            "entry_price": p.get("entry_price"),
            "current_price": p.get("current_price"),
            "stop_loss": p.get("stop_loss", p.get("current_stop")),
            "take_profit": p.get("take_profit"),
            "tp1_price": p.get("tp1_price"),
            "floating_r": p.get("floating_r"),
            "floating_pnl": f"{p.get('floating_pnl')} {p.get('currency')}",
            "dist_sl_pct": p.get("dist_sl_pct"),
            "dist_tp_pct": p.get("dist_tp_pct"),
            "setup_notes": p.get("setup_reason", {}).get("bias_desc", "ICT Setup") if isinstance(p.get("setup_reason"), dict) else "ICT Setup",
        })

    basket_summary = {}
    for sym, d in basket_data.items():
        basket_summary[sym] = {
            "market": d["market"],
            "price": d["price"],
            "1h_adx": d["adx"],
            "1h_trend": d["ema_bias"],
            "24h_range_pct": d["range_24h"],
            "circuit_breaker_disabled": d["is_disabled"],
            "status_note": d["disable_reason"] if d["is_disabled"] else "Active",
        }

    ai_data = None
    used_ai = False

    if api_key:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            prompt = f"""
Ты — институциональный квант-аналитик и директор по управлению рисками (Chief Risk Officer) хедж-фонда.
Проведи детальный аудит текущих ОТКРЫТЫХ ПОЗИЦИЙ и АКТИВНОГО НАБОРА ИНСТРУМЕНТОВ торгового робота ICT Toolkit.

1. ОТКРЫТЫЕ ПОЗИЦИИ В РАБОТЕ:
{json.dumps(pos_summary, indent=2, ensure_ascii=False) if pos_summary else "Открытых позиций сейчас нет (100% капитала свободно)."}

2. АКТИВНАЯ КОРЗИНА ИНСТРУМЕНТОВ:
{json.dumps(basket_summary, indent=2, ensure_ascii=False)}

Критерии анализа:
- Для открытых позиций (если есть):
  Оцени импульс и динамику. Вердикт "recommendation":
  • "HOLD" (удерживать позицию до целей по правилам стратегии)
  • "TIGHTEN_STOP" (подтянуть стоп в безубыток или под локальный экстремум)
  • "TAKE_EARLY_PROFIT" (зафиксировать прибыль досрочно из-за слабости рынка)
  • "WATCH" (внимательно наблюдать, повышенная опасность)
- Для корзины инструментов:
  Оцени пригодность каждого к импульсной торговле пробоев FVG (ADX >= 20, наличие волатильности). Дай оценку quality_score от 1 до 10 и рекомендацию:
  • "KEEP" (оставить в активной торговле)
  • "BENCH" (отправить в запас / на паузу из-за боковика или блокировки)
  • "REPLACE" (рекомендовать заменить через скринер)
- Резюме риск-офицера:
  Режим рынка ("DEFENSIVE", "NEUTRAL", "AGGRESSIVE"), краткая сводка в 1-2 предложениях и главная рекомендация трейдеру.

Верни СТРОГО валидный JSON следующей структуры:
{{
  "positions_audit": [
    {{
      "symbol": "ADA/USDT:USDT",
      "health": "HEALTHY" или "AT_RISK" или "STALLED",
      "recommendation": "HOLD" или "TIGHTEN_STOP" или "TAKE_EARLY_PROFIT" или "WATCH",
      "rationale": "Краткое обоснование в 1-2 предложениях",
      "suggested_action": "Конкретное действие трейдера или бота"
    }}
  ],
  "basket_health": {{
    "<SYMBOL>": {{
      "status": "STRONG" или "CHOPPY" или "NEUTRAL",
      "bias": "BULLISH" или "BEARISH" или "FLAT",
      "quality_score": число от 1 до 10,
      "advice": "KEEP" или "BENCH" или "REPLACE",
      "comment": "Краткий комментарий в 1 предложении"
    }}
  }},
  "risk_officer_verdict": {{
    "market_stance": "DEFENSIVE" или "NEUTRAL" или "AGGRESSIVE",
    "macro_summary": "Сводка макро-состояния рынка в 1-2 предложениях",
    "portfolio_advice": "Главный совет трейдеру на ближайшие часы"
  }}
}}
"""
            models_to_try = ["gemini-3.5-flash", "gemini-flash-latest"]
            for m_name in models_to_try:
                try:
                    resp = client.models.generate_content(
                        model=m_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.2,
                        ),
                    )
                    ai_data = json.loads(resp.text)
                    used_ai = True
                    model_name = m_name
                    break
                except Exception as model_err:
                    print(f"⚠️ Ошибка Gemini ({m_name}): {model_err}")
        except Exception as e:
            print(f"⚠️ Ошибка вызова клиента Gemini: {e}")

    # Квант-фоллбэк при отсутствии ключа или ошибке
    if not ai_data or not isinstance(ai_data, dict):
        pos_audit = []
        for p in open_positions:
            fl_r = p.get("floating_r", 0.0)
            if fl_r >= 1.0:
                rec = "TIGHTEN_STOP"
                hlth = "HEALTHY"
                act = "Достигнут 1.0R. Перевести стоп в безубыток и зафиксировать 50% прибыли."
                rat = f"Плавающая прибыль достигла {fl_r:+.2f}R. Прибыль защищена институциональным правилом Grid B."
            elif fl_r >= 0.3:
                rec = "HOLD"
                hlth = "HEALTHY"
                act = "Удерживать позицию до первой цели TP1."
                rat = f"Позиция развивается в плюс ({fl_r:+.2f}R), структура движения сохраняется."
            elif fl_r <= -0.6:
                rec = "WATCH"
                hlth = "AT_RISK"
                act = "Контролировать уровень стоп-лосса, не усреднять."
                rat = f"Просадка составляет {fl_r:+.2f}R, цена приблизилась к защитному стопу."
            else:
                rec = "HOLD"
                hlth = "NEUTRAL"
                act = "Удерживать сделку согласно первоначальному плану."
                rat = f"Цена вблизи точки входа ({fl_r:+.2f}R), импульс формируется."

            pos_audit.append({
                "symbol": p.get("symbol"),
                "health": hlth,
                "recommendation": rec,
                "rationale": rat,
                "suggested_action": act,
            })

        basket_health = {}
        for sym, d in basket_data.items():
            adx = d.get("adx", 15.0)
            is_dis = d.get("is_disabled", False)
            if is_dis:
                st = "CHOPPY"
                score = 3
                adv = "BENCH"
                com = f"Отключен защитой Circuit Breaker: {d.get('disable_reason', 'флэт')}"
            elif adx >= 20.0:
                st = "STRONG"
                score = 8
                adv = "KEEP"
                com = f"Сильный импульсный тренд (ADX {adx:.1f} >= 20). Приоритет для сделок."
            else:
                st = "CHOPPY"
                score = 4
                adv = "BENCH"
                com = f"Боковой флэт (ADX {adx:.1f} < 20). Высокий риск ложных пробоев."

            basket_health[sym] = {
                "status": st,
                "bias": d.get("ema_bias", "NEUTRAL"),
                "quality_score": score,
                "advice": adv,
                "comment": com,
            }

        ai_data = {
            "positions_audit": pos_audit,
            "basket_health": basket_health,
            "risk_officer_verdict": {
                "market_stance": "DEFENSIVE" if any(d.get("is_disabled") for d in basket_data.values()) else "NEUTRAL",
                "macro_summary": "Квантовый риск-менеджер активен. Контроль просадки и волатильности включен.",
                "portfolio_advice": "Соблюдать риск 1.0% на сделку, торговать только инструменты с ADX >= 20.",
            },
        }

    report_text = format_ai_portfolio_audit_report(open_positions, basket_data, ai_data, used_ai=used_ai, model_name=model_name)

    if send_tg:
        send_telegram_message(report_text)

    return report_text, ai_data


