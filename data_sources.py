"""
Единая точка получения OHLCV-данных. Переключение источника - через config.py
(DATA_SOURCE = "ccxt" или "tinkoff").

ccxt    -> любая крипто-биржа (Binance, Bybit, OKX...), не требует токена для истории
tinkoff -> MOEX через Tinkoff Invest API, требует токен (см. README.md)
"""
import os
import time
import pandas as pd


def fetch_ccxt(symbol: str, timeframe: str, since_ms: int, until_ms: int = None,
                limit_per_call: int = 1000, exchange_id: str = "binance",
                progress: bool = True) -> pd.DataFrame:
    """
    Тянет историю OHLCV с крипто-биржи через ccxt, постранично.
    По умолчанию используется binance (отдает 1 000 баров за вызов вместо ~200 у Bitget,
    что ускоряет загрузку в 5 раз).

    symbol: например "BTC/USDT"
    timeframe: "1m", "5m", "15m", "1h", "4h" ...
    since_ms: начальная точка в unix ms
    until_ms: конечная точка в unix ms (если None - до текущего момента)
    limit_per_call: баров за запрос (для Binance 1000)
    exchange_id: "binance", "bitget" и др.
    """
    import ccxt
    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    all_rows = []
    cursor = since_ms
    now_ms = exchange.milliseconds()
    target_end_ms = min(now_ms, until_ms) if until_ms is not None else now_ms
    last_seen_ts = None
    calls = 0

    while cursor < target_end_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=limit_per_call)
        calls += 1
        if not batch:
            break

        last_ts = batch[-1][0]
        if last_seen_ts is not None and last_ts <= last_seen_ts:
            # биржа перестала продвигаться (достигли конца доступной истории) - стоп
            break
        last_seen_ts = last_ts

        if until_ms is not None:
            filtered = [row for row in batch if row[0] <= until_ms]
            all_rows.extend(filtered)
            if last_ts >= until_ms:
                break
        else:
            all_rows.extend(batch)

        cursor = last_ts + 1

        if progress and calls % 20 == 0:
            print(f"  ...[{symbol}] {exchange_id}: загружено {len(all_rows)} баров, дошли до {pd.to_datetime(last_ts, unit='ms')}", flush=True)

        time.sleep(max(exchange.rateLimit / 1000, 0.05))  # уважаем rate limit биржи

    if not all_rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms")
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="last")]
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
                to_dt: pd.Timestamp = None, token: str = None, class_code: str = "TQBR",
                sandbox: bool = False, progress: bool = True) -> pd.DataFrame:
    """
    Тянет историю свечей с MOEX через официальный T-Bank Invest API (t-tech-investments).
    Поддерживает передачу тикера (SBER, GAZP, LKOH...) или FIGI.
    Поддерживает гибкий горизонт истории до 1 года с авто-нормализацией UTC и прогрессом.
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

    from datetime import timezone
    from_dt = pd.Timestamp(from_dt)
    if from_dt.tzinfo is None:
        from_dt = from_dt.tz_localize(timezone.utc)
    else:
        from_dt = from_dt.tz_convert(timezone.utc)

    if to_dt is None:
        to_dt = pd.Timestamp.now(tz=timezone.utc)
    else:
        to_dt = pd.Timestamp(to_dt)
        if to_dt.tzinfo is None:
            to_dt = to_dt.tz_localize(timezone.utc)
        else:
            to_dt = to_dt.tz_convert(timezone.utc)

    if to_dt <= from_dt:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    target = INVEST_GRPC_API_SANDBOX if sandbox else INVEST_GRPC_API
    rows = []
    with Client(token, target=target) as client:
        inst_meta = resolve_tbank_instrument(client, ticker_or_figi, class_code=class_code)
        instrument_id = inst_meta.get("figi") or inst_meta.get("uid") or ticker_or_figi
        inst_name = inst_meta.get("name", ticker_or_figi)
        print(f"[{ticker_or_figi}] Загрузка истории Т-Банк: {inst_name} (ID: {instrument_id}) с {from_dt.strftime('%Y-%m-%d')} по {to_dt.strftime('%Y-%m-%d')}...", flush=True)

        try:
            candles = client.get_all_candles(
                instrument_id=instrument_id, from_=from_dt, to=to_dt, interval=interval
            )
        except TypeError:
            candles = client.get_all_candles(
                figi=instrument_id, from_=from_dt, to=to_dt, interval=interval
            )

        last_report_time = time.time()
        for candle in candles:
            rows.append({
                "dt": candle.time,
                "open": candle.open.units + candle.open.nano / 1e9,
                "high": candle.high.units + candle.high.nano / 1e9,
                "low": candle.low.units + candle.low.nano / 1e9,
                "close": candle.close.units + candle.close.nano / 1e9,
                "volume": candle.volume,
            })
            if progress and (len(rows) % 10000 == 0 or (time.time() - last_report_time > 5 and len(rows) > 0)):
                cur_str = candle.time.strftime("%Y-%m-%d") if hasattr(candle.time, "strftime") else str(candle.time)
                print(f"  ...[{ticker_or_figi}] Загружено {len(rows)} баров Т-Банк, дошли до {cur_str}", flush=True)
                last_report_time = time.time()

    if not rows:
        print(f"[{ticker_or_figi}] Предупреждение: Т-Банк API вернул 0 свечей за указанный период.", flush=True)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows).set_index("dt")
    if df.index.tz is not None:
        df.index = df.index.tz_convert(None)
    df = df[~df.index.duplicated(keep="last")]
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


def get_cache_path(config, symbol: str, exchange: str = None) -> str:
    cache_dir = getattr(config, "CACHE_DIR", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    slug = get_symbol_slug(symbol)
    data_src = getattr(config, "DATA_SOURCE", "ccxt")
    tbank_tickers = getattr(config, "TBANK_TICKERS", ["SBER", "GAZP", "LKOH", "ROSN", "YDEX", "T"])
    if data_src in ("tbank", "tinkoff") or symbol in tbank_tickers:
        return os.path.join(cache_dir, f"data_tbank_{slug}.csv")
    ex = exchange or getattr(config, "CCXT_HISTORY_EXCHANGE", None) or getattr(config, "CCXT_EXCHANGE", None) or "binance"
    return os.path.join(cache_dir, f"data_{ex}_{slug}.csv")


def get_data(config, symbol: str = None, use_cache: bool = True,
             start_date: str | pd.Timestamp = None,
             end_date: str | pd.Timestamp = None) -> pd.DataFrame:
    """
    Точка входа с поддержкой кэширования, мульти-инструментов, докачивания недостающей истории
    и кастомного горизонта (start_date / end_date).
    Если symbol не указан, берется config.SYMBOL или config.TBANK_TICKER.
    """
    tbank_tickers = getattr(config, "TBANK_TICKERS", ["SBER", "GAZP", "LKOH", "ROSN", "YDEX", "T"])
    if symbol in tbank_tickers:
        data_src = "tbank"
    else:
        data_src = getattr(config, "DATA_SOURCE", "ccxt")

    if data_src in ("tbank", "tinkoff"):
        default_sym = getattr(config, "TBANK_TICKER", "SBER")
    else:
        default_sym = getattr(config, "SYMBOL", "BTC/USDT")
    
    sym = symbol or default_sym
    exchange = (
        getattr(config, "CCXT_HISTORY_EXCHANGE", None)
        or getattr(config, "CCXT_EXCHANGE", None)
        or "binance"
    )
    cache_file = get_cache_path(config, sym, exchange=exchange)

    # Определяем запрашиваемый интервал (tz-naive)
    req_start = start_date if start_date is not None else getattr(config, "START_DATE", "2024-06-11")
    req_start_ts = pd.Timestamp(req_start)
    if req_start_ts.tzinfo is not None:
        req_start_ts = req_start_ts.tz_convert(None)

    req_end = end_date if end_date is not None else pd.Timestamp.now()
    req_end_ts = pd.Timestamp(req_end)
    if req_end_ts.tzinfo is not None:
        req_end_ts = req_end_ts.tz_convert(None)

    force_refresh = getattr(config, "FORCE_REFRESH_DATA", False)

    cached_df = None
    loaded_from_path = None

    if use_cache and not force_refresh:
        cache_dir = getattr(config, "CACHE_DIR", "cache")
        slug = get_symbol_slug(sym)

        # Кандидаты для локального кэша
        candidates = [cache_file]
        if data_src == "ccxt":
            alt_bitget = os.path.join(cache_dir, f"data_bitget_{slug}.csv")
            if alt_bitget not in candidates and os.path.exists(alt_bitget):
                candidates.append(alt_bitget)
            fallback_cache = os.path.join(cache_dir, "data_cache.csv")
            if sym == getattr(config, "SYMBOL", "BTC/USDT"):
                if os.path.exists(fallback_cache) and fallback_cache not in candidates:
                    candidates.append(fallback_cache)
                if os.path.exists("data_cache.csv") and "data_cache.csv" not in candidates:
                    candidates.append("data_cache.csv")

        for cand_path in candidates:
            if os.path.exists(cand_path):
                try:
                    c_df = pd.read_csv(cand_path, index_col=0, parse_dates=True).sort_index()
                    if len(c_df) > 0:
                        cached_df = c_df
                        loaded_from_path = cand_path
                        break
                except Exception as e:
                    print(f"[{sym}] Ошибка чтения кэша {cand_path}: {e}")

    # Проверка достаточности глубины кэша
    if cached_df is not None and len(cached_df) > 0:
        c_min = cached_df.index.min()
        c_max = cached_df.index.max()
        # Допускаем погрешность в несколько минут (например секунды между запусками или неполный 1м бар)
        tolerance = pd.Timedelta(minutes=5)
        if c_min <= req_start_ts + tolerance:
            print(f"[{sym}] Загрузка из локального кэша: {loaded_from_path} (глубина достаточна: {c_min} -> {c_max})")
            sliced_df = cached_df[(cached_df.index >= req_start_ts) & (cached_df.index <= req_end_ts)]
            if len(sliced_df) > 0:
                return sliced_df
        else:
            # В кэше меньше истории, чем запрошено (например 3 месяца вместо 1 года)
            print(f"[{sym}] Внимание: глубина кэша ({loaded_from_path}) недостаточна! Доступно с {c_min}, запрошено с {req_start_ts}.")
            print(f"[{sym}] Докачиваем недостающую историю: с {req_start_ts} по {c_min}...")

            missing_df = None
            if data_src == "ccxt":
                since_ms = int(req_start_ts.timestamp() * 1000)
                until_ms = int(c_min.timestamp() * 1000)
                missing_df = fetch_ccxt(sym, config.BASE_TIMEFRAME, since_ms=since_ms, until_ms=until_ms,
                                        exchange_id=exchange)
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
                missing_df = fetch_tbank(sym, config.BASE_TIMEFRAME,
                                         from_dt=req_start_ts, to_dt=c_min,
                                         token=token, class_code=class_code, sandbox=sandbox)

            if missing_df is not None and len(missing_df) > 0:
                combined_df = pd.concat([missing_df, cached_df])
                combined_df = combined_df[~combined_df.index.duplicated(keep="last")].sort_index()
            else:
                combined_df = cached_df

            combined_df.to_csv(cache_file)
            if sym == getattr(config, "SYMBOL", "BTC/USDT") and data_src == "ccxt":
                fallback_cache = os.path.join(getattr(config, "CACHE_DIR", "cache"), "data_cache.csv")
                combined_df.to_csv(fallback_cache)

            return combined_df[(combined_df.index >= req_start_ts) & (combined_df.index <= req_end_ts)]

    # Первичная загрузка, если кэш отсутствует или включен force_refresh
    print(f"[{sym}] Первичная загрузка истории с {req_start_ts} по {req_end_ts} (источник: {data_src})...")
    if data_src == "ccxt":
        since_ms = int(req_start_ts.timestamp() * 1000)
        until_ms = int(req_end_ts.timestamp() * 1000) if end_date is not None else None
        df = fetch_ccxt(sym, config.BASE_TIMEFRAME, since_ms=since_ms, until_ms=until_ms,
                        exchange_id=exchange)
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
                         from_dt=req_start_ts, to_dt=req_end_ts,
                         token=token, class_code=class_code, sandbox=sandbox)
    elif data_src == "github_csv":
        df = fetch_github_csv(config.GITHUB_CSV_URL)
        df = df[(df.index >= req_start_ts) & (df.index <= req_end_ts)]
    else:
        raise ValueError(f"Неизвестный DATA_SOURCE: {config.DATA_SOURCE}")

    if use_cache and len(df):
        df.to_csv(cache_file)
        if sym == getattr(config, "SYMBOL", "BTC/USDT") and data_src == "ccxt":
            fallback_cache = os.path.join(getattr(config, "CACHE_DIR", "cache"), "data_cache.csv")
            df.to_csv(fallback_cache)

    return df[(df.index >= req_start_ts) & (df.index <= req_end_ts)]
