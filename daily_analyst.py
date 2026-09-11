"""
Ежедневный квант-аналитик рынка и сделок (Daily Market & Trades Analyst).

Функционал:
1. Анализ всех сделок за выбранный день (причины стопов, тейков, серия сделок).
2. Анализ структуры дня (диапазон, тренд, волатильность, переключения HTF bias).
3. Сканер упущенных возможностей (Missed Setups Scanner):
   - Поиск свипов, отсеянных фильтрами (слишком широкий FVG, вне killzone, цена не дошла до FVG).
   - Определение: отработал бы упущенный сетап в плюс или фильтр спас от убытка.
4. Синтез через Google Gemini API (или rule-based квант-движок):
   - Оценка рыночного режима дня.
   - Аудит эффективности фильтров.
   - Конкретные гипотезы по корректировке параметров или добавлению правил.
5. Экспорт отчёта в JSON и Markdown в папку reports/.

Запуск:
  python daily_analyst.py --date 2025-01-15
  python daily_analyst.py --symbol BTC/USDT --latest
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os
import json
import argparse
import pandas as pd
import numpy as np
from datetime import datetime

import config as cfg
from data_sources import get_data, get_symbol_slug
from strategy import resample, compute_bias_series, bias_at, in_killzone
from smartmoneyconcepts import smc


def find_missed_opportunities(df_1m: pd.DataFrame, df_htf: pd.DataFrame,
                              df_ltf: pd.DataFrame, target_date_str: str,
                              symbol: str) -> list:
    """
    Сканирует день на предмет упущенных сетапов:
    - Свипы, не давшие FVG
    - Свипы, отсеянные фильтром ширины FVG (MAX_FVG_ZONE_PCT)
    - Свипы вне Killzones
    - Сетапы, где цена не вернулась в FVG (недоход до лимитки)
    И проверяет, куда ушла цена (была ли там прибыль 2R или стоп).
    """
    target_dt = pd.to_datetime(target_date_str).date()
    day_ltf = df_ltf[df_ltf.index.date == target_dt]
    if len(day_ltf) == 0:
        return []

    bias_series = compute_bias_series(df_htf, cfg.SWING_LENGTH_HTF)
    swings_ltf = smc.swing_highs_lows(df_ltf, swing_length=cfg.SWING_LENGTH_LTF)
    liq_ltf = smc.liquidity(df_ltf, swings_ltf, range_percent=cfg.LIQUIDITY_RANGE_PCT)
    liq_ltf.index = df_ltf.index
    fvg_ltf = smc.fvg(df_ltf)
    fvg_ltf.index = df_ltf.index

    swept = liq_ltf[(liq_ltf["Swept"].notna()) & (liq_ltf["Swept"] != 0)]
    day_swept = swept[swept.index.date == target_dt]

    missed = []
    for idx, row in day_swept.iterrows():
        swept_bar_idx = int(row["Swept"])
        if swept_bar_idx <= 0 or swept_bar_idx >= len(df_ltf):
            continue
        sweep_time = df_ltf.index[swept_bar_idx]
        bias = bias_at(bias_series, sweep_time)
        expected_dir = 1 if row["Liquidity"] == -1 else -1

        kz_ok = in_killzone(sweep_time, cfg.KILLZONES)
        bias_ok = (bias == expected_dir)

        # Проверяем окно FVG
        window = fvg_ltf[(fvg_ltf.index > sweep_time)].head(15)
        matching = window[window["FVG"] == expected_dir]

        reason_missed = None
        fvg_top, fvg_bottom, zone_pct = None, None, None

        if not bias_ok:
            reason_missed = "COUNTER_HTF_BIAS"
        elif cfg.USE_KILLZONES and not kz_ok:
            reason_missed = "OUTSIDE_KILLZONE"
        elif len(matching) == 0:
            reason_missed = "NO_FVG_FORMED"
        else:
            fvg_top = float(matching.iloc[0]["Top"])
            fvg_bottom = float(matching.iloc[0]["Bottom"])
            zone_pct = (fvg_top - fvg_bottom) / fvg_bottom
            if zone_pct > cfg.MAX_FVG_ZONE_PCT:
                reason_missed = "FVG_TOO_WIDE"
            else:
                # FVG валиден по фильтрам, проверяем, был ли вход цены в зону
                fvg_bar_pos = df_ltf.index.get_loc(matching.index[0])
                if fvg_bar_pos + 1 < len(df_ltf):
                    confirm_time = df_ltf.index[fvg_bar_pos + 1]
                    search_path = df_1m[df_1m.index > confirm_time].head(cfg.MAX_WAIT_MIN)
                    touched = False
                    for t, r in search_path.iterrows():
                        if r["low"] <= fvg_top and r["high"] >= fvg_bottom:
                            touched = True
                            break
                    if not touched:
                        reason_missed = "PRICE_NEVER_FILLED_FVG"

        # Если причина отсева зафиксирована — симулируем теоретический исход
        if reason_missed:
            sweep_candle = df_ltf.iloc[swept_bar_idx]
            hypo_entry = fvg_bottom if expected_dir == 1 and fvg_bottom else sweep_candle["close"]
            hypo_stop = sweep_candle["low"] if expected_dir == 1 else sweep_candle["high"]
            hypo_risk = abs(hypo_entry - hypo_stop)
            target = hypo_entry + 2 * hypo_risk if expected_dir == 1 else hypo_entry - 2 * hypo_risk

            # Проверяем движение цены в следующие 6 часов
            future = df_1m[df_1m.index > sweep_time].head(360)
            hypo_outcome = "UNDETERMINED"
            if len(future) and hypo_risk > 0:
                for t, r in future.iterrows():
                    if expected_dir == 1:
                        if r["low"] <= hypo_stop:
                            hypo_outcome = "WOULD_STOP"
                            break
                        if r["high"] >= target:
                            hypo_outcome = "WOULD_WIN_2R"
                            break
                    else:
                        if r["high"] >= hypo_stop:
                            hypo_outcome = "WOULD_STOP"
                            break
                        if r["low"] <= target:
                            hypo_outcome = "WOULD_WIN_2R"
                            break

            missed.append({
                "sweep_time": str(sweep_time),
                "dir": "LONG" if expected_dir == 1 else "SHORT",
                "bias": "BULLISH" if bias == 1 else "BEARISH" if bias == -1 else "NEUTRAL",
                "reason_filtered": reason_missed,
                "in_killzone": kz_ok,
                "zone_pct": round(zone_pct * 100, 3) if zone_pct else None,
                "hypothetical_outcome": hypo_outcome,
            })

    return missed


def synthesize_ai_review(day_summary: dict, trades_today: list,
                         missed_setups: list) -> dict:
    """Генерация аналитического заключение через Gemini API или rule-based движок."""
    api_key = getattr(cfg, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    model_name = getattr(cfg, "GEMINI_MODEL", "gemini-2.5-flash")

    # Базовый количественный разбор
    wins = [t for t in trades_today if t.get("R", 0) > 0]
    losses = [t for t in trades_today if t.get("R", 0) <= 0]
    total_r = sum(t.get("R", 0) for t in trades_today)
    saved_losses = len([m for m in missed_setups if m["hypothetical_outcome"] == "WOULD_STOP"])
    missed_wins = len([m for m in missed_setups if m["hypothetical_outcome"] == "WOULD_WIN_2R"])

    if not api_key:
        regime = "TRENDING" if abs(day_summary.get("net_change_pct", 0)) > 2.0 else "CHOPPY/RANGE"
        return {
            "market_regime": f"{regime} (диапазон {day_summary.get('day_range_pct', 0):.2f}%, изменение {day_summary.get('net_change_pct', 0):.2f}%)",
            "trades_postmortem": (
                f"Совершено {len(trades_today)} сделок: {len(wins)} прибыльных, {len(losses)} стопов. "
                f"Итог дня: {total_r:+.2f}R."
            ),
            "filter_audit": (
                f"Фильтры отсеяли {len(missed_setups)} свипов. Из них фильтры спасли от {saved_losses} стоп-лоссов, "
                f"но пропустили {missed_wins} потенциальных тейков 2R. "
                f"{'Фильтры сработали эффективно в защиту капитала.' if saved_losses >= missed_wins else 'Фильтры оказались излишне консервативными.'}"
            ),
            "actionable_hypotheses": [
                f"Проверить калибровку MAX_FVG_ZONE_PCT: зафиксировано {len([m for m in missed_setups if m['reason_filtered'] == 'FVG_TOO_WIDE'])} отсевов по ширине гэпа.",
                "Передать этот JSON агенту Antigravity для пакетного бэктеста альтернативных параметров."
            ],
            "proposed_agent_prompt": (
                f"Antigravity, изучи daily_review за {day_summary.get('date')}: "
                f"было пропущено {missed_wins} винов из-за фильтров. Протестируй расширение FVG до 0.008 на BTC и ETH."
            ),
            "source": "rule_based_fallback"
        }

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        prompt = f"""
Ты — ведущий Quant/ICT аналитик алгоритмического торгового фонда.
Проведи детальный аудит торгового дня и дай рекомендации по доработке торговой системы.

ДАННЫЕ ТОРГОВОГО ДНЯ:
{json.dumps(day_summary, ensure_ascii=False, indent=2)}

СОВЕРШЕННЫЕ СДЕЛКИ ЗА ДЕНЬ:
{json.dumps(trades_today, ensure_ascii=False, indent=2)}

ОТСЕЯННЫЕ СЕТАПЫ И УПУЩЕННЫЕ ВОЗМОЖНОСТИ:
{json.dumps(missed_setups, ensure_ascii=False, indent=2)}

ИНСТРУКЦИЯ:
Проанализируй день и верни строго JSON со следующими полями:
{{
  "market_regime": <диагностика режима: характер тренда, волатильность, ложные пробои>,
  "trades_postmortem": <подробный разбор совершенных сделок: причины исходов, качество входов>,
  "filter_audit": <оценка работы фильтров (MAX_FVG_ZONE_PCT, KILLZONES): спасли от убытка или отрезали прибыль?>,
  "actionable_hypotheses": [<список конкретных, проверяемых гипотез по улучшению правил стратегии>],
  "proposed_agent_prompt": <четкий текст команды для Antigravity-агента, чтобы он протестировал и внедрил улучшение в код>
}}
"""
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            )
        )
        res = json.loads(response.text)
        res["source"] = f"gemini_{model_name}"
        return res
    except Exception as e:
        fallback = synthesize_ai_review(day_summary, trades_today, missed_setups)
        fallback["trades_postmortem"] += f" (Gemini API error: {e})"
        return fallback


def analyze_day(target_date_str: str, symbol: str = "BTC/USDT") -> tuple[dict, str]:
    """Главный пайплайн анализа конкретного дня."""
    target_dt = pd.to_datetime(target_date_str).date()
    slug = get_symbol_slug(symbol)

    print(f"\nЗагрузка данных для {symbol} на дату {target_date_str}...")
    df_1m = get_data(cfg, symbol=symbol)
    df_htf = resample(df_1m, cfg.HTF_RULE)
    df_ltf = resample(df_1m, cfg.LTF_RULE)

    day_bars = df_1m[df_1m.index.date == target_dt]
    if len(day_bars) == 0:
        raise ValueError(f"На дату {target_date_str} нет баров в кэше данных для {symbol}.")

    day_open = float(day_bars.iloc[0]["open"])
    day_close = float(day_bars.iloc[-1]["close"])
    day_high = float(day_bars["high"].max())
    day_low = float(day_bars["low"].min())
    day_range_pct = ((day_high - day_low) / day_low) * 100.0
    net_change_pct = ((day_close - day_open) / day_open) * 100.0

    day_summary = {
        "date": target_date_str,
        "symbol": symbol,
        "day_open": round(day_open, 2),
        "day_close": round(day_close, 2),
        "day_high": round(day_high, 2),
        "day_low": round(day_low, 2),
        "day_range_pct": round(day_range_pct, 2),
        "net_change_pct": round(net_change_pct, 2),
        "total_bars_1m": len(day_bars),
    }

    # Загрузка сделок за день
    trades_path = f"backtest_results_{slug}.csv"
    if not os.path.exists(trades_path) and symbol == getattr(cfg, "SYMBOL", "BTC/USDT"):
        trades_path = "backtest_results.csv"

    trades_today = []
    if os.path.exists(trades_path):
        all_t = pd.read_csv(trades_path, parse_dates=["fill_time", "exit_time"])
        day_t = all_t[all_t["fill_time"].dt.date == target_dt]
        trades_today = day_t.to_dict(orient="records")
        for t in trades_today:
            for k in ["fill_time", "exit_time", "sweep_time"]:
                if k in t and pd.notna(t[k]):
                    t[k] = str(t[k])

    # Поиск упущенных сетапов
    missed_setups = find_missed_opportunities(df_1m, df_htf, df_ltf, target_date_str, symbol)

    # ИИ-синтез
    ai_synthesis = synthesize_ai_review(day_summary, trades_today, missed_setups)

    # Итоговый структурированный отчет
    full_report = {
        "report_generated_at": datetime.now().isoformat(),
        "market_summary": day_summary,
        "executed_trades": trades_today,
        "missed_setups": missed_setups,
        "ai_analysis": ai_synthesis,
    }

    # Сохраняем в папку reports/
    reports_dir = getattr(cfg, "REPORTS_DIR", "reports")
    os.makedirs(reports_dir, exist_ok=True)
    json_path = os.path.join(reports_dir, f"daily_review_{slug}_{target_date_str}.json")
    md_path = os.path.join(reports_dir, f"daily_review_{slug}_{target_date_str}.md")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(full_report, f, ensure_ascii=False, indent=2)

    # Генерация Markdown
    md_content = f"""# Ежедневный квант-анализ: {symbol} — {target_date_str}

## 1. Рыночный контекст дня
- **Open / Close**: {day_open:.2f} -> {day_close:.2f} ({net_change_pct:+.2f}%)
- **Диапазон (High - Low)**: {day_low:.2f} .. {day_high:.2f} ({day_range_pct:.2f}%)
- **Рыночный режим**: {ai_synthesis.get('market_regime')}

## 2. Итоги совершенных сделок ({len(trades_today)} шт.)
"""
    if len(trades_today):
        md_content += "| Время входа | Направление | Вход | Стоп | Тейк | Выход | Исход | R |\n"
        md_content += "|---|---|---|---|---|---|---|---|\n"
        for t in trades_today:
            md_content += (f"| {t.get('fill_time')} | {t.get('dir')} | {t.get('fill_price')} | "
                           f"{t.get('stop')} | {t.get('target')} | {t.get('exit_price')} | "
                           f"{t.get('outcome')} | **{t.get('R'):+.2f}R** |\n")
    else:
        md_content += "*В этот день сделок по стратегии открыто не было.*\n"

    md_content += f"""
### Разбор сделок от ИИ:
{ai_synthesis.get('trades_postmortem')}

## 3. Анализ упущенных возможностей (Missed Setups)
Обнаружено потенциальных точек ликвидности/свипов: **{len(missed_setups)}**.

"""
    if len(missed_setups):
        md_content += "| Время свипа | Направление | HTF Bias | Причина отсева | В Killzone | Теор. исход |\n"
        md_content += "|---|---|---|---|---|---|\n"
        for m in missed_setups:
            md_content += (f"| {m.get('sweep_time')} | {m.get('dir')} | {m.get('bias')} | "
                           f"`{m.get('reason_filtered')}` | {m.get('in_killzone')} | **{m.get('hypothetical_outcome')}** |\n")

    md_content += f"""
### Аудит фильтров от ИИ:
{ai_synthesis.get('filter_audit')}

## 4. Рекомендации и проверяемые гипотезы:
"""
    for hyp in ai_synthesis.get("actionable_hypotheses", []):
        md_content += f"- {hyp}\n"

    md_content += f"""
---
### Команда для агента Antigravity для внедрения:
> `{ai_synthesis.get('proposed_agent_prompt')}`
"""

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"\nОтчёты успешно сохранены:")
    print(f"  JSON: {json_path}")
    print(f"  Markdown: {md_path}")

    return full_report, md_path


def main():
    parser = argparse.ArgumentParser(description="ICT Daily Market & Trades Analyst")
    parser.add_argument("--date", type=str, default=None, help="Дата анализа в формате YYYY-MM-DD")
    parser.add_argument("--symbol", type=str, default=None, help="Инструмент (по умолчанию из config.py)")
    parser.add_argument("--latest", action="store_true", help="Взять последнюю дату со сделками")

    args = parser.parse_args()

    symbol = args.symbol or getattr(cfg, "SYMBOL", "BTC/USDT")
    slug = get_symbol_slug(symbol)

    target_date = args.date
    if not target_date or args.latest:
        # Пытаемся найти дату из сделок
        trades_path = f"backtest_results_{slug}.csv"
        if not os.path.exists(trades_path):
            trades_path = "backtest_results.csv"

        if os.path.exists(trades_path):
            df_t = pd.read_csv(trades_path, parse_dates=["fill_time"])
            if len(df_t):
                target_date = str(df_t["fill_time"].dt.date.max())
                print(f"Выбрана последняя дата со сделками: {target_date}")
        if not target_date:
            target_date = "2025-01-15"
            print(f"Дата по умолчанию: {target_date}")

    analyze_day(target_date, symbol=symbol)


if __name__ == "__main__":
    main()
