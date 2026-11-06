# -*- coding: utf-8 -*-
"""崩盘后的次日走势：条件细分（在 GitHub 美国节点运行）"""
import json, ssl, urllib.request, statistics as st
from datetime import datetime, timezone

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
U = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=30y&interval=1d"
d = json.loads(urllib.request.urlopen(urllib.request.Request(
    U, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}), timeout=40, context=ctx).read().decode())
res = d["chart"]["result"][0]
cl = [float(c) for c in res["indicators"]["quote"][0]["close"] if c is not None]
ts = [t for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c is not None]

crash = [i for i in range(1, len(cl)) if cl[i] / cl[i - 1] - 1 <= -0.03]
print(f"S&P500 {len(cl)} 根｜崩盘日 {len(crash)} 次\n")


def stat(name, idxs):
    if not idxs:
        print(f"{name}：无样本"); return
    nxt = [cl[i + 1] / cl[i] - 1 for i in idxs if i + 1 < len(cl)]
    w5 = [cl[i + 5] / cl[i] - 1 for i in idxs if i + 5 < len(cl)]
    win = sum(1 for x in nxt if x > 0) / len(nxt) * 100
    print(f"{name}（{len(nxt)} 次）：次日中位 {st.median(nxt)*100:+.2f}%｜胜率 {win:.0f}%"
          f"｜最差 {min(nxt)*100:+.2f}%"
          + (f"｜5日中位 {st.median(w5)*100:+.2f}%" if w5 else ""))


stat("全部崩盘日", crash)

# 条件1：崩盘前市场已经在跌（前5日累计 < -4%）
deep = [i for i in crash if i >= 5 and cl[i - 1] / cl[i - 6] - 1 < -0.04]
stat("崩盘前5日已跌超4%", deep)

# 条件2：连续两天大跌（前一天也跌超2%）
consec = [i for i in crash if i >= 1 and cl[i - 1] / cl[i - 2] - 1 <= -0.02]
stat("连续两日大跌", consec)

# 条件3：崩盘当天跌幅 ≥5%（更剧烈）
severe = [i for i in crash if cl[i] / cl[i - 1] - 1 <= -0.05]
stat("单日跌幅≥5%", severe)

# 条件4：崩盘+前5日已跌+当天跌幅≥4%（最接近当前）
mix = [i for i in crash if i >= 5 and cl[i - 1] / cl[i - 6] - 1 < -0.04 and cl[i] / cl[i - 1] - 1 <= -0.04]
stat("前5日跌超4% 且 当日跌超4%", mix)

print("\n最近 5 次崩盘日期：")
for i in crash[-5:]:
    print(f"  {datetime.fromtimestamp(ts[i], timezone.utc):%Y-%m-%d}  {cl[i]/cl[i-1]-1:+.2%}"
          f"  次日 {cl[i+1]/cl[i]-1:+.2%}" if i + 1 < len(cl) else "")
