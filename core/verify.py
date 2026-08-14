"""输出数字校验:比对模型正文里提到的持仓「现价/涨跌%」与 compute_portfolio 的权威值,
抓出凭空编造(如把万兴 54.86/+0.13% 写成 53.99/-3.46%)。返回更正清单,由 chat 附到答案末尾。

只校持仓标的(有权威实时数据),保守判定(明显不符才报),避免误伤。
"""

from __future__ import annotations

import re

_PRICE_TOL = 0.03    # 现价偏离 >3% 视为不符
_PCT_TOL = 0.8       # 百分比与「今日涨跌」「持仓浮亏」都差 >0.8pp 视为不符


def check_holdings_numbers(text: str, positions: list[dict]) -> list[str]:
    if not text or not positions:
        return []
    issues: list[str] = []
    for p in positions:
        name = (p.get("name") or "").strip()
        price = p.get("price_cny")
        today = p.get("change_pct_today")
        pnl = p.get("pnl_pct")
        if not name or price is None or name not in text:
            continue
        # 在每次提到该标的名之后的小窗口里找「数字（±X%）」这种价+涨跌的写法
        flagged = False
        for m in re.finditer(re.escape(name), text):
            win = text[m.end(): m.end() + 32]
            pm = re.search(r"(\d+(?:\.\d+)?)\s*[（(]\s*([+\-]?\d+(?:\.\d+)?)\s*%", win)
            if not pm:
                continue
            v_price = float(pm.group(1))
            v_pct = float(pm.group(2))
            # 价格:同量级(0.3x~3x,排除误匹配股数/百分比)且偏离过大
            bad_price = (price * 0.3 < v_price < price * 3) and abs(v_price - price) / max(abs(price), 1e-6) > _PRICE_TOL
            near_today = today is not None and abs(v_pct - today) <= _PCT_TOL
            near_pnl = pnl is not None and abs(v_pct - pnl) <= _PCT_TOL
            bad_pct = not (near_today or near_pnl)
            if bad_price or bad_pct:
                truth = [f"现价 {price}"]
                if today is not None:
                    truth.append(f"今日 {today:+.2f}%")
                if pnl is not None:
                    truth.append(f"持仓浮亏 {pnl:+.2f}%")
                issues.append(f"{name}:正文「{pm.group(0).strip()}」与实时不符 → 实为 {' / '.join(truth)}")
                flagged = True
            if flagged:
                break   # 每只只报一次
    return issues
