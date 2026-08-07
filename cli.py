"""命令行跑 TradeAssistant 主 agent（验证/调试用）。

用法：python3 cli.py "你的问题"
"""

from __future__ import annotations

import asyncio
import sys

# 导入即注册工具
import core.tools.market_tools  # noqa: F401
import core.tools.assets  # noqa: F401
import core.tools.ledger_tools  # noqa: F401
from core.agents_factory import primary_agent


async def main(question: str) -> None:
    agent = primary_agent()
    mode = None
    async for ev in agent.run_stream([{"role": "user", "content": question}]):
        t = ev["type"]
        if t == "thinking":
            if mode != "think":
                print("\n\033[90m[思考] ", end="", flush=True); mode = "think"
            print(ev["delta"], end="", flush=True)
        elif t == "content":
            if mode != "content":
                print("\033[0m\n[回答] ", end="", flush=True); mode = "content"
            print(ev["delta"], end="", flush=True)
        elif t == "activity":
            ph = ev["phase"]
            if ph == "start":
                print(f"\n\033[36m[工具→ {ev['tool']}] {ev.get('args')}\033[0m", flush=True); mode = None
            else:
                print(f"\033[36m[工具← {ev['tool']}] {ev.get('result_preview','')[:120]}\033[0m", flush=True); mode = None
        elif t == "done":
            print("\n\033[0m--- done ---")


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "看一下我现在的持仓和翻倍进度"
    asyncio.run(main(q))
