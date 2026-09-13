"""
Live/demo-исполнение стратегии на Bitget через ccxt.

Поддерживает:
- Институциональный риск-менеджмент: 1% от баланса на сделку (или агрессивный до 5%).
- Учет комиссий биржи (Taker 0.06% на открытие и закрытие) и расчет чистого PnL / Net R.
- Автоматический сбор и вывод статистики реальных сделок (--stats).
- Демо-трейдинг (PAPTRADING) без риска денег или торговлю на реальном счёте (--real).
- Мониторинг одной пары (--symbol ETH/USDT:USDT) или всех 5 пар корзины (--all).
- Институциональный фильтр Азиатской сессии (--asian-range) с винрейтом 62.6% и PF 2.34.
- Ограничение торговыми Киллзонами (--killzones: London 07-10 UTC, NY 12-15 UTC).
- ИИ-оценку сетапов перед входом через Gemini (--ai).

Запуск (Демо-режим, риск 1% от баланса, корзина 5 пар, Asian Range):
  python live_trade.py --all --asian-range --risk 1.0

Агрессивный режим (риск 3% - 5% от баланса):
  python live_trade.py --all --asian-range --risk 3.0

Посмотреть собранную статистику и комиссии:
  python live_trade.py --stats
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import os
import time
import json
import argparse
from datetime import datetime, timezone
import ccxt
import pandas as pd
from smartmoneyconcepts import smc

import config as cfg
from strategy import resample, compute_bias_series, bias_at, in_killzone, get_next_killzone_delta
from ict_advanced import compute_asian_ranges, is_asian_range_sweep
from ai_evaluator import evaluate_setup
import telegram_notifier as tg
import chart_generator as cg
import pending_signals as ps

DATA_DIR = getattr(cfg, "DATA_DIR", "data")
SEEN_SIGNALS_PATH = getattr(cfg, "SEEN_SIGNALS_FILE", os.path.join(DATA_DIR, "live_seen_signals.json"))
TRADE_LOG_PATH = getattr(cfg, "TRADE_LOG_FILE", os.path.join(DATA_DIR, "live_trade_log.json"))
ROOT_SEEN_SIGNALS_PATH = "live_seen_signals.json"
ROOT_TRADE_LOG_PATH = "live_trade_log.json"
LOOKBACK_BARS_1M = 1200  # ~20 часов минутных свечей


def load_seen():
    path = SEEN_SIGNALS_PATH
    if not os.path.exists(path) and os.path.exists(ROOT_SEEN_SIGNALS_PATH):
        path = ROOT_SEEN_SIGNALS_PATH
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = set(json.load(f))
            if path != SEEN_SIGNALS_PATH and not os.path.exists(SEEN_SIGNALS_PATH):
                save_seen(data)
            return data
        except Exception:
            return set()
    return set()


def save_seen(seen):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(SEEN_SIGNALS_PATH)), exist_ok=True)
        with open(SEEN_SIGNALS_PATH, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, indent=2)
    except Exception as e:
        print(f"Предупреждение: не удалось сохранить seen signals: {e}")


def load_trades():
    path = TRADE_LOG_PATH
    if not os.path.exists(path) and os.path.exists(ROOT_TRADE_LOG_PATH):
        path = ROOT_TRADE_LOG_PATH
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if path != TRADE_LOG_PATH and not os.path.exists(TRADE_LOG_PATH):
                save_trades(data)
            return data
        except Exception:
            return []
    return []


def save_trades(trades):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(TRADE_LOG_PATH)), exist_ok=True)
        with open(TRADE_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(trades, f, indent=2, default=str)
    except Exception as e:
        print(f"Предупреждение: не удалось сохранить trade log: {e}")


def get_account_balance(exchange):
    """
    Запрашивает актуальный баланс счета с биржи (USDT equity и free margin).
    """
    try:
        balance = exchange.fetch_balance()
        # Разные структуры ответа у ccxt для Bitget swap/futures
        usdt_info = balance.get("USDT", {})
        free = float(usdt_info.get("free", balance.get("free", {}).get("USDT", 0.0) or 0.0))
        total = float(usdt_info.get("total", balance.get("total", {}).get("USDT", 0.0) or 0.0))
        
        # Если баланс нулевой (например, тестовый API ключ без средств), используем запасное значение
        if total <= 0:
            total = float(cfg.POSITION_SIZE_USDT * 10)  # виртуальный банк
            free = total
        return {"equity": total, "free": free}
    except Exception as e:
        print(f"⚠️ Не удалось запросить баланс: {e}. Используем fallback.")
        return {"equity": 1000.0, "free": 1000.0}


def calculate_position_size(exchange, swap_symbol, entry_price, stop_price, risk_pct, leverage):
    """
    Расчет размера позиции по институциональным стандартам (Prop-Firm Grade):
    При срабатывании стоп-лосса теряется РОВНО заданный процент (risk_pct) от текущего баланса.
    
    Формула:
      Risk_USD = Equity * (Risk_Pct / 100)
      Stop_Distance_Pct = |Entry - Stop| / Entry
      Position_Size_USD = Risk_USD / Stop_Distance_Pct
    """
    bal = get_account_balance(exchange)
    equity = bal["equity"]
    free_margin = bal["free"]

    # Ограничение риска
    if risk_pct > cfg.MAX_RISK_PER_TRADE_PCT:
        print(f"⚠️ Запрошенный риск {risk_pct:.1f}% превышает лимит {cfg.MAX_RISK_PER_TRADE_PCT}%. Ограничен до {cfg.MAX_RISK_PER_TRADE_PCT}%.")
        risk_pct = cfg.MAX_RISK_PER_TRADE_PCT

    risk_usd = equity * (risk_pct / 100.0)
    stop_dist = abs(entry_price - stop_price)
    stop_dist_pct = stop_dist / entry_price

    # Учет комиссий биржи в бюджете риска (Net Risk Sizing):
    # При срабатывании стопа сумма (убыток по цене + комиссии) строго равна risk_usd (1.0% депозита)
    fee_roundtrip_pct = cfg.BITGET_TAKER_FEE_PCT * 2  # 0.06% open + 0.06% close = 0.12%
    effective_risk_pct = stop_dist_pct + fee_roundtrip_pct

    if effective_risk_pct <= 0:
        target_pos_usd = cfg.POSITION_SIZE_USDT
    else:
        target_pos_usd = risk_usd / effective_risk_pct

    # Ограничение позиции максимальным плечом и свободной маржой
    max_by_leverage = equity * leverage * 0.90
    max_by_margin = free_margin * leverage * 0.85
    final_pos_usd = min(target_pos_usd, max_by_leverage, max_by_margin)

    if final_pos_usd < target_pos_usd * 0.95:
        print(f"ℹ️ Размер позиции скорректирован лимитом маржи: {final_pos_usd:.1f} USDT (вместо расчетных {target_pos_usd:.1f} USDT).")

    # Проверка минимальных лимитов биржи (минимум 5 USDT на Bitget)
    market = exchange.market(swap_symbol) if exchange.markets else {}
    min_cost = 5.0
    if market and "limits" in market and "cost" in market["limits"]:
        min_cost = float(market["limits"]["cost"].get("min") or 5.0)

    if final_pos_usd < min_cost:
        final_pos_usd = min_cost

    raw_amount = final_pos_usd / entry_price
    amount = float(exchange.amount_to_precision(swap_symbol, raw_amount))

    min_amount = 0.0
    if market and "limits" in market and "amount" in market["limits"]:
        min_amount = float(market["limits"]["amount"].get("min") or 0.0)
    if amount < min_amount:
        amount = min_amount

    final_pos_usd = amount * entry_price

    # Расчет комиссии за открытие (Taker 0.06%)
    est_open_fee = final_pos_usd * cfg.BITGET_TAKER_FEE_PCT

    # Фактический риск: если позиция ограничена плечом/маржей, реальный риск равен:
    actual_risk_usd = final_pos_usd * effective_risk_pct

    return {
        "amount": amount,
        "position_usdt": final_pos_usd,
        "risk_usd": actual_risk_usd,
        "planned_risk_usd": risk_usd,
        "risk_pct": risk_pct,
        "stop_dist_pct": stop_dist_pct,
        "equity_at_entry": equity,
        "est_open_fee": est_open_fee,
    }


def init_exchange(demo_mode=True, leverage=None, margin_mode=None, skip_confirm=False, target_symbols=None):
    api_key = cfg.BITGET_API_KEY
    api_secret = cfg.BITGET_API_SECRET
    api_pass = cfg.BITGET_API_PASSWORD

    if not (api_key and api_secret and api_pass):
        print("\n❌ ОШИБКА: Не заданы API-ключи Bitget!")
        print("Открой файл .env и заполни параметры:")
        print("  BITGET_API_KEY=твой_ключ")
        print("  BITGET_API_SECRET=твой_секрет")
        print("  BITGET_API_PASSWORD=твой_пароль_passphrase")
        print("\nИнструкция:")
        print("  1. Зайди на bitget.com -> Профиль -> Управление API -> Создать API-ключ")
        print("  2. Выбери тип 'Торговля по API' (API trading)")
        print("  3. Обязательно отметь галочку 'Фьючерсы' (USDT-M Futures: чтение и торговля)")
        print("  4. Задай Passphrase (парольную фразу) и сохрани ее в BITGET_API_PASSWORD\n")
        raise RuntimeError("API-ключи Bitget не настроены.")

    exchange = ccxt.bitget({
        "apiKey": api_key,
        "secret": api_secret,
        "password": api_pass,
        "options": {"defaultType": "swap"},
    })

    if demo_mode:
        exchange.enable_demo_trading(True)
        print("🟢 РЕЖИМ: Демо-трейдинг Bitget (PAPTRADING) - реальные деньги НЕ используются")
    else:
        print("🔴 ВНИМАНИЕ: РЕЖИМ РЕАЛЬНОЙ ТОРГОВЛИ (DEMO_MODE=False)!")
        print("Вы собираетесь размещать ордера на реальном депозите.")
        if not skip_confirm:
            confirm = input("Введи 'ДА' заглавными буквами для подтверждения запуска: ")
            if confirm != "ДА":
                raise SystemExit("Запуск отменен пользователем.")
        else:
            print("✅ Запуск на реальном счете подтвержден флагом --yes.")

    exchange.load_markets()

    # Проверка баланса
    bal = get_account_balance(exchange)
    print(f"💰 Баланс аккаунта: {bal['free']:.2f} USDT свободно (Equity: {bal['equity']:.2f} USDT)")

    # Выставляем режим маржи (isolated / cross), плечо и включаем двусторонний режим (Hedge Mode)
    lev = leverage or cfg.LEVERAGE
    m_mode = (margin_mode or getattr(cfg, "MARGIN_MODE", "isolated")).lower()
    if m_mode == "cross":
        m_mode = "crossed"

    symbols_to_init = target_symbols if target_symbols else list(cfg.SWAP_SYMBOLS.values())
    for sym in symbols_to_init:
        # 1. Установка маржинального режима
        try:
            exchange.set_margin_mode(m_mode, sym)
        except Exception:
            pass

        # 2. Установка плеча (в Isolated на Bitget требуется holdSide='long'/'short')
        try:
            if m_mode == "isolated":
                exchange.set_leverage(lev, sym, params={"holdSide": "long"})
                exchange.set_leverage(lev, sym, params={"holdSide": "short"})
            else:
                exchange.set_leverage(lev, sym)
        except Exception:
            try:
                exchange.set_leverage(lev, sym)
            except Exception:
                pass

        # 3. Включение Hedge Mode
        try:
            exchange.set_position_mode(hedged=True, symbol=sym)
        except Exception:
            pass

    return exchange


def fetch_recent(exchange, swap_symbol, timeframe="1m", limit=1000):
    try:
        ohlcv = exchange.fetch_ohlcv(swap_symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms")
        return df.set_index("dt")[["open", "high", "low", "close", "volume"]].sort_index()
    except Exception as e:
        print(f"Ошибка загрузки OHLCV {timeframe} для {swap_symbol}: {e}")
        return None


def place_bracket_order(exchange, swap_symbol, direction, entry_price, stop, target, pos_calc, setup_reason: dict = None, tp1: float = None):
    side = "buy" if direction == 1 else "sell"
    amount = pos_calc["amount"]

    # Расчет этапов Grid B: TP1 на 1.0R (50% + BE), TP2 на 1.618R (Golden Fib 50%)
    risk_unit = abs(entry_price - stop)
    if tp1 is None:
        tp1 = (entry_price + 1.0 * risk_unit) if direction == 1 else (entry_price - 1.0 * risk_unit)
    tp2 = target

    # Уровень безубытка с запасом на комиссию брокера (Open + Close taker fee)
    fee_buf = entry_price * (cfg.BITGET_TAKER_FEE_PCT * 2.2)
    be_stop = (entry_price + fee_buf) if direction == 1 else (entry_price - fee_buf)

    params = {
        "hedged": True,  # Обязательно для Bitget USDT-M (Hedge Mode)
        "stopLoss": {"triggerPrice": float(exchange.price_to_precision(swap_symbol, stop))},
        "takeProfit": {"triggerPrice": float(exchange.price_to_precision(swap_symbol, tp2))},
    }
    order = exchange.create_order(swap_symbol, "market", side, amount, params=params)

    trade_entry = {
        "id": f"{swap_symbol.replace('/', '_').replace(':', '_')}_{int(time.time())}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": swap_symbol,
        "direction": "LONG" if direction == 1 else "SHORT",
        "entry_price": entry_price,
        "stop_loss": stop,
        "current_stop": stop,
        "take_profit": tp2,
        "tp1_price": tp1,
        "tp2_price": tp2,
        "be_price": be_stop,
        "partial_taken": False,
        "is_be_active": False,
        "amount": amount,
        "initial_amount": amount,
        "remaining_amount": amount,
        "position_usdt": pos_calc["position_usdt"],
        "risk_usd": pos_calc["risk_usd"],
        "risk_pct": pos_calc["risk_pct"],
        "order_id": order.get("id"),
        "status": "OPEN",
        "entry_fee": pos_calc["est_open_fee"],
        "exit_fee": 0.0,
        "total_fee": pos_calc["est_open_fee"],
        "banked_pnl": 0.0,
        "banked_r": 0.0,
        "exit_price": None,
        "exit_time": None,
        "exit_reason": None,
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "net_r": 0.0,
        "setup_reason": setup_reason,
    }

    trades = load_trades()
    trades.append(trade_entry)
    save_trades(trades)

    print(f"\n🚀 ОРДЕР РАЗМЕЩЕН: {swap_symbol} | {side.upper()} {amount} @ ~{entry_price:.4f}")
    print(f"   📊 Позиция: {pos_calc['position_usdt']:.2f} USDT (Риск: {pos_calc['risk_pct']:.1f}% = ${pos_calc['risk_usd']:.2f})")
    print(f"   🛑 Stop-Loss:       {stop:.4f} (-{pos_calc['stop_dist_pct']*100:.2f}%)")
    print(f"   🎯 TP1 (50% + BE):  {tp1:.4f} (+{pos_calc['stop_dist_pct']*1.0*100:.2f}%, 1.0R)")
    print(f"   🎯 TP2 (Golden Fib):{tp2:.4f} (+{pos_calc['stop_dist_pct']*1.618*100:.2f}%, 1.618R)")
    print(f"   🛡️ BE Stop (+fees): {be_stop:.4f}")
    print(f"   💳 Комиссия входа:  ~${pos_calc['est_open_fee']:.3f} | ID: {order.get('id')}\n")

    tg.notify_trade_opened(
        market="Bitget Crypto",
        symbol=swap_symbol,
        direction="LONG" if direction == 1 else "SHORT",
        entry_price=entry_price,
        stop_loss=stop,
        take_profit=tp2,
        amount_str=f"{amount} (~{pos_calc['position_usdt']:.2f} USDT)",
        risk_str=f"{pos_calc['risk_pct']:.1f}% (~${pos_calc['risk_usd']:.2f} USDT)",
        setup_reason=setup_reason,
    )

    return order


def reconcile_open_trades(exchange):
    """
    Институциональное сопровождение открытых позиций по сетке Grid B:
    1. При достижении 1.0R фиксирует 50% объема и переносит стоп в безубыток (True BE).
    2. При достижении 1.618R (Golden Fib) фиксирует оставшиеся 50% объема.
    3. При развороте после 1.0R закрывает остаток по безубытку (сохраняя прибыль первой цели).
    4. При срабатывании начального SL закрывает 100% позиции.
    """
    trades = load_trades()
    updated = False

    for t in trades:
        if t.get("status") != "OPEN":
            continue

        sym = t["symbol"]
        entry_p = t["entry_price"]
        sl = t.get("current_stop", t["stop_loss"])
        tp1 = t.get("tp1_price", t.get("take_profit"))
        tp2 = t.get("tp2_price", t.get("take_profit"))
        amt = t.get("remaining_amount", t["amount"])
        init_amt = t.get("initial_amount", t["amount"])
        direction = 1 if t["direction"] == "LONG" else -1
        risk_usd = max(t.get("risk_usd", 10.0), 0.01)

        # Проверяем текущую рыночную цену
        try:
            ticker = exchange.fetch_ticker(sym)
            current_p = ticker["last"]
        except Exception:
            continue

        # Проверяем фактическую позицию на бирже (синхронизация со сработавшими биржевыми SL/TP)
        is_pos_closed_on_exchange = False
        try:
            positions = exchange.fetch_positions([sym])
            active_p = [p for p in positions if float(p.get("contracts") or 0) > 0]
            if not active_p:
                is_pos_closed_on_exchange = True
        except Exception:
            pass

        if is_pos_closed_on_exchange:
            # Получаем фактическую сделку закрытия с биржи (цена, объем, комиссия)
            real_exit_price = None
            real_exit_fee = None
            try:
                my_trades = exchange.fetch_my_trades(sym, limit=5)
                if my_trades:
                    last_trade = my_trades[-1]
                    real_exit_price = float(last_trade.get("price") or 0.0)
                    if last_trade.get("fee") and isinstance(last_trade["fee"], dict):
                        real_exit_fee = float(last_trade["fee"].get("cost") or 0.0)
            except Exception:
                pass

            exit_price = real_exit_price if (real_exit_price and real_exit_price > 0) else current_p
            exit_fee = real_exit_fee if real_exit_fee is not None else ((amt * exit_price) * cfg.BITGET_TAKER_FEE_PCT)
            total_fee = t.get("entry_fee", 0.0) + exit_fee
            gross_pnl = (exit_price - entry_p) * amt * direction
            net_pnl = gross_pnl - total_fee
            net_r = net_pnl / risk_usd

            # Определяем причину закрытия по фактической цене выхода
            if direction == 1:
                if exit_price >= tp2 * 0.999:
                    exit_reason = "TAKE_PROFIT"
                elif exit_price <= sl * 1.001:
                    exit_reason = "STOP_LOSS"
                elif gross_pnl > 0:
                    exit_reason = "MANUAL_PROFIT"
                else:
                    exit_reason = "MANUAL_CLOSE"
            else:
                if exit_price <= tp2 * 1.001:
                    exit_reason = "TAKE_PROFIT"
                elif exit_price >= sl * 0.999:
                    exit_reason = "STOP_LOSS"
                elif gross_pnl > 0:
                    exit_reason = "MANUAL_PROFIT"
                else:
                    exit_reason = "MANUAL_CLOSE"

            t["status"] = "CLOSED"
            t["exit_price"] = exit_price
            t["exit_time"] = datetime.now(timezone.utc).isoformat()
            t["exit_reason"] = exit_reason
            t["exit_fee"] = exit_fee
            t["total_fee"] = total_fee
            t["gross_pnl"] = gross_pnl
            t["net_pnl"] = net_pnl
            t["net_r"] = net_r
            updated = True

            print(f"\n🔔 БИРЖЕВОЙ ОРДЕР ЗАКРЫТ: {sym} ({t['direction']}) -> {exit_reason}")
            print(f"   Цена выхода: ~{exit_price:.4f} | Чистый результат: ${net_pnl:+.2f} ({net_r:+.2f}R)\n")
            tg.notify_trade_closed(
                market="Bitget Crypto",
                symbol=sym,
                direction=t["direction"],
                exit_price=exit_price,
                exit_reason=exit_reason,
                net_pnl=net_pnl,
                currency="USDT",
                net_r=net_r,
                total_fees=total_fee,
            )
            continue

        # ЭТАП 1: Достижение 1.0R (Частичная фиксация 50% + перенос стопа в BE)
        if not t.get("partial_taken", False):
            is_tp1 = (current_p >= tp1) if direction == 1 else (current_p <= tp1)
            is_sl = (current_p <= sl) if direction == 1 else (current_p >= sl)

            if is_tp1:
                # Фиксация 50% объема
                half_amt = init_amt * 0.5
                try:
                    half_amt = float(exchange.amount_to_precision(sym, half_amt))
                    market = exchange.market(sym) if exchange.markets else {}
                    min_amt = float(market.get("limits", {}).get("amount", {}).get("min") or 0.0)
                    if half_amt < min_amt:
                        half_amt = min_amt
                except Exception:
                    pass

                close_side = "sell" if direction == 1 else "buy"
                try:
                    exchange.create_order(
                        sym, "market", close_side, half_amt,
                        params={"reduceOnly": True, "hedged": True}
                    )
                except Exception as e:
                    print(f"⚠️ Ошибка отправки частичного закрытия на биржу: {e}")

                exit_fee = (half_amt * current_p) * cfg.BITGET_TAKER_FEE_PCT
                gross_pnl = (current_p - entry_p) * half_amt * direction
                net_part = gross_pnl - exit_fee - (t["entry_fee"] * 0.5)
                part_r = net_part / risk_usd

                rem_amt = max(0.0, amt - half_amt)
                be_price = t.get("be_price", entry_p)

                t["partial_taken"] = True
                t["remaining_amount"] = rem_amt
                t["current_stop"] = be_price
                t["is_be_active"] = True
                t["banked_pnl"] = t.get("banked_pnl", 0.0) + net_part
                t["banked_r"] = t.get("banked_r", 0.0) + part_r
                t["total_fee"] = t.get("total_fee", t["entry_fee"]) + exit_fee
                updated = True

                print(f"\n🎯 ЧАСТИЧНЫЙ ТЕЙК (1.0R) ВЗЯТ: {sym} ({t['direction']})")
                print(f"   Зафиксировано: 50% ({half_amt}) @ {current_p:.4f} | Прибыль: +${net_part:.2f} (+{part_r:.2f}R)")
                print(f"   🛡️ Стоп перенесён в БЕЗУБЫТОК: {be_price:.4f} | Остаток позиции: {rem_amt}\n")

                tg.notify_partial_take(
                    market="Bitget Crypto",
                    symbol=sym,
                    direction=t["direction"],
                    fill_price=current_p,
                    closed_str=f"{half_amt} ({half_amt * current_p:.1f} USDT)",
                    remaining_str=f"{rem_amt} ({rem_amt * current_p:.1f} USDT)",
                    be_stop=be_price,
                )
                continue

            elif is_sl:
                # Начальный SL до взятия 1.0R (-1.0R)
                close_side = "sell" if direction == 1 else "buy"
                try:
                    exchange.create_order(
                        sym, "market", close_side, amt,
                        params={"reduceOnly": True, "hedged": True}
                    )
                except Exception as e:
                    pass

                exit_fee = (amt * current_p) * cfg.BITGET_TAKER_FEE_PCT
                total_fee = t["entry_fee"] + exit_fee
                gross_pnl = (current_p - entry_p) * amt * direction
                net_pnl = gross_pnl - total_fee
                net_r = net_pnl / risk_usd

                t["status"] = "CLOSED"
                t["exit_price"] = current_p
                t["exit_time"] = datetime.now(timezone.utc).isoformat()
                t["exit_reason"] = "STOP_LOSS"
                t["exit_fee"] = exit_fee
                t["total_fee"] = total_fee
                t["gross_pnl"] = gross_pnl
                t["net_pnl"] = net_pnl
                t["net_r"] = net_r
                updated = True

                print(f"\n🛑 СТОП-ЛОСС СРАБОТАЛ: {sym} ({t['direction']})")
                print(f"   Цена выхода: {current_p:.4f} | Чистый убыток: -${abs(net_pnl):.2f} ({net_r:.2f}R)")
                print(f"   Уплачено комиссий (Open+Close): ${total_fee:.3f}\n")

                tg.notify_trade_closed(
                    market="Bitget Crypto",
                    symbol=sym,
                    direction=t["direction"],
                    exit_price=current_p,
                    exit_reason="STOP_LOSS",
                    net_pnl=net_pnl,
                    currency="USDT",
                    net_r=net_r,
                    total_fees=total_fee,
                )
                continue

        # ЭТАП 2: Позиция уже в безубытке (сопровождение остатка к 1.618R)
        else:
            is_tp2 = (current_p >= tp2) if direction == 1 else (current_p <= tp2)
            is_be = (current_p <= sl) if direction == 1 else (current_p >= sl)

            if is_tp2 or is_be:
                exit_reason = "TAKE_PROFIT" if is_tp2 else "BREAKEVEN"
                close_side = "sell" if direction == 1 else "buy"
                try:
                    exchange.create_order(
                        sym, "market", close_side, amt,
                        params={"reduceOnly": True, "hedged": True}
                    )
                except Exception as e:
                    pass

                exit_fee = (amt * current_p) * cfg.BITGET_TAKER_FEE_PCT
                gross_rem = (current_p - entry_p) * amt * direction
                net_rem = gross_rem - exit_fee - (t["entry_fee"] * 0.5)
                rem_r = net_rem / risk_usd

                total_net_pnl = t.get("banked_pnl", 0.0) + net_rem
                total_r = t.get("banked_r", 0.0) + rem_r
                total_fee = t.get("total_fee", 0.0) + exit_fee

                t["status"] = "CLOSED"
                t["exit_price"] = current_p
                t["exit_time"] = datetime.now(timezone.utc).isoformat()
                t["exit_reason"] = exit_reason
                t["exit_fee"] = exit_fee
                t["total_fee"] = total_fee
                t["gross_pnl"] = t.get("gross_pnl", 0.0) + gross_rem
                t["net_pnl"] = total_net_pnl
                t["net_r"] = total_r
                updated = True

                if is_tp2:
                    print(f"\n🏆 ПОЛНЫЙ ФИБО-ТЕЙК (1.618R) ВЗЯТ: {sym} ({t['direction']})")
                    print(f"   Цена выхода: {current_p:.4f} | Итоговая прибыль: +${total_net_pnl:.2f} (+{total_r:.2f}R)")
                    print(f"   Уплачено комиссий (Open+Close): ${total_fee:.3f}\n")
                else:
                    print(f"\n🛡️ ОСТАТОК ЗАКРЫТ ПО БЕЗУБЫТКУ: {sym} ({t['direction']})")
                    print(f"   Цена выхода: {current_p:.4f} | Итоговая прибыль: +${total_net_pnl:.2f} (+{total_r:.2f}R)")
                    print(f"   Уплачено комиссий: ${total_fee:.3f} (Прибыль 1.0R сохранена!)\n")

                tg.notify_trade_closed(
                    market="Bitget Crypto",
                    symbol=sym,
                    direction=t["direction"],
                    exit_price=current_p,
                    exit_reason=exit_reason,
                    net_pnl=total_net_pnl,
                    currency="USDT",
                    net_r=total_r,
                    total_fees=total_fee,
                )
                continue

    if updated:
        save_trades(trades)


def show_performance_stats():
    """
    Выводит детальный аналитический отчет по всем собранным live/demo сделкам,
    включая комиссии, чистый PnL, винрейт и Profit Factor.
    """
    trades = load_trades()
    print("\n" + "=" * 80)
    print("           📊 СТАТИСТИКА LIVE/DEMO ТОРГОВЛИ И УЧЕТ КОМИССИЙ (BITGET)")
    print("=" * 80)

    if not trades:
        print("Сделок пока нет. Запустите бота для начала торговли.")
        print("=" * 80 + "\n")
        return

    closed = [t for t in trades if t.get("status") == "CLOSED"]
    open_trades = [t for t in trades if t.get("status") == "OPEN"]

    print(f"Всего ордеров: {len(trades)} | Открытых позиций: {len(open_trades)} | Закрытых: {len(closed)}")
    print("-" * 80)

    if open_trades:
        print(f"📌 АКТИВНЫЕ ОТКРЫТЫЕ ПОЗИЦИИ ({len(open_trades)}):")
        for ot in open_trades:
            sr = ot.get("setup_reason")
            ai_str = f" | AI: {sr.get('ai_score')}/10" if (isinstance(sr, dict) and sr.get('ai_score')) else ""
            sr_str = f"\n     ↳ Сетап: {sr.get('bias_desc', '')} свип @ {sr.get('sweep_price', 0):.4f} (FVG: [{sr.get('fvg_bottom', 0):.4f} - {sr.get('fvg_top', 0):.4f}]){ai_str}" if isinstance(sr, dict) else ""
            print(f"   • {ot['symbol']} | {ot['direction']} {ot['amount']} | Вход: {ot['entry_price']:.4f} | SL: {ot['stop_loss']:.4f} | TP: {ot['take_profit']:.4f}{sr_str}")
        print("-" * 80)

    if not closed:
        print(f"Сейчас открыто {len(open_trades)} позиций, закрытых сделок пока нет.")
        print("=" * 80 + "\n")
        return

    wins = [t for t in closed if t.get("net_pnl", 0) > 0]
    losses = [t for t in closed if t.get("net_pnl", 0) <= 0]

    winrate = len(wins) / len(closed) * 100.0 if closed else 0.0
    total_gross = sum(t.get("gross_pnl", 0) for t in closed)
    total_fees = sum(t.get("total_fee", 0) for t in closed)
    total_net = sum(t.get("net_pnl", 0) for t in closed)
    total_r = sum(t.get("net_r", 0) for t in closed)

    gross_wins = sum(t.get("gross_pnl", 0) for t in wins)
    gross_losses = abs(sum(t.get("gross_pnl", 0) for t in losses))
    pf = (gross_wins / gross_losses) if gross_losses > 0 else (999.0 if gross_wins > 0 else 0.0)

    avg_win_usd = (sum(t.get("net_pnl", 0) for t in wins) / len(wins)) if wins else 0.0
    avg_loss_usd = (sum(t.get("net_pnl", 0) for t in losses) / len(losses)) if losses else 0.0

    print(f" Винрейт (Winrate):          {winrate:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f" Грязная прибыль (Gross):    ${total_gross:+.2f}")
    print(f" 💳 Уплачено биржевых комиссий: ${total_fees:.2f}")
    print(f" 💵 ЧИСТАЯ ПРИБЫЛЬ (Net PnL):  ${total_net:+.2f}")
    print(f" Суммарный результат в R:    {total_r:+.2f}R")
    print(f" Профит-фактор (PF):         {pf:.2f}")
    print(f" Средний выигрыш / убыток:   +${avg_win_usd:.2f} / -${abs(avg_loss_usd):.2f}")
    print("-" * 80)

    # Статистика по инструментам
    print("РАЗБИВКА ПО ИНСТРУМЕНТАМ:")
    by_sym = {}
    for t in closed:
        s = t["symbol"]
        if s not in by_sym:
            by_sym[s] = []
        by_sym[s].append(t)

    print(f"{'Инструмент':<18} | {'Сделок':<7} | {'Winrate':<8} | {'Чистый PnL':<12} | {'Комиссии':<9} | {'Net R':<8}")
    print("-" * 75)
    for sym, sym_trades in by_sym.items():
        sym_wins = [t for t in sym_trades if t.get("net_pnl", 0) > 0]
        wr = len(sym_wins) / len(sym_trades) * 100.0
        s_pnl = sum(t.get("net_pnl", 0) for t in sym_trades)
        s_fees = sum(t.get("total_fee", 0) for t in sym_trades)
        s_r = sum(t.get("net_r", 0) for t in sym_trades)
        print(f"{sym:<18} | {len(sym_trades):<7} | {wr:>6.1f}% | ${s_pnl:>+10.2f} | ${s_fees:>7.2f} | {s_r:>+6.2f}R")

    print("\nПОСЛЕДНИЕ ЗАКРЫТЫЕ СДЕЛКИ:")
    fmt = "   {:<16} | {:<5} | {:>9} | Вход: {:>9.4f} | Выход: {:>9.4f} | {:>11} | Net R: {:>6.2f}R | Net PnL: {:>+8.2f} USD"
    for t in closed[-10:]:
        print(fmt.format(
            t["symbol"],
            t["direction"],
            t["amount"],
            t["entry_price"],
            t.get("exit_price") or 0.0,
            t.get("exit_reason", "N/A"),
            t.get("net_r", 0.0),
            t.get("net_pnl", 0.0),
        ))
        sr = t.get("setup_reason")
        if isinstance(sr, dict):
            ai_str = f" | AI: {sr.get('ai_score')}/10" if sr.get('ai_score') is not None else ""
            print(f"      ↳ Сетап: {sr.get('bias_desc', '')} свип @ {sr.get('sweep_price', 0):.4f} | FVG: [{sr.get('fvg_bottom', 0):.4f} - {sr.get('fvg_top', 0):.4f}]{ai_str}")

    print("=" * 80 + "\n")


def execute_or_propose_entry(
    exchange,
    swap_symbol,
    expected_dir,
    current_price,
    stop,
    sweep_time,
    sweep_candle,
    fvg_bottom,
    fvg_top,
    zone_pct,
    bias,
    df_htf,
    df_5m,
    df_1m,
    seen,
    args,
    sig_key,
    asian_ranges=None,
    is_ar_sweep=False,
    ar_type=None,
    timeframe_entry="5m",
):
    """
    Универсальный исполнитель сделки:
    - Расчет целей Grid B (TP1 1.0R + BE, TP2 1.618R Golden Fib).
    - Расчет размера позиции по риску (% от депозита).
    - Проверка маржи и лимитов биржи.
    - ИИ-оценка Gemini (при --ai).
    - Генерация 3-TF графика.
    - Отправка в Telegram (при --confirm или вне киллзон) или прямое размещение.
    """
    risk = (current_price - stop) if expected_dir == 1 else (stop - current_price)
    if risk <= 0 or risk / current_price < cfg.MIN_RISK_PCT:
        seen.add(sig_key)
        return False

    if expected_dir == 1:
        tp1 = current_price + (1.0 * risk)
        target = current_price + (1.618 * risk)
    else:
        tp1 = current_price - (1.0 * risk)
        target = current_price - (1.618 * risk)

    risk_pct = args.risk if args.risk is not None else cfg.RISK_PER_TRADE_PCT
    lev = args.leverage or cfg.LEVERAGE
    pos_calc = calculate_position_size(
        exchange=exchange,
        swap_symbol=swap_symbol,
        entry_price=current_price,
        stop_price=stop,
        risk_pct=risk_pct,
        leverage=lev,
    )

    dir_str = "LONG" if expected_dir == 1 else "SHORT"
    bal_now = get_account_balance(exchange)

    if not getattr(args, "force_leverage", False) and pos_calc["position_usdt"] > bal_now["equity"] * 1.05:
        forced_lev = pos_calc["position_usdt"] / max(bal_now["equity"], 0.01)
        seen.add(sig_key)
        print(f"\nℹ️ [{swap_symbol}] СИГНАЛ ПРОПУЩЕН: МИНИМАЛЬНЫЙ ЛОТ ТРЕБУЕТ ПРИНУДИТЕЛЬНОЕ ПЛЕЧО {forced_lev:.1f}x!")
        print(f"   Позиция биржи: ~${pos_calc['position_usdt']:.2f} USDT при балансе ${bal_now['equity']:.2f} USDT.\n")
        return False

    req_margin = pos_calc["position_usdt"] / lev
    if req_margin > bal_now["free"] * 0.95:
        seen.add(sig_key)
        print(f"\n⚠️ [{swap_symbol}] СИГНАЛ ПРОПУЩЕН: НЕ ХВАТАЕТ СВОБОДНОЙ МАРЖИ!")
        print(f"   Сетап:            {dir_str} по {current_price:.4f} (Свип в {sweep_time} UTC | FVG: [{fvg_bottom:.4f} - {fvg_top:.4f}])")
        print(f"   Требуется маржи:   ~${req_margin:.2f} USDT (при плече {lev}x)")
        print(f"   Свободно на счете: ${bal_now['free']:.2f} USDT (Equity: ${bal_now['equity']:.2f} USDT)\n")
        tg.notify_margin_warning(
            market="Bitget Crypto",
            symbol=swap_symbol,
            direction=dir_str,
            price=current_price,
            required_amount=req_margin,
            available_amount=bal_now["free"],
            currency="USDT",
            hint=f"Пополните счет на ${(req_margin - bal_now['free']):.2f}+ USDT или закройте позицию для входа.",
        )
        return False

    print(f"\n⚡ ОБНАРУЖЕН ВАЛИДНЫЙ СЕТАП ({timeframe_entry.upper()} ВХОД): {swap_symbol} | {dir_str} по {current_price:.4f}")
    print(f"   Время свипа: {sweep_time} UTC | FVG: [{fvg_bottom:.4f} - {fvg_top:.4f}]")
    print(f"   Расчетный SL: {stop:.4f} | TP1 (1.0R): {tp1:.4f} | TP2 (1.618R): {target:.4f}")
    print(f"   Размер позиции: {pos_calc['position_usdt']:.1f} USDT (Риск: {pos_calc['risk_pct']:.1f}% = ${pos_calc['risk_usd']:.2f})")

    score = None
    rec = None
    ai_eval = {}
    if args.ai:
        eval_payload = {
            "symbol": swap_symbol,
            "expected_dir": expected_dir,
            "sweep_time": str(sweep_time),
            "confirm_time": str(datetime.now(timezone.utc)),
            "bias": bias,
            "in_killzone": in_killzone(sweep_time, cfg.KILLZONES),
            "asian_range_sweep": args.asian_range,
            "zone_pct": zone_pct,
            "fvg_top": fvg_top,
            "fvg_bottom": fvg_bottom,
            "risk_pct": risk / current_price,
        }
        ai_eval = evaluate_setup(eval_payload)
        score = ai_eval.get("score", 5)
        rec = ai_eval.get("recommendation", "CAUTION")
        print(f"🤖 ИИ-ОЦЕНКА: {score}/10 | Рекомендация: {rec} ({ai_eval.get('source')})")
        print(f"   Вывод: {ai_eval.get('reasoning')}")
        if rec == "SKIP" or score < cfg.AI_CONFIDENCE_THRESHOLD:
            print(f"⛔ Вход отклонен ИИ (Score {score} < {cfg.AI_CONFIDENCE_THRESHOLD} или SKIP).")
            seen.add(sig_key)
            return False

    setup_reason = {
        "bias": bias,
        "bias_desc": "BULLISH" if expected_dir == 1 else "BEARISH",
        "sweep_time": str(sweep_time),
        "sweep_price": float(sweep_candle["low"] if expected_dir == 1 else sweep_candle["high"]),
        "fvg_bottom": float(fvg_bottom),
        "fvg_top": float(fvg_top),
        "fvg_zone_pct": round(float(zone_pct) * 100, 2),
        "risk_pct": round(float(risk / current_price) * 100, 2),
        "asian_range_sweep": bool(args.asian_range and is_ar_sweep),
        "asian_range_type": ar_type if (args.asian_range and is_ar_sweep) else None,
        "in_killzone": bool(args.killzones and in_killzone(sweep_time, cfg.KILLZONES)),
        "ai_score": score if args.ai else None,
        "ai_recommendation": rec if args.ai else None,
        "ai_reasoning": ai_eval.get("reasoning") if args.ai else None,
        "timeframe_entry": timeframe_entry,
    }

    is_kz = in_killzone(sweep_time, cfg.KILLZONES) if args.killzones else True
    need_confirm = getattr(args, "confirm", False) or (args.killzones and not is_kz)

    chart_bytes = None
    try:
        df_15m = fetch_recent(exchange, swap_symbol, timeframe="15m", limit=60)
        if df_15m is None or len(df_15m) < 10:
            if df_1m is not None and len(df_1m) >= 30:
                df_15m = resample(df_1m, "15min")
            else:
                df_15m = resample(df_5m, "15min")
        ar_tuple = None
        if args.asian_range and is_ar_sweep and asian_ranges:
            last_ar_date = max(asian_ranges.keys())
            ar_tuple = (asian_ranges[last_ar_date].get("low"), asian_ranges[last_ar_date].get("high"))

        chart_bytes = cg.generate_3tf_setup_chart(
            df_1h=df_htf,
            df_15m=df_15m,
            df_5m=df_5m,
            symbol=swap_symbol,
            direction=dir_str,
            entry_price=current_price,
            stop_loss=stop,
            take_profit=target,
            sweep_price=float(sweep_candle["low"] if expected_dir == 1 else sweep_candle["high"]),
            fvg_bottom=float(fvg_bottom),
            fvg_top=float(fvg_top),
            asian_range=ar_tuple,
            bias_desc="BULLISH" if expected_dir == 1 else "BEARISH",
        )
    except Exception as e:
        print(f"⚠️ Ошибка формирования графика 3-TF: {e}")

    if need_confirm:
        session_status = "ВНЕ КИЛЛЗОНЫ" if not is_kz else "РЕЖИМ ПОДТВЕРЖДЕНИЯ"
        print(f"\n💡 [{session_status}] Сетап {swap_symbol} ({dir_str}) по {current_price:.4f} отправлен в Telegram.")
        print(f"   Скриншот 3-TF и кнопки [Открыть] / [Пропустить] направлены пользователю.\n")
        sig_id = ps.create_pending_signal(
            market="bitget",
            symbol=swap_symbol,
            direction=expected_dir,
            entry_price=current_price,
            stop_loss=stop,
            take_profit=target,
            pos_calc=pos_calc,
            setup_reason=setup_reason,
        )
        msg_id = tg.notify_setup_proposal(
            market="Bitget Crypto",
            symbol=swap_symbol,
            direction=dir_str,
            entry_price=current_price,
            stop_loss=stop,
            take_profit=target,
            amount_str=f"${pos_calc['position_usdt']:.2f} USDT ({pos_calc.get('size_contracts', 0)} контр.)",
            risk_str=f"{pos_calc['risk_pct']:.1f}% (${pos_calc['risk_usd']:.2f} USDT)",
            setup_reason=setup_reason,
            chart_bytes=chart_bytes,
            sig_id=sig_id,
            is_off_session=bool(args.killzones and not is_kz),
        )
        if msg_id:
            ps.set_signal_message_id(sig_id, msg_id)
        seen.add(sig_key)
        return True

    try:
        place_bracket_order(
            exchange=exchange,
            swap_symbol=swap_symbol,
            direction=expected_dir,
            entry_price=current_price,
            stop=stop,
            target=target,
            pos_calc=pos_calc,
            setup_reason=setup_reason,
            tp1=tp1,
        )
        if chart_bytes:
            tg.send_telegram_photo(chart_bytes, caption=f"📸 <b>3-TF График открытой сделки:</b> <code>{swap_symbol}</code>")
        seen.add(sig_key)
        return True
    except Exception as e:
        print(f"❌ ОШИБКА размещения ордера на бирже: {e}")
        seen.add(sig_key)
        return False


def scan_5m_sweeps(exchange, target_symbols, seen, args, symbol_states):
    """
    Фаза 1 (IDLE): Сканирование на закрытии 5-минутных свечей.
    Запрашивает 1H для тренда и нативные 5m бары.
    При обнаружении свипа по тренду:
      - Если 5m FVG уже готов и протестирован -> вход.
      - Иначе -> переводит символ в состояние ARMED (охота за 1m FVG на 25 мин).
    """
    trades = load_trades()
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    bal = get_account_balance(exchange)
    max_pos = args.max_pos if args.max_pos is not None else (1 if bal["equity"] < 50 else 3)
    if len(open_trades) >= max_pos:
        return

    now_utc_dt = datetime.now(timezone.utc)
    for sym in target_symbols:
        if any(t["symbol"] == sym for t in open_trades):
            continue
        if symbol_states.get(sym, {}).get("state") == "ARMED":
            continue

        try:
            df_htf = fetch_recent(exchange, sym, timeframe="1h", limit=60)
            if df_htf is None or len(df_htf) < cfg.SWING_LENGTH_HTF * 2:
                continue
            bias_series = compute_bias_series(df_htf, cfg.SWING_LENGTH_HTF)

            df_5m = fetch_recent(exchange, sym, timeframe="5m", limit=120)
            if df_5m is None or len(df_5m) < cfg.SWING_LENGTH_LTF * 2 + 5:
                continue

            # Отсекаем формирующийся бар, анализируем закрытые свечи
            df_closed = df_5m.iloc[:-1].copy()
            current_price = df_5m["close"].iloc[-1]

            # Проверка волатильности рынка (ATR 14 в % от цены для защиты от мертвого боковика)
            if getattr(cfg, "USE_VOLATILITY_FILTER", True):
                tr1 = df_closed["high"] - df_closed["low"]
                tr2 = (df_closed["high"] - df_closed["close"].shift(1)).abs()
                tr3 = (df_closed["low"] - df_closed["close"].shift(1)).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                atr_val = tr.rolling(14).mean().iloc[-1]
                atr_pct = (atr_val / current_price) if current_price > 0 else 0.0
                min_atr = getattr(cfg, "MIN_ATR_5M_PCT", 0.0005)
                if atr_pct < min_atr:
                    # Волатильность усохла - пропускаем сканирование свипов на мертвом рынке
                    continue

            swings_5m = smc.swing_highs_lows(df_closed, swing_length=cfg.SWING_LENGTH_LTF)
            liq_5m = smc.liquidity(df_closed, swings_5m, range_percent=cfg.LIQUIDITY_RANGE_PCT)
            liq_5m.index = df_closed.index
            fvg_5m = smc.fvg(df_closed)
            fvg_5m.index = df_closed.index

            asian_ranges = {}
            if args.asian_range:
                asian_ranges = compute_asian_ranges(df_closed, cfg.ASIAN_HOURS)

            swept = liq_5m[(liq_5m["Swept"].notna()) & (liq_5m["Swept"] != 0)]
            if swept.empty:
                continue

            recent_swept = swept.tail(3)
            for idx, row in recent_swept.iterrows():
                swept_bar_idx = int(row["Swept"])
                if swept_bar_idx <= 0 or swept_bar_idx >= len(df_closed):
                    continue
                sweep_time = df_closed.index[swept_bar_idx]
                sig_key = f"{sym}_{sweep_time}"
                if sig_key in seen:
                    continue

                bias = bias_at(bias_series, sweep_time)
                if bias == 0:
                    continue
                expected_dir = 1 if row["Liquidity"] == -1 else -1
                if expected_dir != bias:
                    continue

                sweep_candle = df_closed.iloc[swept_bar_idx]

                is_ar_sweep = False
                ar_type = None
                if args.asian_range:
                    if not asian_ranges:
                        continue
                    is_ar_sweep, ar_type = is_asian_range_sweep(sweep_time, sweep_candle, asian_ranges)
                    if not is_ar_sweep:
                        continue

                buffer = current_price * getattr(cfg, "STOP_BUFFER_PCT", 0.0015)
                stop = (sweep_candle["low"] - buffer) if expected_dir == 1 else (sweep_candle["high"] + buffer)
                risk = abs(current_price - stop)
                if risk <= 0 or risk / current_price < cfg.MIN_RISK_PCT:
                    seen.add(sig_key)
                    continue

                # Проверка: есть ли уже 5m FVG и тест ценой прямо сейчас
                window_5m = fvg_5m[(fvg_5m.index > sweep_time)].head(15)
                matching_5m = window_5m[window_5m["FVG"] == expected_dir]
                entered_5m = False
                if len(matching_5m) > 0:
                    fvg_top, fvg_bottom = matching_5m.iloc[0]["Top"], matching_5m.iloc[0]["Bottom"]
                    zone_pct = (fvg_top - fvg_bottom) / fvg_bottom
                    min_fvg = getattr(cfg, "MIN_FVG_ZONE_PCT", 0.0015)
                    if min_fvg <= zone_pct <= cfg.MAX_FVG_ZONE_PCT:
                        buffer_in = (fvg_top - fvg_bottom) * 0.1
                        if fvg_bottom - buffer_in <= current_price <= fvg_top + buffer_in:
                            entered_5m = execute_or_propose_entry(
                                exchange=exchange,
                                swap_symbol=sym,
                                expected_dir=expected_dir,
                                current_price=current_price,
                                stop=stop,
                                sweep_time=sweep_time,
                                sweep_candle=sweep_candle,
                                fvg_bottom=fvg_bottom,
                                fvg_top=fvg_top,
                                zone_pct=zone_pct,
                                bias=bias,
                                df_htf=df_htf,
                                df_5m=df_closed,
                                df_1m=None,
                                seen=seen,
                                args=args,
                                sig_key=sig_key,
                                asian_ranges=asian_ranges,
                                is_ar_sweep=is_ar_sweep,
                                ar_type=ar_type,
                                timeframe_entry="5m",
                            )
                            if entered_5m:
                                break

                if entered_5m:
                    break

                # Если вход еще не состоялся - взводим инструмент на 1m охоту
                dir_str = "LONG" if expected_dir == 1 else "SHORT"
                sweep_p = float(sweep_candle["low"] if expected_dir == 1 else sweep_candle["high"])
                symbol_states[sym] = {
                    "state": "ARMED",
                    "armed_data": {
                        "symbol": sym,
                        "direction": expected_dir,
                        "dir_str": dir_str,
                        "sweep_time": sweep_time,
                        "sweep_candle": sweep_candle,
                        "sweep_price": sweep_p,
                        "stop": stop,
                        "bias": bias,
                        "df_htf": df_htf,
                        "df_5m": df_closed,
                        "asian_ranges": asian_ranges,
                        "is_ar_sweep": is_ar_sweep,
                        "ar_type": ar_type,
                        "armed_at": now_utc_dt,
                        "expires_at": now_utc_dt + pd.Timedelta(minutes=25),
                        "sig_key": sig_key,
                    },
                }
                print(f"\n⚡ [ARMED] {sym}: Обнаружен {dir_str} свип на 5m в {sweep_time} UTC!")
                print(f"   Уровень свипа: {sweep_p:.4f} | Стоп: {stop:.4f} | Окно охоты за 1m FVG: 25 мин.\n")
                break
        except Exception as e:
            print(f"⚠️ Ошибка сканирования {sym}: {e}")


def check_armed_symbol_1m(exchange, sym, armed_data, seen, args, symbol_states):
    """
    Фаза 2 (ARMED): Ежеминутный снайперский поиск 1m FVG и его ретеста.
    """
    now_utc_dt = datetime.now(timezone.utc)
    if now_utc_dt >= armed_data["expires_at"]:
        print(f"\n⏰ [{sym}] Время ожидания входа (25 мин) истекло без теста FVG. Сброс в IDLE.\n")
        symbol_states[sym]["state"] = "IDLE"
        symbol_states[sym]["armed_data"] = None
        return

    try:
        df_1m = fetch_recent(exchange, sym, timeframe="1m", limit=60)
        if df_1m is None or len(df_1m) < 15:
            return

        current_price = df_1m["close"].iloc[-1]
        expected_dir = armed_data["direction"]
        stop = armed_data["stop"]

        # Инвалидация сетапа: пробой стоп-уровня фитиля до входа
        if expected_dir == 1 and current_price <= stop:
            print(f"\n❌ [{sym}] СЕТАП АННУЛИРОВАН: цена ({current_price:.4f}) пробила Low свипа ({stop:.4f}). Сброс в IDLE.\n")
            seen.add(armed_data["sig_key"])
            symbol_states[sym]["state"] = "IDLE"
            symbol_states[sym]["armed_data"] = None
            return
        elif expected_dir == -1 and current_price >= stop:
            print(f"\n❌ [{sym}] СЕТАП АННУЛИРОВАН: цена ({current_price:.4f}) пробила High свипа ({stop:.4f}). Сброс в IDLE.\n")
            seen.add(armed_data["sig_key"])
            symbol_states[sym]["state"] = "IDLE"
            symbol_states[sym]["armed_data"] = None
            return

        # Поиск 1m FVG, сформированного после времени свипа
        fvg_1m = smc.fvg(df_1m)
        fvg_1m.index = df_1m.index
        window_1m = fvg_1m[fvg_1m.index > armed_data["sweep_time"]]
        matching_1m = window_1m[window_1m["FVG"] == expected_dir]
        if len(matching_1m) == 0:
            return

        fvg_top, fvg_bottom = matching_1m.iloc[0]["Top"], matching_1m.iloc[0]["Bottom"]
        zone_pct = (fvg_top - fvg_bottom) / fvg_bottom
        min_fvg = getattr(cfg, "MIN_FVG_ZONE_PCT", 0.0005)
        if zone_pct < min_fvg or zone_pct > cfg.MAX_FVG_ZONE_PCT:
            return

        buffer_in = (fvg_top - fvg_bottom) * 0.1
        if not (fvg_bottom - buffer_in <= current_price <= fvg_top + buffer_in):
            return

        # Снайперский вход подтвержден!
        print(f"\n🎯 [1M SNIPER] {sym}: Обнаружен и протестирован 1m FVG [{fvg_bottom:.4f} - {fvg_top:.4f}] по {current_price:.4f}!")
        entered = execute_or_propose_entry(
            exchange=exchange,
            swap_symbol=sym,
            expected_dir=expected_dir,
            current_price=current_price,
            stop=stop,
            sweep_time=armed_data["sweep_time"],
            sweep_candle=armed_data["sweep_candle"],
            fvg_bottom=fvg_bottom,
            fvg_top=fvg_top,
            zone_pct=zone_pct,
            bias=armed_data["bias"],
            df_htf=armed_data["df_htf"],
            df_5m=armed_data["df_5m"],
            df_1m=df_1m,
            seen=seen,
            args=args,
            sig_key=armed_data["sig_key"],
            asian_ranges=armed_data["asian_ranges"],
            is_ar_sweep=armed_data["is_ar_sweep"],
            ar_type=armed_data["ar_type"],
            timeframe_entry="1m",
        )
        if entered:
            symbol_states[sym]["state"] = "IN_TRADE"
            symbol_states[sym]["armed_data"] = None
    except Exception as e:
        print(f"⚠️ Ошибка проверки 1m для {sym}: {e}")


def print_startup_briefing(exchange, args, target_symbols, risk_pct, demo_mode):
    bal = get_account_balance(exchange)
    equity = bal["equity"]
    free_margin = bal["free"]
    lev = args.leverage or cfg.LEVERAGE
    risk_usd = equity * (risk_pct / 100.0)

    # Примеры расчета размера позиции для типичных расстояний стоп-лосса
    pos_at_05 = min(risk_usd / 0.005, equity * lev * 0.90)
    pos_at_10 = min(risk_usd / 0.010, equity * lev * 0.90)
    max_pos = equity * lev * 0.90

    print("=" * 85)
    print("        🚀 ICT INSTITUTIONAL TRADING BOT | BITGET LIVE/DEMO RUNNER")
    print("=" * 85)
    print("💼 1. СОСТОЯНИЕ АККАУНТА И ДЕПОЗИТА:")
    print(f"   • Торговый режим:         {'🟢 DEMO (PAPTRADING, безопасный тест)' if demo_mode else '🔴 REAL TRADING (РЕАЛЬНЫЙ ДЕПОЗИТ)'}")
    print(f"   • Баланс депозита (Equity): {equity:.2f} USDT")
    print(f"   • Свободная маржа:        {free_margin:.2f} USDT")
    print(f"   • Рабочее плечо:          {lev}x")
    m_mode_label = "🔒 ISOLATED (Изолированная - защита баланса от сквизов и проскальзываний)" if getattr(args, "margin_mode", "isolated").lower() == "isolated" else "⚠️ CROSS (Кросс-маржа - общий пул обеспечения)"
    print(f"   • Режим маржи:            {m_mode_label}")
    print()
    print("🎯 2. РАСЧЕТ РАЗМЕРА СЛЕДУЮЩЕЙ СДЕЛКИ (ДИСЦИПЛИНА РИСКА):")
    print(f"   • Риск на сделку (1R):     {risk_pct:.1f}% от баланса = ${risk_usd:.2f} USD")
    print(f"   • Потеря при стоп-лоссе:  -${risk_usd:.2f} USD (ровно {risk_pct:.1f}% от депозита)")
    print(f"   • Прибыль при тейке (1.5R):+${risk_usd * cfg.PARTIAL_TAKE_R:.2f} USD (+{risk_pct * cfg.PARTIAL_TAKE_R:.1f}%)")
    print(f"   • Расчетный объем позиции (зависит от ширины стоп-свечи):")
    print(f"     - при стопе 0.5% (~300$ на BTC):  ~{pos_at_05:.1f} USDT")
    print(f"     - при стопе 1.0% (~600$ на BTC):  ~{pos_at_10:.1f} USDT")
    print(f"   • Максимальный лимит плеча/маржи:  {max_pos:.1f} USDT")
    print()
    print("🛡️ 3. АНАЛИЗ БЕЗОПАСНОСТИ ДЕПОЗИТА ПО ПАРАМ (PRE-FLIGHT RISK CHECK):")
    print(f"   {'Инструмент':<15} | {'Цена':<9} | {'Мин. лот':<9} | {'Мин. объем':<11} | {'Маржа (3x)':<11} | {'Убыток при 1% SL':<17} | {'Статус'}")
    print("   " + "-" * 90)

    for s in target_symbols:
        try:
            m = exchange.market(s) if exchange.markets else {}
            ticker = exchange.fetch_ticker(s)
            p = float(ticker.get('last') or 0.0)
            min_amt = float(m.get('limits', {}).get('amount', {}).get('min') or 0.0)
            min_cost = float(m.get('limits', {}).get('cost', {}).get('min') or 5.0)
            min_pos = max(min_amt * p, min_cost)
            req_margin = min_pos / lev
            loss_1pct = min_pos * 0.01
            loss_pct_of_dep = (loss_1pct / equity) * 100.0 if equity > 0 else 0.0

            if min_pos > equity * lev * 0.95:
                status = "❌ Не хватает маржи"
            elif loss_pct_of_dep > 3.0:
                status = "⚠️ Внимание (>3% риск)"
            elif loss_pct_of_dep > 1.5:
                status = "🟡 Умеренно (~2% риск)"
            else:
                status = "🟢 Безопасно (<1% риск)"

            print(f"   {s:<15} | ${p:<8.2f} | {min_amt:<9} | ${min_pos:<10.2f} | ${req_margin:<10.2f} | -${loss_1pct:.2f} ({loss_pct_of_dep:.1f}% деп.) | {status}")
        except Exception:
            pass
    print("   " + "-" * 90)
    effective_max_pos = args.max_pos if args.max_pos is not None else (1 if equity < 50 else 3)
    if effective_max_pos == 1:
        print("   💡 ЗАЩИТА ДЕПОЗИТА: Включен Single Position Mode (макс. 1 сделка одновременно).")
        print("      Бот защищает маржу и не откроет новую позицию, пока текущая не закроется в TP или SL.")
    else:
        print(f"   🚀 МУЛЬТИ-ПОЗИЦИОННЫЙ РЕЖИМ: Разрешено до {effective_max_pos} одновременных сделок (при наличии свободной маржи).")
    print()

    # 4. Проверка и отображение активных открытых позиций из журнала
    trades = load_trades()
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    if open_trades:
        print("📌 4. ВОССТАНОВЛЕННЫЕ АКТИВНЫЕ ПОЗИЦИИ (ИЗ ЖУРНАЛА СДЕЛОК):")
        for ot in open_trades:
            sr = ot.get("setup_reason")
            sr_text = ""
            if isinstance(sr, dict):
                ai_str = f" | AI: {sr.get('ai_score')}/10" if sr.get('ai_score') is not None else ""
                sr_text = f"\n      ↳ Сетап: {sr.get('bias_desc', '')} свип @ {sr.get('sweep_price', 0):.4f} (FVG: [{sr.get('fvg_bottom', 0):.4f} - {sr.get('fvg_top', 0):.4f}]){ai_str}"
            print(f"   • {ot['symbol']} | {ot['direction']} {ot['amount']} | Вход: {ot['entry_price']:.4f} | SL: {ot['stop_loss']:.4f} | TP: {ot['take_profit']:.4f}{sr_text}")
        print()

    print("⚙️ 5. ПАРАМЕТРЫ СТРАТЕГИИ И ФИЛЬТРЫ:")
    print(f"   • Фильтр Asian Range:     {'ВКЛЮЧЕН (00:00-06:00 UTC, 62.6% WR, PF 2.34)' if args.asian_range else 'Выключен'}")
    print(f"   • Фильтр Killzones:       {'ВКЛЮЧЕН (07-10 & 12-15 UTC)' if args.killzones else 'Выключен (Круглосуточно)'}")
    print(f"   • ИИ-оценка сделок:       {'ВКЛЮЧЕНА (Google Gemini ' + cfg.GEMINI_MODEL + ')' if args.ai else 'Выключена'}")
    print(f"   • Подтверждение Telegram: {'ВКЛЮЧЕНО (для всех сделок со скриншотом 3-TF)' if getattr(args, 'confirm', False) else 'ВКЛЮЧЕНО для сделок вне Киллзон (3-TF скриншот + кнопки)'}")
    print(f"   • Частичный тейк:         {cfg.PARTIAL_TAKE_R:.1f}R (закрытие 50% позиции)")
    print(f"   • Трейлинг остатка:       {cfg.TRAIL_DISTANCE_R:.1f}R от локального экстремума")
    print(f"   • Комиссия биржи (Taker): {cfg.BITGET_TAKER_FEE_PCT * 100:.2f}% (USDT-M Futures)")
    print(f"   • Частота сканирования:   каждые {args.poll_sec} сек.")
    print(f"   • Отслеживаемые пары:     {', '.join(target_symbols)} ({len(target_symbols)} шт.)")
    print("=" * 85 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Live/Demo Trading Bot for Bitget (ICT Strategy)")
    parser.add_argument("--demo", action="store_true", default=True, help="Торговать на демо-счете Bitget (по умолчанию: True)")
    parser.add_argument("--real", action="store_true", help="Включить РЕАЛЬНУЮ торговлю на депозите")
    parser.add_argument("--symbol", type=str, default=None, help="Торговать одним инструментом (напр., ETH/USDT:USDT)")
    parser.add_argument("--all", action="store_true", help="Мониторить все 5 инструментов корзины (BTC, ETH, SOL, BNB, XRP)")
    parser.add_argument("--asian-range", action="store_true", default=cfg.USE_ASIAN_RANGE_FILTER, help="Фильтр ликвидности Азиатской сессии (62.6%% WR)")
    parser.add_argument("--killzones", action="store_true", default=cfg.USE_KILLZONES, help="Торговать строго в London/NY Killzones")
    parser.add_argument("--ai", action="store_true", default=cfg.ENABLE_AI_EVALUATION, help="ИИ-оценка сделок через Gemini перед входом")
    parser.add_argument("--risk", type=float, default=None, help="Риск на сделку в %% от баланса депозита (по умолчанию: 1.0%%, макс: 5.0%%)")
    parser.add_argument("--aggressive", action="store_true", help="Агрессивный режим: риск 3.0%% от баланса на сделку")
    parser.add_argument("--max-pos", type=int, default=None, help="Максимум одновременно открытых позиций (по умолчанию: 1 при балансе < 50 USDT, иначе 3)")
    parser.add_argument("--leverage", type=int, default=None, help="Размер плеча (по умолчанию из config.py)")
    parser.add_argument("--margin-mode", choices=["isolated", "cross"], default=getattr(cfg, "MARGIN_MODE", "isolated"), help="Режим маржи: isolated (изолированная, по умолчанию) или cross (кросс)")
    parser.add_argument("--poll-sec", type=int, default=cfg.POLL_INTERVAL_SEC, help="Интервал проверки рынка в секундах")
    parser.add_argument("--alts", action="store_true", help="Торговать корзиной альтов без принудительного плеча (DOGE, ADA, XRP, BNB, SOL)")
    parser.add_argument("--force-leverage", action="store_true", help="Разрешить торговлю парами с высоким минимальным контрактом (с принудительным плечом)")
    parser.add_argument("--confirm", action="store_true", help="Запрашивать подтверждение сделок через Telegram для всех сделок со скриншотом")
    parser.add_argument("-y", "--yes", action="store_true", help="Автоматическое подтверждение запуска на реальном счете")
    parser.add_argument("--stats", action="store_true", help="Показать детальную статистику сделок, комиссий и PnL и выйти")
    args = parser.parse_args()

    # Если запрошен вывод статистики
    if args.stats:
        show_performance_stats()
        return

    demo_mode = False if args.real else (cfg.DEMO_MODE and not args.real)

    # Определение процента риска
    risk_pct = cfg.RISK_PER_TRADE_PCT
    if args.aggressive:
        risk_pct = 3.0
    if args.risk is not None:
        risk_pct = args.risk

    # Определение списка пар
    if args.symbol:
        sym = args.symbol if ":" in args.symbol else f"{args.symbol}:USDT"
        target_symbols = [sym]
    elif args.alts:
        target_symbols = list(cfg.SMALL_ACCOUNT_SYMBOLS)
    elif args.all:
        target_symbols = list(cfg.SWAP_SYMBOLS.values())
    else:
        target_symbols = [cfg.SWAP_SYMBOL]

    try:
        exchange = init_exchange(demo_mode=demo_mode, leverage=args.leverage, margin_mode=args.margin_mode, skip_confirm=args.yes, target_symbols=target_symbols)
    except RuntimeError:
        sys.exit(1)

    # Вывод полного стартового брифинга по балансу, расчету сделки и параметрам
    print_startup_briefing(exchange, args, target_symbols, risk_pct, demo_mode)

    tg.notify_bot_started(
        bot_name="Bitget USDT-M Futures",
        mode="REAL ACCOUNT" if args.real else "DEMO TRADING",
        symbols=target_symbols,
        risk_pct=risk_pct,
        max_pos=args.max_pos if args.max_pos else 3,
    )

    seen = load_seen()
    symbol_states = {sym: {"state": "IDLE", "armed_data": None} for sym in target_symbols}
    last_5m_checked_bucket = None
    last_1m_checked_bucket = None
    last_in_trade_check_time = 0.0

    print("📡 Иерархический Event-Driven мониторинг запущен:")
    print("   • Фаза 1 (IDLE): Дозор на закрытии 5m свечей (:00, :05, :10...)")
    print("   • Фаза 2 (ARMED): 1m снайперский поиск FVG при обнаружении 5m свипа")
    print("   • Фаза 3 (IN_TRADE): Быстрый 5с опрос тикера для фиксации 1.0R (TP1/BE) и 1.618R")
    print("   • Нажмите Ctrl+C для выхода.\n")

    while True:
        try:
            now_utc_dt = datetime.now(timezone.utc)
            now_utc = now_utc_dt.strftime("%H:%M:%S")
            now_epoch = time.time()

            # 1. Проверяем открытые позиции (IN_TRADE: каждые 5 сек через быстрый fetch_ticker)
            trades = load_trades()
            open_trades = [t for t in trades if t.get("status") == "OPEN"]
            if open_trades:
                if now_epoch - last_in_trade_check_time >= 5.0:
                    reconcile_open_trades(exchange)
                    last_in_trade_check_time = now_epoch

            # 2. Исполнение сигналов, подтвержденных пользователем в Telegram (реактивно)
            approved_signals = ps.get_approved_signals(market="bitget")
            for sig in approved_signals:
                sig_id = sig["id"]
                sig_sym = sig["symbol"]
                sig_dir = sig["direction"]
                sig_entry = sig["entry_price"]
                sig_stop = sig["stop_loss"]
                sig_target = sig["take_profit"]
                sig_pos = sig["pos_calc"]
                sig_reason = sig["setup_reason"]

                print(f"\n🚀 [TELEGRAM ОДОБРЕНИЕ] Пользователь подтвердил вход в {sig_sym} ({'LONG' if sig_dir == 1 else 'SHORT'})!")
                try:
                    ticker = exchange.fetch_ticker(sig_sym)
                    curr_p = ticker["last"]
                    slip_pct = abs(curr_p - sig_entry) / max(sig_entry, 0.0001) * 100
                    if slip_pct > 0.9:
                        print(f"⚠️ [Отклонено] Цена ушла на {slip_pct:.2f}% от сетапа ({curr_p:.4f} vs {sig_entry:.4f}). Вход отменен в целях безопасности.")
                        ps.mark_failed(sig_id, f"Проскальзывание {slip_pct:.2f}% > 0.9%")
                        continue

                    if sig_dir == 1:
                        if curr_p <= sig_stop:
                            print(f"⚠️ [Отклонено] Цена ({curr_p:.4f}) пробила SL ({sig_stop:.4f}). Вход отменен.")
                            ps.mark_failed(sig_id, f"Инвалидация: текущая цена {curr_p:.4f} пробила SL {sig_stop:.4f}")
                            continue
                        if curr_p >= sig_target:
                            print(f"⚠️ [Отклонено] Цена ({curr_p:.4f}) уже достигла TP ({sig_target:.4f}). Вход отменен.")
                            ps.mark_failed(sig_id, f"Инвалидация: текущая цена {curr_p:.4f} уже достигла TP {sig_target:.4f}")
                            continue
                    elif sig_dir == -1:
                        if curr_p >= sig_stop:
                            print(f"⚠️ [Отклонено] Цена ({curr_p:.4f}) пробила SL ({sig_stop:.4f}). Вход отменен.")
                            ps.mark_failed(sig_id, f"Инвалидация: текущая цена {curr_p:.4f} пробила SL {sig_stop:.4f}")
                            continue
                        if curr_p <= sig_target:
                            print(f"⚠️ [Отклонено] Цена ({curr_p:.4f}) уже достигла TP ({sig_target:.4f}). Вход отменен.")
                            ps.mark_failed(sig_id, f"Инвалидация: текущая цена {curr_p:.4f} уже достигла TP {sig_target:.4f}")
                            continue

                    order_res = place_bracket_order(
                        exchange=exchange,
                        swap_symbol=sig_sym,
                        direction=sig_dir,
                        entry_price=curr_p,
                        stop=sig_stop,
                        target=sig_target,
                        pos_calc=sig_pos,
                        setup_reason=sig_reason,
                    )
                    ps.mark_executed(sig_id, {"fill_price": curr_p, "order_res": str(order_res)})
                    print(f"✅ [УСПЕХ] Ордер успешно выставлен на бирже Bitget по подтверждению из Telegram!\n")
                except Exception as e:
                    print(f"❌ Ошибка исполнения подтвержденного ордера: {e}")
                    ps.mark_failed(sig_id, str(e))

            # 3. Фаза 2 (ARMED): Проверка 1m свечей для взведенных пар на :02 секунде каждой минуты
            armed_symbols = [s for s, st in symbol_states.items() if st["state"] == "ARMED"]
            now_1m_bucket = now_utc_dt.strftime("%Y-%m-%d %H:%M")
            if armed_symbols and now_utc_dt.second >= 2 and last_1m_checked_bucket != now_1m_bucket:
                last_1m_checked_bucket = now_1m_bucket
                for sym in armed_symbols:
                    check_armed_symbol_1m(exchange, sym, symbol_states[sym]["armed_data"], seen, args, symbol_states)

            # 4. Фаза 1 (IDLE): Сканирование 5m баров на закрытии свечи (:00, :05, :10...) или на первом старте
            minute_5_boundary = (now_utc_dt.minute // 5) * 5
            now_5m_bucket = f"{now_utc_dt.strftime('%Y-%m-%d %H')}:{minute_5_boundary:02d}"
            is_initial_start = (last_5m_checked_bucket is None)
            is_5m_candle_close = (now_utc_dt.minute % 5 == 0 and now_utc_dt.second >= 2)

            if is_initial_start or (is_5m_candle_close and last_5m_checked_bucket != now_5m_bucket):
                last_5m_checked_bucket = now_5m_bucket
                scan_5m_sweeps(exchange, target_symbols, seen, args, symbol_states)

            save_seen(seen)

            # Расчет секунд до следующей 5-минутной свечи
            sec_into_5m = (now_utc_dt.minute % 5) * 60 + now_utc_dt.second
            sec_to_5m = max(0, 300 - sec_into_5m)
            status_armed = f" | 🎯 Взведено: {len(armed_symbols)}" if armed_symbols else ""
            status_pos = f" | 💼 Позиций: {len(open_trades)}" if open_trades else ""
            sys.stdout.write(f"\r[{now_utc} UTC] Мониторинг {len(target_symbols)} пар (Риск {risk_pct:.1f}%){status_pos}{status_armed} | До 5m скана: {sec_to_5m}с   ")
            sys.stdout.flush()

        except KeyboardInterrupt:
            print("\n🛑 Бот остановлен пользователем.")
            break
        except Exception as e:
            print(f"\n⚠️ Ошибка в цикле: {e} - повтор через 5 секунд")
            time.sleep(5)

        # Реактивный сон: проверяем Telegram-подтверждения каждую секунду
        for _ in range(1):
            if ps.get_approved_signals(market="bitget"):
                break
            time.sleep(1)



if __name__ == "__main__":
    main()
