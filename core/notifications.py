"""通知流:定时任务/价格告警的产出,除了发飞书/微信,也进这里供 web 展示。

内存环形(最近 50 条)+ 落 data/notifications/YYYY-MM-DD.jsonl。
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime

from core.config import data_dir

_recent: deque = deque(maxlen=50)


def _summarize(text: str) -> str:
    """铃铛里只放简短摘要:取首个有效行(去掉 markdown 标记),截断。"""
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("#>*-•　 ").strip()
        line = line.replace("**", "")
        if len(line) >= 4:
            return line[:46] + ("…" if len(line) > 46 else "")
    return (text or "").strip()[:46]


def push(label: str, text: str, summary: str | None = None) -> None:
    now = datetime.now()
    item = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "label": label, "text": text,
            "summary": (summary or _summarize(text))}
    _recent.append(item)
    try:
        d = data_dir() / "notifications"
        d.mkdir(parents=True, exist_ok=True)
        with (d / f"{now.strftime('%Y-%m-%d')}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent(n: int = 30) -> list[dict]:
    """读最近通知:优先持久化文件(跨进程/重启不丢),取最近几天合并后最新 N 条。"""
    items: list[dict] = []
    d = data_dir() / "notifications"
    if d.exists():
        files = sorted(d.glob("*.jsonl"), reverse=True)[:5]  # 最近 5 天
        for p in sorted(files):  # 旧→新
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    if not items:  # 无文件时回退内存
        items = list(_recent)
    return items[-n:][::-1]  # 最新在前
