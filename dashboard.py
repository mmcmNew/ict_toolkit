"""
Локальный интерактивный дашборд: свечи + вся разметка стратегии (FVG, Order Blocks,
зоны ликвидности/sweep, HTF-bias) + все сделки из backtest_results.csv - в том числе
там, где сделки не было, но паттерн сработал.

Поддерживает выбор инструмента:
  python dashboard.py --symbol BTC/USDT
  python dashboard.py --symbol ETH/USDT

Полностью офлайн после первого запуска: график сохраняется как самодостаточный
HTML-файл (plotly.js встроен внутрь), открывается в браузере без сети.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os
import argparse
import webbrowser
import pandas as pd
import plotly.graph_objects as go
from smartmoneyconcepts import smc

import config as cfg
from strategy import resample, compute_bias_series
from data_sources import get_data, get_symbol_slug

# Сколько последних дней показывать по умолчанию
LOOKBACK_DAYS = 30
LOOKBACK_END = None  # None = до последней доступной свечи; либо pd.Timestamp("2025-06-01")


def load_data(symbol: str = None):
    sym = symbol or getattr(cfg, "SYMBOL", "BTC/USDT")
    return get_data(cfg, symbol=sym, use_cache=True)


def build_dashboard(symbol: str = None):
    sym = symbol or getattr(cfg, "SYMBOL", "BTC/USDT")
    slug = get_symbol_slug(sym)

    df_1m_full = load_data(sym)

    end = LOOKBACK_END or df_1m_full.index.max()
    start = end - pd.Timedelta(days=LOOKBACK_DAYS)
    df_1m = df_1m_full[(df_1m_full.index >= start) & (df_1m_full.index <= end)]
    print(f"[{sym}] Окно просмотра: {df_1m.index.min()} -> {df_1m.index.max()} ({len(df_1m)} баров 1m)")

    # HTF bias считаем на ПОЛНОЙ истории (структура зависит от контекста до окна)
    df_htf_full = resample(df_1m_full, cfg.HTF_RULE)
    bias_series = compute_bias_series(df_htf_full, cfg.SWING_LENGTH_HTF)

    df_ltf = resample(df_1m, cfg.LTF_RULE)
    swings_ltf = smc.swing_highs_lows(df_ltf, swing_length=cfg.SWING_LENGTH_LTF)
    liq_ltf = smc.liquidity(df_ltf, swings_ltf, range_percent=cfg.LIQUIDITY_RANGE_PCT)
    liq_ltf.index = df_ltf.index
    fvg_ltf = smc.fvg(df_ltf)
    fvg_ltf.index = df_ltf.index
    ob_ltf = smc.ob(df_ltf, swings_ltf)
    ob_ltf.index = df_ltf.index

    reports_dir = getattr(cfg, "REPORTS_DIR", "reports")
    candidates = [
        os.path.join(reports_dir, f"backtest_results_{slug}.csv"),
        f"backtest_results_{slug}.csv",
    ]
    if sym == getattr(cfg, "SYMBOL", "BTC/USDT"):
        candidates.extend([
            os.path.join(reports_dir, "backtest_results.csv"),
            "backtest_results.csv",
        ])
    trades_file = candidates[0]
    for c in candidates:
        if os.path.exists(c):
            trades_file = c
            break

    trades_all = pd.read_csv(trades_file, parse_dates=["sweep_time", "fill_time", "exit_time"]) \
        if os.path.exists(trades_file) else pd.DataFrame()

    trades = trades_all[(trades_all["fill_time"] >= df_ltf.index.min()) &
                         (trades_all["fill_time"] <= df_ltf.index.max())] if len(trades_all) else trades_all

    fig = go.Figure()

    fig.add_trace(go.Candlestick(
        x=df_ltf.index, open=df_ltf["open"], high=df_ltf["high"],
        low=df_ltf["low"], close=df_ltf["close"], name=cfg.LTF_RULE,
        increasing_line_color="#1D9E75", decreasing_line_color="#E24B4A",
    ))

    bar_span = pd.Timedelta(cfg.LTF_RULE) * 20
    shapes = []

    fvg_found = fvg_ltf.dropna(subset=["FVG"])
    for t, row in fvg_found.iterrows():
        color = "rgba(29,158,117,0.15)" if row["FVG"] == 1 else "rgba(226,75,74,0.15)"
        shapes.append(dict(type="rect", x0=t, x1=t + bar_span, y0=row["Bottom"], y1=row["Top"],
                            fillcolor=color, line_width=0, layer="below"))

    ob_found = ob_ltf.dropna(subset=["OB"])
    for t, row in ob_found.iterrows():
        color = "rgba(55,138,221,0.12)" if row["OB"] == 1 else "rgba(235,104,52,0.12)"
        shapes.append(dict(type="rect", x0=t, x1=t + bar_span, y0=row["Bottom"], y1=row["Top"],
                            fillcolor=color, line_width=0, layer="below"))

    liq_found = liq_ltf.dropna(subset=["Liquidity"])
    swept_only = liq_found[(liq_found["Swept"].notna()) & (liq_found["Swept"] != 0)]
    sweep_x, sweep_y = [], []
    for t, row in swept_only.iterrows():
        pos = int(row["Swept"])
        if 0 < pos < len(df_ltf):
            sweep_x.append(df_ltf.index[pos])
            sweep_y.append(row["Level"])
    fig.add_trace(go.Scatter(x=sweep_x, y=sweep_y, mode="markers", name="Liquidity sweep",
                              marker=dict(symbol="x", size=7, color="#eb6834")))

    if len(trades):
        win = trades[trades["R"] > 0]
        loss = trades[trades["R"] <= 0]
        for subset, color, label in [(win, "#1D9E75", "Win"), (loss, "#E24B4A", "Loss")]:
            if len(subset) == 0:
                continue
            fig.add_trace(go.Scatter(
                x=subset["fill_time"], y=subset["fill_price"],
                mode="markers", name=f"Вход в сделку ({label})",
                marker=dict(symbol="triangle-up" if label == "Win" else "triangle-down",
                            size=11, color=color, line=dict(width=1, color="white")),
                text=[f"R={r:.2f}" for r in subset["R"]], hoverinfo="x+y+text",
            ))
            fig.add_trace(go.Scatter(
                x=subset["exit_time"], y=subset["exit_price"],
                mode="markers", name=f"Выход из сделки ({label})",
                marker=dict(symbol="circle", size=8, color=color, line=dict(width=1, color="white")),
                text=[f"R={r:.2f}" for r in subset["R"]], hoverinfo="x+y+text", showlegend=False,
            ))
        for _, row in trades.iterrows():
            x0, x1 = row["fill_time"], row["exit_time"]
            if pd.isna(x1) or x1 <= x0:
                x1 = x0 + bar_span
            shapes.append(dict(type="line", x0=x0, x1=x1, y0=row["stop"], y1=row["stop"],
                                line=dict(color="#E24B4A", width=1.5, dash="dot")))
            shapes.append(dict(type="line", x0=x0, x1=x1, y0=row["target"], y1=row["target"],
                                line=dict(color="#1D9E75", width=1.5, dash="dot")))
            shapes.append(dict(type="line", x0=x0, x1=x1, y0=row["fill_price"], y1=row["exit_price"],
                                line=dict(color="#378ADD", width=1, dash="dash")))

    # фон по HTF bias
    bias_times = list(bias_series.index) + [df_ltf.index.max()]
    bias_vals = list(bias_series.values)
    for i in range(len(bias_vals)):
        t0, t1 = bias_times[i], bias_times[i+1]
        if t1 < df_ltf.index.min() or t0 > df_ltf.index.max():
            continue
        color = "rgba(29,158,117,0.05)" if bias_vals[i] == 1 else "rgba(226,75,74,0.05)"
        shapes.append(dict(type="rect", x0=max(t0, df_ltf.index.min()), x1=min(t1, df_ltf.index.max()),
                            y0=0, y1=1, yref="paper", fillcolor=color, line_width=0, layer="below"))

    fig.update_layout(shapes=shapes)
    fig.update_layout(
        title=f"{sym} ({cfg.CCXT_EXCHANGE}) - {cfg.LTF_RULE}, последние {LOOKBACK_DAYS} дн. "
              f"(FVG/OB/sweep/bias + сделки)",
        xaxis_rangeslider_visible=True,
        dragmode="zoom",
        height=850,
        template="plotly_white",
        legend=dict(orientation="h", y=1.02),
    )
    fig.update_yaxes(fixedrange=False)

    reports_dir = getattr(cfg, "REPORTS_DIR", "reports")
    os.makedirs(reports_dir, exist_ok=True)
    out_file = os.path.join(reports_dir, f"dashboard_{slug}.html")
    fig.write_html(
        out_file, include_plotlyjs=True,
        config={"scrollZoom": True},
        post_script=f"""
        (function() {{
            var gd = document.getElementsByClassName('plotly-graph-div')[0];
            var xs = {df_ltf.index.astype('int64').tolist()};
            var highs = {df_ltf['high'].tolist()};
            var lows = {df_ltf['low'].tolist()};
            function rescaleY(eventdata) {{
                var xrange = (eventdata && eventdata['xaxis.range[0]']) ? gd.layout.xaxis.range : null;
                if (!xrange) return;
                var x0 = new Date(xrange[0]).getTime() * 1e6;
                var x1 = new Date(xrange[1]).getTime() * 1e6;
                var visHigh = -Infinity, visLow = Infinity;
                for (var i = 0; i < xs.length; i++) {{
                    if (xs[i] >= x0 && xs[i] <= x1) {{
                        if (highs[i] > visHigh) visHigh = highs[i];
                        if (lows[i] < visLow) visLow = lows[i];
                    }}
                }}
                if (visHigh === -Infinity) return;
                var pad = (visHigh - visLow) * 0.08;
                Plotly.relayout(gd, {{'yaxis.range': [visLow - pad, visHigh + pad]}});
            }}
            gd.on('plotly_relayout', rescaleY);
        }})();
        """
    )
    if sym == getattr(cfg, "SYMBOL", "BTC/USDT"):
        fig.write_html(os.path.join(reports_dir, "dashboard.html"), include_plotlyjs=True, config={"scrollZoom": True})

    print(f"Готово: {out_file} ({len(df_ltf)} свечей, {len(fvg_found)} FVG, "
          f"{len(ob_found)} order blocks, {len(swept_only)} sweep, {len(trades)} сделок в окне)")
    webbrowser.open(out_file)


def main():
    parser = argparse.ArgumentParser(description="ICT Interactive Dashboard")
    parser.add_argument("--symbol", type=str, default=None, help="Тикер инструмента (например, BTC/USDT, ETH/USDT)")
    args = parser.parse_args()
    build_dashboard(symbol=args.symbol)


if __name__ == "__main__":
    main()
