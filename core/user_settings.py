"""每用户设置(推送 key + 定时任务配置 + 降噪偏好),存 data/users/<uid>/settings.json。

- 推送 key 用户级(各用各的 Server酱/PushPlus/群机器人),不再全局。
- 定时任务用户级可配置:总开关 + 各类开关;各用户用自己的 LLM key/数据/推送跑。
- 降噪:盘中监控默认只进网页不推手机,风险/进度/周末/告警才推。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULTS = {
    "push": {"serverchan": "", "pushplus": "", "feishu_webhook": "", "wechat_webhook": ""},
    "schedule": {"enabled": True, "intraday": True, "deep": True, "progress": True, "weekend": True},
    "notify": {"push_intraday": False},   # 盘中监控是否推手机(默认否=降噪);其余重要项一律推
    "custom_jobs": [],   # 用户自建定时任务:[{id,name,time:"HH:MM",prompt,enabled,push}]
}


def _path(uid: str) -> Path:
    return ROOT / "data" / "users" / uid / "settings.json"


def _merge(base: dict, over: dict) -> dict:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(uid: str) -> dict:
    d = copy.deepcopy(DEFAULTS)
    p = _path(uid)
    if p.exists():
        try:
            _merge(d, json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return d


def save(uid: str, partial: dict) -> dict:
    d = load(uid)
    _merge(d, partial or {})
    p = _path(uid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d
