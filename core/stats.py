"""运行统计:每日模型调用次数(供前端显示,呼应 ~1000/天 预算)。

持久化到 data_dir()/stats.json(每租户独立),按本地日期分桶(跨天自动归零),
重启/刷新不清零。只保留最近 14 天,避免文件膨胀。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from core.config import data_dir


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _path():
    return data_dir() / "stats.json"


def _load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(d: dict) -> None:
    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")   # 只留最近14天
    d = {k: v for k, v in d.items() if k >= cutoff}
    try:
        _path().write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def incr_llm_call() -> None:
    d = _load()
    day = _today()
    d[day] = int(d.get(day, 0)) + 1
    _save(d)


def today_calls() -> int:
    return int(_load().get(_today(), 0))


# 自动任务(定时/告警)预算上限:达到即暂停自动任务,把剩余额度(~200)留给用户请求。
AUTO_BUDGET = 800


def auto_allowed() -> bool:
    return today_calls() < AUTO_BUDGET


def stats() -> dict:
    return {"date": _today(),
            "llm_calls_today": today_calls(),
            "budget": 1000,
            "auto_budget": AUTO_BUDGET}
