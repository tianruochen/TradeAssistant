"""决策质量归因:逐条 log_decision 对照后续实际价格,判"看多/看空"是否命中 → 决策命中率。

只评估有 6 位 A 股代码 + 有参考价(决策时价)的记录;用当前价对照。
纯读 + 行情查询(quick_price),供 /api/attribution。这是"我的判断准不准"的 track record。
"""

from __future__ import annotations

import re

from core.tools.ledger_tools import decisions
from core.tools.market_tools import quick_price

_LONG = ("买", "加仓", "建仓", "低吸", "持有", "看多", "增持")
_SHORT = ("卖", "减", "清仓", "止损", "看空", "回避")


def _side(direction: str) -> str | None:
    d = direction or ""
    if any(k in d for k in _SHORT):   # 先判卖出类(含"减")
        return "short"
    if any(k in d for k in _LONG):
        return "long"
    return None


def evaluate(limit: int = 40) -> dict:
    items = []
    wins = n = 0
    for r in decisions()[-limit:]:
        code = str(r.get("symbol") or "").strip()
        side = _side(r.get("direction") or "")
        try:
            entry = float(r.get("price")) if r.get("price") not in (None, "") else None
        except (TypeError, ValueError):
            entry = None
        rec = {"ts": r.get("ts"), "name": r.get("name") or code, "code": code,
               "direction": r.get("direction"), "side": side, "entry": entry}
        if not re.fullmatch(r"\d{6}", code) or side is None or entry is None:
            rec["outcome"] = "skip"      # 无码/无方向/无参考价 → 不计入命中率
            items.append(rec)
            continue
        now = quick_price(code)
        if now is None:
            rec["outcome"] = "no_price"
            items.append(rec)
            continue
        chg = round((now - entry) / entry * 100, 2)
        hit = (side == "long" and now > entry) or (side == "short" and now < entry)
        n += 1
        wins += 1 if hit else 0
        rec.update({"now": now, "chg_pct": chg, "hit": hit, "outcome": "hit" if hit else "miss"})
        items.append(rec)
    return {"items": items[::-1], "evaluated": n, "wins": wins,
            "win_rate": round(wins / n * 100, 1) if n else None}
