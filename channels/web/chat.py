"""Web 流式聊天：SSE 端点，接真 agent（带工具/子agent），把 thinking/content/activity 推给浏览器。

POST /api/chat/stream  body: {"messages":[{"role":"user"|"assistant","content":...}, ...]}
  （前端维护多轮 history，整段发来；服务端无状态）
返回 text/event-stream，每行 `data: {json}\n\n`：
  {"type":"thinking","delta"} / {"type":"content","delta"}
  {"type":"activity","tool","phase","args"|"result_preview"}
  {"type":"done"}
"""

from __future__ import annotations

import json

from aiohttp import web

# 导入即注册工具
import core.tools.market_tools  # noqa: F401
import core.tools.assets  # noqa: F401
import core.tools.ledger_tools  # noqa: F401
from core.agents_factory import primary_agent


def _agent(model: str | None = None):
    # 每请求按当前用户上下文(Key/数据目录)构建,不跨用户缓存
    return primary_agent(model_override=model)


async def chat_stream(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    messages = body.get("messages") or []
    if not messages and body.get("message"):
        messages = [{"role": "user", "content": str(body["message"])}]
    # 只保留 role/content，防脏字段
    user_messages = [{"role": m.get("role", "user"), "content": m.get("content", "")}
                     for m in messages if m.get("content")]
    # 自动截断到最近 ~10 轮(20 条)——聊天窗口是临时的,状态在 plan.md/holdings.md 里,
    # 从根上杜绝上下文膨胀(digital-life 曾因无界上下文死循环)。
    if len(user_messages) > 20:
        user_messages = user_messages[-20:]

    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    async def send(obj: dict) -> None:
        await resp.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))

    assistant_parts: list[str] = []
    user_last = user_messages[-1]["content"] if user_messages else ""
    # 先落一条(assistant 暂空):即使模型响应到一半刷新/断连,用户消息也不会丢
    try:
        from core.history import log_turn_open
        log_turn_open(user_last)
    except Exception:
        pass
    try:
        async for ev in _agent(body.get("model")).run_stream(user_messages):
            if ev.get("type") == "content":
                assistant_parts.append(ev.get("delta", ""))
            elif ev.get("type") == "done":
                assistant_parts = [ev.get("message", {}).get("content") or "".join(assistant_parts)]
            await send(ev)
    except Exception as exc:  # noqa: BLE001
        await send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        # 先回填历史(即使写响应失败/客户端已断开,也保住本轮),再收尾
        try:
            from core.history import log_turn_close
            log_turn_close("".join(assistant_parts))
        except Exception:
            pass
        try:
            await send({"type": "end"})
            await resp.write_eof()
        except Exception:
            pass
    return resp
