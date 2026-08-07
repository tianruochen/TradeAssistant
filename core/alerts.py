"""价格事件触发：交易时段轮询持仓/观察池价格，触及止损/买点/突破即唤醒 Alpha。

借鉴智谱 Loop 的"外部事件触发"——不只定时，价格破位/到买点是分钟级的，事件驱动才不错过。
配置见 data/alerts.md。凭据无关；价格走新浪(A股)/新浪港股，绕开挂掉的东财。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from core.config import data_dir
from core.tools.market_tools import quick_price
from channels.base import reply_to_text

logger = logging.getLogger("tradeagent.alerts")


def _parse() -> list[dict]:
    """解析 data/alerts.md：`代码 名称 类型 价格 说明`（# 注释/空行忽略）。"""
    p = data_dir() / "alerts.md"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(">") or line.startswith("格式"):
            continue
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        code, name, typ, price = parts[0], parts[1], parts[2], parts[3]
        if not re.fullmatch(r"\d{4,6}", code) or typ not in ("stop", "buy", "break"):
            continue
        try:
            thr = float(price)
        except ValueError:
            continue
        out.append({"code": code, "name": name, "type": typ, "price": thr,
                    "desc": parts[4] if len(parts) > 4 else ""})
    return out


def _triggered(typ: str, px: float, thr: float) -> bool:
    if typ in ("stop", "buy"):
        return px <= thr          # 跌破止损 / 回落到买点
    return px >= thr              # break 突破


def _in_trading_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= hm <= (11 * 60 + 30) or (13 * 60) <= hm <= (15 * 60)


def check_once(fired: set[tuple[str, str, str]]) -> list[dict]:
    """查一轮，返回本轮新触发的告警（并登记进 fired 去重）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    hits = []
    for a in _parse():
        key = (today, a["code"], a["type"])
        if key in fired:
            continue
        px = quick_price(a["code"])
        if px is None:
            continue
        if _triggered(a["type"], px, a["price"]):
            fired.add(key)
            hits.append({**a, "now": px})
    return hits


async def poll_loop(stop: asyncio.Event) -> None:
    fired: set[tuple[str, str, str]] = set()
    health_fired: set = set()   # 集中度/回撤/止损/硬约束告警按天去重
    label = {"stop": "跌破止损位", "buy": "触及买点", "break": "突破"}
    while not stop.is_set():
        try:
            from core import tenancy
            tenancy.adopt_owner()   # 绑定业主租户:读业主持仓、写业主通知(网页可见)
            if _in_trading_hours(datetime.now()):
                # 纯代码风险扫描(集中度/回撤/止损/硬约束)——不吃 LLM 预算,总是先跑
                try:
                    from core import portfolio_health
                    for it in portfolio_health.scan_and_notify(health_fired):
                        logger.info("风险告警: [%s] %s", it["label"], it["detail"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("风险扫描失败: %s", exc)
                from core.stats import auto_allowed
                if not auto_allowed():
                    logger.info("预算保护:今日调用已达自动上限,暂停价格告警轮询")
                else:
                    for h in check_once(fired):
                        prompt = (
                            f"⚡价格告警:{h['name']}({h['code']}) 现价 {h['now']}，"
                            f"{label[h['type']]}(阈值 {h['price']}，{h['desc']})。"
                            "请 read_holdings + read_strategy，按体系评估该如何处理"
                            "(必要时 consult_risk / consult_hunter),给明确操作建议(含数量/触发价/二选一)。"
                        )
                        logger.info("价格告警触发: %s %s @ %s", h["name"], label[h["type"]], h["now"])
                        try:
                            text = await reply_to_text(prompt)
                            from core.scheduler import _deliver
                            await _deliver("价格告警", text)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("告警处理失败: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("alerts poll err: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=180)  # 交易时段每 3 分钟查一次
        except asyncio.TimeoutError:
            pass
