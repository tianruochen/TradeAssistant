"""工具注册器：注册 name/schema/handler，产出 OpenAI tools 定义，按名分发执行。

handler 签名：handler(args: dict) -> str（返回给模型的文本/JSON 字符串）。
支持 async handler（子 agent 咨询是 async）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    schema: dict[str, Any]          # OpenAI function schema（含 name/description/parameters）
    handler: Callable[[dict], Any]  # (args) -> str | awaitable[str]


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, schema: dict[str, Any], handler: Callable[[dict], Any]) -> None:
        self._tools[name] = Tool(name=name, schema=schema, handler=handler)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def openai_tools(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        names = only if only is not None else self.names()
        out = []
        for n in names:
            t = self._tools.get(n)
            if t:
                out.append({"type": "function", "function": t.schema})
        return out

    async def dispatch(self, name: str, args: dict[str, Any]) -> str:
        t = self._tools.get(name)
        if not t:
            return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
        try:
            res = t.handler(args)
            if inspect.isawaitable(res):
                res = await res
            return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)
        except Exception as exc:  # noqa: BLE001 — 工具错误不应炸掉整个 agent 循环
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


# 全局默认注册器（market/express/sense 工具在各自模块 import 时注册进来）
registry = Registry()
