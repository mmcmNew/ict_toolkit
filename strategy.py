"""
Логика сетапа: HTF bias (BOS/CHoCH) + LTF liquidity sweep внутри killzone,
совпадающий по направлению с bias + LTF FVG как подтверждение/вход.

Это прямое повторение пайплайна, который мы вручную собрали и отладили
в чате (включая фиксы look-ahead bias и нереалистичного fill-price -
см. README.md, раздел "Известные грабли").
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from smartmoneyconcepts import smc


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()


def compute_daily_atr(df_1m: pd.DataFrame, period: int = 14) -> pd.Series:
    """Расчёт дневного ATR (True Range) для адаптивной калибровки R."""
    if df_1m is None or len(df_1m) < 100:
        return pd.Series(dtype=float)
    df_d = df_1m.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if len(df_d) < 2:
        return pd.Series(dtype=float)
    tr1 = df_d["high"] - df_d["low"]
    tr2 = (df_d["high"] - df_d["close"].shift(1)).abs()
    tr3 = (df_d["low"] - df_d["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period, min_periods=1).mean()
    return atr


def compute_5m_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Расчет ATR на барах LTF (5m) для адаптивного анти-шумового стоп-лосса."""
    if df is None or len(df) < 2:
        return pd.Series(dtype=float)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period, min_periods=1).mean()
    return atr


def compute_d1_bias_series(df_1m: pd.DataFrame, swing_length: int = 3) -> pd.Series:
    """Определяет макро-тренд (BOS/CHoCH) на дневном таймфрейме D1."""
    if df_1m is None or len(df_1m) < 200:
        return pd.Series(dtype=int, index=pd.DatetimeIndex([]))
    df_d = df_1m.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if len(df_d) < swing_length * 2:
        return pd.Series(dtype=int, index=pd.DatetimeIndex([]))
    return compute_bias_series(df_d, swing_length=swing_length)


def compute_bias_series(df_htf: pd.DataFrame, swing_length: int) -> pd.Series:
    if len(df_htf) < swing_length * 2:
        return pd.Series(dtype=int, index=pd.DatetimeIndex([]))
    swings = smc.swing_highs_lows(df_htf, swing_length=swing_length)
    bos = smc.bos_choch(df_htf, swings)
    events = []
    for idx, row in bos.iterrows():
        # В smc.bos_choch строка соответствует свингу, а реальный пробой происходит на BrokenIndex
        broken_idx = row.get("BrokenIndex")
        if pd.notna(broken_idx) and 0 <= int(broken_idx) < len(df_htf):
            event_time = df_htf.index[int(broken_idx)]
            val = row.get("BOS")
            if pd.isna(val) or val == 0:
                val = row.get("CHOCH")
            if pd.notna(val) and val != 0:
                events.append((event_time, int(val)))
        elif "BrokenIndex" not in row:
            if idx < len(df_htf):
                event_time = df_htf.index[idx]
                val = row.get("BOS") if pd.notna(row.get("BOS")) else row.get("CHOCH")
                if pd.notna(val) and val != 0:
                    events.append((event_time, int(val)))

    if not events:
        return pd.Series(dtype=int, index=pd.DatetimeIndex([]))

    # Сортируем по времени наступления пробоя (исключает Look-Ahead Bias)
    events.sort(key=lambda x: x[0])
    ev_dict = {t: v for t, v in events}
    return pd.Series(ev_dict, index=pd.DatetimeIndex(list(ev_dict.keys()))).sort_index()


def bias_at(bias_series: pd.Series, ts) -> int:
    if bias_series is None or len(bias_series) == 0:
        return 0
    past = bias_series[bias_series.index <= ts]
    return int(past.iloc[-1]) if len(past) else 0


def in_killzone(ts, killzones) -> bool:
    h = ts.hour
    for a, b in killzones:
        if a <= b:
            if a <= h < b:
                return True
        else:
            if h >= a or h < b:
                return True
    return False


def get_next_killzone_delta(now_utc, killzones):
    """
    Возвращает (seconds_until_start, session_name, target_dt) до следующей Киллзоны.
    Если сейчас внутри Киллзоны, возвращает (0, "INSIDE_KILLZONE", now_utc).
    """
    from datetime import datetime, timezone, timedelta

    if in_killzone(now_utc, killzones):
        return 0, "INSIDE_KILLZONE", now_utc

    kz_names = {
        (7, 10): "London Killzone (07:00-10:00 UTC)",
        (12, 15): "New York Killzone (12:00-15:00 UTC)",
    }

    candidates = []
    for day_offset in (0, 1):
        target_date = (now_utc + timedelta(days=day_offset)).date()
        for start_h, end_h in killzones:
            start_dt = datetime(target_date.year, target_date.month, target_date.day, start_h, 0, 0, tzinfo=timezone.utc)
            if start_dt > now_utc:
                diff_sec = int((start_dt - now_utc).total_seconds())
                name = kz_names.get((start_h, end_h), f"Killzone {start_h:02d}:00-{end_h:02d}:00 UTC")
                candidates.append((diff_sec, name, start_dt))

    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0]
    return 3600, "Unknown Killzone", now_utc + timedelta(hours=1)


def find_candidates(df_htf, df_ltf, cfg, df_1m=None, smt_df=None):
    """
    Возвращает список кандидатов на сделку: HTF bias + LTF sweep + LTF FVG confluence.
    Поддерживает фильтры:
    - Killzones (USE_KILLZONES)
    - Направление (DIRECTION_FILTER: 'all', 'long', 'short')
    - Asian Range High/Low sweep (USE_ASIAN_RANGE_FILTER)
    - SMT Divergence (USE_SMT_FILTER)
    """
    from ict_advanced import compute_asian_ranges, is_asian_range_sweep

    bias_series = compute_bias_series(df_htf, cfg.SWING_LENGTH_HTF)
    d1_bias_series = compute_d1_bias_series(df_1m) if (getattr(cfg, "USE_HTF_D1_FILTER", False) and df_1m is not None) else None
    daily_atr_series = compute_daily_atr(df_1m) if (getattr(cfg, "USE_DYNAMIC_R", False) and df_1m is not None) else None
    daily_atr_ltf = daily_atr_series.reindex(df_ltf.index, method="ffill") if (daily_atr_series is not None and len(daily_atr_series)) else None
    atr_5m_series = compute_5m_atr(df_ltf) if df_ltf is not None else None

    swings_ltf = smc.swing_highs_lows(df_ltf, swing_length=cfg.SWING_LENGTH_LTF)
    high_mask = (swings_ltf["HighLow"].values == 1)
    low_mask = (swings_ltf["HighLow"].values == -1)
    high_indices = np.where(high_mask)[0]
    high_levels = swings_ltf["Level"].values[high_indices].astype(float)
    low_indices = np.where(low_mask)[0]
    low_levels = swings_ltf["Level"].values[low_indices].astype(float)
    liq_ltf = smc.liquidity(df_ltf, swings_ltf, range_percent=cfg.LIQUIDITY_RANGE_PCT)
    liq_ltf.index = df_ltf.index
    swept_ltf = liq_ltf[(liq_ltf["Swept"].notna()) & (liq_ltf["Swept"] != 0)]

    fvg_ltf = smc.fvg(df_ltf)
    fvg_ltf.index = df_ltf.index

    ob_ltf = smc.ob(df_ltf, swings_ltf)
    ob_ltf.index = df_ltf.index
    ob_bull = ob_ltf[ob_ltf["OB"] == 1]
    ob_bear = ob_ltf[ob_ltf["OB"] == -1]

    # Вычисление уровней Азиатской сессии при наличии минутных данных
    asian_ranges = compute_asian_ranges(df_1m, getattr(cfg, "ASIAN_HOURS", (0, 6))) if df_1m is not None else {}

    # Расчет ATR(14) на LTF для фильтрации периодов с усохшей волатильностью
    tr1 = df_ltf["high"] - df_ltf["low"]
    tr2 = (df_ltf["high"] - df_ltf["close"].shift(1)).abs()
    tr3 = (df_ltf["low"] - df_ltf["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr_ltf_series = tr.rolling(14).mean()
    atr_pct_series = atr_ltf_series / df_ltf["close"]

    candidates = []
    seen_sweeps = set()
    for idx, row in swept_ltf.iterrows():
        swept_bar_idx = int(row["Swept"])
        if swept_bar_idx <= 0 or swept_bar_idx >= len(df_ltf):
            continue
        sweep_time = df_ltf.index[swept_bar_idx]
        if sweep_time in seen_sweeps:
            continue
        if getattr(cfg, "USE_KILLZONES", False) and not in_killzone(sweep_time, cfg.KILLZONES):
            continue

        # Фильтр минимальной волатильности (защита от мертвого боковика)
        if getattr(cfg, "USE_VOLATILITY_FILTER", False):
            min_atr = getattr(cfg, "MIN_ATR_5M_PCT", 0.0005)
            curr_atr_pct = float(atr_pct_series.loc[sweep_time]) if sweep_time in atr_pct_series.index else 0.0
            if curr_atr_pct < min_atr:
                continue

        bias = bias_at(bias_series, sweep_time)
        if bias == 0:
            continue
        expected_dir = 1 if row["Liquidity"] == -1 else -1
        if expected_dir != bias:
            continue

        dir_filter = getattr(cfg, "DIRECTION_FILTER", "all")
        if dir_filter in ("long", "LONG") and expected_dir != 1:
            continue
        if dir_filter in ("short", "SHORT") and expected_dir != -1:
            continue

        # Фильтр старшего тренда D1 (блокирует контртренд на макро-масштабе)
        d1_val = 0
        if d1_bias_series is not None and len(d1_bias_series):
            d1_val = bias_at(d1_bias_series, sweep_time)
            if getattr(cfg, "USE_HTF_D1_FILTER", False) and d1_val != 0 and d1_val != expected_dir:
                continue

        sweep_candle = df_ltf.iloc[swept_bar_idx]

        # Проверка свипа азиатской ликвидности
        is_asian, asian_type = is_asian_range_sweep(sweep_time, sweep_candle, asian_ranges)
        if getattr(cfg, "USE_ASIAN_RANGE_FILTER", False) and not is_asian:
            continue

        # Проверка SMT дивергенции
        has_smt = False
        if smt_df is not None and sweep_time in smt_df.index:
            smt_row = smt_df.loc[sweep_time]
            has_smt = bool(smt_row["smt_bullish"]) if expected_dir == 1 else bool(smt_row["smt_bearish"])
        elif smt_df is not None:
            # Ищем SMT в окне +/- 2 бара вокруг свипа
            window_smt = smt_df[(smt_df.index >= sweep_time - pd.Timedelta(minutes=15)) &
                                (smt_df.index <= sweep_time + pd.Timedelta(minutes=15))]
            if len(window_smt):
                col = "smt_bullish" if expected_dir == 1 else "smt_bearish"
                has_smt = bool(window_smt[col].any())

        if getattr(cfg, "USE_SMT_FILTER", False) and not has_smt:
            continue

        # Оптимизированный срез fvg_ltf через iloc вместо тяжелой булевой маски по всему индексу
        window = fvg_ltf.iloc[swept_bar_idx + 1 : swept_bar_idx + 16]
        matching = window[window["FVG"] == expected_dir]
        if len(matching) == 0:
            continue

        fvg_bar_time = matching.index[0]
        fvg_top, fvg_bottom = matching.iloc[0]["Top"], matching.iloc[0]["Bottom"]
        zone_pct = (fvg_top - fvg_bottom) / fvg_bottom
        min_fvg = getattr(cfg, "MIN_FVG_ZONE_PCT", 0.0005)
        if zone_pct < min_fvg or zone_pct > cfg.MAX_FVG_ZONE_PCT:
            continue

        fvg_bar_pos = df_ltf.index.get_loc(fvg_bar_time)
        if fvg_bar_pos + 1 >= len(df_ltf):
            continue
        confirm_time = df_ltf.index[fvg_bar_pos + 1]

        ob_pool = (ob_bull if expected_dir == 1 else ob_bear)
        ob_pool = ob_pool[ob_pool.index <= sweep_time]
        ob_candidate = ob_pool.iloc[-1].to_dict() if len(ob_pool) else None

        fvg_ce = (float(fvg_top) + float(fvg_bottom)) / 2.0
        daily_atr = None
        if daily_atr_ltf is not None and swept_bar_idx < len(daily_atr_ltf):
            datr_val = daily_atr_ltf.iloc[swept_bar_idx]
            if pd.notna(datr_val):
                daily_atr = float(datr_val)

        atr_5m = None
        if atr_5m_series is not None and swept_bar_idx < len(atr_5m_series):
            a_val = atr_5m_series.iloc[swept_bar_idx]
            if pd.notna(a_val):
                atr_5m = float(a_val)

        atr_pct = float(atr_pct_series.iloc[swept_bar_idx]) if swept_bar_idx < len(atr_pct_series) else 0.0

        # Быстрый векторный O(1) поиск ближайшего встречного пула ликвидности (BSL / SSL)
        structural_pool = None
        if expected_dir == 1:
            valid_m = high_indices < swept_bar_idx
            if np.any(valid_m):
                past_h = high_levels[valid_m]
                above = past_h[past_h > float(sweep_candle["high"])]
                if len(above):
                    structural_pool = float(above[-1])
        else:
            valid_m = low_indices < swept_bar_idx
            if np.any(valid_m):
                past_l = low_levels[valid_m]
                below = past_l[past_l < float(sweep_candle["low"])]
                if len(below):
                    structural_pool = float(below[-1])

        seen_sweeps.add(sweep_time)
        candidates.append(dict(
            sweep_time=sweep_time, confirm_time=confirm_time, expected_dir=expected_dir,
            bias=bias, d1_bias=d1_val,
            fvg_top=float(fvg_top), fvg_bottom=float(fvg_bottom), fvg_ce=float(fvg_ce),
            zone_pct=float(zone_pct),
            daily_atr=daily_atr, atr_5m=atr_5m, atr_pct=atr_pct, structural_pool=structural_pool,
            sweep_candle_high=float(sweep_candle["high"]), sweep_candle_low=float(sweep_candle["low"]),
            ob_candidate=ob_candidate,
            is_asian_sweep=is_asian,
            asian_type=asian_type,
            has_smt=has_smt,
        ))

    # Сортируем кандидатов строго хронологически по времени подтверждения
    candidates.sort(key=lambda c: (c["confirm_time"], c["sweep_time"]))
    return candidates
