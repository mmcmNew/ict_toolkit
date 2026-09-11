"""
Диагностическая утилита для проверки подключения к T-Bank Invest API (t-tech-investments).

Проверяет:
1. Корректность токена и gRPC-соединения.
2. Доступные счета (Песочница или Боевой счет) и рублевый баланс.
3. В песочнице: при необходимости создает счет и пополняет виртуальный баланс.
4. Поиск и метаданные тикеров (SBER, GAZP, LKOH, ROSN, YDEX, T): лотность, шаг цены, FIGI.
5. Загрузку рыночных котировок и последних свечей.

Запуск:
  python test_tbank.py
  python test_tbank.py --sandbox
  python test_tbank.py --real
  python test_tbank.py --tickers SBER,GAZP,LKOH,T
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
import argparse
from datetime import timedelta
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config as cfg
from data_sources import resolve_tbank_instrument


def format_money(units: int, nano: int, currency: str = "rub") -> str:
    val = units + nano / 1e9
    return f"{val:,.2f} {currency.upper()}"


def test_tbank_connection(token: str, sandbox: bool = True, tickers: list = None,
                          fund_sandbox: bool = False, fund_amount: int = 100_000):
    print("=" * 75)
    print("      ДИАГНОСТИКА ПОДКЛЮЧЕНИЯ К T-BANK INVEST API (Т-ИНВЕСТИЦИИ)")
    print("=" * 75)

    if not token or token.startswith("your_"):
        print("\n❌ ОШИБКА: Токен Т-Банка не задан!")
        print("\nКак получить токен:")
        print("  1. Откройте личный кабинет Т-Банка: https://www.tbank.ru/invest/")
        print("  2. Перейдите в 'Настройки' -> 'Доступ к API'")
        print("  3. Выпустите 'Токен для T-Invest API' (для песочницы или полный)")
        print("  4. Добавьте его в файл .env:")
        print("     TBANK_TOKEN=t.xxxxxxxxxxxxxxxxxxxxxx")
        print("     TBANK_SANDBOX=True")
        return False

    masked_token = token[:6] + "..." + token[-4:] if len(token) > 12 else "***"
    print(f"  • Токен: {masked_token}")
    print(f"  • Режим: {'🟡 ПЕСОЧНИЦА (SANDBOX - виртуальные деньги)' if sandbox else '🔴 БОЕВОЙ (РЕАЛЬНЫЙ СЧЕТ)'}")

    try:
        from t_tech.invest import Client, CandleInterval
        from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
        from t_tech.invest.utils import now, money_to_decimal, quotation_to_decimal
        from t_tech.invest.schemas import MoneyValue
    except ImportError as e:
        print(f"\n❌ Ошибка импорта t-tech-investments: {e}")
        print("Установите пакет командой:")
        print("pip install t-tech-investments --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple")
        return False

    target = INVEST_GRPC_API_SANDBOX if sandbox else INVEST_GRPC_API
    active_account_id = None

    try:
        with Client(token, target=target) as client:
            print("\n[1/4] Проверка подключения и списка счетов...")
            if sandbox:
                accounts_resp = client.sandbox.get_sandbox_accounts()
                accounts = accounts_resp.accounts
                if not accounts:
                    print("  ⚠️ В песочнице еще нет счетов. Создаем виртуальный счет...")
                    open_resp = client.sandbox.open_sandbox_account()
                    print(f"  ✅ Создан тестовый счет: {open_resp.account_id}")
                    active_account_id = open_resp.account_id
                    fund_sandbox = True
                else:
                    print(f"  ✅ Найдено счетов в песочнице: {len(accounts)}")
                    for acc in accounts:
                        print(f"     - Счёт ID: {acc.id} | Имя: {acc.name or 'Sandbox'} | Открыт: {acc.opened_date}")
                    active_account_id = accounts[0].id

                # Пополнение баланса при необходимости
                if fund_sandbox and active_account_id:
                    print(f"  💳 Пополнение песочницы на {fund_amount:,} RUB...")
                    try:
                        client.sandbox.sandbox_pay_in(
                            account_id=active_account_id,
                            amount=MoneyValue(currency="rub", units=fund_amount, nano=0)
                        )
                        print(f"  ✅ Успешно начислено {fund_amount:,} RUB виртуальных средств!")
                    except Exception as pe:
                        print(f"  ⚠️ Не удалось пополнить песочницу: {pe}")

                # Проверка баланса песочницы
                try:
                    positions = client.sandbox.get_sandbox_positions(account_id=active_account_id)
                    rub_money = next((m for m in positions.money if m.currency.lower() == "rub"), None)
                    rub_balance = f"{rub_money.units + rub_money.nano / 1e9:,.2f} RUB" if rub_money else "0.00 RUB"
                    print(f"  💰 Текущий баланс в песочнице: {rub_balance}")
                except Exception as be:
                    print(f"  ℹ️ Баланс: {be}")

            else:
                user_accounts = client.users.get_accounts().accounts
                print(f"  ✅ Найдено боевых счетов: {len(user_accounts)}")
                for acc in user_accounts:
                    print(f"     - ID: {acc.id} | Тип: {acc.type.name} | Имя: {acc.name} | Статус: {acc.status.name}")
                if user_accounts:
                    active_account_id = user_accounts[0].id
                    try:
                        port = client.operations.get_portfolio(account_id=active_account_id)
                        total = port.total_amount_portfolio
                        print(f"  💰 Оценка портфеля: {format_money(total.units, total.nano, total.currency)}")
                    except Exception as pe:
                        print(f"  ℹ️ Портфель: {pe}")

            # [2/4] Проверка инструментов и тикеров
            test_tickers = tickers or getattr(cfg, "TBANK_TICKERS", ["SBER", "GAZP", "LKOH", "ROSN", "YDEX", "T"])
            print(f"\n[2/4] Проверка метаданных инструментов ({len(test_tickers)} тикеров):")
            resolved_instruments = []
            for t in test_tickers:
                info = resolve_tbank_instrument(client, t, class_code=getattr(cfg, "TBANK_CLASS_CODE", "TQBR"))
                resolved_instruments.append(info)
                print(f"  • {t:<6} -> {info['name']} | FIGI: {info['figi']} | 1 лот = {info['lot']} шт. | Шаг: {info['min_price_increment']}")

            # [3/4] Проверка котировок в реальном времени
            print(f"\n[3/4] Получение текущих рыночных цен...")
            figis = [inst["figi"] for inst in resolved_instruments if inst["figi"]]
            try:
                prices_resp = client.market_data.get_last_prices(instrument_id=figis)
                price_map = {p.instrument_uid: p.price for p in prices_resp.last_prices}
                # также смаппим по figi
                for p in prices_resp.last_prices:
                    price_map[p.figi] = p.price

                for inst in resolved_instruments:
                    p = price_map.get(inst["figi"]) or price_map.get(inst.get("uid"))
                    if p:
                        cur_p = float(quotation_to_decimal(p))
                        lot_val = cur_p * inst["lot"]
                        print(f"  • {inst['ticker']:<6} | Цена: {cur_p:>9.2f} RUB | Стоимость 1 лота ({inst['lot']} шт): {lot_val:>10.2f} RUB")
                    else:
                        print(f"  • {inst['ticker']:<6} | Цена: нет данных (рынок закрыт или нет сделок)")
            except Exception as e:
                print(f"  ⚠️ Ошибка при запросе цен: {e}")

            # [4/4] Тестовая загрузка минутных и часовых свечей
            first_inst = resolved_instruments[0]
            print(f"\n[4/4] Тестовая загрузка свечей для {first_inst['ticker']} ({first_inst['figi']})...")
            to_dt = now()
            from_dt = to_dt - timedelta(days=2)
            candles = list(client.get_all_candles(
                instrument_id=first_inst["figi"],
                from_=from_dt,
                to=to_dt,
                interval=CandleInterval.CANDLE_INTERVAL_HOUR
            ))
            print(f"  ✅ Загружено {len(candles)} часовых свечей за последние 2 дня.")
            if candles:
                last_c = candles[-1]
                o = float(quotation_to_decimal(last_c.open))
                h = float(quotation_to_decimal(last_c.high))
                l = float(quotation_to_decimal(last_c.low))
                c = float(quotation_to_decimal(last_c.close))
                print(f"     Последняя свеча ({last_c.time}): Open={o:.2f}, High={h:.2f}, Low={l:.2f}, Close={c:.2f}, Vol={last_c.volume}")

    except Exception as e:
        print(f"\n❌ Ошибка при взаимодействии с API Т-Банка: {e}")
        import traceback
        traceback.print_exc()
        return False

    print("\n" + "=" * 75)
    print("  🎉 ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ УСПЕШНО!")
    print(f"  Вы готовы к запуску бэктеста и торговли в Т-Банке.")
    print("=" * 75)
    print("\nКоманды для запуска:")
    print("  # Бэктест всей корзины акций РФ:")
    print("  python backtest.py --all --source tbank")
    print("\n  # Бэктест одной акции (например Сбербанк):")
    print("  python backtest.py --ticker SBER --source tbank")
    print("\n  # Запуск торгового бота в песочнице (Sandbox) по всей корзине:")
    print("  python tbank_trade.py --all --sandbox")
    print("\n  # Запуск торгового бота по одной акции:")
    print("  python tbank_trade.py --ticker SBER --sandbox")
    return True


def main():
    parser = argparse.ArgumentParser(description="Диагностика подключения к T-Bank Invest API")
    parser.add_argument("--token", type=str, default="", help="API токен Т-Банка (если не задан в .env)")
    parser.add_argument("--sandbox", action="store_true", default=None, help="Использовать контур песочницы")
    parser.add_argument("--real", action="store_true", help="Использовать боевой контур")
    parser.add_argument("--fund", action="store_true", help="Пополнить виртуальный счёт в песочнице на 100,000 RUB")
    parser.add_argument("--amount", type=int, default=100_000, help="Сумма пополнения песочницы (по умолчанию 100 000 руб)")
    parser.add_argument("--ticker", type=str, default="", help="Одиночный тикер для проверки (например SBER)")
    parser.add_argument("--tickers", type=str, default="", help="Список тикеров через запятую (например SBER,GAZP,LKOH)")

    args = parser.parse_args()

    token = (
        args.token
        or getattr(cfg, "TBANK_TOKEN", "")
        or os.environ.get("TBANK_TOKEN", "")
        or os.environ.get("TINKOFF_TOKEN", "")
        or os.environ.get("INVEST_TOKEN", "")
    )

    if args.real:
        sandbox = False
    elif args.sandbox:
        sandbox = True
    else:
        sandbox = getattr(cfg, "TBANK_SANDBOX", True)

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif args.ticker:
        tickers = [args.ticker.strip().upper()]
    else:
        tickers = getattr(cfg, "TBANK_TICKERS", ["SBER", "GAZP", "LKOH", "ROSN", "YDEX", "T"])

    test_tbank_connection(
        token=token,
        sandbox=sandbox,
        tickers=tickers,
        fund_sandbox=args.fund,
        fund_amount=args.amount
    )


if __name__ == "__main__":
    main()
