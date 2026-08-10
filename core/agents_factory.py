"""Agent 工厂 + 子 agent 咨询工具。

- build_agent(name): 从 agents/<name>/{persona.md,RULES.md} 组装 system prompt + 工具集。
- register_consult_tools(): 给主 agent 注册 consult_<expert> 工具——调用专家子 agent、
  返回其结论文本。扩展新专家 = 加 agents/<name>/ + 在 config.agents.experts 里加一行。
"""

from __future__ import annotations

import time

from core.agent import Agent
from core.config import agents_dir, config, model_config
from core.tools.registry import registry

# 子 agent 咨询结果缓存:同一轮推理里 agent 常重复问同一专家,短 TTL 命中省一次 LLM。
# 键 (uid, expert, question);TTL 短(90s),不跨轮次供陈旧数据。
_CONSULT_CACHE: dict[tuple, tuple[float, str]] = {}
_CONSULT_TTL = 90

# 各角色可用工具集
_MARKET = ["sense_stock_quote", "sense_stock_kline", "sense_sector_flow", "sense_market_scan"]
_READ = ["read_holdings", "read_strategy", "read_watchlist", "read_plan", "read_alerts"]
_PORT = ["compute_portfolio"]   # 代码现算组合总资产/盈亏,禁照抄手打汇总

_EXPERT_TOOLS = {
    "hunter": _MARKET + _READ + ["sense_market_env"],   # 机会挖掘：行情 + 读 + 大盘环境
    "risk": _MARKET + _READ + _PORT + ["check_constraints", "sense_market_env"],  # 风控：行情 + 读 + 组合现算 + 约束 + 环境
    "ledger": ["sense_stock_quote", "read_holdings", "read_watchlist", "read_plan",
               "write_file", "log_trade", "log_decision", "calc_position", "void_trade", "compute_portfolio"],  # 记账：报价 + 读写持仓/计划 + 流水/决策/算账/撤销/组合现算
}

_EXPERT_DESC = {
    "hunter": "机会猎手：挖掘热门板块内的机会标的，给买点/触发条件（观察候选，非买入指令）。",
    "risk": "风控卫士：评估持仓风险，给止损/减仓/仓位建议（按交易策略卖出体系）。",
    "ledger": "仓位管家：维护持仓记录、算翻倍进度（只记账，不做分析）。",
}


def _read(agent_name: str, fname: str) -> str:
    p = agents_dir() / agent_name / fname
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _system_prompt(name: str, is_primary: bool) -> str:
    persona = _read(name, "persona.md")
    rules = _read(name, "RULES.md")
    parts = []
    if persona:
        parts.append(persona)
    if rules:
        parts.append("## 行为规则（RULES）\n" + rules)
    if is_primary:
        experts = config().get("agents", {}).get("experts", [])
        consult = "、".join(f"consult_{e}" for e in experts)
        parts.append(
            "## 你的工作方式\n"
            "你是主决策者，围绕 OKR（翻倍）自驱运转，而不是被动等指令。\n"
            "**循环闭环**：感知（read_holdings 看持仓、compute_portfolio 算实时市值盈亏、sense_* 看盘/板块）→ 对照 read_plan 的目标与进度 → "
            f"必要时调专家 {consult} → 给出一致决策 → 用 write_file 更新 plan.md 的进度/循环记录。\n"
            "**效率优先·别滥用专家**：优先用你自己的工具（compute_portfolio/check_constraints/sense_stock_quote/sense_stock_kline/sense_market_env）直接完成分析；"
            "**只在确实需要某位专家的专门判断时才 consult 那一位，不要每次把三个专家都问一遍**（每次 consult 都会跑一个完整子 agent，很慢很费）。一次咨询通常至多 1 个专家。\n"
            "**决策纪律**：买卖判断前先 read_strategy；标的必须在当日热门板块内；≥2维度确认；买前写死止损。\n"
            "**跨天不失忆**：不靠对话记忆，靠 plan.md / holdings.md / watchlist.md 这些文件接上进度。\n"
            "**上手对齐**：用户第一次给一个大目标（如'帮我盯盘翻倍'）时，先反问 1-3 个关键约束（风险偏好/单票上限/是否接受T+0/播报频率）对齐，再开跑，别一上来就自作主张。\n"
            "回答用清晰中文，先结论后依据。禁止幽灵标的（已清仓的不提），港股市值按汇率换 CNY。"
        )
    return "\n\n".join(parts) if parts else f"你是 {name}。"


def build_agent(name: str, is_primary: bool = False, model_override: str | None = None,
                thinking: bool | None = None) -> Agent:
    if is_primary:
        experts = config().get("agents", {}).get("experts", [])
        tools = _MARKET + _READ + _PORT + ["write_file", "log_trade", "log_decision", "calc_position", "void_trade", "check_constraints", "sense_market_env"] + [f"consult_{e}" for e in experts]
    else:
        tools = _EXPERT_TOOLS.get(name, _MARKET + _READ)
    mc = model_config(name)
    if model_override:
        mc = {**mc, "name": model_override}
    if thinking is not None:
        mc = {**mc, "thinking": thinking}   # 深度思考开关(仅主对话按需开,子agent不开省钱)
    return Agent(
        name=name,
        system_prompt=_system_prompt(name, is_primary),
        tool_names=tools,
        registry=registry,
        model_cfg=mc,
        force_first_tool=False,   # 新中转(思考模型)不丢工具且不支持tool_choice=required(会400);靠模型自主调工具+RULES+verify兜底
    )


def _make_consult_handler(expert: str):
    async def handler(args: dict) -> str:
        question = (args.get("question") or "").strip()
        if not question:
            return '{"error":"question 不能为空"}'
        from core.tenancy import CURRENT_UID
        uid = CURRENT_UID.get() or "_global"
        key = (uid, expert, question)
        hit = _CONSULT_CACHE.get(key)
        now = time.time()
        if hit and hit[0] > now - _CONSULT_TTL:
            return hit[1] + "\n\n(注:同轮重复咨询,返回缓存结论)"
        try:
            agent = build_agent(expert, is_primary=False)
            answer = await agent.run([{"role": "user", "content": question}])
        except Exception as exc:  # noqa: BLE001  子agent失败不拖垮主agent,给占位让其继续
            return f"（{expert} 专家暂时不可用:{type(exc).__name__},本次跳过其意见,请基于其余信息给结论）"
        answer = answer or f"（{expert} 无有效输出,本次跳过）"
        _CONSULT_CACHE[key] = (now, answer)
        # 顺手清理过期项,避免无界增长
        for k in [k for k, v in _CONSULT_CACHE.items() if v[0] <= now - _CONSULT_TTL]:
            _CONSULT_CACHE.pop(k, None)
        return answer
    return handler


async def consult_stream(expert: str, question: str):
    """流式咨询专家子 agent：yield 其内部 activity 事件（带 via=expert 供前端嵌套显示），
    结束时 yield {"type":"consult_final","content": 结论文本}。含同轮缓存 + 失败占位。"""
    question = (question or "").strip()
    if not question:
        yield {"type": "consult_final", "content": '{"error":"question 不能为空"}'}
        return
    from core.tenancy import CURRENT_UID
    uid = CURRENT_UID.get() or "_global"
    key = (uid, expert, question)
    now = time.time()
    hit = _CONSULT_CACHE.get(key)
    if hit and hit[0] > now - _CONSULT_TTL:
        yield {"type": "consult_final", "content": hit[1] + "\n\n(注:同轮重复咨询,返回缓存结论)"}
        return
    answer = ""
    parts: list[str] = []
    try:
        agent = build_agent(expert, is_primary=False)
        async for ev in agent.run_stream([{"role": "user", "content": question}]):
            t = ev.get("type")
            if t == "activity":
                yield {**ev, "via": expert}   # 透传子agent的工具活动,标注来自哪个专家
            elif t == "content":
                parts.append(ev.get("delta", ""))
            elif t == "done":
                answer = ev.get("message", {}).get("content") or "".join(parts)
    except Exception as exc:  # noqa: BLE001  子agent失败不拖垮主agent,给占位让其继续
        yield {"type": "consult_final",
               "content": f"（{expert} 专家暂时不可用:{type(exc).__name__},本次跳过其意见,请基于其余信息给结论）"}
        return
    answer = answer or "".join(parts) or f"（{expert} 无有效输出,本次跳过）"
    _CONSULT_CACHE[key] = (now, answer)
    for k in [k for k, v in _CONSULT_CACHE.items() if v[0] <= now - _CONSULT_TTL]:
        _CONSULT_CACHE.pop(k, None)
    yield {"type": "consult_final", "content": answer}


def register_consult_tools() -> None:
    for expert in config().get("agents", {}).get("experts", []):
        registry.register(
            f"consult_{expert}",
            {
                "name": f"consult_{expert}",
                "description": _EXPERT_DESC.get(expert, f"咨询专家 {expert}") + " 传入具体问题，返回其分析结论。",
                "parameters": {"type": "object", "properties": {
                    "question": {"type": "string", "description": "要咨询的具体问题/需要它分析的内容"}},
                    "required": ["question"]},
            },
            _make_consult_handler(expert),
        )


def primary_agent(model_override: str | None = None, thinking: bool | None = None) -> Agent:
    register_consult_tools()
    return build_agent(config().get("agents", {}).get("primary", "alpha"),
                       is_primary=True, model_override=model_override, thinking=thinking)
