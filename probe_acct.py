# -*- coding: utf-8 -*-
"""测试：能否用现有 OKX 密钥读取本账户真实持仓"""
import os, json, ssl, hmac, base64, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
KEY = os.environ.get("OKX_API_KEY", ""); SEC = os.environ.get("OKX_API_SECRET", ""); PASS = os.environ.get("OKX_API_PASSPHRASE", "")
print("密钥存在:", bool(KEY and SEC and PASS))
print("KEY 前缀:", (KEY[:6] + "..." + KEY[-4:]) if KEY else "无")

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
        return {"http": e.code, "body": e.read().decode()[:300]}
    except Exception as e:
        return {"err": f"{type(e).__name__}: {e}"}

for p in ["/api/v5/account/config",
          "/api/v5/account/positions?instType=SWAP",
          "/api/v5/account/balance?ccy=USDT"]:
    d = okx(p)
    print(f"\n[{p}] -> code={d.get('code')} http={d.get('http')} msg={d.get('msg')}")
    if d.get("data"):
        print("   DATA:", json.dumps(d["data"], ensure_ascii=False)[:700])
    elif d.get("body"):
        print("   BODY:", d["body"][:250])
