"""Model providers behind one interface: `llm.messages.create(...)` in the Anthropic shape.

The agent loop speaks Anthropic-style messages (text / tool_use / tool_result blocks).
OpenAICompatLLM translates to and from OpenAI chat-completions, which OpenRouter and
Mistral both accept. Composio's AnthropicProvider formats the live tool schemas; this
adapter converts them, so the Composio side is the same for every provider.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

def make_llm(cfg: Any) -> tuple[Any, str]:
    """(client, model name) for the configured provider. Call cfg.llm_problems() first."""
    if cfg.llm_provider == "anthropic":
        import anthropic

        return anthropic.Anthropic(api_key=cfg.anthropic_api_key), cfg.anthropic_model
    if cfg.llm_provider == "openai_compatible":
        import openai

        return OpenAICompatLLM(openai.OpenAI(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url)), cfg.llm_model
    raise ValueError(f"unknown LLM_PROVIDER {cfg.llm_provider!r}")


def _field(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class OpenAICompatLLM:
    """Wraps an `openai.OpenAI`-like client so it looks like `anthropic.Anthropic().messages`."""

    def __init__(self, client: Any):
        self._client = client
        self.messages = self

    def create(self, *, model: str, max_tokens: int, system: str, tools: list[dict], messages: list[dict], **_: Any):
        resp = self._client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}] + _to_openai_messages(messages),
            tools=[_to_openai_tool(t) for t in tools],
        )
        if not getattr(resp, "choices", None):
            raise RuntimeError(f"model returned no choices: {str(resp)[:200]}")
        choice = resp.choices[0]
        msg = choice.message
        content: list[SimpleNamespace] = []
        if msg.content:
            content.append(SimpleNamespace(type="text", text=msg.content))
        for i, call in enumerate(msg.tool_calls or []):
            raw = call.function.arguments or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = None
            if not isinstance(args, dict):
                # Keep the call visible so the policy refuses it and the ledger records it.
                args = {"_unparseable_arguments": raw[:500]}
            # Some providers omit ids; the tool result must still point back at its call.
            call_id = call.id or f"call_{i}"
            content.append(SimpleNamespace(type="tool_use", id=call_id, name=call.function.name, input=args))
        if any(b.type == "tool_use" for b in content):
            stop = "tool_use"
        else:
            stop = "max_tokens" if choice.finish_reason == "length" else "end_turn"
        return SimpleNamespace(stop_reason=stop, content=content)


def _to_openai_tool(tool: dict) -> dict:
    return {"type": "function", "function": {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
    }}


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = "\n".join(_field(b, "text") or "" for b in content if _field(b, "type") == "text").strip()
            calls = [{"id": _field(b, "id"), "type": "function", "function": {
                "name": _field(b, "name"), "arguments": json.dumps(_field(b, "input") or {})}}
                for b in content if _field(b, "type") == "tool_use"]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        for b in content:  # a user turn carrying tool results
            if _field(b, "type") == "tool_result":
                body = str(_field(b, "content", ""))
                out.append({"role": "tool", "tool_call_id": _field(b, "tool_use_id"),
                            "content": f"ERROR: {body}" if _field(b, "is_error") else body})
            elif _field(b, "type") == "text":
                out.append({"role": "user", "content": _field(b, "text", "")})
    return out
