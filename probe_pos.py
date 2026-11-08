# -*- coding: utf-8 -*-
"""ZEC 费率极端值历史（Binance 资金费率，可达 333 天）"""
import json, ssl, urllib.request, statistics as st
from datetime import datetime, timezone, timedelta

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
H = {"User-Agent": "Mozilla/5.0"}
BJ = timezone(timedelta(hours=8))


def get(u):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=25, context=ctx).read().decode())


# 1) 资金费率
fr = get("https://fapi.binance.com/fapi/v1/fundingRate?symbol=ZECUSDT&limit=1000")
fr = sorted([(int(x["fundingTime"]) // 1000, float(x["fundingRate"])) for x in fr])
print(f"费率样本 {len(fr)} 条：{datetime.fromtimestamp(fr[0][0], BJ):%Y-%m-%d} → {datetime.fromtimestamp(fr[-1][0], BJ):%Y-%m-%d}")
print(f"历史最高 {max(r for _, r in fr)*10000:.1f}bp｜最低 {min(r for _, r in fr)*10000:.1f}bp")
print(f"当前 {fr[-1][1]*10000:.2f}bp\n")

# 2) 小时价
kl = get("https://fapi.binance.com/fapi/v1/klines?symbol=ZECUSDT&interval=1h&limit=1000")
bars = sorted([(int(k[0]) // 1000, float(k[4])) for k in kl])
px = [p for _, p in bars]
bt = [t for t, _ in bars]


def price_at(ts):
    for i, t in enumerate(bt):
        if t >= ts:
            return px[i]
    return None


TH = 0.0008
ev = [(t, r) for t, r in fr if r >= TH]
groups = []
for t, r in ev:
    if groups and t - groups[-1][-1][0] <= 24 * 3600:
        groups[-1].append((t, r))
    else:
        groups.append([(t, r)])

print(f"费率 ≥{TH*10000:.0f}bp 的时段：{len(groups)} 段\n")
print(f"{'开始时间':<15}{'持续h':<7}{'峰值bp':<9}{'价格':<11}{'24h后':<9}{'72h后':<9}{'7日后':<9}")
res = {"24": [], "72": [], "168": []}
for g in groups:
    t0 = g[0][0]; p0 = price_at(t0)
    if not p0:
        continue
    hours = (g[-1][0] - t0) / 3600 + 8
    peak = max(r for _, r in g) * 10000
    row = f"{datetime.fromtimestamp(t0, BJ):%m-%d %H:%M}  {hours:<7.0f}{peak:<9.1f}{p0:<11,.1f}"
    for h in (24, 72, 168):
        p1 = price_at(t0 + h * 3600)
        if p1:
            chg = (p1 / p0 - 1) * 100
            res[str(h)].append(chg); row += f"{chg:<+9.1f}"
        else:
            row += f"{'-':<9}"
    print(row)

print()
for h, lab in (("24", "24 小时"), ("72", "72 小时"), ("168", "7 天")):
    a = res[h]
    if not a:
        continue
    win = sum(1 for x in a if x > 0) / len(a) * 100
    print(f"{lab}后：中位 {st.median(a):+.1f}%｜上涨概率 {win:.0f}%｜最差 {min(a):+.1f}%｜最好 {max(a):+.1f}%（{len(a)} 次）")
