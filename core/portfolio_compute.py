"""代码计算组合总资产/盈亏——不信 holdings.md 手打的市值/总资产,用 股数×实时价 现算。

持仓列表(股数/成本/市场)仍来自 holdings.md 表格(用户官方),但市值/盈亏/总资产/仓位%
一律由代码计算,现价优先取实时(klineshare/新浪),港股按汇率折 CNY。治"LLM 手填数字出错"。
"""

from __future__ import annotations

import re
import time

from core.config import config, data_dir

_CACHE: dict = {}      # 按 uid 键:{uid: (ts, data)},多用户不互相冲刷
_TTL = 120.0           # 组合缓存 120s(后台每60s预热→始终命中);侧栏/工具读热缓存,不必现算


def _fx() -> float:
    from core.config import fx_hkd_cny
    try:
        return float(fx_hkd_cny())
    except (TypeError, ValueError):
        return 0.86


def _f(s) -> float | None:
    m = re.search(r"[-+]?[\d,]+(?:\.\d+)?", str(s or ""))
    return float(m.group(0).replace(",", "")) if m else None


def _colmap(cells: list[str]) -> dict:
    """按表头名映射列(不依赖固定列序/列数)——agent 可自由增删列、改列名而不破坏解析。"""
    m: dict[str, int] = {}
    for i, h in enumerate(cells):
        h = h.strip()
        if "代码" in h: m.setdefault("code", i)
        elif "股数" in h or "持股" in h or "数量" in h: m.setdefault("shares", i)
        elif "成本" in h: m.setdefault("cost", i)
        elif "现价" in h or "最新" in h: m.setdefault("price", i)
        elif "市值" in h: m.setdefault("mv", i)
        elif "市场" in h or "板块" in h: m.setdefault("market", i)
        elif "标的" in h or "名称" in h or "名字" in h: m.setdefault("name", i)
    return m


def _parse_rows() -> list[dict]:
    """解析 holdings.md 持仓表。**按表头名定位列**,兼容不同列序/列数(agent 常重排表格)。"""
    p = data_dir() / "holdings.md"
    if not p.exists():
        return []
    rows = []
    cm: dict | None = None
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            cm = None            # 离开表格 → 下张表重新识别表头
            continue
        c = [x.strip() for x in s.strip("|").split("|")]
        if all(set(x) <= set(":- ") for x in c):   # 分隔行 |:--|:--|
            continue
        if cm is None:           # 第一条管道行当表头:必须能认出 代码 + 股数/成本
            probe = _colmap(c)
            if "code" in probe and ("shares" in probe or "cost" in probe):
                cm = probe
            continue
        def cell(k):
            i = cm.get(k)
            return c[i] if (i is not None and i < len(c)) else ""
        code = cell("code").strip()
        shares = _f(cell("shares"))
        if shares is None and not code:   # 非数据行(汇总/注释)
            continue
        mkt = cell("market")
        cost_cell, px_cell = cell("cost"), cell("price")
        rows.append({"name": cell("name"), "code": code, "shares": shares, "cost": _f(cost_cell),
                     "px_col": _f(px_cell), "mv_col": _f(cell("mv")), "market": mkt,
                     "is_hk": ("港股" in mkt) or ("HK" in mkt) or ("HKD" in cost_cell) or ("HKD" in px_cell)})
    return rows


def _cash_initial() -> tuple[float | None, float | None]:
    p = data_dir() / "holdings.md"
    t = p.read_text(encoding="utf-8") if p.exists() else ""
    cm = re.search(r"现金余额.*?¥\s*([\d,]+(?:\.\d+)?)", t)
    im = re.search(r"初始本金.*?¥\s*([\d,]+(?:\.\d+)?)", t)
    cash = float(cm.group(1).replace(",", "")) if cm else None
    init = float(im.group(1).replace(",", "")) if im else None
    return cash, init


def compute(live: bool = True) -> dict:
    """算组合:每只 市值=股数×现价(港股×汇率),盈亏=市值-股数×成本,总资产=Σ市值+现金。
    现价优先实时;无代码(如未上市)或取价失败则回退表格现价列/市值列,并在 price_source 标注。"""
    now = time.time()
    from core.tenancy import CURRENT_UID
    uid = CURRENT_UID.get() or "_global"
    hit = _CACHE.get(uid)
    if live and hit and now - hit[0] < _TTL:
        return hit[1]
    fx = _fx()
    rows = _parse_rows()
    positions = []
    total_mv = 0.0
    ks_quote = None
    if live:
        try:
            from core.tools.market_tools import _ks_quote as _kq   # 只用 klineshare(快),不碰慢的新浪兜底
            ks_quote = _kq
        except Exception:  # noqa: BLE001
            ks_quote = None
    for r in rows:
        sh = r["shares"] or 0.0
        cost = r["cost"] or 0.0
        px_cny = None
        src = "stored"
        code = (r["code"] or "").strip()
        # 只对 6 位 A 股走 klineshare 实时(快,~150ms);ETF/港股 klineshare 不支持→快速失败→用快照价。
        # 关键:绝不在这里走新浪兜底(服务器被 403,每只卡~5秒,13只就是分钟级,曾导致"持仓分析"卡死)。
        if ks_quote and len(code) == 6 and code.isdigit():
            try:
                q = ks_quote(code)
            except Exception:  # noqa: BLE001
                q = None
            if q and q.get("price"):
                px_cny = q["price"] * fx if r["is_hk"] else q["price"]
                src = "live"
        if px_cny is None and r["px_col"]:   # 回退表格现价列(港股/ETF/无代码/取价失败)
            px_cny = r["px_col"] * fx if r["is_hk"] else r["px_col"]
            src = "px_col"
        cost_cny = cost * fx if r["is_hk"] else cost
        if px_cny is not None:
            mv = sh * px_cny
        elif r["mv_col"] is not None:        # 最后回退表格市值列
            mv = r["mv_col"]
            px_cny = mv / sh if sh else None
            src = "mv_col"
        else:
            mv = 0.0
        pnl = (mv - sh * cost_cny) if px_cny is not None else None
        # 负成本(日内T摊薄成负)时百分比无意义 → 不给%,只给金额
        pnl_pct = (round(pnl / (sh * cost_cny) * 100, 2)
                   if (pnl is not None and sh * cost_cny > 0) else None)
        total_mv += mv
        positions.append({
            "name": r["name"], "code": code, "shares": sh,
            "cost_cny": round(cost_cny, 3), "price_cny": round(px_cny, 3) if px_cny is not None else None,
            "market_value": round(mv), "pnl": round(pnl) if pnl is not None else None,
            "pnl_pct": pnl_pct, "price_source": src, "market": r["market"],
        })
    cash, init = _cash_initial()
    total_assets = round(total_mv + (cash or 0))
    for p in positions:
        p["weight_pct"] = round(p["market_value"] / total_assets * 100, 1) if total_assets else None
    out = {
        "positions": positions,
        "total_market_value": round(total_mv),
        "cash": cash,
        "cash_pct": round((cash or 0) / total_assets * 100, 1) if total_assets else None,
        "total_assets": total_assets,
        "initial": init,
        "computed": True,
        "fx_hkd_cny": fx,
        "as_of": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "any_live": any(p["price_source"] == "live" for p in positions),
    }
    if init:
        out["target"] = init * 2
        out["progress_pct"] = round((total_assets - init) / init * 100, 2)
        out["to_double"] = round(init * 2 - total_assets)
        out["to_double_pct"] = round((init * 2 - total_assets) / total_assets * 100, 2) if total_assets else None
    if live:
        _CACHE[uid] = (now, out)
    return out
