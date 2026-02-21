# travel_v1/applier.py
"""
Applier: convention-driven mapping of tool output to working_memory.

Uses output_convention.json. A different execution layer would use a different convention.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .common.types import (
    ApplyToolResultPayload,
    ApplyToolResultResult,
    ApplierHandlerReturn,
    Handler,
    handler_output,
)
from .patcher import Patcher


def _default_convention_path() -> str:
    return os.path.join(os.path.dirname(__file__), "common", "output_convention.json")


def _tool_id_from_name(tool_name: str) -> str:
    """Extract tool id from handler path (e.g. noma/flight_quote_search -> flight_quote_search)."""
    return tool_name.split("/", 1)[-1] if "/" in tool_name else tool_name


def _ensure_option_ids(options: List[Dict[str, Any]], prefix: str, index: int) -> List[Dict[str, Any]]:
    """Ensure each option has option_id."""
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


class Applier(Handler):
    """
    Applier that uses output_convention.json to map tool output to working_memory.
    """

    name = "apply_tool_result"

    def __init__(self, *, patcher: Patcher, convention_path: Optional[str] = None) -> None:
        self.patcher = patcher
        path = convention_path or _default_convention_path()
        self._conventions: Dict[str, Any] = {}
        self._status_updates: Dict[str, Dict[str, str]] = {}
        self._load_convention(path)

    def _load_convention(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._conventions = data.get("conventions", {})
            self._status_updates = data.get("status_updates", {})
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Failed to load output convention from {path}: {e}") from e

    def _add_days(self, date_str: str, days: int) -> str:
        """Add days to YYYY-MM-DD string."""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return (dt + timedelta(days=days)).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return date_str

    def _ensure_min_stay_nights(self, trip_intent: Dict[str, Any]) -> None:
        """Ensure each stay has at least 1 night; update next segment depart_date to match."""
        iti = trip_intent.get("itinerary", {}) or {}
        segs = iti.get("segments", []) or []
        lodging = iti.get("lodging", {}) or {}
        stays = lodging.get("stays") or []
        if not stays or len(segs) < 2:
            return
        for j, stay in enumerate(stays):
            if not stay:
                continue
            check_in = stay.get("check_in")
            check_out = stay.get("check_out")
            if not check_in or not check_out:
                continue
            if check_out <= check_in:
                stay["check_out"] = self._add_days(check_in, 1)
                seg_idx = j + 1
                if seg_idx < len(segs) and segs[seg_idx]:
                    segs[seg_idx]["depart_date"] = stay["check_out"]

    def _apply_requirements_extract(
        self, payload: Dict[str, Any], trip_intent: Dict[str, Any], result: Dict[str, Any]
    ) -> ApplierHandlerReturn:
        """Apply trip_requirements_extract result via patcher (same logic as Applier)."""
        extracted = result.get("trip_intent", {}) or {}
        missing = result.get("missing_required_fields", []) or []
        clarifying = result.get("clarifying_questions", []) or []

        patch: Dict[str, Any] = {}
        patch.setdefault("status", {})["missing_required"] = missing

        travelers = extracted.get("travelers")
        if isinstance(travelers, dict) and travelers:
            existing = (trip_intent.get("party", {}) or {}).get("travelers") or {}
            merged = {**existing, **{k: v for k, v in travelers.items() if v is not None}}
            patch.setdefault("party", {}).setdefault("travelers", {})
            patch["party"]["travelers"] = merged

        if extracted.get("trip_type"):
            patch.setdefault("itinerary", {})["trip_type"] = extracted["trip_type"]

        origin = extracted.get("origin")
        dest = extracted.get("destination")
        dates = extracted.get("dates", {}) or {}
        extracted_segs = extracted.get("segments") or []
        existing_segs = (trip_intent.get("itinerary", {}) or {}).get("segments") or []
        lod = extracted.get("lodging", {}) or {}

        segs: List[Dict[str, Any]] = []
        if extracted_segs:
            if existing_segs:
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
                if len(extracted_segs) > len(existing_segs):
                    for es in extracted_segs[len(existing_segs):]:
                        o = es.get("origin") or es.get("origin_code")
                        d = es.get("destination") or es.get("destination_code")
                        o_code = o if isinstance(o, str) else (o.get("code") if isinstance(o, dict) else None)
                        d_code = d if isinstance(d, str) else (d.get("code") if isinstance(d, dict) else None)
                        if o_code and d_code and es.get("depart_date"):
                            segs.append({
                                "segment_id": es.get("segment_id") or f"seg_{len(segs)}",
                                "origin": {"type": "airport", "code": o_code} if isinstance(o_code, str) else (o or {"type": "airport", "code": o_code}),
                                "destination": {"type": "airport", "code": d_code} if isinstance(d_code, str) else (d or {"type": "airport", "code": d_code}),
                                "depart_date": es["depart_date"],
                                "transport_mode": es.get("transport_mode", "flight"),
                                "depart_time_window": es.get("depart_time_window") or {"start": None, "end": None},
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
            segs.append({
                "segment_id": "seg_outbound",
                "origin": {},
                "destination": {"type": "airport", "code": dest},
                "depart_date": dates.get("departure_date"),
                "transport_mode": "flight",
                "depart_time_window": {"start": None, "end": None},
            })
        elif origin:
            segs.append({
                "segment_id": "seg_outbound",
                "origin": {"type": "airport", "code": origin},
                "destination": {},
                "depart_date": dates.get("departure_date"),
                "transport_mode": "flight",
                "depart_time_window": {"start": None, "end": None},
            })

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

        patch.setdefault("itinerary", {}).setdefault("lodging", {})
        if "needed" in lod:
            patch["itinerary"]["lodging"]["needed"] = lod["needed"]
        extracted_stays = extracted.get("stays") or lod.get("stays") or []
        existing_lodging = (trip_intent.get("itinerary") or {}).get("lodging") or {}
        existing_stays = existing_lodging.get("stays") or []
        if not existing_stays and (existing_lodging.get("check_in") or existing_lodging.get("check_out")):
            dest_code = (existing_segs[0].get("destination") or {}).get("code") if existing_segs else None
            existing_stays = [{
                "location_code": dest_code,
                "check_in": existing_lodging.get("check_in"),
                "check_out": existing_lodging.get("check_out"),
            }]
        if extracted_stays:
            if existing_stays:
                applied_stays = []
                for j, existing in enumerate(existing_stays):
                    s = extracted_stays[j] if j < len(extracted_stays) else {}
                    check_in = s.get("check_in") or existing.get("check_in")
                    check_out = s.get("check_out") or existing.get("check_out")
                    if check_in and check_out and check_out <= check_in and existing.get("check_out") and existing.get("check_out") > check_in:
                        check_out = existing["check_out"]
                    applied_stays.append({
                        "location_code": s.get("location_code") or s.get("destination") or existing.get("location_code") or existing.get("destination"),
                        "check_in": check_in,
                        "check_out": check_out,
                        "location_hint": s.get("location_hint") or existing.get("location_hint"),
                    })
                if len(extracted_stays) > len(existing_stays):
                    for s in extracted_stays[len(existing_stays):]:
                        applied_stays.append({
                            "location_code": s.get("location_code") or s.get("destination"),
                            "check_in": s.get("check_in"),
                            "check_out": s.get("check_out"),
                            "location_hint": s.get("location_hint"),
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
            if lod.get("check_in"):
                patch["itinerary"]["lodging"]["check_in"] = lod["check_in"]
            if lod.get("check_out"):
                patch["itinerary"]["lodging"]["check_out"] = lod["check_out"]
        if "location_hint" in lod:
            patch["itinerary"]["lodging"]["location_hint"] = lod["location_hint"]

        if extracted.get("cabin"):
            patch.setdefault("preferences", {}).setdefault("flight", {})["cabin"] = extracted["cabin"]

        constraints = extracted.get("constraints", {}) or {}
        if "max_stops" in constraints:
            patch.setdefault("preferences", {}).setdefault("flight", {})["max_stops"] = constraints["max_stops"]
        if "avoid_red_eye" in constraints:
            patch.setdefault("preferences", {}).setdefault("flight", {})["avoid_red_eye"] = constraints["avoid_red_eye"]
        if "preferred_airlines" in constraints:
            patch.setdefault("preferences", {}).setdefault("flight", {})["preferred_airlines"] = constraints["preferred_airlines"]

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

        patch_result = self.patcher.run({
            "trip_intent": trip_intent,
            "patch": patch,
            "patch_source": "trip_requirements_extract",
            "note": note,
        })
        patch_output = handler_output(patch_result)
        trip_intent = patch_output["trip_intent"]
        self._ensure_min_stay_nights(trip_intent)

        status = trip_intent.setdefault("status", {})
        if missing:
            status["phase"] = "intake"
            status["state"] = "collecting_requirements"
        else:
            status["phase"] = "intake"

        if clarifying:
            status.setdefault("notes", []).append(f"[clarifying_questions] {clarifying}")

        output: ApplyToolResultResult = {"trip_intent": trip_intent, "debug": {"patch": patch, "patch_out": patch_output}}
        return {"success": True, "input": dict(payload), "output": output, "stack": [patch_result]}

    def run(self, payload: ApplyToolResultPayload | Dict[str, Any]) -> ApplierHandlerReturn:
        trip_intent = payload["trip_intent"]
        tool_name = payload["tool_name"]
        result = payload.get("result") or {}
        arguments = payload.get("arguments") or {}

        tool_id = _tool_id_from_name(tool_name)

        if tool_id == "trip_requirements_extract":
            return self._apply_requirements_extract(payload, trip_intent, result)

        status = trip_intent.setdefault("status", {})
        wm = trip_intent.setdefault("working_memory", {})
        self._ensure_working_memory_defaults(wm)

        output_base: ApplyToolResultResult = {"trip_intent": trip_intent, "debug": {}}

        conv = self._conventions.get(tool_id, {})

        if tool_id == "policy_and_risk_check" and "_merge_into" in conv:
            merge_spec = conv["_merge_into"]
            wm_path = merge_spec.get("wm_path", "risk_report")
            merge_keys = merge_spec.get("merge_keys_from_selected", [])
            merged = dict(result)
            sel = wm.get("selected", {}) or {}
            for k in merge_keys:
                if sel.get(k) is not None:
                    merged[k] = sel[k]
            wm[wm_path] = merged
        elif tool_id == "booking_confirm_and_purchase":
            conf = result.get("confirmation")
            if conf is not None:
                wm.setdefault("bookings", []).append(conf)
        elif tool_id == "flight_quote_search":
            options = result.get("options", [])
            options = _ensure_option_ids(options, "flt_seg", arguments.get("segment_index", 0))
            seg_idx = arguments.get("segment_index")
            if seg_idx is not None:
                by_seg = wm.setdefault("flight_quotes_by_segment", [])
                while len(by_seg) <= seg_idx:
                    by_seg.append(None)
                by_seg[seg_idx] = options
                if len(by_seg) == 1 and by_seg[0]:
                    wm["flight_quotes"] = by_seg[0]
            else:
                wm["flight_quotes"] = options
        elif tool_id == "hotel_quote_search":
            options_by_room = result.get("options_by_room")
            stay_idx = arguments.get("stay_index")
            if options_by_room is not None and isinstance(options_by_room, list):
                per_stay = [list(room_opts) for room_opts in options_by_room]
            else:
                options = _ensure_option_ids(result.get("options", []), "htl_stay", stay_idx if stay_idx is not None else 0)
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
        elif tool_id == "trip_option_ranker":
            wm["ranked_bundles"] = result.get("bundles", [])
        elif tool_id == "reservation_hold_create":
            wm["holds"] = result.get("holds", [])
        else:
            for output_key, spec in conv.items():
                if output_key.startswith("_"):
                    continue
                value = result.get(output_key)
                if value is not None:
                    wm_path = (spec or {}).get("wm_path")
                    if wm_path and not (spec or {}).get("append"):
                        wm[wm_path] = value
                    break

        status_update = self._status_updates.get(tool_id, {})
        if status_update:
            status["phase"] = status_update.get("phase", status.get("phase"))
            status["state"] = status_update.get("state", status.get("state"))

        return {"success": True, "input": dict(payload), "output": output_base, "stack": []}

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
