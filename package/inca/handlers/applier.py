# travel_v1/applier.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .common.types import ApplyToolResultPayload, ApplyToolResultResult, ApplierHandlerReturn, Handler, handler_output
from .patcher import Patcher


class Applier(Handler):
    """
    Deterministic tool result applier (v1).

    Responsibilities:
      - Apply tool outputs into canonical TripIntent locations
      - For requirement extraction: convert extractor output -> patch -> call patcher
      - Update status.phase/state appropriately

    Input payload:
      {
        "trip_intent": dict,
        "tool_name": str,
        "result": dict
      }

    Output:
      {
        "trip_intent": dict,
        "debug": dict
      }
    """
    name = "apply_tool_result"

    def __init__(self, *, patcher: Patcher) -> None:
        self.patcher = patcher

    def run(self, payload: ApplyToolResultPayload | Dict[str, Any]) -> ApplierHandlerReturn:
        trip_intent = payload["trip_intent"]
        tool_name = payload["tool_name"]
        result = payload["result"]

        status = trip_intent.setdefault("status", {})
        wm = trip_intent.setdefault("working_memory", {})
        self._ensure_working_memory_defaults(wm)

        if tool_name == "trip_requirements_extract":
            return self._apply_requirements_extract(payload, trip_intent, result)

        output_base: ApplyToolResultResult = {"trip_intent": trip_intent, "debug": {}}

        if tool_name == "flight_quote_search":
            args = payload.get("arguments") or {}
            seg_idx = args.get("segment_index")
            if seg_idx is not None:
                by_seg = wm.setdefault("flight_quotes_by_segment", [])
                while len(by_seg) <= seg_idx:
                    by_seg.append(None)
                by_seg[seg_idx] = result.get("options", [])
                if len(by_seg) == 1 and by_seg[0]:
                    wm["flight_quotes"] = by_seg[0]
            else:
                wm["flight_quotes"] = result.get("options", [])
            status["phase"] = "quote"
            status["state"] = "have_flight_quotes"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        if tool_name == "hotel_quote_search":
            stay_idx = (payload.get("arguments") or {}).get("stay_index")
            if stay_idx is not None:
                by_stay = wm.setdefault("hotel_quotes_by_stay", [])
                while len(by_stay) <= stay_idx:
                    by_stay.append(None)
                by_stay[stay_idx] = result.get("options", [])
                if len(by_stay) == 1 and by_stay[0]:
                    wm["hotel_quotes"] = by_stay[0]
            else:
                wm["hotel_quotes"] = result.get("options", [])
            status["phase"] = "quote"
            status["state"] = "have_hotel_quotes"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        if tool_name == "trip_option_ranker":
            wm["ranked_bundles"] = result.get("bundles", [])
            status["phase"] = "quote"
            status["state"] = "presenting_options"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        if tool_name == "policy_and_risk_check":
            sel = wm.get("selected", {}) or {}
            wm["risk_report"] = {"bundle_id": sel.get("bundle_id"), **result}
            status["phase"] = "quote"
            status["state"] = "risk_checked"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        if tool_name == "reservation_hold_create":
            wm["holds"] = result.get("holds", [])
            status["phase"] = "book"
            status["state"] = "awaiting_purchase_approval"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        if tool_name == "booking_confirm_and_purchase":
            conf = result.get("confirmation")
            if conf is not None:
                wm.setdefault("bookings", []).append(conf)
            status["phase"] = "completed"
            status["state"] = "completed"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        # Unknown tool: record note
        status.setdefault("notes", []).append(f"[unknown_tool_result] {tool_name}")
        return {"success": True, "input": dict(payload), "output": {"trip_intent": trip_intent, "debug": {"unknown_tool": tool_name}}, "stack": []}

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

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

    def _apply_requirements_extract(self, payload: Dict[str, Any], trip_intent: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Expected extractor output (best-effort):
          {
            "trip_intent": {
              "trip_type": "...",
              "origin": "EWR",
              "destination": "DEN",
              "dates": {"departure_date":"YYYY-MM-DD","return_date":"YYYY-MM-DD"},
              "travelers": {"adults":2,"children":0,"infants":0},
              "cabin": "economy",
              "constraints": {...},
              "lodging": {...}
            },
            "missing_required_fields": [...],
            "clarifying_questions": [...]
          }
        """
        extracted = result.get("trip_intent", {}) or {}
        missing = result.get("missing_required_fields", []) or []
        clarifying = result.get("clarifying_questions", []) or []

        patch: Dict[str, Any] = {}

        # --- status missing_required ---
        patch.setdefault("status", {})["missing_required"] = missing

        # --- party travelers ---
        travelers = extracted.get("travelers")
        if isinstance(travelers, dict):
            patch.setdefault("party", {}).setdefault("travelers", {})
            patch["party"]["travelers"] = travelers

        # --- itinerary trip type ---
        if extracted.get("trip_type"):
            patch.setdefault("itinerary", {})["trip_type"] = extracted["trip_type"]

        # --- itinerary segments: multi-city from extracted.segments or single from origin/destination/dates ---
        origin = extracted.get("origin")
        dest = extracted.get("destination")
        dates = extracted.get("dates", {}) or {}
        extracted_segs = extracted.get("segments") or []

        segs: List[Dict[str, Any]] = []
        if extracted_segs:
            for i, es in enumerate(extracted_segs):
                o = (es.get("origin") or es.get("origin_code"))
                d = (es.get("destination") or es.get("destination_code"))
                o_code = o if isinstance(o, str) else (o.get("code") if isinstance(o, dict) else None)
                d_code = d if isinstance(d, str) else (d.get("code") if isinstance(d, dict) else None)
                if o_code and d_code and es.get("depart_date"):
                    segs.append({
                        "segment_id": es.get("segment_id") or f"seg_{i}",
                        "origin": {"type": "airport", "code": o_code} if isinstance(o_code, str) else (o or {"type": "airport", "code": o_code}),
                        "destination": {"type": "airport", "code": d_code} if isinstance(d_code, str) else (d or {"type": "airport", "code": d_code}),
                        "depart_date": es["depart_date"],
                        "transport_mode": es.get("transport_mode", "flight"),
                        "depart_time_window": es.get("depart_time_window") or {"start": None, "end": None},
                    })
        elif origin and dest and dates.get("departure_date"):
            segs.append({
                "segment_id": "seg_outbound",
                "origin": {"type": "airport", "code": origin},
                "destination": {"type": "airport", "code": dest},
                "depart_date": dates["departure_date"],
                "transport_mode": "flight",
                "depart_time_window": {"start": None, "end": None},
            })
            if extracted.get("trip_type") == "round_trip" and dates.get("return_date"):
                segs.append({
                    "segment_id": "seg_return",
                    "origin": {"type": "airport", "code": dest},
                    "destination": {"type": "airport", "code": origin},
                    "depart_date": dates["return_date"],
                    "transport_mode": "flight",
                    "depart_time_window": {"start": None, "end": None},
                })

        if segs:
            patch.setdefault("itinerary", {})["segments"] = segs

        # --- lodging: multi-city stays or single block ---
        lod = extracted.get("lodging", {}) or {}
        patch.setdefault("itinerary", {}).setdefault("lodging", {})
        if "needed" in lod:
            patch["itinerary"]["lodging"]["needed"] = lod["needed"]
        extracted_stays = extracted.get("stays") or lod.get("stays") or []
        if extracted_stays:
            patch["itinerary"]["lodging"]["stays"] = [
                {
                    "location_code": s.get("location_code") or s.get("destination"),
                    "check_in": s.get("check_in"),
                    "check_out": s.get("check_out"),
                    "rooms": s.get("rooms", 1),
                    "guests_per_room": s.get("guests_per_room", 2),
                    "location_hint": s.get("location_hint"),
                }
                for s in extracted_stays
            ]
        else:
            if dates.get("departure_date"):
                patch["itinerary"]["lodging"]["check_in"] = dates["departure_date"]
            if dates.get("return_date"):
                patch["itinerary"]["lodging"]["check_out"] = dates["return_date"]
        if "rooms" in lod:
            patch["itinerary"]["lodging"]["rooms"] = lod["rooms"]
        if "guests_per_room" in lod:
            patch["itinerary"]["lodging"]["guests_per_room"] = lod["guests_per_room"]
        if "location_hint" in lod:
            patch["itinerary"]["lodging"]["location_hint"] = lod["location_hint"]

        # --- preferences ---
        if extracted.get("cabin"):
            patch.setdefault("preferences", {}).setdefault("flight", {})["cabin"] = extracted["cabin"]

        constraints = extracted.get("constraints", {}) or {}
        if "max_stops" in constraints:
            patch.setdefault("preferences", {}).setdefault("flight", {})["max_stops"] = constraints["max_stops"]
        if "avoid_red_eye" in constraints:
            patch.setdefault("preferences", {}).setdefault("flight", {})["avoid_red_eye"] = constraints["avoid_red_eye"]
        if "preferred_airlines" in constraints:
            patch.setdefault("preferences", {}).setdefault("flight", {})["preferred_airlines"] = constraints["preferred_airlines"]

        # Apply patch via patcher so invalidations happen
        patch_result = self.patcher.run({
            "trip_intent": trip_intent,
            "patch": patch,
            "patch_source": "trip_requirements_extract",
            "note": "Applied extracted requirements.",
        })
        patch_output = handler_output(patch_result)
        trip_intent = patch_output["trip_intent"]

        # Update phase/state based on missing
        status = trip_intent.setdefault("status", {})
        if missing:
            status["phase"] = "intake"
            status["state"] = "collecting_requirements"
        else:
            status["phase"] = "intake"
            status["state"] = "ready_to_quote"

        # Record clarifying questions as notes (optional)
        if clarifying:
            status.setdefault("notes", []).append(f"[clarifying_questions] {clarifying}")

        output: ApplyToolResultResult = {"trip_intent": trip_intent, "debug": {"patch": patch, "patch_out": patch_output}}
        return {"success": True, "input": dict(payload), "output": output, "stack": [patch_result]}

    @classmethod
    def run_tests(cls) -> bool:
        """Run a minimal battery of tests for this handler. Returns True on success, raises on failure."""
        from .patcher import Patcher
        patcher = Patcher()
        handler = cls(patcher=patcher)
        trip_intent = {"working_memory": {}, "status": {}, "itinerary": {}, "party": {}}
        payload = {"trip_intent": trip_intent, "tool_name": "flight_quote_search", "result": {"options": [{"option_id": "f1"}]}}
        out = handler.run(payload)
        assert out.get("success") is True
        assert "input" in out and "output" in out and "stack" in out
        o = out["output"]
        assert "trip_intent" in o and "debug" in o
        assert o["trip_intent"].get("working_memory", {}).get("flight_quotes") == [{"option_id": "f1"}]
        assert out["stack"] == []
        payload2 = {"trip_intent": dict(trip_intent), "tool_name": "trip_requirements_extract", "result": {"trip_intent": {"origin": "EWR", "destination": "DEN", "travelers": {"adults": 2}, "dates": {"departure_date": "2025-06-01", "return_date": "2025-06-05"}}, "missing_required_fields": []}}
        out2 = handler.run(payload2)
        assert out2.get("success") is True
        assert len(out2.get("stack", [])) == 1, "should have called patcher once"
        return True