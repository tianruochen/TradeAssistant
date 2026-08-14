"""Web 流式聊天：SSE 端点，接真 agent（带工具/子agent），把 thinking/content/activity 推给浏览器。

POST /api/chat/stream  body: {"messages":[{"role":"user"|"assistant","content":...}, ...]}
  （前端维护多轮 history，整段发来；服务端无状态）
返回 text/event-stream，每行 `data: {json}\n\n`：
  {"type":"thinking","delta"} / {"type":"content","delta"}
  {"type":"activity","tool","phase","args"|"result_preview"}
  {"type":"done"}
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

# 导入即注册工具
import core.tools.market_tools  # noqa: F401
import core.tools.assets  # noqa: F401
import core.tools.ledger_tools  # noqa: F401
from core.agents_factory import primary_agent


def _agent(model: str | None = None, thinking: bool | None = None):
    # 每请求按当前用户上下文(Key/数据目录)构建,不跨用户缓存
    return primary_agent(model_override=model, thinking=thinking)


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

    client_gone = False

    async def send(obj: dict) -> None:
        nonlocal client_gone
        if client_gone:
            return
        try:
            await resp.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
        except Exception:      # 客户端断开(切页/网络中断)→ 标记,停止后续写与生成
            client_gone = True

    assistant_parts: list[str] = []
    user_last = user_messages[-1]["content"] if user_messages else ""
    # 先落一条(assistant 暂空):即使模型响应到一半刷新/断连,用户消息也不会丢
    try:
        from core.history import log_turn_open
        log_turn_open(user_last)
    except Exception:
        pass
    try:
        gen = _agent(body.get("model"), body.get("thinking")).run_stream(user_messages)
        await send({"type": "ping"})   # 立刻发一帧,让连接尽早有数据
        # 关键:用独立 pump 任务消费生成器,主循环只对「队列」做 wait_for 超时心跳。
        # 绝不能对 gen.__anext__() 做 wait_for——超时会取消它、损坏异步生成器(慢中转必现空返回)。
        q: asyncio.Queue = asyncio.Queue()

        async def _pump():
            try:
                async for ev in gen:
                    await q.put(ev)
            except Exception as exc:  # noqa: BLE001
                await q.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                await q.put(None)   # 结束哨兵

        pump = asyncio.create_task(_pump())
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=5)
                except asyncio.TimeoutError:
                    await send({"type": "ping"})   # 每 5s 心跳(不碰生成器),撑过慢中转静默期
                    if client_gone:
                        break
                    continue
                if ev is None:      # 生成器正常结束
                    break
                if ev.get("type") == "content":
                    assistant_parts.append(ev.get("delta", ""))
                elif ev.get("type") == "done":
                    assistant_parts = [ev.get("message", {}).get("content") or "".join(assistant_parts)]
                await send(ev)
                if client_gone:    # 客户端已走 → 取消 pump(释放并发槽/停止空跑模型)
                    break
        finally:
            if not pump.done():
                pump.cancel()
    except Exception as exc:  # noqa: BLE001
        if not client_gone:
            await send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        # 数字校验:抓正文里编造的持仓现价/涨跌,附「数据核对」更正(以实时为准),再存历史
        try:
            full = "".join(assistant_parts)
            if full.strip() and not client_gone:
                from core import verify, market_plane, tenancy
                uid = tenancy.CURRENT_UID.get()
                pf = market_plane.get_portfolio(uid) if uid else None
                issues = verify.check_holdings_numbers(full, (pf or {}).get("positions") or [])
                if issues:
                    foot = "\n\n---\n⚠️ **数据核对**（正文数字与实时不符，以实时为准）：\n" + "\n".join("- " + i for i in issues)
                    await send({"type": "content", "delta": foot})
                    assistant_parts.append(foot)
        except Exception:
            pass
        # 先回填历史(即使写失败/客户端已断开,也保住本轮),再收尾
        try:
            from core.history import log_turn_close
            log_turn_close("".join(assistant_parts))
        except Exception:
            pass
        if not client_gone:
            try:
                await send({"type": "end"})
                await resp.write_eof()
            except Exception:
                pass
    return resp
