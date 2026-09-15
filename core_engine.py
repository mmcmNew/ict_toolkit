"""
core_engine.py - Единый канонический движок исполнения и квант-симуляции ICT Toolkit.

Единый источник истины для:
- backtest.py
- universe_screener.py
- live_trade.py
- tbank_trade.py

Реализует канонический институциональный профиль Grid B:
1. Вход: лимитный ордер на 50% Consequent Encroachment (CE) FVG в окне MAX_WAIT_MIN (120 мин).
2. Стоп: структурный за фитиль свечи свипа (wick) + буфер комиссии 0.15%.
3. Сетка Grid B:
   - TP1 (1.0R): закрытие 50% объема + перевод стопа в Безубыток (цена входа + комиссия).
   - TP2 (1.618R): лимитный тейк оставшихся 50% на золотом сечении Фибоначчи.
   - Достижение TP2 приносит +1.309R чистыми. Откат от 1.0R в БУ приносит +0.50R чистыми.
4. Фильтр волатильности Golden Mean (min FVG 0.05%, min 5m ATR 0.05%).
5. Фильтр тренда (1H ADX >= 20, Displacement Ratio >= 0.45, 1H EMA Alignment).
6. Исключение токсичных четвергов на Мосбирже.
"""

import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime

import config as cfg
from trend_filter import is_market_trending


GRID_B_STAGES = [
    {"target_r": 1.0, "size": 0.5, "move_be": True},
    {"target_r": 1.618, "size": 0.5, "move_be": False},
]


def in_moex_killzone(ts: pd.Timestamp) -> bool:
    """Проверяет золотое торговое окно Мосбиржи 11:00 - 15:00 МСК."""
    msk_hour = (ts.hour + 3) % 24
    t_num = msk_hour * 60 + ts.minute
    return 660 <= t_num < 900  # 11:00 (660m) -> 15:00 (900m)


def compute_metrics(trades: list | pd.DataFrame) -> dict:
    """
    Единый расчет количественных институциональных метрик.
    """
    if isinstance(trades, list):
        if not trades:
            return {
                "trades": 0, "wins": 0, "losses": 0, "winrate": 0.0,
                "total_r": 0.0, "avg_r": 0.0, "profit_factor": 0.0,
                "max_dd_r": 0.0, "recovery_ratio": 0.0, "score": 0.0,
                "long_trades": 0, "short_trades": 0,
            }
        df = pd.DataFrame(trades)
    else:
        df = trades

    if len(df) == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "winrate": 0.0,
            "total_r": 0.0, "avg_r": 0.0, "profit_factor": 0.0,
            "max_dd_r": 0.0, "recovery_ratio": 0.0, "score": 0.0,
            "long_trades": 0, "short_trades": 0,
        }

    sorted_df = df.sort_values("fill_time") if "fill_time" in df.columns else df
    r_series = sorted_df["R"].values
    n = len(r_series)

    wins = r_series[r_series > 0.001]
    losses = r_series[r_series <= 0.001]

    winrate = (len(wins) / n) * 100.0 if n > 0 else 0.0
    total_r = float(np.sum(r_series))
    avg_r = float(np.mean(r_series)) if n > 0 else 0.0

    gross_profit = float(np.sum(wins)) if len(wins) else 0.0
    gross_loss = float(abs(np.sum(losses))) if len(losses) else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.9 if gross_profit > 0 else 0.0)

    # Max Drawdown в R
    cum_r = np.cumsum(r_series)
    peak = np.maximum.accumulate(cum_r)
    drawdown = peak - cum_r
    max_dd_r = float(np.max(drawdown)) if len(drawdown) else 0.0

    # Recovery Ratio: Total Net R / max(Max DD, 0.5)
    recovery_ratio = round(total_r / max(max_dd_r, 0.5), 2) if total_r > 0 else 0.0

    # Composite Score: Recovery * (Winrate / 50) * min(PF, 3.0)
    score = round(recovery_ratio * (winrate / 50.0) * min(profit_factor, 3.0), 2) if total_r > 0 else 0.0

    longs = int((sorted_df["dir"] == "LONG").sum()) if "dir" in sorted_df.columns else 0
    shorts = int((sorted_df["dir"] == "SHORT").sum()) if "dir" in sorted_df.columns else 0

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(winrate, 1),
        "total_r": round(total_r, 2),
        "avg_r": round(avg_r, 3),
        "profit_factor": round(profit_factor, 2),
        "max_dd_r": round(max_dd_r, 2),
        "recovery_ratio": recovery_ratio,
        "score": score,
        "long_trades": longs,
        "short_trades": shorts,
    }


def simulate_grid_b(df_1m: pd.DataFrame, df_htf: pd.DataFrame, df_ltf: pd.DataFrame,
                   candidates: list, symbol: str = "BTC/USDT", is_moex: bool = False,
                   use_vol_filter: bool = True, min_fvg_pct: float = 0.0005,
                   min_atr_5m_pct: float = 0.0005, use_trend_filter: bool = True,
                   min_adx: float = 20.0, use_ai: bool = False, ai_filter: bool = False,
                   max_positions: int = None) -> tuple[pd.DataFrame, dict]:
    """
    Каноническая симуляция по эталонным правилам Grid B.
    """
    trades = []
    active_until = []
    fee_pct = cfg.TBANK_COMMISSION_PCT if is_moex else cfg.FEE_SLIPPAGE_PCT
    buffer_pct = getattr(cfg, "STOP_BUFFER_PCT", 0.0015)
    min_risk_pct = getattr(cfg, "MIN_RISK_PCT", 0.0015)
    max_wait_min = getattr(cfg, "MAX_WAIT_MIN", 120)
    max_hold_min = getattr(cfg, "MAX_HOLD_MIN", 2880)
    moex_exclude_days = getattr(cfg, "MOEX_EXCLUDE_DAYS", ["Thursday"]) if is_moex else []

    for c in candidates:
        st = c["sweep_time"]
        exp_dir = c["expected_dir"]

        # 1. MOEX: Фильтр торгового окна и токсичных дней недели
        if is_moex:
            msk_st = st + pd.Timedelta(hours=3)
            dow = msk_st.day_name()
            if dow in moex_exclude_days:
                continue
            if getattr(cfg, "USE_KILLZONES", True) and not in_moex_killzone(st):
                continue

        # 2. Фильтр волатильности Golden Mean
        if use_vol_filter:
            zone_pct = c.get("zone_pct")
            if zone_pct is None:
                zone_pct = (c["fvg_top"] - c["fvg_bottom"]) / c["fvg_bottom"] if c["fvg_bottom"] else 0.0
            if zone_pct < min_fvg_pct:
                continue
            atr_pct = c.get("atr_pct")
            if atr_pct is not None and atr_pct < min_atr_5m_pct:
                continue

        # 3. Фильтр тренда против распила (Anti-Flat / Trend Filter)
        if use_trend_filter and df_htf is not None:
            trend_ok, trend_reason, trend_metrics = is_market_trending(df_htf, c, min_adx=min_adx)
            if not trend_ok:
                continue

        # 4. Проверка исполнения лимитного ордера в окне MAX_WAIT_MIN (ce или edge)
        fvg_mode = getattr(cfg, "FVG_ENTRY_MODE", "ce")
        ce_price = c.get("fvg_ce", (c["fvg_top"] + c["fvg_bottom"]) / 2.0)
        search_pos = df_1m.index.searchsorted(c["confirm_time"], side="right")
        search_path = df_1m.iloc[search_pos : search_pos + max_wait_min]
        fill_time, fill_price = None, None

        for t, r in search_path.iterrows():
            if fvg_mode == "ce":
                if exp_dir == 1:
                    if r["low"] <= ce_price and r["high"] >= c["fvg_bottom"]:
                        fill_price = ce_price
                        fill_time = t
                        break
                else:
                    if r["high"] >= ce_price and r["low"] <= c["fvg_top"]:
                        fill_price = ce_price
                        fill_time = t
                        break
            else:  # edge entry
                if exp_dir == 1:
                    if r["low"] <= c["fvg_top"]:
                        fill_price = min(r["high"], c["fvg_top"])
                        fill_time = t
                        break
                else:
                    if r["high"] >= c["fvg_bottom"]:
                        fill_price = max(r["low"], c["fvg_bottom"])
                        fill_time = t
                        break

        if fill_time is None:
            continue  # ордер не исполнен (цена не вернулась в зону)

        # 5. Лимит открытых позиций
        if max_positions is not None and max_positions > 0:
            active_until = [exp for exp in active_until if exp > fill_time]
            if len(active_until) >= max_positions:
                continue

        # 6. Расчет истинного структурного стоп-лосса и риска
        buffer = fill_price * buffer_pct
        use_true_struct_stop = getattr(cfg, "USE_TRUE_STRUCTURAL_STOP", True)
        if use_true_struct_stop:
            path_between = df_1m.loc[c["sweep_time"] : fill_time]
            if len(path_between) > 0:
                if exp_dir == 1:
                    struct_low = min(c["sweep_candle_low"], path_between["low"].min())
                    stop = struct_low - buffer
                else:
                    struct_high = max(c["sweep_candle_high"], path_between["high"].max())
                    stop = struct_high + buffer
            else:
                stop = (c["sweep_candle_low"] - buffer) if exp_dir == 1 else (c["sweep_candle_high"] + buffer)
        else:
            stop = (c["sweep_candle_low"] - buffer) if exp_dir == 1 else (c["sweep_candle_high"] + buffer)

        risk = (fill_price - stop) if exp_dir == 1 else (stop - fill_price)

        if risk <= 0 or (risk / fill_price) < min_risk_pct:
            continue

        cost_r = (fill_price * fee_pct * 2.2) / risk
        fee_cost = fill_price * (fee_pct * 2.2)
        be_stop = (fill_price + fee_cost) if exp_dir == 1 else (fill_price - fee_cost)

        # 7. Цели Grid (Crypto vs MOEX)
        if is_moex:
            tp1_r = getattr(cfg, "TBANK_PARTIAL_TAKE_R", 2.5)
            tp2_r = getattr(cfg, "TBANK_RUNNER_TAKE_R", 2.618)
        else:
            tp1_r = getattr(cfg, "PARTIAL_TAKE_R", 1.0)
            tp2_r = getattr(cfg, "RUNNER_TAKE_R", 1.618)

        tp1_price = (fill_price + tp1_r * risk) if exp_dir == 1 else (fill_price - tp1_r * risk)
        tp2_price = (fill_price + tp2_r * risk) if exp_dir == 1 else (fill_price - tp2_r * risk)

        # 8. ИИ-оценка перед входом
        ai_score, ai_rec, ai_reason = None, None, None
        if use_ai:
            from ai_evaluator import evaluate_setup
            eval_payload = {
                "symbol": symbol,
                "expected_dir": exp_dir,
                "sweep_time": str(c["sweep_time"]),
                "confirm_time": str(c["confirm_time"]),
                "bias": c.get("bias", exp_dir),
                "in_killzone": in_moex_killzone(st) if is_moex else True,
                "zone_pct": (c["fvg_top"] - c["fvg_bottom"]) / c["fvg_bottom"],
                "fvg_top": c["fvg_top"],
                "fvg_bottom": c["fvg_bottom"],
                "fvg_ce": ce_price,
                "has_ob": c.get("ob_candidate") is not None,
                "risk_pct": risk / fill_price,
            }
            ai_eval = evaluate_setup(eval_payload)
            ai_score = ai_eval.get("score")
            ai_rec = ai_eval.get("recommendation")
            ai_reason = ai_eval.get("reasoning")
            if ai_filter and (ai_rec == "SKIP" or (ai_score is not None and ai_score < 7)):
                continue

        # 9. Эмуляция удержания позиции и отработки сетки Grid B
        future_pos = df_1m.index.searchsorted(fill_time, side="right")
        future = df_1m.iloc[future_pos : future_pos + max_hold_min]
        if len(future) == 0:
            continue

        current_stop = stop
        is_be_active = False
        rem_pos = 1.0
        banked_r = 0.0
        stage = 0  # 0: ждем TP1 (1.0R); 1: ждем TP2 (1.618R)

        highs = future["high"].values
        lows = future["low"].values
        closes = future["close"].values
        idx_times = future.index

        stopped = False
        exit_time = None
        exit_price = None
        exit_reason = None

        for i in range(len(highs)):
            h, l = highs[i], lows[i]
            t = idx_times[i]

            # Проверка срабатывания стопа
            if exp_dir == 1:
                if l <= current_stop:
                    stopped = True
                    exit_time = t
                    exit_price = current_stop
                    exit_reason = "BE_STOP" if is_be_active else "INITIAL_STOP"
                    break
            else:
                if h >= current_stop:
                    stopped = True
                    exit_time = t
                    exit_price = current_stop
                    exit_reason = "BE_STOP" if is_be_active else "INITIAL_STOP"
                    break

            # Проверка этапов сетки Grid
            if stage == 0:
                hit_tp1 = (h >= tp1_price) if exp_dir == 1 else (l <= tp1_price)
                if hit_tp1:
                    banked_r += 0.5 * tp1_r  # зафиксировано 50% объема
                    rem_pos = 0.5
                    is_be_active = True
                    current_stop = be_stop  # стоп перенесен в безубыток
                    stage = 1

            if stage == 1:
                hit_tp2 = (h >= tp2_price) if exp_dir == 1 else (l <= tp2_price)
                if hit_tp2:
                    banked_r += 0.5 * tp2_r  # зафиксировано оставшиеся 50%
                    rem_pos = 0.0
                    exit_time = t
                    exit_price = tp2_price
                    exit_reason = "GRID_FULL_TP"
                    break

        if stopped:
            rem_r = 0.0 if is_be_active else (-rem_pos * 1.0)
            total_r_net = banked_r + rem_r - cost_r
            outcome = exit_reason
        elif rem_pos <= 0.001:
            total_r_net = banked_r - cost_r
            outcome = "GRID_B_FULL_TP"
        else:
            # Выход по таймауту
            last_p = closes[-1]
            exit_time = idx_times[-1]
            exit_price = last_p
            unrealized_r = ((last_p - fill_price) / risk if exp_dir == 1 else (fill_price - last_p) / risk) * rem_pos
            total_r_net = banked_r + unrealized_r - cost_r
            outcome = "TIMEOUT_P50" if is_be_active else "TIMEOUT"

        trade_row = {
            "symbol": symbol,
            "sweep_time": c["sweep_time"],
            "confirm_time": c["confirm_time"],
            "dir": "LONG" if exp_dir == 1 else "SHORT",
            "fill_time": fill_time,
            "fill_price": round(fill_price, 4),
            "stop": round(stop, 4),
            "target_tp1": round(tp1_price, 4),
            "target_tp2": round(tp2_price, 4),
            "exit_time": exit_time,
            "exit_price": round(exit_price if exit_price else fill_price, 4),
            "outcome": outcome,
            "R": round(total_r_net, 3),
            "banked_r": round(banked_r, 3),
            "is_asian_sweep": c.get("is_asian_sweep", False),
            "has_smt": c.get("has_smt", False),
        }
        if use_ai:
            trade_row["ai_score"] = ai_score
            trade_row["ai_rec"] = ai_rec
            trade_row["ai_reason"] = ai_reason

        trades.append(trade_row)
        active_until.append(exit_time if exit_time else fill_time)

    trades_df = pd.DataFrame(trades)
    if len(trades_df):
        trades_df = trades_df.sort_values("fill_time").reset_index(drop=True)

    metrics = compute_metrics(trades_df)
    metrics["symbol"] = symbol
    return trades_df, metrics
