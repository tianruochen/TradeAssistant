"""群机器人 webhook 推送 —— 最省事、合规的手机通知(飞书群自定义机器人 + 企业微信群机器人)。

secrets.env 配任一即生效,无则 no-op:
  FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
  WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx   # 企业微信群机器人
"""

from __future__ import annotations

import os

import httpx


def _urls() -> tuple[str, str]:
    return os.getenv("FEISHU_WEBHOOK", "").strip(), os.getenv("WECHAT_WEBHOOK", "").strip()


def enabled() -> bool:
    return any(_urls())


async def push(label: str, text: str) -> bool:
    """推到已配置的群机器人。返回是否至少推成功一处。"""
    fs, wx = _urls()
    if not (fs or wx):
        return False
    body = f"【{label}】\n{text}"[:3800]
    sent = False
    async with httpx.AsyncClient(timeout=15) as c:
        if fs:
            try:
                r = await c.post(fs, json={"msg_type": "text", "content": {"text": body}})
                sent = (r.json().get("StatusCode", r.json().get("code", 0)) == 0) or sent
            except Exception:
                pass
        if wx:
            try:
                r = await c.post(wx, json={"msgtype": "text", "text": {"content": body}})
                sent = (r.json().get("errcode", -1) == 0) or sent
            except Exception:
                pass
    return sent
