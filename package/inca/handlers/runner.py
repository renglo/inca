# travel_v1/runner.py
from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .common.types import Event, Handler, RunnerHandlerReturn, RunnerPayload, RunnerResult, ToolCall, handler_output
from .common.stores import DataControllerTripStore, InMemoryTripStore, InMemoryToolStore, TripIntentStore, ToolDefinitionsStore
from .common.defaults import default_developer_prompt, default_system_prompt, default_tools
from .common.openai_adapter import AgentUtilitiesOpenAIResponsesClient, extract_output_text, extract_tool_calls
from .applier import Applier
from .patcher import Patcher
from .reducer import Reducer

from renglo.common import load_config
from renglo.data.data_controller import DataController
from renglo.agent.agent_utilities import AgentUtilities
from renglo.schd.schd_controller import SchdController


@dataclass
class RunnerContext:
    """Request-scoped context for Runner; avoids passing payload-derived vars and using self.* (collision-safe)."""
    portfolio: str = ""
    org: str = ""
    entity_type: str = ""
    entity_id: str = ""
    thread: str = ""
    connection_id: Optional[str] = None
    trip_id: str = ""
    user_text: str = ""


runner_context: ContextVar[RunnerContext] = ContextVar("runner_context", default=RunnerContext())


class Runner(Handler):
    """
    Entrypoint handler (v1): responses_mission_runner

    Responsibilities:
      - Receive user message (trip_id + user_text)
      - Load TripIntent (or initialize)
      - Route message into a structured Event (bundle selection / hold / approve / plain message)
      - Run reducer
      - Execute reducer tool calls deterministically as a "mission queue":
          SHC.handler_call -> applier -> reducer(TOOL_RESULT) -> enqueue follow-ups
      - Optionally call OpenAI Responses API for extra tool calls and/or text
      - Emit UI output through self.AGU.print_chat only (no other handler prints)
      - Save TripIntent after each mutation
    """

    name = "responses_mission_runner"

    def __init__(self) -> None:
        self.config = load_config()
        self.DAC = DataController(config=self.config)
        self.SHC = SchdController(config=self.config)
        self.AGU = None
        self.trip_store: TripIntentStore = InMemoryTripStore()
        self.tool_store: ToolDefinitionsStore = InMemoryToolStore(
            tools=default_tools(),
            system_prompt=default_system_prompt(),
            developer_prompt=default_developer_prompt(),
        )
        self.openai_client = AgentUtilitiesOpenAIResponsesClient(get_agu=lambda: self.AGU)
        self.patcher = Patcher()
        self.applier = Applier(patcher=self.patcher)
        self.reducer = Reducer()

    def _get_context(self) -> RunnerContext:
        return runner_context.get()

    def _set_context(self, context: RunnerContext) -> None:
        runner_context.set(context)

    def _update_context(self, **kwargs: Any) -> None:
        ctx = self._get_context()
        for key, value in kwargs.items():
            setattr(ctx, key, value)
        self._set_context(ctx)

    # -------------------------------------------------------------------------
    # TripIntent initializer
    # -------------------------------------------------------------------------

    def _new_trip_intent(self, trip_id: str, user_message: str) -> Dict[str, Any]:
        now = int(time.time())
        return {
            "schema": "renglo.trip_intent.v1",
            "trip_id": trip_id,
            "created_at": now,
            "updated_at": now,
            "request": {"user_message": user_message, "locale": "en-US", "timezone": "America/New_York"},
            "status": {
                "phase": "intake",
                "state": "collecting_requirements",
                "missing_required": [],
                "assumptions": [],
                "notes": [],
            },
            "party": {
                "travelers": {"adults": 0, "children": 0, "infants": 0},
                "traveler_profile_ids": [],
                "contact": {"email": None, "phone": None},
            },
            "itinerary": {
                "trip_type": None,
                "segments": [],
                "lodging": {
                    "needed": True,
                    "check_in": None,
                    "check_out": None,
                    "rooms": 1,
                    "guests_per_room": 2,
                    "location_hint": None,
                    "stays": [],
                },
                "ground": {"needed": False},
            },
            "preferences": {"flight": {}, "hotel": {}},
            "constraints": {"budget_total": None, "currency": "USD", "refundable_preference": "either"},
            "policy": {"rules": {"require_user_approval_to_purchase": True, "holds_allowed_without_approval": True}},
            "working_memory": {
                "flight_quotes": [],
                "hotel_quotes": [],
                "flight_quotes_by_segment": [],
                "hotel_quotes_by_stay": [],
                "ranked_bundles": [],
                "risk_report": None,
                "selected": {"bundle_id": None, "flight_option_id": None, "hotel_option_id": None, "flight_option_ids": [], "hotel_option_ids": []},
                "holds": [],
                "bookings": [],
            },
            "audit": {"events": []},
        }

    # -------------------------------------------------------------------------
    # Lightweight user-message router (v1)
    # -------------------------------------------------------------------------

    def _route_user_message_to_event(self, user_text: str) -> Event:
        """
        Routes text to one of:
          - USER_SELECTED_BUNDLE: detects token "bndl_xxx"
          - USER_REQUEST_HOLD: detects word "hold"
          - USER_APPROVED_PURCHASE: detects "approve"/"confirm purchase"/"buy" AND
                expects approval_token=... and payment_method_id=... in text (v1 stub)
          - USER_MESSAGE otherwise
        """
        text = (user_text or "").strip()
        lower = text.lower()

        # bundle_id pattern
        m = re.search(r"\b(bndl_[A-Za-z0-9]+)\b", text)
        if m:
            return Event(type="USER_SELECTED_BUNDLE", data={"bundle_id": m.group(1)})

        # hold request
        if re.search(r"\b(hold|place hold|holds)\b", lower):
            return Event(type="USER_REQUEST_HOLD", data={})

        # purchase approval (stub parse)
        if "approve" in lower or "confirm purchase" in lower or re.search(r"\b(buy|purchase)\b", lower):
            am = re.search(r"approval_token\s*=\s*([^\s]+)", text)
            pm = re.search(r"payment_method_id\s*=\s*([^\s]+)", text)
            if am and pm:
                return Event(
                    type="USER_APPROVED_PURCHASE",
                    data={"approval_token": am.group(1), "payment_method_id": pm.group(1)},
                )
            # If user intended approval but didn't pass required fields, treat as normal message
            return Event(type="USER_MESSAGE", data={"text": user_text})

        return Event(type="USER_MESSAGE", data={"text": user_text})

    # -------------------------------------------------------------------------
    # Deterministic tool execution queue
    # -------------------------------------------------------------------------

    def _run_tool_queue_and_followups(
        self,
        *,
        trip_id: str,
        trip_intent: Dict[str, Any],
        tool_queue: List[ToolCall],
        stack: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Executes tool calls deterministically:
          - SHC.handler_call(portfolio, org, tool, handler, args) from context
          - applier applies result
          - reducer(TOOL_RESULT) emits follow-ups
          - follow-ups appended to queue immediately

        If stack is provided, appends each applier and reducer result to it.
        portfolio, org come from request context. UI output via self.AGU.print_chat.
        """
        ctx = self._get_context()
        portfolio = ctx.portfolio
        org = ctx.org

        queue: List[ToolCall] = list(tool_queue)
        if stack is None:
            stack = [] # This list will not be passed back to caller. 

        while queue:
            tc = queue.pop(0)
            try:
                parts = tc.name.split('/')
                if len(parts) == 1:
                    tool = "tools"
                    handler = tc.name
                elif len(parts) >= 2 and len(parts) <= 3:
                    tool = parts[0]
                    handler = '/'.join(parts[1:])
                else:
                    error_msg = f"❌ {tc.name} is not a valid tool. Use 'handler' or 'tool/handler' or 'tool/handler/subhandler'."
                    self.AGU.print_chat(error_msg, "error")
                    raise ValueError(error_msg)

                result = self.SHC.handler_call(portfolio, org, tool, handler, tc.arguments)
            except Exception as e:
                reduced_err = self.reducer.run({
                    "trip_intent": trip_intent,
                    "event": {"type": "TOOL_ERROR", "data": {"tool_name": tc.name, "error": str(e)}},
                })
                stack.append(reduced_err)
                out_err = handler_output(reduced_err)
                trip_intent = out_err["trip_intent"]
                self.trip_store.save(trip_id, trip_intent)
                for msg in (out_err.get("ui_messages") or []):
                    self.AGU.print_chat(msg)
                continue

            applied = self.applier.run({"trip_intent": trip_intent, "tool_name": tc.name, "result": result, "arguments": tc.arguments})
            stack.append(applied)
            out_applied = handler_output(applied)
            trip_intent = out_applied["trip_intent"]
            self.trip_store.save(trip_id, trip_intent)

            reduced = self.reducer.run({
                "trip_intent": trip_intent,
                "event": {"type": "TOOL_RESULT", "data": {"tool_name": tc.name, "result": result}},
            })
            stack.append(reduced)
            out_reduced = handler_output(reduced)
            trip_intent = out_reduced["trip_intent"]
            self.trip_store.save(trip_id, trip_intent)

            for msg in (out_reduced.get("ui_messages") or []):
                self.AGU.print_chat(msg)

            followups = [ToolCall(**x) for x in (out_reduced.get("tool_calls") or [])]
            queue.extend(followups)

        return trip_intent

    # -------------------------------------------------------------------------
    # Public entrypoint
    # -------------------------------------------------------------------------

    def run(self, payload: RunnerPayload | Dict[str, Any]) -> RunnerHandlerReturn:
        """
        payload:
          portfolio, org, entity_type, entity_id, thread (required);
          connection_id (optional);
          data or user_text (message content).

        trip_id is derived from entity_id. AgentUtilities (AGU) is initialized once from these variables.
        """
        connection_id: Optional[str] = payload.get("connectionId") or payload.get("connection_id")

        if "portfolio" not in payload:
            out_err: RunnerResult = {"ok": False, "trip_id": "", "status": {"error": "No portfolio provided"}}
            return {"success": False, "input": dict(payload), "output": out_err, "stack": []}
        portfolio = payload["portfolio"]

        if "org" not in payload:
            out_err = {"ok": False, "trip_id": "", "status": {"error": "No org provided"}}
            return {"success": False, "input": dict(payload), "output": out_err, "stack": []}
        org = payload["org"]

        if "entity_type" not in payload:
            out_err = {"ok": False, "trip_id": "", "status": {"error": "No entity_type provided"}}
            return {"success": False, "input": dict(payload), "output": out_err, "stack": []}
        entity_type = payload["entity_type"]

        if "entity_id" not in payload:
            out_err = {"ok": False, "trip_id": "", "status": {"error": "No entity_id provided"}}
            return {"success": False, "input": dict(payload), "output": out_err, "stack": []}
        entity_id = payload["entity_id"]

        if "thread" not in payload:
            out_err = {"ok": False, "trip_id": entity_id, "status": {"error": "No thread provided"}}
            return {"success": False, "input": dict(payload), "output": out_err, "stack": []}
        thread = payload["thread"]

        self.trip_store = DataControllerTripStore(self.DAC, portfolio, org)

        user_text: str = (payload.get("data") or payload.get("user_text") or "").strip()

        if entity_type == "org-trip":
            parts = entity_id.split("-", 1)
            trip_id = parts[1].strip() if len(parts) > 1 else entity_id
        else:
            trip_id = entity_id

        self.AGU = AgentUtilities(
            self.config,
            portfolio,
            org,
            entity_type,
            entity_id,
            thread,
            connection_id=connection_id,
        )

        ctx = RunnerContext(
            portfolio=portfolio,
            org=org,
            entity_type=entity_type,
            entity_id=entity_id,
            thread=thread,
            connection_id=connection_id,
            trip_id=trip_id,
            user_text=user_text,
        )
        self._set_context(ctx)

        # Load or init TripIntent
        trip_intent = self.trip_store.get(trip_id)
        if not trip_intent:
            trip_intent = self._new_trip_intent(trip_id, user_text)

        # Update request context + timestamps
        trip_intent.setdefault("request", {})["user_message"] = user_text
        trip_intent["updated_at"] = int(time.time())

        # Route user message -> Event
        event = self._route_user_message_to_event(user_text)

        # Add event to the audit (The audit shows the execution event and its timestamp)
        trip_intent.setdefault("audit", {}).setdefault("events", []).append({
            "ts": int(time.time()),
            "type": event.type,
            "data": event.data,
        })

        # 1) Reduce the event
        stack: List[Dict[str, Any]] = []
        reduced = self.reducer.run({"trip_intent": trip_intent, "event": {"type": event.type, "data": event.data}})
        stack.append(reduced)
        out = handler_output(reduced)
        trip_intent = out["trip_intent"]
        self.trip_store.save(trip_id, trip_intent)

        for msg in (out.get("ui_messages") or []):
            self.AGU.print_chat(msg)

        # 2) Execute reducer tool calls deterministically + followups
        initial_calls = [ToolCall(**tc) for tc in (out.get("tool_calls") or [])]
        trip_intent = self._run_tool_queue_and_followups(
            trip_id=trip_id,
            trip_intent=trip_intent,
            tool_queue=initial_calls,
            stack=stack,
        )

        # 3) Optional: allow model to speak or request additional tools (default off; not used when AGU provides run_specialist)
        registry_key = "travel_booking_v1"
        max_model_turns = 0
        if max_model_turns > 0:
            tools = self.tool_store.get_tools(registry_key)
            system_prompt = self.tool_store.get_system_prompt(registry_key)
            developer_prompt = self.tool_store.get_developer_prompt(registry_key)
            input_items: List[Dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "developer", "content": developer_prompt},
                {"role": "developer", "content": "TRIP_INTENT_JSON:\n" + json.dumps(trip_intent)},
                {"role": "user", "content": user_text},
            ]

            for _ in range(max_model_turns):
                resp = self.openai_client.create_response(input_items=input_items, tools=tools)
                out_text = extract_output_text(resp)
                if out_text:
                    self.AGU.print_chat(out_text)

                model_calls = extract_tool_calls(resp)
                if not model_calls:
                    break

                trip_intent = self._run_tool_queue_and_followups(
                    trip_id=trip_id,
                    trip_intent=trip_intent,
                    tool_queue=model_calls,
                    stack=stack,
                )

                input_items.append({"role": "developer", "content": "TRIP_INTENT_JSON:\n" + json.dumps(trip_intent)})

        self.trip_store.save(trip_id, trip_intent)
        output: RunnerResult = {"ok": True, "trip_id": trip_id, "status": trip_intent.get("status", {})}
        return {"success": True, "input": dict(payload), "output": output, "stack": stack}

    @classmethod
    def run_tests(cls) -> bool:
        """Run a minimal battery of tests for this handler. Returns True on success, raises on failure. Skips if renglo is not installed."""
        try:
            from renglo.common import load_config
            from renglo.agent.agent_utilities import AgentUtilities
        except ImportError:
            import sys
            print("  Runner: skip (renglo not installed)")
            return True
        from unittest.mock import MagicMock, patch
        runner = cls()
        mock_agu = MagicMock()
        mock_agu.print_chat = lambda s, *a: None
        extract_result = {
            "trip_intent": {"origin": "EWR", "destination": "DEN", "travelers": {"adults": 2}, "dates": {"departure_date": "2025-06-01", "return_date": "2025-06-05"}},
            "missing_required_fields": [],
        }
        mock_shc = MagicMock()
        mock_shc.handler_call.return_value = extract_result
        runner.SHC = mock_shc
        payload: RunnerPayload = {
            "portfolio": "p1",
            "org": "o1",
            "entity_type": "trip",
            "entity_id": "test-run-1",
            "thread": "th1",
            "data": "fly EWR to DEN",
        }
        with patch("handlers.runner.AgentUtilities", return_value=mock_agu):
            out = runner.run(payload)
        assert out.get("success") is True
        assert "input" in out and "output" in out and "stack" in out
        o = out["output"]
        assert o.get("ok") is True and o.get("trip_id") == "test-run-1" and "status" in o
        assert len(out["stack"]) >= 1, "reducer at least once in stack"
        return True