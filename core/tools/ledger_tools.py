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


# ────────────────────────── 工具 ──────────────────────────

def _handle_log_trade(args: dict) -> str:
    act = (args.get("action") or "").strip()
    if act not in ("buy", "sell"):
        return json.dumps({"error": "action 必须是 buy 或 sell"}, ensure_ascii=False)
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "action": act,
           "symbol": str(args.get("symbol") or ""), "name": str(args.get("name") or ""),
           "shares": args.get("shares"), "price": args.get("price"), "note": str(args.get("note") or "")}
    _append("trades.jsonl", rec)
    return json.dumps({"ok": True, "logged": rec, "realized_pnl": realized_pnl()["total"]}, ensure_ascii=False)


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
