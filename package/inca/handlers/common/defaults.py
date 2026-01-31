# travel_v1/common/defaults.py
from __future__ import annotations

from typing import Any, Dict, List


def default_system_prompt() -> str:
    """
    System prompt (v1). Keep this short and stable.
    Most behavioral guidance should live in:
      - tool schemas
      - reducer logic (deterministic)
      - developer prompt
    """
    return (
        "You are TripOrchestrator.\n"
        "- Ask only for missing REQUIRED info; ask at most 3 questions at a time.\n"
        "- Prefer progress: quote flights/hotels, rank bundles, then risk-check before holds.\n"
        "- Never purchase without explicit user approval.\n"
        "- Do not invent provider data; rely only on tool outputs.\n"
    )


def default_developer_prompt() -> str:
    """
    Developer prompt (v1). This is where you describe your custom rules,
    tool usage guidance, and structure expectations.

    In production, store this in your ToolDefinitionsStore (doc DB),
    versioned by registry_key.
    """
    return (
        "TripIntent is the canonical state and is injected as TRIP_INTENT_JSON.\n"
        "Guidance:\n"
        "- If requirements change, re-quote and re-rank; do not reuse stale quotes.\n"
        "- Always perform policy_and_risk_check before creating holds.\n"
        "- Holds are allowed without approval; purchase requires explicit approval.\n"
        "- When calling tools, use the tool schemas precisely.\n"
    )


def default_tools() -> List[Dict[str, Any]]:
    """
    Placeholder tool definitions for local testing.

    In production, the full function/tool schemas should be stored in your doc DB.
    Keep these permissive for now (additionalProperties=True), but you will likely
    tighten them as you stabilize specialist tool contracts.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "trip_requirements_extract",
                "description": "Extract structured trip requirements from a user message and identify missing required fields.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_message": {"type": "string"},
                        "context": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["user_message"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "flight_quote_search",
                "description": "Search for flight quote options (no hold, no purchase).",
                "parameters": {"type": "object", "additionalProperties": True},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hotel_quote_search",
                "description": "Search for hotel quote options (no hold, no purchase).",
                "parameters": {"type": "object", "additionalProperties": True},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "trip_option_ranker",
                "description": "Rank/compose a small set of flight+hotel bundles with tradeoffs.",
                "parameters": {"type": "object", "additionalProperties": True},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "policy_and_risk_check",
                "description": "Evaluate selected options against policy and common travel risks.",
                "parameters": {"type": "object", "additionalProperties": True},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reservation_hold_create",
                "description": "Create temporary holds for selected options (flight/hotel). Must be idempotent.",
                "parameters": {"type": "object", "additionalProperties": True},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "booking_confirm_and_purchase",
                "description": "Finalize booking/purchase after explicit user approval. Must be idempotent.",
                "parameters": {"type": "object", "additionalProperties": True},
            },
        },
    ]
