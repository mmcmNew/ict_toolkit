import ccxt
ex = ccxt.bitget()
candles = ex.fetch_ohlcv("BTC/USDT", timeframe="5m", limit=10)
print(candles[-1])