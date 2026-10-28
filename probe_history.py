# -*- coding: utf-8 -*-
"""探测历史成交接口 + 拉取三位博主的榜单业绩"""
import os, json, ssl, hmac, base64, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
KEY = os.environ.get("OKX_API_KEY", ""); SEC = os.environ.get("OKX_API_SECRET", ""); PASS = os.environ.get("OKX_API_PASSPHRASE", "")

IDS = {
    "天才铁头娃": "872873784163041281",
    "十一的八字": "872898006700732416",
    "十老板": "872838143249428480",
}

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
        return {"http": e.code, "body": e.read().decode()[:150]}
    except Exception as e:
        return {"err": f"{type(e).__name__}"}

aid = IDS["天才铁头娃"]

print("########## 1) 历史成交类接口探测 ##########")
CANDIDATES = [
    f"/api/v5/orbit/public/position-history?authorId={aid}",
    f"/api/v5/orbit/public/positions-history?authorId={aid}",
    f"/api/v5/orbit/public/trade-history?authorId={aid}",
    f"/api/v5/orbit/public/order-history?authorId={aid}",
    f"/api/v5/orbit/public/closed-positions?authorId={aid}",
    f"/api/v5/orbit/public/lead-trader/position-history?authorId={aid}",
    f"/api/v5/orbit/public/position-history?authorId={aid}&limit=100",
]
for p in CANDIDATES:
    d = okx(p)
    code = d.get("code") or d.get("http") or d.get("err")
    data = d.get("data")
    n = len(data) if isinstance(data, list) else ("dict" if isinstance(data, dict) else 0)
    note = ""
    if isinstance(data, dict):
        note = str(list(data.keys()))[:80]
    print(f"  [{code}] rows={n} {note}  <- {p.split('?')[0]}")

print("\n########## 2) 榜单业绩（30/90天） ##########")
for name, i in IDS.items():
    d = okx(f"/api/v5/orbit/public/leaderboard?authorIds={i}")
    data = d.get("data")
    rows = data.get("data") if isinstance(data, dict) else data
    if not rows:
        print(f"  {name}: 无数据 {str(d)[:120]}")
        continue
    r = rows[0]
    keys = ("nickName", "asset", "pnl", "pnlRatio", "maxDrawdown", "winRatio", "onboardDuration", "followerNum")
    print(f"  {name}: " + json.dumps({k: r.get(k) for k in keys if k in r}, ensure_ascii=False))
