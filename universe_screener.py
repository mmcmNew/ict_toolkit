"""
universe_screener.py - Автоматический скринер инструментов (Universe Screener)
для Crypto (Bitget / Binance) и Мосбиржи (Т-Банк Инвестиции).

Ключевые возможности:
1. Систематический прогон пула кандидатов за rolling-окно (по умолчанию 90 дней).
2. Расчет институциональных метрик: Winrate, Total Net R, Max DD, Profit Factor, Recovery Ratio, Composite Score.
3. Проверка жестких Quality Gates и отсеивание токсичных/неликвидных активов.
4. Проверка микро-депозита (Min notional order <= $6.50 на Bitget) для защиты счетов $10-$50.
5. Интерактивная интеграция с Telegram: формирование сообщения с инлайн-кнопками выбора и применения.
6. Экспорт отчетов в reports/screener_{market}_latest.json и reports/screener_{market}_latest.csv.
"""

import os
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import time
import json
import html
import argparse
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

import config as cfg
from data_sources import get_data, get_symbol_slug
from strategy import find_candidates, resample
from core_engine import simulate_grid_b, compute_metrics
from telegram_notifier import send_telegram_message


# Порог минимального ордера биржи для микро-депозитов ($10 - $50)
MAX_MICRO_NOTIONAL_USD = 6.50

# Fallback-список проверенных минимальных ордеров на Bitget USDT-M Futures
BITGET_FALLBACK_LIMITS = {
    "DOGE/USDT": {"min_notional": 5.00, "is_safe": True},
    "ADA/USDT": {"min_notional": 5.00, "is_safe": True},
    "SUI/USDT": {"min_notional": 5.00, "is_safe": True},
    "NEAR/USDT": {"min_notional": 5.00, "is_safe": True},
    "AVAX/USDT": {"min_notional": 5.00, "is_safe": True},
    "APT/USDT": {"min_notional": 5.00, "is_safe": True},
    "DOT/USDT": {"min_notional": 5.00, "is_safe": True},
    "TRX/USDT": {"min_notional": 5.00, "is_safe": True},
    "XRP/USDT": {"min_notional": 5.00, "is_safe": True},
    "BNB/USDT": {"min_notional": 7.26, "is_safe": False},
    "SOL/USDT": {"min_notional": 10.16, "is_safe": False},
    "BTC/USDT": {"min_notional": 7.72, "is_safe": False},
    "ETH/USDT": {"min_notional": 25.20, "is_safe": False},
    "LINK/USDT": {"min_notional": 11.48, "is_safe": False},
}


def check_crypto_market_limits(symbols: list[str]) -> dict:
    """
    Проверяет минимальный размер ордера (Min Notional / Cost) на Bitget USDT-M Futures.
    Возвращает словарь {symbol: {'min_notional': float, 'is_safe': bool, 'price': float}}.
    """
    limits_map = {}
    try:
        import ccxt
        ex = ccxt.bitget({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
        ex.load_markets()
        for sym in symbols:
            swap_sym = sym if sym.endswith(":USDT") else f"{sym}:USDT"
            if swap_sym in ex.markets:
                m = ex.markets[swap_sym]
                min_amt = (m.get('limits', {}).get('amount', {}) or {}).get('min') or 0.0
                min_cost = (m.get('limits', {}).get('cost', {}) or {}).get('min') or 0.0
                price = 0.0
                try:
                    ticker = ex.fetch_ticker(swap_sym)
                    price = float(ticker.get('last') or 0.0)
                except Exception:
                    pass
                min_notional = max(min_cost, min_amt * price) if price > 0 else max(min_cost, 5.0)
                is_safe = min_notional <= MAX_MICRO_NOTIONAL_USD
                limits_map[sym] = {
                    "min_notional": round(min_notional, 2),
                    "is_safe": is_safe,
                    "price": price,
                }
            elif sym in BITGET_FALLBACK_LIMITS:
                limits_map[sym] = dict(BITGET_FALLBACK_LIMITS[sym], price=0.0)
            else:
                limits_map[sym] = {"min_notional": 5.00, "is_safe": True, "price": 0.0}
    except Exception as e:
        # Fallback при отсутствии сети
        for sym in symbols:
            if sym in BITGET_FALLBACK_LIMITS:
                limits_map[sym] = dict(BITGET_FALLBACK_LIMITS[sym], price=0.0)
            else:
                limits_map[sym] = {"min_notional": 5.00, "is_safe": True, "price": 0.0}

    return limits_map


def run_screener(market: str = "crypto", days: int = 90, candidates: list[str] = None,
                 top_n: int = 4, progress_cb = None) -> dict:
    """
    Основной конвейер скрининга инструментов.
    market: 'crypto' или 'moex'
    days: глубина бэктеста в днях (по умолчанию 90)
    candidates: список инструментов для скрининга (если None - берется из config.py)
    top_n: размер формируемой корзины лучших инструментов
    progress_cb: опциональный callback для вывода прогресса cb(current, total, symbol)
    """
    market = market.lower()
    is_crypto = market == "crypto"

    # 1. Пул кандидатов
    if candidates and len(candidates):
        target_pool = candidates
    elif is_crypto:
        target_pool = getattr(cfg, "CRYPTO_SCREENER_POOL", [
            "DOGE/USDT", "ADA/USDT", "SUI/USDT", "NEAR/USDT", "AVAX/USDT",
            "APT/USDT", "BNB/USDT", "SOL/USDT", "DOT/USDT", "TRX/USDT", "XRP/USDT"
        ])
    else:
        target_pool = getattr(cfg, "MOEX_SCREENER_POOL", [
            "SBER", "GAZP", "LKOH", "ROSN", "YDEX", "NVTK", "GMKN", "TATN", "CHMF", "PLZL", "MOEX", "ALRS"
        ])

    # 2. Настройка параметров институционального профиля Grid B
    start_date = (pd.Timestamp.now() - pd.Timedelta(days=days)).floor("min")
    cfg.START_DATE = start_date.strftime("%Y-%m-%d")
    cfg.DATA_SOURCE = "ccxt" if is_crypto else "tbank"
    cfg.DIRECTION_FILTER = "all"
    cfg.STOP_MODE = "wick"
    cfg.FVG_ENTRY_MODE = "ce"
    cfg.USE_BREAKEVEN = True
    cfg.BREAKEVEN_TRIGGER_R = 1.0
    cfg.PARTIAL_TAKE_R = 1.0
    cfg.PARTIAL_TAKE_SIZE = 0.5
    cfg.TRAIL_DISTANCE_R = 0.8
    cfg.USE_VOLATILITY_FILTER = True
    cfg.MIN_FVG_ZONE_PCT = 0.0005
    cfg.MIN_ATR_5M_PCT = 0.0005

    if is_crypto:
        cfg.USE_KILLZONES = False  # крипта торгуется 24/7
        cfg.USE_ASIAN_RANGE_FILTER = False  # 24/7 торговля, фильтрация через ADX и Trend Filter
    else:
        cfg.USE_KILLZONES = True
        cfg.KILLZONES = getattr(cfg, "MOEX_KILLZONES", [(8, 12)])  # 11:00-15:00 MSK
        cfg.MOEX_EXCLUDE_DAYS = ["Thursday"]  # токсичные четверги

    # 3. Проверка лимитов микро-депозита для крипты
    crypto_limits = {}
    if is_crypto:
        crypto_limits = check_crypto_market_limits(target_pool)

    # 4. Прогон бэктеста по каждому кандидату через единый движок core_engine
    results = []
    total_count = len(target_pool)

    print("=" * 80)
    print(f"🚀 ЗАПУСК UNIVERSE SCREENER: {market.upper()} | Дней: {days} | Кандидатов: {total_count}")
    print(f"Период: {cfg.START_DATE} -> {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 80)

    for idx, sym in enumerate(target_pool):
        if progress_cb:
            try:
                progress_cb(idx + 1, total_count, sym)
            except Exception:
                pass

        print(f"\n[{idx+1}/{total_count}] Скрининг {sym}...", flush=True)

        try:
            df_1m = get_data(cfg, symbol=sym, start_date=start_date, use_cache=True)
            if len(df_1m) < 500:
                print(f"  ⚠️ Недостаточно данных для {sym} (всего {len(df_1m)} баров). Пропуск.")
                results.append({
                    "symbol": sym,
                    "trades": 0, "wins": 0, "losses": 0, "winrate": 0.0,
                    "total_r": 0.0, "max_dd_r": 0.0, "profit_factor": 0.0,
                    "avg_r": 0.0, "recovery_ratio": 0.0, "score": 0.0,
                    "passed": False, "fail_reason": "NO_DATA",
                    "min_notional": crypto_limits.get(sym, {}).get("min_notional", 0.0) if is_crypto else 0.0,
                })
                continue

            df_htf = resample(df_1m, cfg.HTF_RULE)
            df_ltf = resample(df_1m, cfg.LTF_RULE)
            candidates_list = find_candidates(df_htf, df_ltf, cfg, df_1m=df_1m)

            if not candidates_list:
                trades_df = pd.DataFrame()
                metrics = compute_metrics([])
            else:
                trades_df, metrics = simulate_grid_b(
                    df_1m, df_htf, df_ltf, candidates_list,
                    symbol=sym, is_moex=not is_crypto,
                    use_vol_filter=True, min_fvg_pct=0.0005, min_atr_5m_pct=0.0005,
                    use_trend_filter=True, min_adx=20.0
                )
            trades_cnt = metrics["trades"]
            winrate = metrics["winrate"]
            total_r = metrics["total_r"]
            max_dd = metrics["max_dd_r"]
            pf = metrics["profit_factor"]
            avg_r = metrics["avg_r"]

            # Recovery Ratio: Total R / max(Max DD, 0.5)
            recovery = round(total_r / max(max_dd, 0.5), 2) if total_r > 0 else 0.0

            # Composite Score: Recovery * (Winrate / 50) * min(PF, 3.0)
            score = round(recovery * (winrate / 50.0) * min(pf, 3.0), 2) if total_r > 0 else 0.0

            # Quality Gate Checks
            is_micro_safe = True
            min_notional = 0.0
            if is_crypto:
                lim = crypto_limits.get(sym, {})
                min_notional = lim.get("min_notional", 5.0)
                is_micro_safe = lim.get("is_safe", True)

            fail_reasons = []
            min_trades_req = 8 if days >= 60 else 4
            if trades_cnt < min_trades_req:
                fail_reasons.append(f"Мало сделок ({trades_cnt}<{min_trades_req})")
            if winrate < 60.0:
                fail_reasons.append(f"WR {winrate:.1f}% < 60%")
            if pf < 1.60:
                fail_reasons.append(f"PF {pf:.2f} < 1.6")
            if max_dd > 5.5:
                fail_reasons.append(f"DD {max_dd:.1f}R > 5.5R")
            if total_r <= 0:
                fail_reasons.append(f"Убыток ({total_r:.1f}R)")
            if not is_micro_safe:
                fail_reasons.append(f"Мин. лот ${min_notional:.2f} > ${MAX_MICRO_NOTIONAL_USD}")

            passed = len(fail_reasons) == 0
            reason_str = ", ".join(fail_reasons) if fail_reasons else "OK"

            res_item = {
                "symbol": sym,
                "trades": trades_cnt,
                "wins": metrics["wins"],
                "losses": metrics["losses"],
                "winrate": winrate,
                "total_r": total_r,
                "max_dd_r": max_dd,
                "profit_factor": pf,
                "avg_r": avg_r,
                "recovery_ratio": recovery,
                "score": score,
                "passed": passed,
                "fail_reason": reason_str,
                "min_notional": min_notional,
            }
            results.append(res_item)

            status_mark = "✅ [PASS]" if passed else f"❌ [FAIL: {reason_str}]"
            print(f"  -> {sym}: {trades_cnt} сд | WR: {winrate}% | Total R: {total_r:+.2f}R | DD: {max_dd:.2f}R | PF: {pf:.2f} | Score: {score} | {status_mark}")

        except Exception as e:
            print(f"  ❌ Ошибка скрининга {sym}: {e}")
            results.append({
                "symbol": sym,
                "trades": 0, "wins": 0, "losses": 0, "winrate": 0.0,
                "total_r": 0.0, "max_dd_r": 0.0, "profit_factor": 0.0,
                "avg_r": 0.0, "recovery_ratio": 0.0, "score": 0.0,
                "passed": False, "fail_reason": f"ERROR: {str(e)[:30]}",
                "min_notional": 0.0,
            })

    # 5. Сортировка: сначала PASS по убыванию Score, затем остальные
    passed_items = sorted([r for r in results if r["passed"]], key=lambda x: x["score"], reverse=True)
    failed_items = sorted([r for r in results if not r["passed"]], key=lambda x: (x["total_r"], x["score"]), reverse=True)
    all_ranked = passed_items + failed_items

    # Выбор Топ-N
    if len(passed_items) >= top_n:
        recommended_syms = [r["symbol"] for r in passed_items[:top_n]]
    else:
        # Если прошедших фильтр меньше top_n, берем всех прошедших + лучших из безопасных по марже
        rec = [r["symbol"] for r in passed_items]
        for f in failed_items:
            if len(rec) >= top_n:
                break
            if f["total_r"] > 0 and (not is_crypto or f.get("min_notional", 99) <= MAX_MICRO_NOTIONAL_USD):
                rec.append(f["symbol"])
        recommended_syms = rec if rec else [r["symbol"] for r in all_ranked[:top_n]]

    # 6. Сохранение отчетов
    reports_dir = getattr(cfg, "REPORTS_DIR", "reports")
    os.makedirs(reports_dir, exist_ok=True)

    df_report = pd.DataFrame(all_ranked)
    csv_latest = os.path.join(reports_dir, f"screener_{market}_latest.csv")
    json_latest = os.path.join(reports_dir, f"screener_{market}_latest.json")
    df_report.to_csv(csv_latest, index=False)

    report_payload = {
        "market": market,
        "days": days,
        "timestamp": datetime.now().isoformat(),
        "total_scanned": total_count,
        "passed_count": len(passed_items),
        "recommended_symbols": recommended_syms,
        "items": all_ranked,
    }
    with open(json_latest, "w", encoding="utf-8") as f:
        json.dump(report_payload, f, indent=4, ensure_ascii=False)

    print("\n" + "=" * 80)
    print(f"🏁 СКРИНИНГ ЗАВЕРШЕН. Прошло фильтры: {len(passed_items)}/{total_count}")
    print(f"Рекомендованная корзина ({len(recommended_syms)}): {', '.join(recommended_syms)}")
    print(f"Отчет сохранен: {json_latest}")
    print("=" * 80)

    return report_payload


def format_screener_telegram_message(report: dict) -> tuple[str, dict]:
    """
    Формирует красивое HTML-сообщение и инлайн-клавиатуру для Telegram.
    Под сообщением формируются кнопки:
    1. [✅ Применить Топ в бота]
    2. [📋 Выбрать вручную]
    3. [🔄 Крипта (90д)] | [🇷🇺 Мосбиржа (90д)]
    """
    market = report.get("market", "crypto").lower()
    days = report.get("days", 90)
    items = report.get("items", [])
    rec_syms = report.get("recommended_symbols", [])

    is_crypto = market == "crypto"
    market_name = "КРИПТОВАЛЮТ (Bitget)" if is_crypto else "МОСБИРЖИ (Т-Банк)"

    lines = [
        f"🔍 <b>СКРИНЕР АКТИВОВ: {market_name}</b>",
        f"<i>Анализ за {days} дн. по институциональному профилю Grid B</i>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    passed_items = [it for it in items if it.get("passed")]
    failed_items = [it for it in items if not it.get("passed")]

    if passed_items:
        lines.append("🏆 <b>ТОП КАНДИДАТЫ (КВАЛИФИЦИРОВАНЫ):</b>")
        for idx, it in enumerate(passed_items, 1):
            sym = it["symbol"]
            wr = it["winrate"]
            tot_r = it["total_r"]
            dd = it["max_dd_r"]
            pf = it["profit_factor"]
            score = it["score"]
            trades = it["trades"]
            lines.append(
                f"<b>{idx}. 🟢 {sym}</b>\n"
                f"   └ WR: <b>{wr:.1f}%</b> ({trades} сд) | R: <b>{tot_r:+.1f}R</b> | DD: <b>{dd:.1f}R</b> | PF: <b>{pf:.2f}</b> | Score: <b>{score}</b>"
            )
        lines.append("─────────────────────")

    if failed_items:
        lines.append("⚠️ <b>ОТКЛОНЕННЫЕ ИНСТРУМЕНТЫ:</b>")
        for it in failed_items[:5]:
            sym = it["symbol"]
            reason = it.get("fail_reason", "FAIL")
            tot_r = it.get("total_r", 0.0)
            lines.append(f"• <b>{sym}</b>: {tot_r:+.1f}R <i>({html.escape(reason)})</i>")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")

    rec_str = ", ".join(rec_syms) if rec_syms else "—"
    lines.append(f"💡 <b>Рекомендованный состав корзины ({len(rec_syms)}):</b>\n<code>{rec_str}</code>")

    # Формирование инлайн-кнопок
    top_label = f"✅ Применить Топ-{len(rec_syms)} в бота" if rec_syms else "✅ Применить выбор"
    inline_keyboard = [
        [{"text": top_label, "callback_data": f"screen:apply:{market}"}],
        [{"text": "📋 Выбрать пары вручную", "callback_data": f"screen:manual:{market}"}],
        [
            {"text": "🔄 Крипта (90д)", "callback_data": "screen:run:crypto"},
            {"text": "🇷🇺 Мосбиржа (90д)", "callback_data": "screen:run:moex"},
        ],
    ]

    return "\n".join(lines), {"inline_keyboard": inline_keyboard}


def format_manual_selection_keyboard(market: str, selected_symbols: list[str], all_symbols: list[str]) -> tuple[str, dict]:
    """
    Формирует интерактивное меню с чекбоксами для ручного выбора корзины.
    """
    market = market.lower()
    is_crypto = market == "crypto"
    market_title = "криптовалют" if is_crypto else "акций Мосбиржи"

    text = (
        f"📋 <b>РУЧНОЙ ВЫБОР КОРЗИНЫ ({market_title.upper()})</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Нажимайте на кнопки инструментов, чтобы включить (✅) или исключить (▫️) их из активной торговли.\n\n"
        f"<b>Выбрано сейчас ({len(selected_symbols)}):</b>\n"
        f"<code>{', '.join(selected_symbols) if selected_symbols else 'Ничего не выбрано'}</code>"
    )

    rows = []
    # Размещаем по 2 кнопки в ряд
    for i in range(0, len(all_symbols), 2):
        row = []
        for sym in all_symbols[i:i+2]:
            is_sel = sym in selected_symbols
            mark = "✅" if is_sel else "▫️"
            short_name = sym.replace("/USDT", "")
            btn_text = f"{mark} {short_name}"
            row.append({
                "text": btn_text,
                "callback_data": f"screen:toggle:{market}:{sym}",
            })
        rows.append(row)

    # Нижняя строка управления
    rows.append([
        {"text": f"💾 Сохранить и применить ({len(selected_symbols)})", "callback_data": f"screen:save:{market}"},
    ])
    rows.append([
        {"text": "🔙 Назад к лидерборду", "callback_data": f"screen:back:{market}"},
    ])

    return text, {"inline_keyboard": rows}


def main():
    parser = argparse.ArgumentParser(description="Universe Screener for ICT Toolkit")
    parser.add_argument("--crypto", action="store_true", help="Скрининг криптовалютного рынка (Bitget / Binance)")
    parser.add_argument("--moex", action="store_true", help="Скрининг акций Мосбиржи (Т-Банк)")
    parser.add_argument("--days", type=int, default=90, help="Глубина выборки в днях (по умолчанию 90)")
    parser.add_argument("--top", type=int, default=None, help="Количество лучших инструментов в корзине")
    parser.add_argument("--candidates", type=str, default=None, help="Список тикеров через запятую")
    parser.add_argument("--no-notify", action="store_true", help="Не отправлять отчет в Telegram с инлайн-кнопками")

    args = parser.parse_args()

    market = "moex" if args.moex else "crypto"
    top_n = args.top if args.top is not None else (4 if market == "crypto" else 5)
    cand_list = [s.strip().upper() for s in args.candidates.split(",")] if args.candidates else None

    report = run_screener(market=market, days=args.days, candidates=cand_list, top_n=top_n)

    if args.update_config and report.get("recommended_symbols"):
        rec = report["recommended_symbols"]
        print(f"\nОбновление config.py активной корзиной: {rec}...")
        ok = cfg.update_active_symbols(market, rec)
        if ok:
            print("✅ Конфигурация успешно обновлена.")
        else:
            print("❌ Ошибка обновления конфигурации.")

    if not args.no_notify:
        print("\nОтправка интерактивного отчета в Telegram...")
        msg_text, reply_markup = format_screener_telegram_message(report)
        # Отправляем через Bot API с клавиатурой
        from telegram_bot import send_bot_reply
        ok = send_bot_reply(msg_text, reply_markup=reply_markup)
        if ok:
            print("✅ Отчет успешно отправлен в Telegram.")
        else:
            print("⚠️ Не удалось отправить через send_bot_reply, пробуем send_telegram_message...")
            send_telegram_message(msg_text)


if __name__ == "__main__":
    main()
