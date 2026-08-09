"""手机通知推送 —— 支持四种,配任一即生效(secrets.env),无则 no-op:

个人微信(最省事,推到你自己微信):
  SERVERCHAN_KEY=SCTxxxx          # Server酱:关注「方糖」公众号拿 SendKey
  PUSHPLUS_TOKEN=xxxx             # PushPlus:pushplus.plus 拿 token
群机器人(推到群):
  FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx     # 飞书群自定义机器人
  WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx  # 企业微信群机器人
"""

from __future__ import annotations

import os

import httpx


def _env() -> dict:
    """当前用户的推送 key(优先其账户设置),缺省回退全局 env。"""
    cfg = {
        "serverchan": os.getenv("SERVERCHAN_KEY", "").strip(),
        "pushplus": os.getenv("PUSHPLUS_TOKEN", "").strip(),
        "feishu": os.getenv("FEISHU_WEBHOOK", "").strip(),
        "wechat": os.getenv("WECHAT_WEBHOOK", "").strip(),
    }
    try:
        from core.tenancy import current_uid
        from core import user_settings
        uid = current_uid()
        if uid:
            p = (user_settings.load(uid) or {}).get("push") or {}
            for k, src in (("serverchan", "serverchan"), ("pushplus", "pushplus"),
                           ("feishu", "feishu_webhook"), ("wechat", "wechat_webhook")):
                if (p.get(src) or "").strip():
                    cfg[k] = p[src].strip()   # 用户级覆盖全局
    except Exception:
        pass
    return cfg


def enabled() -> bool:
    return any(_env().values())


async def push(label: str, text: str) -> bool:
    """推到所有已配置渠道。返回是否至少成功一处。"""
    e = _env()
    if not any(e.values()):
        return False
    title = f"【{label}】"
    body = f"{title}\n{text}"[:3800]
    sent = False
    async with httpx.AsyncClient(timeout=15) as c:
        if e["serverchan"]:                     # Server酱 → 个人微信
            try:
                r = await c.post(f"https://sctapi.ftqq.com/{e['serverchan']}.send",
                                 data={"title": title, "desp": text[:3000]})
                sent = (r.json().get("code", -1) == 0) or sent
            except Exception:
                pass
        if e["pushplus"]:                        # PushPlus → 个人微信
            try:
                r = await c.post("https://www.pushplus.plus/send",
                                 json={"token": e["pushplus"], "title": title, "content": body})
                sent = (r.json().get("code", -1) == 200) or sent
            except Exception:
                pass
        if e["feishu"]:                          # 飞书群机器人
            try:
                r = await c.post(e["feishu"], json={"msg_type": "text", "content": {"text": body}})
                sent = (r.json().get("StatusCode", r.json().get("code", 0)) == 0) or sent
            except Exception:
                pass
        if e["wechat"]:                          # 企业微信群机器人
            try:
                r = await c.post(e["wechat"], json={"msgtype": "text", "text": {"content": body}})
                sent = (r.json().get("errcode", -1) == 0) or sent
            except Exception:
                pass
    return sent
