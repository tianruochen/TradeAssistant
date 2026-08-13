"""行情数据平面(L2):后台按交易时段轮询,预取行情并算好组合,存内存(+落盘)。

消费层(工具/侧栏/绩效/定时任务)读本地热数据,请求路径里不再现查行情 API。
- 市场数据全局共享(报价/指数按 symbol 键,多用户去重);派生组合按 uid。
- 盘中每 45s 预热;盘后/非交易日暂停(用最后一次收盘快照)。
- 落盘 data/market/snapshot.json,重启不冷启。

设计见 docs/architecture/market-data-plane.md。P1+P2:预热 + Derived 缓存。P3(异动自动分析)后续。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

logger = logging.getLogger("tradeagent.mplane")

_INTRADAY_SEC = 45      # 盘中预热间隔
_OFFHOURS_SEC = 300     # 盘后/午休预热间隔(慢)
_PORT_TTL = 150.0       # 派生组合缓存有效期(>2×盘中间隔,始终命中)

# 派生组合:uid -> (ts, portfolio_dict)。市场数据本身缓存在 market_tools/market_env 的按-symbol 缓存里。
_PORT: dict[str, tuple[float, dict]] = {}
_last_env: dict | None = None
_stats = {"ticks": 0, "last_ok": 0.0, "last_err": ""}


def get_portfolio(uid: str) -> dict | None:
    """读某用户的热组合快照;无/过期返回 None(调用方自行回源)。"""
    e = _PORT.get(uid)
    if e and time.time() - e[0] < _PORT_TTL:
        return e[1]
    return None


def get_env() -> dict | None:
    return _last_env


def stats() -> dict:
    return dict(_stats)


def _is_trading_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    # 9:25–11:35 / 12:55–15:05(含集合竞价与收盘缓冲)
    return (565 <= hm <= 695) or (775 <= hm <= 905)


def _snapshot_path():
    from core.config import ROOT
    return ROOT / "data" / "market" / "snapshot.json"


def _persist() -> None:
    try:
        p = _snapshot_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"env": _last_env, "port": {u: v[1] for u, v in _PORT.items()},
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:  # noqa: BLE001
        logger.warning("快照落盘失败: %r", exc)


def _load_persisted() -> None:
    """启动时用上次落盘的快照热身(重启不冷启)。"""
    global _last_env
    try:
        p = _snapshot_path()
        if not p.exists():
            return
        d = json.loads(p.read_text(encoding="utf-8"))
        _last_env = d.get("env")
        now = time.time()
        for uid, port in (d.get("port") or {}).items():
            _PORT[uid] = (now, port)   # 落盘值先当热用,下个 tick 会刷新
        logger.info("行情平面:载入上次快照(%d 用户组合)", len(_PORT))
    except Exception as exc:  # noqa: BLE001
        logger.warning("快照载入失败: %r", exc)


def _poll_once() -> None:
    """一个 tick:刷大盘环境(全局)+ 每个已配 key 用户的组合(顺带预热其持仓报价缓存)。"""
    global _last_env
    from core import users, tenancy, market_env, portfolio_compute
    try:
        _last_env = market_env.classify()   # 预热全局指数缓存
    except Exception as exc:  # noqa: BLE001
        logger.warning("大盘环境预热失败: %r", exc)
    for u in users.all_users():
        if not u.get("llm_key"):
            continue
        uid = u["uid"]
        tenancy.set_user(uid, None)
        try:
            # compute(live=True) 会逐只走 klineshare 预热报价缓存,并算好组合;结果进 _PORT
            p = portfolio_compute.compute(live=True)
            _PORT[uid] = (time.time(), p)
        except Exception as exc:  # noqa: BLE001
            logger.warning("组合预热失败[uid %s]: %r", uid, exc)
    _stats["ticks"] += 1
    _stats["last_ok"] = time.time()
    _persist()


async def poll_loop(stop: asyncio.Event) -> None:
    _load_persisted()
    logger.info("行情数据平面启动")
    while not stop.is_set():
        try:
            await asyncio.to_thread(_poll_once)   # 同步取数放线程,不阻塞事件循环
        except Exception as exc:  # noqa: BLE001
            _stats["last_err"] = repr(exc)[:120]
            logger.warning("行情平面 tick 异常: %r", exc)
        interval = _INTRADAY_SEC if _is_trading_hours() else _OFFHOURS_SEC
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
