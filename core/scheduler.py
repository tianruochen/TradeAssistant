"""轻量定时任务:交易时段到点跑主 agent,把结果投递到可用通道(飞书/微信,否则记日志)。

不搬 digital-life 的整套 lifecycle 引擎——只是一个按本地时间(北京)触发的 asyncio 循环。
weekdays 触发,每个 (label,time) 每天只触发一次。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from channels.base import reply_to_text

logger = logging.getLogger("tradeagent.scheduler")

_INTRADAY_PROMPT = (
    "盘中定时监控(循环闭环)。①read_plan 看目标与今日待办 → ②read_holdings + sense_stock_quote 查持仓对止损位、"
    "sense_sector_flow+sense_market_scan 看异动 → ③有持仓触发止损/买点或明显机会才展开(可 consult_risk/consult_hunter)、"
    "无实质变化就一句'盘中平稳,无操作信号' → ④有进展则 write_file 更新 plan.md 循环记录。禁刷屏:与上一条无新信息就别发。"
)
_DEEP_PROMPT = (
    "盘面深度研究(循环闭环)。①read_plan+read_strategy 看目标与纪律 → ②consult_hunter 挖热门板块内的机会候选 → "
    "③产出候选清单(板块/买点/触发条件)写入 watchlist,并在 alerts.md 加对应买点触发 → ④write_file 更新 plan.md 进度。"
)
_PROGRESS_PROMPT = (
    "收盘翻倍进度(循环闭环)。①consult_ledger 更新持仓收盘价与快照 → ②对照 plan.md OKR 算进度 → "
    "③给:总资产/当日盈亏/距初始%/距翻倍还需%/前三集中度 → ④write_file 把当日循环记录追加进 plan.md。简洁。"
)
_WEEKEND_PROMPT = (
    "周末汇总(周末不开盘,不做交易,只梳理)。"
    "①read_holdings + read_plan + read_watchlist + read_strategy 掌握现状与候选池; "
    "②本周复盘:持仓表现、已实现盈亏、纪律执行(有无破止损/超仓/追高)、候选池进展; "
    "③消息面梳理:结合已知的政策周期/行业景气/财报披露窗口,列出下周值得关注的板块与潜在催化。"
    "**严禁编造具体的未经证实的新闻/数字/事件**;拿不准的一律标注'待核实',并提示用户补充你看到的消息; "
    "④下周计划:在当前市场环境纪律下给关注清单+触发条件,write_file 更新 plan.md / watchlist.md。分点、简洁。"
)

# (label, [HH:MM...], prompt) —— 工作日跑交易节奏
_JOBS = [
    ("盘中监控", ["09:35", "10:05", "10:35", "11:05", "13:05", "13:35", "14:05", "14:35"], _INTRADAY_PROMPT),
    ("深度研究", ["11:15", "13:15"], _DEEP_PROMPT),
    ("翻倍进度", ["16:05"], _PROGRESS_PROMPT),
]
# 周末只做一次消息面/复盘汇总(周六上午),不跑交易节奏
_WEEKEND_JOBS = [
    ("周末汇总", ["10:00"], _WEEKEND_PROMPT),
]


_JOB_CAT = {"盘中监控": "intraday", "深度研究": "deep", "翻倍进度": "progress", "周末汇总": "weekend"}


async def _deliver(label: str, text: str, push_external: bool = True) -> None:
    """网页通知流总是写;push_external=False 时不推手机(降噪,如盘中平稳)。"""
    from channels import feishu, wechat, push
    from core import notifications
    notifications.push(label, text)
    if not push_external:
        return
    sent = False
    if push.enabled():                                  # Server酱/PushPlus/群机器人(按当前用户 key)
        sent = await push.push(label, text) or sent
    chat = os.getenv("SCHEDULE_FEISHU_CHAT", "")
    if feishu.enabled() and chat:
        sent = await feishu.send(chat, f"【{label}】\n{text}") or sent
    if wechat.enabled():
        sent = await wechat.send(f"【{label}】\n{text}") or sent
    if not sent:
        logger.info("[定时·%s] 无外部通道(仅进网页)", label)


async def run_job(label: str, prompt: str, push_external: bool = True) -> str:
    text = await reply_to_text(prompt)
    await _deliver(label, text, push_external)
    return text


JOB_TIMEOUT = 240   # 单个定时任务硬超时(秒)


async def run_loop(stop: asyncio.Event) -> None:
    fired: set = set()   # (date, hhmm, label, uid)
    while not stop.is_set():
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        hhmm = now.strftime("%H:%M")
        weekday = now.weekday()
        if hhmm == "00:00":
            fired = {f for f in fired if f[0] == today}
        base_jobs = _JOBS if weekday < 5 else (_WEEKEND_JOBS if weekday == 5 else [])
        if weekday < 5:   # 节假日休市 → 跳过交易节奏(真实交易日历)
            try:
                from core.tools.market_tools import is_trading_day
                if not is_trading_day(today):
                    base_jobs = []
            except Exception:
                pass
        if base_jobs:
            from core import users, tenancy, user_settings, notifications
            from core.stats import auto_allowed
            for u in users.all_users():
                uid, key = u["uid"], u["llm_key"]
                if not key:                       # 没填 LLM key 的用户不跑(也无从跑)
                    continue
                st = user_settings.load(uid)
                if not st.get("schedule", {}).get("enabled", True):
                    continue
                tenancy.set_user(uid, key or None, u.get("model") or None)  # 用该用户 key/数据/推送
                if not auto_allowed():            # 该用户当日额度(stats 按租户)
                    continue
                for label, times, prompt in base_jobs:
                    cat = _JOB_CAT.get(label)
                    if cat and not st.get("schedule", {}).get(cat, True):
                        continue                  # 用户关了这类任务
                    if hhmm in times and (today, hhmm, label, uid) not in fired:
                        fired.add((today, hhmm, label, uid))
                        push_ext = (label != "盘中监控") or st.get("notify", {}).get("push_intraday", False)
                        logger.info("触发定时任务: %s @ %s [uid %s]", label, hhmm, uid)
                        try:
                            await asyncio.wait_for(run_job(label, prompt, push_ext), timeout=JOB_TIMEOUT)
                        except asyncio.TimeoutError:
                            logger.warning("定时任务 %s 超时[uid %s]", label, uid)
                            try:
                                notifications.push(label, f"⏱️ 本次「{label}」超时(>{JOB_TIMEOUT}s,多为行情拥堵),已跳过。")
                            except Exception:
                                pass
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("定时任务 %s 失败[uid %s]: %r", label, uid, exc, exc_info=True)
                            try:
                                notifications.push(label, f"⚠️ 本次「{label}」执行失败({type(exc).__name__}),已跳过。")
                            except Exception:
                                pass
        # 用户自建定时任务(任意日触发,不受交易日门限;各用户自己的 key/数据/推送)
        try:
            from core import users, tenancy, user_settings, notifications
            from core.stats import auto_allowed
            for u in users.all_users():
                uid, key = u["uid"], u["llm_key"]
                if not key:
                    continue
                st = user_settings.load(uid)
                if not st.get("schedule", {}).get("enabled", True):
                    continue
                for j in (st.get("custom_jobs") or []):
                    if not j.get("enabled", True) or j.get("time") != hhmm:
                        continue
                    jid = str(j.get("id") or j.get("name") or hhmm)
                    fk = (today, hhmm, "custom:" + jid, uid)
                    if fk in fired:
                        continue
                    fired.add(fk)
                    tenancy.set_user(uid, key or None, u.get("model") or None)
                    if not auto_allowed():
                        continue
                    label = j.get("name") or "自定义任务"
                    prompt = (j.get("prompt") or "").strip()
                    if not prompt:
                        continue
                    logger.info("触发自定义任务: %s @ %s [uid %s]", label, hhmm, uid)
                    try:
                        await asyncio.wait_for(run_job(label, prompt, bool(j.get("push", True))), timeout=JOB_TIMEOUT)
                    except asyncio.TimeoutError:
                        notifications.push(label, f"⏱️ 自定义任务「{label}」超时,已跳过。")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("自定义任务 %s 失败[uid %s]: %r", label, uid, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("自定义任务循环异常: %r", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
