# -*- coding: utf-8 -*-
"""探测 OKX 公开接口，尝试用昵称反查 authorId"""
import json, ssl, urllib.request, urllib.error

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            txt = r.read().decode("utf-8", "replace")
            return r.status, txt[:600]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


CANDIDATES = [
    ("已知ID-看字段", "https://www.okx.com/api/v5/orbit/public/leaderboard?authorIds=966711406828093440"),
    ("榜单-无参", "https://www.okx.com/api/v5/orbit/public/leaderboard"),
    ("榜单-排序", "https://www.okx.com/api/v5/orbit/public/leaderboard?sortBy=pnl&period=30&limit=50"),
    ("交易员列表", "https://www.okx.com/api/v5/orbit/public/trader/list?limit=50"),
    ("搜索-十老板", "https://www.okx.com/api/v5/orbit/public/search?keyword=%E5%8D%81%E8%80%81%E6%9D%BF"),
    ("搜索2", "https://www.okx.com/api/v5/copytrading/public-lead-traders?limit=50"),
]

for name, url in CANDIDATES:
    st, body = get(url)
    print(f"=== {name} [{st}] ===")
    print(body.replace("\n", " ")[:420])
    print()
