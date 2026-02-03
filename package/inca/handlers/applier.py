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
        # Tool names are "extension/handler" or "extension/handler/subhandler"; match by handler part.
        handler = tool_name.split("/", 1)[-1] if "/" in tool_name else tool_name
        result = payload["result"]

        status = trip_intent.setdefault("status", {})
        wm = trip_intent.setdefault("working_memory", {})
        self._ensure_working_memory_defaults(wm)

        if handler == "trip_requirements_extract":
            return self._apply_requirements_extract(payload, trip_intent, result)

        output_base: ApplyToolResultResult = {"trip_intent": trip_intent, "debug": {}}

        if handler == "flight_quote_search":
            args = payload.get("arguments") or {}
            seg_idx = args.get("segment_index")
            options = self._ensure_option_ids(result.get("options", []), "flt_seg", seg_idx if seg_idx is not None else 0)
            if seg_idx is not None:
                by_seg = wm.setdefault("flight_quotes_by_segment", [])
                while len(by_seg) <= seg_idx:
                    by_seg.append(None)
                by_seg[seg_idx] = options
                if len(by_seg) == 1 and by_seg[0]:
                    wm["flight_quotes"] = by_seg[0]
            else:
                wm["flight_quotes"] = options
            status["phase"] = "quote"
            status["state"] = "have_flight_quotes"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        if handler == "hotel_quote_search":
            stay_idx = (payload.get("arguments") or {}).get("stay_index")
            options_by_room = result.get("options_by_room")
            if options_by_room is not None and isinstance(options_by_room, list):
                # Multi-room: each room is its own segment (list of options). Store as list-of-lists per stay.
                per_stay = [list(room_opts) for room_opts in options_by_room]
            else:
                options = self._ensure_option_ids(result.get("options", []), "htl_stay", stay_idx if stay_idx is not None else 0)
                per_stay = [options]
            if stay_idx is not None:
                by_stay = wm.setdefault("hotel_quotes_by_stay", [])
                while len(by_stay) <= stay_idx:
                    by_stay.append(None)
                by_stay[stay_idx] = per_stay
                if len(by_stay) == 1 and by_stay[0]:
                    flat = [o for room_list in by_stay[0] for o in (room_list or [])]
                    wm["hotel_quotes"] = flat
            else:
                wm["hotel_quotes"] = [o for room_list in per_stay for o in (room_list or [])]
            status["phase"] = "quote"
            status["state"] = "have_hotel_quotes"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        if handler == "trip_option_ranker":
            wm["ranked_bundles"] = result.get("bundles", [])
            status["phase"] = "quote"
            status["state"] = "presenting_options"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        if handler == "policy_and_risk_check":
            sel = wm.get("selected", {}) or {}
            wm["risk_report"] = {"bundle_id": sel.get("bundle_id"), **result}
            status["phase"] = "quote"
            status["state"] = "risk_checked"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        if handler == "reservation_hold_create":
            wm["holds"] = result.get("holds", [])
            status["phase"] = "book"
            status["state"] = "awaiting_purchase_approval"
            return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

        if handler == "booking_confirm_and_purchase":
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

    @staticmethod
    def _ensure_option_ids(options: List[Dict[str, Any]], prefix: str, index: int) -> List[Dict[str, Any]]:
        """Ensure each option has option_id (or id for hotels) so TripOptionRanker and downstream lookup match."""
        if not options:
            return options
        out = []
        for i, opt in enumerate(options):
            if not isinstance(opt, dict):
                out.append(opt)
                continue
            o = dict(opt)
            existing = o.get("option_id") or o.get("id")
            if not existing:
                o["option_id"] = f"{prefix}{index}_{i}"
                if "id" not in o and prefix.startswith("htl"):
                    o["id"] = o["option_id"]
            out.append(o)
        return out

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

        # --- party travelers --- (merge so follow-ups like "We are 4 adults" update correctly)
        travelers = extracted.get("travelers")
        if isinstance(travelers, dict) and travelers:
            existing = (trip_intent.get("party", {}) or {}).get("travelers") or {}
            merged = {**existing, **{k: v for k, v in travelers.items() if v is not None}}
            patch.setdefault("party", {}).setdefault("travelers", {})
            patch["party"]["travelers"] = merged

        # --- itinerary trip type ---
        if extracted.get("trip_type"):
            patch.setdefault("itinerary", {})["trip_type"] = extracted["trip_type"]

        # --- itinerary segments: multi-city from extracted.segments or single from origin/destination/dates ---
        origin = extracted.get("origin")
        dest = extracted.get("destination")
        dates = extracted.get("dates", {}) or {}
        extracted_segs = extracted.get("segments") or []
        existing_segs = (trip_intent.get("itinerary", {}) or {}).get("segments") or []

        segs: List[Dict[str, Any]] = []
        if extracted_segs:
            # If extractor returned fewer segments than we already had, preserve existing structure and
            # only overlay fields from extractor (avoids dropping cities on partial corrections like "remember we depart from JFK").
            if existing_segs and len(existing_segs) > len(extracted_segs):
                for i, existing in enumerate(existing_segs):
                    es = extracted_segs[i] if i < len(extracted_segs) else {}
                    o = (es.get("origin") or es.get("origin_code")) or (existing.get("origin") or {}).get("code")
                    d = (es.get("destination") or es.get("destination_code")) or (existing.get("destination") or {}).get("code")
                    o_code = o if isinstance(o, str) else (o.get("code") if isinstance(o, dict) else None)
                    d_code = d if isinstance(d, str) else (d.get("code") if isinstance(d, dict) else None)
                    depart_date = es.get("depart_date") or existing.get("depart_date")
                    if o_code and d_code and depart_date:
                        segs.append({
                            "segment_id": existing.get("segment_id") or es.get("segment_id") or f"seg_{i}",
                            "origin": {"type": "airport", "code": o_code} if isinstance(o_code, str) else (existing.get("origin") or {"type": "airport", "code": o_code}),
                            "destination": {"type": "airport", "code": d_code} if isinstance(d_code, str) else (existing.get("destination") or {"type": "airport", "code": d_code}),
                            "depart_date": depart_date,
                            "transport_mode": es.get("transport_mode") or existing.get("transport_mode", "flight"),
                            "depart_time_window": es.get("depart_time_window") or existing.get("depart_time_window") or {"start": None, "end": None},
                        })
            else:
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
        elif origin and dest:
            # Build segment(s) from origin/destination; depart_date can be filled later
            segs.append({
                "segment_id": "seg_outbound",
                "origin": {"type": "airport", "code": origin},
                "destination": {"type": "airport", "code": dest},
                "depart_date": dates.get("departure_date"),
                "transport_mode": "flight",
                "depart_time_window": {"start": None, "end": None},
            })
            if extracted.get("trip_type") == "round_trip" and dates.get("return_date"):
                segs.append({
                    "segment_id": "seg_return",
                    "origin": {"type": "airport", "code": dest},
                    "destination": {"type": "airport", "code": origin},
                    "depart_date": dates.get("return_date"),
                    "transport_mode": "flight",
                    "depart_time_window": {"start": None, "end": None},
                })
        elif dest:
            # Only destination (e.g. "I'm going to San Francisco") -> one segment, origin missing
            segs.append({
                "segment_id": "seg_outbound",
                "origin": {},
                "destination": {"type": "airport", "code": dest},
                "depart_date": dates.get("departure_date"),
                "transport_mode": "flight",
                "depart_time_window": {"start": None, "end": None},
            })
        elif origin:
            # Only origin (e.g. "I'm flying from JFK") -> one segment, destination missing
            segs.append({
                "segment_id": "seg_outbound",
                "origin": {"type": "airport", "code": origin},
                "destination": {},
                "depart_date": dates.get("departure_date"),
                "transport_mode": "flight",
                "depart_time_window": {"start": None, "end": None},
            })

        # Assume return to origin: if last segment's destination is not the first segment's origin, add return leg
        if segs:
            first_origin_code = (segs[0].get("origin") or {}).get("code") if isinstance(segs[0].get("origin"), dict) else None
            last_dest_code = (segs[-1].get("destination") or {}).get("code") if isinstance(segs[-1].get("destination"), dict) else None
            if first_origin_code and last_dest_code and first_origin_code != last_dest_code:
                return_stays = extracted.get("stays") or lod.get("stays") or []
                return_depart_date = dates.get("return_date")
                if not return_depart_date and return_stays:
                    return_depart_date = (return_stays[-1] or {}).get("check_out")
                segs.append({
                    "segment_id": "seg_return",
                    "origin": {"type": "airport", "code": last_dest_code},
                    "destination": {"type": "airport", "code": first_origin_code},
                    "depart_date": return_depart_date,
                    "transport_mode": "flight",
                    "depart_time_window": {"start": None, "end": None},
                })

        if segs:
            patch.setdefault("itinerary", {})["segments"] = segs

        # --- lodging: multi-city stays or single block (rooms/guests_per_room omitted; hotel search uses party.travelers) ---
        lod = extracted.get("lodging", {}) or {}
        patch.setdefault("itinerary", {}).setdefault("lodging", {})
        if "needed" in lod:
            patch["itinerary"]["lodging"]["needed"] = lod["needed"]
        extracted_stays = extracted.get("stays") or lod.get("stays") or []
        existing_stays = ((trip_intent.get("itinerary") or {}).get("lodging") or {}).get("stays") or []
        if extracted_stays:
            if existing_stays and len(existing_stays) > len(extracted_stays):
                applied_stays = []
                for j, existing in enumerate(existing_stays):
                    s = extracted_stays[j] if j < len(extracted_stays) else {}
                    applied_stays.append({
                        "location_code": s.get("location_code") or s.get("destination") or existing.get("location_code") or existing.get("destination"),
                        "check_in": s.get("check_in") or existing.get("check_in"),
                        "check_out": s.get("check_out") or existing.get("check_out"),
                        "location_hint": s.get("location_hint") or existing.get("location_hint"),
                    })
                patch["itinerary"]["lodging"]["stays"] = applied_stays
            else:
                patch["itinerary"]["lodging"]["stays"] = [
                    {
                        "location_code": s.get("location_code") or s.get("destination"),
                        "check_in": s.get("check_in"),
                        "check_out": s.get("check_out"),
                        "location_hint": s.get("location_hint"),
                    }
                    for s in extracted_stays
                ]
        else:
            if dates.get("departure_date"):
                patch["itinerary"]["lodging"]["check_in"] = dates["departure_date"]
            if dates.get("return_date"):
                patch["itinerary"]["lodging"]["check_out"] = dates["return_date"]
            # Apply explicit hotel dates from user (e.g. "check-in February 12 2026")
            if lod.get("check_in"):
                patch["itinerary"]["lodging"]["check_in"] = lod["check_in"]
            if lod.get("check_out"):
                patch["itinerary"]["lodging"]["check_out"] = lod["check_out"]
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

        # Build a short summary of what was added/changed in this round for the note
        summary_parts: List[str] = []
        if origin:
            summary_parts.append(f"origin: {origin}")
        if dest:
            summary_parts.append(f"destination: {dest}")
        if dates:
            dep = dates.get("departure_date")
            ret = dates.get("return_date")
            if dep and ret:
                summary_parts.append(f"dates: {dep} → {ret}")
            elif dep:
                summary_parts.append(f"departure_date: {dep}")
            elif ret:
                summary_parts.append(f"return_date: {ret}")
        if isinstance(travelers, dict) and travelers:
            t = [f"{v} {k}" for k, v in travelers.items() if v and v != 0]
            if t:
                summary_parts.append(f"travelers: {', '.join(t)}")
        if extracted.get("trip_type"):
            summary_parts.append(f"trip_type: {extracted['trip_type']}")
        if segs:
            seg_bits = []
            for s in segs:
                o_code = (s.get("origin") or {}).get("code") or "?"
                d_code = (s.get("destination") or {}).get("code") or "?"
                dt = s.get("depart_date") or ""
                seg_bits.append(f"{o_code}→{d_code}" + (f" ({dt})" if dt else ""))
            summary_parts.append(f"segments: {', '.join(seg_bits)}")
        lod = extracted.get("lodging", {}) or {}
        if lod.get("check_in") or lod.get("check_out"):
            summary_parts.append(f"lodging: check_in {lod.get('check_in', '—')}, check_out {lod.get('check_out', '—')}")
        if extracted.get("cabin"):
            summary_parts.append(f"cabin: {extracted['cabin']}")
        if missing:
            summary_parts.append(f"missing_required: {missing}")
        else:
            summary_parts.append("missing_required: none")
        summary = "; ".join(summary_parts) if summary_parts else "no fields changed"
        note = f"Applied extracted requirements: {summary}"

        # Apply patch via patcher so invalidations happen
        patch_result = self.patcher.run({
            "trip_intent": trip_intent,
            "patch": patch,
            "patch_source": "trip_requirements_extract",
            "note": note,
        })
        patch_output = handler_output(patch_result)
        trip_intent = patch_output["trip_intent"]

        # Update phase/state based on missing. When missing is empty, do not set state to ready_to_quote
        # here — the reducer controls confirmation flow (awaiting_confirmation → user confirms → ready_to_quote).
        status = trip_intent.setdefault("status", {})
        if missing:
            status["phase"] = "intake"
            status["state"] = "collecting_requirements"
        else:
            status["phase"] = "intake"
            # Leave state unchanged so reducer can keep awaiting_confirmation until user confirms

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