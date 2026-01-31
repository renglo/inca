# travel_v1/common/openai_adapter.py
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Protocol

from .types import ToolCall


class OpenAIResponsesClient(Protocol):
    """
    Minimal adapter interface for the OpenAI Responses API.
    Implementations should return a dict-like response.

    Expected (best-effort) keys in response:
      - "output_text": str (optional)
      - "output": list[dict] (optional) where items may include tool calls

    This stays intentionally loose to accommodate SDK changes and custom wrappers.
    """

    def create_response(
        self,
        *,
        input_items: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        ...


class NoOpOpenAIResponsesClient(OpenAIResponsesClient):
    """Default in-memory client; returns empty response (no model calls)."""

    def create_response(
        self,
        *,
        input_items: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {"output_text": "", "output": []}


class AgentUtilitiesOpenAIResponsesClient(OpenAIResponsesClient):
    """
    Delegates to AgentUtilities.llm_responses using a getter so Runner
    can pass AGU without re-initializing (AGU is set in run(), not __init__).
    """

    def __init__(self, get_agu: Callable[[], Any]) -> None:
        self.get_agu = get_agu

    def create_response(
        self,
        *,
        input_items: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        agu = self.get_agu()
        if agu is None:
            return {"output_text": "", "output": []}
        return agu.llm_responses(input_items=input_items, tools=tools, model=model)


def extract_output_text(resp: Dict[str, Any]) -> str:
    """
    Best-effort extraction of assistant text from a Responses API response dict.
    """
    text = resp.get("output_text")
    if isinstance(text, str):
        return text.strip()

    # Some wrappers might provide "text" or "message"
    for k in ("text", "message", "content"):
        v = resp.get(k)
        if isinstance(v, str):
            return v.strip()

    return ""


def extract_tool_calls(resp: Dict[str, Any]) -> List[ToolCall]:
    """
    Best-effort tool call extraction from a Responses API response dict.

    Supports common shapes:
      resp["output"] = [
        {"type":"tool_call","id":"...","name":"...","arguments":{...}},
        {"type":"function_call","id":"...","function":{"name":"...","arguments":"{...json...}"}}
      ]

    Also tolerates:
      {"type":"tool_call","tool_call_id":"...","name":"...","arguments":"{...json...}"}
    """
    calls: List[ToolCall] = []
    output = resp.get("output") or []
    if not isinstance(output, list):
        return calls

    for item in output:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t not in ("tool_call", "function_call"):
            continue

        call_id = item.get("id") or item.get("tool_call_id")

        # name can be at top-level or inside "function"
        name = item.get("name")
        fn = item.get("function")
        if not name and isinstance(fn, dict):
            name = fn.get("name")

        if not name or not isinstance(name, str):
            continue

        args: Any = item.get("arguments")
        if args is None and isinstance(fn, dict):
            args = fn.get("arguments")

        # args may arrive as JSON string or dict
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"_raw": args}

        if args is None:
            args = {}
        if not isinstance(args, dict):
            # be permissive; wrap non-dict
            args = {"_value": args}

        calls.append(ToolCall(name=name, arguments=args, call_id=call_id))

    return calls


# -----------------------------------------------------------------------------
# Optional: example SDK wrapper (pseudo-code)
# -----------------------------------------------------------------------------
#
# If you use the official OpenAI Python SDK, you can wrap it like:
#
# class OpenAIClientWrapper(OpenAIResponsesClient):
#     def __init__(self, openai_sdk_client: Any):
#         self.client = openai_sdk_client
#
#     def create_response(self, *, model: str, input_items: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
#         # This depends on your SDK version. Keep it in your app layer.
#         resp = self.client.responses.create(
#             model=model,
#             input=input_items,
#             tools=tools,
#         )
#         # Normalize to dict
#         return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
#
# -----------------------------------------------------------------------------
