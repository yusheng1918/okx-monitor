# -*- coding: utf-8 -*-
"""对比：OKX 实际持仓 vs 看板显示"""
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

def go(path):
    try:
        with urllib.request.urlopen(urllib.request.Request("https://www.okx.com" + path,
                                    headers=hdr("GET", path)), timeout=20, context=ctx) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"err": type(e).__name__}

d = go("/api/v5/account/positions?instType=SWAP")
for p in d.get("data") or []:
    pos = float(p.get("pos") or 0)
    mark = float(p.get("markPx") or 0)
    notional = float(p.get("notionalUsd") or 0)
    print(f"POS|{p.get('instId')}|pos={pos}张|avg={p.get('avgPx')}|mark={mark}|"
          f"notionalUsd={notional}|推算式qty={notional/mark if mark else 0:.4f}|liq={p.get('liqPx')}|"
          f"upl={p.get('upl')}")

inst = go("/api/v5/public/instruments?instType=SWAP&instId=ETH-USDT-SWAP")
if inst.get("data"):
    x = inst["data"][0]
    print(f"CTVAL|{x.get('instId')}|ctVal={x.get('ctVal')}|ctValCcy={x.get('ctValCcy')}|lotSz={x.get('lotSz')}|minSz={x.get('minSz')}")
