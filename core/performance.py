"""实盘绩效跟踪(track record):从每日快照建资产曲线 + 已实现盈亏/胜率/最大回撤。

数据源(每用户 data_dir 隔离):
- holdings_history/YYYY-MM-DD.md 的「当前总资产」→ 资产曲线各点;holdings.md 为最新点。
- ledger(trades/decisions)→ 已实现盈亏 / 胜率 / 决策条数。
纯读、无副作用,供 /api/performance 与前端绩效视图。
"""

from __future__ import annotations

import re

from core.config import data_dir

_TOTAL_RE = re.compile(r"当前总资产.*?¥\s*([\d,]+(?:\.\d+)?)")
_INIT_RE = re.compile(r"初始本金.*?¥\s*([\d,]+(?:\.\d+)?)")


def _num(text: str, rx: re.Pattern) -> float | None:
    m = rx.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _equity_points() -> list[dict]:
    """按日期升序的资产曲线 [{date, total}]。"""
    pts: dict[str, float] = {}
    hh = data_dir() / "holdings_history"
    if hh.exists():
        for p in hh.glob("*.md"):
            t = _num(p.read_text(encoding="utf-8"), _TOTAL_RE)
            if t is not None:
                pts[p.stem] = round(t, 2)
    # 最新一点用 holdings.md(可能比最后一张快照更新)
    hp = data_dir() / "holdings.md"
    if hp.exists():
        txt = hp.read_text(encoding="utf-8")
        t = _num(txt, _TOTAL_RE)
        m = re.search(r"更新[:：]\s*(\d{4}-\d{2}-\d{2})", txt)
        if t is not None:
            pts[m.group(1) if m else "latest"] = round(t, 2)
    return [{"date": d, "total": pts[d]} for d in sorted(pts)]


def _max_drawdown(vals: list[float]) -> float:
    """最大回撤%(峰值到谷底的最大跌幅)。"""
    peak = None
    mdd = 0.0
    for v in vals:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    return round(mdd, 2)


def summary() -> dict:
    from core.tools.ledger_tools import realized_pnl, decisions
    pts = _equity_points()
    hp = data_dir() / "holdings.md"
    initial = _num(hp.read_text(encoding="utf-8"), _INIT_RE) if hp.exists() else None

    out: dict = {"points": pts, "initial": initial}
    if pts:
        latest = pts[-1]["total"]
        out["latest"] = latest
        if initial:
            out["return_pct"] = round((latest - initial) / initial * 100, 2)
            out["target"] = round(initial * 2, 2)
            out["progress_pct"] = round((latest - initial) / initial * 100, 2)
        out["max_drawdown_pct"] = _max_drawdown([p["total"] for p in pts])
    pnl = realized_pnl()
    out["realized_pnl"] = pnl.get("total_pnl")
    out["win_rate"] = pnl.get("win_rate")
    out["closed_trades"] = pnl.get("closed_trades")
    out["trade_count"] = pnl.get("trade_count")
    out["decision_count"] = len(decisions())
    return out
