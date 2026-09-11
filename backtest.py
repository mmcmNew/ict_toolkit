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
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os
import argparse
import pandas as pd
import numpy as np
import config as cfg
from data_sources import get_data, get_symbol_slug
from strategy import resample, find_candidates
from ai_evaluator import evaluate_setup


def compute_metrics(trades_df: pd.DataFrame) -> dict:
    """Расчёт детальных квант-метрик по списку сделок."""
    if len(trades_df) == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "winrate": 0.0,
            "total_r": 0.0, "avg_r": 0.0, "profit_factor": 0.0,
            "max_dd_r": 0.0, "long_trades": 0, "short_trades": 0
        }

    wins = trades_df[trades_df["R"] > 0]
    losses = trades_df[trades_df["R"] <= 0]
    winrate = (len(wins) / len(trades_df)) * 100.0
    total_r = trades_df["R"].sum()
    avg_r = trades_df["R"].mean()

    gross_profit = wins["R"].sum()
    gross_loss = abs(losses["R"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.9 if gross_profit > 0 else 0.0)

    # Расчёт просадки в R (Max Drawdown)
    cum_r = trades_df["R"].cumsum()
    peak = cum_r.cummax()
    drawdown = cum_r - peak
    max_dd_r = abs(drawdown.min()) if len(drawdown) else 0.0

    longs = (trades_df["dir"] == "LONG").sum()
    shorts = (trades_df["dir"] == "SHORT").sum()

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
             use_ai: bool = False, ai_filter: bool = False) -> pd.DataFrame:
    trades = []
    for c in candidates:
        search_path = df_1m[df_1m.index > c["confirm_time"]].head(cfg.MAX_WAIT_MIN)
        fill_time, fill_price = None, None
        for t, r in search_path.iterrows():
            if r["low"] <= c["fvg_top"] and r["high"] >= c["fvg_bottom"]:
                fill_price = (min(r["high"], c["fvg_top"]) if c["expected_dir"] == 1
                              else max(r["low"], c["fvg_bottom"]))
                fill_time = t
                break
        if fill_time is None:
            continue  # цена не вернулась в зону - сигнал не исполнен

        buffer = fill_price * 0.0008
        if stop_mode == "wick":
            stop = (c["sweep_candle_low"] - buffer if c["expected_dir"] == 1
                    else c["sweep_candle_high"] + buffer)
        else:  # "ob"
            if c["ob_candidate"] is None:
                continue
            stop = (c["ob_candidate"]["Bottom"] - buffer if c["expected_dir"] == 1
                    else c["ob_candidate"]["Top"] + buffer)

        risk = (fill_price - stop) if c["expected_dir"] == 1 else (stop - fill_price)
        if risk <= 0 or risk / fill_price < cfg.MIN_RISK_PCT:
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
                "in_killzone": True,
                "zone_pct": zone_pct,
                "fvg_top": c["fvg_top"],
                "fvg_bottom": c["fvg_bottom"],
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

        target_partial = (fill_price + cfg.PARTIAL_TAKE_R * risk if c["expected_dir"] == 1
                          else fill_price - cfg.PARTIAL_TAKE_R * risk)

        future = df_1m[df_1m.index > fill_time].head(cfg.MAX_HOLD_MIN)
        if len(future) == 0:
            continue

        hit_partial_time, stopped = None, False
        for t, r in future.iterrows():
            if c["expected_dir"] == 1:
                if r["low"] <= stop:
                    stopped = True; break
                if r["high"] >= target_partial:
                    hit_partial_time = t; break
            else:
                if r["high"] >= stop:
                    stopped = True; break
                if r["low"] <= target_partial:
                    hit_partial_time = t; break

        if stopped:
            r_net = -1.0 - (fill_price * cfg.FEE_SLIPPAGE_PCT) / risk
            trade_row = dict(
                symbol=symbol, sweep_time=c["sweep_time"],
                dir="LONG" if c["expected_dir"] == 1 else "SHORT",
                fill_time=fill_time, fill_price=round(fill_price, 4),
                stop=round(stop, 4), target=round(target_partial, 4),
                exit_time=t, exit_price=round(stop, 4),
                outcome="STOP", R=round(r_net, 3)
            )
            if use_ai:
                trade_row["ai_score"] = ai_score
                trade_row["ai_rec"] = ai_rec
                trade_row["ai_reason"] = ai_reason
            trades.append(trade_row)
            continue

        if hit_partial_time is None:
            continue  # ни стоп, ни частичный тейк за время удержания

        half1_R = cfg.PARTIAL_TAKE_SIZE * cfg.PARTIAL_TAKE_R
        remaining = df_1m[df_1m.index > hit_partial_time].head(cfg.MAX_HOLD_MIN)
        extreme = target_partial
        trail_stop = fill_price
        exit_price, exit_reason, exit_time = None, None, None
        for t, r in remaining.iterrows():
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
        total_R_gross = half1_R + (1 - cfg.PARTIAL_TAKE_SIZE) * half2_R
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

    return pd.DataFrame(trades)


def run_single_symbol(symbol: str, use_ai: bool = False, ai_filter: bool = False, smt_df: pd.DataFrame = None) -> tuple[pd.DataFrame, dict]:
    """Прогон бэктеста для одного инструмента."""
    print(f"\n[{symbol}] Загрузка данных...")
    df_1m = get_data(cfg, symbol=symbol)
    if len(df_1m) == 0:
        print(f"[{symbol}] Нет данных.")
        return pd.DataFrame(), {}

    print(f"[{symbol}] Баров 1m: {len(df_1m)} | Период: {df_1m.index.min()} -> {df_1m.index.max()}")
    df_htf = resample(df_1m, cfg.HTF_RULE)
    df_ltf = resample(df_1m, cfg.LTF_RULE)

    candidates = find_candidates(df_htf, df_ltf, cfg, df_1m=df_1m, smt_df=smt_df)
    print(f"[{symbol}] Кандидатов на сделку: {len(candidates)}")

    res = simulate(df_1m, df_htf, df_ltf, candidates, stop_mode=cfg.STOP_MODE,
                   symbol=symbol, use_ai=use_ai, ai_filter=ai_filter)

    # Сохраняем индивидуальный файл сделок
    slug = get_symbol_slug(symbol)
    res_path = f"backtest_results_{slug}.csv"
    res.to_csv(res_path, index=False)
    if symbol == getattr(cfg, "SYMBOL", "BTC/USDT"):
        res.to_csv("backtest_results.csv", index=False)

    metrics = compute_metrics(res)
    metrics["symbol"] = symbol
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


def main():
    parser = argparse.ArgumentParser(description="ICT Multi-Instrument Backtest & AI Evaluator")
    parser.add_argument("--source", type=str, default=None, choices=["ccxt", "tbank", "tinkoff", "github_csv"],
                        help="Источник данных (ccxt для крипты, tbank для акций РФ)")
    parser.add_argument("--symbols", "--tickers", dest="symbols", type=str, default=None,
                        help="Список тикеров через запятую (например, BTC/USDT,ETH/USDT или SBER,GAZP,LKOH)")
    parser.add_argument("--all", action="store_true", help="Запустить по всей корзине инструментов")
    parser.add_argument("--symbol", "--ticker", dest="symbol", type=str, default=None, help="Одиночный инструмент/тикер")
    parser.add_argument("--ai", action="store_true", help="Включить ИИ-оценку сетапов")
    parser.add_argument("--ai-filter", action="store_true", help="Отсеивать сетапы с низкой оценкой ИИ")
    parser.add_argument("--killzones", action="store_true", help="Торговать строго внутри Killzones (London/NY)")
    parser.add_argument("--smt", action="store_true", help="Требовать подтверждения SMT-дивергенцией (BTC vs ETH)")
    parser.add_argument("--asian-range", action="store_true", help="Требовать свип уровней Азиатской сессии")

    args = parser.parse_args()

    if args.source:
        cfg.DATA_SOURCE = args.source

    if args.killzones:
        cfg.USE_KILLZONES = True
    if args.smt:
        cfg.USE_SMT_FILTER = True
    if args.asian_range:
        cfg.USE_ASIAN_RANGE_FILTER = True

    use_ai = args.ai or getattr(cfg, "ENABLE_AI_EVALUATION", False) or args.ai_filter
    ai_filter = args.ai_filter

    is_tbank = cfg.DATA_SOURCE in ("tbank", "tinkoff")

    # Определение списка инструментов
    if args.symbols:
        target_symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.all:
        if is_tbank:
            target_symbols = getattr(cfg, "TBANK_TICKERS", ["SBER", "GAZP", "LKOH", "ROSN", "YDEX", "T"])
        else:
            target_symbols = getattr(cfg, "SYMBOLS", [getattr(cfg, "SYMBOL", "BTC/USDT")])
    elif args.symbol:
        target_symbols = [args.symbol.strip()]
    else:
        if is_tbank:
            target_symbols = getattr(cfg, "TBANK_TICKERS", ["SBER", "GAZP", "LKOH", "ROSN", "YDEX", "T"])
        else:
            target_symbols = getattr(cfg, "SYMBOLS", [getattr(cfg, "SYMBOL", "BTC/USDT")])

    # Прекалькуляция SMT сигналов между BTC и ETH (только для крипты)
    smt_df = None
    if not is_tbank and getattr(cfg, "USE_SMT_FILTER", False):
        try:
            from ict_advanced import compute_smt_signals
            df_btc = get_data(cfg, symbol="BTC/USDT", use_cache=True)
            df_eth = get_data(cfg, symbol="ETH/USDT", use_cache=True)
            if len(df_btc) and len(df_eth):
                df_btc_ltf = resample(df_btc, cfg.LTF_RULE)
                df_eth_ltf = resample(df_eth, cfg.LTF_RULE)
                smt_df = compute_smt_signals(df_btc_ltf, df_eth_ltf, lookback=getattr(cfg, "SMT_LOOKBACK_BARS", 12))
        except Exception as e:
            smt_df = None

    print("="*80)
    print(f"ICT BACKTEST RUNNER | Источник: {cfg.DATA_SOURCE} | Инструментов: {len(target_symbols)}")
    print(f"Инструменты: {', '.join(target_symbols)}")
    print(f"Фильтры: Killzones={cfg.USE_KILLZONES} | SMT={getattr(cfg, 'USE_SMT_FILTER', False)} | AsianRange={getattr(cfg, 'USE_ASIAN_RANGE_FILTER', False)}")
    print(f"ИИ-оценка: {'ВКЛЮЧЕНА (фильтрация: ' + str(ai_filter) + ')' if use_ai else 'ВЫКЛЮЧЕНА'}")
    print("="*80)

    all_trades = []
    summary_rows = []

    for sym in target_symbols:
        trades_df, metrics = run_single_symbol(sym, use_ai=use_ai, ai_filter=ai_filter, smt_df=smt_df)
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
    summary_df_full.to_csv("backtest_summary.csv", index=False)
    print("\nСводка сохранена в: backtest_summary.csv")

    # Анализ отклонений
    analyze_discrepancies(summary_df)


if __name__ == "__main__":
    main()
