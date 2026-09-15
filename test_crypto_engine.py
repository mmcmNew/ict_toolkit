"""
test_crypto_engine.py - Квантовый скрипт повторного тестирования стратегии по криптовалюте
через канонический институциональный движок core_engine.py.
"""
import os
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import time
import copy
import pandas as pd
import numpy as np
from datetime import datetime

import config as cfg
from data_sources import get_data, get_symbol_slug
from strategy import resample, find_candidates
from core_engine import simulate_grid_b, compute_metrics

# Список целевых инструментов (основные + корзина альтов)
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "ADA/USDT",
    "DOGE/USDT",
    "XRP/USDT",
]

PROFILES = [
    {
        "name": "1. Baseline (24/7, VolFilter ON, Trend OFF)",
        "use_asian": False,
        "use_kz": False,
        "use_trend_filter": False,
        "entry_mode": "ce",
        "symbols": SYMBOLS,
        "min_adx": 20.0,
        "min_fvg": 0.0005,
        "min_atr": 0.0005,
    },
    {
        "name": "2. Asian Range Judas (Asian ON, Trend OFF)",
        "use_asian": True,
        "use_kz": False,
        "use_trend_filter": False,
        "entry_mode": "ce",
        "symbols": SYMBOLS,
        "min_adx": 20.0,
        "min_fvg": 0.0005,
        "min_atr": 0.0005,
    },
    {
        "name": "3. Trend Filter ADX (24/7, ADX>=20, Disp>=0.45, EMA ON)",
        "use_asian": False,
        "use_kz": False,
        "use_trend_filter": True,
        "entry_mode": "ce",
        "symbols": SYMBOLS,
        "min_adx": 20.0,
        "min_fvg": 0.0005,
        "min_atr": 0.0005,
    },
    {
        "name": "4. Vetted Basket (SOL, XRP, BTC, ETH) + True Stop + Trend Filter",
        "use_asian": False,
        "use_kz": False,
        "use_trend_filter": True,
        "entry_mode": "ce",
        "symbols": ["SOL/USDT", "XRP/USDT", "BTC/USDT", "ETH/USDT"],
        "min_adx": 20.0,
        "min_fvg": 0.0005,
        "min_atr": 0.0005,
    },
    {
        "name": "5. Vetted Basket (SOL, XRP, BTC, ETH) + Edge Entry + Grid A (1.2R/2.0R)",
        "use_asian": False,
        "use_kz": False,
        "use_trend_filter": True,
        "entry_mode": "edge",
        "symbols": ["SOL/USDT", "XRP/USDT", "BTC/USDT", "ETH/USDT"],
        "min_adx": 20.0,
        "min_fvg": 0.0005,
        "min_atr": 0.0005,
    },
]


def run_benchmark(target_symbols=SYMBOLS, start_date="2026-06-11"):
    print("=" * 90)
    print(f"КВАНТОВОЕ ТЕСТИРОВАНИЕ КРИПТОВАЛЮТНОЙ СТРАТЕГИИ ЧЕРЕЗ CORE_ENGINE.PY")
    print(f"Период: {start_date} -> сейчас | Инструментов: {len(target_symbols)}")
    print(f"Инструменты: {', '.join(target_symbols)}")
    print("=" * 90)

    # 1. Предзагрузка и ресемплинг данных для всех инструментов
    data_store = {}
    for sym in target_symbols:
        print(f"Загрузка данных для {sym}...")
        df_1m = get_data(cfg, symbol=sym, start_date=start_date, use_cache=True)
        if len(df_1m) < 1000:
            print(f"  [!] Мало данных для {sym} ({len(df_1m)} баров). Пропуск.")
            continue
        df_htf = resample(df_1m, cfg.HTF_RULE)
        df_ltf = resample(df_1m, cfg.LTF_RULE)
        print(f"  -> {sym}: {len(df_1m):,} 1m баров | 1H: {len(df_htf)} | 5m: {len(df_ltf)}")
        data_store[sym] = {
            "df_1m": df_1m,
            "df_htf": df_htf,
            "df_ltf": df_ltf,
        }

    all_profile_results = []

    # 2. Прогон по каждому институциональному профилю
    for prof in PROFILES:
        prof_name = prof["name"]
        print("\n" + "#" * 90)
        print(f"ЗАПУСК ПРОФИЛЯ: {prof_name}")
        print("#" * 90)

        # Конфигурируем параметры на модуле cfg
        cfg.USE_ASIAN_RANGE_FILTER = prof["use_asian"]
        cfg.USE_KILLZONES = prof["use_kz"]
        cfg.USE_VOLATILITY_FILTER = True
        cfg.MIN_FVG_ZONE_PCT = prof["min_fvg"]
        cfg.MIN_ATR_5M_PCT = prof["min_atr"]
        cfg.FVG_ENTRY_MODE = prof.get("entry_mode", "ce")
        cfg.PARTIAL_TAKE_R = 1.2 if "Grid A" in prof_name else 1.0
        cfg.RUNNER_TAKE_R = 2.0 if "Grid A" in prof_name else 1.618

        prof_trades = []
        symbol_summaries = []
        prof_symbols = prof.get("symbols", SYMBOLS)

        for sym in prof_symbols:
            if sym not in data_store:
                continue
            d = data_store[sym]
            df_1m = d["df_1m"]
            df_htf = d["df_htf"]
            df_ltf = d["df_ltf"]

            # Поиск кандидатов под текущий профиль
            cands = find_candidates(df_htf, df_ltf, cfg, df_1m=df_1m)

            # Симуляция через канонический Grid B
            trades_df, metrics = simulate_grid_b(
                df_1m, df_htf, df_ltf, cands,
                symbol=sym,
                is_moex=False,
                use_vol_filter=True,
                min_fvg_pct=prof["min_fvg"],
                min_atr_5m_pct=prof["min_atr"],
                use_trend_filter=prof["use_trend_filter"],
                min_adx=prof["min_adx"],
            )

            metrics["profile"] = prof_name
            symbol_summaries.append(metrics)
            if len(trades_df):
                trades_df["profile"] = prof_name
                prof_trades.append(trades_df)

        # Сводка по профилю
        prof_df = pd.DataFrame(symbol_summaries)
        combined_trades = pd.concat(prof_trades, ignore_index=True) if prof_trades else pd.DataFrame()
        port_metrics = compute_metrics(combined_trades)
        port_metrics["symbol"] = "== PORTFOLIO =="
        port_metrics["profile"] = prof_name

        print(f"\nРезультаты профиля [{prof_name}]:")
        print(f"{'Symbol':<15} | {'Trades':<7} | {'Winrate':<8} | {'Total R':<10} | {'Max DD':<9} | {'PF':<6} | {'Avg R':<8} | {'Rec Ratio':<9}")
        print("-" * 88)
        for _, row in prof_df.iterrows():
            print(f"{row['symbol']:<15} | {row['trades']:<7} | {row['winrate']:>6.1f}% | {row['total_r']:>8.2f}R | {row['max_dd_r']:>7.2f}R | {row['profit_factor']:>5.2f} | {row['avg_r']:>7.3f}R | {row['recovery_ratio']:>8.2f}")
        print("-" * 88)
        print(f"{port_metrics['symbol']:<15} | {port_metrics['trades']:<7} | {port_metrics['winrate']:>6.1f}% | {port_metrics['total_r']:>8.2f}R | {port_metrics['max_dd_r']:>7.2f}R | {port_metrics['profit_factor']:>5.2f} | {port_metrics['avg_r']:>7.3f}R | {port_metrics['recovery_ratio']:>8.2f}")

        symbol_summaries.append(port_metrics)
        all_profile_results.extend(symbol_summaries)

    # 3. Экспорт общей сравнительной таблицы
    out_df = pd.DataFrame(all_profile_results)
    os.makedirs(cfg.REPORTS_DIR, exist_ok=True)
    out_path = os.path.join(cfg.REPORTS_DIR, "crypto_engine_benchmark.csv")
    out_df.to_csv(out_path, index=False)
    print("\n" + "=" * 90)
    print(f"Все результаты тестирования успешно сохранены в: {out_path}")
    print("=" * 90)

    # 4. Сравнительная сводка по портфелям профилей
    portfolios = out_df[out_df["symbol"] == "== PORTFOLIO =="]
    print("\nИТОГОВОЕ СРАВНЕНИЕ ПРОФИЛЕЙ (ПОРТФЕЛЬ КРИПТЫ):")
    print(f"{'Профиль':<52} | {'Сделок':<6} | {'Винрейт':<8} | {'Суммарный R':<12} | {'PF':<6} | {'Max DD':<8} | {'Rec Ratio':<9}")
    print("-" * 110)
    for _, r in portfolios.iterrows():
        print(f"{r['profile']:<52} | {r['trades']:<6} | {r['winrate']:>6.1f}% | {r['total_r']:>10.2f}R | {r['profit_factor']:>5.2f} | {r['max_dd_r']:>6.2f}R | {r['recovery_ratio']:>8.2f}")
    print("=" * 110)


if __name__ == "__main__":
    run_benchmark()
