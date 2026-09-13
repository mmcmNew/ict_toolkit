"""
trend_filter.py - Фильтр тренда и защита от бокового распила (Anti-Chop / Flat Filter).

Ключевые механизмы:
1. HTF ADX (Average Directional Index на 1H):
   - ADX(14) >= 20: рынок находится в фазе направленного тренда.
   - ADX(14) < 20: бестрендовый рынок / боковая компрессия -> БЛОКИРОВКА СЕТАПОВ.
2. ICT Displacement Ratio (Импульс vs Доджи):
   - Свеча формирования FVG обязана иметь выраженное тело:
     |Close - Open| / (High - Low) >= 0.50 (не менее 50% свечи - тело).
   - Отсекает нерешительные доджи и свечи с длинными фитилями внутри флэта.
3. HTF EMA Alignment (1H EMA 20 / EMA 50):
   - Проверка согласованности направления сетапа с локальным среднесрочным трендом.
"""

import numpy as np
import pandas as pd


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Рассчитывает Average Directional Index (ADX) по алгоритму Уэллса Уайлдера.
    """
    if len(df) < period * 2:
        return pd.Series(25.0, index=df.index)  # fallback при малой истории

    high = df["high"]
    low = df["low"]
    close = df["close"]

    # True Range (TR)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Directional Movement (+DM / -DM)
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    pos_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    neg_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Wilder's Smoothing
    tr_smooth = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    pos_dm_smooth = pd.Series(pos_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean()
    neg_dm_smooth = pd.Series(neg_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean()

    # Directional Indicators (+DI / -DI)
    pos_di = 100.0 * (pos_dm_smooth / (tr_smooth + 1e-9))
    neg_di = 100.0 * (neg_dm_smooth / (tr_smooth + 1e-9))

    # Directional Index (DX) & ADX
    di_sum = pos_di + neg_di + 1e-9
    dx = 100.0 * ((pos_di - neg_di).abs() / di_sum)
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()

    return adx


def check_displacement(fvg_candle: dict) -> tuple[bool, float]:
    """
    Проверяет ICT Displacement свечи, сформировавшей FVG:
    Отношение размера тела свечи к её полному размаху (High - Low) должно быть >= 50%.
    """
    if not fvg_candle:
        return True, 1.0

    o = fvg_candle.get("open", 0.0)
    c = fvg_candle.get("close", 0.0)
    h = fvg_candle.get("high", 0.0)
    l = fvg_candle.get("low", 0.0)

    body = abs(c - o)
    full_range = h - l
    if full_range <= 1e-9:
        return False, 0.0

    ratio = body / full_range
    is_valid = ratio >= 0.45  # 45-50% порог для истинного импульса
    return is_valid, round(ratio, 3)


def check_htf_trend_alignment(df_1h: pd.DataFrame, expected_dir: int,
                              time_point: pd.Timestamp = None) -> tuple[bool, str]:
    """
    Проверяет согласованность сделки с трендом 1H через EMA 20 и EMA 50.
    expected_dir: 1 (LONG) или -1 (SHORT).
    """
    if len(df_1h) < 50:
        return True, "INSUFFICIENT_1H_DATA"

    hist = df_1h[df_1h.index <= time_point] if time_point is not None else df_1h
    if len(hist) < 50:
        hist = df_1h.head(50)

    close = hist["close"]
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    last_p = close.iloc[-1]

    if expected_dir == 1:
        # Для покупок: цена выше EMA 50 или EMA 20 выше EMA 50
        if last_p < ema50 * 0.99 and ema20 < ema50:
            return False, f"DOWN_TREND_1H (Price {last_p:.2f} < EMA50 {ema50:.2f})"
        return True, "UP_TREND_1H"
    else:
        # Для продаж: цена ниже EMA 50 или EMA 20 ниже EMA 50
        if last_p > ema50 * 1.01 and ema20 > ema50:
            return False, f"UP_TREND_1H (Price {last_p:.2f} > EMA50 {ema50:.2f})"
        return True, "DOWN_TREND_1H"


def is_market_trending(df_1h: pd.DataFrame, candidate: dict,
                       min_adx: float = 20.0) -> tuple[bool, str, dict]:
    """
    Комплексный гейткипер тренда:
    1. 1H ADX >= min_adx (по умолчанию 20.0).
    2. Displacement Ratio свечи FVG >= 0.45.
    3. Согласованность с 1H EMA трендом.

    Возвращает (is_trending: bool, reason: str, metrics: dict).
    """
    metrics = {
        "adx_1h": 0.0,
        "displacement_ratio": 1.0,
        "ema_alignment": True,
    }

    # 1. Проверка Displacement свечи FVG
    fvg_candle = candidate.get("fvg_candle")
    if fvg_candle:
        disp_ok, disp_ratio = check_displacement(fvg_candle)
        metrics["displacement_ratio"] = disp_ratio
        if not disp_ok:
            return False, f"LACK_OF_DISPLACEMENT (Body/Range {disp_ratio*100:.1f}% < 45%)", metrics

    # 2. Проверка 1H ADX
    if df_1h is not None and len(df_1h) >= 28:
        conf_time = candidate.get("confirm_time")
        hist_1h = df_1h[df_1h.index <= conf_time] if conf_time is not None else df_1h
        if len(hist_1h) >= 28:
            adx_series = compute_adx(hist_1h)
            current_adx = float(adx_series.iloc[-1])
            metrics["adx_1h"] = round(current_adx, 2)
            if current_adx < min_adx:
                return False, f"FLAT_CHOP_1H (ADX {current_adx:.1f} < {min_adx})", metrics

    # 3. Проверка 1H EMA Alignment
    if df_1h is not None and len(df_1h) >= 50:
        conf_time = candidate.get("confirm_time")
        exp_dir = candidate.get("expected_dir", 1)
        ema_ok, ema_reason = check_htf_trend_alignment(df_1h, exp_dir, time_point=conf_time)
        metrics["ema_alignment"] = ema_ok
        if not ema_ok:
            return False, f"COUNTER_TREND_1H ({ema_reason})", metrics

    return True, "TRENDING_CONFIRMED", metrics
