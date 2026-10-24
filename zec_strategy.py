# -*- coding: utf-8 -*-
"""
ZEC 回踩确认策略信号（15分钟线）
思路：不追单。前低/前高确立 → 反向运动确认 → 回踩不破前低/前高 → 重新顺向时给信号。
与博主监控共用推送通道（Bark + Server酱）与操作记录（alerts.jsonl）。
"""

import datetime
import json
import os
import urllib.request

from okx_monitor_cloud import push, append_alerts, log, ALERTS_FILE

# ================= 策略参数（可调） =================
INST_ID = "ZEC-USDT-SWAP"
BAR = "15m"
LOOKBACK = 20        # 前低/前高搜索窗口：近20根15分钟K线（约5小时）
BOUNCE_PCT = 0.008   # 反弹/回落确认幅度：0.8%
PULLBACK_ZONE = 0.015  # 回踩须落在此区间内（前低上方1.5%以内 / 前高下方1.5%以内）
TRIG_PCT = 0.003     # 触发幅度：从回踩点再顺向运动0.3%
# ===================================================

BASE_URL = "https://www.okx.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zec_state.json")


def http_get_json(path):
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def get_candles():
    """返回按时间从旧到新排列的K线列表 [{ts,o,h,l,c,confirm}]"""
    data = http_get_json(f"/api/v5/market/candles?instId={INST_ID}&bar={BAR}&limit=100")
    if str(data.get("code")) != "0":
        raise RuntimeError(f"candles API error: {data}")
    rows = data.get("data") or []
    out = []
    for r in reversed(rows):  # 接口默认最新在前 → 反转为旧→新
        out.append({"ts": r[0], "o": float(r[1]), "h": float(r[2]),
                    "l": float(r[3]), "c": float(r[4]), "confirm": r[8] if len(r) > 8 else "1"})
    return out


def fnum(x, n=2):
    return f"{x:.{n}f}"


def detect_long(window, cur_price):
    """做多：前低L → 反弹≥BOUNCE → 回踩PL不破L且贴近L → 现价再涨TRIG"""
    lows = [b["l"] for b in window]
    iL = lows.index(min(lows))
    L = lows[iL]
    after = window[iL + 1:]
    if not after:
        return None, f"窗口内最低点在最新一根，等待反弹确认（L={fnum(L)}）"
    highs = [b["h"] for b in after]
    iP = highs.index(max(highs))
    H1 = highs[iP]
    if (H1 - L) / L < BOUNCE_PCT:
        return None, f"已有前低 L={fnum(L)}，反弹幅度不足（最高 {fnum(H1)}），等待企稳，不追空不开多"
    post_peak = after[iP + 1:]
    if not post_peak:
        return None, f"前低 L={fnum(L)} 后反弹至 {fnum(H1)}（+{(H1-L)/L*100:.2f}%），正在反弹中——按纪律不追多，等回踩"
    pl = min(b["l"] for b in post_peak)
    if pl <= L:
        return None, f"回踩已跌破前低 L={fnum(L)}（回踩低点 {fnum(pl)}），做多逻辑失效，观望"
    if pl > L * (1 + PULLBACK_ZONE):
        return None, f"回踩过浅（低点 {fnum(pl)}，距前低超过1.5%），未构成有效回踩，继续观望"
    if cur_price >= pl * (1 + TRIG_PCT):
        return ("做多", f"🟢 ZEC 策略信号[开多参考]：15分钟线前低 {fnum(L)} 企稳反弹后回踩 {fnum(pl)} 未破前低，"
                       f"现价 {fnum(cur_price)} 重新上攻 → 可考虑开多。"
                       f"参考入场 {fnum(cur_price)}｜止损 {fnum(L*0.998)} 下方｜第一目标 {fnum(H1)}（前反弹高点）")
    return None, f"回踩确认中：前低 {fnum(L)}，回踩低点 {fnum(pl)} 未破前低，等待重新上攻（现价 {fnum(cur_price)}）"


def detect_short(window, cur_price):
    """做空：前高H → 回落≥BOUNCE → 回抽PH不破H且贴近H → 现价再跌TRIG"""
    highs = [b["h"] for b in window]
    iH = highs.index(max(highs))
    H = highs[iH]
    after = window[iH + 1:]
    if not after:
        return None, f"窗口内最高点在最新一根，等待回落确认（H={fnum(H)}）"
    lows = [b["l"] for b in after]
    iT = lows.index(min(lows))
    T1 = lows[iT]
    if (H - T1) / H < BOUNCE_PCT:
        return None, f"已有前高 H={fnum(H)}，回落幅度不足（最低 {fnum(T1)}），不追多不追空"
    post_trough = after[iT + 1:]
    if not post_trough:
        return None, f"前高 H={fnum(H)} 后回落至 {fnum(T1)}（-{(H-T1)/H*100:.2f}%），正在下跌中——按纪律不追空，等回抽"
    ph = max(b["h"] for b in post_trough)
    if ph >= H:
        return None, f"回抽已突破前高 H={fnum(H)}（回抽高点 {fnum(ph)}），做空逻辑失效，观望"
    if ph < H * (1 - PULLBACK_ZONE):
        return None, f"回抽过浅（高点 {fnum(ph)}，距前高超过1.5%），未构成有效回抽，继续观望"
    if cur_price <= ph * (1 - TRIG_PCT):
        return ("做空", f"🔴 ZEC 策略信号[开空参考]：15分钟线前高 {fnum(H)} 见顶回落回抽 {fnum(ph)} 未破前高，"
                       f"现价 {fnum(cur_price)} 再度回落 → 可考虑开空。"
                       f"参考入场 {fnum(cur_price)}｜止损 {fnum(H*1.002)} 上方｜第一目标 {fnum(T1)}（前回落低点）")
    return None, f"回抽确认中：前高 {fnum(H)}，回抽高点 {fnum(ph)} 未破前高，等待再度回落（现价 {fnum(cur_price)}）"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    state["updated"] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def main():
    candles = get_candles()
    confirmed = [c for c in candles if c["confirm"] == "1"]
    if len(confirmed) < LOOKBACK + 2:
        log(f"K线不足（{len(confirmed)}根），跳过")
        return
    window = confirmed[-LOOKBACK:]
    live = candles[-1]  # 最新一根（可能未收盘），用其收盘价当现价
    cur_price = live["c"]

    state = load_state()
    fired = state.get("fired", {})  # {"long": "1162.00", "short": "1184.98"}
    new_alerts = []

    sig_l, msg_l = detect_long(window, cur_price)
    log("[做多检测] " + msg_l)
    sig_s, msg_s = detect_short(window, cur_price)
    log("[做空检测] " + msg_s)

    # 互斥过滤：多空同时满足 = 震荡矛盾区（V型/宽幅扫荡），按纪律均不推送
    if sig_l and sig_s:
        log("⚠ 多空信号同时出现，判定为震荡矛盾区，均不推送（符合'方向矛盾不动手'纪律）")
        sig_l, sig_s = None, None

    if sig_l:
        key = f"{min(b['l'] for b in window):.2f}"
        if fired.get("long") != key:
            new_alerts.append(("做多信号", msg_l))
            fired["long"] = key
        else:
            log("做多信号已推送过（同一前低不重复推送）")

    if sig_s:
        key = f"{max(b['h'] for b in window):.2f}"
        if fired.get("short") != key:
            new_alerts.append(("做空信号", msg_s))
            fired["short"] = key
        else:
            log("做空信号已推送过（同一前高不重复推送）")

    if new_alerts:
        for kind, text in new_alerts:
            push(f"ZEC策略·{kind}", text)
            log(f"[{kind}] {text}")
        append_alerts(new_alerts)  # 记入看板操作记录

    state["fired"] = fired
    save_state(state)
    log("本轮ZEC策略检测完成")


if __name__ == "__main__":
    main()
