# travel_v1/runner.py
from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from decimal import Decimal
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


def _json_serializable_default(obj: Any) -> Any:
    """Default for json.dumps so Decimal and other non-JSON types are serializable."""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


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
      - For USER_MESSAGE, reducer returns only trip_requirements_extract so memorialization
        runs and is persisted before any quote/search tools (which may fail) are attempted.
      - Optionally call OpenAI Responses API for extra tool calls and/or text
      - Emit UI output through self.AGU.save_chat only (no other handler prints)
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
            "request": {
                "user_message": user_message,
                "locale": "en-US",
                "timezone": "America/New_York",
                "now_iso": None,
                "now_date": None,
            },
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
                "guests": [],
                "contact": {"email": None, "phone": None},
            },
            "itinerary": {
                "trip_type": None,
                "segments": [],
                "lodging": {
                    "needed": True,
                    "check_in": None,
                    "check_out": None,
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
    # trip_requirements_extract (LLM-based, uses self.AGU.llm)
    # -------------------------------------------------------------------------

    def _call_trip_requirements_extract(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract trip requirements from user_message using LLM. Merges with context.current_intent.
        Returns the format expected by the applier: trip_intent, missing_required_fields, clarifying_questions.
        """
        user_message = (arguments.get("user_message") or "").strip()
        context = arguments.get("context") or {}
        timezone = context.get("timezone", "America/New_York")
        current_intent = context.get("current_intent") or {}

        try:
            tz = ZoneInfo(timezone) if timezone else ZoneInfo("America/New_York")
        except Exception:
            tz = ZoneInfo("America/New_York")
        now_dt = datetime.now(tz)
        now_iso = now_dt.isoformat()
        now_date = now_dt.strftime("%Y-%m-%d")

        prompt_text = f"""You are a travel requirements extractor. Humans communicate in fragments and often change their mind. Your job is to incrementally assemble the trip from whatever the user says and the current state.

Time context (use for all date decisions):
- Today's date and time (user timezone): {now_iso}
- Today's date (YYYY-MM-DD): {now_date}

Rules:
- Merge this message with current_intent: add, update, or remove only what this message implies. Output only fields you can infer from this message; leave others absent so they are merged from current state. The user may correct themselves (e.g. "actually 2 adults") or add one detail at a time.
- CRITICAL — Preserve full itinerary on partial corrections: When current_intent already has multiple segments and/or stays (multi-city), and the user message only corrects or adds ONE detail (e.g. "we depart from JFK", "remember we're flying from JFK", "departure is June 1st", "actually 2 adults"), you MUST output the SAME number and sequence of segments and stays as in current_intent. Only update the specific field mentioned (e.g. set first segment origin to JFK). Do NOT output a shorter or simplified itinerary that drops cities already in current_intent. If the user fully rephrases the trip ("we're doing X then Y then Z"), then output the new full itinerary; but for short corrections or reminders, preserve every segment and stay.
- Dates must be YYYY-MM-DD. All trip dates (departure_date, return_date, check_in, check_out) must be on or after today ({now_date}). If the user says a date without a year or a date in the past, use the next occurrence in the future (e.g. if today is 2026-01-29 and the user says "March 12", use 2026-03-12).
- Origin/destination: use IATA airport codes when possible (e.g. Newark->EWR, JFK, San Francisco->SFO, Los Angeles->LAX, Dallas->DFW, Miami->MIA, Orlando->MCO).
- Multi-city: When the user says they fly to multiple cities in sequence (e.g. "Dallas to Miami for 3 days then to Orlando for 2 days"), you MUST output "segments" (one flight leg per segment) and "stays" (one stay per city). First segment origin = departure city (e.g. JFK if "flying from JFK"); then one leg per city-to-city; include return to origin as last segment. Example: "JFK to San Francisco then LA then back to JFK" → segments = [{{"origin": "JFK", "destination": "SFO", "depart_date": "..."}}, {{"origin": "SFO", "destination": "LAX", "depart_date": "..."}}, {{"origin": "LAX", "destination": "JFK", "depart_date": "..."}}]; stays = [{{"location_code": "SFO", "check_in": "...", "check_out": "..."}}, {{"location_code": "LAX", "check_in": "...", "check_out": "..."}}]. Infer dates: "3 days" means check_out = check_in + 3 days; next stay's check_in = previous stay's check_out; next segment's depart_date = day user leaves that city (e.g. same as that stay's check_out).
- travelers: object with adults (required), children, infants (integers, default 0).
- List missing_required_fields as paths still needed for quoting: e.g. ["party.travelers.adults", "itinerary.lodging.stays[0].check_in", "itinerary.segments[0].destination.code"]. For multi_city include each segment and each stay (e.g. itinerary.segments[1].destination.code, itinerary.lodging.stays[1].location_code). Use [] when nothing is missing.
- clarifying_questions: array of strings (can be empty).
Return ONLY valid JSON, no markdown or explanation.

Output schema (return exactly this structure; for multi_city include segments and stays arrays):
{{
  "trip_intent": {{
    "origin": "IATA or null (first segment origin if multi_city)",
    "destination": "IATA or null (first segment destination if multi_city)",
    "trip_type": "one_way|round_trip|multi_city or null",
    "dates": {{ "departure_date": "YYYY-MM-DD or null", "return_date": "YYYY-MM-DD or null" }},
    "segments": [{{ "origin": "IATA", "destination": "IATA", "depart_date": "YYYY-MM-DD" }}] or omit if single origin/destination,
    "stays": [{{ "location_code": "IATA or city code", "check_in": "YYYY-MM-DD", "check_out": "YYYY-MM-DD" }}] or omit if single lodging,
    "travelers": {{ "adults": number, "children": number, "infants": number }},
    "lodging": {{ "needed": true, "check_in": "YYYY-MM-DD or null", "check_out": "YYYY-MM-DD or null" }},
    "cabin": "economy or null",
    "constraints": {{ "max_stops": number, "avoid_red_eye": boolean }}
  }},
  "missing_required_fields": ["path1", "path2"],
  "clarifying_questions": ["question1"]
}}

User message: {user_message}

Current intent (merge with this; only overwrite fields the user message provides):
{json.dumps(current_intent, indent=2, default=_json_serializable_default)}

Timezone: {timezone}
Today (YYYY-MM-DD): {now_date}
"""

        try:
            prompt = {
                "model": getattr(self.AGU, "AI_2_MODEL", "gpt-4o-mini"),
                "messages": [{"role": "user", "content": prompt_text}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            response = self.AGU.llm(prompt)
            print(f'LLM Response>> Requirement Extraction:{response}')
            if not response or not getattr(response, "content", None):
                return {
                    "success": False,
                    "trip_intent": {},
                    "missing_required_fields": [
                        "party.travelers.adults",
                        "itinerary.segments",
                        "itinerary.lodging.check_in",
                        "itinerary.lodging.check_out",
                    ],
                    "clarifying_questions": ["I couldn't parse that. Can you tell me origin, destination, dates, and number of travelers?"],
                }
            raw = response.content
            if hasattr(self.AGU, "clean_json_response") and callable(self.AGU.clean_json_response):
                parsed = self.AGU.clean_json_response(raw)
            else:
                parsed = json.loads(raw)
            trip_intent = (parsed.get("trip_intent") or {}) if isinstance(parsed.get("trip_intent"), dict) else {}
            trip_intent = self._normalize_extract_trip_intent(trip_intent, now_date=now_date)
            missing = parsed.get("missing_required_fields")
            if not isinstance(missing, list):
                missing = []
            clarifying = parsed.get("clarifying_questions")
            if not isinstance(clarifying, list):
                clarifying = []
            return {
                "success": True,
                "trip_intent": trip_intent,
                "missing_required_fields": missing,
                "clarifying_questions": clarifying,
            }
        except Exception as e:
            return {
                "success": False,
                "trip_intent": {},
                "missing_required_fields": [
                    "party.travelers.adults",
                    "itinerary.segments",
                    "itinerary.lodging.check_in",
                    "itinerary.lodging.check_out",
                ],
                "clarifying_questions": [f"I had trouble understanding: {str(e)}. Please tell me origin, destination, dates, and number of travelers."],
            }

    def _normalize_extract_trip_intent(self, trip_intent: Dict[str, Any], now_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Coerce LLM-extracted trip_intent: numeric fields to int; clamp date fields to >= now_date.
        Ensures adults, children, infants are int; strips rooms/guests_per_room from lodging; dates are never in the past.
        """
        if not trip_intent:
            return trip_intent
        out = dict(trip_intent)

        travelers = out.get("travelers")
        if isinstance(travelers, dict):
            t = dict(travelers)
            for key in ("adults", "children", "infants"):
                if key in t and t[key] is not None:
                    try:
                        t[key] = int(t[key])
                    except (TypeError, ValueError):
                        t[key] = 1 if key == "adults" else 0
            out["travelers"] = t

        lodging = out.get("lodging")
        if isinstance(lodging, dict):
            lod = dict(lodging)
            for key in ("rooms", "guests_per_room"):
                lod.pop(key, None)
            out["lodging"] = lod

        if now_date:
            def clamp_date(d: Optional[str]) -> Optional[str]:
                if not d or not isinstance(d, str) or len(d) != 10:
                    return d
                return d if d >= now_date else now_date

            dates = out.get("dates")
            if isinstance(dates, dict):
                out["dates"] = dict(dates)
                if "departure_date" in out["dates"] and out["dates"]["departure_date"]:
                    out["dates"]["departure_date"] = clamp_date(out["dates"]["departure_date"])
                if "return_date" in out["dates"] and out["dates"]["return_date"]:
                    out["dates"]["return_date"] = clamp_date(out["dates"]["return_date"])
            lod = out.get("lodging")
            if isinstance(lod, dict):
                out["lodging"] = dict(lod)
                if lod.get("check_in"):
                    out["lodging"]["check_in"] = clamp_date(lod["check_in"])
                if lod.get("check_out"):
                    out["lodging"]["check_out"] = clamp_date(lod["check_out"])
            for seg in out.get("segments") or []:
                if isinstance(seg, dict) and seg.get("depart_date"):
                    seg["depart_date"] = clamp_date(seg["depart_date"])
            for stay in out.get("stays") or []:
                if isinstance(stay, dict):
                    if stay.get("check_in"):
                        stay["check_in"] = clamp_date(stay["check_in"])
                    if stay.get("check_out"):
                        stay["check_out"] = clamp_date(stay["check_out"])

        return out

    def _call_generate_followup_questions(self, arguments: Dict[str, Any]) -> str:
        """
        Ask the LLM to generate 1-3 short, conversational questions for what's still missing.
        Uses full trip_intent and missing paths so the LLM can ask in context.
        """
        trip_intent = arguments.get("trip_intent") or {}
        missing = arguments.get("missing") or []
        user_message = (arguments.get("user_message") or "").strip()

        if not missing:
            return ""

        trip_snapshot = json.dumps(trip_intent, indent=2, default=_json_serializable_default)
        missing_str = ", ".join(missing)

        prompt_text = f"""You are helping the user plan a trip. We have partial trip details and still need a few things to get quotes.

Current trip state (partial):
{trip_snapshot}

Fields still missing for quoting: {missing_str}

The user just said: "{user_message}"

Generate 1-3 short, natural, conversational questions to ask the user to fill in what's missing. Be friendly and concise. Speak directly to the user (e.g. "When would you like to fly?" not "The user should provide..."). Return only the questions as plain text; you can use line breaks or a short paragraph. No JSON, no numbering unless it reads naturally."""

        try:
            prompt = {
                "model": getattr(self.AGU, "AI_2_MODEL", "gpt-4o-mini"),
                "messages": [{"role": "user", "content": prompt_text}],
                "temperature": 0.3,
            }
            response = self.AGU.llm(prompt)
            if not response or not getattr(response, "content", None):
                return "To get your quotes I still need: departure date, and hotel check-in and check-out dates. Can you share those?"
            return (response.content or "").strip()
        except Exception as e:
            return f"I still need a few details to find options — when are you traveling, and what are your hotel check-in and check-out dates?"

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

    MAX_TOOL_RUNS_PER_TURN = 50
    MAX_RUNS_PER_TOOL_NAME = 3

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

        Safeguards: max MAX_TOOL_RUNS_PER_TURN total runs per turn; max MAX_RUNS_PER_TOOL_NAME
        runs per tool name. Exceeding either stops the loop to avoid runaway.
        If stack is provided, appends each applier and reducer result to it.
        portfolio, org come from request context. UI output via self.AGU.save_chat.
        """
        ctx = self._get_context()
        portfolio = ctx.portfolio
        org = ctx.org

        queue: List[ToolCall] = list(tool_queue)
        if stack is None:
            stack = []  # This list will not be passed back to caller.

        run_count = 0
        tool_run_count: Dict[str, int] = {}

        while queue:
            run_count += 1
            if run_count > self.MAX_TOOL_RUNS_PER_TURN:
                status = trip_intent.setdefault("status", {})
                status["phase"] = "error"
                status["state"] = "retryable"
                status.setdefault("notes", []).append(
                    "[runaway] Too many tool runs this turn; stopping to avoid loop. Say 'try again' or send a new message."
                )
                self.trip_store.save(trip_id, trip_intent)
                self.AGU.save_chat({"role": "assistant", "content": "Something went wrong after many steps. Please try again or send a new message."})
                break

            tc = queue.pop(0)
            if tool_run_count.get(tc.name, 0) >= self.MAX_RUNS_PER_TOOL_NAME:
                status = trip_intent.setdefault("status", {})
                status.setdefault("notes", []).append(f"[runaway] Skipping {tc.name}: already run {self.MAX_RUNS_PER_TOOL_NAME} times this turn.")
                self.trip_store.save(trip_id, trip_intent)
                continue

            tool_run_count[tc.name] = tool_run_count.get(tc.name, 0) + 1

            try:
                if tc.name == "trip_requirements_extract":
                    print(f"[IncaRunner] Calling _call_trip_requirements_extract (internal; handler_call not used)")
                    result = self._call_trip_requirements_extract(tc.arguments)
                    print(f"[IncaRunner] trip_requirements_extract result success={result.get('success')}")
                elif tc.name == "generate_followup_questions":
                    msg = self._call_generate_followup_questions(tc.arguments)
                    if msg:
                        self.AGU.save_chat({"role": "assistant", "content": msg})
                    continue
                else:
                    # Tool names are always "x/y" (extension/handler) or "x/y/z" (extension/handler/subhandler).
                    parts = tc.name.split('/')
                    if len(parts) < 2:
                        error_msg = f"❌ {tc.name} is not a valid tool. Use 'extension/handler' or 'extension/handler/subhandler'."
                        self.AGU.print_chat(error_msg, "error")
                        raise ValueError(error_msg)
                    extension = parts[0]
                    handler = '/'.join(parts[1:])
                    result = self.SHC.handler_call(portfolio, org, extension, handler, tc.arguments)
                # Treat handler_call failure (no exception but success=False) as TOOL_ERROR so we don't apply bad result or re-queue.
                if not result.get("success"):
                    err_msg = result.get("output") or result.get("error") or "Handler call failed"
                    raise RuntimeError(err_msg if isinstance(err_msg, str) else str(err_msg))
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
                    m = { "role": "assistant", "content":f'{msg}'}
                    self.AGU.save_chat(m)
                continue

            # Pass handler output (canonical) to applier so it receives { options } / { bundles } etc.
            result_for_applier = result.get("output", result) if isinstance(result.get("output"), dict) else result
            applied = self.applier.run({"trip_intent": trip_intent, "tool_name": tc.name, "result": result_for_applier, "arguments": tc.arguments})
            stack.append(applied)
            out_applied = handler_output(applied)
            trip_intent = out_applied["trip_intent"]
            wm = trip_intent.setdefault("working_memory", {})
            if tc.name == "noma/trip_option_ranker":
                bundles_from_result = result_for_applier.get("bundles", []) if isinstance(result_for_applier, dict) else []
                wm["ranked_bundles"] = bundles_from_result
            status = trip_intent.setdefault("status", {})
            status.setdefault("notes", []).append(
                "[tool_success] " + tc.name + " | input: " + json.dumps(tc.arguments, default=_json_serializable_default)
            )
            self.trip_store.save(trip_id, trip_intent)

            event_data: Dict[str, Any] = {"tool_name": tc.name, "result": result}
            if tc.name == "trip_requirements_extract" and tc.arguments:
                event_data["user_message"] = tc.arguments.get("user_message", "")
            reduced = self.reducer.run({
                "trip_intent": trip_intent,
                "event": {"type": "TOOL_RESULT", "data": event_data},
            })
            stack.append(reduced)
            out_reduced = handler_output(reduced)
            trip_intent = out_reduced["trip_intent"]
            self.trip_store.save(trip_id, trip_intent)

            ui_msgs = out_reduced.get("ui_messages") or []
            wm = trip_intent.get("working_memory") or {}
            ranked_bundles = wm.get("ranked_bundles") or []
            if ui_msgs and ranked_bundles:
                self.AGU.save_chat(ranked_bundles, interface="bundle", msg_type="widget")
                ui_msgs = [msg for msg in ui_msgs if not (isinstance(msg, str) and msg.strip().startswith("Here are the top options"))]
            for msg in ui_msgs:
                m = {"role": "assistant", "content": f"{msg}"}
                self.AGU.save_chat(m)

            if ranked_bundles:
                self.trip_store.save(trip_id, trip_intent)

            followups = [ToolCall(**x) for x in (out_reduced.get("tool_calls") or [])]
            queue.extend(followups)

        return trip_intent

    # -------------------------------------------------------------------------
    # Public entrypoint
    # -------------------------------------------------------------------------

    def run(self, payload: RunnerPayload | Dict[str, Any]) -> RunnerHandlerReturn:
        function = 'run > runner'
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
        
        stack: List[Dict[str, Any]] = []
        
        # Create thread/message document
        print('Creating document for this turn ...')
        created= self.AGU.new_chat_message_document(user_text)
        stack.append(created)  
        if not created['success']:
            return {'success':False,'function':function,'output':created,'stack':stack}
            

        # Load or init TripIntent
        trip_intent = self.trip_store.get(trip_id)
        if not trip_intent:
            trip_intent = self._new_trip_intent(trip_id, user_text)

        # Update request context + timestamps + current time (so dates are always in the future)
        req = trip_intent.setdefault("request", {})
        req["user_message"] = user_text
        try:
            tz = ZoneInfo(req.get("timezone") or "America/New_York")
        except Exception:
            tz = ZoneInfo("America/New_York")
        now_dt = datetime.now(tz)
        req["now_iso"] = now_dt.isoformat()
        req["now_date"] = now_dt.strftime("%Y-%m-%d")
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
        
        reduced = self.reducer.run({"trip_intent": trip_intent, "event": {"type": event.type, "data": event.data}})
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
                {"role": "developer", "content": "TRIP_INTENT_JSON:\n" + json.dumps(trip_intent, default=_json_serializable_default)},
                {"role": "user", "content": user_text},
            ]

            for _ in range(max_model_turns):
                resp = self.openai_client.create_response(input_items=input_items, tools=tools)
                out_text = extract_output_text(resp)
                if out_text:
                    m = { "role": "assistant", "content":f'{out_text}'}
                    self.AGU.save_chat(m)

                model_calls = extract_tool_calls(resp)
                if not model_calls:
                    break

                trip_intent = self._run_tool_queue_and_followups(
                    trip_id=trip_id,
                    trip_intent=trip_intent,
                    tool_queue=model_calls,
                    stack=stack,
                )

                input_items.append({"role": "developer", "content": "TRIP_INTENT_JSON:\n" + json.dumps(trip_intent, default=_json_serializable_default)})

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