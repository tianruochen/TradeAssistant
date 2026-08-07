"""历史:按日期持久化 web 对话 + 汇总当日表现(快照)。

- 每完成一轮 web 对话,append 到 data/conversations/YYYY-MM-DD.jsonl。
- 当日表现取 data/holdings_history/YYYY-MM-DD.md(定时任务/记账生成的每日快照)。
- 供左侧日历:哪些天有数据、点某天看那天的对话 + 表现。
"""

from __future__ import annotations

import json
from datetime import datetime

from core.config import data_dir


def _conv_dir():
    d = data_dir() / "conversations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_turn(user_text: str, assistant_text: str) -> None:
    if not (user_text or assistant_text):
        return
    day = datetime.now().strftime("%Y-%m-%d")
    rec = {"ts": datetime.now().strftime("%H:%M:%S"), "user": user_text, "assistant": assistant_text}
    with (_conv_dir() / f"{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def log_turn_open(user_text: str) -> None:
    """流式开始就先落一条(assistant 暂空),这样即使中途刷新/断连,用户消息也不丢。"""
    if not (user_text or "").strip():
        return
    day = datetime.now().strftime("%Y-%m-%d")
    rec = {"ts": datetime.now().strftime("%H:%M:%S"), "user": user_text, "assistant": ""}
    with (_conv_dir() / f"{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def log_turn_close(assistant_text: str) -> None:
    """流式结束回填最后一条的 assistant(即 log_turn_open 落的那条)。"""
    day = datetime.now().strftime("%Y-%m-%d")
    p = _conv_dir() / f"{day}.jsonl"
    if not p.exists():
        return
    lines = p.read_text(encoding="utf-8").splitlines()
    if not lines:
        return
    try:
        rec = json.loads(lines[-1])
    except json.JSONDecodeError:
        return
    rec["assistant"] = assistant_text
    lines[-1] = json.dumps(rec, ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def delete_turn(date: str, user_text: str) -> int:
    """按 user 文本删除当天某轮对话(彻底删档,刷新不再回来)。返回删除条数(0/1)。"""
    cp = _conv_dir() / f"{date}.jsonl"
    if not cp.exists() or not (user_text or "").strip():
        return 0
    target = user_text.strip()
    kept, removed = [], 0
    for line in cp.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and removed == 0:
            try:
                if (json.loads(s).get("user") or "").strip() == target:
                    removed += 1
                    continue
            except json.JSONDecodeError:
                pass
        if s:
            kept.append(line)
    cp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return removed


def days() -> list[str]:
    """有对话或有快照的日期(降序)。"""
    ds = set()
    cdir = _conv_dir()
    for p in cdir.glob("*.jsonl"):
        ds.add(p.stem)
    hdir = data_dir() / "holdings_history"
    if hdir.exists():
        for p in hdir.glob("*.md"):
            ds.add(p.stem)
    return sorted(ds, reverse=True)


def day(date: str) -> dict:
    convs = []
    cp = _conv_dir() / f"{date}.jsonl"
    if cp.exists():
        for line in cp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    convs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    snap = ""
    sp = data_dir() / "holdings_history" / f"{date}.md"
    if sp.exists():
        snap = sp.read_text(encoding="utf-8")
    return {"date": date, "conversations": convs, "snapshot_md": snap}
