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

# ---------------------------------------------------------------- 真实股价（对标永续）
# 股票永续合约与真实股票可能长期存在 3%~5% 溢价，分析时必须叠加真实价
UNDERLYING = {
    "MU":      {"market": "us", "sym": "MU",  "name": "美光"},
    "SNDK":    {"market": "us", "sym": "SNDK", "name": "闪迪"},
    "SKHYNIX": {"market": "kr", "sym": "000660", "name": "SK海力士"},
}
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_FX_CACHE = {}


def _get_text(url, referer=None, timeout=12):
    h = dict(_UA)
    if referer:
        h["Referer"] = referer
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                  timeout=timeout, context=ctx).read().decode("gbk", "replace")


def usdkrw():
    """美元兑韩元，用于把韩股换算成美元"""
    if _FX_CACHE.get("v"):
        return _FX_CACHE["v"]
    v = None
    try:
        d = json.loads(_get_text("https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?interval=1d&range=1d"))
        v = float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        pass
    if not v:
        try:
            t = _get_text("https://hq.sinajs.cn/list=fx_susdkrw", referer="https://finance.sina.com.cn")
            parts = t.split('"')[1].split(",")
            v = float(parts[1])
        except Exception:
            v = 1380.0
    _FX_CACHE["v"] = v
    return v


def quote_underlying(code):
    """返回真实股价（统一折算成 USDT 计价）"""
    info = UNDERLYING.get(code)
    if not info:
        return None
    market, sym, name = info["market"], info["sym"], info["name"]
    price, prev, chg, src, cur = None, None, None, None, "USD"
    ext_px, ext_chg, ext_time = None, None, None      # 盘前/盘后
    bid, ask, bid_sz, ask_sz = None, None, None, None

    # 源 A：新浪（中国市场友好，含盘后价与扩展时段时间）
    try:
        t = _get_text(f"https://hq.sinajs.cn/list=gb_{sym.lower()}", referer="https://finance.sina.com.cn")
        p = t.split('"')[1].split(",")
        if len(p) > 4 and float(p[1]) > 0:
            price = float(p[1]); chg = float(p[2]); src = "sina"
            prev = float(p[1]) / (1 + chg / 100.0) if chg else None
        if len(p) > 24:
            try:
                if float(p[21]) > 0:
                    ext_px = float(p[21])
                    ext_chg = float(p[22]) if p[22] else None
                    ext_time = p[24].strip() if len(p[24]) > 3 else None
            except Exception:
                pass
    except Exception:
        pass

    # 源 B：腾讯（韩股 + 买卖一档）
    try:
        t = _get_text(f"https://qt.gtimg.cn/q={'kr' if market == 'kr' else 'us'}{sym}")
        p = t.split('"')[1].split("~")
        if market == "kr" and len(p) > 32 and float(p[3]) > 0:
            price = float(p[3]); chg = float(p[32]) if p[32] else None
            cur = "KRW"; src = "tencent"
        elif market == "us" and len(p) > 20:
            if price is None and float(p[3]) > 0:
                price = float(p[3]); chg = float(p[32]) if len(p) > 32 and p[32] else None
                src = "tencent"
            if float(p[9] or 0) > 0:
                bid, bid_sz, ask, ask_sz = float(p[9]), float(p[10] or 0), float(p[19] or 0), float(p[20] or 0)
    except Exception:
        pass

    # 源 C：雅虎（海外 IP 友好）
    if price is None:
        try:
            ys = f"{sym}.KS" if market == "kr" else sym
            d = json.loads(_get_text(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ys}?interval=1d&range=1d"))
            m = d["chart"]["result"][0]["meta"]
            price = float(m.get("regularMarketPrice") or 0)
            prev = m.get("chartPreviousClose")
            src = "yahoo"
            if market == "kr":
                cur = "KRW"
        except Exception:
            pass

    if not price:
        return None
    fx = usdkrw() if cur == "KRW" else 1.0
    # 用「最近一笔成交」算：盘前/盘后价优先于收盘价
    latest = ext_px if ext_px else price
    return {"name": name, "market": market, "raw": price, "currency": cur,
            "usd": price / fx, "chg": chg, "src": src, "fx": fx,
            "ext": ext_px, "ext_chg": ext_chg, "ext_time": ext_time,
            "ext_usd": (ext_px / fx) if ext_px else None,
            "latest_usd": latest / fx,
            "bid": bid, "ask": ask, "bid_sz": bid_sz, "ask_sz": ask_sz}

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
    tick = None
    if coin not in _NO_SPOT:          # 股票类合约没有现货对，失败一次就不再重试
        tick = http_json(f"{G}/spot/tickers?currency_pair={coin}_USDT", tries=1, timeout=6)
        if not tick:
            _NO_SPOT.add(coin)
        elif isinstance(tick, list):
            tick = tick[0] if tick else None
    tick = tick or {}

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


_NO_SPOT = set()   # 无现货对的合约（股票类）

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
            tp3 = tp2 - atr4 * 1.3
        else:
            stop = lo - atr4 * 0.65
            tp1 = c["p"] + d_tp
            tp2 = tp1 + atr4 * 1.3
            tp3 = tp2 + atr4 * 1.3
        return lo, hi, stop, tp1, tp2, tp3

    zones = {"sell": [], "buy": []}
    # 震荡市不预设方向：两侧都标“区间”，仓位都按轻仓
    if a["bias"] == "偏空":
        sell_tag, buy_tag = "顺势", "逆势"
    elif a["bias"] == "偏多":
        sell_tag, buy_tag = "逆势", "顺势"
    else:
        sell_tag = buy_tag = "区间"
    for c in choose(upc, "up"):
        lo, hi, stop, tp1, tp2, tp3 = size_zone(c, "up")
        rr = (c["p"] - tp1) / (stop - c["p"]) if stop > c["p"] else 0
        zones["sell"].append({"tag": sell_tag, "lo": lo, "hi": hi,
                              "entry": c["p"], "stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                              "rr": rr, "score": c["score"], "reasons": list(dict.fromkeys(c["tags"]))})
    for c in choose(dnc, "dn"):
        lo, hi, stop, tp1, tp2, tp3 = size_zone(c, "dn")
        rr = (tp1 - c["p"]) / (c["p"] - stop) if c["p"] > stop else 0
        zones["buy"].append({"tag": buy_tag, "lo": lo, "hi": hi,
                             "entry": c["p"], "stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                             "rr": rr, "score": c["score"], "reasons": list(dict.fromkeys(c["tags"]))})
    if os.environ.get("MM_DEBUG"):
        log(f"DEBUG {a['coin']} up={[(round(c['p'],1), round(c['score'],1), round(c['dist_atr'],2), c['tags']) for c in upc]}")
        log(f"DEBUG {a['coin']} dn={[(round(c['p'],1), round(c['score'],1), round(c['dist_atr'],2), c['tags']) for c in dnc]}")
    return zones

# ---------------------------------------------------------------- 推送
def bark_keys(cfg):
    """收集所有收件人 key（去重保序）"""
    keys = []
    if cfg.get("bark_key"):
        keys.append(str(cfg["bark_key"]).strip())
    for k in (cfg.get("bark_keys") or []):
        if k and str(k).strip():
            keys.append(str(k).strip())
    for env in ("BARK_KEY", "BARK_KEY_2", "BARK_KEY_3", "BARK_KEYS"):
        v = os.environ.get(env, "") or ""
        for part in v.replace(";", ",").split(","):
            if part.strip():
                keys.append(part.strip())
    seen, uniq = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k); uniq.append(k)
    return uniq


def push(cfg, title, body, coin=None, important=False, url=None, dry=False):
    prefix = cfg.get("title_prefix") or ""
    if prefix and not title.startswith(prefix) and not title.startswith("DEEP"):
        title = prefix + title
    keys = bark_keys(cfg)
    if dry or not keys:
        rec = {"ts": now().isoformat(), "title": title, "body": body, "sent": False,
               "reason": "dry-run" if dry else "no-key"}
        with open(os.path.join(DATA, "push.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log(f"PUSH-SKIP {title}")
        return False
    base = {"title": title, "body": body, "group": coin or "行情",
            "sound": cfg.get("sound", "bell"), "icon": "https://api.day.app/static/favicon.ico"}
    if important:
        base["level"] = cfg.get("level_important", "timeSensitive")
    if url:
        base["url"] = url
    sent = 0
    for k in keys:
        payload = dict(base, device_key=k)
        try:
            r = http_post(cfg["bark_server"].rstrip("/") + "/push", payload)
            ok = r.get("code") == 200
            sent += 1 if ok else 0
            with open(os.path.join(DATA, "push.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now().isoformat(), "title": title, "key": k[-4:],
                                    "sent": ok, "resp": r}, ensure_ascii=False) + "\n")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:120]
            except Exception:
                detail = ""
            log(f"PUSH-HTTP {e.code} {detail} {title} ({k[-4:]})")
        except Exception as e:
            log(f"PUSH-ERR {type(e).__name__} {str(e)[:50]} ({k[-4:]})")
    log(f"PUSH {sent}/{len(keys)} OK {title}")
    return sent > 0

# ---------------------------------------------------------------- 触发判断
def load_positions(cfg):
    """持仓来源：环境变量 POSITIONS_JSON（Secret，优先）或 config.json"""
    raw = (os.environ.get("POSITIONS_JSON") or "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            log(f"POSITIONS_JSON 解析失败: {e}")
    return cfg.get("positions") or []


def pos_rows(positions, px_map):
    """算出每条持仓的实时状态"""
    rows = []
    for p in positions:
        c = p.get("coin"); side = p.get("side"); px = px_map.get(c)
        if not px or side not in ("sell", "buy"):
            continue
        d = dec_of(px)
        entry = float(p.get("entry") or 0); qty = float(p.get("qty") or 0)
        pnl = (entry - px) * qty if side == "sell" else (px - entry) * qty
        pct = (entry - px) / entry * 100 if side == "sell" else (px - entry) / entry * 100
        tps = p.get("tps") or []
        nxt = next((t for t in tps if (side == "sell" and px > t) or (side == "buy" and px < t)), None)
        rows.append({"coin": c, "side": side, "entry": entry, "qty": qty, "px": px,
                     "pnl": pnl, "pct": pct, "stop": p.get("stop"), "tps": tps, "next_tp": nxt,
                     "dec": d, "label": "空" if side == "sell" else "多"})
    return rows


def check_positions(positions, px_map, state, ts):
    """价格触达止盈/止损时生成提醒"""
    ev = []
    ref = state.setdefault("pos_state", {})
    for p in positions:
        c = p.get("coin"); side = p.get("side"); px = px_map.get(c)
        if not px or side not in ("sell", "buy"):
            continue
        d = dec_of(px); entry = float(p.get("entry") or 0); qty = float(p.get("qty") or 0)
        tps = p.get("tps") or []
        pcts = p.get("tp_pcts") or ([100.0 / len(tps)] * len(tps) if tps else [])
        key = f"{c}:{side}:{entry}"
        st = ref.setdefault(key, {"tps_hit": [False] * len(tps), "stop_hit": False})
        if len(st.get("tps_hit", [])) != len(tps):
            st["tps_hit"] = [False] * len(tps)

        hit = []
        for i, tp in enumerate(tps):
            if st["tps_hit"][i]:
                continue
            if (side == "sell" and px <= tp) or (side == "buy" and px >= tp):
                st["tps_hit"][i] = True
                hit.append(i)
        if hit:
            closed = sum(qty * (pcts[i] if i < len(pcts) else 0) / 100.0
                         for i in range(len(tps)) if st["tps_hit"][i])
            remain = qty - closed
            lines = []
            for i in hit:
                q = round(qty * (pcts[i] if i < len(pcts) else 0) / 100.0, 3)
                lines.append(f"TP{i+1} {tps[i]:,.{d}f} → 平 {q} 个")
            pnl = (entry - px) * qty if side == "sell" else (px - entry) * qty
            nxt = next((tps[i] for i in range(len(tps)) if not st["tps_hit"][i]), None)
            stop = p.get("stop")
            stop_txt = f"｜止损 {stop:,.{d}f}" if stop else ""
            nxt_txt = f"下一目标 {nxt:,.0f}" if nxt else "已到最后一档"
            ev.append(dict(coin=c, rule="tp", ts=ts, important=True,
                           title=f"DEEP-持仓（{c}）到达 {'/'.join('TP' + str(i + 1) for i in hit)}",
                           body=(f"{'空' if side == 'sell' else '多'}单 {qty:g} 个 @{entry:,.{d}f}｜现价 {px:,.{d}f}\n"
                                 + "\n".join(lines) +
                                 f"\n剩余 {max(remain, 0):g} 个｜浮盈 {pnl:+,.0f} USDT\n"
                                 f"{nxt_txt}{stop_txt}")))

        stop = p.get("stop")
        if stop and not st.get("stop_hit"):
            if (side == "sell" and px >= stop) or (side == "buy" and px <= stop):
                st["stop_hit"] = True
                pnl = (entry - px) * qty if side == "sell" else (px - entry) * qty
                ev.append(dict(coin=c, rule="posstop", ts=ts, important=True,
                               title=f"DEEP-持仓（{c}）触发止损 {stop:,.{d}f}",
                               body=f"{'空' if side == 'sell' else '多'}单 {qty:g} 个 @{entry:,.{d}f}\n"
                                    f"现价 {px:,.{d}f}｜本单盈亏 {pnl:+,.0f} USDT\n"
                                    f"按计划离场，不要扛单"))
    return ev


def fire(state, key, minutes, ts):
    last = state["cooldowns"].get(key, 0)
    if ts - last < minutes * 60:
        return False
    state["cooldowns"][key] = ts
    return True

def oi_meaning(px_chg, oi_chg):
    """把「价格 + 持仓量」翻译成直白结论：谁在进场、还能不能追"""
    if oi_chg >= 0 and px_chg >= 0:
        return "价涨+增仓", "新多头进场，上涨是新增资金推的", "回调可跟多，别追高"
    if oi_chg >= 0 and px_chg < 0:
        return "价跌+增仓", "新空头进场，下跌有新增资金推动", "反弹到阻力做空，别抄底"
    if oi_chg < 0 and px_chg >= 0:
        return "价涨+减仓", "空头被迫平仓（挤空），没有新多接力", "涨势容易衰竭，别追多"
    return "价跌+减仓", "多头止损离场（去杠杆），不是新空进攻", "跌势接近尾声，别追空"


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

    # 2) 未持仓：判断是否进入区间（含“两轮之间穿越”的情况，避免快速插针漏报）
    path_lo, path_hi = min(prev_px, px), max(prev_px, px)
    if not state["active"].get(c):
        for side, label in (("sell", "卖区"), ("buy", "买区")):
            for i, zz in enumerate(z[side]):
                inside = zz["lo"] <= px <= zz["hi"]
                near = abs(px / zz["entry"] - 1) < 0.0012
                crossed = path_lo <= zz["hi"] and path_hi >= zz["lo"]
                if (inside or near or crossed) and fire(state, f"{c}:zone{side}{i}", cfg["zone_cooldown_min"], ts):
                    dirn = "开空" if side == "sell" else "开多"
                    size = "中仓2%" if zz["tag"] == "顺势" else ("轻仓1%" if zz["tag"] == "逆势" else "区间1%")
                    # 另一侧同样给出来，标注顺势/逆势
                    opp = z["buy"] if side == "sell" else z["sell"]
                    extra = ""
                    if opp:
                        o = opp[0]
                        od = "做多" if side == "sell" else "做空"
                        osz = "中仓2%" if o["tag"] == "顺势" else ("轻仓1%" if o["tag"] == "逆势" else "区间1%")
                        arrow = "跌到" if side == "sell" else "涨到"
                        extra = (f"\n———\n反向：{arrow} {o['entry']:,.{d}f} 可{od}"
                                 f"（{o['tag']}·{osz}）\n"
                                 f"止损 {o['stop']:,.{d}f}｜目标 {o['tp1']:,.{d}f}/{o['tp2']:,.{d}f}｜RR {o['rr']:.2f}")
                    ev.append(dict(coin=c, rule="zone", ts=ts, important=True,
                                   title=f"{c} 进入{label} {zz['entry']:,.{d}f}",
                                   body=(f"{dirn}·【{zz['tag']}】｜区间 {zz['lo']:,.{d}f}-{zz['hi']:,.{d}f}\n"
                                         f"★挂在 {zz['lo'] if side == 'sell' else zz['hi']:,.{d}f}"
                                         f"（靠现价一侧，更容易成交）\n"
                                         f"止损 {zz['stop']:,.{d}f}（破了无条件走）\n"
                                         f"三档止盈：{zz['tp1']:,.{d}f} / {zz['tp2']:,.{d}f} / {zz['tp3']:,.{d}f}\n"
                                         f"→ 到1档平50%并把止损移到成本，到2档再平25%，剩25%拿3档\n"
                                         f"{size}｜杠杆≤10x｜RR {zz['rr']:.2f}｜{'、'.join(zz['reasons'][:2])}" + extra)))
                    state["active"][c] = {"side": side, "idx": i, "entry": zz["entry"], "stop": zz["stop"],
                                          "tp1": zz["tp1"], "tp2": zz["tp2"], "tp3": zz.get("tp3"),
                                          "tag": zz["tag"], "ts": ts, "tp1_hit": False}
                    break
            if state["active"].get(c):
                break

    # 2.5) 提前预警：距区间还剩 0.4%~0.9% 时先提醒一次，便于提前挂单
    if not state["active"].get(c):
        fired_pre = False
        for side, label in (("sell", "卖区"), ("buy", "买区")):
            if fired_pre:
                break
            for i, zz in enumerate(z[side]):
                edge = zz["lo"] if side == "sell" else zz["hi"]
                gap = (edge / px - 1) if side == "sell" else (px / edge - 1)
                if 0.004 <= gap <= 0.009 and fire(state, f"{c}:pre{side}{i}", 120, ts):
                    dirn = "开空" if side == "sell" else "开多"
                    ev.append(dict(coin=c, rule="prezone", ts=ts, important=False,
                                   title=f"{c} 距{label}还有 {gap*100:.1f}%｜可提前挂单",
                                   body=(f"{dirn}·【{zz['tag']}】现价 {px:,.{d}f}\n"
                                         f"区间 {zz['lo']:,.{d}f}-{zz['hi']:,.{d}f}｜建议挂 {edge:,.{d}f}\n"
                                         f"止损 {zz['stop']:,.{d}f}｜目标 {zz['tp1']:,.{d}f}/{zz['tp2']:,.{d}f}\n"
                                         f"（进入区间时会再提醒一次）")))
                    fired_pre = True
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

    # 3.5) 关键位触碰：价格进入关键位 ±0.15% 的带内（需从带外进入，避免反复刷）
    v90 = a["vp90"] or {}
    watch = [("30日POC", v30.get("poc")), ("30日VAL", v30.get("val")), ("30日VAH", v30.get("vah")),
             ("EMA200(4H)", a.get("ema200")), ("90日VAL", v90.get("val"))]
    for name, lvl in watch:
        if not lvl:
            continue
        tol = float(cfg.get("touch_tol_pct", 0.15)) / 100.0
        now_d = abs(px / lvl - 1)
        prev_d = abs(prev_px / lvl - 1)
        band_lo, band_hi = lvl * (1 - tol), lvl * (1 + tol)
        crossed = path_lo <= band_hi and path_hi >= band_lo   # 两轮之间穿过该带
        if (now_d <= tol < prev_d or crossed) and fire(state, f"{c}:touch:{name}", int(cfg.get("touch_cooldown_min", 240)), ts):
            up = px < lvl
            ev.append(dict(coin=c, rule="touch", ts=ts, important=True,
                           title=f"{c} 触及{name} {lvl:,.{d}f}",
                           body=(f"现价 {px:,.{d}f}（{(px/lvl-1)*100:+.2f}%）｜{name}是"
                                 f"{'上方阻力，看能否站上' if up else '下方支撑，看能否守住'}\n"
                                 f"结构 {a['tf']['4h']['struct']}｜1H RSI {a['tf']['1h']['rsi']:.0f}｜"
                                 f"评分 {a['total']:+d}")))

    if abs(a["funding"]) >= 1.0 and not a.get("spot_only") and fire(state, f"{c}:funding", 240, ts):
        ev.append(dict(coin=c, rule="funding", ts=ts, important=False,
                       title=f"{c} 资金费率 {a['funding']:+.2f}bp",
                       body=f"年化 {a['funding_ann']:+.0f}%，{'多头拥挤' if a['funding']>0 else '空头拥挤'}（拥挤度≠方向）｜现价 {px:,.{d}f}"))
    if abs(a["oi_chg"]) >= 2.5 and not a.get("spot_only") and fire(state, f"{c}:oi", 180, ts):
        kind, meaning, advice = oi_meaning(a["chg24"], a["oi_chg"])
        ev.append(dict(coin=c, rule="oi", ts=ts, important=False,
                       title=f"{c} 持仓量 {a['oi_chg']:+.1f}%｜{kind}",
                       body=(f"{meaning}\n"
                             f"→ {advice}\n"
                             f"现价 {px:,.{d}f}（24h {a['chg24']:+.2f}%）｜OI ${a['oi_usd']/1000:.2f}B｜"
                             f"强平 多{a['liq_l']:.0f}M/空{a['liq_s']:.0f}M")))
    r1 = a["tf"]["1h"]["rsi"]
    if (r1 < 20 or r1 > 80) and fire(state, f"{c}:rsi", 120, ts):
        ev.append(dict(coin=c, rule="rsi", ts=ts, important=False,
                       title=f"{c} 1H RSI {r1:.0f}",
                       body=f"{'超卖，反弹概率上升' if r1<20 else '超买，回落风险'}｜现价 {px:,.{d}f}"))

    # 3.8) 趋势强度变化（方向转换 / 结构翻转 / ADX 与评分位移）
    cur = {"total": a["total"], "adx": a["tf"]["4h"]["adx"],
           "struct": a["tf"]["4h"]["struct"], "bias": a["bias"]}
    refs = state.setdefault("trend_ref", {})
    ref = refs.get(c)
    if ref:
        d_adx = cur["adx"] - ref["adx"]
        d_score = cur["total"] - ref["total"]
        flipped = cur["struct"] != ref["struct"]
        bias_changed = ref.get("bias") and ref["bias"] != cur["bias"]
        if bias_changed and fire(state, f"{c}:bias", 240, ts):
            ev.append(dict(coin=c, rule="trend", ts=ts, important=True,
                           title=f"{c} 方向由「{ref['bias']}」转为「{cur['bias']}」",
                           body=(f"综合评分 {ref['total']:+d} → {cur['total']:+d}｜ADX {ref['adx']:.0f} → {cur['adx']:.0f}\n"
                                 f"结构 {cur['struct']}｜1H RSI {r1:.0f}\n"
                                 f"→ {'原方向失效，先离场，等新结构确认' if '震荡' not in cur['bias'] else '趋势结束进入震荡，空/多单都应收紧'}")))
            refs[c] = dict(ts=ts, **cur)
        elif flipped and fire(state, f"{c}:struct", 360, ts):
            stronger = "HH/HL" in str(cur["struct"])
            ev.append(dict(coin=c, rule="trend", ts=ts, important=True,
                           title=f"{c} 结构转为{cur['struct']}（{'转强' if stronger else '转弱'}）",
                           body=(f"4H 结构 {ref['struct']} → {cur['struct']}\n"
                                 f"含义：{'回落不再创新低、反弹开始创新高，下跌趋势被破坏' if stronger else '反弹不再创新高、回落开始创新低，下跌趋势确认'}\n"
                                 f"{'多单可持有，空单减仓' if stronger else '空单可持有，多单离场'}")))
            refs[c] = dict(ts=ts, **cur)
        elif abs(d_adx) >= 4 and abs(d_score) >= 3 and fire(state, f"{c}:trend", 240, ts):
            same_dir = (cur["total"] < 0) == (ref["total"] < 0)
            up = cur["total"] < 0
            if same_dir and abs(cur["total"]) > abs(ref["total"]):
                word = "下跌趋势增强" if up else "上涨趋势增强"
                hint = "动能加速，顺势单可持有，别逆势抢反弹" if up else "动能加速，回调可跟多"
            elif same_dir:
                word = "下跌趋势减弱" if up else "上涨趋势减弱"
                hint = "动能衰减，空单收紧止损/减仓" if up else "动能衰减，多单减仓"
            else:
                word = "方向出现转换"
                hint = "原方向失效，先离场等新结构"
            ev.append(dict(coin=c, rule="trend", ts=ts, important=True,
                           title=f"{c} {word}",
                           body=(f"ADX(4H) {ref['adx']:.0f} → {cur['adx']:.0f}｜综合评分 {ref['total']:+d} → {cur['total']:+d}\n"
                                 f"结构 {cur['struct']}｜1H RSI {r1:.0f}\n"
                                 f"→ {hint}")))
            refs[c] = dict(ts=ts, **cur)
        elif "bias" not in ref or ts - ref.get("ts", 0) > 10800:
            refs[c] = dict(ts=ts, **cur)
    else:
        refs[c] = dict(ts=ts, **cur)
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
.t-n{color:var(--sub);border-color:var(--line);background:#0e1621}
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

    pos = dash.get("positions") or []
    if pos:
        prow = []
        for p in pos:
            d = p["dec"]
            col = "#1fbf9a" if p["pnl"] >= 0 else "#ef5a55"
            stop_txt = f"{p['stop']:,.{d}f}" if p["stop"] else "—"
            nxt_txt = f"{p['next_tp']:,.{d}f}" if p["next_tp"] else "已到末档"
            prow.append(
                f"<tr><td><b>{p['coin']}</b></td><td>{p['label']}</td>"
                f"<td class='num'>{p['entry']:,.{d}f}</td><td class='num'>{p['px']:,.{d}f}</td>"
                f"<td class='num'>{p['qty']:g}</td>"
                f"<td class='num' style='color:{col};font-weight:600'>{p['pnl']:+,.0f}</td>"
                f"<td class='num' style='color:{col}'>{p['pct']:+.2f}%</td>"
                f"<td class='num'>{stop_txt}</td><td class='num'>{nxt_txt}</td></tr>")
        h.append(f"""<div class="card"><div class="ch"><div class="px" style="font-size:19px">我的持仓</div>
<span class="badge neu">{len(pos)} 笔</span></div>
<table><thead><tr><th>币</th><th>方向</th><th>成本</th><th>现价</th><th>数量</th>
<th>浮盈(USDT)</th><th>幅度</th><th>止损</th><th>下一目标</th></tr></thead>
<tbody>{''.join(prow)}</tbody></table></div>""")

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
                tcls = {"顺势": "t-s", "逆势": "t-r"}.get(z["tag"], "t-n")
                rows.append(f"""<tr>
<td class="{dcls}">{dname}</td><td><span class="tag {tcls}">{z['tag']}</span></td>
<td class="num">{fmt(z['lo'],d)} - {fmt(z['hi'],d)}</td>
<td class="num">{fmt(z['entry'],d)}</td><td class="num">{fmt(z['stop'],d)}</td>
<td class="num">{fmt(z['tp1'],d)}</td><td class="num">{fmt(z['tp2'],d)}</td>
<td class="num">{z['rr']:.2f}</td>
<td>{'中仓 2%' if z['tag']=='顺势' else ('轻仓 1%' if z['tag']=='逆势' else '区间 1%')}</td>
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
{"" if not c.get('under') else f'''<div style="margin:6px 0 2px;font-size:13px;color:#cfd8e3">
真实{c["under"]["name"]} 收盘 <b>{c["under"]["raw"]:,.2f} {c["under"]["currency"]}</b>（{c["under"]["chg"]:+.2f}%）
{"　｜　盘后 <b>" + format(c["under"]["ext"], ",.2f") + "</b>（" + format(c["under"]["ext_chg"] or 0, "+.2f") + "%）" + (c["under"].get("ext_time") or "") if c["under"].get("ext") else ""}
{f'　｜　买一 <b>{c["under"]["bid"]:,.2f}</b> / 卖一 <b>{c["under"]["ask"]:,.2f}</b>' if c["under"].get("bid") else ""}
<br>≈ ${c["under"]["latest_usd"]:,.2f}　｜　基差 <b style="color:{"#ef5a55" if c["premium"]>0 else "#1fbf9a"}">{c["premium"]:+.2f}%</b>
{"（永续比真股贵）" if c["premium"]>0 else "（永续比真股便宜）"}
</div>'''}
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
<b style="color:var(--txt)">关键位触碰</b>：价格从带外进入 30日 POC / VAL / VAH、EMA200(4H)、90日 VAL 的 ±0.15% 内时单独推送一次，冷却 4 小时。<br>
<b style="color:var(--txt)">趋势强度</b>：4H 结构在 HH/HL 与 LH/LL 之间翻转（6 小时冷却），或 ADX(4H) 变动 ≥4 且综合评分变动 ≥3 时推送，直接告诉你趋势在增强还是减弱。<br>
<b style="color:var(--txt)">持仓量(OI)</b>：推送会把「价格涨跌 + OI 增减」翻译成结论——谁在进场、该不该追，不用自己换算。<br>
<b style="color:var(--txt)">仓位</b>：顺势 2%（中仓）、逆势 1%（轻仓）；名义杠杆 = 风险% ÷ 止损距离%。<br>
<span style="color:var(--amber)">本页面为程序自动生成，仅作研究参考，不构成投资建议。杠杆交易风险极高，请自行控制仓位。</span>
</div></div></body></html>""")
    return "".join(h)

# ---------------------------------------------------------------- 主流程
def cycle(cfg, dry=False):
    state = load_state()
    from concurrent.futures import ThreadPoolExecutor

    def _one(c):
        try:
            a = analyze(c)
            if not a:
                log(f"{c} analyze failed")
                return None
            a["zones"] = build_zones(a)
            return a
        except Exception as e:
            log(f"{c} analyze error {type(e).__name__} {e}")
            return None

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(cfg["coins"])))) as ex:
        results = list(ex.map(_one, cfg["coins"]))
    coins = [a for a in results if a]
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

    # 我的持仓：到达止盈/止损时提醒
    # 股票永续：叠加真实股价，算基差（溢价/折价）
    for a in coins:
        if a["coin"] not in UNDERLYING:
            continue
        try:
            q = quote_underlying(a["coin"])
        except Exception:
            q = None
        if not q:
            continue
        a["under"] = q
        # 基差用「最近一笔真实成交」：盘前/盘后价优先
        a["premium"] = (a["px"] / q["latest_usd"] - 1) * 100.0
        thr = float(cfg.get("premium_alert_pct", 2.0))
        if abs(a["premium"]) >= thr and fire(state, f"{a['coin']}:premium",
                                            int(cfg.get("premium_cooldown_min", 240)), time.time()):
            d2 = dec_of(a["px"])
            dirn = "溢价" if a["premium"] > 0 else "折价"
            tip = ("永续比真股贵，做多要付溢价，回落风险大；空头有基差回补空间"
                   if a["premium"] > 0 else
                   "永续比真股便宜，做空要付折价，反弹风险大；多头有基差收敛空间")
            ext_txt = ""
            if q.get("ext"):
                ext_txt = (f"\n{q['ext']:,.2f} {q['currency']}"
                           f"（{q['ext_chg']:+.2f}%）盘后 {q.get('ext_time') or ''}")
            events.append(dict(coin=a["coin"], rule="premium", ts=time.time(), important=True,
                               title=f"{a['coin']} 永续{dirn} {abs(a['premium']):.1f}%",
                               body=(f"真股收盘 {q['raw']:,.2f} {q['currency']}"
                                     f"（{q['chg']:+.2f}%）≈ ${q['usd']:,.2f}" + ext_txt +
                                     f"\n永续 {a['px']:,.{d2}f}｜基差 {a['premium']:+.2f}%\n{tip}")))

    px_map = {a["coin"]: a["px"] for a in coins}
    positions = load_positions(cfg)
    if positions:
        for e in check_positions(positions, px_map, state, time.time()):
            events.append(e)

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
        rows = []
        for a in coins:
            dd = dec_of(a["px"]); zz = a["zones"]
            seg = f"{a['coin']} {a['px']:,.{dd}f} {a['bias']}{a['total']:+d}"
            if zz["sell"]:
                s = zz["sell"][0]
                seg += f"\n  空 {s['entry']:,.{dd}f}【{s['tag']}】"
            if zz["buy"]:
                b = zz["buy"][0]
                seg += f"　多 {b['entry']:,.{dd}f}【{b['tag']}】"
            rows.append(seg)
        events.append(dict(coin="行情", rule="heartbeat", ts=time.time(), important=False,
                           title=f"四小时双向点位 · 整体{coins[0]['bias']}",
                           body="\n".join(rows)))

    for e in events:
        push(cfg, e["title"], e["body"], coin=e.get("coin"),
             important=e.get("important", False), url=cfg.get("dashboard_url") or None, dry=dry)
        state["events"].append({"title": e["title"], "body": e["body"], "ts": e["ts"]})

    dash = {"ts": now().strftime("%Y-%m-%d %H:%M:%S"), "coins": coins, "events": state["events"],
            "positions": pos_rows(positions, px_map) if positions else []}
    html = render(dash, cfg)
    with open(DASH_OUT, "w", encoding="utf-8") as f:
        f.write(html)

    # 发布节流：至少间隔 PUBLISH_GAP 秒才允许 workflow 提交看板
    PUBLISH_GAP = int(os.environ.get("PUBLISH_GAP", "600"))
    flag = os.path.join(DATA, "publish.ok")
    if time.time() - state.get("last_publish", 0) >= PUBLISH_GAP:
        state["last_publish"] = time.time()
        with open(flag, "w", encoding="utf-8") as f:
            f.write(str(int(state["last_publish"])))
    else:
        try:
            os.remove(flag)
        except OSError:
            pass
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








