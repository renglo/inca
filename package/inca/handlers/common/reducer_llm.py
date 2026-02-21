# travel_v1/common/reducer_llm.py
"""
LLM client for reducer intent classification.

Used for: confirmation detection, intent classification, error recovery.
When no client is provided, reducer falls back to programmatic logic.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Protocol


class ReducerLLMClient(Protocol):
    """
    Minimal interface for reducer LLM classification.

    Used when state is awaiting_confirmation or when handling TOOL_ERROR.
    """

    def classify_confirmation(self, user_message: str, trip_summary: str) -> bool:
        """
        Return True if user_message indicates confirmation to proceed (e.g. search for flights).
        Return False if it's a change request, question, or unclear.
        """
        ...

    def infer_clarifying_question(
        self,
        user_message: str,
        conversation_history: list,
        trip_summary: str,
    ) -> Optional[str]:
        """
        Interpret the user message and conversation. Return a clarifying question if the user
        asked to change something but did NOT specify the new value. Return None if they
        already specified it (e.g. "March 10", "two days before") or if no change request.
        """
        ...

    def classify_error_recovery(
        self, user_message: str, tool_name: str, error_text: str
    ) -> Dict[str, Any]:
        """
        Return intent for error recovery: {"retry": bool, "modify_and_retry": bool, "suggested_changes": [...]}.
        """
        ...


class NoOpReducerLLMClient:
    """
    No-op client: uses programmatic heuristics. No LLM calls.
    """

    def classify_confirmation(self, user_message: str, trip_summary: str) -> bool:
        return _programmatic_is_confirmation(user_message)

    def infer_clarifying_question(
        self,
        user_message: str,
        conversation_history: list,
        trip_summary: str,
    ) -> Optional[str]:
        return None

    def classify_error_recovery(
        self, user_message: str, tool_name: str, error_text: str
    ) -> Dict[str, Any]:
        t = (user_message or "").strip().lower()
        retry_phrases = ("try again", "retry", "please try", "go again")
        return {
            "retry": any(p in t for p in retry_phrases) or len(t) < 20 and "yes" in t,
            "modify_and_retry": False,
            "suggested_changes": [],
        }


def _programmatic_is_confirmation(text: str) -> bool:
    """Programmatic fallback for confirmation detection."""
    t = (text or "").strip().lower()
    if not t:
        return False
    confirmations = (
        "yes", "y", "ok", "okay", "looks good", "look good", "go ahead", "correct",
        "that's right", "thats right", "confirm", "proceed", "search", "find flights",
        "find hotels", "get quotes", "sounds good", "perfect", "good", "continue",
        "book it", "that works",
    )
    if t in confirmations:
        return True
    if len(t) <= 4 and t in ("yes", "y", "ok"):
        return True
    if any(t.startswith(c) for c in ("yes ", "yes,", "ok ", "ok,", "sure ", "go ahead")):
        return True
    if len(t) < 50 and ("go ahead" in t or "looks good" in t or "sounds good" in t or "let's go" in t or "that works" in t or "book it" in t):
        return True
    return False


class ReducerLLMClientFromOpenAI:
    """
    LLM client that uses OpenAI Responses API via a create_response callable.

    Expects create_response(input_items=[...], tools=[]) and returns
    {"output_text": "..."} with JSON in output_text.
    """

    def __init__(
        self,
        create_response: Callable[..., Dict[str, Any]],
        model: Optional[str] = None,
    ) -> None:
        self.create_response = create_response
        self.model = model

    def classify_confirmation(self, user_message: str, trip_summary: str) -> bool:
        prompt = (
            f"Trip summary shown to user:\n{trip_summary[:500]}\n\n"
            f"User reply: \"{user_message}\"\n\n"
            "Does the user confirm they want to proceed with searching for flights/hotels? "
            "Answer with JSON only: {\"is_confirmation\": true} or {\"is_confirmation\": false}. "
            "If they ask to change something, say no. If unclear, say no."
        )
        try:
            resp = self.create_response(
                input_items=[{"role": "user", "content": prompt}],
                tools=[],
                model=self.model,
            )
            text = (resp.get("output_text") or "").strip()
            if not text:
                return _programmatic_is_confirmation(user_message)
            # Extract JSON from response (may be wrapped in markdown)
            match = re.search(r"\{[^{}]*\"is_confirmation\"[^{}]*\}", text)
            if match:
                data = json.loads(match.group())
                return bool(data.get("is_confirmation", False))
        except Exception:
            pass
        return _programmatic_is_confirmation(user_message)

    def infer_clarifying_question(
        self,
        user_message: str,
        conversation_history: list,
        trip_summary: str,
    ) -> Optional[str]:
        conv_text = ""
        if conversation_history:
            parts: List[str] = []
            for m in conversation_history[-8:]:
                role = (m.get("role") or "user").lower()
                content = m.get("content") or ""
                if isinstance(content, str) and content.strip():
                    parts.append(f"{role}: {content[:300]}")
            conv_text = "\n".join(parts)
        prompt = (
            "You are helping with a travel booking flow. The user just saw a trip summary and replied.\n\n"
            f"Trip summary:\n{trip_summary[:600]}\n\n"
            f"Recent conversation:\n{conv_text or '(none)'}\n\n"
            f"User's latest message: \"{user_message}\"\n\n"
            "Interpret the message. If the user wants to CHANGE something (dates, destination, origin, travelers, etc.) "
            "but did NOT specify the new value, return a short clarifying question. "
            "If they ALREADY specified it (e.g. 'change departure to March 10', 'depart two days before', "
            "'make it Miami', 'add one adult'), return null—no question needed. "
            "Use the conversation to interpret relative references ('two days earlier' = 2 days before current departure, etc.).\n\n"
            "Return JSON only: {\"question\": \"What date would you like to depart?\"} or {\"question\": null}."
        )
        try:
            resp = self.create_response(
                input_items=[{"role": "user", "content": prompt}],
                tools=[],
                model=self.model,
            )
            text = (resp.get("output_text") or "").strip()
            if text:
                match = re.search(r"\{[^{}]*(?:\"[^\"]*\"[^{}]*)*\}", text, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    q = data.get("question")
                    if isinstance(q, str) and q.strip():
                        return q.strip()
        except Exception:
            pass
        return None

    def classify_error_recovery(
        self, user_message: str, tool_name: str, error_text: str
    ) -> Dict[str, Any]:
        prompt = (
            f"Tool {tool_name} failed: {error_text}\n\n"
            f"User said: \"{user_message}\"\n\n"
            "Return JSON: {\"retry\": bool, \"modify_and_retry\": bool, \"suggested_changes\": []}. "
            "retry=true if user wants to try again. modify_and_retry=true if they want to change something first."
        )
        try:
            resp = self.create_response(
                input_items=[{"role": "user", "content": prompt}],
                tools=[],
                model=self.model,
            )
            text = (resp.get("output_text") or "").strip()
            if text:
                match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    return {
                        "retry": bool(data.get("retry", False)),
                        "modify_and_retry": bool(data.get("modify_and_retry", False)),
                        "suggested_changes": data.get("suggested_changes") or [],
                    }
        except Exception:
            pass
        return NoOpReducerLLMClient().classify_error_recovery(
            user_message, tool_name, error_text
        )
