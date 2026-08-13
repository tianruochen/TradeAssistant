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

# ── P3 异动自动分析 ──
_QUOTE_CHG: dict[str, float] = {}    # code -> 当日涨跌%(用于大幅波动检测)
_PREV_ENV: str | None = None         # 上一 tick 的大盘环境(判断切换)
_FIRED: set = set()                  # (date, uid, event_key) 当天去重
_LAST_REPORT: dict[str, float] = {}  # uid -> 上次自动报告时间(冷却)
_PENDING: list[tuple[str, list]] = []  # 待 async 处理的 (uid, events)
_BIG_MOVE = 6.0        # 持仓单只当日振幅≥此值 → 异动
_REPORT_COOLDOWN = 1800.0   # 每用户自动报告冷却(30min),防刷屏烧钱


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

    # ── 路径A:全局大盘 + 热点(与用户无关,一次拉取全体共享) ──
    try:
        _last_env = market_env.classify()          # 预热全局指数缓存(腾讯源,可用)
    except Exception as exc:  # noqa: BLE001
        logger.warning("大盘环境预热失败: %r", exc)
    try:
        _last_sectors = mt.sector_heat()           # 行业热度(腾讯行业ETF涨跌;东财被墙的可用替代)
    except Exception as exc:  # noqa: BLE001
        logger.warning("板块热点预热失败: %r", exc)

    # ── 路径B:用户标的报价并集去重预热 + 各自算组合 ──
    all_users = [u for u in users.all_users() if u.get("llm_key")]
    codes: set[str] = set()
    for u in all_users:                              # 先收集所有用户持仓代码(并集:A股/ETF 6位 + 港股 5位)
        tenancy.set_user(u["uid"], None)
        try:
            for r in portfolio_compute._parse_rows():
                c = (r.get("code") or "").strip()
                if c.isdigit() and len(c) in (5, 6):
                    codes.add(c)
        except Exception:  # noqa: BLE001
            pass
    for c in codes:                                  # 每只只预热一次(多用户共享报价缓存);捕获涨跌%供异动检测
        try:
            q = mt._ks_quote(c) if len(c) == 6 else None
            if not (q and q.get("change_pct") is not None):
                q = mt._tencent_quote(c)             # ETF/港股/klineshare取不到 → 腾讯
            if q and q.get("change_pct") is not None:
                ch = float(q["change_pct"])
                _QUOTE_CHG[c] = ch * 100 if abs(ch) < 1 else ch   # 兼容比率/百分比两种口径
        except Exception:  # noqa: BLE001
            pass
    for u in all_users:                              # 报价已热 → 各用户算组合(读热缓存,快)
        uid = u["uid"]
        tenancy.set_user(uid, None)
        try:
            pf = portfolio_compute.compute(live=True)
            _PORT[uid] = (time.time(), pf)
            evs = _detect(uid, pf)                    # P3:检测异动
            if evs:
                _PENDING.append((uid, evs))
        except Exception as exc:  # noqa: BLE001
            logger.warning("组合预热失败[uid %s]: %r", uid, exc)

    global _PREV_ENV
    _PREV_ENV = (_last_env or {}).get("env")
    _stats["ticks"] += 1
    _stats["last_ok"] = time.time()
    _persist()


def _detect(uid: str, pf: dict) -> list[dict]:
    """基于热数据检测异动(纯读,不调 LLM)。返回事件列表。"""
    evs = []
    for p in (pf.get("positions") or []):
        code, name = p.get("code") or "", p.get("name") or ""
        pnl = p.get("pnl_pct")
        if pnl is not None:
            if pnl <= -15:
                evs.append({"key": f"clear:{code}", "sev": "high",
                            "desc": f"{name} 浮亏 {pnl}%,触及清仓线(≤-15%)"})
            elif pnl <= -8:
                evs.append({"key": f"halve:{code}", "sev": "med",
                            "desc": f"{name} 浮亏 {pnl}%,触及减半线(≤-8%)"})
        chg = _QUOTE_CHG.get(code)
        if chg is not None and abs(chg) >= _BIG_MOVE:
            evs.append({"key": f"move:{code}:{'up' if chg > 0 else 'dn'}", "sev": "med",
                        "desc": f"{name} 当日{'大涨' if chg > 0 else '大跌'} {chg:+.1f}%"})
    # 大盘环境切换
    cur_env = (_last_env or {}).get("env")
    if _PREV_ENV and cur_env and cur_env != _PREV_ENV:
        evs.append({"key": f"regime:{cur_env}", "sev": "high",
                    "desc": f"大盘环境切换:{_PREV_ENV}→{cur_env}"})
    return evs


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
        await _process_events()   # P3:对检测到的异动触发自动分析(带去重/冷却/预算/时段闸)
        interval = _INTRADAY_SEC if _is_trading_hours() else _OFFHOURS_SEC
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _process_events() -> None:
    """把 _poll_once 检测到的异动,经四道闸(时段/去重/冷却/预算)后触发 LLM 报告并推送。"""
    global _PENDING
    pending, _PENDING = _PENDING, []
    if not pending or not _is_trading_hours():   # 只在盘中自动分析(闭市不打扰)
        return
    from datetime import datetime
    from core import tenancy, user_settings
    from core.stats import auto_allowed
    from core.scheduler import run_job
    today = datetime.now().strftime("%Y-%m-%d")
    for k in [k for k in _FIRED if k[0] != today]:   # 清理隔日去重键,避免无界增长
        _FIRED.discard(k)
    now = time.time()
    for uid, evs in pending:
        fresh = [e for e in evs if (today, uid, e["key"]) not in _FIRED]   # 当天去重
        if not fresh:
            continue
        if now - _LAST_REPORT.get(uid, 0.0) < _REPORT_COOLDOWN:            # 30min 冷却
            continue
        tenancy.set_user(uid, None)
        if not user_settings.load(uid).get("schedule", {}).get("enabled", True):
            continue
        if not auto_allowed():                                            # 当日额度
            continue
        for e in fresh:
            _FIRED.add((today, uid, e["key"]))
        _LAST_REPORT[uid] = now
        lines = "\n".join(f"- {e['desc']}" for e in fresh[:6])
        prompt = ("【异动自动分析】刚检测到以下持仓/大盘异动,请**只针对这些异动**按交易策略快速评估并给可执行建议"
                  "(先读 read_holdings/compute_portfolio 确认实时数据,再结论):\n" + lines)
        try:
            await asyncio.wait_for(run_job("异动分析", prompt, push_external=True), timeout=300)
            logger.info("异动自动分析已推送[uid %s]: %d项", uid, len(fresh))
        except Exception as exc:  # noqa: BLE001
            logger.warning("异动自动分析失败[uid %s]: %r", uid, exc)
