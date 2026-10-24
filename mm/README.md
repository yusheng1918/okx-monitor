# 接入 yusheng1918/okx-monitor（与现有监控并存）

## 先说清楚现状

你的仓库 `yusheng1918/okx-monitor` 里**已经有一套在跑的系统**：

| 现有 | 说明 |
|---|---|
| `.github/workflows/monitor.yml` | `OKX Trader Monitor`，每 3 分钟，已跑 1200+ 次 |
| `okx_monitor_cloud.py` | 监控 OKX 牛人榜 4 位交易员的实盘持仓，开/平/调仓推 Bark |
| `zec_swing.py` / `eth_swing.py` | ZEC / ETH 波段计划 |
| `index.html` | 根目录看板，GitHub Pages 已开启 |
| `alerts.jsonl` | 10KB 操作记录，说明推送确实在跑 |

**这次接入不改它任何一行**，只在旁边新加一个工作流。两套东西各跑各的：

- 它监控「别人怎么下单」→ 跟单信号
- 这套监控「价格到没到我的点位」→ 自己的开仓计划

## 要加的三个东西

```
你的仓库/
├── .github/workflows/
│   ├── monitor.yml          ← 已有，别动
│   └── mm-levels.yml        ← 新增
├── mm/                      ← 新增目录
│   ├── mm_monitor.py
│   ├── config.json
│   └── source_check.py
└── mm-dashboard.html        ← 第一次运行后自动生成（Pages 直接能访问）
```

## 三步接入

### 1. 把文件放进仓库

把 `mm/` 目录和 `.github/workflows/mm-levels.yml` 复制进你的本地仓库，然后：

```bash
git add mm .github/workflows/mm-levels.yml
git commit -m "feat: 多周期做市商价位监测"
git push
```

### 2. 确认 Secret

你的 `BARK_KEY` 已经配好了（现有工作流在用），**不用再加**。

可选：Settings → Secrets and variables → Actions → **Variables** → 新建 `MM_DASHBOARD_URL` =
`https://yusheng1918.github.io/okx-monitor/mm-dashboard.html`
配了之后，推送消息点开就跳到看板。

### 3. 手动跑一次

Actions → 左侧 **MM Levels** → Run workflow。

先看「数据源连通性自检」那段输出，再往下看「分析并推送」。

## 看板地址

```
https://yusheng1918.github.io/okx-monitor/mm-dashboard.html
```

因为你的 Pages 是从根目录发布的，所以新看板放在根目录、用独立文件名，不会覆盖你现有 `index.html`。

## 接 cron-job.org（每分钟）

GitHub 自带 schedule 最快 5 分钟且经常延迟。要精确到分钟就用 cron-job.org 调 API：

| 项 | 值 |
|---|---|
| URL | `https://api.github.com/repos/yusheng1918/okx-monitor/actions/workflows/mm-levels.yml/dispatches` |
| Method | `POST` |
| Header 1 | `Authorization: Bearer 你的Fine-grained-Token` |
| Header 2 | `Accept: application/vnd.github+json` |
| Header 3 | `Content-Type: application/json` |
| Header 4 | `User-Agent: cron-job` |
| Body | `{"ref":"main"}` |

Token：Settings → Developer settings → Fine-grained tokens → 只选 `okx-monitor` 这个仓库 → 权限只给 **Actions: Read and write**。

**注意**：启用 cron-job.org 后，把 `mm-levels.yml` 里这两行注释掉，否则每分钟触发 + 每 5 分钟定时 = 双倍运行：

```yaml
#  schedule:
#    - cron: "*/5 * * * *"
```

## 两套工作流会不会打架？

不会。我的提交步骤做了三件事：

1. 只 `git add mm-dashboard.html`，绝不碰你的 `state.json` / `alerts.jsonl` / `index.html`
2. 内容没变化就不提交（不制造垃圾 commit）
3. 推送前 `git pull --rebase --autostash`，失败重试 3 次 —— 你的 bot 也在提交，这样能避免 non-fast-forward

## 你可能遇到的问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 自检里 Gate 是 `451`，Kraken `OK` | runner 在美国，Gate 限制美国 IP | 程序自动降级为「Kraken 现货」：方向、点位、止损止盈照常，资金费率/OI/多空比显示 `—` |
| 自检全挂 | 出口 IP 被所有交易所拒绝 | 放弃 GitHub，用香港 VPS 版 |
| 推送没反应 | Secret 名字不是 `BARK_KEY`，或 Key 失效 | Actions 日志里会打印 `PUSH-HTTP 400 ... invalid device token` |
| 工作流 60 天后自动停 | GitHub 对长期无提交的仓库会停用 schedule | 每月手动跑一次，或依赖 cron-job.org（不受此限制） |

## 和现有系统的分工建议

你现在的 OKX 监控解决的是「别人在买什么」，这套解决的是「我该在什么价位挂单」。**建议只把后者的推送保留为开仓提醒**，前者的跟单信号看看就好 —— 两者的仓位管理逻辑完全不同，混在一起容易乱。
