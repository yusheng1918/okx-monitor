# -*- coding: utf-8 -*-
"""用 OKX API 密钥查榜单，尝试按昵称找到 authorId"""
import os, json, ssl, hmac, base64, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
KEY = os.environ.get("OKX_API_KEY", "")
SEC = os.environ.get("OKX_API_SECRET", "")
PASS = os.environ.get("OKX_API_PASSPHRASE", "")
print("密钥就绪:", bool(KEY and SEC and PASS))


def sign_headers(method, path):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    msg = ts + method.upper() + path
    mac = hmac.new(SEC.encode(), msg.encode(), hashlib.sha256).digest()
    return {"OK-ACCESS-KEY": KEY, "OK-ACCESS-SIGN": base64.b64encode(mac).decode(),
            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": PASS,
            "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}


def okx_get(path):
    url = "https://www.okx.com" + path
    req = urllib.request.Request(url, headers=sign_headers("GET", path))
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode()[:200]}
    except Exception as e:
        return {"err": f"{type(e).__name__}: {e}"}


targets = ("十一的八字", "十老板")
paths = [
    "/api/v5/orbit/public/leaderboard?limit=100",
    "/api/v5/orbit/public/leaderboard?sortBy=pnl&period=30&limit=100",
    "/api/v5/orbit/public/leaderboard?sortBy=pnl&period=90&limit=100&pageNum=1",
]
for p in paths:
    d = okx_get(p)
    code = d.get("code") or d.get("http") or d.get("err")
    data = d.get("data")
    n = len(data) if isinstance(data, list) else 0
    print(f"\n[{p}] code={code} rows={n}")
    if not isinstance(data, list):
        print("   ", str(d)[:200]); continue
    for row in data[:3]:
        print("   样本:", {k: row.get(k) for k in list(row)[:6]})
    for row in data:
        nick = str(row.get("nickName") or row.get("nickname") or "")
        if any(t in nick for t in targets):
            print("   ★命中:", nick, row.get("authorId") or row.get("uid"))
