"""
Единый конфиг. Меняешь здесь - не трогая логику стратегии/бэктеста.
"""
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---- Источник данных: "ccxt" | "tbank" | "tinkoff" | "github_csv" ----
DATA_SOURCE = "ccxt"

# --- ccxt (крипта) ---
CCXT_EXCHANGE = "binance"
SYMBOL = "SOL/USDT"          # одиночный символ (для обратной совместимости)
SYMBOLS = ["SOL/USDT", "XRP/USDT", "BTC/USDT", "ETH/USDT"]
ALTS_SYMBOLS = ["SOL/USDT", "XRP/USDT", "ETH/USDT"]  # проверенная корзина с подтвержденным edge
SWAP_SYMBOL = "SOL/USDT:USDT"  # для live-исполнения - бессрочный фьючерс (нужен для SHORT)
SWAP_SYMBOLS = {
    "XRP/USDT": "XRP/USDT:USDT",
    "SOL/USDT": "SOL/USDT:USDT",
    "BTC/USDT": "BTC/USDT:USDT",
    "ETH/USDT": "ETH/USDT:USDT",
    "BNB/USDT": "BNB/USDT:USDT",
    "DOGE/USDT": "DOGE/USDT:USDT",
    "ADA/USDT": "ADA/USDT:USDT",
}

# Корзина с ультра-низким минимальным лотом и подтвержденным положительным edge
SMALL_ACCOUNT_SYMBOLS = [
    "XRP/USDT:USDT",
    "SOL/USDT:USDT"
]

# --- Пулы кандидатов для автоматического скринера Universe Screener ---
CRYPTO_SCREENER_POOL = [
    "DOGE/USDT", "ADA/USDT", "SUI/USDT", "NEAR/USDT", "AVAX/USDT",
    "APT/USDT", "BNB/USDT", "SOL/USDT", "DOT/USDT", "TRX/USDT", "XRP/USDT"
]
MOEX_SCREENER_POOL = [
    "SBER", "GAZP", "LKOH", "ROSN", "YDEX", "NVTK", "GMKN", "TATN", "CHMF", "PLZL", "MOEX", "ALRS"
]


# --- Каталоги данных, кэша и отчётов ---
CACHE_DIR = "cache"
REPORTS_DIR = "reports"
DATA_DIR = "data"

# --- Файлы состояния и логов ---
SEEN_SIGNALS_FILE = os.path.join(DATA_DIR, "live_seen_signals.json")
TRADE_LOG_FILE = os.path.join(DATA_DIR, "live_trade_log.json")
TBANK_TRADE_LOG_FILE = os.path.join(DATA_DIR, "tbank_trade_log.json")
TBANK_SEEN_SIGNALS_FILE = os.path.join(DATA_DIR, "tbank_seen_signals.json")
TBANK_ACTIVE_POSITIONS_FILE = os.path.join(DATA_DIR, "tbank_active_positions.json")
PENDING_SIGNALS_FILE = os.path.join(DATA_DIR, "pending_signals.json")
ACTIVE_UNIVERSE_FILE = os.path.join(DATA_DIR, "active_universe.json")

# --- Google Gemini AI / Анализ и оценка сделок ---
import os as _os
GEMINI_API_KEY = _os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = _os.environ.get("GEMINI_MODEL", "gemini-flash-latest")  # быстрая и стабильная модель
ENABLE_AI_EVALUATION = True        # включить оценку перед входом в сделку через Gemini AI
AI_CONFIDENCE_THRESHOLD = 7        # минимальный балл (1-10) для одобрения сделки ИИ

# --- Двухфазный Liquidity Sentry (Passive Sentry -> Active Hunt) ---
USE_LIQUIDITY_SENTRY = True        # предварительный расчет уровней ликвидности и триггерные алерты
HUNT_TIMEOUT_MINUTES = 60          # максимальное время охоты за FVG после свипа уровня (мин)
SENTRY_POLL_INTERVAL_SEC = 5       # частота легкого опроса цен тикера для триггера свипа

# --- Live-торговля (Bitget крипто-фьючерсы) ---
BITGET_API_KEY = _os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = _os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = _os.environ.get("BITGET_API_PASSWORD", "")  # Bitget требует passphrase
DEMO_MODE = False  # False = РЕАЛЬНЫЕ ДЕНЬГИ (или через флаг --real / --demo)
LEVERAGE = int(_os.environ.get("LEVERAGE", "10"))
MARGIN_MODE = _os.environ.get("MARGIN_MODE", "isolated")  # "isolated" (изолированная) | "cross" (кросс)

# --- Риск-менеджмент и дисциплина (Turbo Mode для разгона $11 - $22) ---
RISK_PER_TRADE_PCT = float(_os.environ.get("RISK_PER_TRADE_PCT", "20.0"))  # 20% риск (~$2.20 на текущем балансе $11.05)
MAX_RISK_PER_TRADE_PCT = 25.0   # Потолок риска в агрессивном режиме (до 25%)
POSITION_SIZE_USDT = 50        # фиксированный размер позиции (если не используется риск в %)
BITGET_TAKER_FEE_PCT = 0.0006  # Базовая комиссия тейкера Bitget на USDT-M фьючерсах (0.06%)
POLL_INTERVAL_SEC = 30         # как часто проверять новые сигналы (30 сек)

# --- T-Bank Invest API (Т-Инвестиции / Мосбиржа) ---
TBANK_TOKEN = (
    _os.environ.get("TBANK_TOKEN")
    or _os.environ.get("TINKOFF_TOKEN")
    or _os.environ.get("INVEST_TOKEN", "")
)
TBANK_ACCOUNT_ID = _os.environ.get("TBANK_ACCOUNT_ID", "")
TBANK_SANDBOX = _os.environ.get("TBANK_SANDBOX", "True").lower() in ("true", "1", "yes")
TBANK_TICKER = "SBER"
TBANK_TICKERS = ["SBER", "GAZP", "LKOH", "ROSN", "YDEX"]  # ликвидная корзина акций Мосбиржи (T исключен из-за слабых показателей)
TBANK_CLASS_CODE = "TQBR"  # TQBR - акции РФ, SPBFUT - фьючерсы
TBANK_FIGI = _os.environ.get("TBANK_FIGI", "BBG004730N88")  # SBER (если пусто - резолвится по тикеру)
TBANK_COMMISSION_PCT = 0.0005  # базовая комиссия брокера + биржи (~0.05%)
TBANK_MAX_POSITIONS = 5       # макс. число открытых позиций в корзине акций РФ
TBANK_PARTIAL_TAKE_R = float(_os.environ.get("TBANK_PARTIAL_TAKE_R", "1.0"))   # Сетка B: TP1 на 1.0R (фиксация 50% + True BE)
TBANK_RUNNER_TAKE_R = float(_os.environ.get("TBANK_RUNNER_TAKE_R", "1.618")) # Сетка B: TP2 на 1.618R (Golden Ratio)
TBANK_TRAIL_DISTANCE_R = float(_os.environ.get("TBANK_TRAIL_DISTANCE_R", "0.8")) # трейлинг-стоп остатка 0.8R
TBANK_PARTIAL_TAKE_SIZE = float(_os.environ.get("TBANK_PARTIAL_TAKE_SIZE", "0.5")) # фиксация 50% объема на 1.0R
MOEX_EXCLUDE_DAYS = ["Thursday"]  # Четверг исключен на Мосбирже (день экспираций FORTS, убыток -6.28R)
SSL_TBANK_VERIFY = _os.environ.get("SSL_TBANK_VERIFY", "True").lower() in ("true", "1", "yes")

# Для обратной совместимости:
TINKOFF_TOKEN = TBANK_TOKEN
TINKOFF_FIGI = TBANK_FIGI

# --- Telegram Уведомления ---
TELEGRAM_BOT_TOKEN = _os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_PROXY = _os.environ.get("TELEGRAM_PROXY", "")
TELEGRAM_BASE_URL = _os.environ.get("TELEGRAM_BASE_URL", "")
TELEGRAM_NOTIFICATIONS_ENABLED = _os.environ.get("TELEGRAM_NOTIFICATIONS_ENABLED", "True").lower() in ("true", "1", "yes")

# --- Запасной источник: готовый CSV с github ---
GITHUB_CSV_URL = "https://raw.githubusercontent.com/ff137/bitstamp-btcusd-minute-data/main/data/updates/btcusd_bitstamp_1min_latest.csv"

# ---- Период (3 месяца) ----
START_DATE = "2024-06-11"
FORCE_REFRESH_DATA = False  # False = ВСЕГДА использовать локальный кэш из cache/, не качать заново

# ---- Таймфреймы (Единый рабочий стандарт: 1H Bias ➔ 5m Sweep ➔ 1m FVG) ----
BASE_TIMEFRAME = "1m"    # базовый TF, из которого строятся остальные ресемплингом
HTF_RULE = "1h"          # дефолт bias
LTF_RULE = "5min"        # дефолт sweep + FVG + вход

# Секторальные таймфреймы (Единый институциональный стандарт для Crypto и MOEX):
# 563 сделки в год, +191.41R чистой прибыли, Winrate 74.5%, просадка всего ~3.5-4.1R
CRYPTO_HTF_RULE = "1h"
CRYPTO_LTF_RULE = "5min"
CRYPTO_SWING_LENGTH_HTF = 5
CRYPTO_SWING_LENGTH_LTF = 4

MOEX_HTF_RULE = "1h"
MOEX_LTF_RULE = "5min"
MOEX_SWING_LENGTH_HTF = 5
MOEX_SWING_LENGTH_LTF = 4

# ---- Killzones (UTC, приближённо, без учёта перехода на летнее время) ----
USE_KILLZONES = False  # False = торговать круглосуточно; True = строго в Killzones
KILLZONES = [(7, 10), (12, 15)]  # London (07-10 UTC), New York (12-15 UTC) для крипты
MOEX_KILLZONES = [(8, 12)]       # 11:00 - 15:00 МСК (в UTC: 08:00 - 12:00) золотое окно Мосбиржи

# ---- Фильтр направления ('all' | 'long' | 'short') ----
DIRECTION_FILTER = "all"

# ---- Продвинутые ICT-фильтры (SMT & Asian Range) ----
USE_SMT_FILTER = False          # True = входить только при подтверждении SMT-дивергенцией (BTC vs ETH)
USE_ASIAN_RANGE_FILTER = False  # True = входить только при свипе максимума/минимума Азиатской сессии
ASIAN_HOURS = (0, 6)            # Часы Азиатской сессии (00:00 - 06:00 UTC)
SMT_LOOKBACK_BARS = 12          # Окно сопоставления экстремумов для SMT (бары LTF)

# ---- Слом структуры (Market Structure Shift / CHoCH via smartmoneyconcepts) ----
USE_CHOCH_FILTER = True          # True = обязательное подтверждение сломом структуры (CHoCH / BOS)
CHOCH_CLOSE_BREAK = True         # True = подтверждение закрытием свечи (Close Break)
CHOCH_LOOKAHEAD_BARS = 15        # Окно поиска подтверждения слома структуры после свипа (бары LTF)
CHOCH_ALLOW_BOS = True           # True = учитывать как разворотный CHoCH, так и трендовый BOS

# ---- Параметры стратегии ----
SWING_LENGTH_HTF = 5
SWING_LENGTH_LTF = 4
LIQUIDITY_RANGE_PCT = 0.008
MIN_FVG_ZONE_PCT = 0.0005     # 0.05% (5 bps) оптимальный порог FVG (баланс отсева флэта и сохранения +156R прибыли)
MAX_FVG_ZONE_PCT = 0.006      # 0.60% фильтр аномально широких FVG
MIN_ATR_5M_PCT = 0.0005       # 0.05% (5 bps) минимальный 5m ATR (защита от мертвого ночного распила, DD всего 4.1R)
USE_VOLATILITY_FILTER = True  # True = блокировать сетапы при затухании рыночной волатильности
USE_TREND_FILTER = True       # True = фильтр тренда против бокового распила (1H ADX >= 20 + Displacement)
MIN_ADX_1H = 20.0             # минимальный 1H ADX для отсева мертвого флэта
MIN_RISK_PCT = 0.0015        # фильтр аномально узкого стопа (0.15% отсекает шум, оставляя 73.8% WR сетапы)
FEE_SLIPPAGE_PCT = 0.0008    # комиссия + проскальзывание на сделку
STOP_BUFFER_PCT = 0.0015     # 0.15% буфер за свечу свипа (защита от микро-сквизов)

# ---- Режим входа в FVG: "ce" (50% Consequent Encroachment) | "edge" (край зоны) ----
FVG_ENTRY_MODE = "ce"        # "ce" дает лучшую цену, сокращает стоп на 30-40% и повышает R:R

# ---- Стоп-лосс: "wick" (за sweep-свечу) | "ob" (за Order Block) ----
STOP_MODE = "wick"
USE_TRUE_STRUCTURAL_STOP = True # True = расчет стопа от истинного экстремума между свипом и входом (устраняет выбивание в 60.6% сделок)

# ---- Старший макро-тренд (D1 / 4H Bias) ----
USE_HTF_D1_FILTER = False     # True = блокировать Longs при нисходящем тренде D1 (защита от медвежьего рынка)

# ---- Тейк-профит и динамическая калибровка R ----
USE_DYNAMIC_R = False        # True = расчет тейка на основе волатильности (Daily ATR за 14-30 дней)
DYNAMIC_R_MIN = 1.5          # минимальный тейк при низкой волатильности
DYNAMIC_R_MAX = 2.8          # максимальный тейк при сильной трендовой экспансии
PARTIAL_TAKE_R = 1.0         # 1.0R (Grid B первая цель 50% фиксации + перенос в BE)
RUNNER_TAKE_R = 1.618        # 1.618R (Grid B вторая цель для оставшихся 50%)
PARTIAL_TAKE_SIZE = 0.5      # доля позиции, закрываемая на частичном тейке
TRAIL_DISTANCE_R = 0.8       # трейлинг остатка на 0.8R за экстремумом

# ---- Сетка тейков по Фибоначчи и перевод в безубыток ----
USE_BREAKEVEN = True         # True = переносить стоп в безубыток при взятии первой цели
BREAKEVEN_TRIGGER_R = 1.0    # порог в R для перевода стопа в безубыток
TP_GRID = "1.0:0.5,1.618:0.5" # Эталонная сетка: 50% на 1.0R (BE) + 50% на 1.618R (Golden Fib)


# ---- Окна поиска/удержания (в минутах, на базовом 1м TF) ----
MAX_WAIT_MIN = 120           # сколько ждём возврата цены в FVG-зону (2 часа, исключает тухлые входы)
MAX_HOLD_MIN = 60 * 24 * 2   # максимальное время удержания позиции

# ---- Динамический стоп от волатильности (5m ATR) ----
USE_ATR_STOP = False            # False = стоп строго за фитиль; True = фильтр-гейткипер (браковать, если risk < min_dist)
ATR_STOP_MULT = 1.5             # множитель 5m ATR для фильтра-гейткипера

# ---- Институциональные цели по сетке Фибоначчи и пулам ликвидности (BSL / SSL) ----
USE_STRUCTURAL_TARGETS = False  # True = синхронизация тейков с реальными пулами ликвидности толпы
MIN_STRUCTURAL_R = 1.2          # минимальный потенциал хода R до встречного пула ликвидности

# ---- Тайм-менеджмент Киллзон (Hold / Partial 80% / Close) ----
KZ_EXIT_MODE = "hold"           # "hold" (тянуть) | "partial80" (сброс 80% риска в плюс) | "close" (100% дей-трейдинг)


# ---- Динамическое управление вселенной активов (Universe Management) ----
def update_active_symbols(market: str, symbols: list[str]) -> bool:
    """
    Обновляет активную торговую корзину в config.py и в data/active_universe.json.
    market: 'crypto' или 'moex'
    symbols: список тикеров, например ['DOGE/USDT', 'ADA/USDT', 'SUI/USDT', 'NEAR/USDT'] или ['SBER', 'GAZP', ...]
    """
    global ALTS_SYMBOLS, SMALL_ACCOUNT_SYMBOLS, SWAP_SYMBOLS, TBANK_TICKERS
    import json
    import re
    if not symbols:
        return False

    clean_syms = [s.strip().upper() for s in symbols if s.strip()]
    os.makedirs(DATA_DIR, exist_ok=True)

    # 1. Чтение существующего active_universe.json
    universe_data = {}
    if os.path.exists(ACTIVE_UNIVERSE_FILE):
        try:
            with open(ACTIVE_UNIVERSE_FILE, "r", encoding="utf-8") as f:
                universe_data = json.load(f)
        except Exception:
            universe_data = {}

    if market.lower() == "crypto":
        ALTS_SYMBOLS = clean_syms
        SMALL_ACCOUNT_SYMBOLS = [
    "ADA/USDT:USDT",
    "AVAX/USDT:USDT",
    "DOGE/USDT:USDT",
    "DOT/USDT:USDT",
    "NEAR/USDT:USDT",
    "XRP/USDT:USDT"
]
        for s in clean_syms:
            base = s.split(":")[0]
            SWAP_SYMBOLS[base] = f"{base}:USDT"
        universe_data["crypto"] = clean_syms
    elif market.lower() == "moex":
        TBANK_TICKERS = clean_syms
        universe_data["moex"] = clean_syms
    else:
        return False

    # 2. Сохраняем в data/active_universe.json
    try:
        with open(ACTIVE_UNIVERSE_FILE, "w", encoding="utf-8") as f:
            json.dump(universe_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения {ACTIVE_UNIVERSE_FILE}: {e}")

    # 3. Обновляем config.py в файле для персистентности между перезапусками
    try:
        cfg_path = os.path.abspath(__file__)
        with open(cfg_path, "r", encoding="utf-8") as f:
            code = f.read()

        if market.lower() == "crypto":
            syms_repr = json.dumps(clean_syms)
            code = re.sub(r'ALTS_SYMBOLS\s*=\s*\[[^\]]*\]', f'ALTS_SYMBOLS = {syms_repr}', code)
            small_repr = json.dumps([f"{s}:USDT" if not s.endswith(":USDT") else s for s in clean_syms], indent=4)
            code = re.sub(r'SMALL_ACCOUNT_SYMBOLS\s*=\s*\[[^\]]*\]', f'SMALL_ACCOUNT_SYMBOLS = {small_repr}', code)
        elif market.lower() == "moex":
            syms_repr = json.dumps(clean_syms)
            code = re.sub(r'TBANK_TICKERS\s*=\s*\[[^\]]*\]', f'TBANK_TICKERS = {syms_repr}', code)

        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(code)
    except Exception as e:
        print(f"⚠️ Ошибка обновления config.py: {e}")

    return True


# Подгрузка активной корзины из data/active_universe.json при старте скрипта
if os.path.exists(ACTIVE_UNIVERSE_FILE):
    try:
        import json as _json
        with open(ACTIVE_UNIVERSE_FILE, "r", encoding="utf-8") as _f:
            _universe = _json.load(_f)
            if "crypto" in _universe and _universe["crypto"]:
                ALTS_SYMBOLS = _universe["crypto"]
                SMALL_ACCOUNT_SYMBOLS = [
    "ADA/USDT:USDT",
    "AVAX/USDT:USDT",
    "DOGE/USDT:USDT",
    "DOT/USDT:USDT",
    "NEAR/USDT:USDT",
    "XRP/USDT:USDT"
]
                for _s in ALTS_SYMBOLS:
                    _base = _s.split(":")[0]
                    SWAP_SYMBOLS[_base] = f"{_base}:USDT"
            if "moex" in _universe and _universe["moex"]:
                TBANK_TICKERS = _universe["moex"]
    except Exception:
        pass


# ---- Словарь стандартизированных пресетов стратегии (Strategy Presets Registry) ----
STRATEGY_PRESETS = {
    "PROVEN_CUSTOM_MSS": {
        "name": "Проверенный кастомный сетап (Классика)",
        "description": "1H Bias (свинги 5) + 5m Sweep + FVG (ce) + True Structural Stop + Golden Mean (ATR 0.05% / FVG 0.05%) + 1H ADX>=20 + Grid B (Crypto) / 2.5R (MOEX) без библиотечного фильтра CHoCH.",
        "params": {
            "USE_CHOCH_FILTER": False,
            "USE_VOLATILITY_FILTER": True,
            "MIN_FVG_ZONE_PCT": 0.0005,
            "MIN_ATR_5M_PCT": 0.0005,
            "USE_TREND_FILTER": True,
            "MIN_ADX_1H": 20.0,
            "USE_TRUE_STRUCTURAL_STOP": True,
            "STOP_BUFFER_PCT": 0.0015,
            "FVG_ENTRY_MODE": "ce",
            "PARTIAL_TAKE_R": 1.0,
            "RUNNER_TAKE_R": 1.618,
            "PARTIAL_TAKE_SIZE": 0.5,
            "USE_BREAKEVEN": True,
            "BREAKEVEN_TRIGGER_R": 1.0,
            "USE_ASIAN_RANGE_FILTER": False,
            "USE_KILLZONES": False,
            "TBANK_PARTIAL_TAKE_R": 2.5,
            "TBANK_RUNNER_TAKE_R": 2.618,
        },
    },
    "SMC_STRICT_CHOCH": {
        "name": "Строгий слом структуры (Библиотека smartmoneyconcepts)",
        "description": "Полностью идентичен PROVEN_CUSTOM_MSS + обязательное подтверждение слома структуры закрытием свечи (smc.bos_choch).",
        "params": {
            "USE_CHOCH_FILTER": True,
            "CHOCH_CLOSE_BREAK": True,
            "CHOCH_LOOKAHEAD_BARS": 15,
            "CHOCH_ALLOW_BOS": True,
            "USE_VOLATILITY_FILTER": True,
            "MIN_FVG_ZONE_PCT": 0.0005,
            "MIN_ATR_5M_PCT": 0.0005,
            "USE_TREND_FILTER": True,
            "MIN_ADX_1H": 20.0,
            "USE_TRUE_STRUCTURAL_STOP": True,
            "STOP_BUFFER_PCT": 0.0015,
            "FVG_ENTRY_MODE": "ce",
            "PARTIAL_TAKE_R": 1.0,
            "RUNNER_TAKE_R": 1.618,
            "PARTIAL_TAKE_SIZE": 0.5,
            "USE_BREAKEVEN": True,
            "BREAKEVEN_TRIGGER_R": 1.0,
            "USE_ASIAN_RANGE_FILTER": False,
            "USE_KILLZONES": False,
            "TBANK_PARTIAL_TAKE_R": 2.5,
            "TBANK_RUNNER_TAKE_R": 2.618,
        },
    },
    "ASIAN_SNIPER_PRO": {
        "name": "Азиатский сессионный снайпер",
        "description": "PROVEN_CUSTOM_MSS + строгий фильтр выноса уровней Азиатской сессии (00:00–06:00 UTC).",
        "params": {
            "USE_CHOCH_FILTER": False,
            "USE_VOLATILITY_FILTER": True,
            "MIN_FVG_ZONE_PCT": 0.0005,
            "MIN_ATR_5M_PCT": 0.0005,
            "USE_TREND_FILTER": True,
            "MIN_ADX_1H": 20.0,
            "USE_TRUE_STRUCTURAL_STOP": True,
            "STOP_BUFFER_PCT": 0.0015,
            "FVG_ENTRY_MODE": "ce",
            "PARTIAL_TAKE_R": 1.0,
            "RUNNER_TAKE_R": 1.618,
            "PARTIAL_TAKE_SIZE": 0.5,
            "USE_BREAKEVEN": True,
            "BREAKEVEN_TRIGGER_R": 1.0,
            "USE_ASIAN_RANGE_FILTER": True,
            "ASIAN_HOURS": (0, 6),
            "USE_KILLZONES": False,
            "TBANK_PARTIAL_TAKE_R": 2.5,
            "TBANK_RUNNER_TAKE_R": 2.618,
        },
    },
    "POSITIVE_PROVEN_KZ": {
        "name": "Проверенная плюсовая система (Азия + Киллзоны)",
        "description": "Исходная прибыльная конфигурация до запуска ботов: Asian Range + Killzones (London/NY) + Golden Mean + True Stop + Grid B / 2.5R (MOEX) без библиотеки CHoCH.",
        "params": {
            "USE_CHOCH_FILTER": False,
            "USE_VOLATILITY_FILTER": True,
            "MIN_FVG_ZONE_PCT": 0.0005,
            "MIN_ATR_5M_PCT": 0.0005,
            "USE_TREND_FILTER": True,
            "MIN_ADX_1H": 20.0,
            "USE_TRUE_STRUCTURAL_STOP": True,
            "STOP_BUFFER_PCT": 0.0015,
            "FVG_ENTRY_MODE": "ce",
            "PARTIAL_TAKE_R": 1.0,
            "RUNNER_TAKE_R": 1.618,
            "PARTIAL_TAKE_SIZE": 0.5,
            "USE_BREAKEVEN": True,
            "BREAKEVEN_TRIGGER_R": 1.0,
            "USE_ASIAN_RANGE_FILTER": True,
            "ASIAN_HOURS": (0, 6),
            "USE_KILLZONES": True,
            "KILLZONES": [(7, 10), (12, 15)],
            "MOEX_KILLZONES": [(8, 12)],
            "MOEX_EXCLUDE_DAYS": ["Thursday"],
            "TBANK_PARTIAL_TAKE_R": 2.5,
            "TBANK_RUNNER_TAKE_R": 2.618,
        },
    },
    "POSITIVE_KZ_PLUS_CHOCH": {
        "name": "Проверенная плюсовая система + Библиотечный CHoCH",
        "description": "Полностью идентична POSITIVE_PROVEN_KZ + обязательное подтверждение слома структуры закрытием свечи (smc.bos_choch).",
        "params": {
            "USE_CHOCH_FILTER": True,
            "CHOCH_CLOSE_BREAK": True,
            "CHOCH_LOOKAHEAD_BARS": 15,
            "CHOCH_ALLOW_BOS": True,
            "USE_VOLATILITY_FILTER": True,
            "MIN_FVG_ZONE_PCT": 0.0005,
            "MIN_ATR_5M_PCT": 0.0005,
            "USE_TREND_FILTER": True,
            "MIN_ADX_1H": 20.0,
            "USE_TRUE_STRUCTURAL_STOP": True,
            "STOP_BUFFER_PCT": 0.0015,
            "FVG_ENTRY_MODE": "ce",
            "PARTIAL_TAKE_R": 1.0,
            "RUNNER_TAKE_R": 1.618,
            "PARTIAL_TAKE_SIZE": 0.5,
            "USE_BREAKEVEN": True,
            "BREAKEVEN_TRIGGER_R": 1.0,
            "USE_ASIAN_RANGE_FILTER": True,
            "ASIAN_HOURS": (0, 6),
            "USE_KILLZONES": True,
            "KILLZONES": [(7, 10), (12, 15)],
            "MOEX_KILLZONES": [(8, 12)],
            "MOEX_EXCLUDE_DAYS": ["Thursday"],
            "TBANK_PARTIAL_TAKE_R": 2.5,
            "TBANK_RUNNER_TAKE_R": 2.618,
        },
    },
    "HYBRID_INSTITUTIONAL": {
        "name": "Единый институциональный стандарт (1H/5m + Сетка B 1.0R / 1.618R)",
        "description": "Единая архитектура для Крипты и Мосбиржи: 1H Bias ➔ 5m Sweep ➔ 1m FVG (CE). Сетка B: 50% на 1.0R (+BE) и 50% на 1.618R. 563 сделки/год, +191.41R чистой прибыли, Winrate 74.5%.",
        "params": {
            "USE_CHOCH_FILTER": True,
            "CHOCH_CLOSE_BREAK": True,
            "CHOCH_LOOKAHEAD_BARS": 15,
            "CHOCH_ALLOW_BOS": True,
            "USE_VOLATILITY_FILTER": True,
            "MIN_FVG_ZONE_PCT": 0.0005,
            "MIN_ATR_5M_PCT": 0.0005,
            "USE_TREND_FILTER": True,
            "MIN_ADX_1H": 20.0,
            "USE_TRUE_STRUCTURAL_STOP": True,
            "STOP_BUFFER_PCT": 0.0015,
            "FVG_ENTRY_MODE": "ce",
            "PARTIAL_TAKE_R": 1.0,
            "RUNNER_TAKE_R": 1.618,
            "PARTIAL_TAKE_SIZE": 0.5,
            "USE_BREAKEVEN": True,
            "BREAKEVEN_TRIGGER_R": 1.0,
            "USE_ASIAN_RANGE_FILTER": False,
            "USE_KILLZONES": False,
            "CRYPTO_HTF_RULE": "1h",
            "CRYPTO_LTF_RULE": "5min",
            "MOEX_HTF_RULE": "1h",
            "MOEX_LTF_RULE": "5min",
            "MOEX_EXCLUDE_DAYS": ["Thursday"],
            "TBANK_PARTIAL_TAKE_R": 1.0,
            "TBANK_RUNNER_TAKE_R": 1.618,
        },
    },
    "BASELINE_RAW": {
        "name": "Сырой академический сетап (Без фильтров)",
        "description": "Базовый свип 5m -> FVG без фильтров тренда, волатильности и слома структуры.",
        "params": {
            "USE_CHOCH_FILTER": False,
            "USE_VOLATILITY_FILTER": False,
            "USE_TREND_FILTER": False,
            "USE_TRUE_STRUCTURAL_STOP": False,
            "USE_ASIAN_RANGE_FILTER": False,
            "USE_KILLZONES": False,
            "FVG_ENTRY_MODE": "edge",
            "PARTIAL_TAKE_R": 1.5,
            "TRAIL_DISTANCE_R": 0.8,
            "USE_BREAKEVEN": False,
        },
    },
}


def apply_preset(preset_name: str) -> dict:
    """
    Применяет указанный пресет к глобальным переменным модуля config.
    Возвращает словарь установленных параметров.
    """
    if preset_name not in STRATEGY_PRESETS:
        available = ", ".join(STRATEGY_PRESETS.keys())
        raise ValueError(f"Неизвестный пресет '{preset_name}'. Доступны: {available}")

    preset = STRATEGY_PRESETS[preset_name]
    params = preset["params"]
    import sys
    current_module = sys.modules[__name__]
    for key, val in params.items():
        setattr(current_module, key, val)
    return params

