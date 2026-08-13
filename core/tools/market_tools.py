"""行情工具：全市场报价 / K线 / 板块资金流 / 全市场筛选。

从 digital-life market_tools 精简移植。要点：
- A 股任意 6 位代码可查（akshare 全市场快照，带缓存）。
- 港股（09988/01810 等）价为 HKD → 市值/价按 config 汇率换 CNY，不用 HKD 原值。
- market_scan：eastmoney 全市场快照失败 → 新浪兜底（列少，标 source）。
所有 handler 返回 JSON 字符串。import 本模块即注册进全局 registry。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any

from core.config import fx_hkd_cny
from core.tools.registry import registry

# ── klineshare.cn 行情源(主源,东财/新浪/腾讯为备选)。KLINESHARE_KEY 未配则跳过 ──
_KS = "https://klineshare.cn/v1"


def _ks_key() -> str:
    return os.getenv("KLINESHARE_KEY", "").strip()


def _ks_get(path: str, params: dict, cache_key: str, ttl: int):
    if not _ks_key():
        return None
    now = time.time()
    hit = _cache.get(cache_key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        import httpx
        r = httpx.get(f"{_KS}/{path}", params=params, headers={"X-API-Key": _ks_key()}, timeout=10)
        if r.status_code == 200:
            j = r.json()
            if j.get("success") is not False:
                _cache[cache_key] = (now, j)
                return j
    except Exception:
        return None
    return None


def _ks_quote(code: str) -> dict | None:
    j = _ks_get("realtime", {"symbol": code}, f"ksq:{code}", 90)   # 90s:后台每45s预热→工具多读热缓存
    d = (j or {}).get("data") or {}
    if not d.get("price"):
        return None
    q = d.get("quote") or {}
    try:
        return {"name": d.get("name") or code, "code": code, "market": "A股",
                "price": round(float(d["price"]), 3),
                "change_pct": round(float(d.get("change_percent") or 0) * 100, 2),  # realtime 是比率
                "open": d.get("open"), "high": d.get("high"), "low": d.get("low"),
                "turnover_rate": round(float(q.get("turnover_ratio") or 0), 3),
                "volume_ratio": round(float(q.get("volume_ratio") or 0), 3),
                "amount_yi": (round(float(d.get("turnover") or q.get("turnover") or 0) / 1e8, 2) or None),
                "source": "klineshare"}
    except (TypeError, ValueError):
        return None


def _ks_kline(code: str) -> dict | None:
    j = _ks_get("kline", {"symbol": code}, f"ksk:{code}", 600)
    rows = (j or {}).get("data") or []
    if len(rows) < 2:
        return None
    try:
        closes = [float(r["close"]) for r in rows]
    except (KeyError, TypeError, ValueError):
        return None
    def ma(n): return round(sum(closes[-n:]) / n, 3) if len(closes) >= n else None
    recent = [{"日期": r.get("date"), "开盘": r.get("open"), "收盘": r.get("close"),
               "最高": r.get("high"), "最低": r.get("low"), "成交量": r.get("volume", 0)} for r in rows[-10:]]
    return {"symbol": code, "period": "daily", "last_close": closes[-1],
            "MA5": ma(5), "MA10": ma(10), "MA20": ma(20), "MA60": ma(60), "recent10": recent}

_cache: dict[str, tuple[float, Any]] = {}
_TTL = 60
_SPOT_OK_TTL = 180    # 全市场快照成功缓存(盘中3分钟够用,少打慢接口)
_SPOT_FAIL_TTL = 90   # 失败也缓存,避免每次工具调用都白等死接口重试

# 持仓里的港股别名 → 港股代码（quote 用）
_HK = {
    "阿里巴巴": "09988", "阿里": "09988", "09988": "09988",
    "小米": "01810", "小米集团": "01810", "01810": "01810",
}


def _j(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)


def _trading_days() -> set:
    """klineshare 交易日历(近+未来+最新);无 key/失败返回空集(调用方退化为工作日判断)。"""
    j = _ks_get("calendar", {}, "kscal", 6 * 3600)
    d = (j or {}).get("data") or {}
    days = set(d.get("recent_trading_days") or []) | set(d.get("future_trading_days") or [])
    if d.get("latest_trading_day"):
        days.add(d["latest_trading_day"])
    return days


def is_trading_day(date_str: str | None = None) -> bool:
    """是否 A 股交易日(用真实日历,能识别节假日);无日历时退化为"工作日即交易日"。"""
    ds = date_str or datetime.now().strftime("%Y-%m-%d")
    days = _trading_days()
    if not days:
        try:
            return datetime.strptime(ds, "%Y-%m-%d").weekday() < 5
        except ValueError:
            return True
    return ds in days


def _market_state() -> str:
    """A股此刻交易状态,给数据时效标注用——现价是实时还是收盘价。"""
    now = datetime.now()
    if not is_trading_day():
        return "休市(非交易日/节假日,现价=上一交易日收盘)"
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 15:
        return "盘前(现价=昨日收盘)"
    if 9 * 60 + 30 <= hm <= 11 * 60 + 30 or 13 * 60 <= hm <= 15 * 60:
        return "盘中(实时)"
    if 11 * 60 + 30 < hm < 13 * 60:
        return "午间休市(现价=上午收盘)"
    if hm > 15 * 60:
        return "已收盘(现价=今日收盘)"
    return "集合竞价"


def _stamp(payload: dict, source: str) -> dict:
    """给每个工具结果盖「来源+时间+市场状态」戳,杜绝把旧数据当实时用。"""
    payload["source"] = source
    payload["as_of"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload["market_state"] = _market_state()
    return payload


def _a_spot():
    """全 A 股实时快照（eastmoney），带正/负缓存。失败返回 None。
    负缓存:接口挂时也缓存 None 一段时间,避免每次工具调用都白等 6s+ 重试死接口。"""
    now = time.time()
    hit = _cache.get("A")
    if hit is not None:
        age = now - hit[0]
        if (hit[1] is not None and age < _SPOT_OK_TTL) or (hit[1] is None and age < _SPOT_FAIL_TTL):
            return hit[1]
    df = None
    try:
        import akshare as ak
        d = ak.stock_zh_a_spot_em()
        if d is not None and not d.empty:
            df = d
    except Exception:
        df = None
    _cache["A"] = (now, df)
    return df


def _scan_spot():
    """给 market_scan 用：eastmoney 优先，失败降级新浪。带正/负缓存(避免连撞死接口拖慢 agent)。"""
    now = time.time()
    hit = _cache.get("scan")
    if hit is not None:
        age = now - hit[0]
        if (hit[1][0] is not None and age < _SPOT_OK_TTL) or (hit[1][0] is None and age < _SPOT_FAIL_TTL):
            return hit[1]
    result = (None, None)
    df = _a_spot()
    if df is not None and not df.empty:
        result = (df, "eastmoney")
    else:
        try:
            import akshare as ak
            sdf = ak.stock_zh_a_spot()
            if sdf is not None and not sdf.empty:
                sdf = sdf.copy()
                sdf["代码"] = sdf["代码"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True)
                for col in ("量比", "换手率", "总市值", "市盈率-动态"):
                    if col not in sdf.columns:
                        sdf[col] = float("nan")
                result = (sdf, "sina")
        except Exception:
            result = (None, None)
    _cache["scan"] = (now, result)
    return result


# ────────────────────────── sense_stock_quote ──────────────────────────

def _handle_quote(args: dict) -> str:
    raw = (args.get("symbols") or "").strip()
    if not raw:
        return _j({"error": "symbols 不能为空"})
    fx = fx_hkd_cny()
    out = []
    df = None   # 惰性:仅当 klineshare 未命中(名称查询/失败)才拉东财快照
    for q in [s.strip() for s in raw.split(",") if s.strip()]:
        # 港股
        hk = _HK.get(q)
        if hk:
            price = _hk_price(hk)
            if price is not None:
                out.append({"name": q, "code": hk, "market": "港股",
                            "price_hkd": price, "price_cny": round(price * fx, 3),
                            "note": f"HKD价×{fx}汇率=CNY"})
                continue
        # A 股：6 位代码或名称
        code = q if re.fullmatch(r"\d{6}", q) else None
        if code:                              # 主源 klineshare(6位代码)
            ksq = _ks_quote(code)
            if ksq:
                out.append(ksq)
                continue
        row = None
        if df is None:
            _d = _a_spot()
            df = _d if _d is not None else False   # 失败标 False,本轮不再重复拉
        if df is not False and not df.empty:
            if code is None:
                hit = df[df["名称"].astype(str) == q]
                if not hit.empty:
                    code = str(hit.iloc[0]["代码"])
            if code is not None:
                r = df[df["代码"].astype(str) == code]
                if not r.empty:
                    row = r.iloc[0]
        if row is not None:
            out.append({
                "name": str(row.get("名称", code)), "code": code, "market": "A股",
                "price": _f(row, "最新价"), "change_pct": _f(row, "涨跌幅"),
                "amount_yi": round(_f(row, "成交额") / 1e8, 2),
                "turnover_rate": _f(row, "换手率"), "volume_ratio": _f(row, "量比"),
                "market_cap_yi": round(_f(row, "总市值") / 1e8, 2),
            })
        elif code is not None:
            # 东财快照没命中（多因东财接口挂）→ 新浪单只兜底
            sq = _a_quote_sina(code)
            if sq:
                out.append({"name": sq["name"] or code, "code": code, "market": "A股",
                            "price": sq["price"], "change_pct": sq["change_pct"],
                            "open": sq["open"], "high": sq["high"], "low": sq["low"],
                            "source": "sina", "note": "东财挂,新浪兜底(无量比/换手/市值)"})
            else:
                out.append({"query": q, "error": "未找到（A股传6位代码/名称；港股仅支持已配置的）"})
        else:
            out.append({"query": q, "error": "未找到（A股传6位代码/名称；港股仅支持已配置的）"})
    return _j(_stamp({"quotes": out}, "eastmoney(A股快照)+sina/港股兜底"))


def _hk_price(code: str) -> float | None:
    """新浪港股实时价（HKD）。"""
    try:
        import httpx
        r = httpx.get(f"https://hq.sinajs.cn/list=rt_hk{code}",
                      headers={"Referer": "https://finance.sina.com.cn"}, timeout=8)
        parts = r.text.split('"')[1].split(",")
        return float(parts[6]) if len(parts) > 6 else None
    except Exception:
        return None


def _f(row, col, default=0.0) -> float:
    try:
        v = row.get(col)
        if v is None or str(v) == "":
            return default
        fv = float(v)
        return default if fv != fv else round(fv, 3)
    except (TypeError, ValueError):
        return default


def _a_price_sina(code: str) -> float | None:
    """A 股单只实时价（新浪，绕开挂掉的东财 spot），供告警轮询用。"""
    q = _a_quote_sina(code)
    return q.get("price") if q else None


def _a_quote_sina(code: str) -> dict | None:
    """A 股单只完整报价（新浪）：price/prev_close/change_pct/open/high/low。东财挂时的兜底。"""
    pre = "sh" if str(code).startswith("6") else "sz"
    try:
        import httpx
        r = httpx.get(f"https://hq.sinajs.cn/list={pre}{code}",
                      headers={"Referer": "https://finance.sina.com.cn"}, timeout=8)
        p = r.text.split('"')[1].split(",")
        # 新浪A股: [0名称,1今开,2昨收,3现价,4最高,5最低,...]
        if len(p) < 6 or not p[3]:
            return None
        price, prev = float(p[3]), float(p[2])
        chg = round((price / prev - 1) * 100, 2) if prev else 0.0
        return {"name": p[0], "price": price, "prev_close": prev, "change_pct": chg,
                "open": float(p[1] or 0), "high": float(p[4] or 0), "low": float(p[5] or 0)}
    except Exception:
        return None


def quick_price(code: str) -> float | None:
    """单只最新价：5 位=港股(HKD)，6 位=A股。告警轮询用,轻量。优先 klineshare,回退新浪。"""
    c = str(code).strip()
    if len(c) == 5:
        return _hk_price(c)
    ksq = _ks_quote(c)
    if ksq:
        return ksq["price"]
    return _a_price_sina(c)


# ────────────────────────── sense_stock_kline ──────────────────────────

def _handle_kline(args: dict) -> str:
    sym = (args.get("symbol") or "").strip()
    if not re.fullmatch(r"\d{6}", sym):
        return _j({"error": "symbol 需为 6 位 A 股代码"})
    period = args.get("period") or "daily"
    count = min(int(args.get("count") or 60), 120)
    # 主源:klineshare(日线,含MA);未配Key/失败 → 东财(akshare) → 腾讯兜底
    if period in ("daily", "day", ""):
        ks = _ks_kline(sym)
        if ks:
            return _j(_stamp(ks, "klineshare"))
    # 主源:东财(akshare);挂了 → 腾讯兜底
    try:
        import akshare as ak
        start = (datetime.now() - timedelta(days=(count + 60) * 2)).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(symbol=sym, period=period, start_date=start, adjust="qfq")
        if df is not None and not df.empty:
            df = df.tail(count)
            closes = df["收盘"].astype(float).tolist()
            def ma(n): return round(sum(closes[-n:]) / n, 3) if len(closes) >= n else None
            recent = df.tail(10)[["日期", "开盘", "收盘", "最高", "最低", "成交量"]].to_dict("records")
            return _j(_stamp({"symbol": sym, "period": period, "last_close": closes[-1],
                       "MA5": ma(5), "MA10": ma(10), "MA20": ma(20), "MA60": ma(60), "recent10": recent},
                       "eastmoney"))
    except Exception:
        pass
    tx = _kline_tencent(sym, period, count)
    if tx:
        return _j(tx)
    return _j({"error": "K线获取失败(东财/腾讯均异常)"})


def _kline_tencent(sym: str, period: str, count: int) -> dict | None:
    """腾讯 K 线兜底(绕开挂掉的东财)。返回与东财路径同构的 dict。"""
    pre = "sh" if str(sym).startswith("6") else "sz"
    per = {"daily": "day", "weekly": "week", "monthly": "month"}.get(period, "day")
    try:
        import httpx
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={pre}{sym},{per},,,{min(count + 60, 320)},qfq")
        data = httpx.get(url, timeout=10, follow_redirects=True).json()["data"][f"{pre}{sym}"]
        rows = data.get(f"qfq{per}") or data.get(per) or []
        if not rows:
            return None
        # 用全部拉取的行算均线(需 ≥60 才有 MA60);recent10 只取最后 10 根
        closes = [float(r[2]) for r in rows]
        def ma(n): return round(sum(closes[-n:]) / n, 3) if len(closes) >= n else None
        recent = [{"日期": r[0], "开盘": float(r[1]), "收盘": float(r[2]),
                   "最高": float(r[3]), "最低": float(r[4]), "成交量": float(r[5]) if len(r) > 5 else 0}
                  for r in rows[-10:]]
        return _stamp({"symbol": sym, "period": period, "last_close": closes[-1],
                "MA5": ma(5), "MA10": ma(10), "MA20": ma(20), "MA60": ma(60), "recent10": recent},
                "tencent")
    except Exception:
        return None


# ────────────────────────── sense_sector_flow ──────────────────────────

def _handle_sector_flow(args: dict) -> str:
    indicator = (args.get("indicator") or "今日").strip()
    top_n = min(int(args.get("top_n") or 15), 30)
    try:
        import akshare as ak
        df = ak.stock_sector_fund_flow_rank(indicator=indicator, sector_type="行业资金流")
    except Exception as exc:
        return _j({"error": f"板块资金流暂不可用（东财接口异常）: {str(exc)[:80]}"})
    if df is None or df.empty:
        return _j({"error": "无数据"})
    cmap = {}
    for c in df.columns:
        if "名称" in c: cmap[c] = "name"
        elif "主力净流入" in c and "占比" not in c: cmap[c] = "main_inflow"
        elif c == "今日涨跌幅" or c == "涨跌幅": cmap[c] = "change_pct"
    df = df.rename(columns=cmap)
    top = [{"name": str(r.get("name", "")), "change_pct": _f(r, "change_pct"),
            "main_inflow_yi": round(_f(r, "main_inflow") / 1e8, 2)} for _, r in df.head(top_n).iterrows()]
    return _j(_stamp({"indicator": indicator, "inflow_top": top}, "eastmoney(行业资金流)"))


# ────────────────────────── sense_market_scan ──────────────────────────

def _handle_market_scan(args: dict) -> str:
    df, source = _scan_spot()
    if df is None or df.empty:
        return _j({"error": "全市场行情暂不可用（东财/新浪均异常）"})
    sort_by = (args.get("sort_by") or "涨幅").strip()
    top_n = min(int(args.get("top_n") or 20), 30)
    try:
        min_amt = float(args.get("min_amount_yi") if args.get("min_amount_yi") is not None else 2.0)
    except (TypeError, ValueError):
        min_amt = 2.0
    board = (args.get("board") or "全部").strip()
    note = ""
    if source == "sina" and sort_by in ("量比", "换手"):
        note = f"新浪降级，无{sort_by}，改用涨幅"; sort_by = "涨幅"
    elif source == "sina":
        note = "新浪降级，量比/换手/市值缺失"
    col = {"涨幅": "涨跌幅", "量比": "量比", "换手": "换手率", "成交额": "成交额"}.get(sort_by, "涨跌幅")
    try:
        import pandas as pd
        w = df.copy()
        w = w[~w["名称"].astype(str).str.contains("ST|退", na=False)]
        code_s = w["代码"].astype(str)
        if board == "创业板": w = w[code_s.str.startswith("300")]
        elif board == "科创": w = w[code_s.str.startswith("688")]
        elif board == "主板": w = w[code_s.str.startswith(("60", "00"))]
        w = w[pd.to_numeric(w["成交额"], errors="coerce").fillna(0) >= min_amt * 1e8]
        w = w.assign(_k=pd.to_numeric(w[col], errors="coerce")).sort_values("_k", ascending=False, na_position="last")
    except Exception as exc:
        return _j({"error": f"筛选失败: {exc}"})
    rows = [{"name": str(r.get("名称", "")), "code": str(r.get("代码", "")),
             "price": _f(r, "最新价"), "change_pct": _f(r, "涨跌幅"), "volume_ratio": _f(r, "量比"),
             "turnover_rate": _f(r, "换手率"), "amount_yi": round(_f(r, "成交额") / 1e8, 2),
             "market_cap_yi": round(_f(r, "总市值") / 1e8, 2)} for _, r in w.head(top_n).iterrows()]
    return _j(_stamp({"sort_by": sort_by, "board": board, "note": note,
               "count": len(rows), "results": rows}, source))


# ────────────────────────── 注册 ──────────────────────────

def register() -> None:
    registry.register("sense_stock_quote", {
        "name": "sense_stock_quote",
        "description": "查实时行情。A股传6位代码或名称，港股传名称(阿里/小米)。多只逗号分隔。港股返回HKD价与CNY折算价。",
        "parameters": {"type": "object", "properties": {
            "symbols": {"type": "string", "description": "代码/名称，逗号分隔"}}, "required": ["symbols"]},
    }, _handle_quote)

    registry.register("sense_stock_kline", {
        "name": "sense_stock_kline",
        "description": "查A股K线+均线(MA5/10/20/60)。symbol传6位代码。",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"}, "period": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
            "count": {"type": "integer"}}, "required": ["symbol"]},
    }, _handle_kline)

    registry.register("sense_sector_flow", {
        "name": "sense_sector_flow",
        "description": "行业板块资金流排名(主力净流入)。用于判断热门板块方向。",
        "parameters": {"type": "object", "properties": {
            "indicator": {"type": "string", "enum": ["今日", "5日", "10日"]}, "top_n": {"type": "integer"}}},
    }, _handle_sector_flow)

    registry.register("sense_market_scan", {
        "name": "sense_market_scan",
        "description": "全市场A股筛选(按涨幅/量比/换手/成交额排序,剔除ST与低成交额)。用于挖掘机会标的。",
        "parameters": {"type": "object", "properties": {
            "sort_by": {"type": "string", "enum": ["涨幅", "量比", "换手", "成交额"]},
            "top_n": {"type": "integer"}, "min_amount_yi": {"type": "number"},
            "board": {"type": "string", "enum": ["全部", "主板", "创业板", "科创"]}}},
    }, _handle_market_scan)


register()
