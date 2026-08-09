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
        force_first_tool: bool = False,
    ) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.tool_names = tool_names
        self.registry = registry
        self.client = LLMClient(model_cfg)
        self.max_iterations = max_iterations
        self.force_first_tool = force_first_tool   # 首轮强制调工具(子agent专用:必须先取真数据,禁凭空作答)

    def _tools(self) -> list[dict[str, Any]]:
        return self.registry.openai_tools(self.tool_names) if self.tool_names else []

    async def run_stream(self, user_messages: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}, *user_messages]
        tools = self._tools()
        emitted_content = False   # 整个 run 是否吐过正文(用于识别"空回答",避免前端空气泡)
        for _iter in range(self.max_iterations):
            # 首轮对子agent强制 tool_choice=required:qwen流式常丢工具/编造,强制其先查真数据
            tchoice = "required" if (_iter == 0 and self.force_first_tool and tools) else None
            final_msg: dict[str, Any] | None = None
            async for ev in self.client.stream(messages, tools, tool_choice=tchoice):
                if ev["type"] in ("thinking", "content"):
                    if ev["type"] == "content" and ev.get("delta"):
                        emitted_content = True
                    yield ev
                elif ev["type"] == "final":
                    final_msg = ev["message"]
            if final_msg is None:
                yield self._maybe_empty_done(emitted_content, {"role": "assistant", "content": ""})
                return

            tool_calls = final_msg.get("tool_calls") or []
            # 流式丢工具兜底:qwen 流式约3成概率吐了开场白却没带 tool_calls(实测)。
            # 若本轮没工具调用、正文又不长(像"我先拉数据、首先查行情…"这类承诺而非结论) → 非流式复核,真丢了就补上。
            # 阈值 220:承诺式开场白通常 <150 字,真正的最终结论通常 500+ 字,以此区分,避免给长答案白白多打一次。
            if not tool_calls and len((final_msg.get("content") or "").strip()) < 220:
                try:
                    verify = await self.client.complete(messages, tools)
                except Exception:  # noqa: BLE001  复核失败就按原样(无工具)收尾
                    verify = {}
                if verify.get("tool_calls"):
                    final_msg = verify   # 用可靠的非流式结果(含 tool_calls)接着跑;已流式的开场白留作前缀
                    tool_calls = verify.get("tool_calls") or []

            messages.append(final_msg)
            if not tool_calls:
                yield self._maybe_empty_done(emitted_content, final_msg)
                return

            for tc in tool_calls:
                fn = tc.get("function") or {}
                tname = fn.get("name", "")
                try:
                    targs = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    targs = {}
                yield {"type": "activity", "tool": tname, "phase": "start", "args": targs}
                if tname.startswith("consult_"):
                    # 咨询专家:流式透传子agent的每一步(查行情/看K线…),别让它成60秒黑盒
                    from core.agents_factory import consult_stream
                    result = ""
                    try:
                        async for sub in consult_stream(tname[len("consult_"):], targs.get("question", "")):
                            if sub.get("type") == "consult_final":
                                result = sub.get("content", "")
                            else:
                                yield sub   # 子agent的 activity(带 via=expert)→ 前端嵌套显示
                    except Exception as exc:  # noqa: BLE001
                        result = json.dumps({"error": f"{tname} 执行失败: {type(exc).__name__}: {exc}"[:200],
                                             "result": None}, ensure_ascii=False)
                else:
                    try:
                        result = await self.registry.dispatch(tname, targs)
                    except Exception as exc:  # noqa: BLE001  工具失败不拖垮整轮,回错误串让主agent继续
                        result = json.dumps({"error": f"{tname} 执行失败: {type(exc).__name__}: {exc}"[:200],
                                             "result": None}, ensure_ascii=False)
                yield {"type": "activity", "tool": tname, "phase": "end",
                       "result_preview": result[:300]}
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "name": tname, "content": result})

        yield {"type": "done", "message": {"role": "assistant", "content": "（达到最大迭代次数，未完成）"}}

    @staticmethod
    def _maybe_empty_done(emitted_content: bool, msg: dict[str, Any]) -> dict[str, Any]:
        """收尾:若整轮没吐过正文、且最终消息正文也空(中转波动导致空返回) → 给一句明确提示,
        绝不让前端出现空气泡。"""
        content = (msg.get("content") or "").strip()
        if not emitted_content and not content:
            return {"type": "done", "message": {"role": "assistant",
                    "content": "⚠️ 模型这次返回为空（中转服务波动），请重新发送试试。"}}
        return {"type": "done", "message": msg}

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
