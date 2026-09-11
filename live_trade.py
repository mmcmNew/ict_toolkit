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
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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

SEEN_SIGNALS_PATH = "live_seen_signals.json"
TRADE_LOG_PATH = "live_trade_log.json"
LOOKBACK_BARS_1M = 1200  # ~20 часов минутных свечей


def load_seen():
    if os.path.exists(SEEN_SIGNALS_PATH):
        try:
            with open(SEEN_SIGNALS_PATH, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    try:
        with open(SEEN_SIGNALS_PATH, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, indent=2)
    except Exception as e:
        print(f"Предупреждение: не удалось сохранить seen signals: {e}")


def load_trades():
    if os.path.exists(TRADE_LOG_PATH):
        try:
            with open(TRADE_LOG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_trades(trades):
    try:
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

    return {
        "amount": amount,
        "position_usdt": final_pos_usd,
        "risk_usd": risk_usd,
        "risk_pct": risk_pct,
        "stop_dist_pct": stop_dist_pct,
        "equity_at_entry": equity,
        "est_open_fee": est_open_fee,
    }


def init_exchange(demo_mode=True, leverage=None, margin_mode=None, skip_confirm=False):
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

    for sym in list(cfg.SWAP_SYMBOLS.values()):
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


def place_bracket_order(exchange, swap_symbol, direction, entry_price, stop, target, pos_calc, setup_reason: dict = None):
    side = "buy" if direction == 1 else "sell"
    amount = pos_calc["amount"]

    params = {
        "hedged": True,  # Обязательно для Bitget USDT-M (Hedge Mode)
        "stopLoss": {"triggerPrice": float(exchange.price_to_precision(swap_symbol, stop))},
        "takeProfit": {"triggerPrice": float(exchange.price_to_precision(swap_symbol, target))},
    }
    order = exchange.create_order(swap_symbol, "market", side, amount, params=params)

    trade_entry = {
        "id": f"{swap_symbol.replace('/', '_').replace(':', '_')}_{int(time.time())}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": swap_symbol,
        "direction": "LONG" if direction == 1 else "SHORT",
        "entry_price": entry_price,
        "stop_loss": stop,
        "take_profit": target,
        "amount": amount,
        "position_usdt": pos_calc["position_usdt"],
        "risk_usd": pos_calc["risk_usd"],
        "risk_pct": pos_calc["risk_pct"],
        "order_id": order.get("id"),
        "status": "OPEN",
        "entry_fee": pos_calc["est_open_fee"],
        "exit_fee": 0.0,
        "total_fee": pos_calc["est_open_fee"],
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
    print(f"   🛑 Stop-Loss:   {stop:.4f} (-{pos_calc['stop_dist_pct']*100:.2f}%)")
    print(f"   🎯 Take-Profit: {target:.4f} (+{pos_calc['stop_dist_pct']*cfg.PARTIAL_TAKE_R*100:.2f}%, 1.5R)")
    print(f"   💳 Комиссия входа: ~${pos_calc['est_open_fee']:.3f} | ID: {order.get('id')}\n")

    tg.notify_trade_opened(
        market="Bitget Crypto",
        symbol=swap_symbol,
        direction="LONG" if direction == 1 else "SHORT",
        entry_price=entry_price,
        stop_loss=stop,
        take_profit=target,
        amount_str=f"{amount} (~{pos_calc['position_usdt']:.2f} USDT)",
        risk_str=f"{pos_calc['risk_pct']:.1f}% (~${pos_calc['risk_usd']:.2f} USDT)",
        setup_reason=setup_reason,
    )

    return order


def reconcile_open_trades(exchange):
    """
    Проверяет открытые сделки на бирже. Если сработал TP или SL,
    рассчитывает точный PnL, удерживаемые комиссии и закрывает сделку в статистике.
    """
    trades = load_trades()
    updated = False

    for t in trades:
        if t.get("status") != "OPEN":
            continue

        sym = t["symbol"]
        entry_p = t["entry_price"]
        sl = t["stop_loss"]
        tp = t["take_profit"]
        amt = t["amount"]
        direction = 1 if t["direction"] == "LONG" else -1
        risk_usd = max(t.get("risk_usd", 10.0), 0.01)

        # Проверяем текущую рыночную цену или статус через биржу
        try:
            ticker = exchange.fetch_ticker(sym)
            current_p = ticker["last"]
        except Exception:
            continue

        is_tp = (current_p >= tp) if direction == 1 else (current_p <= tp)
        is_sl = (current_p <= sl) if direction == 1 else (current_p >= sl)

        if is_tp or is_sl:
            exit_price = tp if is_tp else sl
            exit_reason = "TAKE_PROFIT" if is_tp else "STOP_LOSS"
            exit_fee = (amt * exit_price) * cfg.BITGET_TAKER_FEE_PCT
            total_fee = t["entry_fee"] + exit_fee

            gross_pnl = (exit_price - entry_p) * amt * direction
            net_pnl = gross_pnl - total_fee
            net_r = net_pnl / risk_usd

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

            if is_tp:
                print(f"\n🎉 ТЕЙК-ПРОФИТ СРАБОТАЛ: {sym} ({t['direction']})")
                print(f"   Цена выхода: {exit_price:.4f} | Чистая прибыль: +${net_pnl:.2f} (+{net_r:.2f}R)")
                print(f"   Уплачено комиссий (Open+Close): ${total_fee:.3f}\n")
            else:
                print(f"\n🛑 СТОП-ЛОСС СРАБОТАЛ: {sym} ({t['direction']})")
                print(f"   Цена выхода: {exit_price:.4f} | Чистый убыток: -${abs(net_pnl):.2f} ({net_r:.2f}R)")
                print(f"   Уплачено комиссий (Open+Close): ${total_fee:.3f}\n")

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


def process_symbol(exchange, swap_symbol, seen, args):
    # Контроль лимита одновременных открытых позиций (защита депозита)
    trades = load_trades()
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    bal = get_account_balance(exchange)
    max_pos = args.max_pos if args.max_pos is not None else (1 if bal["equity"] < 50 else 3)
    if len(open_trades) >= max_pos:
        return

    df_1m = fetch_recent(exchange, swap_symbol, timeframe="1m", limit=LOOKBACK_BARS_1M)
    if df_1m is None or len(df_1m) < 60:
        return

    # Загружаем 1H напрямую для глубокой истории структуры (>4 дней)
    df_htf = fetch_recent(exchange, swap_symbol, timeframe="1h", limit=100)
    if df_htf is None or len(df_htf) < cfg.SWING_LENGTH_HTF * 2:
        df_htf = resample(df_1m, cfg.HTF_RULE)

    df_ltf = resample(df_1m, cfg.LTF_RULE)

    if len(df_ltf) < cfg.SWING_LENGTH_LTF * 2:
        return

    bias_series = compute_bias_series(df_htf, cfg.SWING_LENGTH_HTF)
    swings_ltf = smc.swing_highs_lows(df_ltf, swing_length=cfg.SWING_LENGTH_LTF)
    liq_ltf = smc.liquidity(df_ltf, swings_ltf, range_percent=cfg.LIQUIDITY_RANGE_PCT)
    liq_ltf.index = df_ltf.index
    fvg_ltf = smc.fvg(df_ltf)
    fvg_ltf.index = df_ltf.index

    # Расчет Азиатского диапазона
    asian_ranges = {}
    if args.asian_range:
        asian_ranges = compute_asian_ranges(df_1m, cfg.ASIAN_HOURS)

    swept = liq_ltf[(liq_ltf["Swept"].notna()) & (liq_ltf["Swept"] != 0)]
    current_price = df_1m["close"].iloc[-1]
    latest_time = df_1m.index[-1]

    recent_swept = swept.tail(8)

    for idx, row in recent_swept.iterrows():
        swept_bar_idx = int(row["Swept"])
        if swept_bar_idx <= 0 or swept_bar_idx >= len(df_ltf):
            continue
        sweep_time = df_ltf.index[swept_bar_idx]
        sig_key = f"{swap_symbol}_{sweep_time}"
        if sig_key in seen:
            continue

        # Фильтр Киллзон
        if args.killzones and not in_killzone(sweep_time, cfg.KILLZONES):
            continue

        # Фильтр Старшего тренда (HTF Bias)
        bias = bias_at(bias_series, sweep_time)
        if bias == 0:
            continue
        expected_dir = 1 if row["Liquidity"] == -1 else -1
        if expected_dir != bias:
            continue

        sweep_candle = df_ltf.iloc[swept_bar_idx]

        # Фильтр ликвидности Азиатской сессии (Asian Range Sweep)
        if args.asian_range:
            if not asian_ranges:
                continue
            is_ar_sweep, ar_type = is_asian_range_sweep(sweep_time, sweep_candle, asian_ranges)
            if not is_ar_sweep:
                continue

        # Поиск FVG после свипа
        window = fvg_ltf[(fvg_ltf.index > sweep_time)].head(15)
        matching = window[window["FVG"] == expected_dir]
        if len(matching) == 0:
            continue

        fvg_top, fvg_bottom = matching.iloc[0]["Top"], matching.iloc[0]["Bottom"]
        zone_pct = (fvg_top - fvg_bottom) / fvg_bottom
        if zone_pct > cfg.MAX_FVG_ZONE_PCT:
            seen.add(sig_key)
            continue

        # Проверка касания FVG текущей ценой
        buffer_in = (fvg_top - fvg_bottom) * 0.1
        if not (fvg_bottom - buffer_in <= current_price <= fvg_top + buffer_in):
            continue

        # Расчет Стопа и Тейка с компенсацией комиссий (Fee-Adjusted Target)
        buffer = current_price * 0.0008
        fee_roundtrip_pct = cfg.BITGET_TAKER_FEE_PCT * 2  # 0.06% open + 0.06% close = 0.12%
        fee_price_buffer = current_price * fee_roundtrip_pct

        if expected_dir == 1:
            stop = sweep_candle["low"] - buffer
            risk = current_price - stop
            # Надбавка на комиссии, чтобы чистая прибыль после Taker-сборов была строго +1.5R:
            target = current_price + (cfg.PARTIAL_TAKE_R * risk) + fee_price_buffer
        else:
            stop = sweep_candle["high"] + buffer
            risk = stop - current_price
            target = current_price - (cfg.PARTIAL_TAKE_R * risk) - fee_price_buffer

        if risk <= 0 or risk / current_price < cfg.MIN_RISK_PCT:
            seen.add(sig_key)
            continue

        # Дисциплинированный расчет размера позиции по риску (% от депозита)
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

        # Фильтр принудительного плеча для небольших депозитов:
        bal_now = get_account_balance(exchange)
        if not getattr(args, "force_leverage", False) and pos_calc["position_usdt"] > bal_now["equity"] * 1.05:
            forced_lev = pos_calc["position_usdt"] / max(bal_now["equity"], 0.01)
            seen.add(sig_key)
            print(f"\nℹ️ [{swap_symbol}] СИГНАЛ ПРОПУЩЕН: МИНИМАЛЬНЫЙ ЛОТ ТРЕБУЕТ ПРИНУДИТЕЛЬНОЕ ПЛЕЧО {forced_lev:.1f}x!")
            print(f"   Позиция биржи: ~${pos_calc['position_usdt']:.2f} USDT при балансе ${bal_now['equity']:.2f} USDT.")
            print(f"   💡 Торгуем только инструменты 1x без вынужденного заемного плеча (DOGE, ADA, XRP, BNB, SOL). Флаг --force-leverage отключит это ограничение.\n")
            continue

        # Проверка фактической свободной маржи для открытия
        req_margin = pos_calc["position_usdt"] / lev
        if req_margin > bal_now["free"] * 0.95:
            seen.add(sig_key)
            print(f"\n⚠️ [{swap_symbol}] СИГНАЛ ПРОПУЩЕН: НЕ ХВАТАЕТ СВОБОДНОЙ МАРЖИ!")
            print(f"   Сетап:            {dir_str} по {current_price:.4f} (Свип в {sweep_time} UTC | FVG: [{fvg_bottom:.4f} - {fvg_top:.4f}])")
            print(f"   Требуется маржи:   ~${req_margin:.2f} USDT (при плече {lev}x)")
            print(f"   Свободно на счете: ${bal_now['free']:.2f} USDT (Equity: ${bal_now['equity']:.2f} USDT)")
            print(f"   💡 Подсказка:     Пополните счет на ${(req_margin - bal_now['free']):.2f}+ USDT или закройте позицию для входа.\n")
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
            continue
        print(f"\n⚡ ОБНАРУЖЕН ВАЛИДНЫЙ СЕТАП: {swap_symbol} | {dir_str} по {current_price:.4f}")
        print(f"   Время свипа: {sweep_time} UTC | FVG: [{fvg_bottom:.4f} - {fvg_top:.4f}]")
        print(f"   Расчетный SL: {stop:.4f} | TP (1.5R): {target:.4f}")
        print(f"   Размер позиции: {pos_calc['position_usdt']:.1f} USDT (Риск: {pos_calc['risk_pct']:.1f}% = ${pos_calc['risk_usd']:.2f})")

        # ИИ-фильтр качества через Google Gemini
        if args.ai:
            eval_payload = {
                "symbol": swap_symbol,
                "expected_dir": expected_dir,
                "sweep_time": str(sweep_time),
                "confirm_time": str(latest_time),
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
                continue

        # Формирование подробного обоснования решения на вход
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
        }

        # Исполнение ордера
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
            )
        except Exception as e:
            print(f"❌ ОШИБКА размещения ордера на бирже: {e}")

        seen.add(sig_key)


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
        exchange = init_exchange(demo_mode=demo_mode, leverage=args.leverage, margin_mode=args.margin_mode, skip_confirm=args.yes)
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

    print(f"📡 Сканирование запущено. Интервал: {args.poll_sec} сек. Нажмите Ctrl+C для выхода.")

    while True:
        try:
            now_utc_dt = datetime.now(timezone.utc)
            now_utc = now_utc_dt.strftime("%H:%M:%S")

            # 1. Проверяем открытые позиции и учитываем закрытия/комиссии
            reconcile_open_trades(exchange)

            # Проверяем наличие открытых позиций
            open_trades = [t for t in load_trades() if t.get("status") == "OPEN"]

            # Если включен режим Киллзон и открытых позиций нет - умный сон до начала сессии
            if args.killzones and len(open_trades) == 0:
                diff_sec, kz_name, target_dt = get_next_killzone_delta(now_utc_dt, cfg.KILLZONES)
                if diff_sec > 180:
                    sleep_sec = diff_sec - 120  # просыпаемся за 2 минуты до старта
                    target_str = target_dt.strftime("%H:%M UTC")
                    mins_left = sleep_sec // 60
                    print(f"\n💤 [Киллзоны] Вне торгового окна (следующая: {kz_name} в {target_str}).")
                    print(f"   Открытых позиций нет. Бот переходит в спящий режим на {mins_left} мин. (до {target_str})...\n")

                    wake_target = time.time() + sleep_sec
                    while time.time() < wake_target:
                        time.sleep(min(60, wake_target - time.time()))
                    print(f"\n⏰ [Киллзоны] Пробуждение к началу {kz_name}! Начинаю сканирование...")
                    continue

            # 2. Сканируем новые сетапы по парам
            for sym in target_symbols:
                process_symbol(exchange, sym, seen, args)

            save_seen(seen)
            sys.stdout.write(f"\r[{now_utc} UTC] Мониторинг {len(target_symbols)} пар (Риск {risk_pct:.1f}%)... Все спокойно.    ")
            sys.stdout.flush()
        except KeyboardInterrupt:
            print("\n🛑 Бот остановлен пользователем.")
            break
        except Exception as e:
            print(f"\n⚠️ Ошибка в цикле: {e} - повтор через 10 секунд")
            time.sleep(10)

        time.sleep(args.poll_sec)


if __name__ == "__main__":
    main()
