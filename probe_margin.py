# -*- coding: utf-8 -*-
"""读取账户余额/持仓/最大可开仓量 与 保证金档位"""
import os, json, ssl, hmac, base64, hashlib, urllib.request
from datetime import datetime, timezone

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
K = os.environ.get("OKX_API_KEY", ""); S = os.environ.get("OKX_API_SECRET", ""); P = os.environ.get("OKX_API_PASSPHRASE", "")

def hdr(method, path):
    n = datetime.now(timezone.utc)
    ts = n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"
    mac = hmac.new(S.encode(), (ts + method + path).encode(), hashlib.sha256).digest()
    return {"OK-ACCESS-KEY": K, "OK-ACCESS-SIGN": base64.b64encode(mac).decode(),
            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": P,
            "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

def go(path, signed=True):
    h = hdr("GET", path) if signed else {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    try:
        with urllib.request.urlopen(urllib.request.Request("https://www.okx.com" + path, headers=h),
                                    timeout=20, context=ctx) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"err": type(e).__name__}

bal = go("/api/v5/account/balance?ccy=USDT")
if bal.get("data"):
    d = bal["data"][0]
    u = next((x for x in d.get("details", []) if x.get("ccy") == "USDT"), {})
    print(f"BAL|总权益usd={d.get('totalEq')}|USDT可用={u.get('availBal')}|USDT权益={u.get('eq')}|占用保证金={u.get('imr')}|冻结={u.get('frozenBal')}")
else:
    print("BAL|ERR", str(bal)[:150])

pos = go("/api/v5/account/positions?instType=SWAP")
for p in pos.get("data") or []:
    print(f"POS|{p.get('instId')}|{p.get('posSide')}|pos={p.get('pos')}|avg={p.get('avgPx')}|"
          f"lever={p.get('lever')}|mgnMode={p.get('mgnMode')}|liq={p.get('liqPx')}|"
          f"imr={p.get('imr')}|upl={p.get('upl')}|notional={p.get('notionalUsd')}")

for td in ("cross", "isolated"):
    ms = go(f"/api/v5/account/max-size?instId=ETH-USDT-SWAP&tdMode={td}&lever=100")
    if ms.get("data"):
        x = ms["data"][0]
        print(f"MAXSZ|{td}|maxBuy={x.get('maxBuy')}|maxSell={x.get('maxSell')}")
    else:
        print(f"MAXSZ|{td}|ERR {str(ms)[:120]}")

tiers = go("/api/v5/public/position-tiers?instType=SWAP&tdId=ETH-USDT-SWAP&tier=1", signed=False)
if tiers.get("data"):
    t = tiers["data"][0]
    print(f"TIER|mmr={t.get('mmr')}|imr={t.get('imr')}|maxSz={t.get('maxSz')}")
else:
    print("TIER|ERR", str(tiers)[:150])
