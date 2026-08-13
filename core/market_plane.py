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
_last_env: dict | None = None        # 路径A:全局大盘环境
_last_sectors: dict | None = None    # 路径A:全局热点(板块资金流)
_stats = {"ticks": 0, "last_ok": 0.0, "last_err": ""}


def get_portfolio(uid: str) -> dict | None:
    """读某用户的热组合快照;无/过期返回 None(调用方自行回源)。"""
    e = _PORT.get(uid)
    if e and time.time() - e[0] < _PORT_TTL:
        return e[1]
    return None


def get_env() -> dict | None:
    return _last_env


def get_sectors() -> dict | None:
    return _last_sectors


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
        data = {"env": _last_env, "sectors": _last_sectors,
                "port": {u: v[1] for u, v in _PORT.items()},
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:  # noqa: BLE001
        logger.warning("快照落盘失败: %r", exc)


def _load_persisted() -> None:
    """启动时用上次落盘的快照热身(重启不冷启)。"""
    global _last_env, _last_sectors
    try:
        p = _snapshot_path()
        if not p.exists():
            return
        d = json.loads(p.read_text(encoding="utf-8"))
        _last_env = d.get("env")
        _last_sectors = d.get("sectors")
        now = time.time()
        for uid, port in (d.get("port") or {}).items():
            _PORT[uid] = (now, port)   # 落盘值先当热用,下个 tick 会刷新
        logger.info("行情平面:载入上次快照(%d 用户组合)", len(_PORT))
    except Exception as exc:  # noqa: BLE001
        logger.warning("快照载入失败: %r", exc)


def _poll_once() -> None:
    """一个 tick,两条并行路径:
    路径A(全局共享,拉一次所有人共用):大盘环境 + 热点板块资金流。
    路径B(按用户):各用户持仓个股报价(按 symbol 并集去重预热)+ 算好组合。"""
    global _last_env, _last_sectors
    import json as _json
    from core import users, tenancy, market_env, portfolio_compute
    from core.tools import market_tools as mt

    # ── 路径A:全局大盘(与用户无关,一次拉取全体共享) ──
    try:
        _last_env = market_env.classify()          # 预热全局指数缓存(腾讯源,可用)
    except Exception as exc:  # noqa: BLE001
        logger.warning("大盘环境预热失败: %r", exc)
    # 注:热点板块资金流走东财、在服务器被墙(会挂起无超时)→ 暂不在平面预热,由工具按需取;
    #     待接入可用的板块数据源(P4)再纳入路径A的全局热点预热。

    # ── 路径B:用户标的报价并集去重预热 + 各自算组合 ──
    all_users = [u for u in users.all_users() if u.get("llm_key")]
    codes: set[str] = set()
    for u in all_users:                              # 先收集所有用户持仓代码(并集)
        tenancy.set_user(u["uid"], None)
        try:
            for r in portfolio_compute._parse_rows():
                c = (r.get("code") or "").strip()
                if len(c) == 6 and c.isdigit():
                    codes.add(c)
        except Exception:  # noqa: BLE001
            pass
    for c in codes:                                  # 每只只预热一次(多用户共享报价缓存)
        try:
            mt._ks_quote(c)
        except Exception:  # noqa: BLE001
            pass
    for u in all_users:                              # 报价已热 → 各用户算组合(读热缓存,快)
        uid = u["uid"]
        tenancy.set_user(uid, None)
        try:
            _PORT[uid] = (time.time(), portfolio_compute.compute(live=True))
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
            await asyncio.wait_for(asyncio.to_thread(_poll_once), timeout=120)   # 硬超时,单tick再慢也不卡死循环
        except asyncio.TimeoutError:
            logger.warning("行情平面 tick 超时(>120s),跳过本轮")
        except Exception as exc:  # noqa: BLE001
            _stats["last_err"] = repr(exc)[:120]
            logger.warning("行情平面 tick 异常: %r", exc)
        interval = _INTRADAY_SEC if _is_trading_hours() else _OFFHOURS_SEC
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
