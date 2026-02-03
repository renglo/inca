# travel_v1/reducer.py
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .common.types import Event, Handler, ReduceTripPayload, ReduceTripResult, ReducerHandlerReturn, ToolCall


class Reducer(Handler):
    """
    Reducer v1.

    Responsibilities:
      - Compute missing required fields for quoting
      - Decide next tool calls deterministically (quote -> rank)
      - Handle bundle selection -> risk check
      - Handle hold request -> create holds (requires selected bundle + non-blocking risk)
      - Handle purchase approval -> purchase (requires holds)

    Input payload:
      {
        "trip_intent": dict,
        "event": {"type": ..., "data": {...}}
      }

    Output:
      {
        "trip_intent": dict,
        "tool_calls": [ {name, arguments, call_id?}, ... ],
        "ui_messages": [str, ...],
        "debug": dict
      }
    """

    name = "reduce_trip"

    # -------------------------------------------------------------------------
    # Helpers: effective segments and stays (multi-city / multi-modal)
    # -------------------------------------------------------------------------

    def _get_flight_segment_indices(self, trip_intent: Dict[str, Any]) -> List[int]:
        """Indices of segments that are flight (transport_mode is None or 'flight')."""
        iti = trip_intent.get("itinerary", {}) or {}
        segs = iti.get("segments", []) or []
        return [
            i for i, s in enumerate(segs)
            if (s or {}).get("transport_mode", "flight") == "flight"
        ]

    def _get_effective_stays(self, trip_intent: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        List of stay dicts for lodging. Uses lodging.stays if non-empty;
        otherwise one stay from top-level check_in/check_out/location (backward compat).
        """
        iti = trip_intent.get("itinerary", {}) or {}
        lodging = iti.get("lodging", {}) or {}
        stays = lodging.get("stays") or []
        if stays:
            return [s or {} for s in stays]
        if not lodging.get("needed", True):
            return []
        # Single-destination fallback: one stay, location = first segment's destination.
        # Multi-city must set lodging.stays (one entry per city); see Example 9 in EXAMPLES.md.
        segs = iti.get("segments", []) or []
        dest_code = (segs[0].get("destination") or {}).get("code") if segs else None
        return [{
            "location_code": dest_code,
            "check_in": lodging.get("check_in"),
            "check_out": lodging.get("check_out"),
            "location_hint": lodging.get("location_hint"),
        }]

    @staticmethod
    def _flatten_hotel_rooms(by_stay: List[Any]) -> List[List[Dict[str, Any]]]:
        """Flatten per-stay hotel quotes so each room is one segment (for ranker and selected lookup)."""
        out: List[List[Dict[str, Any]]] = []
        for stay in by_stay or []:
            if not stay:
                continue
            # stay is list of options (single room) or list of option lists (multi-room)
            if isinstance(stay[0], dict):
                out.append(list(stay))
            else:
                out.extend(list(room_list) for room_list in stay)
        return out

    @staticmethod
    def _room_occupancies_from_travelers(trip_intent: Dict[str, Any]) -> List[int]:
        """Compute guest count per room from party.travelers (adults + children, max 4 per room). Same logic as hotel_quote_search."""
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

    # -------------------------------------------------------------------------
    # Required fields logic
    # -------------------------------------------------------------------------

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

        lodging = (iti.get("lodging", {}) or {})
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

    def _build_questions(self, missing_paths: List[str]) -> str:
        # ask up to 3
        priorities = [
            "itinerary.segments[0].origin.code",
            "itinerary.segments[0].destination.code",
            "itinerary.segments[0].depart_date",
            "itinerary.segments[1].depart_date",
            "party.travelers.adults",
            "itinerary.lodging.check_in",
            "itinerary.lodging.check_out",
        ]
        for i in range(2, 10):
            priorities.extend([
                f"itinerary.segments[{i}].origin.code",
                f"itinerary.segments[{i}].destination.code",
                f"itinerary.segments[{i}].depart_date",
            ])
        for j in range(5):
            priorities.extend([
                f"itinerary.lodging.stays[{j}].location_code",
                f"itinerary.lodging.stays[{j}].check_in",
                f"itinerary.lodging.stays[{j}].check_out",
            ])

        ordered = sorted(missing_paths, key=lambda p: priorities.index(p) if p in priorities else 999)
        top = ordered[:3]

        qs: List[str] = []
        for p in top:
            if ".origin.code" in p:
                qs.append("What airport/city are you departing from for this leg?")
            elif ".destination.code" in p:
                qs.append("What airport/city are you going to for this leg?")
            elif "segments[" in p and "depart_date" in p:
                qs.append("What's the departure date for this leg?")
            elif p.endswith("party.travelers.adults"):
                qs.append("How many adult travelers?")
            elif "lodging.check_in" in p or "lodging.check_out" in p or ("stays[" in p and "check_" in p):
                qs.append("What are the hotel check-in and check-out dates?")
            elif "stays[" in p and "location_code" in p:
                qs.append("Which city/location for this hotel stay?")
            elif p == "itinerary.segments":
                qs.append("What airport/city are you departing from and going to, and what's the departure date?")
            else:
                qs.append(f"I’m missing: {p}. What should it be?")

        return "\n".join(f"- {q}" for q in qs)

    def _format_trip_summary(self, trip_intent: Dict[str, Any]) -> str:
        """Build a short, readable summary of the trip for the user to confirm before quoting."""
        lines: List[str] = ["I have everything I need. Here’s your trip summary:"]
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

    @staticmethod
    def _is_confirmation(text: str) -> bool:
        """True if the user message looks like a confirmation to proceed with quoting."""
        t = (text or "").strip().lower()
        if not t:
            return False
        confirmations = (
            "yes", "y", "ok", "okay", "looks good", "look good", "go ahead", "correct",
            "that's right", "thats right", "confirm", "proceed", "search", "find flights",
            "find hotels", "get quotes", "sounds good", "perfect", "good", "continue",
        )
        if t in confirmations:
            return True
        if len(t) <= 4 and t in ("yes", "y", "ok"):
            return True
        if any(t.startswith(c) for c in ("yes ", "yes,", "ok ", "ok,", "sure ", "go ahead")):
            return True
        # Short replies that clearly contain confirmation intent (e.g. "yes, go ahead")
        if len(t) < 50 and ("go ahead" in t or "looks good" in t or "sounds good" in t or "let's go" in t):
            return True
        return False

    # -------------------------------------------------------------------------
    # Tool input builders
    # -------------------------------------------------------------------------

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
        iti = trip_intent["itinerary"]
        segs = iti["segments"]
        if segment_index >= len(segs):
            return {}
        seg = segs[segment_index] or {}
        travelers = trip_intent["party"]["travelers"]
        prefs = (trip_intent.get("preferences", {}) or {}).get("flight", {}) or {}
        args: Dict[str, Any] = {
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
        return args

    def _build_hotel_quote_args(self, trip_intent: Dict[str, Any], stay_index: int = 0, stay: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        iti = trip_intent.get("itinerary") or {}
        lodging = iti.get("lodging") or {}
        if stay is None:
            stays = self._get_effective_stays(trip_intent)
            stay = stays[stay_index] if stay_index < len(stays) else {}
        hp = (trip_intent.get("preferences", {}) or {}).get("hotel", {}) or {}
        dest = stay.get("location_code") or stay.get("destination") or (lodging.get("location_hint") if not lodging.get("stays") else None)
        args: Dict[str, Any] = {
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
        return args

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

    # -------------------------------------------------------------------------
    # Main reducer
    # -------------------------------------------------------------------------

    def run(self, payload: ReduceTripPayload | Dict[str, Any]) -> ReducerHandlerReturn:
        trip_intent = payload["trip_intent"]
        event = Event(**payload["event"])

        status = trip_intent.setdefault("status", {"phase": "intake", "state": "collecting_requirements", "missing_required": []})
        wm = trip_intent.setdefault("working_memory", {})
        self._ensure_working_memory_defaults(wm)

        tool_calls: List[ToolCall] = []
        ui_messages: List[str] = []

        # -------------------------
        # 1) event-driven actions
        # -------------------------

        if event.type == "USER_MESSAGE":
            status["phase"] = "intake"
            # Keep awaiting_confirmation so that after extractor runs, we can treat the message as confirmation
            if status.get("state") != "awaiting_confirmation":
                status["state"] = "collecting_requirements"
            # New turn: clear any previous tool error so status reflects current turn
            if "last_tool_error" in status:
                del status["last_tool_error"]
            # Pass current trip intent summary so the extractor can merge incrementally
            # (e.g. "We are 4 adults" / "check-in Feb 12" update existing intent)
            context = {
                "timezone": (trip_intent.get("request", {}) or {}).get("timezone", "America/New_York"),
                "current_intent": self._summarize_intent_for_tools(trip_intent),
            }
            tool_calls.append(ToolCall(
                name="trip_requirements_extract",
                arguments={
                    "user_message": event.data["text"],
                    "context": context,
                },
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
                Reducer._flatten_hotel_rooms(hotel_quotes_by_stay_raw)
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
                name="noma/policy_and_risk_check",
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
                ui_messages.append("I can’t place holds because the selected bundle has blocking policy issues.")
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
                        name="noma/reservation_hold_create",
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
                    name="noma/booking_confirm_and_purchase",
                    arguments={
                        "idempotency_key": f"purchase_{trip_intent.get('trip_id')}",
                        "approval_token": event.data["approval_token"],
                        "hold_ids": hold_ids,
                        "payment_method_id": event.data["payment_method_id"],
                        "contact_email": ((trip_intent.get("party", {}) or {}).get("contact", {}) or {}).get("email"),
                    },
                ))

        elif event.type == "TOOL_ERROR":
            status["phase"] = "error"
            status["state"] = "retryable"
            tool_name = event.data.get("tool_name", "unknown")
            error_text = event.data.get("error", "Unknown error")
            ui_messages.append(f"Tool error: {tool_name} — {error_text}")
            # Persist error in status so UI/next turn can show what failed (e.g. "last run: trip_requirements_extract failed").
            status["last_tool_error"] = {
                "tool_name": tool_name,
                "error": error_text,
                "at": int(time.time()),
            }
            status.setdefault("notes", []).append(
                f"[tool_error] {tool_name} failed: {error_text}. Say 'try again' or send a new message to re-run."
            )
            # Return immediately with no follow-up tool_calls so the failed tool is not re-queued (avoids infinite loop).
            output = {
                "trip_intent": trip_intent,
                "tool_calls": [],
                "ui_messages": ui_messages,
                "debug": {"phase": status.get("phase"), "state": status.get("state"), "last_tool_error": status.get("last_tool_error")},
            }
            return {"success": True, "input": dict(payload), "output": output, "stack": []}

        # TOOL_RESULT: the applier already mutated TripIntent, reducer will respond below

        # -------------------------
        # 2) state-driven followups
        # -------------------------

        missing = self._required_fields_missing_for_quotes(trip_intent)
        status["missing_required"] = missing

        if missing:
            status["phase"] = "intake"
            status["state"] = "collecting_requirements"
            # Let the LLM ask what's missing instead of canned questions
            if event.type == "USER_MESSAGE":
                tool_calls_to_return = [tc.__dict__ for tc in tool_calls]  # run extractor first
            else:
                # After extractor ran: ask LLM to generate conversational follow-up questions
                user_message = (trip_intent.get("request") or {}).get("user_message", "")
                tool_calls_to_return = [
                    ToolCall(
                        name="generate_followup_questions",
                        arguments={
                            "trip_intent": trip_intent,
                            "missing": missing,
                            "user_message": user_message,
                        },
                    ).__dict__
                ]
            output: ReduceTripResult = {
                "trip_intent": trip_intent,
                "tool_calls": tool_calls_to_return,
                "ui_messages": ui_messages,
                "debug": {"missing_required": missing},
            }
            return {"success": True, "input": dict(payload), "output": output, "stack": []}

        # For USER_MESSAGE, return only memorialization (trip_requirements_extract). Run and persist
        # that first. Follow-up tools (flight_quote_search, etc.) are emitted in the same user turn
        # when the runner re-invokes the reducer with TOOL_RESULT after extraction is applied, so
        # user input is never lost if a later tool fails.
        if event.type == "USER_MESSAGE":
            output = {
                "trip_intent": trip_intent,
                "tool_calls": [tc.__dict__ for tc in tool_calls],
                "ui_messages": ui_messages,
                "debug": {"missing_required": missing, "phase": status.get("phase"), "state": status.get("state")},
            }
            return {"success": True, "input": dict(payload), "output": output, "stack": []}

        # Requirements satisfied (TOOL_RESULT from trip_requirements_extract, missing empty): show summary
        # and require user confirmation before calling flight/hotel APIs.
        tool_name = (event.data or {}).get("tool_name", "") if event.type == "TOOL_RESULT" else ""
        if event.type == "TOOL_RESULT" and tool_name == "trip_requirements_extract":
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

            if not self._is_confirmation(user_message):
                summary = self._format_trip_summary(trip_intent)
                ui_messages.append(
                    summary + "\n\nReply **Yes** or **Looks good** when you’re ready to search, or tell us what to change."
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
                    tool_calls.append(ToolCall(name="noma/flight_quote_search", arguments=args))
                break

        if lodging_needed and effective_stays and not tool_calls:
            for j, stay in enumerate(effective_stays):
                stay_quotes = (hotel_quotes_by_stay[j] if j < len(hotel_quotes_by_stay) else None) if hotel_quotes_by_stay else (hotel_quotes_flat if j == 0 else None)
                if not stay_quotes:
                    status["phase"] = "quote"
                    status["state"] = "quoting_hotels"
                    tool_calls.append(ToolCall(name="noma/hotel_quote_search", arguments=self._build_hotel_quote_args(trip_intent, stay_index=j, stay=stay)))
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
                    Reducer._flatten_hotel_rooms(hotel_quotes_by_stay) or [hotel_quotes_flat]
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
                    occupancies = Reducer._room_occupancies_from_travelers(trip_intent)
                    if occupancies and sum(occupancies) > 0:
                        ranker_args["room_occupancies"] = occupancies
            else:
                ranker_args["flight_options"] = wm.get("flight_quotes") or []
                ranker_args["hotel_options"] = wm.get("hotel_quotes") or []
            tool_calls.append(ToolCall(name="noma/trip_option_ranker", arguments=ranker_args))

        # Present bundles
        if (wm.get("ranked_bundles") or []) and status.get("state") in ("presenting_options", "ranking_bundles", "have_flight_quotes", "have_hotel_quotes"):
            status["state"] = "presenting_options"
            ui_messages.append(self._render_bundles(trip_intent))

        # Risk report messaging
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

        output: ReduceTripResult = {
            "trip_intent": trip_intent,
            "tool_calls": [tc.__dict__ for tc in tool_calls],
            "ui_messages": ui_messages,
            "debug": {"missing_required": missing, "phase": status.get("phase"), "state": status.get("state")},
        }
        return {"success": True, "input": dict(payload), "output": output, "stack": []}

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

    @classmethod
    def run_tests(cls) -> bool:
        """Run a minimal battery of tests for this handler. Returns True on success, raises on failure."""
        handler = cls()
        trip_intent = {"working_memory": {}, "status": {}, "itinerary": {"segments": [], "lodging": {"needed": True}}, "party": {"travelers": {"adults": 0}}}
        payload = {"trip_intent": trip_intent, "event": {"type": "USER_MESSAGE", "data": {"text": "I want to fly EWR to DEN"}}}
        out = handler.run(payload)
        assert out.get("success") is True
        assert "output" in out and "stack" in out
        o = out["output"]
        assert "trip_intent" in o and "tool_calls" in o and "ui_messages" in o and "debug" in o
        # USER_MESSAGE with missing required: return only trip_requirements_extract so memorialization runs first
        assert len(o["tool_calls"]) == 1 and o["tool_calls"][0].get("name") == "trip_requirements_extract"
        assert out["stack"] == []
        return True