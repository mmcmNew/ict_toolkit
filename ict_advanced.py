"""
Продвинутые концепции ICT (Smart Money Concepts):
1. Отслеживание диапазонов и ликвидности Азиатской сессии (Asian Range High/Low).
2. Детектор межинструментальной дивергенции SMT (Smart Money Tool) между BTC и ETH.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import numpy as np


def compute_asian_ranges(df_1m: pd.DataFrame, asian_hours: tuple = (0, 6)) -> dict:
    """
    Вычисляет максимум и минимум Азиатской сессии (по умолчанию 00:00 - 06:00 UTC)
    для каждого торгового дня.
    Возвращает словарь: {date: {"high": float, "low": float, "open": float, "close": float}}
    """
    start_h, end_h = asian_hours
    asian_bars = df_1m[(df_1m.index.hour >= start_h) & (df_1m.index.hour < end_h)]
    if len(asian_bars) == 0:
        return {}

    ranges = {}
    grouped = asian_bars.groupby(asian_bars.index.date)
    for d, group in grouped:
        # Валидный диапазон: минимум 30 минут данных (>=30 баров для 1m или >=6 баров для 5m)
        if len(group) >= 6:
            ranges[d] = {
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "open": float(group.iloc[0]["open"]),
                "close": float(group.iloc[-1]["close"]),
            }
    return ranges


def is_asian_range_sweep(sweep_time, sweep_candle, asian_ranges: dict, tolerance_pct: float = 0.0008) -> tuple[bool, str]:
    """
    Проверяет, снял ли свип максимум или минимум Азиатской сессии текущего дня.
    Возвращает (is_sweep, 'ASIAN_HIGH' | 'ASIAN_LOW' | None).
    """
    # Азиатская сессия длится до 06:00 UTC. Свип азиатской ликвидности возможен только ПОСЛЕ её окончания
    if hasattr(sweep_time, "hour") and sweep_time.hour < 6:
        return False, None

    d = sweep_time.date()
    ar = asian_ranges.get(d)
    if not ar:
        return False, None

    # Свип азиатского максимума (Buy-side liquidity)
    if sweep_candle["high"] >= ar["high"] * (1.0 - tolerance_pct):
        return True, "ASIAN_HIGH"

    # Свип азиатского минимума (Sell-side liquidity)
    if sweep_candle["low"] <= ar["low"] * (1.0 + tolerance_pct):
        return True, "ASIAN_LOW"

    return False, None


def compute_smt_signals(df_lead_ltf: pd.DataFrame, df_lag_ltf: pd.DataFrame, lookback: int = 12) -> pd.DataFrame:
    """
    Детектор межинструментальной дивергенции SMT между двумя активами (например, BTC и ETH).
    Сравнивает поведение цен на 5m:
    - Bearish SMT: Один инструмент пробивает свинг-хай (Higher High), а второй не может пробить (Lower High).
    - Bullish SMT: Один инструмент пробивает свинг-лоу (Lower Low), а второй удерживает более высокий лоу (Higher Low).
    """
    common_idx = df_lead_ltf.index.intersection(df_lag_ltf.index)
    if len(common_idx) < lookback * 2:
        res = pd.DataFrame(index=df_lead_ltf.index)
        res["smt_bearish"] = False
        res["smt_bullish"] = False
        return res

    lead = df_lead_ltf.loc[common_idx]
    lag = df_lag_ltf.loc[common_idx]

    lead_max = lead["high"].rolling(lookback).max().shift(1)
    lead_min = lead["low"].rolling(lookback).min().shift(1)
    lag_max = lag["high"].rolling(lookback).max().shift(1)
    lag_min = lag["low"].rolling(lookback).min().shift(1)

    # Bearish SMT: Лидер обновляет хай, а ведомый - нет (или наоборот)
    lead_swept_high = lead["high"] > lead_max
    lag_swept_high = lag["high"] > lag_max
    bearish_smt = (lead_swept_high & ~lag_swept_high) | (~lead_swept_high & lag_swept_high)

    # Bullish SMT: Лидер обновляет лоу, а ведомый - нет (или наоборот)
    lead_swept_low = lead["low"] < lead_min
    lag_swept_low = lag["low"] < lag_min
    bullish_smt = (lead_swept_low & ~lag_swept_low) | (~lead_swept_low & lag_swept_low)

    smt_df = pd.DataFrame(index=common_idx)
    smt_df["smt_bearish"] = bearish_smt.fillna(False)
    smt_df["smt_bullish"] = bullish_smt.fillna(False)
    return smt_df
