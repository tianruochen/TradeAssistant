"""配置加载：config.yaml + secrets.env（环境变量）。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_secrets() -> None:
    """把 secrets.env 读进环境变量（简单 KEY=VALUE，忽略注释/空行）。"""
    env_path = ROOT / "secrets.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


@lru_cache(maxsize=1)
def config() -> dict[str, Any]:
    _load_secrets()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    return cfg


def data_dir() -> Path:
    """当前用户数据目录(多租户);无用户上下文回退全局 data/。"""
    from core.tenancy import resolved_data_dir
    return resolved_data_dir()


def agents_dir() -> Path:
    return ROOT / "agents"


def model_config(agent_name: str | None = None) -> dict[str, Any]:
    """默认模型配置；agent 可用 agents/<name>/agent.yaml 的 model 段覆盖。
    模型分层:专家子 agent 用 model.sub_agent(更省更快);主 agent 用 model.name。
    多租户:当前用户在上下文里带了自己的 Key/模型则优先用(自带Key SaaS)。"""
    base = dict(config().get("model") or {})
    sub_model = base.pop("sub_agent", None)   # 分层模型,不作为普通字段下传
    if agent_name:
        # ① 分层:非主 agent(专家)默认降级到 sub_agent 模型
        experts = config().get("agents", {}).get("experts", [])
        primary = config().get("agents", {}).get("primary", "alpha")
        if sub_model and agent_name in experts and agent_name != primary:
            base["name"] = sub_model
        # ② 单 agent 显式覆盖(agent.yaml)优先级高于分层默认
        ay = agents_dir() / agent_name / "agent.yaml"
        if ay.exists():
            over = (yaml.safe_load(ay.read_text(encoding="utf-8")) or {}).get("model") or {}
            base.update({k: v for k, v in over.items() if v})
    from core.tenancy import current_key, current_model
    base["api_key"] = current_key() or base.get("api_key") or os.getenv("LLM_API_KEY", "")
    if current_model():
        base["name"] = current_model()
    return base


def fx_hkd_cny() -> float:
    return float((config().get("fx") or {}).get("hkd_cny") or 0.91)
