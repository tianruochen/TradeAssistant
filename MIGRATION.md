# TradeAssistant 迁移 & 切换清单

## 已完成迁移（P0–P5）

- **引擎**：流式 LLM client（`<think>` 思考/正文拆分 + 工具调用增量）、provider、config。
- **工具**：`sense_stock_quote/kline/sector_flow/market_scan`（含港股换汇率、任意 A 股代码、新浪兜底）、资产读写（read_holdings/strategy/watchlist、write_file）。
- **Agent**：主 agent `zero` + 专家子 agent `hunter/risk/ledger`（consult_* 调用）；persona/RULES 见 `agents/`。
- **数据资产**（`data/`）：`holdings.md`（¥163.5万，港股CNY正确、无幽灵标的）、`watchlist.md`、`交易策略.md`（V3.0）、`holdings_history/`。
- **通道**：web SSE 流式聊天（已跑通）；飞书 webhook+发送、微信轮询+发送（**凭据开关，未配即 no-op**）。
- **定时任务**：盘中半小时监控 / 11:15·13:15 深度研究 / 16:05 翻倍进度（已跑通，无通道时记日志）。

## 上线飞书/微信（需你的凭据，未做）

在 `secrets.env` 填：
```
FEISHU_APP_ID=cli_xxx           # 用你的飞书自建应用
FEISHU_APP_SECRET=xxx
SCHEDULE_FEISHU_CHAT=oc_xxx      # 定时任务/主动推送的目标群
WECHAT_BOT_TOKEN=xxx            # 微信 ClawBot(可选)
```
飞书开放平台 → 事件订阅 URL 指向 `https://<公网或内网穿透>/feishu/webhook`（本机需 frp/cloudflared 等暴露 8760）。填完重启 `python3 server.py` 即生效。

## 切换（⚠️ 未自动执行 —— 等通道验证后再做）

**不要在 TradeAssistant 飞书/微信通道验证可用之前**关掉 digital-life 的交易实例，否则会没有可用 bot。顺序：
1. TradeAssistant 配好飞书凭据 + webhook，飞书 @ zero 能正常回复。
2. 确认定时任务在 TradeAssistant 正常投递到群。
3. 再把 digital-life 的 4 个交易实例下线（数据保留、可回滚）：
   ```
   for iid in 65ec7f1e-... 68554fd0-... 529ff58d-... 09a6fc89-...; do
     curl -X POST http://127.0.0.1:8642/api/system/instances/$iid/active \
       -H 'Content-Type: application/json' -d '{"active":false,"reason":"迁移到 TradeAssistant"}'
   done
   ```
4. digital-life 的 `projects/double_journey/` 保留作历史备份；TradeAssistant `data/` 为新权威源。

> 现状：digital-life 4 个交易 bot 仍在正常运行（飞书可用）；TradeAssistant 已可本地 web 使用。两者暂时并存，零风险切换。
