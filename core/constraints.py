"""硬约束校验——不靠 LLM 自觉,用代码从 holdings.md 解析并核对交易策略的铁律。

依据 交易策略.md §五 仓位管理规则:
- 现金 ≥ 10%(永远保留弹药)
- 单票上限:牛 25% / 震荡 20% / 熊 15%(按环境)
- 总仓位上限:牛 90% / 震荡 70% / 熊 50%
- 同一板块/行业 ≤ 2 只(ETF/打底仓另计)
- 核心仓 ≤ 5 只(ETF/打底仓另计)

供 `check_constraints` 工具 + /api/constraints 用。纯解析,无副作用。
"""

from __future__ import annotations

import re

from core.config import data_dir

# 按市场环境的上限(交易策略 §一/§五)
_SINGLE_CAP = {"bull": 25.0, "shake": 20.0, "bear": 15.0}
_TOTAL_CAP = {"bull": 90.0, "shake": 70.0, "bear": 50.0}
_ENV_CN = {"bull": "牛市", "shake": "震荡市", "bear": "熊市"}


def _is_etf(name: str, market: str) -> bool:
    return "ETF" in name.upper() or "基金" in market


def _sector(market: str) -> str:
    """从「市场」列取板块:'A股·医药CXO' → '医药CXO';无分隔符则原样。"""
    market = market.strip()
    for sep in ("·", "・", "-"):
        if sep in market:
            return market.split(sep)[-1].strip()
    return market


def parse_holdings() -> dict:
    """持仓 + 现金占比。复用 portfolio_compute(按表头解析 + 代码现算),
    不再自己按固定列offset解析(agent 重排表格也不破)。返回 positions/cash_pct/total_assets。"""
    from core import portfolio_compute
    try:
        r = portfolio_compute.compute(live=True)   # 命中plane预热的热缓存,快
    except Exception:  # noqa: BLE001
        r = {}
    positions = []
    for p in (r.get("positions") or []):
        mkt = p.get("market") or ""
        positions.append({
            "name": p.get("name"), "code": p.get("code"),
            "weight_pct": p.get("weight_pct"), "pnl_pct": p.get("pnl_pct"),
            "sector": _sector(mkt), "is_etf": _is_etf(p.get("name") or "", mkt),
        })
    return {"positions": positions, "cash_pct": r.get("cash_pct"),
            "total_assets": r.get("total_assets")}


def check(market_env: str = "shake") -> dict:
    """核对硬约束。market_env: bull|shake|bear(默认震荡,最常见且保守)。"""
    env = market_env if market_env in _SINGLE_CAP else "shake"
    h = parse_holdings()
    positions = h["positions"]
    violations: list[dict] = []

    single_cap = _SINGLE_CAP[env]
    total_cap = _TOTAL_CAP[env]

    # 1) 现金 ≥ 10%
    cash = h["cash_pct"]
    if cash is not None and cash < 10.0:
        violations.append({"rule": "现金≥10%", "severity": "high",
                           "detail": f"现金仅 {cash}%,低于 10% 弹药底线"})

    # 2) 总仓位 ≤ 环境上限
    if cash is not None:
        invested = round(100.0 - cash, 1)
        if invested > total_cap:
            violations.append({"rule": f"总仓位≤{total_cap:.0f}%({_ENV_CN[env]})",
                               "severity": "high",
                               "detail": f"持仓 {invested}%,超 {_ENV_CN[env]}上限 {total_cap:.0f}%"})

    # 3) 单票 ≤ 环境上限
    for p in positions:
        w = p["weight_pct"]
        if w is not None and w > single_cap:
            violations.append({"rule": f"单票≤{single_cap:.0f}%({_ENV_CN[env]})",
                               "severity": "medium",
                               "detail": f"{p['name']} 占 {w}%,超单票上限 {single_cap:.0f}%"})

    # 4) 同板块 ≤ 2 只(ETF/打底另计)
    by_sector: dict[str, list[str]] = {}
    for p in positions:
        if p["is_etf"]:
            continue
        by_sector.setdefault(p["sector"], []).append(p["name"])
    for sec, names in by_sector.items():
        if len(names) > 2:
            violations.append({"rule": "同板块≤2只", "severity": "medium",
                               "detail": f"{sec}: {len(names)}只({'、'.join(names)})"})

    # 5) 核心仓 ≤ 5 只(ETF/打底另计)
    core = [p["name"] for p in positions if not p["is_etf"]]
    if len(core) > 5:
        violations.append({"rule": "核心仓≤5只", "severity": "low",
                           "detail": f"核心仓 {len(core)}只(策略上限5),建议收敛"})

    return {
        "ok": not violations,
        "market_env": env,
        "violations": violations,
        "stats": {"cash_pct": cash, "invested_pct": (round(100.0 - cash, 1) if cash is not None else None),
                  "core_count": len(core), "total_count": len(positions),
                  "single_cap": single_cap, "total_cap": total_cap},
    }
