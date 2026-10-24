# -*- coding: utf-8 -*-
"""
ZEC 波段计划到价提醒（2026-10-24 作战计划，起始=首次运行时刻）
背景：日线空头排列（高点递减 1368→1337→1258→1185），10-24 凌晨大阴线 1236→1158 破位，
     短线 1162-1186 窄幅整理。主空顺大势，回踩博反弹仅轻仓。
- 计划一 开多(轻仓10%)：计划后回踩1145-1158未破1158.3(前低)，站上1170 → 买入1170.00 止损1155.00 目标1186/1210/1240
- 计划二A 开空(轻仓10%)：计划后反弹触及1186-1195，收盘跌回1180下方 → 空1180.00 止损1200.00 目标1158/1130/1110
- 计划二B 开空(中仓20%)：计划后反弹触及1215-1240，收盘跌回1210下方 → 空1210.00 止损1245.00 目标1185/1158/1110
- 联动：BTC收盘跌破80400 → 多头计划作废提醒
- 失效：ZEC收盘破1040 做多全废（下看1000/950）；收盘站上1245 做空全废（趋势转多）
- 互斥：一个方向信号激活期间（未止损/未到目标），抑制反向信号
"""

import datetime
import json
import os
import urllib.request

from okx_monitor_cloud import push, append_alerts, log

PUSH_ENABLED = os.environ.get("ZEC_PUSH_ENABLED", "1") == "1"
ZEC_SYM = "ZECUSDT"
BTC_SYM = "BTCUSDT"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zec_swing_state.json")

# ---- 计划参数 ----
LONG_ZONE_LO = 1145.0
LONG_INVALID = 1158.3       # 前低（10-24 凌晨 1158.36）
LONG_BROKEN = 1158.0        # 15分钟收盘低于此 = 前低失效
LONG_TRIG = 1170.0
LONG_STOP = 1155.0
LONG_T1 = 1186.0
LONG_T2 = 1210.0
LONG_T3 = 1240.0
LONG_ADD = 1190.0           # 站上加中仓参考

SA_ZONE = 1186.0            # 前高附近反弹触区
SA_TRIG = 1180.0
SA_STOP = 1200.0
SA_T = 1110.0

SB_ZONE = 1215.0            # 大阴线实体中部反弹触区
SB_TRIG = 1210.0
SB_STOP = 1245.0
SB_T = 1110.0

SC_ZONE_LO = 1158.0         # 破位回抽触区（前低1158变阻力）
SC_ZONE_HI = 1172.0
SC_TRIG = 1160.0
SC_STOP = 1176.0
SC_T = 1000.0
SC_BROKEN = 1158.0          # 15分钟收盘需先破过1158（破位前提）

BOX_BOTTOM = 1040.0         # 10-18 前低 1049 下方 = 做多全废
TREND_FLIP = 1245.0         # 站上 = 空头全废
BTC_GUARD = 80400.0


def get_ks(sym, limit=60):
    """返回 (旧→新) 的 (ts, 高, 低, 收)。Binance klines 本身按时间升序返回，无需反转"""
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=15m&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        ks = json.loads(r.read().decode())
    return [(int(k[0]), float(k[2]), float(k[3]), float(k[4])) for k in ks]


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
    ks_zec = get_ks(ZEC_SYM)
    ks_btc = get_ks(BTC_SYM, 30)
    zec_last = ks_zec[-1][3]
    btc_last = ks_btc[-1][3]
    log(f"ZEC现价 {zec_last} | BTC现价 {btc_last}")

    s = load_state()
    if not s.get("plan_start"):
        s["plan_start"] = ks_zec[-1][0]  # 首次运行=计划生效时刻
        log(f"计划起始时间戳: {s['plan_start']}")
    plan_start = s["plan_start"]

    # 只统计计划发布之后的K线
    ks_plan = [k for k in ks_zec if k[0] >= plan_start]
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
             f"⚠ ZEC 收盘 {zec_last} 跌破 {BOX_BOTTOM}——所有做多计划作废！\n"
             f"下一支撑 1000/950，等企稳再议。")
    if ks_plan and ks_plan[-1][3] >= TREND_FLIP and not fired.get("invalid_short"):
        fire("invalid_short", "计划失效",
             f"⚠ ZEC 收盘 {zec_last} 站上 {TREND_FLIP}——所有做空计划作废！\n"
             f"趋势转多，此后回踩只做多。")
    if btc_last <= BTC_GUARD and not fired.get("btc_guard"):
        fire("btc_guard", "联动风险",
             f"⚠ BTC 收盘 {btc_last} 跌破 {BTC_GUARD}——ZEC 多头计划作废！\n"
             f"ZEC 多单若持有立即执行止损 {LONG_STOP:.2f}。")

    # ---------- 互斥：信号激活期间抑制反向 ----------
    long_active = bool(fired.get("long_signal")) and not fired.get("long_done")
    short_active = bool(fired.get("shortA_signal") or fired.get("shortB_signal") or fired.get("shortC_signal")) and not fired.get("short_done")

    # ---------- 计划一：开多（逆势轻仓） ----------
    if not long_active and not short_active and not fired.get("invalid_long") and not fired.get("btc_guard"):
        touched = any(l <= LONG_INVALID for _, h, l, c in ks_plan) if ks_plan else False
        broken = any(c < LONG_BROKEN for _, h, l, c in ks_plan) if ks_plan else False
        if touched and not broken and zec_last >= LONG_TRIG:
            fire("long_signal", "开多信号",
                 f"🟢 ZEC 波段操作信号\n"
                 f"日线大方向：空\n4H/1H方向：逆势·开多（回踩确认）\n"
                 f"———————\n"
                 f"🟢 买入价格：{LONG_TRIG:.2f}\n"
                 f"🛑 止损价格：{LONG_STOP:.2f}\n"
                 f"🎯 目标一：{LONG_T1:.2f}\n🎯 目标二：{LONG_T2:.2f}\n🎯 目标三：{LONG_T3:.2f}\n"
                 f"———————\n"
                 f"💼 仓位：轻仓10%（逆势试探）｜站上{LONG_ADD:.2f}加至中仓20%｜日线偏空不建议重仓\n"
                 f"⚠ BTC跌破{BTC_GUARD:.2f}本信号作废\n🕐 {now_str()}")

    # ---------- 计划二A/B：开空（顺势） ----------
    if not long_active and not short_active and not fired.get("invalid_short"):
        touched_A = any(h >= SA_ZONE for _, h, l, c in ks_plan) if ks_plan else False
        touched_B = any(h >= SB_ZONE for _, h, l, c in ks_plan) if ks_plan else False
        if touched_A and zec_last <= SA_TRIG:
            fire("shortA_signal", "开空信号",
                 f"🔴 ZEC 波段操作信号\n"
                 f"日线大方向：空\n4H/1H方向：顺势·开空（反弹滞涨）\n"
                 f"———————\n"
                 f"🔴 开空价格：{SA_TRIG:.2f}\n"
                 f"🛑 止损价格：{SA_STOP:.2f}\n"
                 f"🎯 目标一：1158.00\n🎯 目标二：1130.00\n🎯 目标三：{SA_T:.2f}\n"
                 f"———————\n"
                 f"💼 仓位：轻仓10%（近端空间小）\n"
                 f"⚠ ZEC收盘站上{TREND_FLIP:.2f}本信号作废\n🕐 {now_str()}")
        elif touched_B and zec_last <= SB_TRIG:
            fire("shortB_signal", "开空信号",
                 f"🔴 ZEC 波段操作信号\n"
                 f"日线大方向：空\n4H/1H方向：顺势·开空（反弹滞涨）\n"
                 f"———————\n"
                 f"🔴 开空价格：{SB_TRIG:.2f}\n"
                 f"🛑 止损价格：{SB_STOP:.2f}\n"
                 f"🎯 目标一：1185.00\n🎯 目标二：1158.00\n🎯 目标三：{SB_T:.2f}\n"
                 f"———————\n"
                 f"💼 仓位：中仓20%（顺势主空）｜破1110.00回抽确认可加至重仓30%\n"
                 f"⚠ ZEC收盘站上{TREND_FLIP:.2f}本信号作废\n🕐 {now_str()}")

    # ---------- 计划二C：破位回抽确认开空 ----------
    if not long_active and not short_active and not fired.get("invalid_short"):
        touched_C = any(SC_ZONE_LO <= h <= SC_ZONE_HI for _, h, l, c in ks_plan) if ks_plan else False
        broken_C = any(c < SC_BROKEN for _, h, l, c in ks_plan) if ks_plan else False
        if touched_C and broken_C and zec_last <= SC_TRIG:
            fire("shortC_signal", "开空信号",
                 f"🔴 ZEC 破位回抽开空信号\n"
                 f"日线大方向：空\n4H/1H方向：顺势·开空（破位回抽确认）\n"
                 f"———————\n"
                 f"🔴 开空价格：{SC_TRIG:.2f}（回抽{SC_ZONE_LO:.0f}-{SC_ZONE_HI:.0f}区间均可空）\n"
                 f"🛑 止损价格：{SC_STOP:.2f}\n"
                 f"🎯 目标一：1110.00\n🎯 目标二：1060.00\n🎯 目标三：{SC_T:.2f}\n"
                 f"———————\n"
                 f"💼 仓位：轻仓10%（破位回抽确认）｜现价若已低于开空价则等反弹再空\n"
                 f"⚠ ZEC收盘站上{SC_STOP:.2f}本信号作废\n🕐 {now_str()}")

    # ---------- 激活信号的止损/止盈跟踪 ----------
    if fired.get("long_signal") and not fired.get("long_done"):
        if zec_last < LONG_STOP:
            fire("long_done", "仓位提醒", f"🛑 ZEC 多单止损位 {LONG_STOP:.2f} 已触发（现价 {zec_last}），无条件离场！")
        elif zec_last >= LONG_T3:
            fire("long_done", "仓位提醒", f"🎯 ZEC 多单到达最终目标 {LONG_T3:.2f}（现价 {zec_last}），止盈离场。")
    if (fired.get("shortA_signal") or fired.get("shortB_signal") or fired.get("shortC_signal")) and not fired.get("short_done"):
        if fired.get("shortC_signal"):
            stop, tgt = SC_STOP, SC_T
        elif fired.get("shortA_signal"):
            stop, tgt = SA_STOP, SA_T
        else:
            stop, tgt = SB_STOP, SB_T
        if zec_last >= stop:
            fire("short_done", "仓位提醒", f"🛑 ZEC 空单止损位 {stop:.2f} 已触发（现价 {zec_last}），无条件离场！")
        elif zec_last <= tgt:
            fire("short_done", "仓位提醒", f"🎯 ZEC 空单到达最终目标 {tgt:.2f}（现价 {zec_last}），止盈离场。")

    if new_alerts:
        if PUSH_ENABLED:
            for kind, text in new_alerts:
                push(f"ZEC波段·{kind}", text)
                log(f"[{kind}] {text}")
            append_alerts(new_alerts)
        else:
            for kind, text in new_alerts:
                log(f"[推送暂停·未发送] [{kind}] {text}")

    s["fired"] = fired
    save_state(s)
    log("本轮ZEC波段计划检测完成")


if __name__ == "__main__":
    main()
