"""多租户上下文:用 contextvar 携带"当前请求的用户 + 其 LLM Key"。

- data_dir() 据此指向 data/users/<uid>/(每用户独立持仓/策略/对话/告警/历史)。
- model_config() 据此用该用户自带的 Key(自带Key SaaS)。
- 无用户上下文时(定时任务/CLI)回退到全局 data/ 与 env Key。
异步任务内 contextvar 会随 await 传播,故在请求入口 set 一次即可全链路生效。
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CURRENT_UID: ContextVar = ContextVar("uid", default=None)
CURRENT_KEY: ContextVar = ContextVar("llm_key", default=None)
CURRENT_MODEL: ContextVar = ContextVar("model", default=None)


def set_user(uid: str | None, llm_key: str | None = None, model: str | None = None) -> None:
    CURRENT_UID.set(uid)
    CURRENT_KEY.set(llm_key)
    CURRENT_MODEL.set(model)


def adopt_owner() -> str | None:
    """后台循环(定时任务/价格告警/风险扫描)无请求上下文,默认落到全局 data/,
    登录用户在网页看不到其产出。若配置了 TA_OWNER_UID,则把后台循环绑定到该业主租户,
    使读持仓/写通知都落在业主目录——网页登录即可见。返回采用的 uid(未配置则 None)。
    多用户各自定时任务是后续工作;当前为单业主场景的桥接。"""
    owner = (os.getenv("TA_OWNER_UID") or "").strip()
    if not owner:
        return None
    from core import users
    u = users.get_user(owner) or {}
    set_user(owner, u.get("llm_key") or None, u.get("model") or None)
    return owner


def current_uid() -> str | None:
    return CURRENT_UID.get()


def current_key() -> str | None:
    return CURRENT_KEY.get()


def current_model() -> str | None:
    return CURRENT_MODEL.get()


def resolved_data_dir() -> Path:
    """当前用户的数据目录;无用户时回退全局 data/。"""
    uid = CURRENT_UID.get()
    d = (ROOT / "data" / "users" / uid) if uid else (ROOT / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d
