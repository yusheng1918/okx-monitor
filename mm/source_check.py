# -*- coding: utf-8 -*-
"""在 GitHub Actions / 任意服务器上先跑这个，确认数据源和 Bark 通不通。
用法: python source_check.py
"""
import json, ssl, time, urllib.request, urllib.error, socket

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

TARGETS = [
    ("Gate 永续", "https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=BTC_USDT"),
    ("Gate 现货", "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=BTC_USDT"),
    ("Binance", "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"),
    ("OKX", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"),
    ("Bybit", "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"),
    ("Kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"),
    ("Coinbase", "https://api.coinbase.com/v2/prices/BTC-USD/spot"),
    ("Bark", "https://api.day.app/ping"),
]

print(f"{'源':<12}{'状态':<8}{'耗时':>8}  返回前 90 字符")
print("-" * 90)
ok_any = False
for name, url in TARGETS:
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12, context=CTX) as r:
            body = r.read(200).decode("utf-8", "replace")
        dt = (time.time() - t0) * 1000
        print(f"{name:<12}{'OK':<8}{dt:>7.0f}ms  {body[:90]}")
        if name.startswith(("Gate", "Binance", "OKX", "Bybit", "Kraken")):
            ok_any = True
    except urllib.error.HTTPError as e:
        dt = (time.time() - t0) * 1000
        detail = ""
        try:
            detail = e.read(120).decode("utf-8", "replace")
        except Exception:
            pass
        print(f"{name:<12}{'HTTP ' + str(e.code):<8}{dt:>7.0f}ms  {detail}")
    except Exception as e:
        dt = (time.time() - t0) * 1000
        print(f"{name:<12}{'FAIL':<8}{dt:>7.0f}ms  {type(e).__name__}: {str(e)[:70]}")

print("-" * 90)
print("结论：", "至少有一个行情源可用，可以部署" if ok_any else "所有行情源都不可达，这台机器的出口 IP 被交易所拒绝")
