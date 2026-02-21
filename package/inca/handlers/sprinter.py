# travel_v1/sprinter.py
"""
Sprinter: executes an existing intent document the same way Runner does,
but skips intent creation. Receives the intent document directly and runs
reducer -> tool queue -> applier.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .common.types import RunnerResult, SprinterHandlerReturn, SprinterPayload, ToolCall, handler_output
from .common.stores import WorkspaceTripStore
from .runner import Runner, RunnerContext, runner_context

from renglo.common import load_config
from renglo.agent.agent_utilities import AgentUtilities


class Sprinter(Runner):
    """
    Handler that receives an intent document and executes it the same way Runner does:
    reducer -> run_tool_queue_and_followups -> applier. Skips intent creation and
    user message routing.
    """

    name = "responses_mission_sprinter"

    def run(self, payload: SprinterPayload | Dict[str, Any]) -> SprinterHandlerReturn:
        """
        payload:
          portfolio, org, entity_type, entity_id, thread (required);
          trip_intent (required) — the intent document from another handler;
          connection_id (optional).
        """
        function = "run > sprinter"

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

        if "trip_intent" not in payload:
            out_err = {"ok": False, "trip_id": entity_id, "status": {"error": "No trip_intent provided"}}
            return {"success": False, "input": dict(payload), "output": out_err, "stack": []}
        trip_intent = dict(payload["trip_intent"])

        if entity_type == "org-trip":
            parts = entity_id.split("-", 1)
            trip_id = parts[1].strip() if len(parts) > 1 else entity_id
        else:
            trip_id = entity_id

        trip_intent["trip_id"] = trip_intent.get("trip_id") or trip_id

        self.AGU = AgentUtilities(
            self.config,
            portfolio,
            org,
            entity_type,
            entity_id,
            thread,
            connection_id=connection_id,
        )
        self.trip_store = WorkspaceTripStore(self.AGU)

        ctx = RunnerContext(
            portfolio=portfolio,
            org=org,
            entity_type=entity_type,
            entity_id=entity_id,
            thread=thread,
            connection_id=connection_id,
            trip_id=trip_id,
            user_text="",
        )
        self._set_context(ctx)

        stack: List[Dict[str, Any]] = []

        req = trip_intent.setdefault("request", {})
        req.setdefault("timezone", "America/New_York")
        req.setdefault("now_iso", None)
        req.setdefault("now_date", None)
        trip_intent["updated_at"] = int(time.time())

        self.trip_store.save(trip_id, trip_intent)

        conversation_history: List[Dict[str, str]] = []
        if self.AGU and hasattr(self.AGU, "get_message_history"):
            hist = self.AGU.get_message_history()
            if isinstance(hist, dict) and hist.get("success") and isinstance(hist.get("output"), list):
                conversation_history = hist["output"][-20:]

        reduced = self.reducer.run({
            "trip_intent": trip_intent,
            "event": {"type": "INTENT_READY", "data": {}},
            "conversation_history": conversation_history,
        })
        stack.append(reduced)
        out = handler_output(reduced)
        trip_intent = out["trip_intent"]
        self.trip_store.save(trip_id, trip_intent)

        ui_msgs = out.get("ui_messages") or []
        wm = trip_intent.get("working_memory") or {}
        ranked_bundles = wm.get("ranked_bundles") or []
        if ui_msgs and ranked_bundles:
            self.AGU.save_chat(ranked_bundles, interface="bundle", msg_type="widget")
            ui_msgs = [msg for msg in ui_msgs if not (isinstance(msg, str) and msg.strip().startswith("Here are the top options"))]
        for msg in ui_msgs:
            m = {"role": "assistant", "content": f"{msg}"}
            self.AGU.save_chat(m)

        initial_calls = [ToolCall(**tc) for tc in (out.get("tool_calls") or [])]
        trip_intent = self._run_tool_queue_and_followups(
            trip_id=trip_id,
            trip_intent=trip_intent,
            tool_queue=initial_calls,
            stack=stack,
            conversation_history=conversation_history,
        )

        self.trip_store.save(trip_id, trip_intent)
        output: RunnerResult = {"ok": True, "trip_id": trip_id, "status": trip_intent.get("status", {})}
        return {"success": True, "input": dict(payload), "output": output, "stack": stack}

    @classmethod
    def run_tests(cls) -> bool:
        """Run a minimal battery of tests for Sprinter. Returns True on success, raises on failure."""
        try:
            from renglo.common import load_config
            from renglo.agent.agent_utilities import AgentUtilities
        except ImportError:
            print("  Sprinter: skip (renglo not installed)")
            return True
        from unittest.mock import MagicMock, patch

        def mock_handler_call(portfolio: str, org: str, extension: str, handler: str, args: Dict[str, Any]) -> Dict[str, Any]:
            name = f"{extension}/{handler}" if extension else handler
            if "flight_quote_search" in name:
                return {"success": True, "output": {"options": []}}
            if "hotel_quote_search" in name:
                return {"success": True, "output": {"options": []}}
            if "trip_option_ranker" in name:
                return {"success": True, "output": {"bundles": []}}
            return {"success": True, "output": {}}

        sprinter = cls()
        mock_agu = MagicMock()
        mock_agu.print_chat = lambda s, *a: None
        mock_agu.save_chat = lambda *a, **kw: None
        mock_agu.get_message_history = lambda: {"success": True, "output": []}
        mock_agu.mutate_workspace = lambda changes: True
        mock_agu.get_active_workspace = lambda: {}
        mock_shc = MagicMock()
        mock_shc.handler_call = mock_handler_call
        sprinter.SHC = mock_shc

        trip_intent = {
            "trip_id": "test-sprint-1",
            "itinerary": {
                "segments": [
                    {"origin": {"code": "EWR"}, "destination": {"code": "DEN"}, "depart_date": "2026-06-01", "transport_mode": "flight"},
                    {"origin": {"code": "DEN"}, "destination": {"code": "EWR"}, "depart_date": "2026-06-05", "transport_mode": "flight"},
                ],
                "lodging": {"needed": True, "check_in": "2026-06-01", "check_out": "2026-06-05"},
            },
            "party": {"travelers": {"adults": 2, "children": 0, "infants": 0}},
            "status": {},
            "working_memory": {},
        }
        payload: SprinterPayload = {
            "portfolio": "p1",
            "org": "o1",
            "entity_type": "trip",
            "entity_id": "test-sprint-1",
            "thread": "th1",
            "trip_intent": trip_intent,
        }
        with patch("inca.handlers.runner.AgentUtilities", return_value=mock_agu):
            out = sprinter.run(payload)
        assert out.get("success") is True
        assert "input" in out and "output" in out and "stack" in out
        o = out["output"]
        assert o.get("ok") is True and o.get("trip_id") == "test-sprint-1" and "status" in o
        return True
