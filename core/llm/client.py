"""LLM 客户端：阻塞补全 + 流式（含工具调用增量缓冲）。

digital-life 的 agent 只有阻塞式；TradeAssistant 的 web 要豆包式流式，所以这里实现
`stream()`：边收边吐 thinking / content 增量，同时把分片的 tool_calls 按 index
缓冲拼回完整结构，供上层 agent loop 交错执行工具。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, AsyncIterator

import httpx

from core import stats
from . import providers

_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)
logger = logging.getLogger("tradeagent.llm")

# 中转常见 429「令牌并发过多」+ 瞬时 5xx → 退避重试(并发/拥堵多为短暂)
_RETRY_CODES = {429, 500, 502, 503, 504}
_MAX_TRIES = 7
# 全局并发闸:同一 Key 并发过多正是 429 主因,限流从源头减少触发(定时任务+用户+子agent同抢)
_SEM = asyncio.Semaphore(2)


def _backoff(attempt: int) -> float:
    return min(12.0, 1.0 * (2 ** attempt)) + random.uniform(0, 0.6)   # 1,2,4,8,12(+抖动)


class _ThinkSplitter:
    """把 content 流里的 <think>...</think> 拆成 thinking / content 两路。

    中转的模型（glm-5.2/deepseek/qwen/kimi）不用 reasoning_content 字段，而是把
    思考包在正文的 <think></think> 标签里。逐 delta 喂入，处理跨分片的半个标签，
    输出 [("thinking"|"content", text), ...]。
    """

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.mode = "content"  # or "thinking"

    def _safe_keep(self, tag: str) -> int:
        """buf 末尾有多少字符可能是 tag 的前缀 → 暂时留住，等下一片再判。"""
        for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
            if self.buf.endswith(tag[:k]):
                return k
        return 0

    def feed(self, text: str) -> list[tuple[str, str]]:
        self.buf += text
        out: list[tuple[str, str]] = []
        while True:
            if self.mode == "content":
                i = self.buf.find(self.OPEN)
                if i == -1:
                    keep = self._safe_keep(self.OPEN)
                    emit = self.buf[: len(self.buf) - keep]
                    if emit:
                        out.append(("content", emit))
                    self.buf = self.buf[len(self.buf) - keep:]
                    break
                if i > 0:
                    out.append(("content", self.buf[:i]))
                self.buf = self.buf[i + len(self.OPEN):]
                self.mode = "thinking"
            else:
                j = self.buf.find(self.CLOSE)
                if j == -1:
                    keep = self._safe_keep(self.CLOSE)
                    emit = self.buf[: len(self.buf) - keep]
                    if emit:
                        out.append(("thinking", emit))
                    self.buf = self.buf[len(self.buf) - keep:]
                    break
                if j > 0:
                    out.append(("thinking", self.buf[:j]))
                self.buf = self.buf[j + len(self.CLOSE):]
                self.mode = "content"
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self.buf:
            return []
        kind = "thinking" if self.mode == "thinking" else "content"
        out = [(kind, self.buf)]
        self.buf = ""
        return out



class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, model_cfg: dict[str, Any]) -> None:
        self.cfg = model_cfg
        self.url = providers.chat_url(model_cfg.get("base_url", ""))
        key = (model_cfg.get("api_key") or "").strip()
        # 校验:空 / 占位符 / 非 ASCII(如 'sk-你的key')会让请求头崩(UnicodeEncodeError),提前给清晰报错
        if not key or not key.isascii() or "你的key" in key:
            raise LLMError("未配置有效的 API Key —— 请在网页右上角/账户区「改 Key」填入你的 Key；"
                           "后台定时任务需在 secrets.env 设 TA_OWNER_UID 指向已填 Key 的业主账户。")
        self.headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """阻塞式：返回 assistant message dict（content / reasoning_content / tool_calls）。"""
        stats.incr_llm_call()
        payload = providers.build_payload(self.cfg, messages, tools, stream=False, max_tokens=max_tokens)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            for attempt in range(_MAX_TRIES):
                async with _SEM:
                    r = await c.post(self.url, headers=self.headers, json=payload)
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]
                if r.status_code in _RETRY_CODES and attempt < _MAX_TRIES - 1:
                    logger.warning("LLM %d 重试(%d/%d)", r.status_code, attempt + 1, _MAX_TRIES)
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise LLMError(f"LLM {r.status_code}: {r.text[:300]}")

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式：yield 事件

        - {"type":"thinking","delta": str}   模型思考(reasoning_content)增量
        - {"type":"content","delta": str}    正文增量
        - {"type":"final","message": {...}, "usage": {...}}  收尾，message 含拼好的 tool_calls

        message 结构与 complete() 一致，供 agent loop 判断是否要执行工具。
        """
        stats.incr_llm_call()
        payload = providers.build_payload(self.cfg, messages, tools, stream=True, max_tokens=max_tokens)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}  # index -> {id,type,function:{name,arguments}}
        usage: dict[str, Any] = {}
        splitter = _ThinkSplitter()
        yielded_any = False   # 是否已向调用方吐过任何增量(决定"降级兜底"能否安全介入)

        def _route(kind: str, text: str):
            nonlocal yielded_any
            if kind == "thinking":
                reasoning_parts.append(text)
            else:
                content_parts.append(text)
            yielded_any = True
            return {"type": kind, "delta": text}

        for attempt in range(_MAX_TRIES):
            content_parts.clear(); reasoning_parts.clear(); tool_calls.clear(); usage.clear()
            splitter = _ThinkSplitter()
            try:
                async with _SEM:
                    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                        async with c.stream("POST", self.url, headers=self.headers, json=payload) as r:
                            if r.status_code != 200:
                                body = (await r.aread()).decode("utf-8", "ignore")
                                if r.status_code in _RETRY_CODES and attempt < _MAX_TRIES - 1:
                                    logger.warning("LLM流 %d 重试(%d/%d)", r.status_code, attempt + 1, _MAX_TRIES)
                                    await asyncio.sleep(_backoff(attempt))
                                    continue   # 尚未 yield 任何增量,重试不会重复
                                raise LLMError(f"LLM {r.status_code}: {body[:300]}")
                            async for line in r.aiter_lines():
                                if not line or not line.startswith("data:"):
                                    continue
                                data = line[len("data:"):].strip()
                                if data == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data)
                                except json.JSONDecodeError:
                                    continue
                                if chunk.get("usage"):
                                    usage.update(chunk["usage"])
                                choices = chunk.get("choices") or []
                                if not choices:
                                    continue
                                delta = choices[0].get("delta") or {}

                                # 部分模型用独立 reasoning_content 字段
                                rc = delta.get("reasoning_content")
                                if rc:
                                    yield _route("thinking", rc)

                                # 多数模型把思考包在 content 的 <think></think> 里 → 拆分
                                ct = delta.get("content")
                                if ct:
                                    for kind, text in splitter.feed(ct):
                                        yield _route(kind, text)

                                for tc in delta.get("tool_calls") or []:
                                    idx = tc.get("index", 0)
                                    slot = tool_calls.setdefault(
                                        idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                                    )
                                    if tc.get("id"):
                                        slot["id"] = tc["id"]
                                    fn = tc.get("function") or {}
                                    if fn.get("name"):
                                        slot["function"]["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        slot["function"]["arguments"] += fn["arguments"]
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError,
                    httpx.TransportError) as exc:
                # 中转流式挂住/中断:若还没吐过任何字,转非流式兜底(见下),否则重试/上抛
                if not yielded_any:
                    logger.warning("LLM流中断(%s),转非流式兜底", type(exc).__name__)
                    break
                if attempt < _MAX_TRIES - 1:
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise LLMError(f"LLM流中断: {type(exc).__name__}: {exc}")

            for kind, text in splitter.flush():
                yield _route(kind, text)

            # 中转降级:200 却空流(无正文/无思考/无工具调用)→ 转非流式兜底,别让用户看空白
            if not yielded_any and not tool_calls:
                logger.warning("LLM流返回空(中转降级),转非流式兜底")
                break

            message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
            if reasoning_parts:
                message["reasoning_content"] = "".join(reasoning_parts)
            if tool_calls:
                message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
            yield {"type": "final", "message": message, "usage": usage}
            return

        # 兜底:流式挂住/空流且尚未吐字 → 走非流式(实测中转非流式仍可用)。
        # 一次性拿到整段,当作 content 增量吐出,再收尾;工具调用照样进 final 供 agent 执行。
        try:
            msg = await self.complete(messages, tools, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"流式空返回,非流式兜底也失败: {type(exc).__name__}: {exc}")
        # 非流式正文里也可能带 <think></think>,拆一下让思考归思考
        raw = msg.get("content") or ""
        sp = _ThinkSplitter()
        for kind, text in sp.feed(raw) + sp.flush():
            if text:
                yield {"type": kind, "delta": text}
        if msg.get("reasoning_content"):
            yield {"type": "thinking", "delta": str(msg["reasoning_content"])}
        yield {"type": "final", "message": msg, "usage": {}}
