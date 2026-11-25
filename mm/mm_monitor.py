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
import io, re, json, os, sys, time, math, ssl, hmac, hashlib, base64, argparse, urllib.request, urllib.error, statistics as st
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "monitor_data")

# 模拟仓版本标记：改动点位/仓位逻辑时递增，便于新旧分开统计盈亏
PAPER_VER = "v66-1122"

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
# ---------------------------------------------------------------- 价位/区间
def regime_of(a):
    """行情状态：chop(震荡) / up(上涨趋势) / down(下跌趋势) / mid(过渡)
    依据 4H 的 ADX 与结构，避免"在单边里逆势挂单"
    """
    try:
        t = a["tf"]["4h"]
        adx4 = t.get("adx") or 0
        st4 = t.get("struct") or ""
    except Exception:
        return "mid"
    px = a.get("px") or 0
    e200 = a.get("ema200") or px
    up_struct = st4 in ("HH/HL", "HL")
    dn_struct = st4 in ("LH/LL", "LH")
    if adx4 < 20:
        return "chop"
    if up_struct and px >= e200:
        return "up"
    if dn_struct and px <= e200:
        return "down"
    return "mid"


def vol_state(a):
    """高波动识别：近 6 根 1H 的真实波幅 / 近 48 根均值。
    返回 (是否高波动, 倍数)。高波动时：仓位缩小、止损放宽，避免被 1-2% 的来回扫掉。"""
    try:
        h1 = a.get("h1") or []
        if len(h1) < 50:
            return False, 1.0
        def tr(x, y):
            return max(x["h"] - x["l"], abs(x["h"] - y["c"]), abs(x["l"] - y["c"]))
        recent = sum(tr(h1[i], h1[i-1]) for i in range(-6, 0)) / 6.0
        base = sum(tr(h1[i], h1[i-1]) for i in range(-48, 0)) / 48.0
        if base <= 0:
            return False, 1.0
        r = recent / base
        return (r >= 1.5), round(r, 2)
    except Exception:
        return False, 1.0


def cycle_phase(a):
    """阶段识别：深跌后动能衰竭=筑底；大涨后动能衰竭=筑顶（此时逆势单要减仓）"""
    px = a.get("px") or 0
    hi30 = a.get("hi30d") or px
    lo30 = a.get("lo30d") or px
    try:
        adx4 = a["tf"]["4h"].get("adx") or 0
    except Exception:
        adx4 = 0
    drop = (px / hi30 - 1) * 100 if hi30 else 0
    rise = (px / lo30 - 1) * 100 if lo30 else 0
    if adx4 < 28 and drop <= -25:
        return "bottom"
    if adx4 < 28 and rise >= 25:
        return "top"
    return ""


def dec_of(px):
    return 0 if px > 20000 else (1 if px > 1000 else 2)

def build_zones(a):
    px = a["px"]; atr4 = a["atr4h"] or px * 0.01
    d1 = a.get("d1") or []
    # 成交量参照：用日线均量，给"高成交价位"加权
    vols = [x.get("v") or 0 for x in d1[-60:]] or [1.0]
    vavg = sum(vols) / len(vols) if sum(vols) > 0 else 1.0

    def level_vol(lvl, band=0.012):
        """该价位附近（±1.2%）的成交量 / 均量"""
        v = 0.0
        for x in d1[-120:]:
            if x["l"] * (1 - band) <= lvl <= x["h"] * (1 + band):
                v += (x.get("v") or 0)
        return v / (vavg * 20) if vavg else 1.0

    up, dn = [], []
    seen = {}          # (side, category) -> [已加入的价格]

    def add(lst, p0, w, tag, cat=None, side="up"):
        if not p0 or p0 <= 0:
            return
        # 同类去重：同一类别里 0.6% 内只算一次（避免重复计权）
        if cat:
            key = (side, cat)
            for q in seen.get(key, []):
                if abs(p0 - q) / q < 0.006:
                    return
            seen.setdefault(key, []).append(p0)
        # 成交量加权：高成交价位最多 +40%，低成交 -20%
        lv = level_vol(p0)
        w *= max(0.8, min(1.4, 0.85 + lv * 0.35))
        lst.append((p0, w, tag))

    v30, v90 = a["vp30"] or {}, a["vp90"] or {}
    add(up, v30.get("vah"), 3.0, "30日价值区上沿", "vp", "up")
    add(up, v30.get("poc"), 3.2, "30日POC", "vp", "up")
    add(up, a["ema20"], 2.0, "EMA20(4H)", "ema", "up")
    add(up, a["ema50"], 2.2, "EMA50(4H)", "ema", "up")
    add(up, a["ema200"], 2.4, "EMA200(4H)", "ema", "up")
    for s0 in a["sw_hi"][-3:]:
        add(up, s0, 2.5, "4H前高", f"sw{s0:.0f}", "up")
    # 高点类合并：昨日/上周/24h/7d/30d 同类只算一次
    add(up, a["pdh"], 1.8, "昨日高", "hi_day", "up")
    add(up, a["pwh"], 1.8, "上周高", "hi_day", "up")
    add(up, a["hi24"], 1.4, "24h高", "hi_day", "up")
    add(up, a["hi7d"], 1.6 * 0.85, "7日高", "hi_day", "up")      # 时间衰减
    add(up, a["hi30d"], 2.0 * 0.7, "30日高", "hi_day", "up")     # 时间衰减

    add(dn, v30.get("val"), 3.0, "30日价值区下沿", "vp", "dn")
    add(dn, v30.get("poc"), 3.2, "30日POC", "vp", "dn")
    add(dn, v90.get("val"), 2.6, "90日价值区下沿", "vp", "dn")
    for s0 in a["sw_lo"][-3:]:
        add(dn, s0, 2.5, "4H前低", f"sw{s0:.0f}", "dn")
    add(dn, a["pdl"], 1.8, "昨日低", "lo_day", "dn")
    add(dn, a["pwl"], 1.8, "上周低", "lo_day", "dn")
    add(dn, a["lo24"], 1.5, "24h低", "lo_day", "dn")
    add(dn, a["lo7d"], 1.7 * 0.85, "7日低", "lo_day", "dn")
    add(dn, a["lo30d"], 2.0 * 0.7, "30日低", "lo_day", "dn")

    # 斐波：只在附近没有 EMA/前高前低时才计入（避免重复）
    for r in (0.382, 0.5, 0.618):
        lvl = a["lo30d"] + (a["hi30d"] - a["lo30d"]) * r
        near_other = False
        for q in seen.get(("up", "ema"), []) + seen.get(("dn", "ema"), []):
            if abs(lvl - q) / q < 0.008:
                near_other = True
        for k2 in list(seen.keys()):
            if k2[1].startswith("sw"):
                for q in seen[k2]:
                    if abs(lvl - q) / q < 0.008:
                        near_other = True
        if not near_other:
            add(up if lvl > px else dn, lvl, 1.0, f"fib{int(r*1000)}", "fib", "up" if lvl > px else "dn")

    # 整数关：权重下调到 0.5（加密里有效性弱）
    step = 10 ** math.floor(math.log10(px)) / 10
    for k2 in (0, 1, 2):
        add(up, (math.floor(px / step) + 1 + k2) * step, 0.5 - k2 * 0.1, "整数关", "round", "up")
        add(dn, (math.floor(px / step) - k2) * step, 0.5 - k2 * 0.1, "整数关", "round", "dn")

    # 流动性池：两个几乎相同的摆动高低点（差 <0.25%）
    def add_pool(lst, swings, side, tag):
        sw = sorted(swings)
        for i in range(1, len(sw)):
            if sw[i] > 0 and abs(sw[i] - sw[i-1]) / sw[i] < 0.0025:
                add(lst, (sw[i] + sw[i-1]) / 2, 2.2, tag, f"pool{side}{i}", side)
    add_pool(up, a.get("sw_hi") or [], "up", "等高点(流动性池)")
    add_pool(dn, a.get("sw_lo") or [], "dn", "等低点(流动性池)")

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

    # 距离放宽到 3.5 ATR，但选簇时优先"强簇"（权重高），再靠"提前入场"把成交价拉近
    up_all = cluster(up, "up")
    dn_all = cluster(dn, "dn")
    upc = [c for c in up_all if 0.35 <= c["dist_atr"] <= 3.0]
    dnc = [c for c in dn_all if -3.0 <= c["dist_atr"] <= -0.35]
    if not upc and up_all:
        upc = [min(up_all, key=lambda c: abs(c["dist_atr"]))]
    if not dnc and dn_all:
        dnc = [min(dn_all, key=lambda c: abs(c["dist_atr"]))]

    def choose(cl, side):
        """质量优先：先过硬门槛（权重），再按 强度-距离惩罚 排序，最后保留分散档位"""
        if not cl:
            return []
        # 1) 质量门槛：只考虑权重 ≥2.0 的簇；不足则退到 ≥1.5，再不行全留
        pool = [c for c in cl if c["score"] >= 2.0] or [c for c in cl if c["score"] >= 1.5] or cl
        # 2) 打分：强度 - 距离惩罚（超过 1.2 ATR 才开始扣）
        for c in pool:
            c["util"] = c["score"] - 2.5 * max(0.0, abs(c["dist_atr"]) - 1.0)
        pool.sort(key=lambda c: -c["util"])
        sel = []
        for c in pool:
            if all(abs(abs(c["dist_atr"]) - abs(s2["dist_atr"])) >= 0.8 for s2 in sel):
                sel.append(c)
            if len(sel) == 3:
                break
        return sel

    def nearest_opp(side, entry):
        """止盈指向最近的对手方成交密集区"""
        pool = dnc if side == "up" else upc
        cand = [abs(c["p"] - entry) for c in pool
                if (c["p"] < entry - atr4 * 1.0 if side == "up" else c["p"] > entry + atr4 * 1.0)]
        return min(cand) if cand else None

    def size_zone(c, side):
        _hv2, _vr2 = vol_state(a)
        # 过渡状态 + 1H 动能为正：不做"提前入场"，并要求离现价至少 0.8%（避免贴着现价开空）
        _reg0 = regime_of(a)
        _h1s = (a.get("tf") or {}).get("1h", {}).get("score") or 0
        _no_pull = (_reg0 == "mid" and _h1s > 0)
        _min_dist = 0.006 if _no_pull else 0.0
        _vmult = 1.25 if _hv2 else 1.0
        hw = max(atr4 * 0.28, px * 0.0022) * _vmult
        lo, hi = c["p"] - hw, c["p"] + hw
        # 提前入场：朝现价拉动 40%（上限 0.35 ATR），并强制保留最小间距，避免变成"市价单"
        min_gap = max(0.0030, atr4 / px * 0.12)
        if side == "up":
            shift = 0.0 if _no_pull else min(atr4 * 0.35, max(0.0, c["p"] - px) * 0.30)
            lo -= shift; hi -= shift
            _floor = px * (1 + max(min_gap, _min_dist))
            if lo < _floor:
                lo = _floor
                hi = max(hi, lo + hw * 0.8)
        else:
            shift = 0.0 if _no_pull else min(atr4 * 0.35, max(0.0, px - c["p"]) * 0.30)
            lo += shift; hi += shift
            _ceil = px * (1 - max(min_gap, _min_dist))
            if hi > _ceil:
                hi = _ceil
                lo = min(lo, hi - hw * 0.8)
        entry = (lo + hi) / 2
        d_tp = nearest_opp(side, entry) or atr4 * 2.2
        d_tp = max(min(d_tp, atr4 * 1.9), atr4 * 0.9)
        # —— 资金费率修正：判断轧空/轧多风险，调整止损与首档止盈
        fr = a.get("fund") or 0.0
        squeeze_short = fr <= -3.0      # 空头拥挤：容易向上轧空
        crowd_long = fr >= 3.0          # 多头拥挤：容易向下杀多
        risk_note = ""
        if side == "up":                # 做空单
            if squeeze_short:
                d_tp *= 0.80; risk_note = "轧空风险·减目标"
            elif crowd_long:
                d_tp *= 1.15; risk_note = "多头拥挤·放宽目标"
        else:                           # 做多单
            if crowd_long:
                d_tp *= 0.80; risk_note = "轧多风险·减目标"
            elif squeeze_short:
                d_tp *= 1.15; risk_note = "空头拥挤·放宽目标"
        if side == "up":
            stop = hi + atr4 * 0.65
            min_stop = max(px * 0.015, atr4 * 0.55) * 1.003 * _vmult
            if squeeze_short:
                stop += atr4 * 0.30                           # 轧空风险：止损放宽
            if entry - stop < min_stop:
                stop = entry + min_stop
            tp1 = entry - d_tp
            tp2 = tp1 - atr4 * 1.3
            tp3 = tp2 - atr4 * 1.3
        else:
            stop = lo - atr4 * 0.65
            if crowd_long:
                stop -= atr4 * 0.30                           # 轧多风险：止损放宽
            min_stop = max(px * 0.015, atr4 * 0.55) * 1.003 * _vmult
            if stop - entry < min_stop:
                stop = entry - min_stop
            tp1 = entry + d_tp
            tp2 = tp1 + atr4 * 1.3
            tp3 = tp2 + atr4 * 1.3
        return lo, hi, stop, tp1, tp2, tp3, risk_note

    zones = {"sell": [], "buy": []}
    # 震荡市不预设方向：两侧都标“区间”，仓位都按轻仓
    if a["bias"] == "偏空":
        sell_tag, buy_tag = "顺势", "逆势"
    elif a["bias"] == "偏多":
        sell_tag, buy_tag = "逆势", "顺势"
    else:
        sell_tag = buy_tag = "区间"
    for c in choose(upc, "up"):
        lo, hi, stop, tp1, tp2, tp3, _note = size_zone(c, "up")
        rr = (c["p"] - tp1) / (stop - c["p"]) if stop > c["p"] else 0
        zones["sell"].append({"tag": sell_tag, "lo": lo, "hi": hi,
                              "entry": c["p"], "stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                              "rr": rr, "score": c["score"], "reasons": list(dict.fromkeys(c["tags"])),
                              "fund_note": _note})
    for c in choose(dnc, "dn"):
        lo, hi, stop, tp1, tp2, tp3, _note = size_zone(c, "dn")
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

# ---------------------------------------------------------------- 箱体识别
def box_levels(h1, px, lookback=96):
    """用摆动高低点 + 聚簇 + 触及次数，算出箱体顶底"""
    b = h1[-lookback:]
    if len(b) < 40:
        return None
    hi = max(x["h"] for x in b)
    lo = min(x["l"] for x in b)
    if lo <= 0 or hi <= lo:
        return None
    sh, sl = [], []
    for i in range(3, len(b) - 3):
        if b[i]["h"] == max(x["h"] for x in b[i - 3:i + 4]):
            sh.append(b[i]["h"])
        if b[i]["l"] == min(x["l"] for x in b[i - 3:i + 4]):
            sl.append(b[i]["l"])

    def cluster(levels, tol=0.0035):
        levels = sorted(levels)
        groups, cur = [], []
        for v in levels:
            if not cur or v / cur[-1] - 1 <= tol:
                cur.append(v)
            else:
                groups.append(cur); cur = [v]
        if cur:
            groups.append(cur)
        return sorted([(sum(g) / len(g), len(g)) for g in groups], key=lambda x: -x[1])

    tops = [x for x in cluster(sh) if x[0] >= hi * 0.985]
    bots = [x for x in cluster(sl) if x[0] <= lo * 1.015]
    top = max(tops, key=lambda x: x[0]) if tops else (hi, 1)
    bot = min(bots, key=lambda x: x[0]) if bots else (lo, 1)

    def touches(level, kind, tol=0.0015):
        """数 K 线实际碰到该价位的次数（比只数摆动点更真实）"""
        n = 0
        for x in b:
            ref = x["h"] if kind == "h" else x["l"]
            if level and abs(ref / level - 1) <= tol:
                n += 1
        return n

    return {"hi": hi, "lo": lo,
            "top": top[0], "top_n": touches(top[0], "h"),
            "bot": bot[0], "bot_n": touches(bot[0], "l"),
            "width": (hi / lo - 1) * 100,
            "pos": (px - lo) / (hi - lo) * 100 if hi > lo else 50}


# ---------------------------------------------------------------- 触发判断
def fetch_my_positions():
    """用 OKX 密钥直接读本账户真实持仓（全自动，不用手改 Secret）"""
    key = os.environ.get("OKX_API_KEY"); sec = os.environ.get("OKX_API_SECRET")
    pw = os.environ.get("OKX_API_PASSPHRASE")
    if not (key and sec and pw):
        return None
    import hmac, hashlib, base64
    path = "/api/v5/account/positions?instType=SWAP"
    n0 = datetime.now(timezone.utc)
    ts = n0.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n0.microsecond // 1000:03d}Z"
    mac = hmac.new(sec.encode(), (ts + "GET" + path).encode(), hashlib.sha256).digest()
    hdr = {"OK-ACCESS-KEY": key, "OK-ACCESS-SIGN": base64.b64encode(mac).decode(),
           "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": pw,
           "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(urllib.request.Request(
                "https://www.okx.com" + path, headers=hdr), timeout=15, context=ctx) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        log(f"POS-FETCH-FAIL {type(e).__name__}")
        return None
    if d.get("code") != "0":
        log(f"POS-FETCH-CODE {d.get('code')} {d.get('msg')}")
        return None
    out = []
    for p in d.get("data") or []:
        try:
            if abs(float(p.get("pos") or 0)) < 1e-12:
                continue
            mark = float(p.get("markPx") or p.get("last") or 0)
            notional = float(p.get("notionalUsd") or 0)
            qty = (notional / mark) if mark else abs(float(p.get("pos") or 0))
            if qty <= 0:
                continue
            _ps = str(p.get("posSide") or "")
            _raw = float(p.get("pos") or 0)
            _short = (_ps == "short") or (_raw < 0)      # net 模式下靠 pos 符号判断
            _side = "sell" if _short else "buy"
            # 交叉校验：强平价在成本上方 → 一定是空
            try:
                _liq = float(p["liqPx"]) if p.get("liqPx") else 0.0
                _avg = float(p.get("avgPx") or 0)
                if _liq and _avg and ((_liq > _avg and _side == "buy") or (_liq < _avg and _side == "sell")):
                    _side = "sell" if _liq > _avg else "buy"
                    log(f"POS-SIDE-FIX {p.get('instId')} 强平价与方向冲突，按强平价修正为 {_side}")
            except Exception:
                pass
            out.append({"coin": p["instId"].split("-")[0],
                        "side": _side,
                        "entry": float(p.get("avgPx") or 0),
                        "qty": round(qty, 4),
                        "liqPx": float(p["liqPx"]) if p.get("liqPx") else None,
                        "lever": p.get("lever"),
                        "upl": float(p.get("upl") or 0)})
        except Exception:
            continue
    return out


def merge_positions(api_pos, cfg):
    """API 真实持仓 + 配置里的止盈计划（按 coin+side 匹配）"""
    plan = {}
    for p in (cfg.get("positions") or []):
        if p.get("coin"):
            plan[f"{p['coin']}:{p.get('side')}"] = p
    merged = []
    for p in api_pos:
        base = dict(p)
        tpl = plan.get(f"{p['coin']}:{p['side']}") or {}
        base["stop"] = tpl.get("stop")
        base["tps"] = tpl.get("tps") or []
        base["tp_pcts"] = tpl.get("tp_pcts") or (
            [100.0 / len(base["tps"])] * len(base["tps"]) if base["tps"] else [])
        merged.append(base)
    return merged


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


def build_table(coins, cfg):
    """每日点位表（09/12/16/21/00 各推一次）
    每币三行：
      BTC---偏空-箱体49%（现价80,531 区间79,324-81,788）
      空 80,758【顺势-开空】止81,581 目标79,863/78,979
      多 80,053【逆势-开多】止79,230 目标80,948/81,833
    """
    lines = []
    for a in coins:
        c = a["coin"]; d = dec_of(a["px"]); z = a["zones"]
        bx = box_levels(a.get("h1") or [], a["px"])
        pos_txt = f"箱体{bx['pos']:.0f}%" if bx else "箱体—"
        _rmap = {"chop": "震荡", "up": "上涨趋势", "down": "下跌趋势", "mid": "过渡"}
        _pminfo = {"bottom": "-筑底区", "top": "-筑顶区"}.get(cycle_phase(a), "")
        head = f"{c}---{a['bias']}-{pos_txt}-{_rmap.get(regime_of(a), '')}{_pminfo}"
        head += f"（现价{a['px']:,.{d}f}"
        if bx:
            head += f" 区间{bx['lo']:,.{d}f}-{bx['hi']:,.{d}f}"
            head += f" 顶{bx['top_n']}次/底{bx['bot_n']}次"
        head += "）"
        lines.append(head)
        if z["sell"]:
            s = z["sell"][0]
            sz = "中仓2%" if s["tag"] == "顺势" else ("轻仓1%" if s["tag"] == "逆势" else "区间1%")
            dp = (s["lo"] / a["px"] - 1) * 100
            lines.append(f"空 {s['lo']:,.{d}f}（{dp:+.2f}%）【{s['tag']}-开空·权重{s['score']:.1f}】止{s['stop']:,.{d}f} "
                         f"目标{s['tp1']:,.{d}f}/{s['tp2']:,.{d}f}｜{sz}"
                         + (f"·{s['fund_note']}" if s.get("fund_note") else ""))
        if z["buy"]:
            b = z["buy"][0]
            sz = "中仓2%" if b["tag"] == "顺势" else ("轻仓1%" if b["tag"] == "逆势" else "区间1%")
            dp = (b["hi"] / a["px"] - 1) * 100
            lines.append(f"多 {b['hi']:,.{d}f}（{dp:+.2f}%）【{b['tag']}-开多·权重{b['score']:.1f}】止{b['stop']:,.{d}f} "
                         f"目标{b['tp1']:,.{d}f}/{b['tp2']:,.{d}f}｜{sz}"
                         + (f"·{b['fund_note']}" if b.get("fund_note") else ""))
    return "\n".join(lines)


def build_work_signal(a):
    """WORK（饼哥）风格的 ETH 方向信号（与点位表同一时点推送）"""
    c = a["coin"]; d = dec_of(a["px"]); z = a["zones"]
    bias = a["bias"]
    tx = "多" if bias == "偏多" else ("空" if bias == "偏空" else "")
    lines = [f"日线方向：{bias}｜现价 {a['px']:,.{d}f}"]
    if not tx:
        lines.append("方向不明（震荡），只做区间，不追单")
        return f"WORK（饼哥）-{c} 观望", "\n".join(lines)
    main_side = "buy" if tx == "多" else "sell"
    alt_side = "sell" if main_side == "buy" else "buy"
    for side, tag_word in ((main_side, "顺势"), (alt_side, "逆势")):
        zs = z[side]
        if not zs:
            continue
        s = zs[0]
        price = s["hi"] if side == "buy" else s["lo"]
        act = "开多" if side == "buy" else "开空"
        sz = "中仓2%" if s["tag"] == "顺势" else ("轻仓1%" if s["tag"] == "逆势" else "区间1%")
        label = "🟢 买入价" if side == "buy" else "🔴 卖出价"
        lines.append(f"\n{act}【{s['tag']}·{sz}】")
        lines.append(f"{label}：{price:,.{d}f}")
        lines.append(f"🛑 止损：{s['stop']:,.{d}f}")
        lines.append(f"🎯 目标：{s['tp1']:,.{d}f} / {s['tp2']:,.{d}f} / {s['tp3']:,.{d}f}")
    lines.append("\n⚠ 挂单价≠现价，不到不成交")
    return f"WORK（饼哥）-{c} 开{tx}信号", "\n".join(lines)


def check_long_ready(coins, state, cfg, ts):
    """顺势多单机会：总分转多 + 4H/1H 同向 + 结构转 HL + 站上 4H EMA20"""
    out = []
    fired = state.setdefault("long_ready", {})
    cnt = state.setdefault("long_ready_cnt", {})
    cd = float(cfg.get("long_ready_cooldown_min", 240)) * 60
    for a in coins:
        c = a["coin"]; t = a["tf"]
        if a["total"] <= -3:                     # 趋势重新转空 → 计数归零
            cnt[c] = 0
        if ts - (fired.get(c) or 0) < cd:
            continue
        if a["total"] < 3:                       # 总分必须转多
            continue
        if t["4h"]["score"] < 3 or t["1h"]["score"] < 3:
            continue
        if t["4h"]["struct"] not in ("HH/HL", "HL"):
            continue
        if a["px"] <= t["4h"]["ema20"]:          # 必须站上 4H EMA20
            continue
        if t["1h"]["rsi"] < 50:
            continue
        fired[c] = ts
        n = int(cnt.get(c, 0)) + 1
        cnt[c] = n
        if n == 1:
            tag = "首次突破｜1/3仓｜最易假突破"
            adv = "先上 1/3，回踩不破再加"
        elif n == 2:
            tag = "第2次突破｜1/2仓｜结构改善待确认"
            adv = "可上 1/2，仍要等回踩确认"
        elif n == 3:
            tag = "第3次突破｜降仓或不做｜警惕三顶"
            adv = "只做 1/4 或不做，前两次失败说明上方压力极大"
        else:
            tag = f"第{n}次突破｜建议不做｜压力极强"
            adv = "不建议参与，等结构彻底改变"
        d = dec_of(a["px"])
        zs = (a.get("zones") or {}).get("buy") or []
        z = zs[0] if zs else None
        stop = z["stop"] if z else t["4h"]["ema20"] * 0.985
        if (a["px"] - stop) / a["px"] > 0.03:      # 止损过宽则收紧到 3%
            stop = a["px"] * 0.97
        risk = (a["px"] - stop) / a["px"] * 100
        body = (f"现价 {a['px']:,.{d}f}｜总分 {a['total']:+d}（{a['bias']}）\n"
                f"日线 {t['1d']['score']:+d}｜4H {t['4h']['score']:+d}｜1H {t['1h']['score']:+d}\n"
                f"结构 {t['4h']['struct']}｜4H EMA20 {t['4h']['ema20']:,.{d}f}（已站上）\n"
                f"RSI 1H {t['1h']['rsi']:.0f}\n"
                f"参考止损 {stop:,.{d}f}（-{risk:.1f}%）\n"
                + (f"买区目标 {z['tp1']:,.{d}f} / {z['tp2']:,.{d}f}\n" if z else "")
                + f"建议：{adv}\n"
                + "这是趋势转多信号，不是抢反弹｜单笔风险≤2%")
        out.append(dict(coin=c, rule="longready", ts=ts, important=True,
                        title=f"DEEP（单独提醒）-[{tag}]{c} 顺势多单机会", body=body))
    return out


def check_eth_short_ready(coins, state, cfg, ts):
    """ETH 滞涨确认空点：到卖区 + 1H 收阴 + RSI 回落 + 4H 未转强"""
    a = next((x for x in coins if x["coin"] == "ETH"), None)
    if not a:
        return []
    key = state.setdefault("eth_short", {})
    cd = float(cfg.get("eth_short_cooldown_min", 240))
    if ts - key.get("ts", 0) < cd * 60:
        return []
    px = a["px"]; d = dec_of(px)
    sells = (a.get("zones") or {}).get("sell") or []
    if not sells:
        return []
    zone = sells[0]
    if px < zone["lo"] * 0.997:            # 必须已进入卖区附近
        return []
    h1 = a.get("h1") or []
    if len(h1) < 3:
        return []
    rsi = a["tf"]["1h"]["rsi"]
    prev_rsi = key.get("rsi")
    key["rsi"] = rsi
    bearish = h1[-1]["c"] < h1[-1]["o"]
    falling = prev_rsi is not None and rsi < prev_rsi
    four = a["tf"]["4h"]["score"]
    if not (bearish and falling and four <= 3):
        return []
    key["ts"] = ts
    body = (f"现价 {px:,.{d}f}｜已到卖区 {zone['lo']:,.{d}f}-{zone['hi']:,.{d}f}\n"
            f"1H 收阴｜RSI {prev_rsi:.0f} → {rsi:.0f}（回落）\n"
            f"4H 评分 {four:+d}（未转强）\n"
            f"止损 {zone['stop']:,.{d}f}｜目标 {zone['tp1']:,.{d}f} / {zone['tp2']:,.{d}f}\n"
            f"轻仓 1%｜滞涨确认，不是提前挂单")
    return [dict(coin="ETH", rule="ethshort", ts=ts, important=True,
                 title="DEEP（单独提醒）-ETH 空单机会（滞涨确认）", body=body)]


def paper_ref_levels(coins, state):
    """每日锁定前一日的最高/最低，作为突破参考（固定值，不随价格移动）"""
    ref = state.setdefault("paper_ref", {})
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    if ref.get("day") != day or not ref.get("levels"):
        lv = {}
        for a in coins:
            h1 = a.get("h1") or []
            if len(h1) >= 48:
                prev = h1[-48:-24]          # 前一日 24 根小时线
                lv[a["coin"]] = {"hi": max(x["h"] for x in prev), "lo": min(x["l"] for x in prev)}
        ref["day"] = day
        ref["levels"] = lv
    return ref.get("levels") or {}


CORR_GROUP = {"BTC": ("BTC", "ETH", "SOL"), "ETH": ("BTC", "ETH", "SOL"),
              "SOL": ("BTC", "ETH", "SOL"), "ZEC": ("ZEC",)}


def has_opposite(pp, c, side):
    """同组内是否已有反向持仓（BTC/ETH/SOL 视为高相关组）"""
    grp = CORR_GROUP.get(c, (c,))
    other = "buy" if side == "sell" else "sell"
    return any(v.get("status") == "open" and v.get("side") == other
               and v.get("coin") in grp for v in pp.values())


def check_paper_signals(coins, state, ts, track="paper", notional=2000.0, label="",
                        acct=None, risk_scale=1.0):
    """模拟仓触发：① 距区间 ≤0.35%  ② 跌破昨日低点/站上昨日高点

    track/notional 支持双轨（A 稳健 / B 激进）并行测试
    """
    out = []
    pp = state.setdefault(track, {})
    refs = paper_ref_levels(coins, state)
    CD = 4 * 3600
    _base_eq = float(acct) if acct else 10000.0
    _realized = sum((v.get("pnl") or 0) for v in (state.get(track) or {}).values()
                    if v.get("status") == "closed")
    ACCT = max(_base_eq * 0.2, _base_eq + _realized)   # 本金 + 已实现盈亏 = 权益
    CRYPTO_ONLY = ("BTC", "ETH", "ZEC", "SOL")
    BJ_TZ = timezone(timedelta(hours=8))
    # —— 熔断统计（从已平仓记录实时算，不额外存状态）
    _closed = sorted([p for p in pp.values() if p.get("status") == "closed" and p.get("closed")],
                     key=lambda p: p["closed"])
    _consec = 0
    for _p in reversed(_closed):
        if (_p.get("pnl") or 0) < 0:
            _consec += 1
        else:
            break
    _today = datetime.now(BJ_TZ).strftime("%m-%d")
    _day_pnl = sum(_p.get("pnl") or 0 for _p in _closed
                   if datetime.fromtimestamp(_p["closed"], BJ_TZ).strftime("%m-%d") == _today)
    _halt = _day_pnl <= -0.06 * ACCT          # 单日亏损 6% 停手
    _open_now = [v for v in pp.values() if v.get("status") == "open"]
    _open_swing = [v for v in _open_now if v.get("kind", "swing") == "swing"]
    _open_sell = sum(1 for v in _open_swing if v.get("side") == "sell")
    _open_buy = sum(1 for v in _open_swing if v.get("side") == "buy")
    _open_notional = sum((v.get("qty") or 0) * (v.get("entry") or 0) for v in _open_now)
    if _halt:
        log(f"paper{label} 熔断：今日已亏 {_day_pnl:.0f}，本周期不开新仓")
    for a in coins:
        c = a["coin"]; px = a["px"]
        if c not in CRYPTO_ONLY:
            continue                          # 股票不参与模拟仓
        if _halt:
            continue
        r = refs.get(c) or {}
        zs = a.get("zones") or {}
        for side, lst in (("sell", zs.get("sell") or []), ("buy", zs.get("buy") or [])):
            key = f"{c}:{side}:{int(ts)}"
            # 同币同向若还有持仓就跳过；冷却看最近一次平仓时间
            _same = [v for v in pp.values() if v.get("coin") == c and v.get("side") == side
                     and v.get("kind", "swing") == "swing"]
            if any(v.get("status") == "open" for v in _same):
                continue
            _cls = [v for v in _same if v.get("status") == "closed" and v.get("closed")]
            if _cls:
                _last = max(_cls, key=lambda v: v["closed"])
                # 冷却：盈利平仓 1 小时、亏损平仓 2 小时
                _cd = 3600 if (_last.get("pnl") or 0) > 0 else 7200
                if ts - (_last.get("closed") or 0) < _cd:
                    continue
            near = False; z = None
            if lst:
                z = lst[0]
                edge = z["lo"] if side == "sell" else z["hi"]
                near = abs(px - edge) / px <= 0.0040
            # 突破追单已停用（2026-11-24）：今日突破单 24 笔 0 胜，全是追高被套
            brk = False
            if not near:
                continue
            # 记录入场前 6 小时涨跌，供后续统计"弱势单 vs 反弹单"
            h1 = a.get("h1") or []
            chg6 = ((h1[-1]["c"] / h1[-7]["c"] - 1) * 100) if len(h1) >= 7 else 0.0
            # 数据异常：单小时暴涨暴跌 >30% 视为异常，跳过
            _h1 = a.get("h1") or []
            if len(_h1) >= 2 and _h1[-2]["c"] > 0:
                if abs(_h1[-1]["c"] / _h1[-2]["c"] - 1) > 0.30:
                    log(f"{c} 价格异常跳动，跳过本周期")
                    continue
            # 资金费率只作提示，不做硬过滤（付费/收费本身是公平的，且反映拥挤方向）
            _fr = a.get("fund") or 0.0
            # 总敞口限制：同方向最多 2 笔；总额外名不超账户 2 倍
            if side == "sell" and _open_sell >= 2:
                continue
            if side == "buy" and _open_buy >= 2:
                continue
            if has_opposite(pp, c, side):
                continue            # 高相关币已有反向仓，不再开（避免碰巧对冲）
            if _open_notional >= ACCT * 2:
                continue
            # 行情状态过滤：单边趋势里不逆势开单
            reg = regime_of(a)
            _ct = ((reg == "up" and side == "sell") or (reg == "down" and side == "buy"))
            if _ct and (z is None or float(z.get("score") or 0) < 8.0):
                continue                                  # 逆势单：只放行权重≥8 的强位
            # 同一价位被测试 ≥3 次就不再参与（避免三顶/三底假突破）
            if z is not None:
                lvl = z["entry"]
                hits = 0; _in = False
                _band = (a["atr4h"] or lvl * 0.02) * 0.25     # 只算"真正触及"（0.25 ATR 内）
                for x in (a.get("h1") or [])[-36:]:            # 回看 36 小时
                    _now = abs(x["h"] - lvl) <= _band or abs(x["c"] - lvl) <= _band
                    if _now and not _in:
                        hits += 1
                    _in = _now
                if hits >= 4:                                  # 4 次以上视为"磨烂了"
                    continue
                if reg == "chop" and hits < 1:
                    continue            # 震荡市第一次触碰先观察，等二次确认
            if brk:
                pl = auto_plan(a, {"entry": px, "side": side})
                stop_, tps_ = pl["stop"], pl["tps"]
                why = (f"跌破昨日低点 {r['lo']:,.0f}" if side == "sell"
                       else f"站上昨日高点 {r['hi']:,.0f}")
                tag = "突破"
            else:
                stop_, tps_ = z["stop"], [z["tp1"], z["tp2"], z["tp3"]]
                why = f"接近{('卖区' if side == 'sell' else '买区')} {edge:,.0f}"
                tag = z.get("tag", "")
            # 方向校验：止盈必须在入场价的正确一侧，否则用 ATR 重算
            bad = ((side == "sell" and (any(t >= px for t in tps_) or stop_ <= px)) or
                   (side == "buy" and (any(t <= px for t in tps_) or stop_ >= px)))
            if bad:
                pl = auto_plan(a, {"entry": px, "side": side})
                stop_, tps_ = pl["stop"], pl["tps"]
                why += "｜区间价已失效，改用 ATR"
            # 按信号强度分级定仓：8x / 4x / 2x，并用"单笔亏损≤账户4%"封顶
            wt = float(z["score"]) if z else 1.5
            if wt >= 4.0:
                mult, tier, cap = 8.0, "强(权重≥4)", 0.04
            elif wt >= 2.0:
                mult, tier, cap = 4.0, "中(权重2-4)", 0.025
            else:
                mult, tier, cap = 2.0, "弱(权重<2)", 0.012
            if _ct:
                mult *= 0.3                     # 逆势单：仓位压到三成
            if _consec >= 3:
                mult *= 0.5                     # 连亏 3 笔，仓位减半
            if datetime.now(BJ_TZ).weekday() >= 5:
                mult *= 0.6                     # 周末流动性差，降仓
            if reg == "chop":
                mult *= 0.5                     # 震荡市仓位减半
            _hv, _vr = vol_state(a)
            if _hv:
                mult *= 0.6                     # 高波动：仓位再降 40%
            ph = cycle_phase(a)
            if ph == "bottom" and side == "sell":
                mult *= 0.5                     # 筑底区做空减半
            if ph == "top" and side == "buy":
                mult *= 0.5                     # 筑顶区做多减半
            notional_used = notional * mult * (ACCT / _base_eq)
            stop_pct = abs(px - stop_) / px
            capped = False
            cap = cap * risk_scale
            if (not _hv) and notional_used * stop_pct > ACCT * cap:
                notional_used = ACCT * cap / stop_pct
                capped = True
            # === 真实保证金约束：本金有限，已占用部分必须扣除 ===
            _lev = max(1.0, min(10.0, float(mult)))
            _margin_used = 0.0
            for _v0 in _open_now:
                _m0 = _v0.get("margin")
                if _m0 is None:
                    _q0 = float(_v0.get("qty") or 0); _e0 = float(_v0.get("entry") or 0)
                    _l0 = float(_v0.get("lev_used") or _v0.get("mult") or 1) or 1.0
                    _m0 = _q0 * _e0 / _l0
                _margin_used += float(_m0)
            _avail = max(0.0, ACCT - _margin_used)
            if _avail < ACCT * 0.05:
                log(f"{c} 保证金不足：可用 {_avail:,.0f}U（已用 {_margin_used:,.0f}U），跳过")
                continue
            _max_n = _avail * 0.9 * _lev
            if notional_used > _max_n:
                notional_used = _max_n
                capped = True
            if notional_used < 300:
                log(f"{c} 可用保证金仅 {_avail:,.0f}U，名义不足 300U，跳过")
                continue
            qty = notional_used / px
            # 分批入场：突破单不分批（追单再补仓没有意义），区间单才分批
            atr4_sig = a["atr4h"] or px * 0.02
            if brk:
                trg = [px]                                  # 突破单：一次成交
            elif side == "sell":
                trg = [edge, min(edge + atr4_sig * 0.30, stop_ - atr4_sig * 0.1),
                       min(edge + atr4_sig * 0.60, stop_ - atr4_sig * 0.05)]
            else:
                trg = [edge, max(edge - atr4_sig * 0.30, stop_ + atr4_sig * 0.1),
                       max(edge - atr4_sig * 0.60, stop_ + atr4_sig * 0.05)]
            _main_q = (qty * 0.7) if brk else (qty * 0.7 / 3.0)
            pp[key] = {"coin": c, "side": side, "entry": px, "qty": _main_q, "stop": stop_,
                       "tps": list(tps_), "tp_done": 0, "opened": ts, "status": "open",
                       "tag": tag, "pnl": 0.0, "why": why, "weight": wt, "tier": tier,
                       "mult": round(notional_used / notional, 1),
                       "lev_used": _lev, "margin": notional_used / _lev,
                       "trg": trg, "trg_done": 1, "each": (0.0 if brk else _main_q), "ver": PAPER_VER, "reg": reg, "ph": ph,
                       "atr": atr4_sig, "best": px, "best_ts": ts, "ct": bool(_ct), "chg6": chg6,
                       "orig_qty": _main_q}
            # 趋势持有仓（runner）：30% 数量，宽止损、无分批止盈，最长 72 小时
            _rq = qty * 0.30
            if _rq > 0:
                _rst = (px + atr4_sig * 2.0) if side == "sell" else (px - atr4_sig * 2.0)
                pp[f"{key}:RUN"] = {"coin": c, "side": side, "entry": px, "qty": _rq,
                                    "stop": _rst, "tps": [], "tp_done": 0, "opened": ts,
                                    "status": "open", "pnl": 0.0, "why": why + "｜趋势仓",
                                    "tag": tag, "ver": PAPER_VER, "kind": "runner",
                                    "atr": atr4_sig, "best": px, "best_ts": ts,
                                    "reg": reg, "tl": 72 * 3600,
                                    "lev_used": _lev, "margin": (_rq * px) / _lev}
            w = "空" if side == "sell" else "多"
            risk_acct = notional_used * stop_pct / ACCT * 100
            out.append(dict(coin=c, rule="paperopen", ts=ts, important=True,
                            title=f"GG-模拟开仓{label}（{c}）{w}",
                            body=(f"入场 {px:,.2f}｜首档数量 {_main_q:.4f}（总名义 ${notional_used:,.0f}）\n"
                                  f"触发：{why}\n"
                                  f"信号强度：{tier}｜权重 {wt:.1f}｜目标倍数 {mult:.0f}x"
                                  + (f"（风险封顶→{notional_used/notional:.1f}x）" if capped else "") + "\n"
                                  + (f"分批：{trg[0]:,.{dec_of(px)}f} / {trg[1]:,.{dec_of(px)}f} / {trg[2]:,.{dec_of(px)}f}（每档 1/3）\n"
                                     if not brk else "分批：不分批（突破单一次成交）\n")
                                  + f"止损 {stop_:,.2f}（-{stop_pct*100:.1f}%）｜账户风险 {risk_acct:.1f}%\n"
                                  + f"费率 {_fr:+.2f}bp"
                                  + ("（多头付费）" if _fr > 0.5 else ("（空头付费）" if _fr < -0.5 else "（接近零）")) + "\n"
                                  + f"止盈 {' / '.join(f'{t:,.2f}' for t in tps_)}\n"
                                  + "模拟仓，不影响真实账户")))
    return out


STOCK_REF = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "TSLA", "INTC", "MRVL", "MU"]


def stock_snapshot():
    """当日美股等权指数 vs 加密（从最近一次 22:30 开盘起算）"""
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    od = now_bj.replace(hour=22, minute=30, second=0, microsecond=0)
    if now_bj.hour < 5 or now_bj.hour < 22 or (now_bj.hour == 22 and now_bj.minute < 30):
        od -= timedelta(days=1)
    ots = int(od.timestamp())
    chg = {}
    for s0 in STOCK_REF:
        try:
            k = klines(s0, "1h", 30)
        except Exception:
            k = []
        base = next((x["c"] for x in k if x["t"] >= ots), None)
        if base:
            chg[s0] = (k[-1]["c"] / base - 1) * 100
    if not chg:
        return None
    idx = sum(chg.values()) / len(chg)
    lines = [f"美股等权（{len(chg)} 只）{idx:+.2f}%｜自 {od:%m-%d %H:%M} 起"]
    for s0, v in sorted(chg.items(), key=lambda x: -x[1]):
        lines.append(f"  {s0:6} {v:+.2f}%")
    lines.append("")
    lines.append("加密同期：")
    for c0 in ("BTC", "ETH", "ZEC", "SOL"):
        try:
            k = klines(c0, "1h", 30)
        except Exception:
            continue
        base = next((x["c"] for x in k if x["t"] >= ots), None)
        if base:
            lines.append(f"  {c0:4} {(k[-1]['c']/base-1)*100:+.2f}%")
    verdict = ("美股强势 → 加密偏多环境" if idx >= 0.8 else
               "美股弱势 → 加密偏空环境" if idx <= -0.8 else "美股窄幅 → 加密走自身逻辑")
    lines.append("")
    lines.append("结论：" + verdict + "（相关性 0.4-0.5，仅作环境参考）")
    return idx, "\n".join(lines)


def check_stock_ref(coins, state, cfg, ts):
    """美股开盘后 10 分钟、收盘后 10 分钟各推一次走势参考"""
    out = []
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    hm = now_bj.hour * 60 + now_bj.minute
    slot = None
    if 22 * 60 + 30 <= hm <= 22 * 60 + 45:
        slot = "open"
    elif 5 * 60 <= hm <= 5 * 60 + 15:
        slot = "close"
    if not slot:
        return out
    key = f"stock_{slot}_{now_bj:%m%d}"
    if state.get("stock_ref_key") == key:
        return out
    snap = stock_snapshot()
    if not snap:
        return out
    state["stock_ref_key"] = key
    idx, body = snap
    out.append(dict(coin="US", rule="stockref", ts=ts, important=True,
                    title=("美股开盘·走势参考" if slot == "open" else "美股收盘·走势参考"),
                    body=body))
    return out


# ================== MM 视角（扫损确认）独立策略 ==================
def mm_sweep_signal(a):
    """MM 视角：价格扫过阻力/支撑（吃掉止损）后快速收回，才给信号。
    做空：最近 3 小时内某根 1H 高点 > 卖区上沿 + 0.25 ATR，且现价回到卖区下方
    做多：镜像
    返回 (side, entry, stop, tps, info) 或 None
    """
    zs = a.get("zones") or {}
    px = a["px"]; atr = a["atr4h"] or px * 0.02
    h1 = a.get("h1") or []
    if len(h1) < 6:
        return None
    recent = h1[-3:]
    # 做空：扫上沿后回落
    for z in (zs.get("sell") or [])[:1]:
        hi = z["hi"]; lo = z["lo"]
        swp = max(x["h"] for x in recent)
        if swp > hi + atr * 0.25 and px < hi:
            stop = swp + atr * 0.25
            d = abs(z.get("tp1", px - atr * 1.5) - px) or atr * 1.5
            tps = [px - d, px - d - atr * 1.2]
            return ("sell", px, stop, tps, f"扫过卖区上沿 {hi:,.0f} 后回落（扫损确认）")
    # 做多：扫下沿后收回
    for z in (zs.get("buy") or [])[:1]:
        lo = z["lo"]; hi = z["hi"]
        swp = min(x["l"] for x in recent)
        if swp < lo - atr * 0.25 and px > lo:
            stop = swp - atr * 0.25
            d = abs(z.get("tp1", px + atr * 1.5) - px) or atr * 1.5
            tps = [px + d, px + d + atr * 1.2]
            return ("buy", px, stop, tps, f"扫破买区下沿 {lo:,.0f} 后收回（扫损确认）")
    return None


def check_mm_signals(coins, state, ts, track, notional, label="", acct=10000.0, risk_scale=1.0):
    out = []
    pp = state.setdefault(track, {})
    CD = 3600
    for a in coins:
        c = a["coin"]
        if c not in ("BTC", "ETH", "ZEC", "SOL"):
            continue
        sig = mm_sweep_signal(a)
        if not sig:
            continue
        side, px, stop, tps, why = sig
        _same = [v for v in pp.values() if v.get("coin") == c and v.get("side") == side]
        if any(v.get("status") == "open" for v in _same):
            continue
        _cls = [v for v in _same if v.get("status") == "closed" and v.get("closed")]
        if _cls:
            _last = max(_cls, key=lambda v: v["closed"])
            if ts - (_last.get("closed") or 0) < CD:
                continue
        if any(v.get("status") == "open" for v in pp.values()):
            _opens = [v for v in pp.values() if v.get("status") == "open"]
            if len(_opens) >= 2:
                continue
        if has_opposite(pp, c, side):
            continue
        # 仓位：按止损距离反算，风险 1.5%（A 基准），B/C 通过 notional 放大
        eq = acct + sum((v.get("pnl") or 0) for v in pp.values() if v.get("status") == "closed")
        stop_pct = abs(px - stop) / px
        risk_frac = 0.015 * risk_scale
        notional_used = min(notional * (eq / acct), eq * risk_frac / max(stop_pct, 0.005))
        # 真实保证金：扣除同轨已占用
        _lev = max(1.0, min(10.0, notional / 1000.0))
        _mused = 0.0
        for _v0 in pp.values():
            if _v0.get("status") != "open":
                continue
            _m0 = _v0.get("margin")
            if _m0 is None:
                _m0 = (float(_v0.get("qty") or 0) * float(_v0.get("entry") or 0)) / max(1.0, float(_v0.get("lev_used") or 1))
            _mused += float(_m0)
        _avail = max(0.0, eq - _mused)
        if _avail < eq * 0.05:
            log(f"MM {c} 保证金不足：可用 {_avail:,.0f}U，跳过")
            continue
        notional_used = min(notional_used, _avail * 0.9 * _lev)
        if notional_used < 300:
            log(f"MM {c} 可用保证金仅 {_avail:,.0f}U，跳过")
            continue
        qty = notional_used / px
        key = f"MM:{c}:{side}:{int(ts)}"
        pp[key] = {"coin": c, "side": side, "entry": px, "qty": qty, "stop": stop,
                   "tps": list(tps), "tp_done": 0, "opened": ts, "status": "open",
                   "pnl": 0.0, "why": why, "tag": "MM扫损", "ver": PAPER_VER,
                   "atr": a["atr4h"] or px * 0.02, "best": px, "best_ts": ts,
                   "reg": regime_of(a), "tl": 12 * 3600, "mult": round(notional_used / notional, 2),
                   "lev_used": _lev, "margin": notional_used / _lev}
        w = "空" if side == "sell" else "多"
        out.append(dict(coin=c, rule="mmopen", ts=ts, important=True,
                        title=f"MM-扫损开仓{label}（{c}）{w}",
                        body=(f"入场 {px:,.2f}｜名义 ${notional_used:,.0f}（{notional_used/notional:.1f}x 基准）\n"
                              f"触发：{why}\n"
                              f"止损 {stop:,.2f}（-{stop_pct*100:.1f}%）｜止盈 "
                              f"{' / '.join(f'{t:,.2f}' for t in tps)}\n"
                              f"MM 视角：等扫损失败才入场，止损放在扫描高点之外")))
    return out


def check_us_ref(coins, state, cfg, ts):
    """美股开盘 35 分钟后（北京 23:05）推一条"当天方向参考"：
    用 QQQ/SPY + 加密同期涨跌做环境确认，不产生交易信号。
    """
    out = []
    if not cfg.get("us_ref", True):
        return out
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    if now_bj.weekday() >= 5:
        return out
    hm = now_bj.hour * 60 + now_bj.minute
    if not (23 * 60 + 5 <= hm <= 23 * 60 + 20):
        return out
    key = f"usref_{now_bj:%m%d}"
    if state.get("us_ref_key") == key:
        return out
    od = now_bj.replace(hour=22, minute=30, second=0, microsecond=0)
    ots = int(od.timestamp())

    def chg(sym, n=12):
        try:
            k = klines(sym, "1h", n)
        except Exception:
            return None
        base = next((x["c"] for x in k if x["t"] >= ots), None)
        return (k[-1]["c"] / base - 1) * 100 if base else None

    qqq = chg("QQQ"); spy = chg("SPY")
    cr = {c: chg(c) for c in ("BTC", "ETH", "ZEC", "SOL")}
    cr = {k: v for k, v in cr.items() if v is not None}
    if qqq is None or not cr:
        return out
    state["us_ref_key"] = key
    avg_c = sum(cr.values()) / len(cr)
    lines = [f"纳指100 {qqq:+.2f}%｜标普500 {(spy or 0):+.2f}%（自 22:30 起）", ""]
    for c, v in cr.items():
        lines.append(f"{c} {v:+.2f}%")
    lines.append("")
    lines.append(f"加密均值 {avg_c:+.2f}%｜纳指 {qqq:+.2f}%")
    if qqq > 0.3 and avg_c > 0.3:
        vd = "同向偏多 → 环境一致，做多可正常执行"
    elif qqq < -0.3 and avg_c < -0.3:
        vd = "同向偏空 → 环境一致，做空可正常执行"
    elif qqq > 0.3 and avg_c < -0.3:
        vd = "纳指涨、加密跌 → 加密偏弱，做多谨慎"
    elif qqq < -0.3 and avg_c > 0.3:
        vd = "纳指跌、加密涨 → 加密偏强，做空谨慎"
    else:
        vd = "两边都窄幅 → 加密走自身逻辑"
    lines.append("")
    lines.append("参考：" + vd)
    lines.append("（相关 0.63、同期不领先，仅作环境确认）")
    out.append(dict(coin="US", rule="usref", ts=ts, important=True,
                    title="美股开盘方向参考（23:05）", body="\n".join(lines)))
    log("us_ref pushed qqq=%.2f avg_c=%.2f" % (qqq, avg_c))
    return out


def combo_signal(a):
    """GG 点位 + MM 确认：
    ① 近 6 小时内价格曾触及 GG 的区间（GG 本会开仓）
    ② 之后又扫过区间外侧 0.25 ATR（MIM 的扫损动作）
    ③ 现在回到区间内侧 → 综合信号
    返回 (side, entry, stop, tps, info) 或 None
    """
    zs = a.get("zones") or {}
    px = a["px"]; atr = a["atr4h"] or px * 0.02
    h1 = a.get("h1") or []
    if len(h1) < 8:
        return None
    recent = h1[-6:]
    for z in (zs.get("sell") or [])[:1]:
        lo, hi = z["lo"], z["hi"]
        touched = any(x["l"] <= lo * 1.004 for x in recent)
        swept = any(x["h"] >= hi + atr * 0.25 for x in recent)
        if touched and swept and px < hi:
            swp = max(x["h"] for x in recent)
            stop = swp + atr * 0.25
            tp1 = z.get("tp1") or (px - atr * 1.5)
            tps = [tp1, tp1 - atr * 1.2]
            return ("sell", px, stop, tps,
                    f"先触及卖区 {lo:,.0f}、再扫到 {swp:,.0f} 后回落")
    for z in (zs.get("buy") or [])[:1]:
        lo, hi = z["lo"], z["hi"]
        touched = any(x["h"] >= hi * 0.996 for x in recent)
        swept = any(x["l"] <= lo - atr * 0.25 for x in recent)
        if touched and swept and px > lo:
            swp = min(x["l"] for x in recent)
            stop = swp - atr * 0.25
            tp1 = z.get("tp1") or (px + atr * 1.5)
            tps = [tp1, tp1 + atr * 1.2]
            return ("buy", px, stop, tps,
                    f"先触及买区 {hi:,.0f}、再扫到 {swp:,.0f} 后收回")
    return None


def check_combo_signals(coins, state, cfg, ts):
    """GG 点位 + MM 确认 的综合信号（仅推送，不建模拟仓）"""
    out = []
    cd = int(cfg.get("combo_cooldown_min", 180)) * 60
    cdmap = state.setdefault("combo_ts", {})
    for a in coins:
        c = a["coin"]
        if c not in ("BTC", "ETH", "ZEC", "SOL"):
            continue
        if ts - (cdmap.get(c) or 0) < cd:
            continue
        sig = combo_signal(a)
        if not sig:
            continue
        side, px, stop, tps, why = sig
        d = dec_of(px)
        cdmap[c] = ts
        w = "空" if side == "sell" else "多"
        # 同时记入独立模拟轨 paper_combo（本金 10,000，单笔风险约 3%）
        try:
            pc = state.setdefault("paper_combo", {})
            eq = 10000.0 + sum((v.get("pnl") or 0) for v in pc.values() if v.get("status") == "closed")
            stop_pct = abs(px - stop) / px
            notional_used = min(2000.0 * (eq / 10000.0), eq * 0.03 / max(stop_pct, 0.005))
            _lev = 5.0
            _mused = 0.0
            for _v0 in pc.values():
                if _v0.get("status") != "open":
                    continue
                _m0 = _v0.get("margin")
                if _m0 is None:
                    _m0 = (float(_v0.get("qty") or 0) * float(_v0.get("entry") or 0)) / max(1.0, float(_v0.get("lev_used") or 1))
                _mused += float(_m0)
            _avail = max(0.0, eq - _mused)
            if _avail < eq * 0.05:
                log(f"COMBO {c} 保证金不足：可用 {_avail:,.0f}U，跳过")
                raise RuntimeError("no-margin")
            notional_used = min(notional_used, _avail * 0.9 * _lev)
            pc[f"COMBO:{c}:{side}:{int(ts)}"] = {
                "coin": c, "side": side, "entry": px, "qty": notional_used / px,
                "stop": stop, "tps": list(tps), "tp_done": 0, "opened": ts,
                "status": "open", "pnl": 0.0, "why": why, "tag": "GG+MM",
                "ver": PAPER_VER, "atr": a["atr4h"] or px * 0.02,
                "best": px, "best_ts": ts, "reg": "mid", "tl": 48 * 3600,
                "kind": "swing", "orig_qty": notional_used / px,
                "lev_used": _lev, "margin": notional_used / _lev,
            }
        except Exception as _e:
            log(f"combo paper error {_e}")
        out.append(dict(coin=c, rule="combo", ts=ts, important=True,
                        title=f"(GG+MM)-综合信号（{c}）{w}",
                        body=(f"入场 {px:,.{d}f}｜方向 {w}\n"
                              f"逻辑：{why}\n"
                              f"止损 {stop:,.{d}f}（-{abs(stop-px)/px*100:.1f}%）\n"
                              f"止盈 {' / '.join(f'{t:,.{d}f}' for t in tps)}\n"
                              f"GG 给出位置，MM 确认扫损失败后才触发")))
    return out


def check_resonance(coins, state, cfg, ts):
    """多币共振爬升：≥3 个币的 15m 同时"低点抬高 + 2 小时涨 0.4-2%"。
    只做提示（不做自动开仓），给出短线做多的入场/止损/目标。
    """
    out = []
    cd = int(cfg.get("resonance_cooldown_min", 240)) * 60
    if ts - (state.get("resonance_ts") or 0) < cd:
        return out
    hits = []
    for a in coins:
        c = a["coin"]
        if c not in ("BTC", "ETH", "ZEC", "SOL"):
            continue
        try:
            k = klines(c, "15m", 12)
        except Exception:
            k = []
        if len(k) < 9:
            continue
        seg = k[-9:]
        hl = all(seg[i]["l"] >= seg[i-1]["l"] * 0.998 for i in range(1, 9))
        chg = (seg[-1]["c"] / seg[0]["o"] - 1) * 100
        if hl and 0.4 <= chg <= 2.0:
            hits.append((c, a["px"], min(x["l"] for x in seg), chg))
    if len(hits) < 3:
        return out
    state["resonance_ts"] = ts
    lines = [f"共振爬升 {len(hits)}/4 个币同时抬高低点（15m）",
             "统计：共振后 4 小时上涨概率约 66%（对照单币 47%）",
             "性质：短线 scalp，不是趋势单 —— 轻仓、快进快出", ""]
    for c, px, low, chg in hits:
        d = dec_of(px)
        stop = low * 0.996                      # 最近 15m 低点下方 0.4%
        tp1 = px * 1.006                        # +0.6%
        tp2 = px * 1.010                        # +1.0%（8 小时内 52% 可达）
        lines.append(f"{c} {px:,.{d}f}｜2h 涨 {chg:+.2f}%")
        lines.append(f"  买 {px:,.{d}f}｜止{stop:,.{d}f}｜目标 {tp1:,.{d}f}/{tp2:,.{d}f}")
    lines.append("")
    lines.append("建议：仓位 0.3%，持有上限 4-8 小时，到 TP1 先平一半")
    out.append(dict(coin="MULTI", rule="resonance", ts=ts, important=True,
                    title="共振｜GG-多币共振短线（做多）", body="\n".join(lines)))
    # 记入独立模拟轨 paper_res，便于单独统计共振策略胜率
    pr = state.setdefault("paper_res", {})
    for c, px, low, chg in hits:
        key = f"RES:{c}:{int(ts)}"
        pr[key] = {"coin": c, "side": "buy", "entry": px, "qty": (1000.0 / px),
                   "stop": low * 0.996, "tps": [px * 1.006, px * 1.010], "tl": 8 * 3600,
                   "tp_done": 0, "opened": ts, "status": "open", "pnl": 0.0,
                   "atr": px * 0.008, "best": px, "best_ts": ts, "reg": "chop",
                   "tag": "共振", "why": f"多币共振（2h {chg:+.2f}%）", "ver": PAPER_VER}
    log("resonance alert: " + ",".join(h[0] for h in hits))
    return out


def check_table(coins, state, cfg, ts):
    """到点推点位表：09:00 / 12:00 / 16:00 / 21:00 / 00:00（北京时间）"""
    slots = cfg.get("table_slots") or [9, 12, 16, 21, 0]
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    day_key = now_bj.strftime("%Y-%m-%d")
    fired = state.setdefault("table_fired", {})
    for h in slots:
        slot_id = f"{day_key} {h:02d}"
        if fired.get(slot_id):
            continue
        start = now_bj.replace(hour=h, minute=0, second=0, microsecond=0)
        delta = (now_bj - start).total_seconds() / 60.0
        window = float(cfg.get("table_window_min", 25))
        if 0 <= delta <= window:
            fired[slot_id] = True
            # 只保留最近 20 条记录
            if len(fired) > 40:
                for k in sorted(fired)[:-20]:
                    fired.pop(k, None)
            evs = []
            title = f"点位表 {now_bj.strftime('%m-%d %H:%M')}"
            body = build_table(coins, cfg) + "\n———\n单笔风险≤2%｜杠杆≤10x｜到1档平50%并移止损到成本"
            _ps = paper_summary(state)
            if _ps:
                body += _ps
            evs.append(dict(coin="行情", rule="table", ts=ts, important=True, title=title, body=body))
            # WORK（饼哥）ETH 方向信号，同一时点
            eth = next((x for x in coins if x["coin"] == "ETH"), None)
            if eth and (cfg.get("work_signal_coins") or ["ETH"]).count("ETH"):
                wt, wb = build_work_signal(eth)
                evs.append(dict(coin="ETH", rule="worksig", ts=ts, important=True,
                                title=f"{wt} {now_bj.strftime('%m-%d %H:%M')}", body=wb))
            return evs
    return []


def auto_plan(a, p):
    """没有手动计划时，按 ATR + 箱体自动生成止损与三档止盈"""
    px = a["px"]; atr = px * (a.get("atr_pct") or 1.2) / 100.0
    entry = float(p["entry"]); side = p["side"]
    bx = box_levels(a.get("h1") or [], px)
    if side == "sell":
        stop = entry + atr * 0.65
        tps = [entry - atr * k for k in (1.0, 1.6, 2.3)]
        if bx and bx["lo"] < entry:          # 末档不跌破箱底，但不能破坏排序
            cand = max(tps[-1], bx["lo"])
            if cand < tps[-2]:
                tps[-1] = cand
    else:
        stop = entry - atr * 0.65
        tps = [entry + atr * k for k in (1.0, 1.6, 2.3)]
        if bx and bx["hi"] > entry:
            cand = min(tps[-1], bx["hi"])
            if cand > tps[-2]:
                tps[-1] = cand
    return {"stop": round(stop, 2), "tps": [round(t, 2) for t in tps]}


def position_advice(a, p):
    """给新持仓算：箱体位置 + ATR 三档止盈止损"""
    d = dec_of(a["px"]); px = a["px"]
    atr_p = a.get("atr_pct") or 1.2
    atr = px * atr_p / 100.0
    entry = p["entry"]; side = p["side"]
    bx = box_levels(a.get("h1") or [], px)
    lines = []
    if bx:
        lines.append(f"箱体位置 {bx['pos']:.0f}%（{bx['lo']:,.{d}f}-{bx['hi']:,.{d}f}）")
    if side == "sell":          # 空单
        stop = entry + atr * 0.65
        tps = [entry - atr * k for k in (1.0, 1.6, 2.3)]
        if bx:
            tps = [max(t, bx["lo"]) for t in tps]     # 不跌破箱底
    else:                       # 多单
        stop = entry - atr * 0.65
        tps = [entry + atr * k for k in (1.0, 1.6, 2.3)]
        if bx:
            tps = [min(t, bx["hi"]) for t in tps]     # 不突破箱顶
    lines.append(f"止损 {stop:,.{d}f}（{abs(stop/entry-1)*100:.2f}%）")
    lines.append(f"止盈 {tps[0]:,.{d}f} / {tps[1]:,.{d}f} / {tps[2]:,.{d}f}")
    lines.append("分批 50% / 25% / 25%｜到1档把止损移到成本")
    return "\n".join(lines)


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
                           title=f"GG-持仓（{c}）到达 {'/'.join('TP' + str(i + 1) for i in hit)}",
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
                               title=f"GG-持仓（{c}）触发止损 {stop:,.{d}f}",
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


def paper_open(ev, a, state, ts):
    """模拟开仓：区间信号触发时自动建仓（名义 $2000）"""
    pp = state.setdefault("paper", {})
    key = f"{ev['coin']}:{ev.get('side')}"
    if pp.get(key, {}).get("status") == "open":
        return None
    entry = a["px"]; qty = 2000.0 / entry
    pp[key] = {"coin": ev["coin"], "side": ev.get("side"), "entry": entry,
               "qty": qty, "stop": ev["stop"], "tps": list(ev["tps"]),
               "tp_done": 0, "opened": ts, "status": "open",
               "tag": ev.get("tag", ""), "pnl": 0.0}
    w = "空" if ev.get("side") == "sell" else "多"
    return dict(coin=ev["coin"], rule="paperopen", ts=ts, important=True,
                title=f"GG-模拟开仓（{ev['coin']}）{w}",
                body=(f"入场 {entry:,.2f}｜数量 {qty:.4f}（名义 $2,000）\n"
                      f"【{ev.get('tag','')}】止损 {ev['stop']:,.2f}\n"
                      f"止盈 {' / '.join(f'{t:,.2f}' for t in ev['tps'])}\n"
                      f"模拟仓，不影响真实账户"))


def paper_manage(px_map, state, ts, track="paper", label=""):
    """模拟仓：判止损/分批止盈"""
    out = []
    pp = state.setdefault(track, {})
    for key, p in list(pp.items()):
        if p.get("status") != "open":
            continue
        px = px_map.get(p["coin"])
        if not px:
            continue
        d = dec_of(px); sell = p["side"] == "sell"
        # 分批补仓：价格深入区间时补第 2/3 档
        trg = p.get("trg") or []
        while trg and p.get("trg_done", len(trg)) < len(trg):
            t = trg[p["trg_done"]]
            reach = (px >= t) if sell else (px <= t)
            if not reach:
                break
            add = p.get("each", p["qty"])
            old_q = p["qty"]
            p["entry"] = (p["entry"] * old_q + t * add) / (old_q + add)
            p["qty"] = old_q + add
            p["trg_done"] = p.get("trg_done", 1) + 1
            out.append(dict(coin=p["coin"], rule="paperadd", ts=ts, important=False,
                            title=f"GG-模拟补仓{label}（{p['coin']}）",
                            body=(f"第 {p['trg_done']}/3 档成交 @ {t:,.{d}f}\n"
                                  f"均价 {p['entry']:,.{d}f}｜总数量 {p['qty']:.4f}")))
        # 滚仓：TP1 后价格回撤 0.6-1.5 ATR 且仍在成本之上，补回一半仓位（每笔只滚一次）
        if p.get("tp_done", 0) >= 1 and not p.get("rolled"):
            _a0 = p.get("atr") or (p["entry"] * 0.02)
            _best_now = p.get("best", p["entry"])
            _best_now = min(_best_now, px) if sell else max(_best_now, px)
            _retr = (_best_now - px) if sell else (px - _best_now)
            _inzone = (px < p["entry"]) if sell else (px > p["entry"])
            if 0.6 * _a0 <= _retr <= 1.5 * _a0 and _inzone:
                _add = (p.get("orig_qty") or p["qty"]) * 0.5
                p["qty"] = p["qty"] + _add
                p["roll_qty"] = _add
                p["rolled"] = True
                p["stop"] = (px + _a0 * 0.8) if sell else (px - _a0 * 0.8)
                out.append(dict(coin=p["coin"], rule="paperroll", ts=ts, important=True,
                                title=f"GG-滚仓加回{label}（{p['coin']}）",
                                body=(f"回撤 {_retr/_a0:.1f} ATR 后加回 {_add:.4f}\n"
                                      f"加仓价 {px:,.{d}f}｜总数量 {p['qty']:.4f}\n"
                                      f"止损移到 {p['stop']:,.{d}f}")))
        # 跟踪止损：TP1 之后把止损朝有利方向跟进（0.6 ATR 追踪最优价）
        _a = p.get("atr") or (p["entry"] * 0.02)
        _old_best = p.get("best", p["entry"])
        _best = min(_old_best, px) if sell else max(_old_best, px)
        if _best != _old_best:
            p["best_ts"] = ts
        p["best"] = _best
        if p.get("kind") == "runner":
            _favr = ((p["entry"] - px) if sell else (px - p["entry"]))
            if _favr >= _a * 1.0:
                _tr = (_best + _a * 1.5) if sell else (_best - _a * 1.5)
                p["stop"] = min(p["stop"], _tr) if sell else max(p["stop"], _tr)
        if p.get("tp_done", 0) >= 1:
            _trail = (_best + _a * 0.80) if sell else (_best - _a * 0.80)
            p["stop"] = min(p["stop"], _trail) if sell else max(p["stop"], _trail)
        # 卡在 TP1 与入场之间超过 12 小时：先平掉剩余，避免反复磨
        _stall = ts - p.get("best_ts", p.get("opened", ts))
        if 1 <= p.get("tp_done", 0) < len(p["tps"]) and _stall > 8 * 3600:
            pnl = ((p["entry"] - px) if sell else (px - p["entry"])) * p["qty"]
            p["status"] = "closed"; p["exit"] = px; p["closed"] = ts
            p["reason"] = p.get("reason") or "平仓"
            p["pnl"] = round(p.get("pnl", 0) + pnl, 2)
            p["reason"] = "滞涨8h"
            out.append(dict(coin=p["coin"], rule="paperclose", ts=ts, important=False,
                            title=f"GG-模拟滞涨平仓{label}（{p['coin']}）",
                            body=(f"TP1 后 {_stall//3600:.0f} 小时没创新低/新高，先平剩余\n"
                                  f"{p['entry']:,.{d}f} → {px:,.{d}f}\n"
                                  f"本笔 {pnl:+,.0f} USDT｜累计 {p['pnl']:+,.0f}")))
            continue
        # 时间止损：24 小时未触及第一档止盈就平掉
        _tl = p.get("tl") or 48 * 3600           # 共振用 8 小时，其他 48 小时上限
        if p.get("tp_done", 0) == 0 and ts - p.get("opened", ts) > _tl:
            pnl = ((p["entry"] - px) if sell else (px - p["entry"])) * p["qty"]
            p["status"] = "closed"; p["exit"] = px; p["closed"] = ts
            p["reason"] = p.get("reason") or "平仓"
            p["pnl"] = round(p.get("pnl", 0) + pnl, 2)
            p["reason"] = "超时"
            out.append(dict(coin=p["coin"], rule="paperclose", ts=ts, important=True,
                            title=f"GG-模拟超时平仓{label}（{p['coin']}）",
                            body=(f"持仓 {_tl//3600} 小时未到 TP1\n"
                                  f"{p['entry']:,.{d}f} → {px:,.{d}f}\n"
                                  f"本笔 {pnl:+,.0f} USDT｜累计 {p['pnl']:+,.0f}")))
            continue
        # 宽保本：浮盈 2% 移到成本，3.5% 锁 1%（0.6% 太容易被噪音扫，2% 才需要真趋势）
        _fav2 = ((p["entry"] - px) if sell else (px - p["entry"])) / p["entry"]
        if _fav2 >= 0.035:
            _lock = p["entry"] * (0.99 if sell else 1.01)      # 锁 1%
            p["stop"] = min(p["stop"], _lock) if sell else max(p["stop"], _lock)
        elif _fav2 >= 0.02:
            p["stop"] = min(p["stop"], p["entry"]) if sell else max(p["stop"], p["entry"])
        _buf = _a * 0.15                      # 止损确认缓冲：插针不算
        stop_hit = (px >= p["stop"] + _buf) if sell else (px <= p["stop"] - _buf)
        if stop_hit:
            pnl = (p["entry"] - p["stop"] if sell else p["stop"] - p["entry"]) * p["qty"]
            p["status"] = "closed"; p["exit"] = p["stop"]; p["closed"] = ts
            p["reason"] = "止损"
            p["pnl"] = round(p.get("pnl", 0) + pnl, 2)
            out.append(dict(coin=p["coin"], rule="paperclose", ts=ts, important=True,
                            title=f"GG-模拟止损{label}（{p['coin']}）",
                            body=(f"{p['entry']:,.{d}f} → {p['stop']:,.{d}f}\n"
                                  f"本笔 {pnl:+,.0f} USDT｜累计 {p['pnl']:+,.0f}")))
            continue
        for i, tp in enumerate(p["tps"]):
            if i < p["tp_done"]:
                continue
            hit = (px <= tp) if sell else (px >= tp)
            if not hit:
                break
            frac = [0.5, 0.25, 0.25][i] if i < 3 else 1.0
            part = p["qty"] * frac
            pnl = ((p["entry"] - tp) if sell else (tp - p["entry"])) * part
            p["pnl"] = round(p.get("pnl", 0) + pnl, 2)
            p["tp_done"] = i + 1
            if i == 0:
                _a = p.get("atr") or (p["entry"] * 0.02)
                # 锁住部分 TP1 利润（止损放在 TP1 之外 0.35 ATR），而不是只移到成本
                p["stop"] = (tp - _a * 0.50) if sell else (tp + _a * 0.50)
                p["tp1_price"] = tp
            if i == len(p["tps"]) - 1:
                p["status"] = "closed"; p["exit"] = tp; p["closed"] = ts
                p["reason"] = f"止盈{i+1}"
            out.append(dict(coin=p["coin"], rule="papertp", ts=ts, important=True,
                            title=f"GG-模拟止盈{i+1}{label}（{p['coin']}）",
                            body=(f"触及 {tp:,.{d}f}，平掉 {frac*100:.0f}%\n"
                                  f"本档 {pnl:+,.0f} USDT｜累计 {p['pnl']:+,.0f}"
                                  + ("\n止损已移到成本" if i == 0 else ""))))
            if p["status"] == "closed":
                break
    return out


def paper_apply_cmds(state):
    """从 mm/paper_cmds.json 读取人工调整指令（幂等）"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_cmds.json")
    try:
        with open(path, encoding="utf-8") as f:
            cmds = json.load(f)
    except Exception:
        return
    pp = state.setdefault("paper", {})
    for c in cmds or []:
        k = f"{c.get('coin')}:{c.get('side')}"
        p = pp.get(k)
        if not p or p.get("status") != "open":
            continue
        if c.get("stop") is not None:
            p["stop"] = float(c["stop"])
        if c.get("tps"):
            p["tps"] = [float(x) for x in c["tps"]]
        if c.get("close"):
            p["status"] = "closed"
            p["closed"] = time.time()
            p["exit"] = float(c.get("px") or p["entry"])


def paper_summary(state):
    pp = state.get("paper") or {}
    op = [p for p in pp.values() if p.get("status") == "open"]
    if not op:
        return ""
    lines = ["", "— 模拟仓 —"]
    for p in op:
        w = "空" if p["side"] == "sell" else "多"
        lines.append(f"{p['coin']} {w} {p['entry']:,.2f}｜止损 {p['stop']:,.2f}"
                     f"｜已止盈{p.get('tp_done',0)}档｜{p.get('pnl',0):+,.0f}U")
    return "\n".join(lines)


# 费率状态 × 当日趋势 → 次日下跌概率（实测，Gate 数据，119 天样本）
# 值：(次日下跌概率%, 样本数)
FUND_PROB = {
    "BTC": {("中性", "上涨"): (61, 57), ("中性", "下跌"): (41, 58)},
    "ETH": {("中性", "上涨"): (54, 65), ("中性", "下跌"): (35, 54)},
    "ZEC": {("中性", "上涨"): (51, 43), ("中性", "下跌"): (52, 44),
            ("偏多", "上涨"): (54, 13), ("偏多", "下跌"): (23, 13)},
}


# 事件研究：费率极值出现后，未来 1/3/7 日「上涨概率%」（不含当日趋势条件）
# 值：(1日涨%, 3日涨%, 7日涨%, 样本数)。样本 <8 时仅作提示
FUND_EVENT = {
    "ZEC": {"空头拥挤": (44, 56, 50, 16),
            "空头偏挤": (34, 37, 49, 35),
            "多头拥挤": (50, 50, 50, 2)},
    "ETH": {"空头偏挤": (50, 50, 50, 2)},
}


def fund_event(coin, bucket):
    d = FUND_EVENT.get(coin) or {}
    return d.get(bucket, (0, 0, 0, 0))


def fund_prob(coin, bucket, direction):
    """返回 (次日下跌概率, 样本数)；无数据时返回 (0, 0)"""
    d = FUND_PROB.get(coin) or {}
    return d.get((bucket, direction), (0, 0))


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

    # 1.5) 已进区间但未确认：跟踪极值，回撤 0.2% 给「确认信号」
    if act and not act.get("confirmed"):
        ext_key = "ext_hi" if act["side"] == "sell" else "ext_lo"
        if ext_key not in act:
            act[ext_key] = px
        if act["side"] == "sell":
            act[ext_key] = max(act[ext_key], px)
            pull = (act[ext_key] - px) / act[ext_key] if act[ext_key] else 0
            if pull >= 0.002:
                act["confirmed"] = True
                ev.append(dict(coin=c, rule="confirm", ts=ts, important=True,
                               title=f"{c} 空单确认（回落 {pull*100:.2f}%）",
                               body=(f"卖区 {act['entry']:,.{d}f} 触发后冲高到 {act[ext_key]:,.{d}f}，"
                                     f"现价 {px:,.{d}f} 回落 {pull*100:.2f}%\n"
                                     f"→ 滞涨确认，可执行开空\n"
                                     f"止损 {act['stop']:,.{d}f}｜目标 {act['tp1']:,.{d}f}/{act['tp2']:,.{d}f}")))
        else:
            act[ext_key] = min(act[ext_key], px)
            lift = (px - act[ext_key]) / act[ext_key] if act[ext_key] else 0
            if lift >= 0.002:
                act["confirmed"] = True
                ev.append(dict(coin=c, rule="confirm", ts=ts, important=True,
                               title=f"{c} 多单确认（回升 {lift*100:.2f}%）",
                               body=(f"买区 {act['entry']:,.{d}f} 触发后下探到 {act[ext_key]:,.{d}f}，"
                                     f"现价 {px:,.{d}f} 回升 {lift*100:.2f}%\n"
                                     f"→ 企稳确认，可执行开多\n"
                                     f"止损 {act['stop']:,.{d}f}｜目标 {act['tp1']:,.{d}f}/{act['tp2']:,.{d}f}")))

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
                    drift = (px / zz["entry"] - 1) * 100
                    if (side == "sell" and drift > 0.4) or (side == "buy" and drift < -0.4):
                        drift_txt = f"\n⚠ 现价已偏离挂单价 {abs(drift):.2f}%，点位已走出，别追"
                    elif (side == "sell" and drift < -0.4) or (side == "buy" and drift > 0.4):
                        drift_txt = f"\nℹ 现价距挂单价 {abs(drift):.2f}%，还有空间"
                    else:
                        drift_txt = ""
                    ev.append(dict(coin=c, rule="zone", ts=ts, important=True,
                                   side=side, stop=zz["stop"], tag=zz["tag"],
                                   tps=[zz["tp1"], zz["tp2"], zz["tp3"]],
                                   title=f"{c} 进入{label} {zz['entry']:,.{d}f}",
                                   body=(f"{dirn}·【{zz['tag']}】｜区间 {zz['lo']:,.{d}f}-{zz['hi']:,.{d}f}\n"
                                         f"★挂在 {zz['lo'] if side == 'sell' else zz['hi']:,.{d}f}"
                                         f"（靠现价一侧，更容易成交）\n"
                                         f"止损 {zz['stop']:,.{d}f}（破了无条件走）\n"
                                         f"三档止盈：{zz['tp1']:,.{d}f} / {zz['tp2']:,.{d}f} / {zz['tp3']:,.{d}f}\n"
                                         f"→ 到1档平50%并把止损移到成本，到2档再平25%，剩25%拿3档\n"
                                         f"{size}｜杠杆≤10x｜RR {zz['rr']:.2f}｜{'、'.join(zz['reasons'][:2])}"
                                         + drift_txt
                                         + f"\n⏳ 等确认：从极值回撤0.2%再进场（会单独提醒）"
                                         + extra)))
                    state["active"][c] = {"side": side, "idx": i, "entry": zz["entry"], "stop": zz["stop"],
                                          "tp1": zz["tp1"], "tp2": zz["tp2"], "tp3": zz.get("tp3"),
                                          "tag": zz["tag"], "ts": ts, "tp1_hit": False,
                                          "confirmed": False}
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

    # 2.8) 箱体顶底：测试 / 突破
    bx = box_levels(a.get("h1") or [], px)
    if bx and bx["width"] >= 0.8:
        db = dec_of(px)
        mid = (bx["top"] + bx["bot"]) / 2
        # 测试箱顶（被触及≥2次才算硬顶）
        if (bx["top_n"] >= 2 and abs(px / bx["top"] - 1) <= 0.0035
                and fire(state, f"{c}:boxtop", 240, ts)):
            ev.append(dict(coin=c, rule="box", ts=ts, important=True,
                           title=f"{c} 测试箱顶 {bx['top']:,.{db}f}（第{bx['top_n']}次）",
                           body=(f"现价 {px:,.{db}f}｜箱体 {bx['lo']:,.{db}f} - {bx['hi']:,.{db}f}"
                                 f"（宽 {bx['width']:.1f}%）｜位置 {bx['pos']:.0f}%\n"
                                 f"该价位已被触及 {bx['top_n']} 次\n"
                                 f"→ 冲不过去可轻仓空，目标中轴 {mid:,.{db}f}\n"
                                 f"→ 4H 收盘站上 {bx['top'] * 1.002:,.{db}f} 才算真突破")))
        # 测试箱底
        if (bx["bot_n"] >= 2 and abs(px / bx["bot"] - 1) <= 0.0035
                and fire(state, f"{c}:boxbot", 240, ts)):
            ev.append(dict(coin=c, rule="box", ts=ts, important=True,
                           title=f"{c} 测试箱底 {bx['bot']:,.{db}f}（第{bx['bot_n']}次）",
                           body=(f"现价 {px:,.{db}f}｜箱体 {bx['lo']:,.{db}f} - {bx['hi']:,.{db}f}"
                                 f"（宽 {bx['width']:.1f}%）｜位置 {bx['pos']:.0f}%\n"
                                 f"该价位已被触及 {bx['bot_n']} 次\n"
                                 f"→ 撑住可轻仓多，目标中轴 {mid:,.{db}f}\n"
                                 f"→ 4H 收盘跌破 {bx['bot'] * 0.998:,.{db}f} 才算真破位")))
        # 突破箱体
        if bx["hi"] and prev_px <= bx["hi"] < px and fire(state, f"{c}:boxup", 360, ts):
            ev.append(dict(coin=c, rule="box", ts=ts, important=True,
                           title=f"{c} 箱体上破 {bx['hi']:,.{db}f}",
                           body=(f"现价 {px:,.{db}f}｜原箱体 {bx['lo']:,.{db}f} - {bx['hi']:,.{db}f}"
                                 f"（宽 {bx['width']:.1f}%）\n"
                                 f"→ 突破成立，回踩 {bx['hi']:,.{db}f} 不破可做多\n"
                                 f"→ 若 4H 收盘又跌回 {bx['hi']:,.{db}f} 下方 = 假突破，反手")))
        if bx["lo"] and prev_px >= bx["lo"] > px and fire(state, f"{c}:boxdn", 360, ts):
            ev.append(dict(coin=c, rule="box", ts=ts, important=True,
                           title=f"{c} 箱体下破 {bx['lo']:,.{db}f}",
                           body=(f"现价 {px:,.{db}f}｜原箱体 {bx['lo']:,.{db}f} - {bx['hi']:,.{db}f}"
                                 f"（宽 {bx['width']:.1f}%）\n"
                                 f"→ 破位成立，反抽 {bx['lo']:,.{db}f} 不破可做空\n"
                                 f"→ 若 4H 收盘又站回 {bx['lo']:,.{db}f} 上方 = 假破位，反手")))

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

def _okx_headers(method, path):
    """有密钥就签名；没有则返回匿名头（公开行情可用，持仓接口会 401）"""
    key = os.environ.get("OKX_API_KEY", "").strip()
    sec = os.environ.get("OKX_API_SECRET", "").strip()
    pwd = os.environ.get("OKX_API_PASSPHRASE", "").strip()
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
         "Accept": "application/json", "Referer": "https://www.okx.com/"}
    if not (key and sec and pwd):
        return h
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    msg = (ts + method.upper() + path).encode()
    sig = base64.b64encode(hmac.new(sec.encode(), msg, hashlib.sha256).digest()).decode()
    h.update({"OK-ACCESS-KEY": key, "OK-ACCESS-SIGN": sig,
              "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": pwd})
    return h


def fetch_okx_traders(traders_map=None, timeout=12):
    """抓 OKX Orbit 公开持仓（带密钥签名）；返回 {uid: [positions]} 或 None"""
    uid = {"872873784163041281": "天才铁头娃",
           "872898006700732416": "十一的八字",
           "872838143249428480": "十老板",
           "872913887149293569": "天才亏钱娃"}
    out, ok_any = {}, False
    for aid, nick in uid.items():
        path = f"/api/v5/orbit/public/position-current?authorId={aid}"
        try:
            req = urllib.request.Request("https://www.okx.com" + path,
                                         headers=_okx_headers("GET", path))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                code = r.getcode()
                body = r.read(200000).decode("utf-8", "replace")
            if code == 200:
                j = json.loads(body)
                rows = j.get("data") or []
                if isinstance(rows, dict):
                    rows = rows.get("positions") or rows.get("posData") or []
                rows = rows if isinstance(rows, list) else []
                # data 形如 [{"posData":[{...}]}]，需要摊平
                _flat = []
                for _r0 in rows:
                    if isinstance(_r0, dict) and isinstance(_r0.get("posData"), list):
                        _flat.extend(_r0["posData"])
                    elif isinstance(_r0, dict):
                        _flat.append(_r0)
                rows = _flat
                # 只保留"仍有仓位"的记录（pos 非 0）
                def _alive(r0):
                    try:
                        return abs(float(r0.get("pos") or 0)) > 0
                    except Exception:
                        return True
                alive = [r0 for r0 in rows if _alive(r0)]
                _CV = {"BTC-USDT-SWAP": 0.01, "ETH-USDT-SWAP": 0.1, "SOL-USDT-SWAP": 1.0,
                       "ZEC-USDT-SWAP": 0.1, "DOGE-USDT-SWAP": 1000.0, "XRP-USDT-SWAP": 100.0,
                       "SOXL-USDT-SWAP": 1.0}
                for _r9 in alive:
                    _raw_q = abs(float(_r9.get("pos") or 0))
                    _inst9 = str(_r9.get("instId") or "")
                    _cv = _CV.get(_inst9) or 1.0
                    _r9["coinQty"] = _raw_q * _cv
                    _r9["ctVal"] = _cv
                out[aid] = alive
                ok_any = True
                log(f"okx {nick}: raw={len(rows)} alive={len(alive)}")
                if rows:
                    try:
                        log("okx sample " + nick + ": " + json.dumps(rows[0], ensure_ascii=False)[:400])
                    except Exception:
                        pass
                for r0 in alive[:3]:
                    log(f"   {r0.get('instId')} {r0.get('posSide')} pos={r0.get('pos')} upl={r0.get('upl')}")
                ok_any = True
            else:
                log(f"okx trader {nick} HTTP {code}")
                out[aid] = None
        except urllib.error.HTTPError as e:
            log(f"okx trader {nick} HTTP {e.code}")
            out[aid] = None
        except Exception as e:
            log(f"okx trader {nick} {type(e).__name__}")
            out[aid] = None
    return out if (ok_any or out) else None


def probe_okx_history(author_id="872873784163041281"):
    """探测可能的历史平仓接口，结果打到日志"""
    cands = [
        f"/api/v5/orbit/public/position-history?authorId={author_id}",
        f"/api/v5/orbit/public/position-his?authorId={author_id}",
        f"/api/v5/orbit/public/trade-history?authorId={author_id}",
        f"/api/v5/orbit/public/close-position?authorId={author_id}",
        f"/api/v5/orbit/public/position-history-current?authorId={author_id}",
        f"/api/v5/orbit/public/position-history?authorId={author_id}&instType=SWAP",
        f"/api/v5/orbit/public/order-history?authorId={author_id}",
    ]
    _res = []
    for path in cands:
        try:
            req = urllib.request.Request("https://www.okx.com" + path, headers=_okx_headers("GET", path))
            with urllib.request.urlopen(req, timeout=12) as r:
                code = r.getcode(); body = r.read(800).decode("utf-8", "replace")
            _line = f"{path.split('?')[0]} -> {code} {body[:160]}"
            _res.append(_line); log("PROBE " + _line)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read(120).decode("utf-8", "replace")
            except Exception:
                detail = ""
            _line = f"{path.split('?')[0]} -> HTTP {e.code} {detail[:120]}"
            _res.append(_line); log("PROBE " + _line)
        except Exception as e:
            _line = f"{path.split('?')[0]} -> {type(e).__name__}"
            _res.append(_line); log("PROBE " + _line)
    return _res


_CTV = {"cache": {}}


def ct_val(inst_id, state=None):
    """合约面值（张 → 币）。ETH 0.1 / BTC 0.01 / SOL 1 ..."""
    if not inst_id:
        return 1.0
    if inst_id in _CTV["cache"]:
        return _CTV["cache"][inst_id]
    if state is not None:
        c0 = (state.get("ctval") or {}).get(inst_id)
        if c0:
            _CTV["cache"][inst_id] = float(c0)
            return float(c0)
    path = f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
    try:
        req = urllib.request.Request("https://www.okx.com" + path, headers=_okx_headers("GET", path))
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read(50000).decode("utf-8", "replace"))
        for it in (j.get("data") or []):
            if str(it.get("instId")) == inst_id and it.get("ctVal"):
                v = float(it["ctVal"])
                _CTV["cache"][inst_id] = v
                if state is not None:
                    state.setdefault("ctval", {})[inst_id] = v
                return v
    except Exception:
        pass
    # 兜底：常见合约面值
    _DEF = {"BTC-USDT-SWAP": 0.01, "ETH-USDT-SWAP": 0.1, "SOL-USDT-SWAP": 1.0,
            "ZEC-USDT-SWAP": 0.1, "DOGE-USDT-SWAP": 1000.0, "XRP-USDT-SWAP": 100.0,
            "SOXL-USDT-SWAP": 1.0}
    v0 = _DEF.get(inst_id, 1.0)
    _CTV["cache"][inst_id] = v0
    if state is not None:
        state.setdefault("ctval", {})[inst_id] = v0
    return v0


def fetch_okx_history(uid, nick, limit=100, timeout=15):
    """拉取历史平仓记录（orbit public position-history）"""
    path = f"/api/v5/orbit/public/position-history?authorId={uid}&limit={limit}"
    out = []
    try:
        req = urllib.request.Request("https://www.okx.com" + path, headers=_okx_headers("GET", path))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read(400000).decode("utf-8", "replace"))
    except Exception as e:
        log(f"okx history {nick} {type(e).__name__}")
        return out
    rows = j.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("list") or []
    for r0 in rows:
        if not isinstance(r0, dict):
            continue
        try:
            _ot0 = int(r0.get("cTime") or 0) // 1000          # 开仓时间
            _ct0 = int(r0.get("uTime") or 0) // 1000          # 平仓时间
            ts = _ct0 or _ot0                                  # 展示用：平仓时间
        except Exception:
            ts = 0
            _ot0 = _ct0 = 0
        inst = str(r0.get("instId") or "").replace("-USDT-SWAP", "")
        side_raw = str(r0.get("posSide") or r0.get("direction") or "")
        side = "空" if ("short" in side_raw or "空" in side_raw or "sell" in side_raw) else ("多" if side_raw else "")
        try:
            qty = abs(float(r0.get("closeAmount") or r0.get("closeTotalPos") or 0))
        except Exception:
            qty = 0.0
        try:
            qty = qty * float(r0.get("ctVal") or 1)
        except Exception:
            pass
        pnl = 0.0
        for k in ("pnl", "realizedPnl", "closePnl", "profit"):
            if r0.get(k) not in (None, ""):
                try:
                    pnl = float(r0[k])
                    break
                except Exception:
                    pass
        def _f(k):
            try:
                return float(r0.get(k) or 0)
            except Exception:
                return 0.0
        _ot = 0
        try:
            _ot = int(r0.get("cTime") or 0) // 1000
        except Exception:
            _ot = 0
        _ct = ts or 0
        _dur = max(0, _ct - _ot) // 60 if (_ot and _ct) else None
        try:
            _tot = abs(float(r0.get("openMaxAmount") or 0)) * float(r0.get("ctVal") or 1)
        except Exception:
            _tot = 0.0
        _open_s = (datetime.fromtimestamp(_ot0, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                   if _ot0 else "")
        out.append({"ts": datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
                    "open_ts": _open_s,
                    "nick": nick, "coin": inst, "side": side, "qty": qty, "total_qty": _tot,
                    "upl": _f("realizedPnl") or pnl,
                    "avg": _f("openAvgPx"), "close_avg": _f("closeAvgPx"),
                    "fee": _f("fee"), "ctype": str(r0.get("closeType") or ""),
                    "dur": _dur, "lever": r0.get("lever"), "_raw": r0})
    return out


def render(dash, cfg):
    import html as _h
    import json as _json
    coins = dash.get("coins") or []
    ts = dash.get("ts", "")
    CSS = """<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--tx:#c9d1d9;--dim:#8b949e;--up:#3fb950;--dn:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.55 -apple-system,Segoe UI,Roboto,"Microsoft YaHei",sans-serif}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
h1{font-size:16px;margin:0;font-weight:600}.meta{color:var(--dim);font-size:12px}
.wrap{display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:14px;padding:14px;align-items:stretch}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:auto auto;gap:14px;align-content:space-between;height:100%}
@media(max-width:1100px){.wrap{grid-template-columns:1fr}.grid2{grid-template-columns:1fr}}
.card,.side,.rank-wrap{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card{overflow:hidden}.side{display:flex;flex-direction:column;align-self:stretch;overflow:auto}
.row{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.coin{font-size:18px;font-weight:700}.px{font-size:17px;font-variant-numeric:tabular-nums}
.tag{font-size:11px;padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
.big{font-size:22px;font-weight:700}.up{color:var(--up)}.dn{color:var(--dn)}.dim{color:var(--dim)}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
td{padding:5px 0;vertical-align:top}td.k{color:var(--dim);width:76px}
.num{font-variant-numeric:tabular-nums}.w{font-size:11px;color:var(--dim)}
.bar{height:4px;background:#21262d;border-radius:2px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:var(--acc)}
.rank-wrap{margin:4px 14px 18px}
h2{font-size:14px;margin:0 0 10px;font-weight:600}
.rank-t{width:100%;border-collapse:collapse;font-size:13px}
.rank-t th{text-align:left;color:var(--dim);font-weight:500;font-size:12px;padding:6px 8px;border-bottom:1px solid var(--line)}
.rank-t td{padding:7px 8px;border-bottom:1px solid #21262d}
.rank-t tr:first-child td{background:#1c2128}
.note{color:var(--dim);font-size:11px;margin:10px 0 0;line-height:1.6}
.pblock{padding:10px 0;flex:0 0 auto}.pblock:last-of-type .pmsep{display:none}
.pcoin{font-weight:700;font-size:14px;margin-bottom:3px}
.pline{font-size:12.5px;line-height:1.75;margin:3px 0}
.dim2{color:#6e7681;font-size:11.5px}
.plabel{font-size:11px;letter-spacing:.5px;margin:4px 0 2px}.plabel.gg{color:#58a6ff}.plabel.mm{color:#d29922}
.pmsep{height:3px;background:linear-gradient(90deg,#58a6ff,#d29922);border-radius:2px;margin:8px 0 4px;opacity:.75}
.h4wrap{grid-column:1 / -1;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.btrader{font-size:14px;font-weight:700;margin:14px 0 6px;color:#e6edf3}
.bblock{padding:6px 0 12px;border-top:1px solid #21262d}.bblock:first-of-type{border-top:none}
.bsub{font-size:12px;color:var(--acc);margin:10px 0 4px}
.bstat{font-size:12px;color:#9aa7b8;margin:6px 0 2px}.bempty{color:var(--dim);font-size:12.5px;padding:2px 0 4px}
footer{padding:10px 18px;color:var(--dim);font-size:12px;border-top:1px solid var(--line)}
@media(max-width:760px){.hist-t th:nth-child(6),.hist-t td:nth-child(6),.hist-t th:nth-child(7),.hist-t td:nth-child(7),.hist-t th:nth-child(9),.hist-t td:nth-child(9),.hist-t th:nth-child(10),.hist-t td:nth-child(10){display:none}}
</style>"""

    def fd(v, dd):
        return "—" if v is None else f"{v:,.{dd}f}"

    cards, panel = [], []
    for c in coins:
        px = c["px"]
        dd = 0 if px >= 10000 else (1 if px >= 1000 else 2)
        tf = c["tf"]
        z = c.get("zones") or {}
        atr = c.get("atr4h") or px * 0.02
        tot = c["total"]
        cls = "up" if tot >= 3 else ("dn" if tot <= -3 else "dim")
        rows = [f'<tr><td class="k">日/4H/1H</td><td>{tf["1d"]["score"]:+d} / {tf["4h"]["score"]:+d} / '
                f'{tf["1h"]["score"]:+d} · RSI {tf["1h"]["rsi"]:.0f}</td></tr>',
                f'<tr><td class="k">结构</td><td>{tf["4h"]["struct"]} · ATR {c.get("atr_pct", 0):.2f}%</td></tr>']
        gg, mm = [], []
        s0 = (z.get("sell") or [None])[0]
        b0 = (z.get("buy") or [None])[0]
        if s0:
            dist = (s0["lo"] / px - 1) * 100
            rows.append(f'<tr><td class="k">GG 空</td><td><span class="dn num">{fd(s0["lo"], dd)}</span> '
                        f'({dist:+.2f}%) <span class="w">权重 {s0["score"]:.1f}</span><br>'
                        f'<span class="dim">止 {fd(s0["stop"], dd)} · 目标 {fd(s0["tp1"], dd)}/{fd(s0["tp2"], dd)}</span><br>'
                        f'<span class="w">{_h.escape(" + ".join(s0["reasons"][:3]))}</span></td></tr>')
            gg.append(f'<div class="pline"><span class="dn">空</span> <span class="num">{fd(s0["lo"], dd)}</span>'
                      f'<span class="dim">（{dist:+.2f}%）</span> <span class="w">权重{s0["score"]:.1f}</span><br>'
                      f'<span class="dim2">止 {fd(s0["stop"], dd)} · 目标 {fd(s0["tp1"], dd)}/{fd(s0["tp2"], dd)}</span></div>')
            sw = s0["hi"] + atr * 0.25
            rows.append(f'<tr><td class="k">MM 空</td><td class="dim">扫到 <span class="num">{fd(sw, dd)}</span>'
                        f' 后回落到 {fd(s0["hi"], dd)} 下方</td></tr>')
            mm.append(f'<div class="pline"><span class="dn">空</span> 扫到 <span class="num">{fd(sw, dd)}</span>'
                      f' → 回落 <span class="num">{fd(s0["hi"], dd)}</span> 下方<br>'
                      f'<span class="dim2">目标 {fd(s0["tp1"], dd)}</span></div>')
        if b0:
            dist = (b0["hi"] / px - 1) * 100
            rows.append(f'<tr><td class="k">GG 多</td><td><span class="up num">{fd(b0["hi"], dd)}</span> '
                        f'({dist:+.2f}%) <span class="w">权重 {b0["score"]:.1f}</span><br>'
                        f'<span class="dim">止 {fd(b0["stop"], dd)} · 目标 {fd(b0["tp1"], dd)}/{fd(b0["tp2"], dd)}</span><br>'
                        f'<span class="w">{_h.escape(" + ".join(b0["reasons"][:3]))}</span></td></tr>')
            gg.append(f'<div class="pline"><span class="up">多</span> <span class="num">{fd(b0["hi"], dd)}</span>'
                      f'<span class="dim">（{dist:+.2f}%）</span> <span class="w">权重{b0["score"]:.1f}</span><br>'
                      f'<span class="dim2">止 {fd(b0["stop"], dd)} · 目标 {fd(b0["tp1"], dd)}/{fd(b0["tp2"], dd)}</span></div>')
            sw = b0["lo"] - atr * 0.25
            rows.append(f'<tr><td class="k">MM 多</td><td class="dim">扫到 <span class="num">{fd(sw, dd)}</span>'
                        f' 后收回 {fd(b0["lo"], dd)} 上方</td></tr>')
            mm.append(f'<div class="pline"><span class="up">多</span> 扫到 <span class="num">{fd(sw, dd)}</span>'
                      f' → 收回 <span class="num">{fd(b0["lo"], dd)}</span> 上方<br>'
                      f'<span class="dim2">目标 {fd(b0["tp1"], dd)}</span></div>')
        cards.append(f'<div class="card"><div class="row"><div class="coin">{c["coin"]}</div>'
                     f'<div class="px num">{fd(px, dd)}</div></div>'
                     f'<div class="row" style="margin-top:6px"><div class="big {cls}">{c["bias"]} {tot:+d}</div>'
                     f'<div class="tag">{c.get("phase", "")}</div></div>'
                     f'<div class="bar"><i style="width:50%"></i></div>'
                     f'<table>{"".join(rows)}</table></div>')
        panel.append(f'<div class="pblock"><div class="pcoin">{c["coin"]}<span class="dim"> {fd(px, dd)}</span></div>'
                     f'<div class="plabel gg">GG · 到点挂单</div>{"".join(gg) or "<div class=pline><span class=dim>无</span></div>"}'
                     f'<div class="plabel mm">MM · 扫损确认</div>{"".join(mm) or "<div class=pline><span class=dim>无</span></div>"}'
                     f'<div class="pmsep"></div></div>')

    # 四小时双向点位
    h4rows = []
    for c in coins:
        px = c["px"]
        dd = 0 if px >= 10000 else (1 if px >= 1000 else 2)
        z = c.get("zones") or {}
        s0 = (z.get("sell") or [None])[0]
        b0 = (z.get("buy") or [None])[0]
        s_txt = (f'<span class="dn num">{fd(s0["lo"], dd)}</span>'
                 f'<span class="dim">（{(s0["lo"]/px-1)*100:+.2f}%）</span><br>'
                 f'<span class="dim2">{_h.escape(" + ".join(s0["reasons"][:3]))}</span>') if s0 else '<span class="dim">—</span>'
        b_txt = (f'<span class="up num">{fd(b0["hi"], dd)}</span>'
                 f'<span class="dim">（{(b0["hi"]/px-1)*100:+.2f}%）</span><br>'
                 f'<span class="dim2">{_h.escape(" + ".join(b0["reasons"][:3]))}</span>') if b0 else '<span class="dim">—</span>'
        h4rows.append(f'<tr><td><b>{c["coin"]}</b><br><span class="dim num">{fd(px, dd)}</span></td>'
                      f'<td>{s_txt}</td><td>{b_txt}</td></tr>')
    h4 = ('<div class="h4wrap"><h2>DEEP-四小时双向点位</h2>'
          '<table class="rank-t"><tr><th>币种 / 现价</th><th>空（阻力挂空）</th><th>多（支撑挂多）</th></tr>'
          f'<tbody>{"".join(h4rows)}</tbody></table>'
          '<p class="note">四小时级别的双向挂单位：价格到「空」做空、到「多」做多。</p></div>')

    # 优先操作排序
    opps = []
    for c in coins:
        px = c["px"]; dd = 0 if px >= 10000 else (1 if px >= 1000 else 2)
        z = c.get("zones") or {}
        for side, key, zk in (("空", "sell", None), ("多", "buy", None)):
            zz = (z.get(key) or [None])[0]
            if not zz:
                continue
            entry = zz["lo"] if side == "空" else zz["hi"]
            risk = abs(entry - zz["stop"])
            if risk <= 0:
                continue
            rr = abs(zz["tp1"] - entry) / risk
            dist = (entry / px - 1) * 100
            score = zz["score"] * min(rr, 3.0) / (1 + abs(dist) / 2.0)
            opps.append((score, c["coin"], side, entry, zz["stop"], zz["tp1"], rr, dist, zz["score"], dd))
    opps.sort(reverse=True)
    orows = []
    for i, (sc, coin, side, e, st, tp, rr, dist, w, dd) in enumerate(opps[:8], 1):
        rr_cls = "up" if rr >= 1.5 else ("dim" if rr >= 1.0 else "dn")
        d_cls = "up" if abs(dist) <= 1 else "dim"
        orows.append(f'<tr><td class="dim">{i}</td><td><b>{coin}</b></td>'
                     f'<td class="{"dn" if side=="空" else "up"}">{side}</td>'
                     f'<td class="num">{fd(e, dd)}</td><td class="num dim">{fd(st, dd)}</td>'
                     f'<td class="num">{fd(tp, dd)}</td><td class="num {rr_cls}">{rr:.2f}</td>'
                     f'<td class="num {d_cls}">{dist:+.2f}%</td><td class="num dim">{w:.1f}</td>'
                     f'<td class="num acc">{sc:.1f}</td></tr>')
    rank = ('<section class="rank-wrap"><h2>优先操作排序（权重 × 盈亏比 ÷ 距离惩罚）</h2>'
            '<table class="rank-t"><tr><th>#</th><th>币种</th><th>方向</th><th>入场</th><th>止损</th>'
            '<th>目标1</th><th>盈亏比</th><th>距现价</th><th>权重</th><th>评分</th></tr><tbody>'
            + "".join(orows) + '</tbody></table>'
            '<p class="note">评分 = 权重 × min(盈亏比,3) ÷ (1 + |距现价%|/2)。</p></section>')

    # 关注博主
    blog = ""
    try:
        def _find(name):
            for c0 in (DATA, BASE, os.path.join(BASE, ".."), os.getcwd(),
                       os.path.join(os.getcwd(), ".."), "/home/runner/work/okx-monitor/okx-monitor"):
                f0 = os.path.abspath(os.path.join(c0, name))
                if os.path.exists(f0):
                    return f0
            return None
        _bjp = _find("blogger.json")
        _bj = _json.load(open(_bjp, encoding="utf-8")) if _bjp else None
        sp = _find("okx_state.json") or _find("state.json")
        ap = _find("alerts.jsonl")
        st = _json.load(open(sp, encoding="utf-8")) if sp else {}
        if _bj:
            st = {"updated": _bj.get("updated"),
                  "traders": {t.get("uid"): {"nickname": t.get("nick"), "positions": t.get("positions") or []}
                              for t in (_bj.get("traders") or [])}}
        traders = st.get("traders") or {}
        closed = []
        if _bj and _bj.get("closed"):
            for _r in _bj["closed"]:
                closed.append({"ts": _r.get("ts"), "type": "平仓",
                               "text": f"博主（{_r.get('nick')}）平{_r.get('side')}\n"
                                       f"{_r.get('coin')} ·\n"
                                       f"浮盈 {_r.get('upl') or 0}"})
        if not closed and ap and os.path.exists(ap):
            for line in io.open(ap, encoding="utf-8", errors="replace").read().splitlines()[-400:]:
                try:
                    j = _json.loads(line)
                except Exception:
                    continue
                if "平" in str(j.get("type", "")) or "平" in str(j.get("text", ""))[:40]:
                    closed.append(j)
        cmap = {}
        for r in closed:
            t = str(r.get("text") or "")
            m1 = re.search(r"博主（(.+?)）", t)
            m2 = re.search(r"\n([A-Z]{2,6})\s*·", t)
            m3 = re.search(r"(平多|平空|开多|开空)", t)
            m4 = re.search(r"浮盈\s*([+-]?[\d,\.]+)", t)
            if not m1:
                continue
            pnl = None
            if m4:
                try:
                    pnl = float(m4.group(1).replace(",", ""))
                except Exception:
                    pnl = None
            sd = m3.group(1) if m3 else ""
            cmap.setdefault(m1.group(1), []).append(
                {"ts": str(r.get("ts", "")), "coin": m2.group(1) if m2 else "",
                 "side": "空" if "空" in sd else ("多" if "多" in sd else ""), "pnl": pnl})
        blocks = []
        for uid, t in traders.items():
            nick = t.get("nickname") or uid
            pos = t.get("positions") or []
            seg = [f'<div class="btrader">{_h.escape(nick)}<span class="dim"> · '
                   + (f"持仓 {len(pos)} 笔" if pos else "空仓") + '</span></div>']
            if pos:
                trs = []
                for x in pos:
                    inst = str(x.get("instId", "")).replace("-USDT-SWAP", "")
                    sd = "空" if str(x.get("posSide", "")).lower() == "short" else "多"
                    try:
                        upnl = float(x.get("upl") or 0); cost = float(x.get("avgPx") or 0); last = float(x.get("last") or 0)
                    except Exception:
                        upnl = cost = last = 0.0
                    cl = "up" if upnl > 0 else ("dn" if upnl < 0 else "dim")
                    trs.append(f'<tr><td><b>{_h.escape(inst)}</b></td>'
                               f'<td class="{"dn" if sd=="空" else "up"}">{sd}</td>'
                               f'<td class="num dim">{x.get("lever","-")}x</td><td class="num">{format(x.get("coinQty") or x.get("pos") or 0, "g")}</td>'
                               f'<td class="num">{cost:,.2f}</td><td class="num">{last:,.2f}</td>'
                               f'<td class="num {cl}">{upnl:+,.0f}</td></tr>')
                seg.append('<table class="rank-t"><tr><th>币种</th><th>方向</th><th>杠杆</th><th>数量(币)</th>'
                           '<th>成本</th><th>现价</th><th>浮盈USDT</th></tr><tbody>' + "".join(trs) + '</tbody></table>')
            else:
                seg.append('<div class="bempty">当前无持仓</div>')
            recs = sorted(cmap.get(nick) or [], key=lambda r0: str(r0.get("ts") or ""), reverse=True)
            if recs:
                _n = len(recs)
                _w = sum(1 for r0 in recs if (float(r0.get("pnl") or 0) if str(r0.get("pnl") or 0).replace("-","").replace(".","").isdigit() else 0) > 0)
                _sum = sum((float(r0.get("pnl") or 0) if str(r0.get("pnl") or 0).replace("-","").replace(".","").isdigit() else 0) for r0 in recs)
                _c2 = "up" if _sum > 0 else "dn"
                seg.append(f'<div class="bstat">平仓 {_n} 笔｜胜 {_w}（{_w/_n*100:.0f}%）｜'
                           f'合计 <span class="{_c2}">{_sum:+,.0f}</span> USDT</div>')
                hrs = []
                for r in recs[:8]:
                    def _num6(v6):
                        try:
                            return float(v6)
                        except Exception:
                            return None
                    _oa = _num6(r.get("avg")); _ca2 = _num6(r.get("close_avg"))
                    _q6 = _num6(r.get("qty")); _fe = _num6(r.get("fee")) or 0.0
                    _pv = None
                    if _oa and _ca2 and _q6:
                        _dv = 1.0 if r.get("side") == "多" else -1.0
                        _pv = _dv * (_ca2 - _oa) * _q6 - abs(_fe)
                    if _pv is None:
                        _pv = _num6(r.get("pnl")) or 0.0
                    cl = "up" if _pv > 0 else "dn"
                    pt = format(_pv, "+,.0f")
                    _dur = r.get("dur")
                    dtxt = (f"{_dur//60}h{_dur%60}m" if isinstance(_dur, int) and _dur >= 60 else
                            (f"{_dur}m" if isinstance(_dur, int) else "—"))
                    hrs.append(f'<tr><td class="dim">{_h.escape(str(r.get("open_ts","")))}</td>'
                               f'<td class="dim">{_h.escape(str(r.get("ts","")))}</td>'
                               f'<td>{_h.escape(str(r.get("coin","")))}</td>'
                               f'<td class="{"dn" if r.get("side")=="空" else "up"}">{r.get("side","")}</td>'
                               f'<td class="num">{format(r.get("qty") or 0, "g")}</td>'
                               f'<td class="num dim">{r.get("avg") if isinstance(r.get("avg"),str) else format(r.get("avg") or 0,",.2f")}</td>'
                               f'<td class="num dim">{format(r.get("close_avg") or 0, ",.2f")}</td>'
                               f'<td class="num {cl}">{pt}</td>'
                               f'<td class="num dim">{format(r.get("fee") or 0, ",.1f")}</td>'
                               f'<td class="dim">{_h.escape(str(r.get("ctype") or ""))}</td>'
                               f'<td class="dim">{dtxt}</td></tr>')
                seg.append('<div class="bsub">平仓记录</div><table class="rank-t hist-t">'
                           '<tr><th>开仓时间</th><th>平仓时间</th><th>币种</th><th>方向</th><th>数量(币)</th>'
                           '<th>开仓均价</th><th>平仓均价</th><th>净利USDT</th><th>手续费</th>'
                           '<th>类型</th><th>时长</th></tr><tbody>' + "".join(hrs) + '</tbody></table>')
            blocks.append('<div class="bblock">' + "".join(seg) + '</div>')
        if blocks:
            blog = ('<section class="rank-wrap"><h2>关注博主 · OKX 交易员持仓与平仓</h2>'
                    f'<p class="note">数据时间：{_h.escape(str(st.get("updated","")))}（来源 state.json / alerts.jsonl）</p>'
                    + "".join(blocks) + '</section>')
    except Exception:
        blog = ""

    return ('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="refresh" content="120">'
            f'<title>实时点位看板</title>{CSS}</head><body>'
            '<header><h1>实时点位看板 · GG / MM</h1>'
            f'<div class="meta">更新于 {ts} · 每 2 分钟自动刷新</div></header>'
            f'<div class="wrap"><div class="grid2">{"".join(cards[:2])}{h4}{"".join(cards[2:])}</div>'
            f'<section class="side"><h2>点位表 · GG / MM 对照</h2>{"".join(panel)}'
            '<p class="note">GG＝到价即挂；MM＝先扫过再回落才进。</p></section></div>'
            f'{rank}{blog}'
            '<footer>数据源 Gate.io USDT 永续 · 与推送同源 · 仅供参考</footer>'
            '</body></html>')



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
    # 每日点位表（09/12/16/21/00）
    for e in check_table(coins, state, cfg, time.time()):
        events.append(e)
    for e in check_eth_short_ready(coins, state, cfg, time.time()):
        events.append(e)
    for e in check_long_ready(coins, state, cfg, time.time()):
        events.append(e)

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
    api_pos = fetch_my_positions()
    if api_pos is not None:
        # 数量/成本来自 OKX 真实持仓，止盈计划仍来自 POSITIONS_JSON
        positions = merge_positions(api_pos, {"positions": load_positions(cfg)})
        # 没有手动计划的持仓 → 按 ATR+箱体自动生成止损止盈
        _cmap = {x["coin"]: x for x in coins}
        for p in positions:
            if (not p.get("stop") or not p.get("tps")) and p["coin"] in _cmap:
                _pl = auto_plan(_cmap[p["coin"]], p)
                p["stop"] = _pl["stop"]
                p["tps"] = _pl["tps"]
                p["tp_pcts"] = [50, 25, 25]
                p["auto"] = True
        # 持仓变化：新开仓 / 加仓 / 减仓 / 平仓
        prev_qty = state.setdefault("pos_qty", {})
        coin_map = {x["coin"]: x for x in coins}
        now_keys = set()
        for p in positions:
            k = f"{p['coin']}:{p['side']}"
            now_keys.add(k)
            old = prev_qty.get(k)
            a_c = coin_map.get(p["coin"])
            word = "空" if p["side"] == "sell" else "多"
            d3 = dec_of(p.get("entry") or 0)
            if old is None:
                # 新开仓：给完整建议
                body = (f"{word}单 {p['qty']:.3f} 个｜成本 {p['entry']:,.{d3}f}\n")
                if p.get("lever"):
                    body += f"杠杆 {p['lever']}x"
                if p.get("liqPx"):
                    body += f"｜强平 {p['liqPx']:,.{d3}f}"
                body += "\n"
                if a_c:
                    body += position_advice(a_c, p) + "\n"
                body += f"当前浮盈 {p['upl']:+,.0f} USDT"
                events.append(dict(coin=p["coin"], rule="posopen", ts=time.time(), important=True,
                                   title=f"GG-持仓（{p['coin']}）新开{word}", body=body))
            elif abs(p["qty"] - old) / max(old, 1e-9) > 0.02:
                act = "减仓" if p["qty"] < old else "加仓"
                body = (f"{word}单 {old:.3f} → {p['qty']:.3f} 个\n"
                        f"成本 {p['entry']:,.{d3}f}｜浮盈 {p['upl']:+,.0f} USDT\n"
                        f"止损仍按 {p.get('stop'):,.{d3}f}" if p.get("stop") else
                        f"{word}单 {old:.3f} → {p['qty']:.3f} 个\n成本 {p['entry']:,.{d3}f}｜浮盈 {p['upl']:+,.0f} USDT")
                events.append(dict(coin=p["coin"], rule="poschg", ts=time.time(), important=True,
                                   title=f"GG-持仓（{p['coin']}）{act}", body=body))
            prev_qty[k] = p["qty"]
        # 平仓检测
        for k in list(prev_qty.keys()):
            if k in now_keys:
                continue
            coin, side = k.split(":", 1)
            if coin in [x["coin"] for x in coins]:
                events.append(dict(coin=coin, rule="posclose", ts=time.time(), important=True,
                                   title=f"GG-持仓（{coin}）已平仓",
                                   body=f"{'空' if side == 'sell' else '多'}单已离场\n如已止盈，注意把该笔盈亏记入账本"))
            prev_qty.pop(k, None)
    else:
        positions = load_positions(cfg)
    if positions:
        for e in check_positions(positions, px_map, state, time.time()):
            events.append(e)

    # 模拟仓：自动开仓 + 止损止盈管理（双轨并行：A 稳健 / B 激进）
    try:
        # 一键重置模拟仓（config 里改 reset_paper_ts 即可，幂等）
        _rst = cfg.get("reset_paper_ts")
        if _rst and str(state.get("paper_reset_ts")) != str(_rst):
            for _k in ("paper", "paper_b", "paper_c", "paper_mm_a", "paper_mm_b",
                       "paper_mm_c", "paper_res"):
                state.pop(_k, None)
            state["paper_reset_ts"] = _rst
            log(f"paper state reset by flag {_rst}")
        paper_apply_cmds(state)
        _ts = time.time()
        for e in check_combo_signals(coins, state, cfg, _ts):
            events.append(e)
        for e in paper_manage(px_map, state, _ts, track="paper_combo", label="COMBO"):
            events.append(e)
        for e in check_us_ref(coins, state, cfg, _ts):
            events.append(e)
        # 共振短线已停用（用户要求移除）
        # MM 策略三轨（扫损确认）
        for _tk, _nm, _lab, _rs in (("paper_mm_a", 2000.0, "MM-A", 1.0),
                                    ("paper_mm_b", 6000.0, "MM-B", 3.0),
                                    ("paper_mm_c", 10000.0, "MM-C", 6.0)):
            for e in check_mm_signals(coins, state, _ts, track=_tk, notional=_nm,
                                      label=_lab, acct=10000.0, risk_scale=_rs):
                events.append(e)
            for e in paper_manage(px_map, state, _ts, track=_tk, label=_lab):
                events.append(e)
        # C 轨：本金 10,000，单笔风险约 10%（3 次连亏约 -30%）
        for e in check_paper_signals(coins, state, _ts, track="paper_c", notional=10000.0,
                                     label="C", acct=10000.0, risk_scale=2.5):
            events.append(e)
        for e in paper_manage(px_map, state, _ts, track="paper_c", label="C"):
            events.append(e)
        # A 轨：名义 $2,000（约 0.6% 风险/笔）
        for e in check_paper_signals(coins, state, _ts, track="paper", notional=2000.0, label="", acct=10000.0):
            events.append(e)
        for e in paper_manage(px_map, state, _ts, track="paper"):
            events.append(e)
        # B 轨：名义 $6,000（约 1.8% 风险/笔），独立状态、独立推送
        for e in check_paper_signals(coins, state, _ts, track="paper_b", notional=6000.0, label="B", acct=10000.0):
            events.append(e)
        for e in paper_manage(px_map, state, _ts, track="paper_b", label="B"):
            events.append(e)
    except Exception as _ex:
        log(f"paper err {_ex}")

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
    # 抓 OKX 公开持仓（带密钥签名），写缓存 + blogger.json 供两边看板使用
    try:
        if state.get("probe_ver") != PAPER_VER:
            state["probe_ver"] = PAPER_VER
            state["probe_log"] = probe_okx_history()
        _okx = fetch_okx_traders({})
        log("okx traders probe: " + ("OK" if _okx is not None else "FAIL"))
        if _okx is not None:
            _nick = {"872873784163041281": "天才铁头娃", "872898006700732416": "十一的八字",
                     "872838143249428480": "十老板", "872913887149293569": "天才亏钱娃"}
            _st = {"updated": now().strftime("%Y-%m-%d %H:%M:%S"), "traders": {}}
            if state.get("hist_ts_fix") != "v2":
                state["hist_ts_fix"] = "v2"
                state["blog_closed"] = []
                log("历史记录重建：修正平仓时间字段")
            _lastpos = state.get("okx_last") or {}
            for _aid, _rows in _okx.items():
                if _rows is None:                     # 本次抓取失败 → 用上次
                    _rows = _lastpos.get(_aid) or []
                    log(f"okx {_nick.get(_aid,_aid)} 本次失败，沿用上次 {len(_rows)} 笔")
                _st["traders"][_aid] = {"nickname": _nick.get(_aid, _aid),
                                        "positions": _rows if isinstance(_rows, list) else [],
                                        "asset": None}
            # 极端情况：接口整个失败，仍保留全部 4 位
            for _aid, _nk3 in _nick.items():
                if _aid not in _st["traders"]:
                    _st["traders"][_aid] = {"nickname": _nk3,
                                            "positions": (_lastpos.get(_aid) or []),
                                            "asset": None}
            for _aid, _v3 in _st["traders"].items():
                if _v3.get("positions"):
                    _lastpos[_aid] = _v3["positions"]
            state["okx_last"] = _lastpos
            for _cand in (os.path.join(DATA, "okx_state.json"),
                          os.path.join(os.getcwd(), "state.json")):
                try:
                    with open(_cand, "w", encoding="utf-8") as _f:
                        json.dump(_st, _f, ensure_ascii=False, indent=1)
                    log("okx state written -> " + _cand)
                    break
                except Exception:
                    continue
            # 拉取 OKX 官方历史平仓，合并去重
            try:
                _merged = {(_x.get("nick"), _x.get("coin"), _x.get("ts")): _x
                           for _x in (state.get("blog_closed") or [])}
                _sample = None
                for _aid2, _nk2 in _nick.items():
                    for _h in fetch_okx_history(_aid2, _nk2):
                        if _sample is None:
                            _sample = _h.get("_raw")
                        _h.pop("_raw", None)
                        _merged[(_h["nick"], _h["coin"], _h["ts"])] = _h
                _lst = [z for z in _merged.values()
                        if z.get("close_avg") or z.get("ctype") or z.get("fee") is not None]
                _lst = sorted(_lst, key=lambda z: str(z.get("ts") or ""))
                state["blog_closed"] = _lst[-1200:]
                state["hist_sample"] = _sample
                log(f"okx history merged {len(_lst)}")
            except Exception as _e4:
                log(f"okx history merge error {type(_e4).__name__}: {_e4}")
            # 首次：从 alerts.jsonl 播种历史平仓记录
            if not state.get("blog_closed"):
                _seed = []
                for _c3 in (os.path.join(BASE, "..", "alerts.jsonl"), os.path.join(os.getcwd(), "alerts.jsonl")):
                    if not os.path.exists(_c3):
                        continue
                    try:
                        with open(_c3, encoding="utf-8", errors="replace") as _f3:
                            for _ln in _f3.read().splitlines()[-5000:]:
                                _ln = _ln.strip()
                                if not _ln or "平仓" not in _ln:
                                    continue
                                try:
                                    _j3 = json.loads(_ln)
                                except Exception:
                                    continue
                                _t3 = str(_j3.get("text") or "")
                                _mn = re.search(r"博主（(.+?)）", _t3)
                                _mc = re.search(r"\n([A-Z]{2,6})\s*·", _t3)
                                _ms = re.search(r"(平多|平空)", _t3)
                                _mp = re.search(r"浮盈\s*([+-]?[\d,\.]+)", _t3)
                                if not _mn:
                                    continue
                                try:
                                    _pv = float(_mp.group(1).replace(",", "")) if _mp else 0.0
                                except Exception:
                                    _pv = 0.0
                                _seed.append({"ts": _j3.get("ts"),
                                              "nick": _mn.group(1),
                                              "coin": _mc.group(1) if _mc else "",
                                              "side": ("空" if "空" in (_ms.group(1) if _ms else "") else
                                                       ("多" if "多" in (_ms.group(1) if _ms else "") else "")),
                                              "upl": _pv})
                        break
                    except Exception:
                        continue
                if _seed:
                    state["blog_closed"] = _seed[-1200:]
                    log(f"blog_closed seeded {len(_seed)}")
            _bj = {"updated": _st["updated"],
                   "probe": state.get("probe_log") or [],
                   "hist_sample": state.get("hist_sample"),
                   "traders": [{"uid": k0, "nick": v0["nickname"], "positions": v0["positions"]}
                               for k0, v0 in _st["traders"].items()],
                   "closed": state.get("blog_closed") or []}
            for _c2 in (os.path.join(BASE, "..", "blogger.json"), os.path.join(os.getcwd(), "blogger.json")):
                try:
                    with open(_c2, "w", encoding="utf-8") as _f2:
                        json.dump(_bj, _f2, ensure_ascii=False, indent=1)
                    log("blogger.json written -> " + _c2)
                    break
                except Exception:
                    continue
        # ---- 与上一轮快照比对，推送开/平/加/减仓 ----
        try:
            _prev = state.get("okx_prev") or {}
            _cur = {}
            for _aid, _rows in _okx.items():
                for _r in (_rows or []):
                    _inst = str(_r.get("instId", "")).replace("-USDT-SWAP", "")
                    _side = "空" if str(_r.get("posSide", "")).lower() == "short" else "多"
                    try:
                        _qty = abs(float(_r.get("pos") or 0))
                    except Exception:
                        _qty = 0.0
                    _cur[f"{_aid}|{_inst}|{_side}"] = {
                        "qty": _qty, "coinQty": _r.get("coinQty"), "avg": _r.get("avgPx"),
                        "lever": _r.get("lever"), "upl": _r.get("upl"),
                        "nick": _nick.get(_aid, _aid)}
            if _prev:
                def _fmt_q(v):
                    try:
                        return f"{float(v):,.4g}"
                    except Exception:
                        return "—"

                def _hist_of(nick, coin, side):
                    _hs = [h for h in (state.get("blog_closed") or [])
                           if h.get("nick") == nick and h.get("coin") == coin and h.get("side") == side]
                    return max(_hs, key=lambda z: str(z.get("ts") or "")) if _hs else None

                for _k, _v in _cur.items():
                    _p = _prev.get(_k)
                    _nick = _v["nick"]; _inst = _k.split("|")[1]; _side = _k.split("|")[2]
                    _avg = float(_v.get("avg") or 0)
                    _d = 0 if _avg >= 10000 else 2
                    _cq = float(_v.get("coinQty") or 0)
                    _lev = _v.get("lever") or "-"
                    if _p is None:
                        body = (f"{_inst} · 开{_side}\n"
                                f"数量：{_fmt_q(_cq)} 个\n"
                                f"价格：{_avg:,.{_d}f}\n"
                                f"杠杆：{_lev}x")
                        push(cfg, f"DEEP-博主（{_nick}）", body, coin=_inst, important=True, dry=dry)
                        continue
                    _pq = float(_p.get("coinQty") or 0)
                    _diff = _cq - _pq
                    if _diff > 1e-9:
                        body = (f"{_inst} · 加{_side}\n"
                                f"加仓数量：{_fmt_q(_diff)} 个\n"
                                f"加仓价格：{_avg:,.{_d}f}\n"
                                f"加仓后均价：{_avg:,.{_d}f}\n"
                                f"加仓总数：{_fmt_q(_cq)} 个")
                        push(cfg, f"DEEP-博主（{_nick}）", body, coin=_inst, important=False, dry=dry)
                    elif _diff < -1e-9:
                        body = (f"{_inst} · 减{_side}\n"
                                f"减仓数量：{_fmt_q(abs(_diff))} 个\n"
                                f"减仓价格：{_avg:,.{_d}f}\n"
                                f"减仓后总数：{_fmt_q(_cq)} 个")
                        push(cfg, f"DEEP-博主（{_nick}）", body, coin=_inst, important=False, dry=dry)
                for _k, _p in _prev.items():
                    if _k in _cur or not _p.get("qty"):
                        continue
                    _nick = _p.get("nick", "?"); _inst = _k.split("|")[1]; _side = _k.split("|")[2]
                    _pq = float(_p.get("coinQty") or _p.get("qty") or 0)
                    _pavg = float(_p.get("avg") or 0)
                    _d = 0 if _pavg >= 10000 else 2
                    _h = _hist_of(_nick, _inst, _side)
                    _ct2 = str((_h or {}).get("ctype") or "")
                    _tot2 = float((_h or {}).get("total_qty") or 0)
                    _allc = (_ct2 == "allClose") or (_tot2 and _pq >= _tot2 * 0.98)
                    _warn = ""
                    if _allc:
                        _head = f"{_inst} · 平{_side}（全部平仓）"
                        _qline = (f"本次平仓：{_fmt_q(_pq)} 个" +
                                  (f"（原持仓 {_fmt_q(_tot2)} 个）" if _tot2 else ""))
                    else:
                        _head = f"{_inst} · 平{_side}（部分平仓）"
                        _qline = (f"本次平仓：{_fmt_q(_pq)} 个" +
                                  (f"（原持仓 {_fmt_q(_tot2)} 个，剩余 {_fmt_q(max(0.0,_tot2-_pq))} 个）" if _tot2 else ""))
                    lines = [_head, _qline]
                    if _h:
                        _ca = float(_h.get("close_avg") or 0)
                        _pnl = float(_h.get("upl") or 0)
                        _fee = float(_h.get("fee") or 0)
                        _dur = _h.get("dur")
                        lines.append(f"平仓价：{_ca:,.{_d}f}" if _ca else "平仓价：—")
                        lines.append(f"开仓均价：{_pavg:,.{_d}f}")
                        # 本次平仓的净利：价差 × 本次数量 − 手续费
                        _dir = 1.0 if _side == "多" else -1.0
                        _est = _dir * (_ca - _pavg) * _pq - abs(_fee) if _ca else None
                        if _est is not None:
                            lines.append(f"本次净利：{_est:+,.0f} USDT")
                            if abs(_pnl - _est) > max(80.0, abs(_est) * 0.25):
                                lines.append(f"（该仓位累计已实现 {_pnl:+,.0f}，含前期分批平仓）")
                        else:
                            lines.append(f"净利：{_pnl:+,.0f} USDT")
                        lines.append(f"手续费：{_fee:,.1f}")
                        if _h.get("open_ts"):
                            lines.append(f"开仓时间：{_h['open_ts']}")
                        if isinstance(_dur, int):
                            lines.append(f"持仓时长：{(str(_dur//60)+'h'+str(_dur%60)+'m') if _dur>=60 else (str(_dur)+'m')}")
                    else:
                        lines.append(f"开仓均价：{_pavg:,.{_d}f}")
                    # 只推送，不写历史（历史以 OKX 官方 position-history 为准，避免出现"无明细"的假记录）
                    push(cfg, f"DEEP-博主（{_nick}）", "\n".join(lines), coin=_inst, important=True, dry=dry)
            state["okx_prev"] = _cur
        except Exception as _e2:
            log(f"okx push error {type(_e2).__name__}: {_e2}")
    except Exception as _e:
        log(f"okx probe error {type(_e).__name__}: {_e}")

    html = render(dash, cfg)
    with open(DASH_OUT, "w", encoding="utf-8") as f:
        f.write(html)

    # 发布节流：至少间隔 PUBLISH_GAP 秒才允许 workflow 提交看板
    PUBLISH_GAP = int(os.environ.get("PUBLISH_GAP", "180"))
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
    for _tk, _lab in (("paper", "A"), ("paper_b", "B"), ("paper_c", "C"),
                      ("paper_mm_a", "MM-A"), ("paper_mm_b", "MM-B"), ("paper_mm_c", "MM-C"),
                      ("paper_combo", "COMBO")):
        _pp = state.get(_tk) or {}
        _op = {k: v for k, v in _pp.items() if v.get("status") == "open"}
        if _pp:
            _tot = sum(v.get("pnl") or 0 for v in _pp.values())
            # 按版本分组统计，方便对比改动前后
            _v1 = [v for v in _pp.values() if v.get("ver") != PAPER_VER]
            _v2 = [v for v in _pp.values() if v.get("ver") == PAPER_VER]
            def _s(vs):
                return (f"n={len(vs)} pnl={sum(v.get('pnl') or 0 for v in vs):+.2f}"
                        f" win={sum(1 for v in vs if (v.get('pnl') or 0) > 0)}")
            _txt = " ".join(f"{k}[{v.get('status')},in={v.get('entry')},pnl={v.get('pnl')},c6={v.get('chg6',0):+.2f},rs={v.get('reason','')}]"
                            for k, v in _pp.items())
            log(f"paper{_lab} total={len(_pp)} open={len(_op)} sum_pnl={_tot:+.2f}"
                f" | 旧版 {_s(_v1)} | 新版 {_s(_v2)} :: {_txt}")
        else:
            log(f"paper{_lab} total=0 open=0")

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
        time.sleep(max(2, cfg["interval_sec"] - (time.time() - t0)))

if __name__ == "__main__":
    main()




















































