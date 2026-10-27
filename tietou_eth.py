# -*- coding: utf-8 -*-
"""
天才铁头娃 · ETH「反弹做空」规则自动化监控（WORK 饼哥版）

复盘依据（其近 100 笔真实平仓）：
  1. 33 笔 ETH 空单开仓价全部贴着反弹高点/前期密集区，从不追跌 —— 这是唯一稳定 edge
  2. 盈利单平均只吃 1.05% 价格幅度即走；趋势单才持有到前低
  3. 亏损单平均 2.45% 且几乎不止损 → 我们强制加止损（盈亏比 0.39 的坑不能踩）

本程序做三件事：
  A. 每天 08:00 后推一次「日内点位预估」：开空区 / 开多区 + 预估止盈
  B. 价格进入区间 → 预警；滞涨/企稳确认 → 开仓信号（含止损止盈）
  C. 开仓后跟踪 T1/T2/T3 止盈与止损离场提醒
所有推送标题前缀：WORK（饼哥）-
"""

import datetime
import json
import os
import urllib.request

from okx_monitor_cloud import push, append_alerts, log

PUSH_ENABLED = os.environ.get("TIETOU_PUSH_ENABLED", "1") == "1"
PREFIX = "WORK（饼哥）- "
SYM = "ETHUSDT"
BTC_SYM = "BTCUSDT"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tietou_eth_state.json")

CONFIRM_PCT = 0.0035        # 回落/站回 0.35% 视为确认
TOUCH_LOOKBACK = 8          # 最近 8 根 15m 内触及过区间才算有效
COOLDOWN_MIN = 6            # 两次推送最小间隔（分钟）


def tz_now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def now_str():
    return tz_now().strftime("%m-%d %H:%M")


def today_str():
    return tz_now().strftime("%Y-%m-%d")


def r2(x):
    return round(float(x), 2)


def get_ks(sym, interval, limit):
    """返回 [(ts, open, high, low, close)]，Binance 本身时间升序"""
    url = (f"https://data-api.binance.vision/api/v3/klines"
           f"?symbol={sym}&interval={interval}&limit={limit}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        ks = json.loads(r.read().decode())
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in ks]


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    s["updated"] = tz_now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


# ---------------- 点位计算 ----------------

def build_zones(daily, price):
    """基于已完成日线计算空/多区间，并保证现价上下各有可用区间"""
    days = daily[:-1]                      # 去掉今天未完成的那根
    pdh, pdl = days[-1][2], days[-1][3]    # 昨日高/低
    hi3 = max(x[2] for x in days[-3:])
    lo3 = min(x[3] for x in days[-3:])
    hi5 = max(x[2] for x in days[-5:])
    lo5 = min(x[3] for x in days[-5:])

    short_c = [
        {"key": "S1", "lo": r2(pdh), "hi": r2(pdh * 1.004), "label": "昨日高点"},
        {"key": "S2", "lo": r2(hi3), "hi": r2(hi3 * 1.004), "label": "3日高点"},
        {"key": "S3", "lo": r2(hi5), "hi": r2(hi5 * 1.004), "label": "5日高点"},
        {"key": "S4", "lo": r2(hi5 * 1.012), "hi": r2(hi5 * 1.016), "label": "强反弹区"},
    ]
    long_c = [
        {"key": "L1", "lo": r2(pdl * 0.996), "hi": r2(pdl), "label": "昨日低点"},
        {"key": "L2", "lo": r2(lo3 * 0.996), "hi": r2(lo3), "label": "3日低点"},
        {"key": "L3", "lo": r2(lo5 * 0.996), "hi": r2(lo5), "label": "5日低点"},
        {"key": "L4", "lo": r2(lo5 * 0.982), "hi": r2(lo5 * 0.987), "label": "深回调区"},
    ]

    # 只保留现价上方的空区 / 下方的多区，去重后取最近 3 个
    shorts, seen = [], set()
    for z in sorted(short_c, key=lambda x: x["lo"]):
        if z["lo"] < price * 1.001:
            continue
        k = int(round(z["lo"] / 3))
        if k in seen:
            continue
        seen.add(k)
        shorts.append(z)
    longs, seen = [], set()
    for z in sorted(long_c, key=lambda x: -x["hi"]):
        if z["hi"] > price * 0.999:
            continue
        k = int(round(z["hi"] / 3))
        if k in seen:
            continue
        seen.add(k)
        longs.append(z)

    # 不足则用当前价按比例补足，保证任何行情都有可执行点位
    fill = 0
    while len(shorts) < 2 and fill < 4:
        fill += 1
        p = price * (1.008 + 0.012 * (fill - 1))
        if all(abs(p - z["lo"]) / p > 0.004 for z in shorts):
            shorts.append({"key": f"SF{fill}", "lo": r2(p), "hi": r2(p * 1.004), "label": "动态阻力"})
    fill = 0
    while len(longs) < 2 and fill < 4:
        fill += 1
        p = price * (0.992 - 0.012 * (fill - 1))
        if all(abs(p - z["hi"]) / p > 0.004 for z in longs):
            longs.append({"key": f"LF{fill}", "lo": r2(p * 0.996), "hi": r2(p), "label": "动态支撑"})
    shorts, longs = shorts[:3], longs[:3]

    for i, z in enumerate(shorts, 1):
        z["idx"] = i
        z["kind"] = "short"
        z.update(short_plan(z, lo3))
    for i, z in enumerate(longs, 1):
        z["idx"] = i
        z["kind"] = "long"
        z.update(long_plan(z, hi3))
    return shorts, longs, {"pdh": pdh, "pdl": pdl, "hi3": hi3, "lo3": lo3, "hi5": hi5, "lo5": lo5}


def short_plan(z, lo3):
    """空：区间下沿挂空，止损区间上沿之上；止盈 0.6% / 1.1% / 结构低点（过远则取 1.8%）"""
    e = z["lo"]
    tp3 = lo3 if (e * 0.97 <= lo3 < e * 0.989) else e * 0.982
    return {
        "entry": r2(e),
        "stop": r2(z["hi"] * 1.003),
        "tp1": r2(e * 0.994),
        "tp2": r2(e * 0.989),
        "tp3": r2(tp3),
    }


def long_plan(z, hi3):
    """多：区间上沿接多，止损区间下沿之下；止盈 0.6% / 1.1% / 上方阻力（过远则取 1.8%）"""
    e = z["hi"]
    tp3 = hi3 if (e * 1.011 < hi3 <= e * 1.03) else e * 1.018
    return {
        "entry": r2(e),
        "stop": r2(z["lo"] * 0.997),
        "tp1": r2(e * 1.006),
        "tp2": r2(e * 1.011),
        "tp3": r2(tp3),
    }


def dir_word(trend_down):
    return "空" if trend_down else "多"


def main():
    daily = get_ks(SYM, "1d", 20)
    m15 = get_ks(SYM, "15m", 60)
    btc15 = get_ks(BTC_SYM, "15m", 60)
    btc_d = get_ks(BTC_SYM, "1d", 5)

    price = m15[-1][4]
    btc_price = btc15[-1][4]
    btc_pdl = btc_d[-2][3]
    log(f"ETH现价 {price} | BTC现价 {btc_price} | BTC昨低 {btc_pdl}")

    shorts, longs, lv = build_zones(daily, price)

    # 日线方向：价格在 MA5 下方 + MA5 下穿 MA10 + 日线高点递减
    closes = [x[4] for x in daily[:-1]]
    ma5 = sum(closes[-5:]) / min(5, len(closes))
    ma10 = sum(closes[-10:]) / min(10, len(closes))
    highs = [x[2] for x in daily[:-1]]
    trend_down = price < ma5 and ma5 <= ma10 and highs[-1] < highs[-3]
    btc_weak = btc_price < btc_pdl
    max_stop_pct = max(abs(z["stop"] - z["entry"]) / z["entry"] for z in shorts + longs) * 100

    s = load_state()
    fired = s.get("fired", {})
    today = today_str()
    if s.get("fired_date") != today:
        fired = {}
        s["fired"] = {}
        s["fired_date"] = today
    new_alerts = []
    last_push_ts = s.get("last_push_ts", 0)
    now_ts = int(tz_now().timestamp())

    def cooled():
        return now_ts - last_push_ts >= COOLDOWN_MIN * 60

    def push_now(kind, text):
        new_alerts.append((kind, text))

    # ---------- A. 每日点位预估 ----------
    if s.get("forecast_date") != today and tz_now().hour >= 8:
        lines = [f"📅 {tz_now().strftime('%m-%d')} 日内点位预估（反弹做空为主）",
                 f"现价 {price:.2f}｜日线方向：{dir_word(trend_down)}",
                 "———————"]
        for z in shorts:
            lines.append(f"🔴 空{z['idx']} {z['lo']:.2f}-{z['hi']:.2f}（{z['label']}）")
            lines.append(f"    挂空 {z['entry']:.2f}｜止损 {z['stop']:.2f}｜止盈 {z['tp1']:.2f}/{z['tp2']:.2f}/{z['tp3']:.2f}")
        lines.append("———————")
        for z in longs:
            lines.append(f"🟢 多{z['idx']} {z['lo']:.2f}-{z['hi']:.2f}（{z['label']}）")
            lines.append(f"    接多 {z['entry']:.2f}｜止损 {z['stop']:.2f}｜止盈 {z['tp1']:.2f}/{z['tp2']:.2f}/{z['tp3']:.2f}")
        lines.append("———————")
        lines.append(f"⚠ 止损铁律：单笔止损幅度控制在 {max_stop_pct:.1f}% 内，不扛单、不补仓")
        lines.append(f"🕐 {now_str()}")
        push_now("点位预估", "\n".join(lines))
        s["forecast_date"] = today
        fired["forecast"] = now_str()

    # ---------- B. 区间预警 + 确认信号 ----------
    active = s.get("active")
    recent = m15[-TOUCH_LOOKBACK:]
    last = m15[-1]

    if not active:
        # 空区：先判确认（回落），再判预警（仍在区间内）
        for z in shorts:
            wkey, ckey = f"warn_short_{z['key']}", f"sig_short_{z['key']}"
            if not fired.get(ckey) and cooled():
                touched = any(k[2] >= z["lo"] for k in recent)
                if touched and price <= z["lo"] * (1 - CONFIRM_PCT):
                    push_now("开空信号",
                             f"🔴 ETH 反弹做空信号（跟铁头娃规则）\n"
                             f"日线方向：{dir_word(trend_down)}｜4H/1H：反弹滞涨确认\n"
                             f"———————\n"
                             f"🔴 开空价格：{z['entry']:.2f}\n"
                             f"🛑 止损价格：{z['stop']:.2f}\n"
                             f"🎯 目标一：{z['tp1']:.2f}\n🎯 目标二：{z['tp2']:.2f}\n🎯 目标三：{z['tp3']:.2f}\n"
                             f"———————\n"
                             f"💼 仓位：轻仓10%（首笔）｜跌破 {z['tp1']:.2f} 可加至 20%\n"
                             f"⚠ 他不止损、我们必须止：破 {z['stop']:.2f} 无条件走\n"
                             f"🕐 {now_str()}")
                    fired[ckey] = now_str()
                    s["active"] = {"dir": "short", "entry": z["entry"], "stop": z["stop"],
                                   "tp1": z["tp1"], "tp2": z["tp2"], "tp3": z["tp3"],
                                   "zone": z["key"], "opened": now_str()}
                    active = s["active"]
                    last_push_ts = now_ts
                    break
            if not fired.get(wkey) and cooled():
                if z["lo"] * 0.999 <= price <= z["hi"] * 1.001:
                    push_now("做空预警",
                             f"🎯 ETH 进入做空区 {z['lo']:.2f}-{z['hi']:.2f}（{z['label']}）\n"
                             f"现价 {price:.2f}｜上方空{z['idx']}\n"
                             f"挂空 {z['entry']:.2f}｜止损 {z['stop']:.2f}\n"
                             f"止盈 {z['tp1']:.2f} / {z['tp2']:.2f} / {z['tp3']:.2f}\n"
                             f"———————\n"
                             f"⏳ 等 15 分钟收阴回到 {z['lo'] * (1 - CONFIRM_PCT):.2f} 下方再动手，不追高\n"
                             f"🕐 {now_str()}")
                    fired[wkey] = now_str()
                    break

        # 多区：先判确认（站回），再判预警（仍在区间内）
        if not active:
            for z in longs:
                wkey, ckey = f"warn_long_{z['key']}", f"sig_long_{z['key']}"
                if not fired.get(ckey) and cooled() and not btc_weak:
                    touched = any(k[3] <= z["hi"] for k in recent)
                    if touched and price >= z["hi"] * (1 + CONFIRM_PCT):
                        push_now("开多信号",
                                 f"🟢 ETH 回调做多信号（支撑企稳）\n"
                                 f"日线方向：{dir_word(trend_down)}｜4H/1H：回调企稳\n"
                                 f"———————\n"
                                 f"🟢 买入价格：{z['entry']:.2f}\n"
                                 f"🛑 止损价格：{z['stop']:.2f}\n"
                                 f"🎯 目标一：{z['tp1']:.2f}\n🎯 目标二：{z['tp2']:.2f}\n🎯 目标三：{z['tp3']:.2f}\n"
                                 f"———————\n"
                                 f"💼 仓位：轻仓10%（逆势）｜日线偏空不建议加重\n"
                                 f"⚠ 逆势抄底是他亏钱的主因（SNDK 净亏 12.7 万），严格止损 {z['stop']:.2f}\n"
                                 f"🕐 {now_str()}")
                        fired[ckey] = now_str()
                        s["active"] = {"dir": "long", "entry": z["entry"], "stop": z["stop"],
                                       "tp1": z["tp1"], "tp2": z["tp2"], "tp3": z["tp3"],
                                       "zone": z["key"], "opened": now_str()}
                        active = s["active"]
                        last_push_ts = now_ts
                        break
                if not fired.get(wkey) and cooled():
                    if z["lo"] * 0.998 <= price <= z["hi"] * 1.001:
                        push_now("做多预警",
                                 f"🟢 ETH 进入做多区 {z['lo']:.2f}-{z['hi']:.2f}（{z['label']}）\n"
                                 f"现价 {price:.2f}｜下方多{z['idx']}\n"
                                 f"接多 {z['entry']:.2f}｜止损 {z['stop']:.2f}\n"
                                 f"止盈 {z['tp1']:.2f} / {z['tp2']:.2f} / {z['tp3']:.2f}\n"
                                 f"———————\n"
                                 f"⏳ 等他这类逆势抄底多半会浮亏，务必等 15 分钟站回 {z['hi'] * (1 + CONFIRM_PCT):.2f} 上方再进\n"
                                 f"🕐 {now_str()}")
                        fired[wkey] = now_str()
                        break

    # ---------- C. 持仓跟踪（优先级：止损 > 全部止盈 > 目标二 > 目标一） ----------
    if active and cooled():
        d = active["dir"]
        if d == "short":
            checks = [("stop", lambda p: p >= active["stop"],
                       f"🛑 ETH 空单止损 {active['stop']:.2f} 触发（现价 {price:.2f}），无条件离场！"),
                      ("tp3", lambda p: p <= active["tp3"],
                       f"✅ ETH 空单到达最终目标 {active['tp3']:.2f}（现价 {price:.2f}），全部止盈离场。"),
                      ("tp2", lambda p: p <= active["tp2"],
                       f"🎯 ETH 空单到达目标二 {active['tp2']:.2f}（现价 {price:.2f}），减仓一半、止损移至 {active['tp1']:.2f}。"),
                      ("tp1", lambda p: p <= active["tp1"],
                       f"🎯 ETH 空单到达目标一 {active['tp1']:.2f}（现价 {price:.2f}），减仓一半、止损下移至成本。")]
        else:
            checks = [("stop", lambda p: p <= active["stop"],
                       f"🛑 ETH 多单止损 {active['stop']:.2f} 触发（现价 {price:.2f}），无条件离场！"),
                      ("tp3", lambda p: p >= active["tp3"],
                       f"✅ ETH 多单到达最终目标 {active['tp3']:.2f}（现价 {price:.2f}），全部止盈离场。"),
                      ("tp2", lambda p: p >= active["tp2"],
                       f"🎯 ETH 多单到达目标二 {active['tp2']:.2f}（现价 {price:.2f}），减仓一半、止损移至 {active['tp1']:.2f}。"),
                      ("tp1", lambda p: p >= active["tp1"],
                       f"🎯 ETH 多单到达目标一 {active['tp1']:.2f}（现价 {price:.2f}），减仓一半、止损上移至成本。")]
        for tag, cond, msg in checks:
            if tag in active.get("hit", []) or fired.get(f"pos_{tag}"):
                continue
            if cond(price):
                push_now("仓位提醒", msg + f"\n🕐 {now_str()}")
                fired[f"pos_{tag}"] = now_str()
                active.setdefault("hit", []).append(tag)
                s["active"] = active
                s.setdefault("history", []).append(
                    {"dir": d, "zone": active["zone"], "entry": active["entry"],
                     "exit": tag, "opened": active["opened"], "closed": now_str()})
                if tag in ("stop", "tp3"):
                    s["active"] = None
                    active = None
                break

    # ---------- 发送 ----------
    if new_alerts:
        if PUSH_ENABLED:
            for kind, text in new_alerts:
                push(PREFIX + f"ETH{kind}", text)
                log(f"[{kind}] {text}")
            append_alerts(new_alerts)
        else:
            for kind, text in new_alerts:
                log(f"[推送暂停·未发送] [{kind}] {text}")
        s["last_push_ts"] = now_ts

    s["fired"] = fired
    s["levels"] = {"price": price, "shorts": shorts, "longs": longs}
    save_state(s)
    log("本轮铁头娃规则检测完成")


if __name__ == "__main__":
    main()
