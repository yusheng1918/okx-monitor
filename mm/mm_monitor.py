# -*- coding: utf-8 -*-
"""
mm_monitor.py — 多周期做市商视角监测器
用法:
  python mm_monitor.py --once      跑一轮（适合 Windows 计划任务每分钟执行）
  python mm_monitor.py --loop      常驻循环
  python mm_monitor.py --test      发一条测试推送
输出:
  dashboard.html          自动刷新的看板（手机浏览器也能开）
  monitor_data/state.json 状态与冷却
  monitor_data/events.jsonl 事件流水
  monitor_data/push.jsonl   推送流水
"""
import json, os, sys, time, math, ssl, argparse, urllib.request, urllib.error, statistics as st
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "monitor_data")
os.makedirs(DATA, exist_ok=True)
DASH_OUT = os.environ.get("DASHBOARD_PATH") or os.path.join(BASE, "dashboard.html")
TZ = timezone(timedelta(hours=8))
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
G = "https://api.gateio.ws/api/v4"

# ---------------------------------------------------------------- 基础
def now():
    return datetime.now(TZ)

def log(msg):
    line = f"[{now():%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    with open(os.path.join(DATA, "monitor.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_cfg():
    p = os.path.join(BASE, "config.json")
    cfg = {"bark_key": "", "bark_server": "https://api.day.app", "interval_sec": 60,
           "coins": ["BTC", "ETH", "ZEC"], "sound": "bell", "level_important": "timeSensitive",
           "cooldown_min": 45, "zone_cooldown_min": 90, "heartbeat_hours": 4}
    if os.path.exists(p):
        cfg.update(json.load(open(p, encoding="utf-8-sig")))
    if os.environ.get("BARK_KEY"):
        cfg["bark_key"] = os.environ["BARK_KEY"].strip()
    if os.environ.get("DASHBOARD_URL"):
        cfg["dashboard_url"] = os.environ["DASHBOARD_URL"].strip()
    return cfg

def http_json(url, tries=3, timeout=15):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(0.6 + i * 0.8)
    log(f"FETCH-FAIL {url[:80]} {type(last).__name__}")
    return None

def http_post(url, payload, timeout=12):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "User-Agent": "mm-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8"))

def load_state():
    p = os.path.join(DATA, "state.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {"cooldowns": {}, "last": {}, "events": [], "last_heartbeat": 0}

def save_state(s):
    s["events"] = s.get("events", [])[-40:]
    with open(os.path.join(DATA, "state.json"), "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)

# ---------------------------------------------------------------- 指标
def klines(coin, bar, limit):
    u = f"{G}/futures/usdt/candlesticks?contract={coin}_USDT&interval={bar}&limit={limit}"
    raw = http_json(u)
    if not raw:
        return []
    out = []
    for r in raw:
        out.append({"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]),
                    "l": float(r["l"]), "c": float(r["c"]), "v": float(r.get("sum") or 0)})
    out.sort(key=lambda x: x["t"])
    return out

def ema(v, n):
    k = 2.0 / (n + 1); e = None; out = []
    for x in v:
        e = x if e is None else e + k * (x - e); out.append(e)
    return out

def rsi(b, n=14):
    if len(b) < n + 2: return 50.0
    g = [max(b[i]["c"] - b[i-1]["c"], 0) for i in range(1, len(b))]
    l = [max(b[i-1]["c"] - b[i]["c"], 0) for i in range(1, len(b))]
    ag = sum(g[:n]) / n; al = sum(l[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n; al = (al * (n - 1) + l[i]) / n
    return 100 - 100 / (1 + (ag / al if al else 999))

def atr(b, n=14):
    if len(b) < n + 2: return 0.0
    trs = [max(b[i]["h"] - b[i]["l"], abs(b[i]["h"] - b[i-1]["c"]), abs(b[i]["l"] - b[i-1]["c"]))
           for i in range(1, len(b))]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a

def macd_hist(cl):
    e12, e26 = ema(cl, 12), ema(cl, 26)
    dif = [x - y for x, y in zip(e12, e26)]
    dea = ema(dif, 9)
    return dif[-1] - dea[-1]

def dmi(b, n=14):
    if len(b) < n * 2: return 0, 0, 0
    pdm, ndm, tr = [], [], []
    for i in range(1, len(b)):
        up = b[i]["h"] - b[i-1]["h"]; dn = b[i-1]["l"] - b[i]["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(b[i]["h"] - b[i]["l"], abs(b[i]["h"] - b[i-1]["c"]), abs(b[i]["l"] - b[i-1]["c"])))
    def sm(x):
        s = sum(x[:n]); o = [s]
        for v in x[n:]:
            s = s - s / n + v; o.append(s)
        return o
    t_, p_, n_ = sm(tr), sm(pdm), sm(ndm)
    pdi = 100 * p_[-1] / t_[-1] if t_[-1] else 0
    ndi = 100 * n_[-1] / t_[-1] if t_[-1] else 0
    dx = [100 * abs(100*p_[i]/t_[i] - 100*n_[i]/t_[i]) / ((100*p_[i]/t_[i] + 100*n_[i]/t_[i]) or 1)
          for i in range(len(t_))]
    a = sum(dx[:n]) / n
    for d in dx[n:]:
        a = (a * (n - 1) + d) / n
    return pdi, ndi, a

def swings(b, k=3, keep=5):
    hs, ls = [], []
    for i in range(k, len(b) - k):
        seg = b[i-k:i+k+1]
        if b[i]["h"] == max(x["h"] for x in seg): hs.append(b[i]["h"])
        if b[i]["l"] == min(x["l"] for x in seg): ls.append(b[i]["l"])
    return hs[-keep:], ls[-keep:]

def volume_profile(bars, nbins=90):
    if not bars: return None
    lo = min(b["l"] for b in bars); hi = max(b["h"] for b in bars)
    if hi <= lo: return None
    step = (hi - lo) / nbins
    prof = [0.0] * nbins
    for b in bars:
        i0 = max(0, min(nbins - 1, int((b["l"] - lo) / step)))
        i1 = max(0, min(nbins - 1, int((b["h"] - lo) / step)))
        v = b["v"] / max(1, i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            prof[i] += v
    tot = sum(prof) or 1
    ci = max(range(nbins), key=lambda i: prof[i])
    poc = lo + (ci + 0.5) * step
    acc = prof[ci]; a, b_ = ci, ci
    while acc < tot * 0.7 and (a > 0 or b_ < nbins - 1):
        up = prof[b_ + 1] if b_ < nbins - 1 else -1
        dn = prof[a - 1] if a > 0 else -1
        if up >= dn: b_ += 1; acc += max(0, up)
        else: a -= 1; acc += max(0, dn)
    return {"poc": poc, "val": lo + a * step, "vah": lo + (b_ + 1) * step, "lo": lo, "hi": hi}

def tf_signal(bars):
    cl = [b["c"] for b in bars]
    px = cl[-1]
    e20 = ema(cl, 20)[-1]
    e50 = ema(cl, 50)[-1] if len(cl) > 50 else e20
    e200 = ema(cl, 200)[-1] if len(cl) > 200 else None
    r = rsi(bars)
    hist = macd_hist(cl)
    pdi, ndi, adx = dmi(bars)
    hs, ls = swings(bars, k=2, keep=4)
    struct = "—"
    if len(hs) >= 3 and len(ls) >= 3:
        if hs[-1] > hs[-2] and ls[-1] > ls[-2]: struct = "HH/HL"
        elif hs[-1] < hs[-2] and ls[-1] < ls[-2]: struct = "LH/LL"
        elif hs[-1] < hs[-2]: struct = "LH"
        else: struct = "HL"
    sig = [px > e20, e20 > e50, (px > e200 if e200 else False), hist > 0, r > 50, pdi > ndi, struct == "HH/HL"]
    score = sum(1 if s else -1 for s in sig)
    return {"score": score, "rsi": r, "adx": adx, "pdi": pdi, "ndi": ndi, "struct": struct,
            "px_vs_e20": (px / e20 - 1) * 100, "ema20": e20, "ema50": e50, "ema200": e200,
            "macd_bp": hist / px * 1e4,
            "label": "偏多" if score >= 3 else ("偏空" if score <= -3 else "中性")}

# ---------------------------------------------------------------- 单币分析
def analyze_gate(coin):
    h1 = klines(coin, "1h", 800)
    h4 = klines(coin, "4h", 300)
    d1 = klines(coin, "1d", 160)
    if not h1 or not h4 or not d1:
        return None
    meta = http_json(f"{G}/futures/usdt/contracts/{coin}_USDT") or {}
    stats = http_json(f"{G}/futures/usdt/contract_stats?contract={coin}_USDT&interval=1h&limit=48") or []
    tick = http_json(f"{G}/spot/tickers?currency_pair={coin}_USDT") or [{}]
    tick = tick[0] if isinstance(tick, list) else tick

    px = h1[-1]["c"]
    s1, s4, sd = tf_signal(h1), tf_signal(h4), tf_signal(d1)
    total = s1["score"] + s4["score"] + sd["score"]
    bias = "偏空" if total <= -3 else ("偏多" if total >= 3 else "震荡")

    vp30 = volume_profile(h1[-720:]) if len(h1) >= 100 else volume_profile(h1)
    vp90 = volume_profile(d1[-90:])
    a4 = atr(h4, 14)
    hs, ls = swings(h4, k=3, keep=5)

    days = {}
    for b in h1:
        days.setdefault(b["t"] // 86400, []).append(b)
    dk = sorted(days)
    pdh = max(x["h"] for x in days[dk[-2]]) if len(dk) > 1 else None
    pdl = min(x["l"] for x in days[dk[-2]]) if len(dk) > 1 else None
    weeks = {}
    for b in h1:
        weeks.setdefault(b["t"] // (86400 * 7), []).append(b)
    wk = sorted(weeks)
    pwh = max(x["h"] for x in weeks[wk[-2]]) if len(wk) > 1 else None
    pwl = min(x["l"] for x in weeks[wk[-2]]) if len(wk) > 1 else None

    oi_now = stats[-1].get("open_interest") if stats else None
    oi_24 = stats[-25].get("open_interest") if len(stats) > 25 else None
    oi_chg = (oi_now / oi_24 - 1) * 100 if oi_now and oi_24 else 0
    funding = float(meta.get("funding_rate") or 0) * 1e4
    liq_l = sum((x.get("long_liq_usd") or 0) for x in stats[-24:]) / 1e6
    liq_s = sum((x.get("short_liq_usd") or 0) for x in stats[-24:]) / 1e6

    return {
        "coin": coin, "px": px, "chg24": (px / h1[-25]["c"] - 1) * 100 if len(h1) > 25 else 0,
        "chg7d": (px / h1[-169]["c"] - 1) * 100 if len(h1) > 169 else 0,
        "h1": h1, "h4": h4, "d1": d1,
        "tf": {"1h": s1, "4h": s4, "1d": sd}, "total": total, "bias": bias,
        "vp30": vp30, "vp90": vp90, "atr4h": a4, "atr_pct": a4 / px * 100,
        "sw_hi": hs, "sw_lo": ls, "pdh": pdh, "pdl": pdl, "pwh": pwh, "pwl": pwl,
        "ema20": s4["ema20"], "ema50": s4["ema50"], "ema200": s4["ema200"],
        "funding": funding, "funding_ann": funding * 3 * 365 / 100,
        "oi_usd": (float(meta.get("position_size") or 0) * float(meta.get("quanto_multiplier") or 1) * px / 1e6),
        "oi_chg": oi_chg, "liq_l": liq_l, "liq_s": liq_s,
        "lsr": (stats[-1].get("lsr_account") if stats else None),
        "top_lsr": (stats[-1].get("top_lsr_size") if stats else None),
        "spot": float(tick.get("last") or px),
        "hi24": max(b["h"] for b in h1[-24:]), "lo24": min(b["l"] for b in h1[-24:]),
        "hi7d": max(b["h"] for b in h1[-168:]), "lo7d": min(b["l"] for b in h1[-168:]),
        "hi30d": max(b["h"] for b in h1[-720:]), "lo30d": min(b["l"] for b in h1[-720:]),
        "source": "Gate 永续", "spot_only": False,
    }


KRAKEN_PAIR = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "ZEC": "ZECUSD"}

def kraken_ohlc(coin, interval, limit):
    pair = KRAKEN_PAIR.get(coin)
    if not pair:
        return []
    d = http_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}", tries=2)
    if not d or d.get("error"):
        return []
    res = d.get("result") or {}
    key = next((k for k in res if k != "last"), None)
    if not key:
        return []
    out = []
    for x in res[key][-limit:]:
        try:
            out.append({"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
                        "c": float(x[4]), "v": float(x[6]) * float(x[5])})
        except Exception:
            continue
    return out


def analyze_spot(coin):
    """降级模式：只看现货 K 线，无持仓量/资金费率/多空比。"""
    h1 = kraken_ohlc(coin, 60, 720)
    h4 = kraken_ohlc(coin, 240, 300)
    d1 = kraken_ohlc(coin, 1440, 160)
    if len(h1) < 200 or len(h4) < 60 or len(d1) < 30:
        return None
    px = h1[-1]["c"]
    s1, s4, sd = tf_signal(h1), tf_signal(h4), tf_signal(d1)
    total = s1["score"] + s4["score"] + sd["score"]
    vp30 = volume_profile(h1[-720:])
    vp90 = volume_profile(d1[-90:])
    a4 = atr(h4, 14)
    hs, ls = swings(h4, k=3, keep=5)
    days = {}
    for b in h1:
        days.setdefault(b["t"] // 86400, []).append(b)
    dk = sorted(days)
    pdh = max(x["h"] for x in days[dk[-2]]) if len(dk) > 1 else None
    pdl = min(x["l"] for x in days[dk[-2]]) if len(dk) > 1 else None
    weeks = {}
    for b in h1:
        weeks.setdefault(b["t"] // (86400 * 7), []).append(b)
    wk = sorted(weeks)
    pwh = max(x["h"] for x in weeks[wk[-2]]) if len(wk) > 1 else None
    pwl = min(x["l"] for x in weeks[wk[-2]]) if len(wk) > 1 else None
    return {
        "coin": coin, "px": px,
        "chg24": (px / h1[-25]["c"] - 1) * 100 if len(h1) > 25 else 0,
        "chg7d": (px / h1[-169]["c"] - 1) * 100 if len(h1) > 169 else 0,
        "h1": h1, "h4": h4, "d1": d1,
        "tf": {"1h": s1, "4h": s4, "1d": sd}, "total": total,
        "bias": "偏空" if total <= -3 else ("偏多" if total >= 3 else "震荡"),
        "vp30": vp30, "vp90": vp90, "atr4h": a4, "atr_pct": a4 / px * 100,
        "sw_hi": hs, "sw_lo": ls, "pdh": pdh, "pdl": pdl, "pwh": pwh, "pwl": pwl,
        "ema20": s4["ema20"], "ema50": s4["ema50"], "ema200": s4["ema200"],
        "funding": 0.0, "funding_ann": 0.0, "oi_usd": 0.0, "oi_chg": 0.0,
        "liq_l": 0.0, "liq_s": 0.0, "lsr": None, "top_lsr": None, "spot": px,
        "hi24": max(b["h"] for b in h1[-24:]), "lo24": min(b["l"] for b in h1[-24:]),
        "hi7d": max(b["h"] for b in h1[-168:]), "lo7d": min(b["l"] for b in h1[-168:]),
        "hi30d": max(b["h"] for b in h1[-720:]), "lo30d": min(b["l"] for b in h1[-720:]),
        "source": "Kraken 现货（降级）", "spot_only": True,
    }


def analyze(coin):
    a = analyze_gate(coin)
    if a:
        return a
    log(f"{coin} Gate 不可用，切换 Kraken 现货降级模式")
    return analyze_spot(coin)

# ---------------------------------------------------------------- 价位/区间
def dec_of(px):
    return 0 if px > 20000 else (1 if px > 1000 else 2)

def build_zones(a):
    px = a["px"]; atr4 = a["atr4h"] or px * 0.01
    up, dn = [], []
    def add(lst, p, w, tag):
        if p and p > 0:
            lst.append((p, w, tag))
    v30, v90 = a["vp30"] or {}, a["vp90"] or {}
    add(up, v30.get("vah"), 3.0, "30日价值区上沿")
    add(up, v30.get("poc"), 3.2, "30日POC")
    add(up, a["ema20"], 2.0, "EMA20(4H)")
    add(up, a["ema50"], 2.2, "EMA50(4H)")
    add(up, a["ema200"], 2.4, "EMA200(4H)")
    for s in a["sw_hi"][-3:]:
        add(up, s, 2.5, "4H前高")
    add(up, a["pdh"], 1.8, "昨日高")
    add(up, a["pwh"], 1.8, "上周高")
    add(up, a["hi24"], 1.4, "24h高")
    add(up, a["hi7d"], 1.6, "7日高")
    add(up, a["hi30d"], 2.0, "30日高")
    add(dn, v30.get("val"), 3.0, "30日价值区下沿")
    add(dn, v30.get("poc"), 3.2, "30日POC")
    add(dn, v90.get("val"), 2.6, "90日价值区下沿")
    for s in a["sw_lo"][-3:]:
        add(dn, s, 2.5, "4H前低")
    add(dn, a["pdl"], 1.8, "昨日低")
    add(dn, a["pwl"], 1.8, "上周低")
    add(dn, a["lo24"], 1.5, "24h低")
    add(dn, a["lo7d"], 1.7, "7日低")
    add(dn, a["lo30d"], 2.0, "30日低")
    for r in (0.382, 0.5, 0.618):
        add(up, a["lo30d"] + (a["hi30d"] - a["lo30d"]) * r, 1.2, f"fib{int(r*1000)}")
    step = 10 ** math.floor(math.log10(px)) / 10
    for k in (0, 1, 2):
        add(up, (math.floor(px / step) + 1 + k) * step, 1.2 - k * 0.15, "整数关")
        add(dn, (math.floor(px / step) - k) * step, 1.2 - k * 0.15, "整数关")

    def cluster(items, side):
        items = sorted(items)
        out = []
        for p, w, tag in items:
            if side == "up" and p <= px * 1.002: continue
            if side == "dn" and p >= px * 0.998: continue
            if out and abs(p - out[-1]["p"]) / out[-1]["p"] < 0.004:
                c = out[-1]
                c["p"] = (c["p"] * c["n"] + p) / (c["n"] + 1)
                c["score"] += w; c["n"] += 1; c["tags"].append(tag)
            else:
                out.append({"p": p, "score": w, "n": 1, "tags": [tag]})
        for c in out:
            c["dist_atr"] = (c["p"] - px) / atr4
        return out

    upc = [c for c in cluster(up, "up") if 0.5 <= c["dist_atr"] <= 5.0]
    dnc = [c for c in cluster(dn, "dn") if -5.0 <= c["dist_atr"] <= -0.5]

    def choose(cl, side):
        """按由近及远取最多 3 档，彼此至少隔 0.8 个 ATR；过滤弱簇"""
        if not cl:
            return []
        cl = sorted(cl, key=lambda c: abs(c["dist_atr"]))
        sel = []
        for c in cl:
            if c["score"] < 2.0:
                continue
            if all(abs(abs(c["dist_atr"]) - abs(s["dist_atr"])) >= 0.8 for s in sel):
                sel.append(c)
            if len(sel) == 3:
                break
        return sel or cl[:1]

    def nearest_opp(side, entry):
        """止盈指向最近的对手方成交密集区"""
        pool = dnc if side == "up" else upc
        cand = [abs(c["p"] - entry) for c in pool
                if (c["p"] < entry - atr4 * 1.0 if side == "up" else c["p"] > entry + atr4 * 1.0)]
        return min(cand) if cand else None

    def size_zone(c, side):
        hw = max(atr4 * 0.28, px * 0.0022)
        lo, hi = c["p"] - hw, c["p"] + hw
        d_tp = nearest_opp(side, c["p"]) or atr4 * 2.2
        d_tp = max(min(d_tp, atr4 * 1.9), atr4 * 0.9)      # 止盈距离夹在 1.0~3.2 ATR
        if side == "up":
            stop = hi + atr4 * 0.65
            tp1 = c["p"] - d_tp
            tp2 = tp1 - atr4 * 1.3
        else:
            stop = lo - atr4 * 0.65
            tp1 = c["p"] + d_tp
            tp2 = tp1 + atr4 * 1.3
        return lo, hi, stop, tp1, tp2

    zones = {"sell": [], "buy": []}
    for c in choose(upc, "up"):
        lo, hi, stop, tp1, tp2 = size_zone(c, "up")
        rr = (c["p"] - tp1) / (stop - c["p"]) if stop > c["p"] else 0
        zones["sell"].append({"tag": "顺势" if a["bias"] == "偏空" else "逆势", "lo": lo, "hi": hi,
                              "entry": c["p"], "stop": stop, "tp1": tp1, "tp2": tp2, "rr": rr,
                              "score": c["score"], "reasons": list(dict.fromkeys(c["tags"]))})
    for c in choose(dnc, "dn"):
        lo, hi, stop, tp1, tp2 = size_zone(c, "dn")
        rr = (tp1 - c["p"]) / (c["p"] - stop) if c["p"] > stop else 0
        zones["buy"].append({"tag": "逆势" if a["bias"] == "偏空" else "顺势", "lo": lo, "hi": hi,
                             "entry": c["p"], "stop": stop, "tp1": tp1, "tp2": tp2, "rr": rr,
                             "score": c["score"], "reasons": list(dict.fromkeys(c["tags"]))})
    if os.environ.get("MM_DEBUG"):
        log(f"DEBUG {a['coin']} up={[(round(c['p'],1), round(c['score'],1), round(c['dist_atr'],2), c['tags']) for c in upc]}")
        log(f"DEBUG {a['coin']} dn={[(round(c['p'],1), round(c['score'],1), round(c['dist_atr'],2), c['tags']) for c in dnc]}")
    return zones

# ---------------------------------------------------------------- 推送
def push(cfg, title, body, coin=None, important=False, url=None, dry=False):
    if dry or not cfg.get("bark_key"):
        rec = {"ts": now().isoformat(), "title": title, "body": body, "sent": False,
               "reason": "dry-run" if dry else "no-key"}
        with open(os.path.join(DATA, "push.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log(f"PUSH-SKIP {title}")
        return False
    payload = {"device_key": cfg["bark_key"], "title": title, "body": body,
               "group": coin or "行情", "sound": cfg.get("sound", "bell"),
               "icon": "https://api.day.app/static/favicon.ico"}
    if important:
        payload["level"] = cfg.get("level_important", "timeSensitive")
    if url:
        payload["url"] = url
    try:
        r = http_post(cfg["bark_server"].rstrip("/") + "/push", payload)
        ok = r.get("code") == 200
        with open(os.path.join(DATA, "push.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now().isoformat(), "title": title, "body": body,
                                "sent": ok, "resp": r}, ensure_ascii=False) + "\n")
        log(f"PUSH {'OK' if ok else 'FAIL'} {title}")
        return ok
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:160]
        except Exception:
            detail = ""
        log(f"PUSH-HTTP {e.code} {detail} {title}")
        return False
    except Exception as e:
        log(f"PUSH-ERR {type(e).__name__} {str(e)[:60]}")
        return False

# ---------------------------------------------------------------- 触发判断
def fire(state, key, minutes, ts):
    last = state["cooldowns"].get(key, 0)
    if ts - last < minutes * 60:
        return False
    state["cooldowns"][key] = ts
    return True

def check(a, z, state, cfg):
    ts = time.time(); ev = []
    px = a["px"]; c = a["coin"]; d = dec_of(px)
    st = state["last"].get(c)
    if not st:                      # 第一次运行只建基线，不推历史状态
        return ev
    prev_px = st.get("px", px)
    act = state.setdefault("active", {}).get(c)

    # 1) 已持仓：先判止损/止盈
    if act:
        if (ts - act["ts"]) > 86400:
            state["active"].pop(c, None); act = None
        elif act["side"] == "sell":
            if px >= act["stop"]:
                ev.append(dict(coin=c, rule="stop", ts=ts, important=True,
                               title=f"{c} 空单止损 {act['stop']:,.{d}f}",
                               body=f"卖区逻辑作废，按纪律离场。现价 {px:,.{d}f}\n不要摊平，等新结构再挂"))
                state["active"].pop(c, None); act = None
            elif not act.get("tp1_hit") and px <= act["tp1"]:
                ev.append(dict(coin=c, rule="tp", ts=ts, important=False,
                               title=f"{c} 空单到达止盈1 {act['tp1']:,.{d}f}",
                               body=f"建议减半仓，剩余止损移到成本 {act['entry']:,.{d}f}。现价 {px:,.{d}f}"))
                act["tp1_hit"] = True
        elif act["side"] == "buy":
            if px <= act["stop"]:
                ev.append(dict(coin=c, rule="stop", ts=ts, important=True,
                               title=f"{c} 多单止损 {act['stop']:,.{d}f}",
                               body=f"买区逻辑作废，按纪律离场。现价 {px:,.{d}f}"))
                state["active"].pop(c, None); act = None
            elif not act.get("tp1_hit") and px >= act["tp1"]:
                ev.append(dict(coin=c, rule="tp", ts=ts, important=False,
                               title=f"{c} 多单到达止盈1 {act['tp1']:,.{d}f}",
                               body=f"建议减半仓，剩余止损移到成本 {act['entry']:,.{d}f}。现价 {px:,.{d}f}"))
                act["tp1_hit"] = True

    # 2) 未持仓：判断是否进入区间
    if not state["active"].get(c):
        for side, label in (("sell", "卖区"), ("buy", "买区")):
            for i, zz in enumerate(z[side]):
                inside = zz["lo"] <= px <= zz["hi"]
                near = abs(px / zz["entry"] - 1) < 0.0012
                if (inside or near) and fire(state, f"{c}:zone{side}{i}", cfg["zone_cooldown_min"], ts):
                    dirn = "开空" if side == "sell" else "开多"
                    ev.append(dict(coin=c, rule="zone", ts=ts, important=True,
                                   title=f"{c} 进入{label} {zz['entry']:,.{d}f}",
                                   body=(f"{dirn}·{zz['tag']}｜挂单 {zz['lo']:,.{d}f}-{zz['hi']:,.{d}f}\n"
                                         f"止损 {zz['stop']:,.{d}f}｜TP {zz['tp1']:,.{d}f}/{zz['tp2']:,.{d}f}\n"
                                         f"{'中仓2%' if zz['tag']=='顺势' else '轻仓1%'}｜RR {zz['rr']:.2f}｜{'、'.join(zz['reasons'][:2])}")))
                    state["active"][c] = {"side": side, "idx": i, "entry": zz["entry"], "stop": zz["stop"],
                                          "tp1": zz["tp1"], "tp2": zz["tp2"], "tag": zz["tag"],
                                          "ts": ts, "tp1_hit": False}
                    break
            if state["active"].get(c):
                break

    # 3) 结构穿越（需要前值，避免首轮误报）
    v30 = a["vp30"] or {}
    if v30.get("vah") and prev_px <= v30["vah"] < px and fire(state, f"{c}:breakup", 360, ts):
        ev.append(dict(coin=c, rule="structure", ts=ts, important=True,
                       title=f"{c} 收复30日价值区上沿",
                       body=f"现价 {px:,.{d}f} 上破 {v30['vah']:,.{d}f}，空头逻辑需重估"))
    if v30.get("val") and prev_px >= v30["val"] > px and fire(state, f"{c}:breakdn", 360, ts):
        ev.append(dict(coin=c, rule="structure", ts=ts, important=False,
                       title=f"{c} 跌破30日价值区下沿",
                       body=f"现价 {px:,.{d}f} 下破 {v30['val']:,.{d}f}，进入折价区，等扫单企稳"))

    if abs(a["funding"]) >= 1.0 and not a.get("spot_only") and fire(state, f"{c}:funding", 240, ts):
        ev.append(dict(coin=c, rule="funding", ts=ts, important=False,
                       title=f"{c} 资金费率 {a['funding']:+.2f}bp",
                       body=f"年化 {a['funding_ann']:+.0f}%，{'多头拥挤' if a['funding']>0 else '空头拥挤'}｜现价 {px:,.{d}f}"))
    if abs(a["oi_chg"]) >= 2.5 and not a.get("spot_only") and fire(state, f"{c}:oi", 180, ts):
        ev.append(dict(coin=c, rule="oi", ts=ts, important=False,
                       title=f"{c} 持仓量异动 {a['oi_chg']:+.1f}%",
                       body=f"OI ${a['oi_usd']/1000:.2f}B｜24h强平 多{a['liq_l']:.0f}M/空{a['liq_s']:.0f}M"))
    r1 = a["tf"]["1h"]["rsi"]
    if (r1 < 20 or r1 > 80) and fire(state, f"{c}:rsi", 120, ts):
        ev.append(dict(coin=c, rule="rsi", ts=ts, important=False,
                       title=f"{c} 1H RSI {r1:.0f}",
                       body=f"{'超卖，反弹概率上升' if r1<20 else '超买，回落风险'}｜现价 {px:,.{d}f}"))
    return ev

# ---------------------------------------------------------------- 看板
CSS = """
:root{--bg:#0b1017;--card:#121a26;--line:#1f2b3d;--txt:#dbe4f2;--sub:#8b9ab0;--green:#1fbf9a;--red:#ef5a55;--amber:#f5a623;--blue:#4a9eff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:18px 14px 60px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:20px;font-weight:600;letter-spacing:.4px}
.top{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;margin-bottom:6px}
.meta{color:var(--sub);font-size:12px}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;border:1px solid var(--line)}
.bear{background:rgba(239,90,85,.12);color:var(--red);border-color:rgba(239,90,85,.35)}
.bull{background:rgba(31,191,154,.12);color:var(--green);border-color:rgba(31,191,154,.35)}
.neu{background:rgba(245,166,35,.12);color:var(--amber);border-color:rgba(245,166,35,.35)}
.live{background:rgba(74,158,255,.12);color:var(--blue);border-color:rgba(74,158,255,.35)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-top:16px}
.ch{display:flex;flex-wrap:wrap;align-items:center;gap:14px;margin-bottom:12px}
.px{font-size:26px;font-weight:600}
.chg{font-size:13px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px;margin:12px 0 16px}
.m{background:#0e1621;border:1px solid var(--line);border-radius:10px;padding:9px 11px}
.m span{display:block;color:var(--sub);font-size:11px;margin-bottom:3px}
.m b{font-size:15px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{background:#0e1621;color:var(--sub);font-weight:500;text-align:left;padding:8px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 9px;border-bottom:1px solid #17202e;vertical-align:top}
tr:last-child td{border-bottom:none}
.dir-s{color:var(--red);font-weight:600}
.dir-b{color:var(--green);font-weight:600}
.tag{font-size:11px;padding:1px 7px;border-radius:5px;border:1px solid}
.t-s{color:var(--red);border-color:rgba(239,90,85,.4);background:rgba(239,90,85,.1)}
.t-r{color:var(--amber);border-color:rgba(245,166,35,.4);background:rgba(245,166,35,.1)}
.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.sub{color:var(--sub);font-size:11.5px}
.bars{display:flex;gap:4px;margin-top:8px}
.bar{height:5px;border-radius:3px;background:#223047}
.bar.on{background:var(--red)}
.bar.on2{background:var(--green)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.grid2{grid-template-columns:1fr} table{font-size:11px} th,td{padding:6px 4px} .px{font-size:22px} .wrap{padding:0}}
.ev{font-size:12.5px;border-left:2px solid var(--line);padding:4px 0 4px 10px;margin-bottom:8px}
.ev b{color:var(--amber);font-weight:600}
.foot{color:var(--sub);font-size:11.5px;margin-top:26px;line-height:1.8}
.pill{font-size:11px;color:var(--sub);border:1px solid var(--line);border-radius:6px;padding:1px 6px}
"""

def fmt(x, d):
    return f"{x:,.{d}f}"

def render(dash, cfg):
    coins = dash["coins"]; ts = dash["ts"]; events = dash["events"]
    bias = coins[0]["bias"] if coins else "震荡"
    bcls = "bear" if bias == "偏空" else ("bull" if bias == "偏多" else "neu")
    mode = "LIVE 已连接" if cfg.get("bark_key") else "DRY-RUN 未配置Key"
    total_txt = " ".join(f"{c['coin']} {c['total']:+d}" for c in coins)

    h = []
    h.append(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{max(30, cfg.get('interval_sec',60))}">
<title>做市商监测 · BTC ETH ZEC</title><style>{CSS}</style></head><body><div class="wrap">
<div class="top"><h1>做市商视角 · 多周期监测</h1>
<span class="badge {bcls}">整体 {bias}</span>
<span class="badge {'live' if cfg.get('bark_key') else 'neu'}">{mode}</span>
<span class="badge neu">每 {cfg.get('interval_sec',60)} 秒自动刷新</span></div>
<div class="meta">更新时间 {ts}　｜　周期共振 {total_txt}　｜　数据源 Gate 永续 + Binance 现货（OKX 报价通常偏差 ±0.05%~0.15%）</div>
""")

    for c in coins:
        d = dec_of(c["px"])
        tf = c["tf"]
        def bcls2(lbl):
            return "bear" if "偏空" in lbl else ("bull" if "偏多" in lbl else "neu")
        rows = []
        for side, label, key in (("sell", "卖出区（做空）", "sell"), ("buy", "买入区（做多）", "buy")):
            for i, z in enumerate(c["zones"][key]):
                dcls = "dir-s" if side == "sell" else "dir-b"
                dname = "开空" if side == "sell" else "开多"
                tcls = "t-s" if z["tag"] == "顺势" else "t-r"
                rows.append(f"""<tr>
<td class="{dcls}">{dname}</td><td><span class="tag {tcls}">{z['tag']}</span></td>
<td class="num">{fmt(z['lo'],d)} - {fmt(z['hi'],d)}</td>
<td class="num">{fmt(z['entry'],d)}</td><td class="num">{fmt(z['stop'],d)}</td>
<td class="num">{fmt(z['tp1'],d)}</td><td class="num">{fmt(z['tp2'],d)}</td>
<td class="num">{z['rr']:.2f}</td>
<td>{'中仓 2%' if z['tag']=='顺势' else '轻仓 1%'}</td>
<td class="sub">{'、'.join(z['reasons'][:3])}</td></tr>""")
        lv = f"""<tr><td class="num">{fmt(c['vp30']['vah'],d)}</td><td>30日VAH</td><td class="num">{(c['vp30']['vah']/c['px']-1)*100:+.2f}%</td></tr>
<tr><td class="num">{fmt(c['vp30']['poc'],d)}</td><td>30日POC（MM成本）</td><td class="num">{(c['vp30']['poc']/c['px']-1)*100:+.2f}%</td></tr>
<tr><td class="num">{fmt(c['vp30']['val'],d)}</td><td>30日VAL</td><td class="num">{(c['vp30']['val']/c['px']-1)*100:+.2f}%</td></tr>
<tr><td class="num">{fmt(c['vp90']['val'],d)}</td><td>90日VAL</td><td class="num">{(c['vp90']['val']/c['px']-1)*100:+.2f}%</td></tr>
<tr><td class="num">{fmt(c['ema200'],d)}</td><td>EMA200(4H)</td><td class="num">{(c['ema200']/c['px']-1)*100:+.2f}%</td></tr>""" if c["vp90"] else ""

        h.append(f"""<div class="card">
<div class="ch"><div class="px">{c['coin']} {fmt(c['px'],d)}</div>
<span class="chg {('bull' if c['chg24']>=0 else 'inherit')}" style="color:{'#1fbf9a' if c['chg24']>=0 else '#ef5a55'}">{c['chg24']:+.2f}% 24h</span>
<span class="badge {bcls2(tf['1d']['label'])}">日线 {tf['1d']['label']} {tf['1d']['score']:+d}</span>
<span class="badge {bcls2(tf['4h']['label'])}">4H {tf['4h']['label']} {tf['4h']['score']:+d}</span>
<span class="badge {bcls2(tf['1h']['label'])}">1H {tf['1h']['label']} {tf['1h']['score']:+d}</span>
<span class="badge {bcls}">综合 {c['bias']} {c['total']:+d}</span>
<span class="badge neu">{c.get('source','Gate 永续')}</span></div>
<div class="metrics">
<div class="m"><span>1H RSI / 4H RSI</span><b>{tf['1h']['rsi']:.0f} / {tf['4h']['rsi']:.0f}</b></div>
<div class="m"><span>ADX(4H)</span><b>{tf['4h']['adx']:.0f}</b></div>
<div class="m"><span>ATR(4H)</span><b>{c['atr_pct']:.2f}%</b></div>
<div class="m"><span>资金费率</span><b style="color:{'#8b9ab0' if c.get('spot_only') else ('#ef5a55' if c['funding']>0 else '#1fbf9a')}">{'—' if c.get('spot_only') else format(c['funding'], '+.2f') + 'bp'}</b></div>
<div class="m"><span>OI 24h</span><b>{'—' if c.get('spot_only') else format(c['oi_chg'], '+.1f') + '%'}</b></div>
<div class="m"><span>散户多空比</span><b>{format(c['lsr'], '.2f') if c['lsr'] else '—'}</b></div>
<div class="m"><span>大户持仓比</span><b>{format(c['top_lsr'], '.2f') if c['top_lsr'] else '—'}</b></div>
<div class="m"><span>24h 强平 多/空</span><b>{'—' if c.get('spot_only') else format(c['liq_l'], '.0f') + 'M / ' + format(c['liq_s'], '.0f') + 'M'}</b></div>
</div>
<table><thead><tr><th>方向</th><th>属性</th><th>挂单区间</th><th>入场</th><th>止损</th><th>TP1</th><th>TP2</th><th>RR</th><th>仓位</th><th>依据</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<div class="grid2" style="margin-top:16px">
<div><table><thead><tr><th>关键价位</th><th>类型</th><th>距现价</th></tr></thead><tbody>{lv}</tbody></table></div>
<div class="sub" style="padding-top:4px">
结构：{tf['4h']['struct']}　｜　24h 区间 {fmt(c['lo24'],d)} - {fmt(c['hi24'],d)}<br>
现价距 30 日低点 {(c['px']/c['lo30d']-1)*100:+.1f}%，距 30 日高点 {(c['px']/c['hi30d']-1)*100:+.1f}%<br>
EMA20/50/200(4H)：{fmt(c['ema20'],d)} / {fmt(c['ema50'],d)} / {fmt(c['ema200'],d)}<br>
<span style="color:#f5a623">现价位于 {'所有均线下方（空头排列）' if c['px'] < min(c['ema20'], c['ema50'], c['ema200']) else ('均线上方' if c['px'] > max(c['ema20'], c['ema50'], c['ema200']) else '均线之间')}</span>
</div>
</div></div>""")
    evs = events[-12:][::-1]
    evhtml = "".join(
        f'<div class="ev"><b>{e["title"]}</b><div class="sub">{e["body"].replace(chr(10), " · ")}</div>'
        f'<div class="sub" style="opacity:.6">{datetime.fromtimestamp(e["ts"], TZ):%m-%d %H:%M:%S}</div></div>'
        for e in evs) or '<div class="sub">暂无触发的推送事件</div>'

    h.append(f"""<div class="card"><h1 style="font-size:16px;margin-bottom:12px">最近推送</h1>{evhtml}</div>
<div class="foot">
<b style="color:var(--txt)">判定方法</b>：日线 / 4H / 1H 各用 7 个信号打分（价格vs EMA20、EMA20vsEMA50、价格vs EMA200、MACD柱、RSI50、DI方向、结构 HH/HL），单周期 −7~+7，三周期合计 ≤ −3 判偏空、≥ +3 判偏多。<br>
<b style="color:var(--txt)">点位方法</b>：把成交量分布（30日/90日 POC、VAL、VAH）、EMA20/50/200(4H)、4H 摆动高低点、昨日/上周高低点、30日高低点、斐波那契与整数关全部作为候选，按 0.4% 聚簇；取距现价 0.5~5.0 个 ATR、得分 ≥2 的簇，由近及远最多 3 档。止损放在区间外 0.65 个 ATR（合计约 0.9 个 ATR），止盈取前方最近的成交密集区并夹在 0.9~1.9 个 ATR。<br>
<b style="color:var(--txt)">仓位</b>：顺势 2%（中仓）、逆势 1%（轻仓）；名义杠杆 = 风险% ÷ 止损距离%。<br>
<span style="color:var(--amber)">本页面为程序自动生成，仅作研究参考，不构成投资建议。杠杆交易风险极高，请自行控制仓位。</span>
</div></div></body></html>""")
    return "".join(h)

# ---------------------------------------------------------------- 主流程
def cycle(cfg, dry=False):
    state = load_state()
    coins = []
    for c in cfg["coins"]:
        a = analyze(c)
        if not a:
            log(f"{c} analyze failed")
            continue
        a["zones"] = build_zones(a)
        coins.append(a)
    if not coins:
        log("no data, skip cycle")
        return
    coins.sort(key=lambda x: cfg["coins"].index(x["coin"]))

    events = []
    first = not state.get("last")
    state.setdefault("active", {})
    for a in coins:
        for e in check(a, a["zones"], state, cfg):
            events.append(e)
        state["last"][a["coin"]] = {"px": a["px"], "ts": time.time(),
                                    "zones": a["zones"], "bias": a["bias"]}

    if first:
        state["last_heartbeat"] = time.time()
        parts = []
        for a in coins:
            zs = a["zones"]["sell"]; zb = a["zones"]["buy"]
            parts.append(f"{a['coin']} {a['px']:,.{dec_of(a['px'])}f}（{a['bias']}）")
        events.append(dict(coin="系统", rule="startup", ts=time.time(), important=False,
                           title="监测器已启动",
                           body=" / ".join(parts) + f"\n每 {cfg['interval_sec']} 秒扫描一次，只在价格进入挂单区时推送"))

    # 定时摘要
    if cfg.get("heartbeat_hours", 0) and time.time() - state.get("last_heartbeat", 0) > cfg["heartbeat_hours"] * 3600:
        state["last_heartbeat"] = time.time()
        head = " ".join(f"{a['coin']} {a['chg24']:+.1f}%" for a in coins)
        events.append(dict(coin="行情", rule="heartbeat", ts=time.time(), important=False,
                           title=f"四小时摘要 · 整体{coins[0]['bias']}",
                           body=head + f"\n1H RSI " + " / ".join(f"{a['coin']} {a['tf']['1h']['rsi']:.0f}" for a in coins)))

    for e in events:
        push(cfg, e["title"], e["body"], coin=e.get("coin"),
             important=e.get("important", False), url=cfg.get("dashboard_url") or None, dry=dry)
        state["events"].append({"title": e["title"], "body": e["body"], "ts": e["ts"]})

    dash = {"ts": now().strftime("%Y-%m-%d %H:%M:%S"), "coins": coins, "events": state["events"]}
    html = render(dash, cfg)
    with open(DASH_OUT, "w", encoding="utf-8") as f:
        f.write(html)
    save_state(state)
    log(f"cycle done coins={len(coins)} events={len(events)} px=" +
        " ".join(f"{a['coin']}:{a['px']:.2f}" for a in coins))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    cfg = load_cfg()
    if args.test:
        ok = push(cfg, "监测器测试", "如果你看到这条，Bark 通道已经打通。\n这是一个测试消息，不需要任何操作。",
                  coin="系统", important=True)
        log(f"test push -> {ok}")
        return
    if args.once:
        cycle(cfg)
        return
    log(f"loop start interval={cfg['interval_sec']}s key={'yes' if cfg.get('bark_key') else 'no'}")
    while True:
        t0 = time.time()
        try:
            cycle(cfg)
        except Exception as e:
            log(f"CYCLE-ERR {type(e).__name__} {e}")
        time.sleep(max(10, cfg["interval_sec"] - (time.time() - t0)))

if __name__ == "__main__":
    main()








