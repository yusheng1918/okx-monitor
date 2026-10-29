# -*- coding: utf-8 -*-
"""用 OKX 历史成交的平仓时间 vs 我们推送时间，量出真实延迟（云端运行）"""
import os, json, ssl, hmac, base64, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
KEY = os.environ.get("OKX_API_KEY", ""); SEC = os.environ.get("OKX_API_SECRET", ""); PASS = os.environ.get("OKX_API_PASSPHRASE", "")
BJ = timezone(timedelta(hours=8))
AID = "872873784163041281"

def hdr(method, path):
    n0 = datetime.now(timezone.utc)
    ts = n0.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n0.microsecond // 1000:03d}Z"
    mac = hmac.new(SEC.encode(), (ts + method + path).encode(), hashlib.sha256).digest()
    return {"OK-ACCESS-KEY": KEY, "OK-ACCESS-SIGN": base64.b64encode(mac).decode(),
            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": PASS,
            "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

def okx(path):
    try:
        with urllib.request.urlopen(urllib.request.Request("https://www.okx.com" + path,
                                                           headers=hdr("GET", path)), timeout=25, context=ctx) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"err": type(e).__name__}

d = okx(f"/api/v5/orbit/public/position-history?authorId={AID}&limit=20")
rows = d.get("data") or []
print(f"HIST|{len(rows)}")
for r in rows[:12]:
    ct = datetime.fromtimestamp(int(r["cTime"]) / 1000, BJ)
    ut = datetime.fromtimestamp(int(r["uTime"]) / 1000, BJ)
    print(f"HIST|{r['instId']}|{r['posSide']}|开 {ct:%m-%d %H:%M:%S}|平 {ut:%m-%d %H:%M:%S}|盈亏 {r.get('pnl')}")
