# TradeAssistant

个人 A 股交易**决策助手**（纯软件工具，不代客、不荐股）。Web 优先、豆包式流式对话；主 agent **Alpha** 按你的交易策略做分析，内部按需调用专家子 agent（机会挖掘 / 风控 / 记账）。支持飞书 / 微信通道与盘中定时任务。

> ⚠️ 所有分析仅供参考，**不构成投资建议**，交易决策与盈亏自负。

## 特性

- **豆包式流式对话**：思考（`<think>`）与正文分两路，前端可折叠"深度思考"；工具调用增量按 index 拼回，实时活动卡。
- **多专家 agent**：Alpha（主决策）+ hunter / risk / ledger（`consult_*` 工具）。扩展新专家 = 加 `agents/<name>/` + config 一行。
- **交易策略驱动**：择股走完整体系（市场环境 → 热门板块 → 买点 → ≥2 维度确认 → 止损），不只看资金流向。
- **风控三件套（纯代码，不耗 LLM）**：硬约束校验（现金≥10%/仓位/单票/同板块≤2/核心≤5）、大盘环境自动判定（上证vsMA250 + 创业板指vsMA120）、集中度/回撤/止损告警。
- **交易流水 & 决策留痕**：FIFO 已实现盈亏 + 胜率。
- **多租户 SaaS（自带 Key）**：注册制账户，数据按 `data/users/<uid>/` 隔离；每用户可用自己的 LLM Key/模型。
- **定时任务**：工作日盘中监控 / 深度研究 / 收盘翻倍进度；周末只做复盘汇总。产出进网页通知流（🔔）并作为消息落进对话，可选推飞书/微信。

## 快速开始

```bash
git clone https://github.com/tianruochen/TradeAssistant.git
cd TradeAssistant
pip install -r requirements.txt

cp secrets.env.example secrets.env    # 填入你的 LLM_API_KEY（OpenAI 兼容中转/官方）

python3 server.py                     # 或 ./tradeagent start
# 打开 http://127.0.0.1:8760 —— 首个注册的账户即你的业主账户
```

默认模型走 `config.yaml` 的 `model`（示例用 xinlicloud 中转，OpenAI 兼容）；改成你自己的中转/官方地址与模型名即可。

### 常驻运行

```bash
./tradeagent start | stop | restart | status | logs
# 解释器可用 TA_PYTHON 覆盖，默认 /opt/miniconda3/bin/python3.13
```

### 单业主后台任务（可选）
定时任务/告警要在网页登录账户里可见，需把该账户 uid 填进 `secrets.env` 的 `TA_OWNER_UID`（注册后从 `data/users.db` 取）。

## 配置

`config.yaml`：
- `model.name` 主 agent 模型、`model.sub_agent` 专家子 agent 模型（分层省钱）、`model.base_url`
- `server.port`（默认 8760）
- `agents.primary` / `agents.experts`

`secrets.env`（不入库）：`LLM_API_KEY`、`TA_OWNER_UID`。

## 目录

```
core/        引擎：llm(providers+client 流式/重试/并发闸) / agent loop / tools /
             scheduler / market_env / constraints / portfolio_health / stats / users / tenancy
channels/    web(SSE 流式) / feishu / wechat
agents/      alpha(主) + hunter/risk/ledger(专家) 的 persona/RULES
web/         单文件前端 index.html（原生 JS + marked，无需构建）
data/        运行时数据(gitignore)：holdings.md / watchlist.md / 交易策略.md / 快照 / 每用户目录
             仅 *.example.md 模板入库；新用户注册自动播种
config.yaml  server.py(入口)  tradeagent(常驻脚本)  requirements.txt
```

## 数据与隐私

- `secrets.env`、`data/`（真实持仓/策略/对话/通知/账户）**均不入库**，仅 `*.example.md` 模板入库。
- 第三方 LLM 中转会经手你的对话内容，敏感信息谨慎；生产建议用官方 API。

## 飞书 / 微信（可选）
填好凭据后 `channels/feishu.py` / `wechat.py` 生效，定时任务/告警可推到手机。见 `MIGRATION.md`。
