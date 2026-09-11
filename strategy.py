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

import pandas as pd
from smartmoneyconcepts import smc


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()


def compute_bias_series(df_htf: pd.DataFrame, swing_length: int) -> pd.Series:
    if len(df_htf) < swing_length * 2:
        return pd.Series(dtype=int, index=pd.DatetimeIndex([]))
    swings = smc.swing_highs_lows(df_htf, swing_length=swing_length)
    bos = smc.bos_choch(df_htf, swings)
    bos.index = df_htf.index  # ВАЖНО: smc возвращает RangeIndex, приходится восстанавливать вручную
    events = []
    for t, row in bos.iterrows():
        if pd.notna(row["BOS"]):
            events.append((t, row["BOS"]))
        elif pd.notna(row["CHOCH"]):
            events.append((t, row["CHOCH"]))
    if not events:
        return pd.Series(dtype=int, index=pd.DatetimeIndex([]))
    return pd.Series({t: v for t, v in events}, index=pd.DatetimeIndex([t for t, _ in events])).sort_index()


def bias_at(bias_series: pd.Series, ts) -> int:
    if bias_series is None or len(bias_series) == 0:
        return 0
    past = bias_series[bias_series.index <= ts]
    return int(past.iloc[-1]) if len(past) else 0


def in_killzone(ts, killzones) -> bool:
    h = ts.hour
    return any(a <= h < b for a, b in killzones)


def find_candidates(df_htf, df_ltf, cfg, df_1m=None, smt_df=None):
    """
    Возвращает список кандидатов на сделку: HTF bias + LTF sweep + LTF FVG confluence.
    Поддерживает фильтры:
    - Killzones (USE_KILLZONES)
    - Asian Range High/Low sweep (USE_ASIAN_RANGE_FILTER)
    - SMT Divergence (USE_SMT_FILTER)
    """
    from ict_advanced import compute_asian_ranges, is_asian_range_sweep

    bias_series = compute_bias_series(df_htf, cfg.SWING_LENGTH_HTF)

    swings_ltf = smc.swing_highs_lows(df_ltf, swing_length=cfg.SWING_LENGTH_LTF)
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

    candidates = []
    for idx, row in swept_ltf.iterrows():
        swept_bar_idx = int(row["Swept"])
        if swept_bar_idx <= 0 or swept_bar_idx >= len(df_ltf):
            continue
        sweep_time = df_ltf.index[swept_bar_idx]
        if getattr(cfg, "USE_KILLZONES", False) and not in_killzone(sweep_time, cfg.KILLZONES):
            continue
        bias = bias_at(bias_series, sweep_time)
        if bias == 0:
            continue
        expected_dir = 1 if row["Liquidity"] == -1 else -1
        if expected_dir != bias:
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

        window = fvg_ltf[(fvg_ltf.index > sweep_time)].head(15)
        matching = window[window["FVG"] == expected_dir]
        if len(matching) == 0:
            continue

        fvg_bar_time = matching.index[0]
        fvg_top, fvg_bottom = matching.iloc[0]["Top"], matching.iloc[0]["Bottom"]
        zone_pct = (fvg_top - fvg_bottom) / fvg_bottom
        if zone_pct > cfg.MAX_FVG_ZONE_PCT:
            continue

        fvg_bar_pos = df_ltf.index.get_loc(fvg_bar_time)
        if fvg_bar_pos + 1 >= len(df_ltf):
            continue
        confirm_time = df_ltf.index[fvg_bar_pos + 1]

        ob_pool = (ob_bull if expected_dir == 1 else ob_bear)
        ob_pool = ob_pool[ob_pool.index <= sweep_time]
        ob_candidate = ob_pool.iloc[-1].to_dict() if len(ob_pool) else None

        candidates.append(dict(
            sweep_time=sweep_time, confirm_time=confirm_time, expected_dir=expected_dir,
            fvg_top=float(fvg_top), fvg_bottom=float(fvg_bottom),
            sweep_candle_high=float(sweep_candle["high"]), sweep_candle_low=float(sweep_candle["low"]),
            ob_candidate=ob_candidate,
            is_asian_sweep=is_asian,
            asian_type=asian_type,
            has_smt=has_smt,
        ))
    return candidates
