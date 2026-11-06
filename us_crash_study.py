# -*- coding: utf-8 -*-
"""美股崩盘日（单日跌幅≥3%）之后的历史表现（在 GitHub 美国节点运行）"""
import json, ssl, urllib.request, statistics as st
from collections import Counter

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
U = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=30y&interval=1d"
req = urllib.request.Request(U, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
d = json.loads(urllib.request.urlopen(req, timeout=40, context=ctx).read().decode())
res = d["chart"]["result"][0]
ts = res["timestamp"]
cl = res["indicators"]["quote"][0]["close"]
from datetime import datetime, timezone, timedelta
rows = []
for t, c in zip(ts, cl):
    if c is None:
        continue
    rows.append((datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), float(c)))
print(f"S&P500 {len(rows)} 根：{rows[0][0]} → {rows[-1][0]}")

crash = []
for i in range(1, len(rows)):
    r = rows[i][1] / rows[i - 1][1] - 1
    if r <= -0.03:
        crash.append((i, rows[i][0], r))
print(f"单日跌幅 ≥3% 的次数：{len(crash)}\n")

print(f"{'日期':<12}{'跌幅':<9}{'1日':<9}{'5日':<9}{'20日':<9}{'60日':<9}")
fwd = {1: [], 5: [], 20: [], 60: []}
for idx, dt, r in crash:
    line = f"{dt:<12}{r*100:<+9.2f}"
    for h in (1, 5, 20, 60):
        if idx + h < len(rows):
            v = rows[idx + h][1] / rows[idx][1] - 1
            fwd[h].append(v); line += f"{v*100:<+9.2f}"
        else:
            line += f"{'-':<9}"
    print(line)

print()
for h in (1, 5, 20, 60):
    a = fwd[h]
    if not a:
        continue
    win = sum(1 for x in a if x > 0) / len(a) * 100
    print(f"崩盘后 {h:>2} 日：中位 {st.median(a)*100:+.2f}%｜胜率 {win:.0f}%｜"
          f"最差 {min(a)*100:+.2f}%｜最好 {max(a)*100:+.2f}%（{len(a)} 次）")

print("\n各年代次数：", dict(sorted(Counter(dt[:3] + "0s" for _, dt, _ in crash).items())))
