"""
chart_generator.py - Генератор мульти-таймфрейм графиков сетапов для ICT Toolkit.

Создает скриншоты в стиле TradingView Dark:
- Панель 1: 1H (HTF Trend & Market Structure Context)
- Панель 2: 15M (Intermediate Liquidity Sweep Context)
- Панель 3: 5M (Execution: Entry, Stop-Loss, Take-Profit 1.5R, FVG зона, Asian Range)

Работает в headless-режиме (Agg backend) без GUI окон, рендерит изображение за <0.1 сек.
"""

import io
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")  # Headless backend для серверов и фоновых демонов
import matplotlib.pyplot as plt
import matplotlib.patches as patches


def _plot_subchart(
    ax,
    df_slice: pd.DataFrame,
    show_fvg: tuple = None,
    sweep_p: float = None,
    entry: float = None,
    sl: float = None,
    tp: float = None,
    asian_range: tuple = None,
    title: str = "",
    is_execution: bool = False,
):
    """Отрисовка свечей и уровней на одном таймфрейме."""
    if df_slice is None or len(df_slice) == 0:
        return

    n = len(df_slice)
    ax.set_facecolor("#131722")
    ax.grid(True, color="#2a2e39", linestyle="--", linewidth=0.5, alpha=0.6)

    # 1. Свечи (зеленый / красный)
    for i in range(n):
        o = df_slice["open"].iloc[i]
        c = df_slice["close"].iloc[i]
        h = df_slice["high"].iloc[i]
        l = df_slice["low"].iloc[i]
        color = "#089981" if c >= o else "#f23645"

        # Фитиль (тень)
        ax.plot([i, i], [l, h], color=color, linewidth=1.0)
        # Тело свечи
        body_bottom = min(o, c)
        body_h = max(abs(c - o), (h - l) * 0.02)
        rect = patches.Rectangle((i - 0.35, body_bottom), 0.7, body_h, color=color, alpha=0.9)
        ax.add_patch(rect)

    # 2. Азиатский диапазон (Asian Range)
    if asian_range and asian_range[0] and asian_range[1]:
        ar_low, ar_high = asian_range
        ar_box = patches.Rectangle(
            (0, ar_low),
            n + 4,
            ar_high - ar_low,
            facecolor="#787b86",
            alpha=0.12,
            edgecolor="#787b86",
            linestyle=":",
            linewidth=0.8,
            label=f"Asian Range [{ar_low:,.4f} - {ar_high:,.4f}]",
        )
        ax.add_patch(ar_box)

    # 3. FVG зона (Fair Value Gap)
    if show_fvg and show_fvg[0] and show_fvg[1]:
        bot, top = show_fvg
        fvg_color = "#2962ff" if is_execution else "#536dfe"
        fvg_box = patches.Rectangle(
            (max(0, n - 25), bot),
            28,
            top - bot,
            facecolor=fvg_color,
            alpha=0.25,
            edgecolor=fvg_color,
            linestyle="--",
            linewidth=0.9,
            label=f"FVG [{bot:,.4f} - {top:,.4f}]",
        )
        ax.add_patch(fvg_box)

    # 4. Уровни (Свип, Вход, Стоп, Тейк)
    if sweep_p:
        ax.axhline(sweep_p, color="#ffd700", linestyle=":", linewidth=1.2, alpha=0.9, label=f"Sweep ({sweep_p:,.4f})")
    if entry:
        ax.axhline(entry, color="#2962ff", linestyle="-", linewidth=1.2, label=f"Entry ({entry:,.4f})")
    if sl:
        ax.axhline(sl, color="#f23645", linestyle="--", linewidth=1.4, label=f"Stop-Loss ({sl:,.4f})")
    if tp:
        ax.axhline(tp, color="#089981", linestyle="--", linewidth=1.4, label=f"Take-Profit 1.5R ({tp:,.4f})")

    ax.set_title(title, color="#d1d4dc", fontsize=10, fontweight="bold", loc="left", pad=6)
    ax.set_xlim(-1, n + 4)
    ax.tick_params(colors="#787b86", labelsize=8)
    for s in ax.spines.values():
        s.set_color("#2a2e39")

    # Автоматический расчет границ цен
    all_p = [df_slice["low"].min(), df_slice["high"].max()]
    if sl:
        all_p.append(sl)
    if tp:
        all_p.append(tp)
    if sweep_p:
        all_p.append(sweep_p)
    if show_fvg and show_fvg[0] and show_fvg[1]:
        all_p.extend([show_fvg[0], show_fvg[1]])

    valid_p = [p for p in all_p if p is not None and not np.isnan(p)]
    if not valid_p:
        return
    p_min, p_max = min(valid_p), max(valid_p)
    pad = max((p_max - p_min) * 0.08, 0.0001)
    ax.set_ylim(p_min - pad, p_max + pad)


def generate_3tf_setup_chart(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    sweep_price: float = None,
    fvg_bottom: float = None,
    fvg_top: float = None,
    asian_range: tuple = None,
    bias_desc: str = "BULLISH",
) -> bytes:
    """
    Генерирует мульти-таймфрейм изображение графика сетапа (1H + 15M + 5M).
    Возвращает бинарные данные PNG.
    """
    fig, (ax_1h, ax_15m, ax_5m) = plt.subplots(3, 1, figsize=(11, 10), dpi=120)
    try:
        fig.patch.set_facecolor("#131722")

        # 1. 1H HTF Bias & Trend Structure
        slice_1h = df_1h.tail(35).copy().reset_index(drop=True)
        _plot_subchart(
            ax=ax_1h,
            df_slice=slice_1h,
            title=f"1. HTF Context [1H] — Trend Bias: {bias_desc.upper()}",
        )

        # 2. 15M Market Structure & Sweep Context
        slice_15m = df_15m.tail(40).copy().reset_index(drop=True)
        _plot_subchart(
            ax=ax_15m,
            df_slice=slice_15m,
            sweep_p=sweep_price,
            asian_range=asian_range,
            title="2. Structure Context [15M] — Liquidity Sweep & Session Context",
        )

        # 3. 5M Execution Panel (FVG + Entry + SL + TP 1.5R)
        slice_5m = df_5m.tail(45).copy().reset_index(drop=True)
        _plot_subchart(
            ax=ax_5m,
            df_slice=slice_5m,
            show_fvg=(fvg_bottom, fvg_top),
            sweep_p=sweep_price,
            entry=entry_price,
            sl=stop_loss,
            tp=take_profit,
            asian_range=asian_range,
            title=f"3. Execution [5M] — Entry @ {entry_price:,.4f} | SL: {stop_loss:,.4f} | TP (1.5R): {take_profit:,.4f}",
            is_execution=True,
        )
        ax_5m.legend(loc="upper left", facecolor="#1e222d", edgecolor="#2a2e39", fontsize=8, labelcolor="#d1d4dc")

        dir_str = "LONG" if str(direction).upper() in ("LONG", "1") else "SHORT"
        fig.suptitle(
            f"ICT MULTI-TIMEFRAME ANALYSIS | {symbol} [{dir_str}]",
            color="#ffffff",
            fontsize=13,
            fontweight="bold",
            y=0.995,
        )

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor(), edgecolor="none")
        buf.seek(0)
        return buf.getvalue()
    finally:
        plt.close(fig)
