# -*- coding: utf-8 -*-
"""
欧易「牛人榜」实盘持仓监控 —— 云端版（GitHub Actions 定时运行）
支持同时监控多位交易员。

运行逻辑：
  1. 读取仓库里的 state.json（上一次快照）
  2. 逐个调欧易接口拉每位交易员当前持仓
  3. 对比 → 开仓/平仓/调仓告警
  4. 推送到 iPhone（Bark）和/或 微信（Server酱）
  5. 把新快照写回 state.json（由 workflow 提交回仓库）

所需 Secrets：
  OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE   欧易只读 API
  BARK_KEY              可选，iPhone Bark 设备 Key
  SERVERCHAN_SENDKEY    可选，微信 Server酱 SendKey
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request

API_KEY = os.environ["OKX_API_KEY"]
API_SECRET = os.environ["OKX_API_SECRET"]
API_PASSPHRASE = os.environ["OKX_API_PASSPHRASE"]

# ============ 监控名单（新增博主在这里加一行即可） ============
TRADERS = [
    {"nickname": "梭哈闪迪",   "authorId": "966711406828093440"},
    {"nickname": "天才铁头娃", "authorId": "872873784163041281"},
    {"nickname": "阿坝阿坝168", "authorId": "872856236919042050"},
]
# =============================================================

BASE_URL = "https://www.okx.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
ALERTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.jsonl")
ALERTS_MAX = 500  # 最多保留最近500条操作记录

BARK_KEY = os.environ.get("BARK_KEY", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_SENDKEY", "")

# 博主开平仓推送总开关（"1"=推送，"0"=暂停）
# 注意：eth_swing.py / zec_swing.py 的波段推送不走此开关，不受影响
MONITOR_PUSH_ENABLED = os.environ.get("MONITOR_PUSH_ENABLED", "1") == "1"


def log(msg):
    print(datetime.datetime.now(datetime.UTC).strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def sign(ts, method, path):
    msg = f"{ts}{method.upper()}{path}"
    mac = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def okx_get(path):
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    headers = {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign(ts, "GET", path),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    req = urllib.request.Request(BASE_URL + path, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def get_ctval(inst_id, _cache={}):
    """查询合约面值（每张 = 多少个币），如 ETH-USDT-SWAP -> 0.1 ETH。结果按轮缓存"""
    if inst_id in _cache:
        return _cache[inst_id]
    data = okx_get(f"/api/v5/public/instruments?instType=SWAP&instId={inst_id}")
    ctval = None
    rows = data.get("data") or []
    if rows:
        try:
            ctval = float(rows[0].get("ctVal"))
        except Exception:
            ctval = None
    _cache[inst_id] = ctval
    return ctval


def enrich_positions(positions):
    """给每个持仓补上精确的币数量 coinQty = 张数 × 合约面值"""
    for p in positions:
        try:
            ctval = get_ctval(p.get("instId", ""))
            pos_f = float(p.get("pos") or 0)
            if ctval and pos_f:
                p["coinQty"] = round(pos_f * ctval, 6)
        except Exception as e:
            log(f"面值查询失败 {p.get('instId')}: {e}")
    return positions


def get_positions(author_id):
    data = okx_get(f"/api/v5/orbit/public/position-current?authorId={author_id}")
    if str(data.get("code")) != "0":
        raise RuntimeError(f"API error: {data}")
    rows = data.get("data") or []
    if rows and isinstance(rows[0], dict) and isinstance(rows[0].get("posData"), list):
        return rows[0]["posData"]
    return rows


def get_leaderboard_assets(author_ids):
    """从聪明钱榜接口抓取各博主的实盘账户资产量（USDT）。返回 {authorId: {"asset":..,"pnl":..}}"""
    path = ("/api/v5/orbit/public/leaderboard?authorIds=" + ",".join(author_ids)
            + "&sortBy=pnl&period=90")
    data = okx_get(path)
    if str(data.get("code")) != "0":
        raise RuntimeError(f"leaderboard API error: {data}")
    inner = data.get("data") or {}
    rows = inner.get("data") if isinstance(inner, dict) else inner
    out = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        aid = str(r.get("authorId") or r.get("uid") or "")
        out[aid] = {"asset": r.get("asset"), "pnl": r.get("pnl")}
    log(f"聪明钱榜返回 {len(out)} 位博主资产: " + json.dumps(out, ensure_ascii=False)[:500])
    return out


def fmt_ts(ms):
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000,
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def fmt_px(v):
    """价格统一格式化为最多6位有效数字，去掉冗长小数"""
    try:
        return f"{float(v):.6g}"
    except Exception:
        return str(v)


def fmt_qty(p):
    """数量以'币个数'展示：优先用官方合约面值精确计算（coinQty），回退到反推，最后回退张数"""
    qty = p.get("pos") or p.get("openMaxAmount")
    if qty is None:
        return "?"
    ccy = p.get("posCcy") or ""
    if p.get("coinQty"):
        return f"{float(p['coinQty']):.4g}个{ccy}({qty}张)"
    try:
        pos_f, last_f, notional = float(p.get("pos")), float(p.get("last")), float(p.get("notionalUsd") or 0)
        if pos_f > 0 and last_f > 0 and notional > 0:
            ctval = notional / (pos_f * last_f)
            return f"{float(qty) * ctval:.4g}个{ccy}({qty}张)"
    except Exception:
        pass
    return f"{qty}张"


def pos_key(p):
    return f"{p.get('instId')}|{p.get('posSide') or p.get('direction')}"


def describe(p):
    side = p.get("posSide") or p.get("direction") or "?"
    return (f"{p.get('instId', '?')} {side} "
            f"{p.get('lever', '?')}x {p.get('mgnMode', '')} "
            f"开仓均价 {p.get('openAvgPx', p.get('avgPx', '?'))} "
            f"数量 {fmt_qty(p)} "
            f"开仓时间 {fmt_ts(p.get('cTime'))}")


def push(title, body):
    if BARK_KEY:
        try:
            url = ("https://api.day.app/" + urllib.parse.quote(BARK_KEY) + "/"
                   + urllib.parse.quote(title) + "/" + urllib.parse.quote(body)
                   + "?group=OKXMonitor")
            urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15).read()
            log("Bark 推送成功")
        except Exception as e:
            log(f"Bark 推送失败: {e}")
    if SERVERCHAN_KEY:
        try:
            body_data = urllib.parse.urlencode({"title": title, "desp": body}).encode()
            urllib.request.urlopen(urllib.request.Request(
                f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
                data=body_data, headers={"User-Agent": "Mozilla/5.0"}), timeout=15).read()
            log("Server酱推送成功")
        except Exception as e:
            log(f"Server酱推送失败: {e}")
    if not BARK_KEY and not SERVERCHAN_KEY:
        log(f"[未配置推送通道] {title} | {body}")


def append_alerts(alerts):
    """把本轮推送的告警追加到 alerts.jsonl（网页看板底部的操作记录来源）"""
    if not alerts:
        return
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    lines = []
    for kind, text in alerts:
        lines.append(json.dumps({"ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                                 "type": kind, "text": text}, ensure_ascii=False))
    old = []
    try:
        with open(ALERTS_FILE, encoding="utf-8") as f:
            old = [l for l in f.read().splitlines() if l.strip()]
    except Exception:
        pass
    all_lines = old + lines
    if len(all_lines) > ALERTS_MAX:
        all_lines = all_lines[-ALERTS_MAX:]
    with open(ALERTS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines) + "\n")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(trader_states):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "updated": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
            "traders": trader_states,
        }, f, ensure_ascii=False, indent=1)


def diff_one(nickname, old_positions, positions):
    """返回 (alerts, first_run)"""
    if old_positions is None:
        log(f"[{nickname}] 首次跟踪，记录初始快照（不推送）：{len(positions)} 个持仓")
        for p in positions:
            log("  · " + describe(p))
        return [], True
    old_map = {pos_key(p): p for p in old_positions}
    new_map = {pos_key(p): p for p in positions}
    alerts = []
    for k, p in new_map.items():
        if k not in old_map:
            alerts.append(("开仓", f"🟢 {nickname} 新开仓 → " + describe(p)))
    for k, p in old_map.items():
        if k not in new_map:
            alerts.append(("平仓", f"🔴 {nickname} 已平仓 → " + describe(p)))
    for k, p in new_map.items():
        if k in old_map:
            o = old_map[k]
            # 兼容两种字段结构：当前持仓接口用 avgPx/pos，历史接口用 openAvgPx/openMaxAmount
            o_avg, n_avg = o.get("avgPx") or o.get("openAvgPx"), p.get("avgPx") or p.get("openAvgPx")
            o_amt, n_amt = o.get("pos") or o.get("openMaxAmount"), p.get("pos") or p.get("openMaxAmount")
            if str(o_avg) != str(n_avg) or str(o_amt) != str(n_amt):
                alerts.append(("调仓", f"🟡 {nickname} 仓位变动 [{k}] 均价 {fmt_px(o_avg)} → {fmt_px(n_avg)}，数量 {fmt_qty(o)} → {fmt_qty(p)}"))
    return alerts, False


def main():
    state = load_state()
    old_traders = state.get("traders") or {}
    # 兼容旧版单交易员格式
    if not old_traders and state.get("positions") is not None:
        old_traders = {"966711406828093440": {"positions": state["positions"]}}

    new_states = {}
    all_alerts = []
    try:
        lb = get_leaderboard_assets([t["authorId"] for t in TRADERS])
    except Exception as e:
        log(f"资产抓取失败: {e}")
        lb = {}
    for t in TRADERS:
        nickname, author_id = t["nickname"], t["authorId"]
        try:
            positions = get_positions(author_id)
        except Exception as e:
            log(f"[{nickname}] 拉取失败: {e}")
            # 拉取失败时保留旧状态，下一轮再试
            new_states[author_id] = old_traders.get(author_id, {"nickname": nickname, "positions": None})
            new_states[author_id]["nickname"] = nickname
            continue
        log(f"[{nickname}] 当前 {len(positions)} 个持仓")
        positions = enrich_positions(positions)
        old = (old_traders.get(author_id) or {}).get("positions")
        alerts, _ = diff_one(nickname, old, positions)
        for kind, text in alerts:
            all_alerts.append((kind, text))
            if MONITOR_PUSH_ENABLED:
                push(f"{nickname}·{kind}", text)
            else:
                log(f"[推送已暂停] [{kind}] {text[:60]}")
            log(f"[{kind}] {text}")
        new_states[author_id] = {"nickname": nickname, "positions": positions,
                                 "asset": (lb.get(author_id) or {}).get("asset")}

    # 操作记录落盘（推送与看板同源）
    append_alerts(all_alerts)

    # 快照写入策略：
    #  - 仓位有变化 → 立即写入（触发 git 提交 → 网页 1-2 分钟内同步）
    #  - 仓位无变化 → 保持文件不变（不产生提交），除非快照已超过 30 分钟（定期刷新现价）
    changed = False
    for aid, ns in new_states.items():
        old_pos = (old_traders.get(aid) or {}).get("positions")
        if old_pos != ns.get("positions"):
            changed = True
            break

    if not changed:
        stale = True
        try:
            updated = datetime.datetime.strptime(state.get("updated", ""), "%Y-%m-%d %H:%M:%S")
            age = datetime.datetime.now() - updated
            stale = age.total_seconds() > 30 * 60
        except Exception:
            stale = True
        if stale:
            log("仓位无变化，但快照已超过30分钟，刷新现价")
            save_state(new_states)
        else:
            log("仓位无变化，快照保持不变（不提交）")
    else:
        save_state(new_states)
    log("本轮完成")


if __name__ == "__main__":
    main()
