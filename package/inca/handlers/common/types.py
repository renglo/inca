# travel_v1/common/types.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, NotRequired, Optional, Protocol, Literal, TypedDict

# -----------------------------
# Handler return convention: {success, input, output, stack}
# -----------------------------


class HandlerResult(TypedDict):
    """
    Standard return shape for all handlers.
    output: canonical result the caller expects (typed per handler, e.g. RunnerResult).
    stack: list of handler return dicts from every call made inside run().
    """
    success: bool
    input: Dict[str, Any]
    output: Any
    stack: List[Dict[str, Any]]


def handler_output(result: Dict[str, Any]) -> Any:
    """
    Return the canonical output from a handler result.
    Accepts new format {success, input, output, stack} or legacy (whole dict is output).
    """
    if "success" in result and "output" in result and "stack" in result:
        return result["output"]
    return result


# -----------------------------
# Handler payload/result types (backwards compatible: dicts still valid)
# -----------------------------


class ReduceTripPayload(TypedDict):
    trip_intent: Dict[str, Any]
    event: Dict[str, Any]


class ReduceTripResult(TypedDict):
    trip_intent: Dict[str, Any]
    tool_calls: List[Dict[str, Any]]
    ui_messages: List[str]
    debug: Dict[str, Any]


class ApplyToolResultPayload(TypedDict):
    trip_intent: Dict[str, Any]
    tool_name: str
    result: Dict[str, Any]


class ApplyToolResultResult(TypedDict):
    trip_intent: Dict[str, Any]
    debug: Dict[str, Any]


class ApplyPatchPayload(TypedDict):
    trip_intent: Dict[str, Any]
    patch: Dict[str, Any]
    patch_source: NotRequired[str]
    note: NotRequired[str]


class ApplyPatchResult(TypedDict):
    trip_intent: Dict[str, Any]
    changed_paths: List[str]
    invalidations: Dict[str, Any]
    suggested_next_tools: List[str]


class RunnerPayload(TypedDict):
    portfolio: str
    org: str
    entity_type: str
    entity_id: str
    thread: str
    connection_id: NotRequired[str]
    data: NotRequired[str]
    user_text: NotRequired[str]


class SprinterPayload(TypedDict):
    portfolio: str
    org: str
    entity_type: str
    entity_id: str
    thread: str
    trip_intent: Dict[str, Any]
    connection_id: NotRequired[str]


class RunnerResult(TypedDict):
    ok: bool
    trip_id: str
    status: Dict[str, Any]


class SpecialistToolPayload(TypedDict):
    run_specialist: Callable[[str, Dict[str, Any]], Dict[str, Any]]
    arguments: Dict[str, Any]


class SpecialistToolResult(TypedDict):
    tool_name: str
    result: NotRequired[Dict[str, Any]]
    error: NotRequired[str]


# Typed handler returns: input/output use the handler's Payload/Result types
class PatcherHandlerReturn(TypedDict):
    success: bool
    input: ApplyPatchPayload
    output: ApplyPatchResult
    stack: List[Dict[str, Any]]


class ApplierHandlerReturn(TypedDict):
    success: bool
    input: ApplyToolResultPayload
    output: ApplyToolResultResult
    stack: List[Dict[str, Any]]


class ReducerHandlerReturn(TypedDict):
    success: bool
    input: ReduceTripPayload
    output: ReduceTripResult
    stack: List[Dict[str, Any]]


class RunnerHandlerReturn(TypedDict):
    success: bool
    input: RunnerPayload
    output: RunnerResult
    stack: List[Dict[str, Any]]


class SprinterHandlerReturn(TypedDict):
    success: bool
    input: SprinterPayload
    output: RunnerResult
    stack: List[Dict[str, Any]]


class SpecialistToolHandlerReturn(TypedDict):
    success: bool
    input: SpecialistToolPayload
    output: SpecialistToolResult
    stack: List[Dict[str, Any]]


# -----------------------------
# Core handler contract
# -----------------------------

class Handler(Protocol):
    """
    Standalone handler interface.
    Each handler must have a single entrypoint: run(payload) -> output dict.

    Implementations may narrow payload/result with TypedDicts; any dict
    conforming to that shape remains valid (backwards compatible).
    """
    name: str

    def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ...


# -----------------------------
# External hooks
# -----------------------------

RunSpecialistFn = Callable[[str, Dict[str, Any]], Dict[str, Any]]


# -----------------------------
# Events
# -----------------------------

EventType = Literal[
    "USER_MESSAGE",
    "USER_SELECTED_BUNDLE",
    "USER_REQUEST_HOLD",
    "USER_APPROVED_PURCHASE",
    "TOOL_RESULT",
    "TOOL_ERROR",
    "INTENT_READY",
]

@dataclass
class Event:
    type: EventType
    data: Dict[str, Any]


# -----------------------------
# Tool calls
# -----------------------------

@dataclass
class ToolCall:
    """
    Canonical representation of a tool request.

    call_id is optional (Responses API can provide an id, but v1 logic doesn't require it).
    """
    name: str
    arguments: Dict[str, Any]
    call_id: Optional[str] = None


# -----------------------------
# Stores (protocols are declared in common/stores.py)
# -----------------------------

class TripIntentStore(Protocol):
    def get(self, trip_id: str) -> Optional[Dict[str, Any]]: ...
    def save(self, trip_id: str, doc: Dict[str, Any]) -> None: ...


class ToolDefinitionsStore(Protocol):
    def get_tools(self, registry_key: str) -> List[Dict[str, Any]]: ...
    def get_system_prompt(self, registry_key: str) -> str: ...
    def get_developer_prompt(self, registry_key: str) -> str: ...


# -----------------------------
# OpenAI Responses adapter (protocol in common/openai_adapter.py)
# -----------------------------

class OpenAIResponsesClient(Protocol):
    def create_response(
        self,
        *,
        model: str,
        input_items: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        ...
