"""TradeAssistant 服务入口（aiohttp）。

P0：最小可启动 + /health。
P3 会挂上 SSE 流式聊天端点与静态前端；P4 挂飞书/微信通道。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from core.config import config
from channels.web.chat import chat_stream
from channels import feishu, wechat
from core import scheduler, alerts, users, tenancy

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
ROOT = Path(__file__).resolve().parent

_OPEN_PATHS = {"/", "/health", "/api/register", "/api/login"}


@web.middleware
async def auth_mw(request: web.Request, handler):
    path = request.path
    if path in _OPEN_PATHS or not path.startswith("/api/"):
        return await handler(request)
    token = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    uid = users.uid_for_token(token)
    if not uid:
        return web.json_response({"error": "unauthorized"}, status=401)
    u = users.get_user(uid) or {}
    tenancy.set_user(uid, u.get("llm_key") or None, u.get("model") or None)
    request["uid"] = uid
    return await handler(request)


async def register_handler(req: web.Request) -> web.Response:
    b = await req.json()
    # 生产加固:REGISTER_CODE 未设=开放注册;设为 off/closed=完全关闭;其它值=需邀请码匹配
    import os
    code = (os.getenv("REGISTER_CODE") or "").strip()
    if code.lower() in ("off", "closed", "disabled"):
        return web.json_response({"error": "注册已关闭"}, status=403)
    if code and (b.get("invite") or "").strip() != code:
        return web.json_response({"error": "邀请码错误"}, status=403)
    ok, res = users.register(b.get("username", ""), b.get("password", ""))
    if not ok:
        return web.json_response({"error": res}, status=400)
    token = users.create_session(res)
    return web.json_response({"ok": True, "token": token})


async def login_handler(req: web.Request) -> web.Response:
    b = await req.json()
    uid = users.authenticate(b.get("username", ""), b.get("password", ""))
    if not uid:
        return web.json_response({"error": "用户名或密码错误"}, status=401)
    return web.json_response({"ok": True, "token": users.create_session(uid)})


async def me_handler(req: web.Request) -> web.Response:
    u = users.get_user(req["uid"]) or {}
    return web.json_response({"username": u.get("username"), "has_key": bool(u.get("llm_key")),
                             "model": u.get("model") or ""})


async def set_key_handler(req: web.Request) -> web.Response:
    b = await req.json()
    users.set_user_key(req["uid"], b.get("llm_key", "").strip(), b.get("model", "").strip())
    # 立即更新当前请求上下文
    tenancy.set_user(req["uid"], b.get("llm_key", "").strip() or None, b.get("model", "").strip() or None)
    return web.json_response({"ok": True})


async def health(_req: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "tradeagent"})


async def stats_handler(_req: web.Request) -> web.Response:
    import asyncio
    from core.stats import stats
    data = stats()
    data["portfolio"] = await asyncio.to_thread(_portfolio_snapshot)
    data["model"] = (config().get("model") or {}).get("name", "")
    return web.json_response(data)


def _portfolio_snapshot() -> dict:
    """组合摘要给侧栏 OKR:优先代码现算(60s缓存),失败回退手打摘要。"""
    try:
        from core import portfolio_compute
        return portfolio_compute.compute(live=True)
    except Exception:  # noqa: BLE001
        from core.portfolio import summary
        return summary()


_MODELS_CACHE: dict = {}


async def models_handler(_req: web.Request) -> web.Response:
    """返回中转可用模型列表(供前端下拉),带缓存。"""
    import time
    import httpx
    now = time.time()
    if _MODELS_CACHE.get("t", 0) > now - 300 and _MODELS_CACHE.get("ids"):
        ids = _MODELS_CACHE["ids"]
    else:
        from core.config import model_config
        mc = model_config()
        ids = []
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{mc.get('base_url','').rstrip('/')}/models",
                                headers={"Authorization": f"Bearer {mc.get('api_key','')}"})
                ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
        except Exception:
            ids = []
        if not ids:
            ids = [mc.get("name", "qwen3.8-max")]
        _MODELS_CACHE.update(t=now, ids=ids)
    return web.json_response({"models": ids, "current": (config().get("model") or {}).get("name", "")})


async def history_days(_req: web.Request) -> web.Response:
    from core.history import days
    return web.json_response({"days": days()})


async def history_day(req: web.Request) -> web.Response:
    from core.history import day
    date = req.match_info["date"]
    return web.json_response(day(date))


async def history_delete(req: web.Request) -> web.Response:
    from core.history import delete_turn
    b = await req.json()
    n = delete_turn(b.get("date", ""), b.get("user", ""))
    return web.json_response({"ok": True, "removed": n})


async def notifications_handler(_req: web.Request) -> web.Response:
    from core.notifications import recent
    return web.json_response({"notifications": recent(30)})


async def ledger_handler(_req: web.Request) -> web.Response:
    from core.tools.ledger_tools import trades, decisions, realized_pnl
    return web.json_response({"trades": trades()[-100:][::-1], "decisions": decisions()[-100:][::-1],
                              "pnl": realized_pnl()})


async def constraints_handler(req: web.Request) -> web.Response:
    from core import constraints, market_env
    env = req.query.get("env", "auto")
    detected = None
    if env in ("", "auto"):
        detected = market_env.classify()
        env = detected.get("env") or "shake"
    res = constraints.check(env)
    if detected is not None:
        res["env_source"] = {"auto_detected": detected.get("env_cn"), "detail": detected.get("detail")}
    return web.json_response(res)


async def market_env_handler(_req: web.Request) -> web.Response:
    from core import market_env
    return web.json_response(market_env.classify())


async def assets_handler(_req: web.Request) -> web.Response:
    from core.config import data_dir
    d = data_dir()
    def rd(f):
        p = d / f
        return p.read_text(encoding="utf-8") if p.exists() else ""
    return web.json_response({"holdings": rd("holdings.md"), "watchlist": rd("watchlist.md")})


async def health_scan_handler(_req: web.Request) -> web.Response:
    from core import portfolio_health
    return web.json_response({"items": portfolio_health.scan()})


async def performance_handler(_req: web.Request) -> web.Response:
    from core import performance
    return web.json_response(performance.summary())


async def settings_get(req: web.Request) -> web.Response:
    from core import user_settings
    return web.json_response(user_settings.load(req["uid"]))


async def settings_post(req: web.Request) -> web.Response:
    from core import user_settings
    b = await req.json()
    return web.json_response(user_settings.save(req["uid"], b))


async def strategy_get(_req: web.Request) -> web.Response:
    from core.config import data_dir
    p = data_dir() / "交易策略.md"
    return web.json_response({"text": p.read_text(encoding="utf-8") if p.exists() else ""})


async def strategy_post(req: web.Request) -> web.Response:
    from core.config import data_dir
    b = await req.json()
    text = str(b.get("text") or "")
    p = data_dir() / "交易策略.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)   # 原子写
    return web.json_response({"ok": True, "bytes": len(text)})


async def attribution_handler(_req: web.Request) -> web.Response:
    import asyncio
    from core import attribution
    return web.json_response(await asyncio.to_thread(attribution.evaluate))


async def index(_req: web.Request) -> web.Response:
    return web.FileResponse(ROOT / "web" / "index.html")


async def _on_startup(app: web.Application) -> None:
    app["stop"] = asyncio.Event()
    app["bg"] = [
        asyncio.create_task(scheduler.run_loop(app["stop"])),
        asyncio.create_task(alerts.poll_loop(app["stop"])),   # 价格事件触发
        asyncio.create_task(wechat.poll_loop(app["stop"])),   # 无凭据自动返回
    ]


async def _on_cleanup(app: web.Application) -> None:
    app["stop"].set()
    for t in app.get("bg", []):
        t.cancel()


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth_mw])
    app.router.add_get("/health", health)
    app.router.add_post("/api/register", register_handler)
    app.router.add_post("/api/login", login_handler)
    app.router.add_get("/api/me", me_handler)
    app.router.add_post("/api/key", set_key_handler)
    app.router.add_get("/api/stats", stats_handler)
    app.router.add_get("/api/models", models_handler)
    app.router.add_get("/api/history", history_days)
    app.router.add_get("/api/history/{date}", history_day)
    app.router.add_post("/api/history/delete", history_delete)
    app.router.add_get("/api/notifications", notifications_handler)
    app.router.add_get("/api/ledger", ledger_handler)
    app.router.add_get("/api/constraints", constraints_handler)
    app.router.add_get("/api/market_env", market_env_handler)
    app.router.add_get("/api/assets", assets_handler)
    app.router.add_get("/api/health", health_scan_handler)
    app.router.add_get("/api/performance", performance_handler)
    app.router.add_get("/api/settings", settings_get)
    app.router.add_post("/api/settings", settings_post)
    app.router.add_get("/api/strategy", strategy_get)
    app.router.add_post("/api/strategy", strategy_post)
    app.router.add_get("/api/attribution", attribution_handler)
    app.router.add_get("/", index)
    app.router.add_post("/api/chat/stream", chat_stream)     # SSE 接真 agent
    app.router.add_post("/feishu/webhook", feishu.webhook)   # 飞书事件订阅(不经 auth_mw:非 /api/)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    import os
    cfg = config().get("server") or {}
    host = os.getenv("TA_HOST") or cfg.get("host", "0.0.0.0")   # 生产走 nginx 时设 127.0.0.1
    port = int(os.getenv("TA_PORT") or cfg.get("port", 8760))
    web.run_app(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
