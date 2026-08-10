"""交易流水 + 决策留痕(每用户独立,多租户 data_dir 自动隔离)。

- log_trade:用户报买卖 → 追加 trades.jsonl,并可算 FIFO 已实现盈亏。
- log_decision:Alpha 给出买卖分析/提示 → 追加 decisions.jsonl(事后可对照结果复盘)。
- 读取 + realized_pnl 供 API/侧栏用。
"""

from __future__ import annotations

import json
from datetime import datetime

from core.config import data_dir
from core.tools.registry import registry


def _append(fname: str, rec: dict) -> None:
    p = data_dir() / fname
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _rewrite(fname: str, recs: list[dict]) -> None:
    p = data_dir() / fname
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    tmp.replace(p)   # 原子替换,避免半写损坏


def _next_id(recs: list[dict]) -> int:
    return max((int(r.get("id") or 0) for r in recs), default=0) + 1


def _read(fname: str) -> list[dict]:
    p = data_dir() / fname
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def trades() -> list[dict]:
    return _read("trades.jsonl")


def decisions() -> list[dict]:
    return _read("decisions.jsonl")


def realized_pnl() -> dict:
    """FIFO 撮合已实现盈亏(不计手续费,MVP)。返回 总额 + 每标的。"""
    lots: dict[str, list[list]] = {}   # symbol -> [[shares, price], ...] 买入队列
    per: dict[str, float] = {}
    total = 0.0
    wins = 0; closed = 0   # 以「卖出笔」为单位统计胜率
    for t in trades():
        sym = str(t.get("symbol") or t.get("name") or "?")
        act = t.get("action")
        try:
            sh = float(t.get("shares") or 0); px = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if act == "buy":
            lots.setdefault(sym, []).append([sh, px])
        elif act == "sell":
            remain = sh
            q = lots.setdefault(sym, [])
            sell_pnl = 0.0; matched = False
            while remain > 1e-9 and q:
                lot = q[0]
                m = min(remain, lot[0])
                gain = (px - lot[1]) * m
                total += gain; sell_pnl += gain; matched = True
                per[sym] = per.get(sym, 0.0) + gain
                lot[0] -= m; remain -= m
                if lot[0] <= 1e-9:
                    q.pop(0)
            if matched:
                closed += 1
                if sell_pnl > 0:
                    wins += 1
    return {"total": round(total, 2), "total_pnl": round(total, 2),
            "per_symbol": {k: round(v, 2) for k, v in per.items()},
            "win_rate": round(wins / closed * 100, 1) if closed else None,
            "closed_trades": closed, "trade_count": len(trades())}


# ────────────────────────── 成本/盈亏计算器(确定性,禁 LLM 口算) ──────────────────────────

def compute_trades(current_shares: float, current_cost: float, seq: list[dict]) -> dict:
    """给定当前持仓(股数+成本价)与一串成交,确定性算出结果。绝不让 LLM 口算。
    - moving_avg(移动加权平均,标准记账):买入摊高、卖出按均价结算已实现盈亏。
    - t0_amortize(日内T摊低成本,散户口径):仅当净股数不变(纯回转)时有意义——
      把回转净差价直接冲减持仓成本,股数不变。
    返回两种口径 + 客观现金流,供 agent 如实呈现(不猜、不来回改)。"""
    shares = float(current_shares or 0)
    pool = shares * float(current_cost or 0)   # 总成本
    realized = 0.0
    cash = 0.0
    buy_amt = buy_sh = sell_amt = sell_sh = 0.0
    steps = []
    for t in seq:
        act = t.get("action")
        sh = float(t.get("shares") or 0)
        px = float(t.get("price") or 0)
        if act == "buy":
            pool += sh * px; shares += sh; cash -= sh * px
            buy_amt += sh * px; buy_sh += sh
        elif act == "sell":
            avg = pool / shares if shares > 1e-9 else 0.0
            realized += (px - avg) * sh
            pool -= avg * sh; shares -= sh; cash += sh * px
            sell_amt += sh * px; sell_sh += sh
        steps.append({"action": act, "shares": sh, "price": px,
                      "after_shares": round(shares, 2),
                      "after_avg_cost": round(pool / shares, 4) if shares > 1e-9 else 0.0})
    out = {
        "method_moving_avg": {
            "final_shares": round(shares, 2),
            "final_avg_cost": round(pool / shares, 4) if shares > 1e-9 else 0.0,
            "realized_pnl": round(realized, 2),
        },
        "cash_delta": round(cash, 2),           # 客观现金变动(正=净流入)
        "steps": steps,
    }
    net_share_change = round(buy_sh - sell_sh, 4)
    # 纯日内回转(买卖股数相等、净持仓不变)→ 给散户"摊低成本"口径
    if abs(net_share_change) < 1e-9 and current_shares and sell_sh > 0 and buy_sh > 0:
        net_cash = sell_amt - buy_amt   # 回转净差价(正=赚)
        new_pool = current_shares * float(current_cost or 0) - net_cash
        out["method_t0_amortize"] = {
            "final_shares": round(float(current_shares), 2),
            "final_avg_cost": round(new_pool / current_shares, 4),
            "roundtrip_gain": round(net_cash, 2),
            "note": "日内T回转:股数不变,差价冲减持仓成本(散户'摊低成本'口径)",
        }
    return out


# ────────────────────────── 工具 ──────────────────────────

def _handle_log_trade(args: dict) -> str:
    act = (args.get("action") or "").strip()
    if act not in ("buy", "sell"):
        return json.dumps({"error": "action 必须是 buy 或 sell"}, ensure_ascii=False)
    try:
        shares = float(args.get("shares") or 0); price = float(args.get("price") or 0)
    except (TypeError, ValueError):
        return json.dumps({"error": "shares/price 必须是数字"}, ensure_ascii=False)
    symbol = str(args.get("symbol") or "")
    existing = trades()
    # 幂等去重:10 分钟内同(方向/代码/股数/价)视为重复调用,直接跳过(治"同一笔录 3 次")
    now = datetime.now()
    for t in existing:
        if (t.get("action") == act and str(t.get("symbol") or "") == symbol
                and float(t.get("shares") or 0) == shares and float(t.get("price") or 0) == price):
            try:
                dt = datetime.strptime(str(t.get("ts") or ""), "%Y-%m-%d %H:%M:%S")
                if abs((now - dt).total_seconds()) < 600:
                    return json.dumps({"ok": True, "duplicate_skipped": True,
                                       "msg": f"该成交(#{t.get('id')})10分钟内已记录过,已跳过重复。如确要再记一笔请说明。",
                                       "existing": t}, ensure_ascii=False)
            except ValueError:
                pass
    rec = {"id": _next_id(existing), "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "action": act,
           "symbol": symbol, "name": str(args.get("name") or ""),
           "shares": shares, "price": price, "note": str(args.get("note") or "")}
    _append("trades.jsonl", rec)
    return json.dumps({"ok": True, "logged": rec,
                       "cash_delta": round((price * shares) * (1 if act == "sell" else -1), 2),
                       "realized_pnl_total": realized_pnl()["total"],
                       "hint": "只调一次;成本/盈亏用 calc_position 算,勿口算。"}, ensure_ascii=False)


def _handle_void_trade(args: dict) -> str:
    tid = args.get("trade_id")
    recs = trades()
    if tid is None:
        return json.dumps({"error": "需要 trade_id;先看流水拿 id"}, ensure_ascii=False)
    kept = [r for r in recs if int(r.get("id") or -1) != int(tid)]
    if len(kept) == len(recs):
        return json.dumps({"error": f"未找到 id={tid} 的流水"}, ensure_ascii=False)
    _rewrite("trades.jsonl", kept)
    return json.dumps({"ok": True, "voided_id": int(tid), "remaining": len(kept)}, ensure_ascii=False)


def _handle_calc_position(args: dict) -> str:
    try:
        cs = float(args.get("current_shares") or 0); cc = float(args.get("current_cost") or 0)
    except (TypeError, ValueError):
        return json.dumps({"error": "current_shares/current_cost 必须是数字"}, ensure_ascii=False)
    seq = args.get("trades") or []
    if not isinstance(seq, list) or not seq:
        return json.dumps({"error": "trades 需为非空数组,每项含 action/shares/price"}, ensure_ascii=False)
    return json.dumps(compute_trades(cs, cc, seq), ensure_ascii=False)


def _handle_compute_portfolio(_args: dict) -> str:
    from core import portfolio_compute
    return json.dumps(portfolio_compute.compute(live=True), ensure_ascii=False)



def _handle_log_decision(args: dict) -> str:
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "symbol": str(args.get("symbol") or ""), "name": str(args.get("name") or ""),
           "direction": str(args.get("direction") or ""), "reason": str(args.get("reason") or ""),
           "trigger": str(args.get("trigger") or ""), "price": args.get("price")}
    _append("decisions.jsonl", rec)
    return json.dumps({"ok": True, "logged": rec}, ensure_ascii=False)


def _handle_check_constraints(args: dict) -> str:
    from core import constraints, market_env
    env = (args.get("market_env") or "auto").strip()
    detected = None
    if env in ("", "auto"):
        detected = market_env.classify()
        env = detected.get("env") or "shake"   # 判不出回退震荡(保守)
    result = constraints.check(env)
    if detected is not None:
        result["env_source"] = {"auto_detected": detected.get("env_cn"), "detail": detected.get("detail")}
    return json.dumps(result, ensure_ascii=False)


def _handle_market_env(_args: dict) -> str:
    from core import market_env
    return json.dumps(market_env.classify(), ensure_ascii=False)


def register() -> None:
    registry.register("log_trade", {
        "name": "log_trade",
        "description": "记录一笔真实成交流水(用户报了买/卖就调)。用于自动算已实现盈亏/胜率。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["buy", "sell"]},
            "symbol": {"type": "string", "description": "代码"}, "name": {"type": "string"},
            "shares": {"type": "number"}, "price": {"type": "number"}, "note": {"type": "string"}},
            "required": ["action", "symbol", "shares", "price"]},
    }, _handle_log_trade)
    registry.register("void_trade", {
        "name": "void_trade",
        "description": "作废/撤销一笔记错的流水(按 trade_id)。误记/重复时用来清理,不用麻烦用户手工删。",
        "parameters": {"type": "object", "properties": {
            "trade_id": {"type": "number", "description": "要作废的流水 id(从流水记录里看)"}},
            "required": ["trade_id"]},
    }, _handle_void_trade)
    registry.register("calc_position", {
        "name": "calc_position",
        "description": "确定性计算成交对持仓成本/已实现盈亏/现金的影响——**任何涉及成本价、盈亏、日内T摊薄的计算都必须调它,严禁自己口算**。传当前股数+成本价+这串成交,返回移动加权平均口径与(纯回转时)日内T摊低成本口径,以及客观现金变动。",
        "parameters": {"type": "object", "properties": {
            "current_shares": {"type": "number", "description": "本次成交前的持股数"},
            "current_cost": {"type": "number", "description": "本次成交前的持仓成本价"},
            "trades": {"type": "array", "description": "按时间顺序的成交列表",
                       "items": {"type": "object", "properties": {
                           "action": {"type": "string", "enum": ["buy", "sell"]},
                           "shares": {"type": "number"}, "price": {"type": "number"}}}}},
            "required": ["current_shares", "current_cost", "trades"]},
    }, _handle_calc_position)
    registry.register("compute_portfolio", {
        "name": "compute_portfolio",
        "description": "代码现算组合总资产/盈亏/仓位%——**报总资产、总盈亏、持仓市值、仓位占比、翻倍进度时必须调它,禁止照抄 holdings.md 手打的汇总数或自己心算**。持仓列表取自 holdings.md(股数/成本),市值=股数×实时价(港股按汇率折CNY),现价实时。返回每只与合计。",
        "parameters": {"type": "object", "properties": {}},
    }, _handle_compute_portfolio)
    registry.register("log_decision", {
        "name": "log_decision",
        "description": "记录一条买卖分析/提示(给出方向性提示时调),事后可对照结果复盘胜率。",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"}, "name": {"type": "string"},
            "direction": {"type": "string", "description": "如 买入/卖出/减仓/持有/清仓/观察"},
            "reason": {"type": "string"}, "trigger": {"type": "string", "description": "触发条件/价位"},
            "price": {"type": "number"}}, "required": ["symbol", "direction", "reason"]},
    }, _handle_log_decision)
    registry.register("check_constraints", {
        "name": "check_constraints",
        "description": "硬约束校验:按交易策略从 holdings.md 核对现金≥10%/总仓位/单票上限/同板块≤2只/核心仓≤5只,返回违规清单。给买卖建议或做体检前调。",
        "parameters": {"type": "object", "properties": {
            "market_env": {"type": "string", "enum": ["auto", "bull", "shake", "bear"],
                           "description": "市场环境:auto=自动判定(默认,推荐)/牛bull/震荡shake/熊bear。定单票与总仓位上限"}}},
    }, _handle_check_constraints)
    registry.register("sense_market_env", {
        "name": "sense_market_env",
        "description": "判定当前大盘环境(牛/震荡/熊):看上证vsMA250+创业板指vsMA120,背离以弱者为准。定仓位总阀门,给建议前先看。",
        "parameters": {"type": "object", "properties": {}},
    }, _handle_market_env)


register()
