"""LLM provider 适配（精简版，OpenAI 兼容）。

TradeAssistant 默认走 glm-5.2（xinlicloud 中转，OpenAI 兼容）。这里只保留必要的
payload 构造 + reasoning 抽取，不搬 digital-life 的全套家族分支。

- 出站 reasoning：GLM/DeepSeek/Kimi/Qwen 四家 OpenAI 兼容 API 都用 message.reasoning_content
  （流式为 delta.reasoning_content），同名，直接读。
- 工具调用：标准 OpenAI tools / tool_calls 协议。
"""

from __future__ import annotations

from typing import Any


def build_payload(
    model_cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    stream: bool = False,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model_cfg["name"],
        "messages": messages,
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if stream:
        # 让中转在流式结尾带上 usage
        payload["stream_options"] = {"include_usage": True}
    # Qwen3 系:显式开启思考(reasoning_content)。仅流式;由请求的 thinking 开关控制(默认关)
    name = str(model_cfg.get("name", "")).lower()
    if stream and "qwen" in name and model_cfg.get("thinking") is True:
        payload["enable_thinking"] = True
    return payload


def extract_reasoning(message: dict[str, Any]) -> str:
    """从非流式返回的 message 里取思考内容（reasoning_content）。"""
    return str(message.get("reasoning_content") or "").strip()


def chat_url(base_url: str) -> str:
    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"
