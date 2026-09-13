"""
Прогон стратегии на исторических данных с поддержкой:
- Мульти-инструментального тестирования (сравнение BTC, ETH, SOL и др.)
- Выявления аномалий и переобучения (discrepancy analysis)
- ИИ-оценки сетапов через Google Gemini API / Rule-based evaluator
- Детальных квант-метрик: Winrate, Total R, Profit Factor, Max Drawdown (R)

Запуск:
  python backtest.py                         # одиночный бэктест по умолчанию
  python backtest.py --all                   # по всем инструментам из config.SYMBOLS
  python backtest.py --symbols BTC/USDT,ETH/USDT
  python backtest.py --ai                    # с включением ИИ-оценки сделок
  python backtest.py --ai-filter             # пропускать сделки с оценкой ниже порога
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import os
import argparse
import pandas as pd
import numpy as np
import config as cfg
from data_sources import get_data, get_symbol_slug
from strategy import resample, find_candidates, in_killzone
from ai_evaluator import evaluate_setup


def compute_metrics(trades_df: pd.DataFrame) -> dict:
    """Расчёт детальных квант-метрик по списку сделок с гарантией хронологического порядка."""
    if len(trades_df) == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "winrate": 0.0,
            "total_r": 0.0, "avg_r": 0.0, "profit_factor": 0.0,
            "max_dd_r": 0.0, "long_trades": 0, "short_trades": 0
        }

    # Гарантируем хронологическую сортировку для корректного расчета кумулятивного PnL и Max Drawdown
    sort_col = "exit_time" if "exit_time" in trades_df.columns else "fill_time"
    sorted_df = trades_df.sort_values(sort_col).reset_index(drop=True)

    wins = sorted_df[sorted_df["R"] > 0]
    losses = sorted_df[sorted_df["R"] <= 0]
    winrate = (len(wins) / len(sorted_df)) * 100.0
    total_r = sorted_df["R"].sum()
    avg_r = sorted_df["R"].mean()

    gross_profit = wins["R"].sum()
    gross_loss = abs(losses["R"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.9 if gross_profit > 0 else 0.0)

    # Расчёт просадки в R (Max Drawdown) строго во времени
    cum_r = sorted_df["R"].cumsum()
    peak = cum_r.cummax()
    drawdown = cum_r - peak
    max_dd_r = abs(drawdown.min()) if len(drawdown) else 0.0

    longs = (sorted_df["dir"] == "LONG").sum()
    shorts = (sorted_df["dir"] == "SHORT").sum()

    return {
        "trades": len(trades_df),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(winrate, 1),
        "total_r": round(total_r, 2),
        "avg_r": round(avg_r, 3),
        "profit_factor": round(profit_factor, 2),
        "max_dd_r": round(max_dd_r, 2),
        "long_trades": longs,
        "short_trades": shorts,
    }


def simulate(df_1m: pd.DataFrame, df_htf: pd.DataFrame, df_ltf: pd.DataFrame,
             candidates: list, stop_mode: str, symbol: str = "BTC/USDT",
             use_ai: bool = False, ai_filter: bool = False,
             max_positions: int = None) -> pd.DataFrame:
    trades = []
    active_until = []  # отслеживание открытых сделок для эмуляции лимита max_positions
    for c in candidates:
        search_path = df_1m[df_1m.index > c["confirm_time"]].head(cfg.MAX_WAIT_MIN)
        fill_time, fill_price = None, None
        fvg_mode = getattr(cfg, "FVG_ENTRY_MODE", "ce")
        ce_price = c.get("fvg_ce", (c["fvg_top"] + c["fvg_bottom"]) / 2.0)

        for t, r in search_path.iterrows():
            if fvg_mode == "ce":
                # Вход лимитным ордером на 50% FVG (Consequent Encroachment)
                if c["expected_dir"] == 1:
                    if r["low"] <= ce_price and r["high"] >= c["fvg_bottom"]:
                        fill_price = ce_price
                        fill_time = t
                        break
                else:
                    if r["high"] >= ce_price and r["low"] <= c["fvg_top"]:
                        fill_price = ce_price
                        fill_time = t
                        break
            else:
                if r["low"] <= c["fvg_top"] and r["high"] >= c["fvg_bottom"]:
                    fill_price = (min(r["high"], c["fvg_top"]) if c["expected_dir"] == 1
                                  else max(r["low"], c["fvg_bottom"]))
                    fill_time = t
                    break
        if fill_time is None:
            continue  # цена не вернулась в зону - сигнал не исполнен

        # Эмуляция риск-контроля: пропуск новых сигналов, если лимит позиций исчерпан
        if max_positions is not None and max_positions > 0:
            active_until = [exp for exp in active_until if exp > fill_time]
            if len(active_until) >= max_positions:
                continue

        buffer_pct = getattr(cfg, "STOP_BUFFER_PCT", 0.0015)
        buffer = fill_price * buffer_pct
        if stop_mode == "wick":
            stop = (c["sweep_candle_low"] - buffer if c["expected_dir"] == 1
                    else c["sweep_candle_high"] + buffer)
        else:  # "ob"
            if c["ob_candidate"] is None:
                continue
            stop = (c["ob_candidate"]["Bottom"] - buffer if c["expected_dir"] == 1
                    else c["ob_candidate"]["Top"] + buffer)

        risk = (fill_price - stop) if c["expected_dir"] == 1 else (stop - fill_price)
        if risk <= 0:
            continue

        # Институциональный фильтр-гейткипер (ATR и Min Risk):
        # Стоп ВСЕГДА ставится строго структурно за фитиль.
        # Если расстояние меньше допустимого порога, сделка бракуется (continue),
        # а не расширяется искусственно в воздухе (устранение «парадокса ножниц ATR»).
        use_atr_stop = getattr(cfg, "USE_ATR_STOP", False)
        atr_mult = getattr(cfg, "ATR_STOP_MULT", 1.5)
        atr_5m = c.get("atr_5m")
        if use_atr_stop and atr_5m and atr_5m > 0:
            min_dist = atr_5m * atr_mult
            if risk < min_dist:
                continue

        min_risk_pct = getattr(cfg, "MIN_RISK_PCT", 0.0025)
        if risk <= 0 or (risk / fill_price) < min_risk_pct:
            continue

        # Подготовка данных для ИИ-оценки
        ai_score, ai_rec, ai_reason = None, None, None
        if use_ai:
            zone_pct = (c["fvg_top"] - c["fvg_bottom"]) / c["fvg_bottom"] if c["fvg_bottom"] else 0
            eval_payload = {
                "symbol": symbol,
                "expected_dir": c["expected_dir"],
                "sweep_time": str(c["sweep_time"]),
                "confirm_time": str(c["confirm_time"]),
                "bias": c.get("bias", c["expected_dir"]),
                "d1_bias": c.get("d1_bias", 0),
                "daily_atr": c.get("daily_atr"),
                "in_killzone": True,
                "zone_pct": zone_pct,
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

            if ai_filter:
                threshold = getattr(cfg, "AI_CONFIDENCE_THRESHOLD", 7)
                if ai_rec == "SKIP" or (ai_score is not None and ai_score < threshold):
                    continue  # ИИ отфильтровал сделку с низкой вероятностью

        take_r = cfg.PARTIAL_TAKE_R
        structural_pool = c.get("structural_pool")
        use_structural = getattr(cfg, "USE_STRUCTURAL_TARGETS", False)
        min_struct_r = getattr(cfg, "MIN_STRUCTURAL_R", 1.2)

        if use_structural and structural_pool is not None and risk > 0:
            struct_dist = (structural_pool - fill_price) if c["expected_dir"] == 1 else (fill_price - structural_pool)
            struct_r = struct_dist / risk
            # Если ближайший встречный пул ликвидности ближе min_struct_r, потенциал зажат - сделка бракуется
            if struct_r < min_struct_r:
                continue
            # Синхронизация цели с пулом ликвидности BSL/SSL (потолок 2.5R)
            take_r = max(min_struct_r, min(2.5, round(struct_r, 2)))
        elif getattr(cfg, "USE_DYNAMIC_R", False) and c.get("daily_atr") and risk > 0:
            calc_r = (0.6 * c["daily_atr"]) / risk
            min_r = getattr(cfg, "DYNAMIC_R_MIN", 1.5)
            max_r = getattr(cfg, "DYNAMIC_R_MAX", 2.8)
            take_r = max(min_r, min(max_r, round(calc_r, 2)))

        target_partial = (fill_price + take_r * risk if c["expected_dir"] == 1
                          else fill_price - take_r * risk)

        use_be = getattr(cfg, "USE_BREAKEVEN", False)
        be_trigger_r = getattr(cfg, "BREAKEVEN_TRIGGER_R", 0.75)
        fee_cost = fill_price * cfg.FEE_SLIPPAGE_PCT
        be_stop = (fill_price + fee_cost if c["expected_dir"] == 1 else fill_price - fee_cost)
        be_trigger_price = (fill_price + be_trigger_r * risk if c["expected_dir"] == 1
                            else fill_price - be_trigger_r * risk)

        future = df_1m[df_1m.index > fill_time].head(cfg.MAX_HOLD_MIN)
        if len(future) == 0:
            continue

        hit_partial_time, stopped = None, False
        is_be_activated = False
        current_stop = stop

        kz_exit_mode = getattr(cfg, "KZ_EXIT_MODE", "hold")
        kz_list = getattr(cfg, "KILLZONES", []) if (getattr(cfg, "USE_KILLZONES", False) or kz_exit_mode != "hold") else []
        was_in_kz = in_killzone(fill_time, kz_list) if (kz_list and len(kz_list)) else False

        kz_exited = False
        kz_banked_r = 0.0
        kz_exit_weight = 1.0

        for t, r in future.iterrows():
            # Проверка закрытия Киллзоны (time-based exit)
            if was_in_kz and kz_exit_mode in ("partial80", "close") and not kz_exited:
                if not in_killzone(t, kz_list):
                    kz_exited = True
                    cur_price = r["close"]
                    cur_r = (cur_price - fill_price) / risk if c["expected_dir"] == 1 else (fill_price - cur_price) / risk

                    if kz_exit_mode == "close":
                        cost_R = (fill_price * cfg.FEE_SLIPPAGE_PCT) / risk
                        r_net = cur_r - cost_R
                        trade_row = dict(
                            symbol=symbol, sweep_time=c["sweep_time"],
                            dir="LONG" if c["expected_dir"] == 1 else "SHORT",
                            fill_time=fill_time, fill_price=round(fill_price, 4),
                            stop=round(stop, 4), target=round(target_partial, 4),
                            exit_time=t, exit_price=round(cur_price, 4),
                            outcome="KZ_CLOSE", R=round(r_net, 3),
                            is_asian_sweep=c.get("is_asian_sweep", False),
                            has_smt=c.get("has_smt", False)
                        )
                        if use_ai:
                            trade_row["ai_score"] = ai_score
                            trade_row["ai_rec"] = ai_rec
                            trade_row["ai_reason"] = ai_reason
                        trades.append(trade_row)
                        active_until.append(t)
                        stopped = True
                        break

                    elif kz_exit_mode == "partial80":
                        if cur_r > 0.1:
                            kz_banked_r = 0.80 * cur_r
                            kz_exit_weight = 0.20
                            is_be_activated = True
                            current_stop = be_stop

            if c["expected_dir"] == 1:
                # Триггер перевода в безубыток
                if use_be and not is_be_activated and r["high"] >= be_trigger_price:
                    is_be_activated = True
                    current_stop = max(current_stop, be_stop)

                if r["low"] <= current_stop:
                    stopped = True; break
                if r["high"] >= target_partial:
                    hit_partial_time = t; break
            else:
                if use_be and not is_be_activated and r["low"] <= be_trigger_price:
                    is_be_activated = True
                    current_stop = min(current_stop, be_stop)

                if r["high"] >= current_stop:
                    stopped = True; break
                if r["low"] <= target_partial:
                    hit_partial_time = t; break

        if stopped:
            if kz_exit_mode == "close" and kz_exited:
                continue
            if kz_banked_r > 0:
                rem_r = 0.0 if is_be_activated else -1.0
                cost_R = (fill_price * cfg.FEE_SLIPPAGE_PCT) / risk
                r_net = kz_banked_r + kz_exit_weight * rem_r - cost_R
                outcome = "KZ_P80+BE" if is_be_activated else "KZ_P80+STOP"
            elif is_be_activated:
                r_net = 0.0  # чистый безубыток с учетом покрытия комиссии
                outcome = "BE_STOP"
            else:
                r_net = -1.0 - (fill_price * cfg.FEE_SLIPPAGE_PCT) / risk
                outcome = "STOP"

            trade_row = dict(
                symbol=symbol, sweep_time=c["sweep_time"],
                dir="LONG" if c["expected_dir"] == 1 else "SHORT",
                fill_time=fill_time, fill_price=round(fill_price, 4),
                stop=round(stop, 4), target=round(target_partial, 4),
                exit_time=t, exit_price=round(current_stop, 4),
                outcome=outcome, R=round(r_net, 3),
                is_asian_sweep=c.get("is_asian_sweep", False),
                has_smt=c.get("has_smt", False)
            )
            if use_ai:
                trade_row["ai_score"] = ai_score
                trade_row["ai_rec"] = ai_rec
                trade_row["ai_reason"] = ai_reason
            trades.append(trade_row)
            active_until.append(t)
            continue

        if hit_partial_time is None:
            if kz_banked_r > 0:
                last_p = future.iloc[-1]["close"]
                rem_r = (last_p - fill_price) / risk if c["expected_dir"] == 1 else (fill_price - last_p) / risk
                cost_R = (fill_price * cfg.FEE_SLIPPAGE_PCT) / risk
                r_net = kz_banked_r + kz_exit_weight * rem_r - cost_R
                trade_row = dict(
                    symbol=symbol, sweep_time=c["sweep_time"],
                    dir="LONG" if c["expected_dir"] == 1 else "SHORT",
                    fill_time=fill_time, fill_price=round(fill_price, 4),
                    stop=round(stop, 4), target=round(target_partial, 4),
                    exit_time=future.index[-1], exit_price=round(last_p, 4),
                    outcome="KZ_P80+TIMEOUT", R=round(r_net, 3),
                    is_asian_sweep=c.get("is_asian_sweep", False),
                    has_smt=c.get("has_smt", False)
                )
                if use_ai:
                    trade_row["ai_score"] = ai_score
                    trade_row["ai_rec"] = ai_rec
                    trade_row["ai_reason"] = ai_reason
                trades.append(trade_row)
                active_until.append(future.index[-1])
            continue  # ни стоп, ни частичный тейк за время удержания

        half1_R = kz_banked_r if kz_banked_r > 0 else (cfg.PARTIAL_TAKE_SIZE * take_r)
        rem_size = kz_exit_weight if kz_banked_r > 0 else (1 - cfg.PARTIAL_TAKE_SIZE)
        remaining = df_1m[df_1m.index > hit_partial_time].head(cfg.MAX_HOLD_MIN)
        extreme = target_partial
        trail_stop = fill_price
        exit_price, exit_reason, exit_time = None, None, None
        for t, r in remaining.iterrows():
            if kz_exit_mode == "close" and was_in_kz and not in_killzone(t, kz_list):
                exit_price, exit_reason, exit_time = r["close"], "KZ_CLOSE", t; break
            if c["expected_dir"] == 1:
                extreme = max(extreme, r["high"])
                trail_stop = max(trail_stop, extreme - cfg.TRAIL_DISTANCE_R * risk)
                if r["low"] <= trail_stop:
                    exit_price, exit_reason, exit_time = trail_stop, "TRAIL", t; break
            else:
                extreme = min(extreme, r["low"])
                trail_stop = min(trail_stop, extreme + cfg.TRAIL_DISTANCE_R * risk)
                if r["high"] >= trail_stop:
                    exit_price, exit_reason, exit_time = trail_stop, "TRAIL", t; break
        if exit_price is None:
            exit_price = remaining.iloc[-1]["close"] if len(remaining) else target_partial
            exit_time = remaining.index[-1] if len(remaining) else hit_partial_time
            exit_reason = "TIMEOUT"

        half2_R = ((exit_price - fill_price) / risk if c["expected_dir"] == 1
                   else (fill_price - exit_price) / risk)
        total_R_gross = half1_R + rem_size * half2_R
        cost_R = (fill_price * cfg.FEE_SLIPPAGE_PCT) / risk
        total_R_net = total_R_gross - cost_R

        trade_row = dict(
            symbol=symbol, sweep_time=c["sweep_time"],
            dir="LONG" if c["expected_dir"] == 1 else "SHORT",
            fill_time=fill_time, fill_price=round(fill_price, 4),
            stop=round(stop, 4), target=round(target_partial, 4),
            exit_time=exit_time, exit_price=round(exit_price, 4),
            outcome=f"PARTIAL+{exit_reason}", R=round(total_R_net, 3),
            is_asian_sweep=c.get("is_asian_sweep", False),
            has_smt=c.get("has_smt", False)
        )
        if use_ai:
            trade_row["ai_score"] = ai_score
            trade_row["ai_rec"] = ai_rec
            trade_row["ai_reason"] = ai_reason
        trades.append(trade_row)
        active_until.append(exit_time)

    trades_df = pd.DataFrame(trades)
    if len(trades_df):
        trades_df = trades_df.sort_values("fill_time").reset_index(drop=True)
    return trades_df


def run_single_symbol(symbol: str, use_ai: bool = False, ai_filter: bool = False,
                      smt_df: pd.DataFrame = None, max_positions: int = None,
                      start_date = None) -> tuple[pd.DataFrame, dict]:
    """Прогон бэктеста для одного инструмента."""
    print(f"\n[{symbol}] Загрузка данных...")
    df_1m = get_data(cfg, symbol=symbol, start_date=start_date)
    if len(df_1m) == 0:
        print(f"[{symbol}] Нет данных.")
        return pd.DataFrame(), {}

    print(f"[{symbol}] Баров 1m: {len(df_1m)} | Период: {df_1m.index.min()} -> {df_1m.index.max()}")
    df_htf = resample(df_1m, cfg.HTF_RULE)
    df_ltf = resample(df_1m, cfg.LTF_RULE)

    candidates = find_candidates(df_htf, df_ltf, cfg, df_1m=df_1m, smt_df=smt_df)
    print(f"[{symbol}] Кандидатов на сделку: {len(candidates)}")

    from core_engine import simulate_grid_b
    is_moex = cfg.DATA_SOURCE in ("tbank", "tinkoff")
    res, metrics = simulate_grid_b(
        df_1m, df_htf, df_ltf, candidates,
        symbol=symbol, is_moex=is_moex,
        use_vol_filter=getattr(cfg, "USE_VOLATILITY_FILTER", True),
        min_fvg_pct=getattr(cfg, "MIN_FVG_ZONE_PCT", 0.0005),
        min_atr_5m_pct=getattr(cfg, "MIN_ATR_5M_PCT", 0.0005),
        use_trend_filter=getattr(cfg, "USE_TREND_FILTER", True),
        min_adx=getattr(cfg, "MIN_ADX_1H", 20.0),
        use_ai=use_ai, ai_filter=ai_filter,
        max_positions=max_positions
    )

    # Сохраняем индивидуальный файл сделок
    slug = get_symbol_slug(symbol)
    reports_dir = getattr(cfg, "REPORTS_DIR", "reports")
    os.makedirs(reports_dir, exist_ok=True)
    res_path = os.path.join(reports_dir, f"backtest_results_{slug}.csv")
    res.to_csv(res_path, index=False)
    if symbol == getattr(cfg, "SYMBOL", "BTC/USDT"):
        res.to_csv(os.path.join(reports_dir, "backtest_results.csv"), index=False)

    return res, metrics


def analyze_discrepancies(summary_df: pd.DataFrame):
    """
    Интеллектуальный анализ отклонений между инструментами:
    выявление нестабильности edge, переобучения на BTC или аномалий волатильности.
    """
    print("\n" + "="*80)
    print("АНАЛИЗ ОТКЛОНЕНИЙ И УСТОЙЧИВОСТИ (DISCREPANCY & OVERFITTING CHECK)")
    print("="*80)

    if len(summary_df) <= 1:
        print("Тестирование проводилось только на одном инструменте. Для проверки устойчивости добавьте другие пары.")
        return

    winrates = summary_df["winrate"].tolist()
    total_rs = summary_df["total_r"].tolist()
    symbols = summary_df["symbol"].tolist()

    wr_spread = max(winrates) - min(winrates)
    has_negative = any(r < 0 for r in total_rs)

    if has_negative:
        losing_syms = [s for s, r in zip(symbols, total_rs) if r < 0]
        print(f"ВНИМАНИЕ: На инструментах {losing_syms} получен ОТРИЦАТЕЛЬНЫЙ результат!")
        print("  Причина: Стратегия может быть переобучена под характер движений одного актива,")
        print("  либо волатильность/размах FVG на этих монетах требует индивидуальной калибровки MAX_FVG_ZONE_PCT.")
    else:
        print("ПОЛОЖИТЕЛЬНЫЙ РЕЗУЛЬТАТ: Все протестированные инструменты показали плюс по R.")

    if wr_spread > 15.0:
        print(f"РАЗБРОС ВИНРЕЙТА: Высокая вариативность winrate между парами ({wr_spread:.1f}%).")
        print("  Рекомендуется проверить частоту ложных свипов на высоковолатильных альткоинах.")
    else:
        print(f"СТАБИЛЬНОСТЬ: Винрейт распределен равномерно (разброс {wr_spread:.1f}%). Паттерн работает схоже.")

    # Проверка Long/Short баланса
    for idx, row in summary_df.iterrows():
        total = row["trades"]
        if total > 0:
            long_ratio = row["long_trades"] / total
            if long_ratio > 0.8 or long_ratio < 0.2:
                print(f"  * {row['symbol']}: Дисбаланс направлений (Long {long_ratio*100:.0f}%, Short {(1-long_ratio)*100:.0f}%). "
                      f"Возможна чувствительность к затяжному макро-тренду.")


def parse_killzone_ranges(hours_str: str, is_msk: bool = False) -> list[tuple[int, int]]:
    """
    Парсит строку диапазонов часов вида '11-15' или '8-12,14-17'.
    Если is_msk=True, переводит часы MSK -> UTC (-3 часа).
    """
    ranges = []
    for part in hours_str.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split("-")
        if len(tokens) == 2:
            s_h = int(tokens[0].strip())
            e_h = int(tokens[1].strip())
            if is_msk:
                s_h = (s_h - 3) % 24
                e_h = (e_h - 3) % 24
            ranges.append((s_h, e_h))
    return ranges


def main():
    parser = argparse.ArgumentParser(description="ICT Multi-Instrument Backtest & AI Evaluator")
    parser.add_argument("--source", type=str, default=None, choices=["ccxt", "tbank", "tinkoff", "github_csv"],
                        help="Источник данных (ccxt для крипты, tbank для акций РФ)")
    parser.add_argument("--symbols", "--tickers", dest="symbols", type=str, default=None,
                        help="Список тикеров через запятую (например, BTC/USDT,ETH/USDT или SBER,GAZP,LKOH)")
    parser.add_argument("--all", action="store_true", help="Запустить по всей корзине инструментов")
    parser.add_argument("--alts", action="store_true",
                        help="Тестировать корзину альткоинов (DOGE/USDT, ADA/USDT, XRP/USDT, BNB/USDT, SOL/USDT согласно ADR 2.C)")
    parser.add_argument("--symbol", "--ticker", dest="symbol", type=str, default=None, help="Одиночный инструмент/тикер")
    parser.add_argument("--exclude", type=str, default=None,
                        help="Список тикеров для исключения через запятую (например, GAZP,T)")

    # Горизонт тестирования
    parser.add_argument("--days", type=int, default=None,
                        help="Глубина теста в днях (например, --days 365 для годового теста)")
    parser.add_argument("--start-date", type=str, default=None,
                        help="Дата начала бэктеста в формате ГГГГ-ММ-ДД (например, 2025-09-12)")

    # Фильтр по направлению сделок
    parser.add_argument("--direction", "--dir", dest="direction", type=str, choices=["all", "long", "short"], default="all",
                        help="Направление сделок: all (по умолчанию), long (только покупки), short (только продажи)")
    parser.add_argument("--long-only", action="store_true", help="Торговать только в LONG")
    parser.add_argument("--short-only", action="store_true", help="Торговать только в SHORT")

    # Киллзоны (Killzones)
    parser.add_argument("--killzones", action="store_true", help="Торговать строго внутри Killzones")
    parser.add_argument("--kz-start", type=int, default=None, help="Час начала киллзоны (0-23)")
    parser.add_argument("--kz-end", type=int, default=None, help="Час окончания киллзоны (0-23)")
    parser.add_argument("--kz-hours", type=str, default=None, help="Диапазоны часов киллзон, например '8-12' или '11-15,16-18'")
    parser.add_argument("--kz-msk", type=str, default=None, help="Диапазоны киллзон по Москве (MSK = UTC+3), например '11-15'")
    parser.add_argument("--msk", action="store_true", help="Интерпретировать часы киллзоны (--kz-start/end, --kz-hours) как Московское время MSK (UTC+3)")

    # Дополнительные ICT фильтры и ИИ
    parser.add_argument("--smt", action="store_true", help="Требовать подтверждения SMT-дивергенцией (BTC vs ETH)")
    parser.add_argument("--asian-range", action="store_true", help="Требовать свип уровней Азиатской сессии")
    parser.add_argument("--ai", action="store_true", help="Включить ИИ-оценку сетапов")
    parser.add_argument("--ai-filter", action="store_true", help="Отсеивать сетапы с низкой оценкой ИИ")

    # Тюнинг параметров стратегии и риск-менеджмента
    parser.add_argument("--partial-r", type=float, default=None, help="Уровень первого частичного тейка в R (по умолчанию 1.5)")
    parser.add_argument("--partial-size", type=float, default=None, help="Доля позиции для частичного тейка (по умолчанию 0.5)")
    parser.add_argument("--trail-r", type=float, default=None, help="Дистанция трейлинг-стопа в R (по умолчанию 0.8)")
    parser.add_argument("--be", "--breakeven", dest="breakeven", action="store_true",
                        help="Переводить стоп в безубыток при достижении порога прибыли в R")
    parser.add_argument("--be-r", "--be-trigger-r", dest="be_r", type=float, default=None,
                        help="Порог в R для перевода стопа в безубыток (по умолчанию 1.0R)")
    parser.add_argument("--max-fvg", type=float, default=None, help="Макс. допустимый размер FVG в долях цены (по умолчанию 0.006)")
    parser.add_argument("--min-risk", type=float, default=None, help="Мин. допустимый риск/стоп в долях цены (по умолчанию 0.002)")
    parser.add_argument("--dynamic-r", action="store_true", help="Динамический расчет тейка на основе волатильности (Daily ATR)")
    parser.add_argument("--d1-filter", action="store_true", help="Блокировать контртрендовые сделки по дневному тренду D1")
    parser.add_argument("--fvg-mode", type=str, choices=["ce", "edge"], default=None, help="Режим входа в FVG: ce (50%% Consequent Encroachment) или edge (край)")
    parser.add_argument("--atr-stop", dest="atr_stop", action="store_true", default=None,
                        help="Адаптивный минимальный стоп от 5m ATR (защита от выбивания шумом)")
    parser.add_argument("--no-atr-stop", dest="atr_stop", action="store_false",
                        help="Отключить адаптивный ATR-стоп")
    parser.add_argument("--atr-mult", type=float, default=None,
                        help="Множитель 5m ATR для минимального стопа (по умолчанию 1.5)")
    parser.add_argument("--structural-targets", "--struct-tp", dest="structural_targets", action="store_true",
                        help="Синхронизация тейков по сетке Фибоначчи со свингами ликвидности (BSL/SSL)")
    parser.add_argument("--min-struct-r", type=float, default=None,
                        help="Минимальное R до встречного пула ликвидности (по умолчанию 1.2)")
    parser.add_argument("--kz-exit", "--kz-exit-mode", dest="kz_exit", type=str, choices=["hold", "partial80", "close"], default=None,
                        help="Действие при закрытии Киллзоны: hold (тянуть), partial80 (фикс 80%% в плюс), close (100%% выход)")
    parser.add_argument("--vol-filter", dest="vol_filter", action="store_true", default=None,
                        help="Фильтр минимальной волатильности (5m ATR) для отсева мертвого рынка")
    parser.add_argument("--no-vol-filter", dest="vol_filter", action="store_false",
                        help="Отключить фильтр волатильности")
    parser.add_argument("--min-fvg", type=float, default=None,
                        help="Минимальная ширина FVG в долях цены (например 0.0015 = 0.15%%)")
    parser.add_argument("--min-atr", type=float, default=None,
                        help="Минимальный 5m ATR в долях цены (например 0.0015 = 0.15%%)")
    parser.add_argument("--model", type=str, default=None, help="Модель Gemini для оценки (например gemini-3.6-flash, gemini-3.8-flash)")
    parser.add_argument("--max-pos", type=int, default=None, help="Макс. количество одновременно открытых позиций")
    parser.add_argument("--tag", type=str, default=None, help="Суффикс для имени файла сводки (например alts_1y -> backtest_summary_alts_1y.csv)")

    args = parser.parse_args()

    if args.source:
        cfg.DATA_SOURCE = args.source

    is_tbank = cfg.DATA_SOURCE in ("tbank", "tinkoff")

    # Расчет горизонта бэктеста (--days / --start-date)
    start_date = None
    if args.days is not None:
        start_date = (pd.Timestamp.now() - pd.Timedelta(days=args.days)).floor("min")
    elif args.start_date is not None:
        start_date = pd.Timestamp(args.start_date)

    if start_date is not None:
        cfg.START_DATE = start_date.strftime("%Y-%m-%d")

    # Применение фильтра направления
    if args.long_only:
        cfg.DIRECTION_FILTER = "long"
    elif args.short_only:
        cfg.DIRECTION_FILTER = "short"
    elif args.direction:
        cfg.DIRECTION_FILTER = args.direction.lower()

    # Настройка киллзон
    custom_kz = False
    if args.kz_msk:
        cfg.KILLZONES = parse_killzone_ranges(args.kz_msk, is_msk=True)
        cfg.USE_KILLZONES = True
        custom_kz = True
    elif args.kz_hours:
        cfg.KILLZONES = parse_killzone_ranges(args.kz_hours, is_msk=args.msk)
        cfg.USE_KILLZONES = True
        custom_kz = True
    elif args.kz_start is not None and args.kz_end is not None:
        s_h, e_h = args.kz_start, args.kz_end
        if args.msk:
            s_h = (s_h - 3) % 24
            e_h = (e_h - 3) % 24
        cfg.KILLZONES = [(s_h, e_h)]
        cfg.USE_KILLZONES = True
        custom_kz = True
    elif args.killzones:
        cfg.USE_KILLZONES = True
        if is_tbank and not custom_kz:
            cfg.KILLZONES = getattr(cfg, "MOEX_KILLZONES", [(8, 12)])

    if args.smt:
        cfg.USE_SMT_FILTER = True
    if args.asian_range:
        cfg.USE_ASIAN_RANGE_FILTER = True

    # Тюнинг параметров стратегии
    if args.partial_r is not None:
        cfg.PARTIAL_TAKE_R = args.partial_r
    if args.partial_size is not None:
        cfg.PARTIAL_TAKE_SIZE = args.partial_size
    if args.trail_r is not None:
        cfg.TRAIL_DISTANCE_R = args.trail_r
    if args.breakeven:
        cfg.USE_BREAKEVEN = True
    if args.be_r is not None:
        cfg.BREAKEVEN_TRIGGER_R = args.be_r
        cfg.USE_BREAKEVEN = True
    if args.max_fvg is not None:
        cfg.MAX_FVG_ZONE_PCT = args.max_fvg
    if args.min_risk is not None:
        cfg.MIN_RISK_PCT = args.min_risk

    if args.dynamic_r:
        cfg.USE_DYNAMIC_R = True
    if args.vol_filter is not None:
        cfg.USE_VOLATILITY_FILTER = args.vol_filter
    if args.min_fvg is not None:
        cfg.MIN_FVG_ZONE_PCT = args.min_fvg
    if args.min_atr is not None:
        cfg.MIN_ATR_5M_PCT = args.min_atr
    if args.d1_filter:
        cfg.USE_HTF_D1_FILTER = True
    if args.fvg_mode:
        cfg.FVG_ENTRY_MODE = args.fvg_mode
    if args.atr_stop is not None:
        cfg.USE_ATR_STOP = args.atr_stop
    if args.atr_mult is not None:
        cfg.ATR_STOP_MULT = args.atr_mult
    if args.structural_targets:
        cfg.USE_STRUCTURAL_TARGETS = True
    if args.min_struct_r is not None:
        cfg.MIN_STRUCTURAL_R = args.min_struct_r
    if args.kz_exit:
        cfg.KZ_EXIT_MODE = args.kz_exit
    if args.model:
        cfg.GEMINI_MODEL = args.model

    use_ai = args.ai or getattr(cfg, "ENABLE_AI_EVALUATION", False) or args.ai_filter
    ai_filter = args.ai_filter

    # Определение списка инструментов
    ALTS_BASKET = getattr(cfg, "ALTS_SYMBOLS", ["BNB/USDT", "SOL/USDT", "DOGE/USDT", "ADA/USDT"])

    if args.symbols:
        target_symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.alts:
        target_symbols = list(ALTS_BASKET)
    elif args.all:
        if is_tbank:
            target_symbols = getattr(cfg, "TBANK_TICKERS", ["SBER", "GAZP", "LKOH", "ROSN", "YDEX"])
        else:
            target_symbols = getattr(cfg, "SYMBOLS", [getattr(cfg, "SYMBOL", "BTC/USDT")])
    elif args.symbol:
        target_symbols = [args.symbol.strip()]
    else:
        if is_tbank:
            target_symbols = getattr(cfg, "TBANK_TICKERS", ["SBER", "GAZP", "LKOH", "ROSN", "YDEX"])
        else:
            target_symbols = getattr(cfg, "SYMBOLS", [getattr(cfg, "SYMBOL", "BTC/USDT")])

    # Исключение инструментов
    if args.exclude:
        excludes = [s.strip().upper() for s in args.exclude.split(",") if s.strip()]
        target_symbols = [s for s in target_symbols if s.upper() not in excludes]

    # Прекалькуляция SMT сигналов между BTC и ETH (только для крипты)
    smt_df = None
    if not is_tbank and getattr(cfg, "USE_SMT_FILTER", False):
        try:
            from ict_advanced import compute_smt_signals
            df_btc = get_data(cfg, symbol="BTC/USDT", use_cache=True, start_date=start_date)
            df_eth = get_data(cfg, symbol="ETH/USDT", use_cache=True, start_date=start_date)
            if len(df_btc) and len(df_eth):
                df_btc_ltf = resample(df_btc, cfg.LTF_RULE)
                df_eth_ltf = resample(df_eth, cfg.LTF_RULE)
                smt_df = compute_smt_signals(df_btc_ltf, df_eth_ltf, lookback=getattr(cfg, "SMT_LOOKBACK_BARS", 12))
        except Exception as e:
            smt_df = None

    # Формирование описания киллзоны
    if cfg.USE_KILLZONES:
        kz_desc_utc = ", ".join(f"{a:02d}:00-{b:02d}:00 UTC" for a, b in cfg.KILLZONES)
        kz_desc_msk = ", ".join(f"{(a+3)%24:02d}:00-{(b+3)%24:02d}:00 MSK" for a, b in cfg.KILLZONES)
        kz_info = f"ВКЛ [{kz_desc_utc} / {kz_desc_msk}]"
    else:
        kz_info = "ВЫКЛ (Круглосуточно)"

    dir_info = getattr(cfg, "DIRECTION_FILTER", "all").upper()
    if dir_info == "ALL":
        dir_str = "LONG + SHORT"
    elif dir_info == "LONG":
        dir_str = "ТОЛЬКО LONG"
    else:
        dir_str = "ТОЛЬКО SHORT"

    period_str = f"{cfg.START_DATE} -> сейчас"
    if args.days is not None:
        period_str += f" ({args.days} дн.)"

    print("="*80)
    print(f"ICT BACKTEST RUNNER | Источник: {cfg.DATA_SOURCE} | Инструментов: {len(target_symbols)}")
    print(f"Инструменты: {', '.join(target_symbols)}")
    if args.exclude:
        print(f"Исключены:   {args.exclude}")
    print(f"Период:      {period_str}")
    print(f"Направление: {dir_str}")
    print(f"Киллзоны:    {kz_info}")
    vol_str = f"ВКЛ (min ATR={getattr(cfg, 'MIN_ATR_5M_PCT', 0.0015)*100:.2f}%)" if getattr(cfg, 'USE_VOLATILITY_FILTER', False) else "ВЫКЛ"
    print(f"Фильтры:     SMT={getattr(cfg, 'USE_SMT_FILTER', False)} | AsianRange={getattr(cfg, 'USE_ASIAN_RANGE_FILTER', False)} | Volatility={vol_str}")
    be_str = f"ВКЛ ({cfg.BREAKEVEN_TRIGGER_R:.1f}R)" if getattr(cfg, "USE_BREAKEVEN", False) else "ВЫКЛ"
    min_fvg_str = f"{getattr(cfg, 'MIN_FVG_ZONE_PCT', 0.0015)*100:.2f}%"
    print(f"Параметры:   TP={cfg.PARTIAL_TAKE_R}R ({cfg.PARTIAL_TAKE_SIZE*100:.0f}%) | Trail={cfg.TRAIL_DISTANCE_R}R | BE={be_str} | FVG=[{min_fvg_str} - {cfg.MAX_FVG_ZONE_PCT*100:.2f}%] | Min Stop={cfg.MIN_RISK_PCT*100:.2f}%")
    print(f"ИИ-оценка:   {'ВКЛЮЧЕНА (фильтрация: ' + str(ai_filter) + ')' if use_ai else 'ВЫКЛЮЧЕНА'}")
    print("="*80)

    all_trades = []
    summary_rows = []

    for sym in target_symbols:
        trades_df, metrics = run_single_symbol(sym, use_ai=use_ai, ai_filter=ai_filter, smt_df=smt_df,
                                               max_positions=args.max_pos, start_date=start_date)
        if len(trades_df):
            all_trades.append(trades_df)
        if metrics:
            summary_rows.append(metrics)

    if not summary_rows:
        print("\nНет данных или сделок ни по одному инструменту.")
        return

    summary_df = pd.DataFrame(summary_rows)

    # Итог по портфелю (Portfolio Total)
    if len(all_trades):
        combined_df = pd.concat(all_trades, ignore_index=True)
        port_metrics = compute_metrics(combined_df)
        port_metrics["symbol"] = "PORTFOLIO TOTAL"
        summary_rows.append(port_metrics)
        summary_df_full = pd.DataFrame(summary_rows)
    else:
        summary_df_full = summary_df

    # Печать сводной таблицы
    print("\n" + "="*80)
    print("СВОДНАЯ СРАВНИТЕЛЬНАЯ ТАБЛИЦА ПО ИНСТРУМЕНТАМ")
    print("="*80)
    fmt_header = f"{'Symbol':<16} | {'Trades':<7} | {'Winrate':<8} | {'Total R':<9} | {'Max DD':<8} | {'P.Factor':<8} | {'Avg R':<7}"
    print(fmt_header)
    print("-" * len(fmt_header))

    for _, r in summary_df_full.iterrows():
        is_total = r["symbol"] == "PORTFOLIO TOTAL"
        prefix = "=" if is_total else " "
        line = (f"{prefix}{r['symbol']:<15} | {r['trades']:<7} | {r['winrate']:>5.1f}%  | "
                f"{r['total_r']:>+7.2f}R | {r['max_dd_r']:>6.2f}R | {r['profit_factor']:>8.2f} | {r['avg_r']:>+6.3f}R")
        if is_total:
            print("-" * len(fmt_header))
        print(line)

    # Сохранение сводки
    reports_dir = getattr(cfg, "REPORTS_DIR", "reports")
    os.makedirs(reports_dir, exist_ok=True)
    summary_filename = f"backtest_summary_{args.tag}.csv" if args.tag else "backtest_summary.csv"
    summary_path = os.path.join(reports_dir, summary_filename)
    summary_df_full.to_csv(summary_path, index=False)
    print(f"\nСводка сохранена в: {summary_path}")

    # Анализ отклонений
    analyze_discrepancies(summary_df)


if __name__ == "__main__":
    main()
