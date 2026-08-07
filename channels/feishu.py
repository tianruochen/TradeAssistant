"""飞书通道:事件 webhook 接收 + 发送。

- send(chat_id, text):tenant_access_token → POST im/v1/messages。
- webhook(request):飞书事件订阅回调(url_verification 校验 + im.message.receive_v1)。
  收到消息 → reply_to_text() 跑主 agent → send() 回群/私聊。

上线需在 secrets.env 配 FEISHU_APP_ID / FEISHU_APP_SECRET,并在飞书开放平台把事件订阅
URL 指向 http(s)://<公网>/feishu/webhook。无凭据时 send/webhook 均安全 no-op。
"""

from __future__ import annotations

import json
import os

import httpx
from aiohttp import web

from channels.base import reply_for_chat

_DOMAIN = "https://open.feishu.cn"


def _creds() -> tuple[str, str]:
    return os.getenv("FEISHU_APP_ID", ""), os.getenv("FEISHU_APP_SECRET", "")


def enabled() -> bool:
    return all(_creds())


async def _token() -> str:
    app_id, secret = _creds()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal",
                         json={"app_id": app_id, "app_secret": secret})
        return r.json().get("tenant_access_token", "")


async def send(chat_id: str, text: str) -> bool:
    if not enabled() or not chat_id:
        return False
    token = await _token()
    if not token:
        return False
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{_DOMAIN}/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": chat_id, "msg_type": "text",
                  "content": json.dumps({"text": text}, ensure_ascii=False)},
        )
        return r.json().get("code") == 0


async def webhook(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"code": -1})
    # 事件订阅 URL 校验
    if body.get("type") == "url_verification":
        return web.json_response({"challenge": body.get("challenge", "")})
    # im.message.receive_v1
    event = body.get("event") or {}
    msg = event.get("message") or {}
    if msg.get("message_type") == "text":
        chat_id = msg.get("chat_id", "")
        try:
            text = json.loads(msg.get("content") or "{}").get("text", "").strip()
        except Exception:
            text = ""
        if text and chat_id:
            # 去掉 @机器人 占位
            text = text.replace("@_user_1", "").strip()
            reply = await reply_for_chat(chat_id, text)
            await send(chat_id, reply)
    return web.json_response({"code": 0})
