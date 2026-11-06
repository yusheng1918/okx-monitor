# -*- coding: utf-8 -*-
"""标普跌4-5% / 纳指跌5% 之后的历史走势（GitHub 美国节点）"""
import json, ssl, urllib.request, statistics as st
from datetime import datetime, timezone

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def load(sym):
    U = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=30y&interval=1d"
    d = json.loads(urllib.request.urlopen(urllib.request.Request(
        U, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}), timeout=40, context=ctx).read().decode())
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    ts, cl = [], []
    for t, c in zip(r["timestamp"], q["close"]):
        if c is not None:
            ts.append(t); cl.append(float(c))
    return ts, cl

def analyze(name, sym, lo, hi):
    ts, cl = load(sym)
    idx = [i for i in range(6, len(cl)) if lo <= (cl[i] / cl[i - 1] - 1) < hi]
    if not idx:
        print(f"\n{name}：无样本"); return
    def med(idxs, h):
        a = [cl[i + h] / cl[i] - 1 for i in idxs if i + h < len(cl)]
        w = sum(1 for x in a if x > 0) / len(a) * 100 if a else 0
        return (st.median(a) * 100 if a else 0), w, (min(a) * 100 if a else 0)
    print(f"\n===== {name}｜跌幅 {lo*100:.0f}%~{hi*100:.0f}%｜样本 {len(idx)} 次 =====")
    for h, lab in ((1, "次日"), (5, "5日"), (20, "20日")):
        m, w, mn = med(idx, h)
        print(f"  {lab}：中位 {m:+.2f}%｜胜率 {w:.0f}%｜最差 {mn:+.2f}%")
    # 前5日也在跌的子集
    sub = [i for i in idx if cl[i - 1] / cl[i - 6] - 1 < -0.03]
    if sub:
        print(f"  └ 其中「前5日已跌超3%」{len(sub)} 次：", end="")
        for h in (1, 5):
            m, w, mn = med(sub, h)
            print(f"{h}日中位 {m:+.2f}%/{w:.0f}%  ", end="")
        print()
    # 收复崩盘日收盘价需要多少天
    days = []
    for i in idx:
        for j in range(i + 1, min(i + 250, len(cl))):
            if cl[j] >= cl[i - 1]:
                days.append(j - i); break
    if days:
        print(f"  收复崩盘前收盘：中位 {st.median(days):.0f} 个交易日（{len(days)}/{len(idx)} 次在一年内收复）")
    print("  最近 3 次：", end="")
    for i in idx[-3:]:
        dt = datetime.fromtimestamp(ts[i], timezone.utc).strftime("%Y-%m-%d")
        nd = cl[i + 1] / cl[i] - 1 if i + 1 < len(cl) else 0
        print(f"{dt} {cl[i]/cl[i-1]-1:+.1%}→次日{nd:+.1%}  ", end="")
    print()

analyze("标普500 跌4%~5%", "%5EGSPC", -0.05, -0.04)
analyze("标普500 跌≥5%", "%5EGSPC", -0.20, -0.05)
analyze("纳斯达克 跌≥4.5%", "%5EIXIC", -0.20, -0.045)
