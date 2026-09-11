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
CCXT_EXCHANGE = "bitget"
SYMBOL = "BTC/USDT"          # одиночный символ (для обратной совместимости)
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]  # 5 пар для тестирования
SWAP_SYMBOL = "BTC/USDT:USDT"  # для live-исполнения - бессрочный фьючерс (нужен для SHORT)
SWAP_SYMBOLS = {
    "BTC/USDT": "BTC/USDT:USDT",
    "ETH/USDT": "ETH/USDT:USDT",
    "SOL/USDT": "SOL/USDT:USDT",
    "BNB/USDT": "BNB/USDT:USDT",
    "XRP/USDT": "XRP/USDT:USDT",
}

# --- Кэш данных и отчёты ---
CACHE_DIR = "cache"
REPORTS_DIR = "reports"

# --- Google Gemini AI / Анализ и оценка сделок ---
import os as _os
GEMINI_API_KEY = _os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"  # актуальная сбалансированная модель для квант-анализа
ENABLE_AI_EVALUATION = False       # включить оценку перед входом в сделку (или через флаг --ai)
AI_CONFIDENCE_THRESHOLD = 7        # минимальный балл (1-10) для одобрения сделки ИИ

# --- Live-торговля (Bitget крипто-фьючерсы) ---
BITGET_API_KEY = _os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = _os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = _os.environ.get("BITGET_API_PASSWORD", "")  # Bitget требует passphrase
DEMO_MODE = True   # True = демо-счёт (PAPTRADING), False = РЕАЛЬНЫЕ ДЕНЬГИ
LEVERAGE = int(_os.environ.get("LEVERAGE", "3"))

# --- Риск-менеджмент и дисциплина (Prop-Firm Grade) ---
RISK_PER_TRADE_PCT = float(_os.environ.get("RISK_PER_TRADE_PCT", "1.0"))  # Базовый риск: 1% от баланса
MAX_RISK_PER_TRADE_PCT = 5.0   # Потолок риска в агрессивном режиме (до 5%)
POSITION_SIZE_USDT = 50        # фиксированный размер позиции (если не используется риск в %)
BITGET_TAKER_FEE_PCT = 0.0006  # Базовая комиссия тейкера Bitget на USDT-M фьючерсах (0.06%)
POLL_INTERVAL_SEC = 60         # как часто проверять новые сигналы

# --- T-Bank Invest API (Т-Инвестиции / Мосбиржа) ---
TBANK_TOKEN = (
    _os.environ.get("TBANK_TOKEN")
    or _os.environ.get("TINKOFF_TOKEN")
    or _os.environ.get("INVEST_TOKEN", "")
)
TBANK_ACCOUNT_ID = _os.environ.get("TBANK_ACCOUNT_ID", "")
TBANK_SANDBOX = _os.environ.get("TBANK_SANDBOX", "True").lower() in ("true", "1", "yes")
TBANK_TICKER = "SBER"
TBANK_TICKERS = ["SBER", "GAZP", "LKOH", "ROSN", "YDEX", "T"]  # ликвидная корзина акций Мосбиржи
TBANK_CLASS_CODE = "TQBR"  # TQBR - акции РФ, SPBFUT - фьючерсы
TBANK_FIGI = _os.environ.get("TBANK_FIGI", "BBG004730N88")  # SBER (если пусто - резолвится по тикеру)
TBANK_COMMISSION_PCT = 0.0005  # базовая комиссия брокера + биржи (~0.05%)
TBANK_MAX_POSITIONS = 1       # защита депозита: макс. число открытых позиций в корзине
SSL_TBANK_VERIFY = _os.environ.get("SSL_TBANK_VERIFY", "True").lower() in ("true", "1", "yes")

# Для обратной совместимости:
TINKOFF_TOKEN = TBANK_TOKEN
TINKOFF_FIGI = TBANK_FIGI

# --- Запасной источник: готовый CSV с github ---
GITHUB_CSV_URL = "https://raw.githubusercontent.com/ff137/bitstamp-btcusd-minute-data/main/data/updates/btcusd_bitstamp_1min_latest.csv"

# ---- Период (3 месяца) ----
START_DATE = "2026-06-11"
FORCE_REFRESH_DATA = False  # False = ВСЕГДА использовать локальный кэш из cache/, не качать заново

# ---- Таймфреймы (по итогам обсуждения: 1H bias + 5m sweep/вход) ----
BASE_TIMEFRAME = "1m"    # базовый TF, из которого строятся остальные ресемплингом
HTF_RULE = "1h"          # bias
LTF_RULE = "5min"        # sweep + FVG + вход

# ---- Killzones (UTC, приближённо, без учёта перехода на летнее время) ----
USE_KILLZONES = False  # False = торговать круглосуточно; True = строго в Killzones
KILLZONES = [(7, 10), (12, 15)]  # London (07-10 UTC), New York (12-15 UTC)

# ---- Продвинутые ICT-фильтры (SMT & Asian Range) ----
USE_SMT_FILTER = False          # True = входить только при подтверждении SMT-дивергенцией (BTC vs ETH)
USE_ASIAN_RANGE_FILTER = False  # True = входить только при свипе максимума/минимума Азиатской сессии
ASIAN_HOURS = (0, 6)            # Часы Азиатской сессии (00:00 - 06:00 UTC)
SMT_LOOKBACK_BARS = 12          # Окно сопоставления экстремумов для SMT (бары LTF)

# ---- Параметры стратегии ----
SWING_LENGTH_HTF = 5
SWING_LENGTH_LTF = 4
LIQUIDITY_RANGE_PCT = 0.008
MAX_FVG_ZONE_PCT = 0.006     # фильтр аномально широких FVG
MIN_RISK_PCT = 0.002         # фильтр аномально узкого стопа
FEE_SLIPPAGE_PCT = 0.0008    # комиссия + проскальзывание на сделку

# ---- Стоп-лосс: "wick" (за sweep-свечу) | "ob" (за Order Block) ----
STOP_MODE = "wick"

# ---- Тейк-профит (оптимизировано по итогам 3-месячного теста) ----
PARTIAL_TAKE_R = 1.5     # частичный тейк на 1.5R (повышает винрейт до 57%+)
PARTIAL_TAKE_SIZE = 0.5  # доля позиции, закрываемая на частичном тейке
TRAIL_DISTANCE_R = 0.8   # трейлинг остатка на 0.8R за экстремумом

# ---- Окна поиска/удержания (в минутах, на базовом 1м TF) ----
MAX_WAIT_MIN = 120        # сколько ждём возврата цены в FVG-зону (2 часа, исключает тухлые входы)
MAX_HOLD_MIN = 60 * 24 * 2  # максимальное время удержания позиции
