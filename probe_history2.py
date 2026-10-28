# -*- coding: utf-8 -*-
"""拉取三位博主的历史成交（最多100条/人）并输出为可解析行"""
import os, json, ssl, hmac, base64, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
KEY = os.environ.get("OKX_API_KEY", ""); SEC = os.environ.get("OKX_API_SECRET", ""); PASS = os.environ.get("OKX_API_PASSPHRASE", "")

IDS = {"天才铁头娃": "872873784163041281", "十一的八字": "872898006700732416", "十老板": "872838143249428480"}

def hdr(method, path):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    mac = hmac.new(SEC.encode(), (ts + method + path).encode(), hashlib.sha256).digest()
    return {"OK-ACCESS-KEY": KEY, "OK-ACCESS-SIGN": base64.b64encode(mac).decode(),
            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": PASS,
            "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

def okx(path):
    try:
        with urllib.request.urlopen(urllib.request.Request("https://www.okx.com" + path,
                                                           headers=hdr("GET", path)), timeout=25, context=ctx) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode()[:150]}
    except Exception as e:
        return {"err": type(e).__name__}

for name, aid in IDS.items():
    d = okx(f"/api/v5/orbit/public/position-history?authorId={aid}&limit=100")
    data = d.get("data")
    rows = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else [])
    print(f"HDR|{name}|{aid}|{len(rows)}|{(d.get('code') or d.get('http'))}")
    for r in rows:
        print("HIST|" + json.dumps(r, ensure_ascii=False))

print("\n===== 榜单业绩 =====")
for name, aid in IDS.items():
    d = okx(f"/api/v5/orbit/public/leaderboard?authorIds={aid}")
    data = d.get("data")
    rows = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else [])
    if rows:
        r = rows[0]
        print(f"LB|{name}|" + json.dumps(r, ensure_ascii=False))
    else:
        print(f"LB|{name}|EMPTY|{str(d)[:120]}")
