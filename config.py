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
SYMBOL = "BTC/USDT"          # одиночный символ (для обратной совместимости)
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "DOGE/USDT", "ADA/USDT"]
ALTS_SYMBOLS = ["BNB/USDT", "SOL/USDT", "DOGE/USDT", "ADA/USDT"]  # XRP исключен из-за структурной токсичности
SWAP_SYMBOL = "BTC/USDT:USDT"  # для live-исполнения - бессрочный фьючерс (нужен для SHORT)
SWAP_SYMBOLS = {
    "DOGE/USDT": "DOGE/USDT:USDT",
    "ADA/USDT": "ADA/USDT:USDT",
    "XRP/USDT": "XRP/USDT:USDT",
    "BNB/USDT": "BNB/USDT:USDT",
    "SOL/USDT": "SOL/USDT:USDT",
    "BTC/USDT": "BTC/USDT:USDT",
    "ETH/USDT": "ETH/USDT:USDT",
}

# Корзина с ультра-низким минимальным лотом (DOGE, ADA, BNB, SOL - без XRP)
SMALL_ACCOUNT_SYMBOLS = [
    "DOGE/USDT:USDT",
    "ADA/USDT:USDT",
    "BNB/USDT:USDT",
    "SOL/USDT:USDT",
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

# --- Google Gemini AI / Анализ и оценка сделок ---
import os as _os
GEMINI_API_KEY = _os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = _os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")  # актуальная быстрая модель
ENABLE_AI_EVALUATION = False       # включить оценку перед входом в сделку (или через флаг --ai)
AI_CONFIDENCE_THRESHOLD = 7        # минимальный балл (1-10) для одобрения сделки ИИ

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
TBANK_PARTIAL_TAKE_R = float(_os.environ.get("TBANK_PARTIAL_TAKE_R", "2.5"))   # Тейк 2.5R для акций РФ (+107.6R, PF 2.46 на тестах)
TBANK_TRAIL_DISTANCE_R = float(_os.environ.get("TBANK_TRAIL_DISTANCE_R", "0.8")) # трейлинг-стоп остатка 0.8R
TBANK_PARTIAL_TAKE_SIZE = float(_os.environ.get("TBANK_PARTIAL_TAKE_SIZE", "0.5")) # фиксация 50% объема на 2.5R
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

# ---- Таймфреймы (по итогам обсуждения: 1H bias + 5m sweep/вход) ----
BASE_TIMEFRAME = "1m"    # базовый TF, из которого строятся остальные ресемплингом
HTF_RULE = "1h"          # bias
LTF_RULE = "5min"        # sweep + FVG + вход

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

# ---- Параметры стратегии ----
SWING_LENGTH_HTF = 5
SWING_LENGTH_LTF = 4
LIQUIDITY_RANGE_PCT = 0.008
MIN_FVG_ZONE_PCT = 0.0005     # 0.05% (5 bps) оптимальный порог FVG (баланс отсева флэта и сохранения +156R прибыли)
MAX_FVG_ZONE_PCT = 0.006      # 0.60% фильтр аномально широких FVG
MIN_ATR_5M_PCT = 0.0005       # 0.05% (5 bps) минимальный 5m ATR (защита от мертвого ночного распила, DD всего 4.1R)
USE_VOLATILITY_FILTER = True  # True = блокировать сетапы при затухании рыночной волатильности
MIN_RISK_PCT = 0.0015        # фильтр аномально узкого стопа (0.15% отсекает шум, оставляя 73.8% WR сетапы)
FEE_SLIPPAGE_PCT = 0.0008    # комиссия + проскальзывание на сделку
STOP_BUFFER_PCT = 0.0015     # 0.15% буфер за свечу свипа (защита от микро-сквизов)

# ---- Режим входа в FVG: "ce" (50% Consequent Encroachment) | "edge" (край зоны) ----
FVG_ENTRY_MODE = "ce"        # "ce" дает лучшую цену, сокращает стоп на 30-40% и повышает R:R

# ---- Стоп-лосс: "wick" (за sweep-свечу) | "ob" (за Order Block) ----
STOP_MODE = "wick"

# ---- Старший макро-тренд (D1 / 4H Bias) ----
USE_HTF_D1_FILTER = False     # True = блокировать Longs при нисходящем тренде D1 (защита от медвежьего рынка)

# ---- Тейк-профит и динамическая калибровка R ----
USE_DYNAMIC_R = False        # True = расчет тейка на основе волатильности (Daily ATR за 14-30 дней)
DYNAMIC_R_MIN = 1.5          # минимальный тейк при низкой волатильности
DYNAMIC_R_MAX = 2.8          # максимальный тейк при сильной трендовой экспансии
PARTIAL_TAKE_R = 1.0         # 1.0R (Grid B первая цель 50% фиксации + перенос в BE)
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

