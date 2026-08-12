"""资产文件工具：读持仓/策略/观察池，写持仓/观察池（限定在 data/ 目录内）。

替代 digital-life 的 terminal 工具——收窄成只读写交易资产文件，安全且够用。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from core.config import data_dir
from core.tools.registry import registry

_STRATEGY = "交易策略.md"
_HOLDINGS = "holdings.md"
_WATCHLIST = "watchlist.md"
_PLAN = "plan.md"
_ALERTS = "alerts.md"


def _safe(name: str) -> Path:
    """只允许 data/ 下的文件（含 holdings_history 子目录），禁止越界。"""
    p = (data_dir() / name).resolve()
    if not str(p).startswith(str(data_dir().resolve())):
        raise ValueError("path outside data dir")
    return p


def _read(name: str) -> str:
    p = _safe(name)
    if not p.exists():
        return json.dumps({"error": f"{name} 不存在"}, ensure_ascii=False)
    return p.read_text(encoding="utf-8")


def _handle_read_holdings(_args: dict) -> str:
    return _read(_HOLDINGS)


def _handle_read_strategy(_args: dict) -> str:
    return _read(_STRATEGY)


def _handle_read_watchlist(_args: dict) -> str:
    return _read(_WATCHLIST)


def _handle_read_plan(_args: dict) -> str:
    return _read(_PLAN)


def _handle_read_alerts(_args: dict) -> str:
    return _read(_ALERTS)


def _handle_write_file(args: dict) -> str:
    """在 data/ 内写文件（受控白名单）。"""
    name = (args.get("path") or "").strip()
    content = args.get("content")
    if not name or content is None:
        return json.dumps({"error": "需要 path 和 content"}, ensure_ascii=False)
    allowed = (name in (_HOLDINGS, _WATCHLIST, _PLAN, _ALERTS)
               or name.startswith("holdings_history/"))
    if not allowed:
        return json.dumps({"error": "只允许写 holdings.md/watchlist.md/plan.md/alerts.md/holdings_history/*"},
                          ensure_ascii=False)
    p = _safe(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = str(content)
    if name == _HOLDINGS and p.exists():
        # 禁止 agent 改现价/市值/盈亏/盈亏%列——这些由 compute_portfolio 实时算,不许模型手写。
        # 写入时把这几列冻结成旧文件里同代码的值(agent 只能改股数/成本/名称/市场)。
        text = _freeze_price_cols(text, p.read_text(encoding="utf-8"))
    p.write_text(text, encoding="utf-8")
    return json.dumps({"ok": True, "path": name, "bytes": len(text),
                       "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False)


def _row_cells(line: str) -> list[str] | None:
    """持仓数据行(| 序号 | ... |,≥11列且首列是数字)→ 单元格列表,否则 None。"""
    if not line.strip().startswith("|"):
        return None
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 11 or not cells[0].isdigit():
        return None
    return cells


def _freeze_price_cols(new_text: str, old_text: str) -> str:
    """把新内容里每只持仓的 现价/市值/盈亏/盈亏%(列5-8)替换成旧文件里同代码的值。
    列: 0#|1名|2代码|3股数|4成本|5现价|6市值|7盈亏|8盈亏%|9持仓%|10市场。新增标的(旧文件没有)保留原值。"""
    old = {}
    for ln in old_text.splitlines():
        c = _row_cells(ln)
        if c:
            old[c[2]] = c[5:9]   # code -> [现价,市值,盈亏,盈亏%]
    out = []
    for ln in new_text.splitlines():
        c = _row_cells(ln)
        if c and c[2] in old:
            c[5:9] = old[c[2]]
            out.append("| " + " | ".join(c) + " |")
        else:
            out.append(ln)
    return "\n".join(out) + ("\n" if new_text.endswith("\n") else "")


def register() -> None:
    for nm, desc, h in [
        ("read_holdings", "读当前持仓(唯一权威事实源 holdings.md)。答持仓/仓位/盈亏前必先读。", _handle_read_holdings),
        ("read_strategy", "读交易策略手册(交易策略.md)。做买卖决策前必读。", _handle_read_strategy),
        ("read_watchlist", "读观察池(watchlist.md,关注未持有的标的)。", _handle_read_watchlist),
        ("read_plan", "读作战计划(plan.md):翻倍OKR + 阶段子目标 + 当前进度/待办。每个循环先读它接上进度。", _handle_read_plan),
        ("read_alerts", "读价格告警配置(alerts.md):各持仓的止损位/买回位、观察池买点触发价。", _handle_read_alerts),
    ]:
        registry.register(nm, {"name": nm, "description": desc,
                               "parameters": {"type": "object", "properties": {}}}, h)
    registry.register("write_file", {
        "name": "write_file",
        "description": "写交易资产文件。path 仅限 holdings.md / watchlist.md / plan.md / alerts.md / holdings_history/YYYY-MM-DD.md。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    }, _handle_write_file)


register()
