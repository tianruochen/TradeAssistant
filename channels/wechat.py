"""微信通道(ClawBot):长轮询收消息 + 发送。

digital-life 用 ilinkai.weixin.qq.com/ilink/bot 的 getupdates 长轮询 + send。这里精简移植:
- send(text):POST 发消息到 bot。
- poll_loop():后台协程,轮询 getupdates,收到文本 → reply_to_text() → send()。
上线需 secrets.env 配 WECHAT_BOT_TOKEN(控制台扫码登录获取)。无凭据时不启动轮询。
"""

from __future__ import annotations

import asyncio
import os

import httpx

from channels.base import reply_for_chat

_BASE = "https://ilinkai.weixin.qq.com/ilink/bot"


def _token() -> str:
    return os.getenv("WECHAT_BOT_TOKEN", "")


def enabled() -> bool:
    return bool(_token())


async def send(text: str, chat_id: str = "") -> bool:
    if not enabled():
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{_BASE}/send", json={"token": _token(), "chat_id": chat_id, "text": text})
            return r.status_code == 200
    except Exception:
        return False


async def poll_loop(stop: asyncio.Event) -> None:
    """长轮询收消息(ClawBot getupdates)。凭据缺失直接返回。"""
    if not enabled():
        return
    offset = 0
    while not stop.is_set():
        try:
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.post(f"{_BASE}/getupdates", json={"token": _token(), "offset": offset, "timeout": 25})
            for upd in (r.json().get("updates") or []):
                offset = max(offset, int(upd.get("id", 0)) + 1)
                text = (upd.get("text") or "").strip()
                chat_id = upd.get("chat_id", "")
                if text:
                    reply = await reply_for_chat(chat_id or "_wechat", text)
                    await send(reply, chat_id)
        except Exception:
            await asyncio.sleep(3)
