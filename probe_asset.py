# -*- coding: utf-8 -*-
import os, json, ssl, hmac, base64, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
KEY = os.environ.get("OKX_API_KEY", ""); SEC = os.environ.get("OKX_API_SECRET", ""); PASS = os.environ.get("OKX_API_PASSPHRASE", "")
AID = "872873784163041281"   # 天才铁头娃

def hdr(method, path):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    mac = hmac.new(SEC.encode(), (ts + method + path).encode(), hashlib.sha256).digest()
    return {"OK-ACCESS-KEY": KEY, "OK-ACCESS-SIGN": base64.b64encode(mac).decode(),
            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": PASS,
            "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

def okx(path):
    try:
        with urllib.request.urlopen(urllib.request.Request("https://www.okx.com" + path,
                                                           headers=hdr("GET", path)), timeout=20, context=ctx) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http": e.code}
    except Exception as e:
        return {"err": type(e).__name__}

C = [
    f"/api/v5/orbit/public/account?authorId={AID}",
    f"/api/v5/orbit/public/asset?authorId={AID}",
    f"/api/v5/orbit/public/trader-info?authorId={AID}",
    f"/api/v5/orbit/public/position-summary?authorId={AID}",
    f"/api/v5/orbit/public/leaderboard?authorIds={AID}&period=90",
    f"/api/v5/orbit/public/leaderboard?authorIds={AID}&sortBy=pnl",
]
for p in C:
    d = okx(p)
    print(f"[{d.get('code') or d.get('http') or d.get('err')}] {p.split('?')[0]}")
    if d.get("data"):
        print("   ", json.dumps(d["data"], ensure_ascii=False)[:300])
