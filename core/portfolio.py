"""从 holdings.md 解析组合摘要(供侧栏 OKR 卡显示,随持仓更新自动刷新)。"""

from __future__ import annotations

import re

from core.config import data_dir


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def summary() -> dict:
    p = data_dir() / "holdings.md"
    if not p.exists():
        return {}
    t = p.read_text(encoding="utf-8")
    total = _num(r"当前总资产.*?¥\s*([\d,]+(?:\.\d+)?)", t)
    initial = _num(r"初始本金.*?¥\s*([\d,]+(?:\.\d+)?)", t) or 1_500_000.0
    target = initial * 2
    out = {"initial": initial, "target": target}
    if total is not None:
        out["total_assets"] = round(total, 2)
        out["progress_pct"] = round((total - initial) / initial * 100, 2)
        out["to_double"] = round(target - total, 2)
        out["to_double_pct"] = round((target - total) / total * 100, 2) if total else None
    return out
