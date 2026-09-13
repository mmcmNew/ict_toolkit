"""
Live/Demo/Sandbox торговый бот для T-Bank Invest API (t-tech-investments).

Поддерживает:
- Работу в официальной песочнице Т-Банка (--sandbox) на виртуальных рублях.
- Торговлю на реальном брокерском счете (--real) с подтверждением безопасности.
- Локальный Paper Trading (--paper) без отправки ордеров брокеру.
- Мониторинг одной акции (--ticker SBER) или всей корзины акций РФ (--all).
- Расчет размера позиции по риску (% от рублевого депозита) с учетом лотности акций MOEX.
- Контроль статуса биржевых торгов (Мосбиржа: проверка NORMAL_TRADING).
- Стратегию ICT Smart Money: 1H Bias + 5m Liquidity Sweep + FVG confirmation.
- Сопровождение сделок: частичный тейк 50% на 2.5R (настраивается через config / --tp-r) и трейлинг-стоп остатка на 0.8R.
- ИИ-валидацию сетапов через Google Gemini (--ai).
- Сбор статистики и истории сделок (--stats) в tbank_trade_log.json.

Запуск в песочнице (Sandbox) по всей корзине акций РФ с риском 1%:
  python tbank_trade.py --all --sandbox --risk 1.0

Запуск в песочнице по одной акции (Сбербанк):
  python tbank_trade.py --ticker SBER --sandbox --risk 1.0

Просмотр накопленной статистики и PnL:
  python tbank_trade.py --stats
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import warnings
try:
    from deprecation import DeprecatedWarning
    warnings.filterwarnings("ignore", category=DeprecatedWarning)
except Exception:
    pass
warnings.filterwarnings("ignore", message=".*deprecated.*")

import os
import time
import json
import uuid
import argparse
from datetime import datetime, timezone, timedelta
import pandas as pd
from smartmoneyconcepts import smc

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config as cfg
from data_sources import resolve_tbank_instrument
from strategy import resample, compute_bias_series, bias_at, in_killzone
from ict_advanced import compute_asian_ranges, is_asian_range_sweep
from ai_evaluator import evaluate_setup
from ai_circuit_breaker import is_pair_disabled_today, record_trade_and_check_circuit_breaker, evaluate_pre_trading_universe_ai
from trend_filter import is_market_trending
import telegram_notifier as tg
import chart_generator as cg
import pending_signals as ps

DATA_DIR = getattr(cfg, "DATA_DIR", "data")
SEEN_SIGNALS_PATH = getattr(cfg, "TBANK_SEEN_SIGNALS_FILE", os.path.join(DATA_DIR, "tbank_seen_signals.json"))
TRADE_LOG_PATH = getattr(cfg, "TBANK_TRADE_LOG_FILE", os.path.join(DATA_DIR, "tbank_trade_log.json"))
ACTIVE_POSITIONS_PATH = getattr(cfg, "TBANK_ACTIVE_POSITIONS_FILE", os.path.join(DATA_DIR, "tbank_active_positions.json"))

ROOT_SEEN_SIGNALS_PATH = "tbank_seen_signals.json"
ROOT_TRADE_LOG_PATH = "tbank_trade_log.json"
ROOT_ACTIVE_POSITIONS_PATH = "tbank_active_positions.json"
LOOKBACK_BARS_1M = 1000  # минутные свечи для построения 5m и 1h


def load_seen():
    path = SEEN_SIGNALS_PATH
    if not os.path.exists(path) and os.path.exists(ROOT_SEEN_SIGNALS_PATH):
        path = ROOT_SEEN_SIGNALS_PATH
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = set(json.load(f))
            if path != SEEN_SIGNALS_PATH and not os.path.exists(SEEN_SIGNALS_PATH):
                save_seen(data)
            return data
        except Exception:
            return set()
    return set()


def save_seen(seen):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(SEEN_SIGNALS_PATH)), exist_ok=True)
        with open(SEEN_SIGNALS_PATH, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, indent=2)
    except Exception as e:
        print(f"Предупреждение: не удалось сохранить seen signals: {e}")


def load_trades():
    path = TRADE_LOG_PATH
    if not os.path.exists(path) and os.path.exists(ROOT_TRADE_LOG_PATH):
        path = ROOT_TRADE_LOG_PATH
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if path != TRADE_LOG_PATH and not os.path.exists(TRADE_LOG_PATH):
                save_trades(data)
            return data
        except Exception:
            return []
    return []


def save_trades(trades):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(TRADE_LOG_PATH)), exist_ok=True)
        with open(TRADE_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(trades, f, indent=2, default=str)
    except Exception as e:
        print(f"Предупреждение: не удалось сохранить trade log: {e}")


def load_active_positions() -> dict:
    """Загружает сохраненные открытые позиции из JSON-файла состояния."""
    path = ACTIVE_POSITIONS_PATH
    if not os.path.exists(path) and os.path.exists(ROOT_ACTIVE_POSITIONS_PATH):
        path = ROOT_ACTIVE_POSITIONS_PATH
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if path != ACTIVE_POSITIONS_PATH and not os.path.exists(ACTIVE_POSITIONS_PATH):
                save_active_positions(data)
            return data
        except Exception as e:
            print(f"⚠️ Предупреждение: не удалось прочитать активные позиции: {e}")
            return {}
    return {}


def save_active_positions(positions: dict):
    """Сохраняет текущие открытые позиции в JSON-файл для персистентности между перезапусками."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(ACTIVE_POSITIONS_PATH)), exist_ok=True)
        with open(ACTIVE_POSITIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(positions, f, indent=2, default=str)
    except Exception as e:
        print(f"⚠️ Предупреждение: не удалось сохранить активные позиции: {e}")


def reconcile_positions_with_broker(client, account_id: str, active_positions: dict, meta_by_ticker: dict, sandbox: bool) -> dict:
    """
    Сверяет сохраненные активные позиции с реальным портфелем брокера.
    Если позиция была закрыта вручную или отсутствует на счете, удаляет ее из активных.
    """
    if not active_positions:
        return active_positions

    try:
        if sandbox:
            pos_resp = client.sandbox.get_sandbox_positions(account_id=account_id)
        else:
            pos_resp = client.operations.get_positions(account_id=account_id)

        # Карта FIGI -> количество акций в портфеле
        broker_shares = {}
        for s in pos_resp.securities:
            broker_shares[s.figi] = s.balance

        to_remove = []
        for ticker, p in active_positions.items():
            figi = meta_by_ticker.get(ticker, {}).get("figi")
            if not figi:
                continue
            balance = broker_shares.get(figi, 0)
            if balance <= 0:
                print(f"  ℹ️ [{ticker}] Позиция отсутствует в портфеле брокера (баланс: {balance}). Снята с сопровождения.")
                to_remove.append(ticker)
            else:
                print(f"  ✅ [{ticker}] Позиция подтверждена брокером: {balance} шт. в портфеле.")

        for t in to_remove:
            del active_positions[t]

        if to_remove:
            save_active_positions(active_positions)

    except Exception as e:
        print(f"  ⚠️ Не удалось сверить позиции с брокером: {e}. Используем сохраненное локальное состояние.")

    return active_positions


def show_stats():
    trades = load_trades()
    active_pos = load_active_positions()

    print("\n" + "=" * 85)
    print("           📊 СТАТИСТИКА ТОРГОВЛИ: T-BANK INVEST API (МОСБИРЖА)")
    print("=" * 85)

    if active_pos:
        print(f"📌 АКТИВНЫЕ ПОЗИЦИИ ({len(active_pos)}):")
        for t, p in active_pos.items():
            print(f"   • [{t}] {p.get('dir')} | {p.get('remaining_lots')}/{p.get('total_lots')} лот | Вход: {p.get('entry_price', 0):.2f} | SL: {p.get('current_stop', 0):.2f} | TP1: {p.get('tp1_price', 0):.2f}")
            sr = p.get("setup_reason")
            if sr:
                ai_str = f" | AI: {sr.get('ai_score')}/10" if sr.get('ai_score') is not None else ""
                print(f"     Причина входа: {sr.get('bias_desc', '')} свип @ {sr.get('sweep_price', 0):.2f} (FVG: [{sr.get('fvg_bottom', 0):.2f} - {sr.get('fvg_top', 0):.2f}]){ai_str}")
        print("-" * 85)
    else:
        print("Активных открытых позиций нет.")
        print("-" * 85)

    if not trades:
        print("Закрытых сделок пока нет.")
        print("=" * 85 + "\n")
        return

    df = pd.DataFrame(trades)
    closed = df[df["status"] == "CLOSED"]
    print(f"Всего закрытых сделок: {len(closed)}")

    if len(closed) > 0:
        net_r_total = closed["net_r"].sum() if "net_r" in closed.columns else 0.0
        net_pnl_total = closed["net_pnl_rub"].sum() if "net_pnl_rub" in closed.columns else 0.0
        wins = closed[closed["net_r"] > 0]
        losses = closed[closed["net_r"] <= 0]
        winrate = (len(wins) / len(closed)) * 100.0 if len(closed) else 0.0

        gross_win = wins["net_pnl_rub"].sum() if len(wins) else 0.0
        gross_loss = abs(losses["net_pnl_rub"].sum()) if len(losses) else 0.0
        pf = (gross_win / gross_loss) if gross_loss > 0 else (99.9 if gross_win > 0 else 0.0)

        print(f"Винрейт:              {winrate:.1f}% ({len(wins)}W / {len(losses)}L)")
        print(f"Суммарный Net R:      {net_r_total:+.2f}R")
        print(f"Чистый PnL (RUB):     {net_pnl_total:+,.2f} RUB")
        print(f"Profit Factor:        {pf:.2f}")

        print("\nПоследние 10 сделок:")
        recent = closed.tail(10)
        fmt = "{:<6} | {:<5} | {:>7} лот | Вход: {:>8.2f} | Выход: {:>8.2f} | Net R: {:>6.2f}R | Net PnL: {:>10.2f} RUB"
        for _, r in recent.iterrows():
            print(fmt.format(
                r.get("ticker", "N/A"),
                r.get("dir", "N/A"),
                r.get("lots", 0),
                r.get("entry_price", 0.0),
                r.get("exit_price", 0.0),
                r.get("net_r", 0.0),
                r.get("net_pnl_rub", 0.0)
            ))
            sr = r.get("setup_reason")
            if isinstance(sr, dict):
                ai_str = f" | AI: {sr.get('ai_score')}/10" if sr.get('ai_score') is not None else ""
                print(f"   ↳ Сетап: {sr.get('bias_desc', '')} свип @ {sr.get('sweep_price', 0):.2f} | FVG: [{sr.get('fvg_bottom', 0):.2f}-{sr.get('fvg_top', 0):.2f}]{ai_str}")
    print("=" * 85 + "\n")


def get_account_rub_balance(client, account_id: str, sandbox: bool) -> float:
    """Запрашивает свободный баланс в рублях."""
    try:
        if sandbox:
            pos = client.sandbox.get_sandbox_positions(account_id=account_id)
            for m in pos.money:
                if m.currency.lower() == "rub":
                    return float(m.units + m.nano / 1e9)
        else:
            pos = client.operations.get_positions(account_id=account_id)
            for m in pos.money:
                if m.currency.lower() == "rub":
                    return float(m.units + m.nano / 1e9)
    except Exception as e:
        print(f"  ⚠️ Не удалось получить баланс: {e}")
    return 100_000.0  # запасное значение для расчетов


def is_market_open(client=None, figi: str = "") -> bool:
    """
    Проверяет доступность биржевых торгов на Мосбирже (по времени МСК и расписанию торговых сессий).
    - Пн-Пт: Основная сессия 10:00 - 18:40 МСК, Вечерняя сессия 19:05 - 23:50 МСК
    - Выходные: закрыто
    """
    try:
        now_msk = datetime.now(timezone(timedelta(hours=3)))
        if now_msk.weekday() >= 5:  # Суббота (5) и Воскресенье (6)
            return False
        t_min = now_msk.hour * 60 + now_msk.minute
        # Основная сессия: 10:00 (600 мин) - 18:40 (1120 мин)
        # Вечерняя сессия: 19:05 (1145 мин) - 23:50 (1430 мин)
        if (600 <= t_min <= 1120) or (1145 <= t_min <= 1430):
            return True
        return False
    except Exception:
        return True


def get_moex_sleep_duration() -> tuple[int, str]:
    """
    Рассчитывает время сна до следующего открытия Мосбиржи (МСК).
    - Пн-Пт Основная сессия: 10:00 - 18:40 МСК
    - Пн-Пт Вечерняя сессия: 19:05 - 23:50 МСК
    - Выходные: закрыто до понедельника 10:00 МСК
    """
    now_msk = datetime.now(timezone(timedelta(hours=3)))
    weekday = now_msk.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    t_min = now_msk.hour * 60 + now_msk.minute

    # 1. Если выходные (суббота или воскресенье)
    if weekday == 5:  # Суббота
        days_ahead = 2
        target = now_msk.replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
        desc = "Понедельник 10:00 МСК"
    elif weekday == 6:  # Воскресенье
        days_ahead = 1
        target = now_msk.replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
        desc = "Понедельник 10:00 МСК"
    # 2. Пятница после вечерней сессии (23:50)
    elif weekday == 4 and t_min > 1430:
        days_ahead = 3
        target = now_msk.replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
        desc = "Понедельник 10:00 МСК"
    # 3. Будни до 10:00 МСК
    elif t_min < 600:
        target = now_msk.replace(hour=10, minute=0, second=0, microsecond=0)
        desc = f"сегодня {target.strftime('%H:%M')} МСК"
    # 4. Будни перерыв между сессиями (18:40 - 19:05 МСК)
    elif 1120 < t_min < 1145:
        target = now_msk.replace(hour=19, minute=5, second=0, microsecond=0)
        desc = f"сегодня {target.strftime('%H:%M')} МСК (Вечерняя сессия)"
    # 5. Будни после 23:50 МСК (ночь)
    else:
        target = now_msk.replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)
        desc = f"завтра 10:00 МСК"

    diff_sec = int((target - now_msk).total_seconds())
    return max(60, diff_sec), desc


def fetch_recent_candles(client, figi: str, interval, lookback_bars: int = 1000) -> pd.DataFrame:
    """Тянет свечи за последнее время для расчета индикаторов."""
    from t_tech.invest.utils import now
    from t_tech.invest import CandleInterval

    # Рассчитываем примерный временной интервал
    if interval == CandleInterval.CANDLE_INTERVAL_1_MIN:
        delta = timedelta(minutes=lookback_bars * 2)  # с запасом на выходные и ночи
    elif interval == CandleInterval.CANDLE_INTERVAL_5_MIN:
        delta = timedelta(minutes=lookback_bars * 10)
    elif interval == CandleInterval.CANDLE_INTERVAL_HOUR:
        delta = timedelta(hours=lookback_bars * 3)
    else:
        delta = timedelta(days=30)

    to_dt = now()
    from_dt = to_dt - delta

    try:
        candles = list(client.get_all_candles(
            instrument_id=figi, from_=from_dt, to=to_dt, interval=interval
        ))
    except TypeError:
        candles = list(client.get_all_candles(
            figi=figi, from_=from_dt, to=to_dt, interval=interval
        ))

    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    rows = []
    for c in candles:
        rows.append({
            "dt": c.time,
            "open": c.open.units + c.open.nano / 1e9,
            "high": c.high.units + c.high.nano / 1e9,
            "low": c.low.units + c.low.nano / 1e9,
            "close": c.close.units + c.close.nano / 1e9,
            "volume": c.volume,
        })

    df = pd.DataFrame(rows).set_index("dt")
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


def place_order(client, account_id: str, figi: str, direction_str: str,
                lots: int, sandbox: bool = True, paper: bool = False):
    """
    Отправляет рыночный ордер на покупку/продажу.
    """
    if paper:
        return {"order_id": f"paper_{uuid.uuid4().hex[:8]}", "status": "EXEC_PAPER"}

    from t_tech.invest.schemas import OrderDirection, OrderType, PostOrderRequest

    dir_enum = (
        OrderDirection.ORDER_DIRECTION_BUY if direction_str == "LONG"
        else OrderDirection.ORDER_DIRECTION_SELL
    )

    order_id = str(uuid.uuid4())
    req = PostOrderRequest(
        instrument_id=figi,
        quantity=lots,
        direction=dir_enum,
        account_id=account_id,
        order_type=OrderType.ORDER_TYPE_MARKET,
        order_id=order_id,
    )

    try:
        if sandbox:
            resp = client.sandbox.post_sandbox_order(
                account_id=account_id,
                instrument_id=figi,
                quantity=lots,
                direction=dir_enum,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=order_id
            )
            return {"order_id": resp.order_id, "status": resp.execution_report_status.name}
        else:
            resp = client.orders.post_order(
                account_id=account_id,
                instrument_id=figi,
                quantity=lots,
                direction=dir_enum,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=order_id
            )
            return {"order_id": resp.order_id, "status": resp.execution_report_status.name}
    except Exception as e:
        print(f"  ❌ Ошибка отправки ордера брокеру: {e}")
        return None


def run_tbank_bot(tickers: list, token: str, sandbox: bool, paper: bool,
                  risk_pct: float, use_ai: bool, use_asian: bool, use_kz: bool,
                  poll_sec: int, max_pos: int = 5, confirm: bool = False,
                  take_r: float = None):
    from t_tech.invest import Client, CandleInterval
    from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX

    target = INVEST_GRPC_API_SANDBOX if sandbox else INVEST_GRPC_API
    take_r = take_r if take_r is not None else getattr(cfg, "TBANK_PARTIAL_TAKE_R", 2.5)

    print("=" * 80)
    print("      ЗАПУСК ICT СМАРТ-МАНИ БОТА: T-BANK INVEST API (МОСБИРЖА)")
    print("=" * 80)
    mode_str = "📄 PAPER TRADING (эмуляция без ордеров)" if paper else (
        "🟡 ПЕСОЧНИЦА (Sandbox Т-Банк)" if sandbox else "🔴 РЕАЛЬНЫЙ СЧЕТ (LIVE)"
    )
    print(f"Режим работы:           {mode_str}")
    print(f"Корзина тикеров ({len(tickers)}):   {', '.join(tickers)}")
    print(f"Риск на сделку:         {risk_pct:.1f}% от баланса депозита")
    print(f"Тейк-профит (TP1):      {take_r:.1f}R (частичная фиксация 50%, трейлинг 0.8R)")
    print(f"Лимит позиций:          до {max_pos} одновременных сделок")
    print(f"Фильтр Asian Range:     {'ВКЛЮЧЕН' if use_asian else 'ВЫКЛЮЧЕН'}")
    print(f"Фильтр Киллзон (MOEX):  {'ВКЛЮЧЕН (10:00-14:00, 16:30-18:40 МСК)' if use_kz else 'ВЫКЛЮЧЕН'}")
    print(f"Подтверждение Telegram: {'ВКЛЮЧЕНО для всех сделок' if confirm else 'ВКЛЮЧЕНО для сделок вне Киллзон'}")
    print(f"Google Gemini ИИ-оценка:{'ВКЛЮЧЕНА' if use_ai else 'ВЫКЛЮЧЕНА'}")
    print(f"Интервал опроса:        {poll_sec} сек.")
    print("=" * 80)

    seen_signals = load_seen()
    active_positions = load_active_positions()
    if active_positions:
        print(f"\n🔄 Восстановлено активных позиций из файла состояния ({len(active_positions)}):")
        for t_saved, p_saved in active_positions.items():
            print(f"   • [{t_saved}] {p_saved.get('dir')} | {p_saved.get('remaining_lots')}/{p_saved.get('total_lots')} лот | Вход: {p_saved.get('entry_price', 0):.2f} | SL: {p_saved.get('current_stop', 0):.2f} | TP1: {p_saved.get('tp1_price', 0):.2f}")
            sr = p_saved.get("setup_reason")
            if sr:
                ai_str = f" | AI: {sr.get('ai_score')}/10" if sr.get('ai_score') is not None else ""
                print(f"     Причина входа: {sr.get('bias_desc', '')} свип @ {sr.get('sweep_price', 0):.2f} (FVG: [{sr.get('fvg_bottom', 0):.2f} - {sr.get('fvg_top', 0):.2f}]){ai_str}")
        print()

    with Client(token, target=target) as client:
        # Определение рабочего счета
        account_id = getattr(cfg, "TBANK_ACCOUNT_ID", "")
        if not account_id:
            if sandbox:
                sb_accounts = client.sandbox.get_sandbox_accounts().accounts
                if not sb_accounts:
                    print("Создание виртуального счёта в песочнице...")
                    sb_account = client.sandbox.open_sandbox_account()
                    account_id = sb_account.account_id
                    print(f"Создан виртуальный счёт: {account_id}")
                    print("Пополнение счёта на 1,000,000 руб...")
                    client.sandbox.sandbox_pay_in(
                        account_id=account_id,
                        amount={"currency": "rub", "units": 1000000, "nano": 0}
                    )
                else:
                    account_id = sb_accounts[0].id
            else:
                real_accounts = client.users.get_accounts().accounts
                if real_accounts:
                    account_id = real_accounts[0].id

        print(f"Используется счёт ID: {account_id}\n")

        meta_by_ticker = {}
        for t in tickers:
            try:
                meta = resolve_tbank_instrument(client, t)
                meta_by_ticker[t] = meta
            except Exception as e:
                print(f"⚠️ Ошибка резолвинга тикера {t}: {e}")

        # Сверка активных позиций с портфелем брокера
        reconcile_positions_with_broker(client, account_id, active_positions, meta_by_ticker, sandbox)

        init_balance = get_account_rub_balance(client, account_id, sandbox)
        print(f"\nТекущий свободный баланс: {init_balance:,.2f} RUB")
        print("Начинаю мониторинг рынка...\n")

        tg.notify_bot_started(
            bot_name="T-Bank Invest API (Мосбиржа)",
            mode=mode_str,
            symbols=tickers,
            risk_pct=risk_pct,
            max_pos=max_pos,
        )

        # Пре-маркет ИИ-оценка рыночного режима по акциям РФ
        try:
            evaluate_pre_trading_universe_ai(tickers, market="moex", send_tg=True)
        except Exception as e:
            print(f"⚠️ Ошибка пре-маркет ИИ-оценки акций: {e}")

        try:
            iteration = 0
            while True:
                iteration += 1
                now_utc = datetime.now(timezone.utc)
                now_str = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

                # Динамическая синхронизация корзины акций РФ из data/active_universe.json
                try:
                    u_file = getattr(cfg, "ACTIVE_UNIVERSE_FILE", "data/active_universe.json")
                    if os.path.exists(u_file):
                        with open(u_file, "r", encoding="utf-8") as _uf:
                            _u_data = json.load(_uf)
                        _moex_syms = _u_data.get("moex", [])
                        if _moex_syms and set(_moex_syms) != set(tickers):
                            print(f"\n🔄 [Universe Update] Применена обновленная корзина MOEX из Telegram: {_moex_syms}\n")
                            tickers = _moex_syms
                except Exception:
                    pass

                print(f"[{now_str}] Итерация #{iteration} | Позиций открыто: {len(active_positions)}/{max_pos}")

                # 0. Исполнение сигналов, подтвержденных пользователем в Telegram
                approved_signals = ps.get_approved_signals(market="tbank")
                for sig in approved_signals:
                    sig_id = sig["id"]
                    t = sig["symbol"]
                    direction = "LONG" if sig["direction"] == 1 else "SHORT"
                    pos_calc = sig["pos_calc"]
                    setup_reason = sig["setup_reason"]
                    figi = pos_calc.get("figi") or meta_by_ticker.get(t, {}).get("figi")
                    lots = pos_calc.get("lots", 1)
                    inst = meta_by_ticker.get(t, {})
                    if not figi or not inst:
                        continue

                    print(f"\n🚀 [TELEGRAM ОДОБРЕНИЕ] Пользователь подтвердил вход в акцию {t} ({direction})!")
                    try:
                        p_resp = client.market_data.get_last_prices(instrument_id=[figi])
                        curr_price = float(p_resp.last_prices[0].price.units + p_resp.last_prices[0].price.nano / 1e9)

                        # Проверка инвалидации по SL / TP перед входом
                        if direction == "LONG":
                            if curr_price <= sig["stop_loss"]:
                                print(f"⚠️ [Инвалидация] Цена {curr_price:.2f} <= SL {sig['stop_loss']:.2f} на {t}. Ордер отменен.")
                                ps.mark_failed(sig_id, f"Инвалидация: цена ({curr_price:.2f}) пробила SL ({sig['stop_loss']:.2f})")
                                continue
                            if curr_price >= sig["take_profit"]:
                                print(f"⚠️ [Инвалидация] Цена {curr_price:.2f} >= TP {sig['take_profit']:.2f} на {t}. Ордер отменен.")
                                ps.mark_failed(sig_id, f"Инвалидация: цена ({curr_price:.2f}) уже достигла TP ({sig['take_profit']:.2f})")
                                continue
                        else:
                            if curr_price >= sig["stop_loss"]:
                                print(f"⚠️ [Инвалидация] Цена {curr_price:.2f} >= SL {sig['stop_loss']:.2f} на {t}. Ордер отменен.")
                                ps.mark_failed(sig_id, f"Инвалидация: цена ({curr_price:.2f}) пробила SL ({sig['stop_loss']:.2f})")
                                continue
                            if curr_price <= sig["take_profit"]:
                                print(f"⚠️ [Инвалидация] Цена {curr_price:.2f} <= TP {sig['take_profit']:.2f} на {t}. Ордер отменен.")
                                ps.mark_failed(sig_id, f"Инвалидация: цена ({curr_price:.2f}) уже достигла TP ({sig['take_profit']:.2f})")
                                continue

                        slip_pct = abs(curr_price - sig["entry_price"]) / max(sig["entry_price"], 0.01) * 100
                        if slip_pct > 0.8:
                            print(f"⚠️ [Отклонено] Проскальзывание {slip_pct:.2f}% > 0.8% на {t}. Ордер отменен.")
                            ps.mark_failed(sig_id, f"Проскальзывание {slip_pct:.2f}%")
                            continue

                        order_res = place_order(client, account_id, figi, direction, lots, sandbox, paper)
                        if order_res:
                            risk_distance = abs(curr_price - sig["stop_loss"])
                            tp1_target = curr_price + (take_r * risk_distance) if direction == "LONG" else curr_price - (take_r * risk_distance)
                            active_positions[t] = {
                                "ticker": t,
                                "dir": direction,
                                "entry_price": curr_price,
                                "current_stop": sig["stop_loss"],
                                "tp1_price": tp1_target,
                                "take_r": take_r,
                                "r_unit": risk_distance,
                                "total_lots": lots,
                                "remaining_lots": lots,
                                "partial_taken": False,
                                "open_time": str(now_utc),
                                "setup_reason": setup_reason,
                            }
                            save_active_positions(active_positions)
                            ps.mark_executed(sig_id, {"fill_price": curr_price, "order_res": str(order_res)})
                            tg.notify_trade_opened(
                                market="T-Bank MOEX",
                                symbol=t,
                                direction=direction,
                                entry_price=curr_price,
                                stop_loss=sig["stop_loss"],
                                take_profit=tp1_target,
                                amount_str=f"{lots} лот ({lots * inst['lot']} шт.)",
                                risk_str=f"{risk_pct:.1f}%",
                                setup_reason=setup_reason,
                            )
                            print(f"✅ [УСПЕХ] Ордер по акции {t} успешно открыт через Telegram-подтверждение!\n")
                    except Exception as e:
                        print(f"❌ Ошибка исполнения подтвержденного ордера {t}: {e}")
                        ps.mark_failed(sig_id, str(e))

                # 1. Сопровождение открытых позиций
                for t, pos in list(active_positions.items()):
                    inst = meta_by_ticker[t]
                    # Запрос последней цены
                    try:
                        p_resp = client.market_data.get_last_prices(instrument_id=[inst["figi"]])
                        if not p_resp.last_prices:
                            continue
                        last_q = p_resp.last_prices[0].price
                        curr_price = float(last_q.units + last_q.nano / 1e9)
                    except Exception:
                        continue

                    # Проверка условий выхода
                    entry_p = pos["entry_price"]
                    stop_p = pos["current_stop"]
                    tp1_p = pos["tp1_price"]
                    pos_dir = pos["dir"]
                    r_unit = pos["r_unit"]

                    # Логика для LONG
                    if pos_dir == "LONG":
                        # Стоп-лосс
                        if curr_price <= stop_p:
                            print(f"  🛑 [{t}] СТОП-ЛОСС СРАБОТАЛ на {curr_price:.2f} (SL: {stop_p:.2f})!")
                            close_lots = pos["remaining_lots"]
                            place_order(client, account_id, inst["figi"], "SHORT", close_lots, sandbox, paper)
                            net_r = (curr_price - entry_p) / r_unit
                            pnl_rub = (curr_price - entry_p) * close_lots * inst["lot"]
                            record = {
                                "status": "CLOSED",
                                "ticker": t,
                                "dir": "LONG",
                                "lots": pos["total_lots"],
                                "entry_price": entry_p,
                                "exit_price": curr_price,
                                "exit_reason": "STOP_LOSS",
                                "net_r": round(net_r, 2),
                                "net_pnl_rub": round(pnl_rub, 2),
                                "time": str(now_utc),
                                "setup_reason": pos.get("setup_reason")
                            }
                            trades = load_trades()
                            trades.append(record)
                            save_trades(trades)
                            del active_positions[t]
                            save_active_positions(active_positions)
                            tg.notify_trade_closed(
                                market="T-Bank MOEX",
                                symbol=t,
                                direction="LONG",
                                exit_price=curr_price,
                                exit_reason="STOP_LOSS",
                                net_pnl=pnl_rub,
                                currency="RUB",
                                net_r=net_r,
                            )
                            record_trade_and_check_circuit_breaker(t, outcome=exit_reason, r_net=net_r, exit_time=str(now_utc))
                            continue

                        # Частичный тейк (take_r, 50%)
                        pos_take_r = pos.get("take_r", take_r)
                        if not pos["partial_taken"] and curr_price >= tp1_p:
                            take_size = getattr(cfg, "TBANK_PARTIAL_TAKE_SIZE", 0.5)
                            take_lots = max(1, int(pos["total_lots"] * take_size))
                            print(f"  🎯 [{t}] ЧАСТИЧНЫЙ ТЕЙК ({pos_take_r:.1f}R) достигнут на {curr_price:.2f}! Фиксация {take_lots} лот.")
                            place_order(client, account_id, inst["figi"], "SHORT", take_lots, sandbox, paper)
                            pos["partial_taken"] = True
                            pos["remaining_lots"] -= take_lots
                            # Перенос стопа в безубыток с учетом комиссий брокера и биржи (True BE)
                            be_buffer = entry_p * (cfg.TBANK_COMMISSION_PCT * 2.2)
                            pos["current_stop"] = entry_p + be_buffer
                            print(f"  🛡️ [{t}] Стоп перенесён в безубыток (+комиссии): {pos['current_stop']:.2f}")
                            save_active_positions(active_positions)
                            tg.notify_partial_take(
                                market="T-Bank MOEX",
                                symbol=t,
                                direction="LONG",
                                fill_price=curr_price,
                                closed_str=f"{take_lots} лот ({take_lots * inst['lot']} шт.)",
                                remaining_str=f"{pos['remaining_lots']} лот ({pos['remaining_lots'] * inst['lot']} шт.)",
                                be_stop=pos["current_stop"],
                            )

                        # Трейлинг остатка
                        if pos["partial_taken"]:
                            trail_dist_r = getattr(cfg, "TBANK_TRAIL_DISTANCE_R", 0.8)
                            new_trail = curr_price - (trail_dist_r * r_unit)
                            if new_trail > pos["current_stop"]:
                                pos["current_stop"] = new_trail
                                save_active_positions(active_positions)

                # 2. Проверка биржевого расписания Мосбиржи (Smart Sleep)
                if not paper and not is_market_open(client):
                    if len(active_positions) == 0:
                        sleep_sec, open_desc = get_moex_sleep_duration()
                        mins_left = sleep_sec // 60
                        print(f"\n🌙 [MOEX] Торги закрыты (открытие: {open_desc}).")
                        print(f"   Открытых позиций нет. Бот переходит в спящий режим на {mins_left} мин. (до {open_desc})...\n")

                        wake_target = time.time() + sleep_sec - 120
                        while time.time() < wake_target:
                            time.sleep(min(60, wake_target - time.time()))
                        print(f"\n⏰ [MOEX] Пробуждение к открытию торгов ({open_desc})! Начинаю сканирование...")
                        continue

                # 3. Поиск новых входов по корзине
                if len(active_positions) >= max_pos:
                    print(f"  ℹ️ Лимит открытых позиций ({max_pos}) достигнут. Ожидание сопровождения...")
                    time.sleep(poll_sec)
                    continue

                # Проверка исключенных дней недели (например, Четверг - экспирации FORTS)
                now_msk_dt = now_utc + pd.Timedelta(hours=3)
                current_dow = now_msk_dt.strftime("%A")
                exclude_days = getattr(cfg, "MOEX_EXCLUDE_DAYS", ["Thursday"])
                if current_dow in exclude_days:
                    sys.stdout.write(f"\r[MOEX {now_msk}] ⏸️ {current_dow} исключен из торговли (день экспираций). Мониторинг позиций...   ")
                    sys.stdout.flush()
                    time.sleep(poll_sec)
                    continue

                for t in tickers:
                    if t in active_positions:
                        continue
                    is_dis, dis_reason = is_pair_disabled_today(t)
                    if is_dis:
                        continue

                    inst = meta_by_ticker[t]
                    figi = inst["figi"]

                    # Проверка биржевого расписания
                    if not paper and not is_market_open(client, figi):
                        # Рынок закрыт (ночь или выходные)
                        continue

                    # Загрузка свечей 1m
                    df_1m = fetch_recent_candles(client, figi, CandleInterval.CANDLE_INTERVAL_1_MIN, LOOKBACK_BARS_1M)
                    if len(df_1m) < 100:
                        continue

                    # Ресемплинг
                    df_htf = resample(df_1m, cfg.HTF_RULE)
                    df_ltf = resample(df_1m, cfg.LTF_RULE)

                    if len(df_htf) < cfg.SWING_LENGTH_HTF * 2 or len(df_ltf) < cfg.SWING_LENGTH_LTF * 2:
                        continue

                    # Расчет 1H Bias
                    bias_series = compute_bias_series(df_htf, cfg.SWING_LENGTH_HTF)
                    curr_bias = bias_at(bias_series, df_ltf.index[-1])
                    if curr_bias == 0:
                        continue

                    # Для акций на Мосбирже без маржинального шорта торгуем только LONG
                    if curr_bias == -1:
                        continue

                    # Проверка LTF Swings & Sweeps
                    swings_ltf = smc.swing_highs_lows(df_ltf, swing_length=cfg.SWING_LENGTH_LTF)
                    liq_ltf = smc.liquidity(df_ltf, swings_ltf, range_percent=cfg.LIQUIDITY_RANGE_PCT)
                    liq_ltf.index = df_ltf.index
                    swept = liq_ltf[(liq_ltf["Swept"].notna()) & (liq_ltf["Swept"] != 0)]

                    if len(swept) == 0:
                        continue

                    last_sweep = swept.iloc[-1]
                    swept_bar_idx = int(last_sweep["Swept"])
                    if swept_bar_idx <= 0 or swept_bar_idx >= len(df_ltf):
                        continue

                    last_sweep_time = df_ltf.index[swept_bar_idx]
                    expected_dir = 1 if last_sweep["Liquidity"] == -1 else -1

                    # Свип должен совпадать с 1H трендом
                    if expected_dir != curr_bias:
                        continue

                    sweep_candle = df_ltf.iloc[swept_bar_idx]

                    # Проверка фильтра тренда против бокового распила (Anti-Flat Filter)
                    if getattr(cfg, "USE_TREND_FILTER", True):
                        cand_meta = {
                            "fvg_candle": sweep_candle.to_dict(),
                            "confirm_time": last_sweep_time,
                            "expected_dir": curr_bias,
                        }
                        trend_ok, trend_reason, _ = is_market_trending(df_htf, cand_meta, min_adx=getattr(cfg, "MIN_ADX_1H", 20.0))
                        if not trend_ok:
                            continue

                    # Проверка FVG
                    fvg_ltf = smc.fvg(df_ltf)
                    fvg_ltf.index = df_ltf.index
                    recent_fvgs = fvg_ltf[fvg_ltf.index >= last_sweep_time]
                    matching_fvgs = recent_fvgs[recent_fvgs["FVG"] == curr_bias]

                    if len(matching_fvgs) == 0:
                        continue

                    best_fvg = matching_fvgs.iloc[-1]
                    fvg_time = matching_fvgs.index[-1]
                    fvg_top, fvg_bottom = float(best_fvg["Top"]), float(best_fvg["Bottom"])
                    zone_pct = abs(fvg_top - fvg_bottom) / max(fvg_bottom, 0.0001)
                    min_fvg = getattr(cfg, "MIN_FVG_ZONE_PCT", 0.0005)
                    if zone_pct < min_fvg:
                        continue

                    sig_id = f"{t}_{fvg_time}_{curr_bias}"

                    if sig_id in seen_signals:
                        continue

                    # Параметры сделки
                    direction = "LONG" if curr_bias == 1 else "SHORT"
                    fill_price = float(df_1m["close"].iloc[-1])
                    buffer = fill_price * 0.0008

                    if direction == "LONG":
                        stop_loss = float(sweep_candle["low"]) - buffer
                        risk_distance = fill_price - stop_loss
                    else:
                        stop_loss = float(sweep_candle["high"]) + buffer
                        risk_distance = stop_loss - fill_price

                    if risk_distance <= 0 or risk_distance / fill_price < cfg.MIN_RISK_PCT:
                        continue

                    # Проверка Киллзон (MOEX)
                    is_kz = in_killzone(now_utc, cfg.MOEX_KILLZONES) if use_kz else True

                    # ИИ-валидация через Gemini (при включении)
                    if use_ai:
                        eval_payload = {
                            "symbol": t,
                            "expected_dir": 1 if direction == "LONG" else -1,
                            "sweep_time": str(last_sweep_time),
                            "bias": curr_bias,
                            "in_killzone": bool(use_kz and is_kz),
                            "zone_pct": abs(best_fvg["Top"] - best_fvg["Bottom"]) / fill_price,
                            "risk_pct": risk_distance / fill_price,
                        }
                        ai_eval = evaluate_setup(eval_payload)
                        ai_score = ai_eval.get("score", 5)
                        ai_reason = ai_eval.get("reasoning", "")
                        print(f"  🧠 AI Evaluator для {t}: Оценка {ai_score}/10 | {ai_reason}")
                        if ai_score < cfg.AI_CONFIDENCE_THRESHOLD:
                            print(f"  ⚠️ Сетап {t} отклонён ИИ (балл {ai_score} < {cfg.AI_CONFIDENCE_THRESHOLD})")
                            seen_signals.add(sig_id)
                            save_seen(seen_signals)
                            continue

                    # Расчет лотности по риску
                    curr_balance = get_account_rub_balance(client, account_id, sandbox)
                    risk_amount_rub = curr_balance * (risk_pct / 100.0)
                    share_cost_risk = risk_distance
                    total_shares = risk_amount_rub / share_cost_risk
                    lots = max(1, int(total_shares // inst["lot"]))

                    total_trade_cost = lots * inst["lot"] * fill_price
                    if total_trade_cost > curr_balance:
                        lots = max(1, int(curr_balance // (inst["lot"] * fill_price)))
                        total_trade_cost = lots * inst["lot"] * fill_price

                    if total_trade_cost > curr_balance:
                        req_rub = inst["lot"] * fill_price
                        print(f"\n⚠️ [{t}] СИГНАЛ ПРОПУЩЕН: НЕ ХВАТАЕТ РУБЛЕЙ НА СЧЁТЕ!")
                        print(f"   Сетап:            {direction} по {fill_price:.2f} RUB")
                        print(f"   Требуется на 1 лот ({inst['lot']} шт.): {req_rub:.2f} RUB")
                        print(f"   Доступно на счете: {curr_balance:.2f} RUB")
                        print(f"   💡 Подсказка:     Пополните счет на {(req_rub - curr_balance):.2f}+ RUB или закройте позицию для входа.\n")
                        tg.notify_margin_warning(
                            market="T-Bank MOEX",
                            symbol=t,
                            direction=direction,
                            price=fill_price,
                            required_amount=req_rub,
                            available_amount=curr_balance,
                            currency="RUB",
                            hint=f"Пополните счет на {(req_rub - curr_balance):.2f}+ RUB или закройте позицию для входа.",
                        )
                        seen_signals.add(sig_id)
                        save_seen(seen_signals)
                        continue

                    # Проверка Киллзон и подтверждения через Telegram
                    is_kz = in_killzone(now_utc, cfg.MOEX_KILLZONES) if use_kz else True
                    need_confirm = confirm or (use_kz and not is_kz)

                    # Формирование 3-TF скриншота графика
                    chart_bytes = None
                    tp1_target = fill_price + (take_r * risk_distance) if direction == "LONG" else fill_price - (take_r * risk_distance)
                    try:
                        df_15m = resample(df_1m, "15min")
                        chart_bytes = cg.generate_3tf_setup_chart(
                            df_1h=df_htf,
                            df_15m=df_15m,
                            df_5m=df_ltf,
                            symbol=t,
                            direction=direction,
                            entry_price=fill_price,
                            stop_loss=stop_loss,
                            take_profit=tp1_target,
                            sweep_price=float(sweep_candle["low"] if direction == "LONG" else sweep_candle["high"]),
                            fvg_bottom=float(best_fvg["Bottom"]),
                            fvg_top=float(best_fvg["Top"]),
                            bias_desc="BULLISH" if curr_bias == 1 else "BEARISH",
                        )
                    except Exception as e:
                        print(f"⚠️ Ошибка формирования графика 3-TF: {e}")

                    setup_reason = {
                        "bias": curr_bias,
                        "bias_desc": "BULLISH" if curr_bias == 1 else "BEARISH",
                        "sweep_time": str(last_sweep_time),
                        "sweep_price": float(sweep_candle["low"] if direction == "LONG" else sweep_candle["high"]),
                        "sweep_dir": expected_dir,
                        "fvg_bottom": float(best_fvg["Bottom"]),
                        "fvg_top": float(best_fvg["Top"]),
                        "fvg_time": str(fvg_time),
                        "risk_distance": float(risk_distance),
                        "risk_distance_pct": round(float(risk_distance / fill_price) * 100, 2),
                        "asian_range_sweep": bool(use_asian),
                        "in_killzone": bool(use_kz and is_kz),
                        "ai_score": ai_score if use_ai else None,
                        "ai_reason": ai_reason if use_ai else None,
                    }

                    if need_confirm:
                        session_status = "ВНЕ КИЛЛЗОНЫ МОСБИРЖИ" if not is_kz else "РЕЖИМ ПОДТВЕРЖДЕНИЯ"
                        print(f"\n💡 [{session_status}] Сетап {t} ({direction}) по {fill_price:.2f} RUB отправлен в Telegram.")
                        print(f"   Скриншот 3-TF и кнопки подтверждения направлены пользователю.\n")
                        sig_id_ps = ps.create_pending_signal(
                            market="tbank",
                            symbol=t,
                            direction=1 if direction == "LONG" else -1,
                            entry_price=fill_price,
                            stop_loss=stop_loss,
                            take_profit=tp1_target,
                            pos_calc={"lots": lots, "shares": lots * inst["lot"], "cost_rub": total_trade_cost, "figi": figi},
                            setup_reason=setup_reason,
                        )
                        msg_id = tg.notify_setup_proposal(
                            market="T-Bank MOEX",
                            symbol=t,
                            direction=direction,
                            entry_price=fill_price,
                            stop_loss=stop_loss,
                            take_profit=tp1_target,
                            amount_str=f"{lots} лот ({lots * inst['lot']} шт.) ~{total_trade_cost:,.2f} RUB",
                            risk_str=f"{risk_pct:.1f}% (~{risk_amount_rub:,.2f} RUB)",
                            setup_reason=setup_reason,
                            chart_bytes=chart_bytes,
                            sig_id=sig_id_ps,
                            is_off_session=bool(use_kz and not is_kz),
                        )
                        if msg_id:
                            ps.set_signal_message_id(sig_id_ps, msg_id)
                        seen_signals.add(sig_id)
                        save_seen(seen_signals)
                        continue

                    # Прямой вход в сделку внутри Киллзоны
                    print("\n" + "*" * 75)
                    print(f"  🔥 СИГНАЛ ICT НА ВХОД: {t} {direction}!")
                    print(f"  Цена входа:        {fill_price:.2f} RUB")
                    print(f"  Стоп-лосс:         {stop_loss:.2f} RUB (дистанция: {risk_distance:.2f} RUB / {(risk_distance/fill_price)*100:.2f}%)")
                    print(f"  Тейк {take_r:.1f}R (50%):   {tp1_target:.2f} RUB")
                    print(f"  Объем:             {lots} лот ({lots * inst['lot']} акций) = {total_trade_cost:,.2f} RUB")
                    print("*" * 75 + "\n")

                    order_res = place_order(client, account_id, figi, direction, lots, sandbox, paper)
                    if order_res:
                        active_positions[t] = {
                            "ticker": t,
                            "dir": direction,
                            "entry_price": fill_price,
                            "current_stop": stop_loss,
                            "tp1_price": tp1_target,
                            "take_r": take_r,
                            "r_unit": risk_distance,
                            "total_lots": lots,
                            "remaining_lots": lots,
                            "partial_taken": False,
                            "open_time": str(now_utc),
                            "setup_reason": setup_reason
                        }
                        save_active_positions(active_positions)
                        seen_signals.add(sig_id)
                        save_seen(seen_signals)
                        tg.notify_trade_opened(
                            market="T-Bank MOEX",
                            symbol=t,
                            direction=direction,
                            entry_price=fill_price,
                            stop_loss=stop_loss,
                            take_profit=tp1_target,
                            amount_str=f"{lots} лот ({lots * inst['lot']} шт.) ~{total_trade_cost:,.2f} RUB",
                            risk_str=f"{risk_pct:.1f}% (~{risk_amount_rub:,.2f} RUB)",
                            setup_reason=setup_reason,
                        )
                        if chart_bytes:
                            tg.send_telegram_photo(chart_bytes, caption=f"📸 <b>3-TF График открытой сделки MOEX:</b> <code>{t}</code>")

                # Реактивный сон
                for _ in range(max(1, poll_sec)):
                    if ps.get_approved_signals(market="tbank"):
                        break
                    time.sleep(1)


        except KeyboardInterrupt:
            print("\n⏹️ Бот остановлен пользователем (Ctrl+C).")
            show_stats()


def main():
    parser = argparse.ArgumentParser(description="ICT Trading Bot for T-Bank (Т-Инвестиции)")
    parser.add_argument("--ticker", type=str, default="", help="Одиночный тикер (например SBER)")
    parser.add_argument("--tickers", type=str, default="", help="Список тикеров через запятую (например SBER,GAZP,LKOH)")
    parser.add_argument("--all", action="store_true", help="Торговать всей корзиной из config.TBANK_TICKERS")
    parser.add_argument("--sandbox", action="store_true", default=None, help="Режим песочницы (виртуальные деньги)")
    parser.add_argument("--real", action="store_true", help="Реальный брокерский счет (РЕАЛЬНЫЕ ДЕНЬГИ)")
    parser.add_argument("--paper", action="store_true", help="Локальная эмуляция без отправки ордеров")
    parser.add_argument("--risk", type=float, default=1.0, help="Риск на сделку в %% от депозита (по умолчанию 1.0)")
    parser.add_argument("--ai", action="store_true", help="Включить Gemini ИИ-оценку сетапов")
    parser.add_argument("--asian-range", action="store_true", help="Фильтр азиатской сессии")
    parser.add_argument("--killzones", action="store_true", help="Фильтр торговых часов")
    parser.add_argument("--confirm", action="store_true", help="Запрашивать подтверждение сделок через Telegram для всех сделок со скриншотом 3-TF")
    parser.add_argument("--tp-r", type=float, default=getattr(cfg, "TBANK_PARTIAL_TAKE_R", 2.5), help="Тейк-профит в R (по умолчанию: 2.5R для акций РФ)")
    parser.add_argument("--max-pos", type=int, default=getattr(cfg, "TBANK_MAX_POSITIONS", 5), help="Максимум одновременно открытых позиций (по умолчанию 5)")
    parser.add_argument("--poll", type=int, default=60, help="Периодичность проверки рынка в секундах (по умолчанию 60)")
    parser.add_argument("-y", "--yes", action="store_true", help="Автоматическое подтверждение запуска на реальном счете")
    parser.add_argument("--stats", action="store_true", help="Вывести статистику истории сделок и выйти")

    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    # Определение режима
    if args.paper:
        paper = True
        sandbox = True
    elif args.real:
        paper = False
        sandbox = False
        print("\n" + "!" * 75)
        print("ВНИМАНИЕ: Вы выбрали запуск на РЕАЛЬНОМ брокерском счёте Т-Банка!")
        print("Сделки будут выводиться на Мосбиржу на РЕАЛЬНЫЕ ДЕНЬГИ.")
        print("!" * 75)
        if not args.yes:
            confirm = input("Для подтверждения запуска введите 'ДА': ")
            if confirm.strip() != "ДА":
                print("Запуск отменён.")
                return
        else:
            print("✅ Запуск на реальном счете подтвержден флагом --yes.")
    elif args.sandbox:
        paper = False
        sandbox = True
    else:
        paper = False
        sandbox = getattr(cfg, "TBANK_SANDBOX", True)

    token = (
        getattr(cfg, "TBANK_TOKEN", "")
        or os.environ.get("TBANK_TOKEN", "")
        or os.environ.get("TINKOFF_TOKEN", "")
        or os.environ.get("INVEST_TOKEN", "")
    )

    if not token or token.startswith("your_"):
        print("❌ Ошибка: Не задан TBANK_TOKEN в .env / config.py!")
        print("Для получения свечей и котировок из Т-Банка требуется API-токен.")
        print("Запустите 'python test_tbank.py' для справки по созданию токена.")
        return

    # Определение списка тикеров
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif args.ticker:
        tickers = [args.ticker.strip().upper()]
    elif args.all:
        tickers = getattr(cfg, "TBANK_TICKERS", ["SBER", "GAZP", "LKOH", "ROSN", "YDEX"])
    else:
        tickers = [getattr(cfg, "TBANK_TICKER", "SBER")]

    run_tbank_bot(
        tickers=tickers,
        token=token,
        sandbox=sandbox,
        paper=paper,
        risk_pct=args.risk,
        use_ai=args.ai or getattr(cfg, "ENABLE_AI_EVALUATION", False),
        use_asian=args.asian_range or getattr(cfg, "USE_ASIAN_RANGE_FILTER", False),
        use_kz=args.killzones or getattr(cfg, "USE_KILLZONES", False),
        poll_sec=args.poll,
        max_pos=args.max_pos,
        confirm=args.confirm,
        take_r=args.tp_r,
    )


if __name__ == "__main__":
    main()
