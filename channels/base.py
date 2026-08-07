"""通道公共层：把"收到一条文本 → 跑主 agent → 得到整段回复"抽象出来。

web 走 SSE 流式(channels/web/chat.py);飞书/微信是整段发送,用这里的 reply_to_text()。
主 agent 单例复用,避免每条消息重建。
"""

from __future__ import annotations

# 导入即注册工具
import core.tools.market_tools  # noqa: F401
import core.tools.assets  # noqa: F401
import core.tools.ledger_tools  # noqa: F401
from core.agents_factory import primary_agent

_AGENT = None


def _agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = primary_agent()
    return _AGENT


async def reply_to_text(text: str, history: list[dict] | None = None) -> str:
    """收到用户文本 → 主 agent 跑完 → 返回整段回复文本(供飞书/微信发送)。"""
    msgs = list(history or [])
    msgs.append({"role": "user", "content": text})
    return await _agent().run(msgs)


# ── 通道多轮记忆:按 chat_id 存最近 N 轮,让飞书/微信对话不失忆 ──
_HISTORY: dict[str, list[dict]] = {}
_MAX_TURNS = 12


async def reply_for_chat(chat_id: str, text: str) -> str:
    """带 per-chat 历史的回复(飞书/微信用)。web 端历史由前端维护,不走这里。"""
    hist = _HISTORY.setdefault(chat_id or "_default", [])
    reply = await _agent().run([*hist, {"role": "user", "content": text}])
    hist.append({"role": "user", "content": text})
    hist.append({"role": "assistant", "content": reply})
    if len(hist) > _MAX_TURNS * 2:
        del hist[: len(hist) - _MAX_TURNS * 2]
    return reply
