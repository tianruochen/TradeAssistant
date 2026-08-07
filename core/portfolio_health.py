"""集中度 / 回撤 / 止损 告警——纯代码扫描持仓,触发即推通知(不调 LLM,不吃预算)。

数据源 holdings.md(唯一权威)+ holdings_history/ 快照(算历史峰值→回撤)。
每个问题按天去重,一天最多提醒一次,不刷屏。
"""

from __future__ import annotations

import re

from core.config import data_dir
from core import constraints

# 阈值(依据交易策略 §五止损纪律 + 集中度经验线)
_STOP_HALVE = -8.0     # 单票 -8% 无条件减半
_STOP_CLEAR = -15.0    # 单票 -15% 清仓
_TOP3_CONC = 50.0      # 前三集中度 > 50% 偏高
_DRAWDOWN = 8.0        # 组合自峰值回撤 > 8% 预警


def _snapshot_totals() -> list[float]:
    """从 holdings_history/*.md 各快照提取「当前总资产」,用于算历史峰值。"""
    d = data_dir() / "holdings_history"
    totals: list[float] = []
    if not d.exists():
        return totals
    for p in sorted(d.glob("*.md")):
        m = re.search(r"当前总资产.*?¥\s*([\d,]+(?:\.\d+)?)", p.read_text(encoding="utf-8"))
        if m:
            try:
                totals.append(float(m.group(1).replace(",", "")))
            except ValueError:
                pass
    return totals


def scan() -> list[dict]:
    """返回本次发现的问题清单(每项 {key, severity, label, detail})。纯读,无副作用。"""
    h = constraints.parse_holdings()
    positions = h["positions"]
    out: list[dict] = []

    # 1) 止损纪律
    for p in positions:
        pnl = p.get("pnl_pct")
        if pnl is None:
            continue
        if pnl <= _STOP_CLEAR:
            out.append({"key": f"clear:{p['code']}", "severity": "high", "label": "触发清仓线",
                        "detail": f"{p['name']} 浮亏 {pnl}%（≤-15% 清仓纪律），按体系处置"})
        elif pnl <= _STOP_HALVE:
            out.append({"key": f"halve:{p['code']}", "severity": "medium", "label": "触发减半线",
                        "detail": f"{p['name']} 浮亏 {pnl}%（≤-8% 减半纪律），评估减仓"})

    # 2) 前三集中度
    weights = sorted([p["weight_pct"] for p in positions if p["weight_pct"] is not None], reverse=True)
    top3 = round(sum(weights[:3]), 1)
    if top3 > _TOP3_CONC:
        names = "、".join(p["name"] for p in sorted(
            [q for q in positions if q["weight_pct"] is not None],
            key=lambda x: x["weight_pct"], reverse=True)[:3])
        out.append({"key": "conc3", "severity": "medium", "label": "集中度偏高",
                    "detail": f"前三持仓合计 {top3}%（>{_TOP3_CONC:.0f}%）：{names}，注意分散"})

    # 3) 组合回撤(自历史峰值)
    totals = _snapshot_totals()
    cur = h.get("total_assets")
    if cur is not None:
        totals.append(cur)
    if len(totals) >= 2 and cur is not None:
        peak = max(totals)
        if peak > 0:
            dd = round((peak - cur) / peak * 100, 1)
            if dd > _DRAWDOWN:
                out.append({"key": f"dd:{round(dd)}", "severity": "high", "label": "组合回撤预警",
                            "detail": f"总资产自峰值回撤 {dd}%（峰值¥{peak:,.0f}→现¥{cur:,.0f}），检查风险敞口"})

    # 4) 硬约束 high 违规(环境自适应上限)
    from core import market_env
    env = (market_env.classify().get("env")) or "shake"
    for v in constraints.check(env).get("violations", []):
        if v.get("severity") == "high":
            out.append({"key": f"cons:{v['rule']}", "severity": "high", "label": "硬约束违规",
                        "detail": f"{v['rule']}：{v['detail']}"})

    return out


def scan_and_notify(fired: set) -> list[dict]:
    """扫描 + 按天去重推送到通知流。fired 由调用方跨轮持有。返回本轮新推送项。"""
    from datetime import datetime
    from core import notifications
    today = datetime.now().strftime("%Y-%m-%d")
    fresh = []
    for item in scan():
        dk = (today, item["key"])
        if dk in fired:
            continue
        fired.add(dk)
        notifications.push("风险告警", f"[{item['label']}] {item['detail']}")
        fresh.append(item)
    return fresh
