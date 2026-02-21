# travel_v1/reducer.py
"""
Reducer: uses JSON tool definitions from tool_registry.json.

Tool definitions are decoupled from the intent document. Decision logic (when to
call what) remains here; tool identity comes from the registry.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from .common.types import Event, Handler, ReduceTripPayload, ReduceTripResult, ReducerHandlerReturn, ToolCall
from .common.reducer_llm import NoOpReducerLLMClient, ReducerLLMClient


def _default_registry_path() -> str:
    return os.path.join(os.path.dirname(__file__), "common", "tool_registry.json")


def _flatten_hotel_rooms(by_stay: List[Any]) -> List[List[Dict[str, Any]]]:
    """Flatten per-stay hotel quotes so each room is one segment (for ranker and selected lookup)."""
    out: List[List[Dict[str, Any]]] = []
    for stay in by_stay or []:
        if not stay:
            continue
        if isinstance(stay[0], dict):
            out.append(list(stay))
        else:
            out.extend(list(room_list) for room_list in stay)
    return out


def _room_occupancies_from_travelers(trip_intent: Dict[str, Any]) -> List[int]:
    """Compute guest count per room from party.travelers (adults + children, max 4 per room)."""
    travelers = (trip_intent.get("party") or {}).get("travelers") or {}
    adults = max(0, int(travelers.get("adults") or 0))
    children = max(0, int(travelers.get("children") or 0))
    total = adults + children
    if total <= 0:
        return [1]
    max_per_room = 4
    if total <= max_per_room:
        return [total]
    occupancies: List[int] = []
    remaining = total
    while remaining > 0:
        take = min(max_per_room, remaining)
        occupancies.append(take)
        remaining -= take
    return occupancies


class Reducer(Handler):
    """
    Reducer that loads tool handler paths from a JSON registry.

    Custom registry path: Reducer(registry_path="/path/to/tool_registry.json")
    """

    name = "reduce_trip"

    def __init__(
        self,
        registry_path: Optional[str] = None,
        llm_client: Optional[ReducerLLMClient] = None,
    ) -> None:
        path = registry_path or _default_registry_path()
        self._tool_registry: Dict[str, str] = {}
        self._load_registry(path)
        self._llm_client: ReducerLLMClient = llm_client or NoOpReducerLLMClient()

    def _load_registry(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tools = data.get("tools", [])
            for t in tools:
                tid = t.get("id")
                hpath = t.get("handler_path")
                if tid and hpath:
                    self._tool_registry[tid] = hpath
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Failed to load tool registry from {path}: {e}") from e

    def _handler_path(self, tool_id: str) -> str:
        """Resolve tool id to handler path. Falls back to tool_id if not in registry."""
        return self._tool_registry.get(tool_id, tool_id)

    def _get_flight_segment_indices(self, trip_intent: Dict[str, Any]) -> List[int]:
        iti = trip_intent.get("itinerary", {}) or {}
        segs = iti.get("segments", []) or []
        return [
            i for i, s in enumerate(segs)
            if (s or {}).get("transport_mode", "flight") == "flight"
        ]

    def _get_effective_stays(self, trip_intent: Dict[str, Any]) -> List[Dict[str, Any]]:
        iti = trip_intent.get("itinerary", {}) or {}
        lodging = iti.get("lodging", {}) or {}
        stays = lodging.get("stays") or []
        if stays:
            return [s or {} for s in stays]
        if not lodging.get("needed", True):
            return []
        segs = iti.get("segments", []) or []
        dest_code = (segs[0].get("destination") or {}).get("code") if segs else None
        return [{
            "location_code": dest_code,
            "check_in": lodging.get("check_in"),
            "check_out": lodging.get("check_out"),
            "location_hint": lodging.get("location_hint"),
        }]

    def _required_fields_missing_for_quotes(self, trip_intent: Dict[str, Any]) -> List[str]:
        missing: List[str] = []
        iti = trip_intent.get("itinerary", {}) or {}
        segs = iti.get("segments", []) or []

        if not segs:
            missing.append("itinerary.segments")
        else:
            for i, seg in enumerate(segs):
                s = seg or {}
                if not (s.get("origin") or {}).get("code"):
                    missing.append(f"itinerary.segments[{i}].origin.code")
                if not (s.get("destination") or {}).get("code"):
                    missing.append(f"itinerary.segments[{i}].destination.code")
                if not s.get("depart_date"):
                    missing.append(f"itinerary.segments[{i}].depart_date")

        adults = (trip_intent.get("party", {}) or {}).get("travelers", {}).get("adults", 0)
        if adults < 1:
            missing.append("party.travelers.adults")

        lodging = iti.get("lodging", {}) or {}
        if lodging.get("needed", True):
            stays = self._get_effective_stays(trip_intent)
            if not stays:
                missing.append("itinerary.lodging.check_in")
                missing.append("itinerary.lodging.check_out")
            else:
                used_single = False
                for j, stay in enumerate(stays):
                    if lodging.get("stays"):
                        loc = stay.get("location_code") or stay.get("destination")
                        if not loc:
                            missing.append(f"itinerary.lodging.stays[{j}].location_code")
                        if not stay.get("check_in"):
                            missing.append(f"itinerary.lodging.stays[{j}].check_in")
                        if not stay.get("check_out"):
                            missing.append(f"itinerary.lodging.stays[{j}].check_out")
                    else:
                        if not used_single:
                            if not lodging.get("check_in"):
                                missing.append("itinerary.lodging.check_in")
                            if not lodging.get("check_out"):
                                missing.append("itinerary.lodging.check_out")
                            used_single = True
                        break

        return missing

    def _summarize_intent_for_tools(self, trip_intent: Dict[str, Any]) -> Dict[str, Any]:
        iti = trip_intent.get("itinerary", {}) or {}
        segs = iti.get("segments", []) or []
        party = trip_intent.get("party", {}) or {}
        stays = self._get_effective_stays(trip_intent)
        segments_summary = [
            {
                "origin": (s.get("origin") or {}).get("code"),
                "destination": (s.get("destination") or {}).get("code"),
                "depart_date": s.get("depart_date"),
                "transport_mode": s.get("transport_mode", "flight"),
            }
            for s in segs
        ]
        stays_summary = [
            {"location_code": st.get("location_code") or st.get("destination"), "check_in": st.get("check_in"), "check_out": st.get("check_out")}
            for st in stays
        ]
        return {
            "origin": (segs[0].get("origin") or {}).get("code") if segs else None,
            "destination": (segs[0].get("destination") or {}).get("code") if segs else None,
            "trip_type": iti.get("trip_type"),
            "segments": segments_summary,
            "stays": stays_summary,
            "dates": {
                "departure_date": segs[0].get("depart_date") if segs else None,
                "return_date": segs[1].get("depart_date") if len(segs) > 1 else None,
            },
            "travelers": (party.get("travelers") or {}),
            "constraints": (trip_intent.get("constraints") or {}),
        }

    def _build_flight_quote_args(self, trip_intent: Dict[str, Any], segment_index: int = 0) -> Dict[str, Any]:
        iti = trip_intent.get("itinerary") or {}
        segs = iti.get("segments") or []
        if segment_index >= len(segs):
            return {}
        seg = segs[segment_index] or {}
        travelers = (trip_intent.get("party") or {}).get("travelers") or {}
        prefs = (trip_intent.get("preferences", {}) or {}).get("flight", {}) or {}
        return {
            "origin": (seg.get("origin") or {}).get("code"),
            "destination": (seg.get("destination") or {}).get("code"),
            "departure_date": seg.get("depart_date"),
            "trip_type": "one_way",
            "travelers": travelers,
            "cabin": prefs.get("cabin", "economy"),
            "constraints": {
                "max_stops": prefs.get("max_stops", 1),
                "avoid_red_eye": prefs.get("avoid_red_eye", False),
                "preferred_airlines": prefs.get("preferred_airlines", []),
            },
            "result_limit": 10,
            "segment_index": segment_index,
        }

    def _build_hotel_quote_args(self, trip_intent: Dict[str, Any], stay_index: int = 0, stay: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        iti = trip_intent.get("itinerary") or {}
        lodging = iti.get("lodging") or {}
        if stay is None:
            stays = self._get_effective_stays(trip_intent)
            stay = stays[stay_index] if stay_index < len(stays) else {}
        hp = (trip_intent.get("preferences", {}) or {}).get("hotel", {}) or {}
        dest = stay.get("location_code") or stay.get("destination") or (lodging.get("location_hint") if not lodging.get("stays") else None)
        return {
            "schema": "renglo.trip_intent.v1",
            "itinerary": iti,
            "party": trip_intent.get("party") or {},
            "stay_index": stay_index,
            "destination": dest,
            "dates": {"start_date": stay.get("check_in"), "end_date": stay.get("check_out")},
            "constraints": {
                "hotel_star_min": hp.get("star_min", 3),
                "refundable_only": hp.get("refundable_only", False),
                "location_hint": stay.get("location_hint") or lodging.get("location_hint"),
            },
            "result_limit": 10,
        }

    def _format_trip_summary(self, trip_intent: Dict[str, Any]) -> str:
        lines: List[str] = ["I have everything I need. Here's your trip summary:"]
        iti = trip_intent.get("itinerary", {}) or {}
        segs = iti.get("segments", []) or []
        party = (trip_intent.get("party", {}) or {}).get("travelers", {}) or {}
        adults = party.get("adults", 0) or 0
        children = party.get("children", 0) or 0
        infants = party.get("infants", 0) or 0
        travelers = []
        if adults:
            travelers.append(f"{adults} adult{'s' if adults != 1 else ''}")
        if children:
            travelers.append(f"{children} child/ren")
        if infants:
            travelers.append(f"{infants} infant{'s' if infants != 1 else ''}")
        if travelers:
            lines.append(f"- **Travelers:** {', '.join(travelers)}")
        if segs:
            lines.append("- **Flights:**")
            for i, s in enumerate(segs):
                orig = (s.get("origin") or {}).get("code") or "?"
                dest = (s.get("destination") or {}).get("code") or "?"
                date = s.get("depart_date") or "?"
                lines.append(f"  - Leg {i + 1}: {orig} → {dest} on {date}")
        lodging = iti.get("lodging", {}) or {}
        if lodging.get("needed", True):
            stays = self._get_effective_stays(trip_intent)
            if stays:
                lines.append("- **Hotel stays:**")
                for j, st in enumerate(stays):
                    loc = st.get("location_code") or st.get("destination") or "?"
                    ci = st.get("check_in") or "?"
                    co = st.get("check_out") or "?"
                    lines.append(f"  - Stay {j + 1}: {loc}, check-in {ci}, check-out {co}")
        return "\n".join(lines)

    def _is_confirmation(self, user_message: str, trip_summary: str = "") -> bool:
        """Use LLM client when available; fallback to programmatic heuristics."""
        return self._llm_client.classify_confirmation(user_message, trip_summary)

    def _infer_clarifying_for_change_request(
        self,
        user_message: str,
        conversation_history: List[Dict[str, Any]],
        trip_summary: str,
    ) -> List[str]:
        """Fallback when extractor returns empty clarifying_questions. LLM interprets message and decides."""
        conv = conversation_history if isinstance(conversation_history, list) else []
        q = self._llm_client.infer_clarifying_question(user_message, conv, trip_summary)
        return [q] if q else []

    def _render_bundles(self, trip_intent: Dict[str, Any]) -> str:
        wm = trip_intent.get("working_memory", {}) or {}
        bundles = (wm.get("ranked_bundles") or [])[:3]
        lines = ["Here are the top options:"]
        for b in bundles:
            et = (b.get("estimated_total") or {})
            lines.append(
                f"- {b.get('bundle_id')}: total {et.get('amount')} {et.get('currency','USD')} — {b.get('why_this_bundle','')}".rstrip()
            )
            for t in (b.get("tradeoffs") or [])[:2]:
                lines.append(f"  - tradeoff: {t}")
        lines.append("Reply with a bundle_id to risk-check it, or tell me what to change.")
        return "\n".join(lines)

    def _ensure_working_memory_defaults(self, wm: Dict[str, Any]) -> None:
        wm.setdefault("flight_quotes", [])
        wm.setdefault("hotel_quotes", [])
        wm.setdefault("flight_quotes_by_segment", [])
        wm.setdefault("hotel_quotes_by_stay", [])
        wm.setdefault("ranked_bundles", [])
        wm.setdefault("risk_report", None)
        wm.setdefault("holds", [])
        wm.setdefault("bookings", [])
        sel = wm.setdefault("selected", {"bundle_id": None, "flight_option_id": None, "hotel_option_id": None, "flight_option_ids": [], "hotel_option_ids": []})
        if isinstance(sel, dict):
            sel.setdefault("flight_option_ids", [])
            sel.setdefault("hotel_option_ids", [])

    def run(self, payload: ReduceTripPayload | Dict[str, Any]) -> ReducerHandlerReturn:
        trip_intent = payload["trip_intent"]
        event = Event(**payload["event"])

        status = trip_intent.setdefault("status", {"phase": "intake", "state": "collecting_requirements", "missing_required": []})
        wm = trip_intent.setdefault("working_memory", {})
        self._ensure_working_memory_defaults(wm)

        tool_calls: List[ToolCall] = []
        ui_messages: List[str] = []

        if event.type == "USER_MESSAGE":
            status["phase"] = "intake"
            if status.get("state") != "awaiting_confirmation":
                status["state"] = "collecting_requirements"
            if "last_tool_error" in status:
                del status["last_tool_error"]
            context = {
                "timezone": (trip_intent.get("request", {}) or {}).get("timezone", "America/New_York"),
                "current_intent": self._summarize_intent_for_tools(trip_intent),
                "conversation_history": payload.get("conversation_history") or [],
            }
            tool_calls.append(ToolCall(
                name=self._handler_path("trip_requirements_extract"),
                arguments={"user_message": event.data["text"], "context": context},
            ))

        elif event.type == "USER_SELECTED_BUNDLE":
            bundle_id = event.data["bundle_id"]
            wm["selected"]["bundle_id"] = bundle_id

            bundle = next((b for b in (wm.get("ranked_bundles") or []) if b.get("bundle_id") == bundle_id), None)
            if bundle:
                wm["selected"]["flight_option_id"] = bundle.get("flight_option_id")
                wm["selected"]["hotel_option_id"] = bundle.get("hotel_option_id")
                wm["selected"]["flight_option_ids"] = bundle.get("flight_option_ids") or []
                wm["selected"]["hotel_option_ids"] = bundle.get("hotel_option_ids") or []

            status["phase"] = "quote"
            status["state"] = "risk_checking"

            sel = wm["selected"]
            flight_ids = sel.get("flight_option_ids") or ([sel.get("flight_option_id")] if sel.get("flight_option_id") else [])
            hotel_ids = sel.get("hotel_option_ids") or ([sel.get("hotel_option_id")] if sel.get("hotel_option_id") else [])
            flight_quotes_all = (wm.get("flight_quotes_by_segment") or []) if wm.get("flight_quotes_by_segment") else (wm.get("flight_quotes") or [])
            hotel_quotes_by_stay_raw = wm.get("hotel_quotes_by_stay") or []
            hotel_quotes_all = (
                _flatten_hotel_rooms(hotel_quotes_by_stay_raw)
                if hotel_quotes_by_stay_raw
                else [wm.get("hotel_quotes") or []]
            )
            if not isinstance(flight_quotes_all[0] if flight_quotes_all else None, list):
                flight_quotes_all = [flight_quotes_all] if flight_quotes_all else []
            selected_flights = []
            for opts in flight_quotes_all:
                for o in opts or []:
                    if o.get("option_id") in flight_ids:
                        selected_flights.append(o)
                        break
            selected_hotels = []
            for opts in hotel_quotes_all:
                for o in opts or []:
                    if o.get("option_id") in hotel_ids:
                        selected_hotels.append(o)
                        break
            if not selected_flights and sel.get("flight_option_id"):
                selected_flights = [next((o for o in (wm.get("flight_quotes") or []) if o.get("option_id") == sel["flight_option_id"]), {})]
            if not selected_hotels and sel.get("hotel_option_id"):
                selected_hotels = [next((o for o in (wm.get("hotel_quotes") or []) if o.get("option_id") == sel["hotel_option_id"]), {})]
            if not selected_flights:
                selected_flights = [{}]
            if not selected_hotels:
                selected_hotels = [{}]
            selected_flight = selected_flights[0] if selected_flights else {}
            selected_hotel = selected_hotels[0] if selected_hotels else {}

            tool_calls.append(ToolCall(
                name=self._handler_path("policy_and_risk_check"),
                arguments={
                    "trip_intent": self._summarize_intent_for_tools(trip_intent),
                    "selected_flight": selected_flight,
                    "selected_hotel": selected_hotel,
                    "selected_flights": selected_flights,
                    "selected_hotels": selected_hotels,
                    "org_policy": (trip_intent.get("policy", {}) or {}).get("rules", {}),
                },
            ))

        elif event.type == "USER_REQUEST_HOLD":
            sel = wm.get("selected", {}) or {}
            rr = wm.get("risk_report") or {}

            if not sel.get("bundle_id"):
                ui_messages.append("Please pick a bundle_id first.")
            elif rr.get("blocking_issues"):
                ui_messages.append("I can't place holds because the selected bundle has blocking policy issues.")
            else:
                items: List[Dict[str, Any]] = []
                traveler_profile_ids = (trip_intent.get("party", {}) or {}).get("traveler_profile_ids", []) or []
                flight_ids = sel.get("flight_option_ids") or ([sel.get("flight_option_id")] if sel.get("flight_option_id") else [])
                hotel_ids = sel.get("hotel_option_ids") or ([sel.get("hotel_option_id")] if sel.get("hotel_option_id") else [])
                for fid in flight_ids:
                    if fid:
                        items.append({"item_type": "flight", "option_id": fid, "traveler_profile_ids": traveler_profile_ids})
                for hid in hotel_ids:
                    if hid:
                        items.append({"item_type": "hotel", "option_id": hid, "traveler_profile_ids": traveler_profile_ids})

                if not items:
                    ui_messages.append("Missing selected flight/hotel option ids. Please select the bundle again.")
                else:
                    status["phase"] = "book"
                    status["state"] = "placing_holds"
                    tool_calls.append(ToolCall(
                        name=self._handler_path("reservation_hold_create"),
                        arguments={"idempotency_key": f"hold_{trip_intent.get('trip_id')}_{sel.get('bundle_id')}", "items": items},
                    ))

        elif event.type == "USER_APPROVED_PURCHASE":
            hold_ids = [h["hold_id"] for h in (wm.get("holds") or []) if h.get("status") == "held"]

            if not hold_ids:
                ui_messages.append("No active holds found. Say 'hold' first, then approve purchase.")
            else:
                status["phase"] = "book"
                status["state"] = "purchasing"
                tool_calls.append(ToolCall(
                    name=self._handler_path("booking_confirm_and_purchase"),
                    arguments={
                        "idempotency_key": f"purchase_{trip_intent.get('trip_id')}",
                        "approval_token": event.data["approval_token"],
                        "hold_ids": hold_ids,
                        "payment_method_id": event.data["payment_method_id"],
                        "contact_email": ((trip_intent.get("party", {}) or {}).get("contact", {}) or {}).get("email"),
                    },
                ))

        elif event.type == "INTENT_READY":
            status["phase"] = "intake"
            status["state"] = "ready_to_quote"
            ui_messages.append("Searching for flights and hotels…")

        elif event.type == "TOOL_ERROR":
            status["phase"] = "error"
            status["state"] = "retryable"
            tool_name = event.data.get("tool_name", "unknown")
            error_text = event.data.get("error", "Unknown error")
            ui_messages.append(f"Tool error: {tool_name} — {error_text}")
            status["last_tool_error"] = {
                "tool_name": tool_name,
                "error": error_text,
                "at": int(time.time()),
            }
            status.setdefault("notes", []).append(
                f"[tool_error] {tool_name} failed: {error_text}. Say 'try again' or send a new message."
            )
            output = {
                "trip_intent": trip_intent,
                "tool_calls": [],
                "ui_messages": ui_messages,
                "debug": {"phase": status.get("phase"), "state": status.get("state"), "last_tool_error": status.get("last_tool_error")},
            }
            return {"success": True, "input": dict(payload), "output": output, "stack": []}

        missing = self._required_fields_missing_for_quotes(trip_intent)
        status["missing_required"] = missing

        if missing:
            status["phase"] = "intake"
            status["state"] = "collecting_requirements"
            if event.type == "USER_MESSAGE":
                tool_calls_to_return = [tc.__dict__ for tc in tool_calls]
            else:
                user_message = (trip_intent.get("request") or {}).get("user_message", "")
                tool_calls_to_return = [
                    ToolCall(
                        name=self._handler_path("generate_followup_questions"),
                        arguments={"trip_intent": trip_intent, "missing": missing, "user_message": user_message},
                    ).__dict__
                ]
            output: ReduceTripResult = {
                "trip_intent": trip_intent,
                "tool_calls": tool_calls_to_return,
                "ui_messages": ui_messages,
                "debug": {"missing_required": missing},
            }
            return {"success": True, "input": dict(payload), "output": output, "stack": []}

        if event.type == "USER_MESSAGE":
            output = {
                "trip_intent": trip_intent,
                "tool_calls": [tc.__dict__ for tc in tool_calls],
                "ui_messages": ui_messages,
                "debug": {"missing_required": missing, "phase": status.get("phase"), "state": status.get("state")},
            }
            return {"success": True, "input": dict(payload), "output": output, "stack": []}

        tool_name = (event.data or {}).get("tool_name", "") if event.type == "TOOL_RESULT" else ""
        if event.type == "TOOL_RESULT" and (tool_name == "trip_requirements_extract" or tool_name.endswith("/trip_requirements_extract")):
            current_state = status.get("state", "")
            user_message = (event.data or {}).get("user_message") or (trip_intent.get("request") or {}).get("user_message", "")

            if current_state != "awaiting_confirmation":
                status["phase"] = "intake"
                status["state"] = "awaiting_confirmation"
                summary = self._format_trip_summary(trip_intent)
                ui_messages.append(
                    summary + "\n\nIf this looks correct, reply **Yes** or **Looks good** to search for flights and hotels. "
                    "If something needs to change, tell us what to update."
                )
                output = {
                    "trip_intent": trip_intent,
                    "tool_calls": [],
                    "ui_messages": ui_messages,
                    "debug": {"missing_required": missing, "phase": status.get("phase"), "state": status.get("state")},
                }
                return {"success": True, "input": dict(payload), "output": output, "stack": []}

            summary = self._format_trip_summary(trip_intent)
            if not self._is_confirmation(user_message, summary):
                # User requested a change; surface extractor's clarifying_questions or missing_required
                result = (event.data or {}).get("result") or {}
                clarifying = result.get("clarifying_questions") or []
                extractor_missing = result.get("missing_required_fields") or []
                # Fallback: extractor may return empty clarifying_questions; LLM interprets and decides
                if not clarifying and user_message:
                    conv = (event.data or {}).get("conversation_history") or payload.get("conversation_history") or []
                    clarifying = self._infer_clarifying_for_change_request(user_message, conv, summary)
                if clarifying:
                    for q in clarifying:
                        ui_messages.append(q)
                    output = {
                        "trip_intent": trip_intent,
                        "tool_calls": [],
                        "ui_messages": ui_messages,
                        "debug": {"missing_required": missing, "phase": status.get("phase"), "state": status.get("state")},
                    }
                    return {"success": True, "input": dict(payload), "output": output, "stack": []}
                elif extractor_missing:
                    # Extractor identified missing fields; ask for them
                    tool_calls_to_return = [
                        ToolCall(
                            name=self._handler_path("generate_followup_questions"),
                            arguments={
                                "trip_intent": trip_intent,
                                "missing": extractor_missing,
                                "user_message": user_message,
                            },
                        ).__dict__
                    ]
                    output = {
                        "trip_intent": trip_intent,
                        "tool_calls": tool_calls_to_return,
                        "ui_messages": ui_messages,
                        "debug": {"missing_required": extractor_missing, "phase": status.get("phase"), "state": status.get("state")},
                    }
                    return {"success": True, "input": dict(payload), "output": output, "stack": []}
                ui_messages.append(
                    summary + "\n\nReply **Yes** or **Looks good** when you're ready to search, or tell us what to change."
                )
                output = {
                    "trip_intent": trip_intent,
                    "tool_calls": [],
                    "ui_messages": ui_messages,
                    "debug": {"missing_required": missing, "phase": status.get("phase"), "state": status.get("state")},
                }
                return {"success": True, "input": dict(payload), "output": output, "stack": []}

            status["state"] = "ready_to_quote"
            ui_messages.append("Searching for flights and hotels…")

        lodging_needed = (trip_intent.get("itinerary", {}) or {}).get("lodging", {}).get("needed", True)
        flight_segment_indices = self._get_flight_segment_indices(trip_intent)
        effective_stays = self._get_effective_stays(trip_intent)
        flight_quotes_by_seg = wm.get("flight_quotes_by_segment") or []
        hotel_quotes_by_stay = wm.get("hotel_quotes_by_stay") or []
        flight_quotes_flat = wm.get("flight_quotes") or []
        hotel_quotes_flat = wm.get("hotel_quotes") or []

        use_multi = len(flight_segment_indices) > 1 or len(effective_stays) > 1 or bool(flight_quotes_by_seg or hotel_quotes_by_stay)

        for seg_idx in flight_segment_indices:
            seg_quotes = (flight_quotes_by_seg[seg_idx] if seg_idx < len(flight_quotes_by_seg) else None) if flight_quotes_by_seg else (flight_quotes_flat if seg_idx == 0 else None)
            if not seg_quotes:
                status["phase"] = "quote"
                status["state"] = "quoting_flights"
                args = self._build_flight_quote_args(trip_intent, segment_index=seg_idx)
                if args:
                    tool_calls.append(ToolCall(name=self._handler_path("flight_quote_search"), arguments=args))
                break

        if lodging_needed and effective_stays and not tool_calls:
            for j, stay in enumerate(effective_stays):
                stay_quotes = (hotel_quotes_by_stay[j] if j < len(hotel_quotes_by_stay) else None) if hotel_quotes_by_stay else (hotel_quotes_flat if j == 0 else None)
                if not stay_quotes:
                    status["phase"] = "quote"
                    status["state"] = "quoting_hotels"
                    tool_calls.append(ToolCall(name=self._handler_path("hotel_quote_search"), arguments=self._build_hotel_quote_args(trip_intent, stay_index=j, stay=stay)))
                    break

        has_all_flight_quotes = (
            (flight_quotes_by_seg and len(flight_quotes_by_seg) >= len(flight_segment_indices) and all(flight_quotes_by_seg[i] for i in range(min(len(flight_segment_indices), len(flight_quotes_by_seg)))))
            or (flight_quotes_flat and (not flight_segment_indices or len(flight_segment_indices) == 1))
        )
        if not flight_segment_indices:
            has_all_flight_quotes = True
        has_all_hotel_quotes = (
            not lodging_needed
            or (hotel_quotes_by_stay and len(hotel_quotes_by_stay) >= len(effective_stays) and all(hotel_quotes_by_stay[j] for j in range(len(effective_stays))))
            or (hotel_quotes_flat and (not effective_stays or len(effective_stays) == 1))
        )
        if has_all_flight_quotes and has_all_hotel_quotes and not (wm.get("ranked_bundles") or []):
            status["phase"] = "quote"
            status["state"] = "ranking_bundles"
            ranker_args: Dict[str, Any] = {
                "trip_intent": self._summarize_intent_for_tools(trip_intent),
                "ranking_policy": {"weights": {"price": 0.5, "duration": 0.2, "refundable": 0.2, "convenience": 0.1}},
            }
            if use_multi and (flight_quotes_by_seg or hotel_quotes_by_stay):
                ranker_args["flight_options_by_segment"] = flight_quotes_by_seg or [flight_quotes_flat]
                ranker_args["hotel_options_by_stay"] = (
                    _flatten_hotel_rooms(hotel_quotes_by_stay) or [hotel_quotes_flat]
                )
                room_counts = []
                for stay in (hotel_quotes_by_stay or []):
                    if not stay:
                        room_counts.append(0)
                    elif isinstance(stay[0], dict):
                        room_counts.append(1)
                    else:
                        room_counts.append(len(stay))
                if room_counts:
                    ranker_args["room_counts_per_stay"] = room_counts
                    occupancies = _room_occupancies_from_travelers(trip_intent)
                    if occupancies and sum(occupancies) > 0:
                        ranker_args["room_occupancies"] = occupancies
            else:
                ranker_args["flight_options"] = wm.get("flight_quotes") or []
                ranker_args["hotel_options"] = wm.get("hotel_quotes") or []
            tool_calls.append(ToolCall(name=self._handler_path("trip_option_ranker"), arguments=ranker_args))

        if (wm.get("ranked_bundles") or []) and status.get("state") in ("presenting_options", "ranking_bundles", "have_flight_quotes", "have_hotel_quotes"):
            status["state"] = "presenting_options"
            ui_messages.append(self._render_bundles(trip_intent))

        rr = wm.get("risk_report")
        if rr and rr.get("blocking_issues"):
            ui_messages.append("Selected bundle has blocking issues:")
            for bi in rr["blocking_issues"]:
                ui_messages.append(f"- {bi}")
        elif rr and not rr.get("blocking_issues"):
            if rr.get("risks"):
                ui_messages.append("Risks to note:")
                for r in rr["risks"]:
                    ui_messages.append(f"- {r}")
            ui_messages.append("Say 'hold' to place holds, or pick a different bundle_id.")

        output = {
            "trip_intent": trip_intent,
            "tool_calls": [tc.__dict__ for tc in tool_calls],
            "ui_messages": ui_messages,
            "debug": {"missing_required": missing, "phase": status.get("phase"), "state": status.get("state")},
        }
        return {"success": True, "input": dict(payload), "output": output, "stack": []}

    @classmethod
    def run_tests(cls) -> bool:
        """Run a minimal battery of tests for this handler. Returns True on success, raises on failure."""
        handler = cls()
        trip_intent = {"working_memory": {}, "status": {}, "itinerary": {}, "party": {}}
        payload = {
            "trip_intent": trip_intent,
            "event": {"type": "USER_MESSAGE", "data": {"text": "fly EWR to DEN"}},
        }
        out = handler.run(payload)
        assert out.get("success") is True
        assert "input" in out and "output" in out and "stack" in out
        o = out["output"]
        assert "trip_intent" in o and "tool_calls" in o
        assert len(o["tool_calls"]) >= 1
        assert any("trip_requirements_extract" in (tc.get("name", "") or "") for tc in o["tool_calls"])

        intent_ready_intent = {
            "working_memory": {},
            "status": {},
            "itinerary": {
                "segments": [{"origin": {"code": "EWR"}, "destination": {"code": "DEN"}, "depart_date": "2026-06-01", "transport_mode": "flight"}],
                "lodging": {"needed": True, "check_in": "2026-06-01", "check_out": "2026-06-05"},
            },
            "party": {"travelers": {"adults": 2}},
        }
        intent_ready_out = handler.run({
            "trip_intent": intent_ready_intent,
            "event": {"type": "INTENT_READY", "data": {}},
        })
        assert intent_ready_out.get("success") is True
        o2 = intent_ready_out["output"]
        assert "tool_calls" in o2
        assert any("flight_quote_search" in (tc.get("name", "") or "") or "hotel_quote_search" in (tc.get("name", "") or "") for tc in o2["tool_calls"])
        return True
