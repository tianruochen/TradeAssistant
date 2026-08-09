"""大盘环境判定——按交易策略 §一「市场环境分类（仓位总阀门）」自动定牛/震荡/熊。

判定同时看 上证(vs MA250) + 创业板指(vs MA120),背离时以弱者为准(保守)。
数据走腾讯指数日线(本机东财被墙)。成交额阈值(1.5万亿/1万亿)暂无两市总量数据源,
故以均线关系为主、成交额留待接入——面板会如实标注。

环境直接喂 constraints.check(env),让单票/总仓位上限用对口径。
"""

from __future__ import annotations

import time
from typing import Any

_SH = "sh000001"    # 上证指数
_CYB = "sz399006"   # 创业板指
_cache: dict[str, tuple[float, Any]] = {}
_TTL = 600          # 指数日线 10 分钟缓存足够(收盘后才变)

_ENV_CN = {"bull": "牛市", "shake": "震荡市", "bear": "熊市"}


def _index_closes(sym: str) -> list[float] | None:
    hit = _cache.get(sym)
    if hit and hit[0] > time.time() - _TTL:
        return hit[1]
    import httpx
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,320,qfq"
    try:
        data = httpx.get(url, timeout=10, follow_redirects=True).json()["data"][sym]
        rows = data.get("day") or data.get("qfqday") or []
        closes = [float(r[2]) for r in rows]
    except Exception:
        return None
    if closes:
        _cache[sym] = (time.time(), closes)
    return closes or None


def _ma(closes: list[float], n: int) -> float | None:
    return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None


def classify() -> dict:
    """返回 {env, env_cn, detail, indices, note, market_state}。数据缺失时 env=None。"""
    try:
        from core.tools.market_tools import _market_state
        ms = _market_state()
    except Exception:
        ms = ""
    sh = _index_closes(_SH)
    cyb = _index_closes(_CYB)
    if not sh or not cyb:
        return {"env": None, "env_cn": None, "detail": "指数数据暂不可用(腾讯接口异常)",
                "indices": {}, "note": "无法判定,建议按震荡市保守口径", "market_state": ms}

    sh_c, sh_ma = sh[-1], _ma(sh, 250)
    cyb_c, cyb_ma = cyb[-1], _ma(cyb, 120)

    def _mas(closes: list[float], cur: float, key_n: int) -> dict:
        prev = closes[-2] if len(closes) >= 2 else cur
        d = {"close": round(cur, 2),
             "change_pct": (round((cur - prev) / prev * 100, 2) if prev else None)}  # 今日涨跌幅
        for n in (20, 60, 120, 250):
            m = _ma(closes, n)
            d[f"MA{n}"] = m
            d[f"dev{n}"] = (round((cur - m) / m * 100, 2) if m else None)
        d["key_ma"] = key_n                      # 该指数判定所用的关键均线(上证250/创业板120)
        return d

    indices = {"上证": _mas(sh, sh_c, 250), "创业板指": _mas(cyb, cyb_c, 120)}

    if sh_ma is None or cyb_ma is None:
        return {"env": None, "env_cn": None, "detail": "历史不足以算 MA250/MA120",
                "indices": indices, "note": "按震荡市保守口径", "market_state": ms}

    sh_bull = sh_c > sh_ma
    cyb_bull = cyb_c > cyb_ma
    sh_dev = (sh_c - sh_ma) / sh_ma

    # 交易策略 §一:上证「围绕250日线(偏离<5%)」= 震荡市,是判定的中间带,优先于略上/略下。
    # 牛市需上证明显站上250线且创业板站上MA120;熊市需上证明显跌破250线(且量能萎缩,此处量能待接入)。
    if abs(sh_dev) < 0.05:
        env = "shake"                              # 围绕250线 → 震荡(不论略上略下)
    elif sh_dev >= 0.05 and cyb_bull:
        env = "bull"                               # 上证明显在250线上方 且 创业板站上MA120
    elif sh_dev <= -0.05 and not cyb_bull:
        env = "bear"                               # 上证明显跌破250线 且 创业板破MA120
    else:
        env = "shake"                              # 背离/一强一弱 → 以弱者为准,降级震荡

    detail = (f"上证{'站上' if sh_bull else '跌破'}MA250({sh_dev * 100:+.1f}%,{'±5%内→围绕250线' if abs(sh_dev) < 0.05 else '明显偏离'})、"
              f"创业板指{'站上' if cyb_bull else '跌破'}MA120")
    return {"env": env, "env_cn": _ENV_CN[env], "detail": detail, "indices": indices,
            "note": "均线口径(成交额阈值待接入两市总量,牛/熊极端待量能确认);上证±5%内按震荡",
            "market_state": ms}
