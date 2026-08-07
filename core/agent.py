"""Agent 循环：流式 tool-call 循环。

run_stream(messages) 逐步 yield 事件：
  {"type":"thinking","delta"}  思考增量（<think> 或 reasoning_content）
  {"type":"content","delta"}   正文增量
  {"type":"activity","tool","phase":"start|end","args"/"result_preview"}  工具/子agent活动
  {"type":"done","message"}    收尾（无更多工具调用）

主 agent 与专家子 agent 用同一个类，区别只在 system prompt / 可用工具集。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from core.llm.client import LLMClient
from core.tools.registry import Registry


class Agent:
    def __init__(
        self,
        name: str,
        system_prompt: str,
        tool_names: list[str],
        registry: Registry,
        model_cfg: dict[str, Any],
        max_iterations: int = 12,
    ) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.tool_names = tool_names
        self.registry = registry
        self.client = LLMClient(model_cfg)
        self.max_iterations = max_iterations

    def _tools(self) -> list[dict[str, Any]]:
        return self.registry.openai_tools(self.tool_names) if self.tool_names else []

    async def run_stream(self, user_messages: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}, *user_messages]
        tools = self._tools()
        for _ in range(self.max_iterations):
            final_msg: dict[str, Any] | None = None
            async for ev in self.client.stream(messages, tools):
                if ev["type"] in ("thinking", "content"):
                    yield ev
                elif ev["type"] == "final":
                    final_msg = ev["message"]
            if final_msg is None:
                yield {"type": "done", "message": {"role": "assistant", "content": ""}}
                return

            messages.append(final_msg)
            tool_calls = final_msg.get("tool_calls") or []
            if not tool_calls:
                yield {"type": "done", "message": final_msg}
                return

            for tc in tool_calls:
                fn = tc.get("function") or {}
                tname = fn.get("name", "")
                try:
                    targs = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    targs = {}
                yield {"type": "activity", "tool": tname, "phase": "start", "args": targs}
                result = await self.registry.dispatch(tname, targs)
                yield {"type": "activity", "tool": tname, "phase": "end",
                       "result_preview": result[:300]}
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "name": tname, "content": result})

        yield {"type": "done", "message": {"role": "assistant", "content": "（达到最大迭代次数，未完成）"}}

    async def run(self, user_messages: list[dict[str, Any]]) -> str:
        """非流式便捷版：跑完返回最终正文（子 agent 咨询用）。"""
        content_parts: list[str] = []
        final = ""
        async for ev in self.run_stream(user_messages):
            if ev["type"] == "content":
                content_parts.append(ev["delta"])
            elif ev["type"] == "done":
                final = ev["message"].get("content") or "".join(content_parts)
        return final or "".join(content_parts)
