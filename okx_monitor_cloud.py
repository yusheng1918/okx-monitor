# -*- coding: utf-8 -*-
"""
欧易「梭哈闪迪」实盘持仓监控 —— 云端版（GitHub Actions 每 5 分钟定时运行）
与电脑无关：关机、断网、睡觉都不影响监控。

运行逻辑：
  1. 读取仓库里的 state.json（上一次快照的持仓）
  2. 调欧易接口拉当前持仓
  3. 对比 → 开仓/平仓/调仓告警
  4. 推送到 iPhone（Bark）和/或 微信（Server酱）
  5. 把新快照写回 state.json（由 workflow 提交回仓库）

所需 Secrets（仓库 Settings -> Secrets and variables -> Actions）：
  OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE   欧易只读 API
  BARK_KEY              可选，iPhone Bark 设备 Key（推荐，秒推）
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

TRADER_NICKNAME = "梭哈闪迪"
AUTHOR_ID = "966711406828093440"
BASE_URL = "https://www.okx.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

BARK_KEY = os.environ.get("BARK_KEY", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_SENDKEY", "")


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


def get_positions():
    data = okx_get(f"/api/v5/orbit/public/position-current?authorId={AUTHOR_ID}")
    if str(data.get("code")) != "0":
        raise RuntimeError(f"API error: {data}")
    rows = data.get("data") or []
    if rows and isinstance(rows[0], dict) and isinstance(rows[0].get("posData"), list):
        return rows[0]["posData"]
    return rows


def fmt_ts(ms):
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000,
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def pos_key(p):
    return f"{p.get('instId')}|{p.get('posSide') or p.get('direction')}"


def describe(p):
    side = p.get("posSide") or p.get("direction") or "?"
    return (f"{p.get('instId', '?')} {side} "
            f"{p.get('lever', '?')}x {p.get('mgnMode', '')} "
            f"开仓均价 {p.get('openAvgPx', p.get('avgPx', '?'))} "
            f"数量 {p.get('openMaxAmount', p.get('pos', '?'))} "
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


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("positions")
    except Exception:
        return None


def save_state(positions):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"authorId": AUTHOR_ID,
                   "updated": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                   "positions": positions}, f, ensure_ascii=False, indent=1)


def main():
    old = load_state()
    positions = get_positions()
    log(f"当前 {len(positions)} 个持仓")

    if old is None:
        log("首次运行，记录初始快照（不推送）")
        for p in positions:
            log("  · " + describe(p))
        save_state(positions)
        return

    old_map = {pos_key(p): p for p in old}
    new_map = {pos_key(p): p for p in positions}

    alerts = []
    for k, p in new_map.items():
        if k not in old_map:
            alerts.append(("开仓", "🟢 新开仓 → " + describe(p)))
    for k, p in old_map.items():
        if k not in new_map:
            alerts.append(("平仓", "🔴 已平仓 → " + describe(p)))
    for k, p in new_map.items():
        if k in old_map:
            o = old_map[k]
            if str(o.get("openAvgPx")) != str(p.get("openAvgPx")) or str(o.get("openMaxAmount")) != str(p.get("openMaxAmount")):
                alerts.append(("调仓", f"🟡 仓位变动 [{k}] 旧均价 {o.get('openAvgPx')} → 新均价 {p.get('openAvgPx')}，数量 {o.get('openMaxAmount')} → {p.get('openMaxAmount')}"))

    for kind, text in alerts:
        push(f"梭哈闪迪·{kind}", text)
        log(f"[{kind}] {text}")

    save_state(positions)
    if not alerts:
        log("无变动")


if __name__ == "__main__":
    main()
