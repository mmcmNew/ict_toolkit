"""
Единая точка получения OHLCV-данных. Переключение источника - через config.py
(DATA_SOURCE = "ccxt" или "tinkoff").

ccxt    -> любая крипто-биржа (Binance, Bybit, OKX...), не требует токена для истории
tinkoff -> MOEX через Tinkoff Invest API, требует токен (см. README.md)
"""
import os
import time
import pandas as pd


def fetch_ccxt(symbol: str, timeframe: str, since_ms: int, limit_per_call: int = 1000,
                exchange_id: str = "binance", progress: bool = True) -> pd.DataFrame:
    """
    Тянет историю OHLCV с крипто-биржи через ccxt, постранично.

    ВАЖНО: разные биржи отдают разное реальное количество свечей за вызов независимо
    от запрошенного limit (например Bitget фактически отдаёт максимум ~200, даже если
    попросить 1000). Раньше здесь была ошибка: короткий батч интерпретировался как
    "данных больше нет" и пагинация останавливалась после первого запроса. Правильный
    критерий остановки - "курсор перестал продвигаться вперёд", а не "пришло меньше,
    чем просили".

    symbol: например "BTC/USDT"
    timeframe: "1m", "5m", "15m", "1h", "4h" ...
    since_ms: начальная точка в unix ms (например int(pd.Timestamp("2025-01-01").timestamp()*1000))
    """
    import ccxt
    exchange = getattr(ccxt, exchange_id)()
    all_rows = []
    cursor = since_ms
    now_ms = exchange.milliseconds()
    last_seen_ts = None
    calls = 0

    while cursor < now_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=limit_per_call)
        calls += 1
        if not batch:
            break

        last_ts = batch[-1][0]
        if last_seen_ts is not None and last_ts <= last_seen_ts:
            # биржа перестала продвигаться (достигли конца доступной истории) - стоп,
            # а НЕ по факту "батч короче limit_per_call" (это нормально для многих бирж)
            break
        last_seen_ts = last_ts

        all_rows.extend(batch)
        cursor = last_ts + 1

        if progress and calls % 20 == 0:
            print(f"  ...загружено {len(all_rows)} баров, дошли до {pd.to_datetime(last_ts, unit='ms')}", flush=True)

        time.sleep(exchange.rateLimit / 1000)  # уважаем rate limit биржи


    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms")
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    return df.sort_index()


def resolve_tbank_instrument(client, ticker_or_figi: str, class_code: str = "TQBR") -> dict:
    """
    Разрешает тикер (например 'SBER', 'GAZP', 'LKOH') или FIGI в метаданные инструмента:
    figi, ticker, name, lot, min_price_increment, currency, uid.
    """
    from t_tech.invest.utils import quotation_to_decimal
    s = ticker_or_figi.strip()
    
    # Если это уже FIGI (начинается с BBG или длина 12)
    if s.startswith("BBG") and len(s) == 12:
        try:
            res = client.instruments.get_instrument_by(id_type=1, id=s)  # 1 = INSTRUMENT_ID_TYPE_FIGI
            inst = res.instrument
            return {
                "figi": inst.figi,
                "ticker": inst.ticker,
                "name": inst.name,
                "lot": inst.lot or 1,
                "min_price_increment": float(quotation_to_decimal(inst.min_price_increment)) if inst.min_price_increment else 0.01,
                "currency": inst.currency,
                "uid": inst.uid,
                "class_code": inst.class_code,
            }
        except Exception:
            pass

    # Поиск по акциям Мосбиржи
    try:
        shares = client.instruments.shares().instruments
        for sh in shares:
            if sh.ticker.upper() == s.upper() and (not class_code or sh.class_code == class_code):
                return {
                    "figi": sh.figi,
                    "ticker": sh.ticker,
                    "name": sh.name,
                    "lot": sh.lot or 1,
                    "min_price_increment": float(quotation_to_decimal(sh.min_price_increment)) if sh.min_price_increment else 0.01,
                    "currency": sh.currency,
                    "uid": sh.uid,
                    "class_code": sh.class_code,
                }
    except Exception:
        pass

    # Запасной поиск через find_instrument
    try:
        found = client.instruments.find_instrument(query=s).instruments
        for fi in found:
            if fi.ticker.upper() == s.upper():
                return {
                    "figi": fi.figi,
                    "ticker": fi.ticker,
                    "name": fi.name,
                    "lot": fi.lot or 1,
                    "min_price_increment": float(quotation_to_decimal(fi.min_price_increment)) if fi.min_price_increment else 0.01,
                    "currency": fi.currency,
                    "uid": fi.uid,
                    "class_code": fi.class_code,
                }
    except Exception:
        pass

    # Запасной fallback
    return {
        "figi": s,
        "ticker": s,
        "name": s,
        "lot": 1,
        "min_price_increment": 0.01,
        "currency": "rub",
        "uid": s,
        "class_code": class_code,
    }


def fetch_tbank(ticker_or_figi: str, timeframe: str, from_dt: pd.Timestamp,
                to_dt: pd.Timestamp, token: str, class_code: str = "TQBR",
                sandbox: bool = False) -> pd.DataFrame:
    """
    Тянет историю свечей с MOEX через официальный T-Bank Invest API (t-tech-investments).
    Поддерживает передачу тикера (SBER, GAZP, LKOH...) или FIGI.
    """
    try:
        from t_tech.invest import Client, CandleInterval
        from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
    except ImportError:
        try:
            from tinkoff.invest import Client, CandleInterval
            INVEST_GRPC_API = "invest-public-api.tinkoff.ru:443"
            INVEST_GRPC_API_SANDBOX = "sandbox-invest-public-api.tinkoff.ru:443"
        except ImportError:
            raise ImportError(
                "Пакет T-Bank Invest API не установлен. Установите его командой:\n"
                "pip install t-tech-investments --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"
            )

    interval_map = {
        "1m": CandleInterval.CANDLE_INTERVAL_1_MIN,
        "1min": CandleInterval.CANDLE_INTERVAL_1_MIN,
        "5m": CandleInterval.CANDLE_INTERVAL_5_MIN,
        "5min": CandleInterval.CANDLE_INTERVAL_5_MIN,
        "15m": CandleInterval.CANDLE_INTERVAL_15_MIN,
        "15min": CandleInterval.CANDLE_INTERVAL_15_MIN,
        "1h": CandleInterval.CANDLE_INTERVAL_HOUR,
        "60min": CandleInterval.CANDLE_INTERVAL_HOUR,
        "4h": CandleInterval.CANDLE_INTERVAL_4_HOUR,
        "1d": CandleInterval.CANDLE_INTERVAL_DAY,
    }
    if timeframe not in interval_map:
        raise ValueError(f"Неподдерживаемый таймфрейм для Т-Банка: {timeframe}. Допустимы: {list(interval_map.keys())}")
    interval = interval_map[timeframe]

    target = INVEST_GRPC_API_SANDBOX if sandbox else INVEST_GRPC_API
    rows = []
    with Client(token, target=target) as client:
        inst_meta = resolve_tbank_instrument(client, ticker_or_figi, class_code=class_code)
        instrument_id = inst_meta.get("figi") or inst_meta.get("uid") or ticker_or_figi
        print(f"[{ticker_or_figi}] Загрузка истории Т-Банк: {inst_meta.get('name', ticker_or_figi)} (FIGI: {instrument_id}) с {from_dt} по {to_dt}...", flush=True)

        try:
            candles = client.get_all_candles(
                instrument_id=instrument_id, from_=from_dt, to=to_dt, interval=interval
            )
        except TypeError:
            candles = client.get_all_candles(
                figi=instrument_id, from_=from_dt, to=to_dt, interval=interval
            )

        for candle in candles:
            rows.append({
                "dt": candle.time,
                "open": candle.open.units + candle.open.nano / 1e9,
                "high": candle.high.units + candle.high.nano / 1e9,
                "low": candle.low.units + candle.low.nano / 1e9,
                "close": candle.close.units + candle.close.nano / 1e9,
                "volume": candle.volume,
            })

    if not rows:
        print(f"[{ticker_or_figi}] Предупреждение: Т-Банк API вернул 0 свечей за указанный период.", flush=True)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows).set_index("dt")
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


# Алиас для обратной совместимости
def fetch_tinkoff(figi: str, timeframe: str, from_dt: pd.Timestamp, to_dt: pd.Timestamp, token: str) -> pd.DataFrame:
    return fetch_tbank(figi, timeframe, from_dt, to_dt, token=token)


def fetch_github_csv(url: str) -> pd.DataFrame:
    """
    Запасной вариант - готовый исторический CSV (например датасет BTC/USD с
    github.com/ff137/bitstamp-btcusd-minute-data). Полезно для быстрого бэктеста
    без настройки API/токенов.
    """
    df = pd.read_csv(url)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s")
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    return df.sort_index()


def get_symbol_slug(symbol: str) -> str:
    """Безопасное имя файла для кэша из тикера (BTC/USDT -> BTC_USDT, SBER -> SBER)."""
    return symbol.replace("/", "_").replace(":", "_")


def get_cache_path(config, symbol: str) -> str:
    cache_dir = getattr(config, "CACHE_DIR", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    slug = get_symbol_slug(symbol)
    data_src = getattr(config, "DATA_SOURCE", "ccxt")
    if data_src in ("tbank", "tinkoff"):
        return os.path.join(cache_dir, f"data_tbank_{slug}.csv")
    exchange = getattr(config, "CCXT_EXCHANGE", "exchange")
    return os.path.join(cache_dir, f"data_{exchange}_{slug}.csv")


def get_data(config, symbol: str = None, use_cache: bool = True) -> pd.DataFrame:
    """
    Точка входа с поддержкой кэширования и мульти-инструментов.
    Если symbol не указан, берется config.SYMBOL или config.TBANK_TICKER.
    """
    data_src = getattr(config, "DATA_SOURCE", "ccxt")
    if data_src in ("tbank", "tinkoff"):
        default_sym = getattr(config, "TBANK_TICKER", "SBER")
    else:
        default_sym = getattr(config, "SYMBOL", "BTC/USDT")
    
    sym = symbol or default_sym
    cache_file = get_cache_path(config, sym)

    force_refresh = getattr(config, "FORCE_REFRESH_DATA", False)
    if use_cache and not force_refresh:
        if os.path.exists(cache_file):
            print(f"[{sym}] Загрузка из локального кэша: {cache_file} (без обращения к бирже)")
            return pd.read_csv(cache_file, index_col=0, parse_dates=True)
        # Обратная совместимость для дефолтного крипто-кэша
        if sym == getattr(config, "SYMBOL", "BTC/USDT") and os.path.exists("data_cache.csv"):
            print(f"[{sym}] Загрузка из локального кэша: data_cache.csv")
            return pd.read_csv("data_cache.csv", index_col=0, parse_dates=True)

    if data_src == "ccxt":
        since_ms = int(pd.Timestamp(config.START_DATE).timestamp() * 1000)
        df = fetch_ccxt(sym, config.BASE_TIMEFRAME, since_ms,
                        exchange_id=config.CCXT_EXCHANGE)
    elif data_src in ("tbank", "tinkoff"):
        token = (
            getattr(config, "TBANK_TOKEN", "")
            or getattr(config, "TINKOFF_TOKEN", "")
            or os.environ.get("TBANK_TOKEN", "")
            or os.environ.get("INVEST_TOKEN", "")
        )
        if not token:
            raise ValueError(
                "Не указан TBANK_TOKEN в .env / config.py!\n"
                "Получите токен в ЛК Т-Банка (Инвестиции -> Настройки -> Доступ к API) "
                "и пропишите TBANK_TOKEN в .env"
            )
        class_code = getattr(config, "TBANK_CLASS_CODE", "TQBR")
        sandbox = getattr(config, "TBANK_SANDBOX", True)
        df = fetch_tbank(sym, config.BASE_TIMEFRAME,
                         pd.Timestamp(config.START_DATE), pd.Timestamp.now(),
                         token=token, class_code=class_code, sandbox=sandbox)
    elif data_src == "github_csv":
        df = fetch_github_csv(config.GITHUB_CSV_URL)
    else:
        raise ValueError(f"Неизвестный DATA_SOURCE: {config.DATA_SOURCE}")

    if use_cache and len(df):
        df.to_csv(cache_file)
        if sym == getattr(config, "SYMBOL", "BTC/USDT") and data_src == "ccxt":
            df.to_csv("data_cache.csv")

    return df
