# -*- coding: utf-8 -*-
"""
ETH 波段计划到价提醒（2026-10-24 作战计划，计划起始 15:06）
- 计划一 开多(轻仓10%)：计划后回踩2355-2372且未破2351.6，站上2388 → 买入2388.00 止损2348.00 目标2405/2418/2470
- 计划二A 开空(轻仓10%)：计划后反弹触及2405-2420，收盘跌回2400下方 → 空2400.00 止损2432.00 目标2375/2360/2340
- 计划二B 开空(中仓20%)：计划后反弹触及2480-2515，回落至2475下方 → 空2475.00 止损2540.00 目标2400/2360/2330
- 联动：BTC收盘跌破80400 → 多头计划作废提醒
- 失效：ETH收盘破2318 做多全废；收盘站上2540 空头全废
- 互斥：一个方向信号激活期间（未止损/未到目标），抑制反向信号
"""

import datetime
import json
import os
import urllib.request

from okx_monitor_cloud import push, append_alerts, log

PUSH_ENABLED = os.environ.get("ETH_PUSH_ENABLED", "1") == "1"
ETH_SYM = "ETHUSDT"
BTC_SYM = "BTCUSDT"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eth_swing_state.json")

# ---- 计划参数 ----
LONG_ZONE_HI = 2372.0
LONG_INVALID = 2351.6
LONG_BROKEN = 2350.0       # 15分钟收盘低于此 = 前低失效
LONG_TRIG = 2388.0
LONG_STOP = 2348.0
LONG_T1 = 2405.0
LONG_T3 = 2470.0

SA_ZONE = 2405.0
SA_TRIG = 2400.0
SA_STOP = 2432.0
SA_T = 2340.0

SB_ZONE = 2480.0
SB_TRIG = 2475.0
SB_STOP = 2540.0
SB_T = 2330.0

BOX_BOTTOM = 2318.0
TREND_FLIP = 2540.0
BTC_GUARD = 80400.0


def get_ks(sym, limit=60):
    """返回 (旧→新) 的 (ts, 高, 低, 收)"""
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=15m&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        ks = json.loads(r.read().decode())
    return [(int(k[0]), float(k[2]), float(k[3]), float(k[4])) for k in reversed(ks)]


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    s["updated"] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def now_str():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%m-%d %H:%M")


def main():
    ks_eth = get_ks(ETH_SYM)
    ks_btc = get_ks(BTC_SYM, 30)
    eth_last = ks_eth[-1][3]
    btc_last = ks_btc[-1][3]
    log(f"ETH现价 {eth_last} | BTC现价 {btc_last}")

    s = load_state()
    if not s.get("plan_start"):
        s["plan_start"] = ks_eth[-1][0]  # 首次运行=计划生效时刻
        log(f"计划起始时间戳: {s['plan_start']}")
    plan_start = s["plan_start"]

    # 只统计计划发布之后的K线
    ks_plan = [k for k in ks_eth if k[0] >= plan_start]
    fired = s.get("fired", {})
    new_alerts = []

    def fire(key, kind, text):
        if not fired.get(key):
            new_alerts.append((kind, text))
            fired[key] = now_str()
            return True
        return False

    # ---------- 失效检查（最高优先级） ----------
    if ks_plan and ks_plan[-1][3] <= BOX_BOTTOM and not fired.get("invalid_long"):
        fire("invalid_long", "计划失效",
             f"⚠ ETH 收盘 {eth_last} 跌破箱底 {BOX_BOTTOM}——所有做多计划作废！\n"
             f"下一支撑 2240~2260，等企稳再议。")
    if ks_plan and ks_plan[-1][3] >= TREND_FLIP and not fired.get("invalid_short"):
        fire("invalid_short", "计划失效",
             f"⚠ ETH 收盘 {eth_last} 站上 {TREND_FLIP}——所有做空计划作废！\n"
             f"趋势转多，此后回踩只做多。")
    if btc_last <= BTC_GUARD and not fired.get("btc_guard"):
        fire("btc_guard", "联动风险",
             f"⚠ BTC 收盘 {btc_last} 跌破 {BTC_GUARD}——ETH 多头计划作废！\n"
             f"ETH 多单若持有立即执行止损 {LONG_STOP}。")

    # ---------- 互斥：信号激活期间抑制反向 ----------
    long_active = bool(fired.get("long_signal")) and not fired.get("long_done")
    short_active = bool(fired.get("shortA_signal") or fired.get("shortB_signal")) and not fired.get("short_done")

    # ---------- 计划一：开多 ----------
    if not long_active and not short_active and not fired.get("invalid_long") and not fired.get("btc_guard"):
        touched = any(l <= LONG_ZONE_HI for _, h, l, c in ks_plan) if ks_plan else False
        broken = any(c < LONG_BROKEN for _, h, l, c in ks_plan) if ks_plan else False
        if touched and not broken and eth_last >= LONG_TRIG:
            fire("long_signal", "开多信号",
                 f"🟢 ETH 波段操作信号\n"
                 f"日线大方向：空\n4H/1H方向：逆势·开多（回踩确认）\n"
                 f"———————\n"
                 f"🟢 买入价格：{LONG_TRIG:.2f}\n"
                 f"🛑 止损价格：{LONG_STOP:.2f}\n"
                 f"🎯 目标一：{LONG_T1:.2f}\n🎯 目标二：2418.00\n🎯 目标三：{LONG_T3:.2f}\n"
                 f"———————\n"
                 f"💼 仓位：轻仓10%（逆势试探）｜站上2420.00加至中仓20%｜日线偏空不建议重仓\n"
                 f"⚠ BTC跌破{BTC_GUARD:.2f}本信号作废\n🕐 {now_str()}")

    # ---------- 计划二A/B：开空 ----------
    if not long_active and not short_active and not fired.get("invalid_short"):
        touched_A = any(h >= SA_ZONE for _, h, l, c in ks_plan) if ks_plan else False
        touched_B = any(h >= SB_ZONE for _, h, l, c in ks_plan) if ks_plan else False
        if touched_A and eth_last <= SA_TRIG:
            fire("shortA_signal", "开空信号",
                 f"🔴 ETH 波段操作信号\n"
                 f"日线大方向：空\n4H/1H方向：顺势·开空（反弹滞涨）\n"
                 f"———————\n"
                 f"🔴 开空价格：{SA_TRIG:.2f}\n"
                 f"🛑 止损价格：{SA_STOP:.2f}\n"
                 f"🎯 目标一：2375.00\n🎯 目标二：2360.00\n🎯 目标三：{SA_T:.2f}\n"
                 f"———————\n"
                 f"💼 仓位：轻仓10%（近端空间小）\n"
                 f"⚠ ETH收盘站上{TREND_FLIP:.2f}本信号作废\n🕐 {now_str()}")
        elif touched_B and eth_last <= SB_TRIG:
            fire("shortB_signal", "开空信号",
                 f"🔴 ETH 波段操作信号\n"
                 f"日线大方向：空\n4H/1H方向：顺势·开空（反弹滞涨）\n"
                 f"———————\n"
                 f"🔴 开空价格：{SB_TRIG:.2f}\n"
                 f"🛑 止损价格：{SB_STOP:.2f}\n"
                 f"🎯 目标一：2400.00\n🎯 目标二：2360.00\n🎯 目标三：{SB_T:.2f}\n"
                 f"———————\n"
                 f"💼 仓位：中仓20%（顺势主空）｜破2318.00回抽确认可加至重仓30%\n"
                 f"⚠ ETH收盘站上{TREND_FLIP:.2f}本信号作废\n🕐 {now_str()}")

    # ---------- 激活信号的止损/止盈跟踪 ----------
    if fired.get("long_signal") and not fired.get("long_done"):
        if eth_last < LONG_STOP:
            fire("long_done", "仓位提醒", f"🛑 ETH 多单止损位 {LONG_STOP:.2f} 已触发（现价 {eth_last}），无条件离场！")
        elif eth_last >= LONG_T3:
            fire("long_done", "仓位提醒", f"🎯 ETH 多单到达最终目标 {LONG_T3:.2f}（现价 {eth_last}），止盈离场。")
    if (fired.get("shortA_signal") or fired.get("shortB_signal")) and not fired.get("short_done"):
        stop = SA_STOP if fired.get("shortA_signal") else SB_STOP
        tgt = SA_T if fired.get("shortA_signal") else SB_T
        if eth_last >= stop:
            fire("short_done", "仓位提醒", f"🛑 ETH 空单止损位 {stop:.2f} 已触发（现价 {eth_last}），无条件离场！")
        elif eth_last <= tgt:
            fire("short_done", "仓位提醒", f"🎯 ETH 空单到达最终目标 {tgt:.2f}（现价 {eth_last}），止盈离场。")

    if new_alerts:
        if PUSH_ENABLED:
            for kind, text in new_alerts:
                push(f"ETH波段·{kind}", text)
                log(f"[{kind}] {text}")
            append_alerts(new_alerts)
        else:
            for kind, text in new_alerts:
                log(f"[推送暂停·未发送] [{kind}] {text}")

    s["fired"] = fired
    save_state(s)
    log("本轮ETH波段计划检测完成")


if __name__ == "__main__":
    main()
