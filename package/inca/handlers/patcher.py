# travel_v1/patcher.py
from __future__ import annotations

import copy
from typing import Any, Dict, List, Tuple

from .common.types import ApplyPatchPayload, ApplyPatchResult, Handler, PatcherHandlerReturn


class Patcher(Handler):
    """
    Internal tool/handler: apply_patch_to_trip_intent

    Responsibilities (v1):
      1) Deep-merge patch into TripIntent (patch wins)
      2) Compute changed_paths (best-effort)
      3) Invalidate working_memory caches deterministically based on changed_paths
      4) Suggest next tools (best-effort hints)

    Input payload:
      {
        "trip_intent": dict,
        "patch": dict,
        "patch_source": str (optional),
        "note": str (optional)
      }

    Output:
      {
        "trip_intent": dict,
        "changed_paths": [str, ...],
        "invalidations": {"cleared":[str,...], "reason":[str,...]},
        "suggested_next_tools": [str,...]
      }
    """

    name = "apply_patch_to_trip_intent"

    def run(self, payload: ApplyPatchPayload | Dict[str, Any]) -> PatcherHandlerReturn:
        trip_intent = payload["trip_intent"]
        patch = payload["patch"]
        patch_source = payload.get("patch_source", "unknown")
        note = payload.get("note", "")

        before = copy.deepcopy(trip_intent)
        self._deep_merge(trip_intent, patch)

        # Append note
        status = trip_intent.setdefault("status", {})
        status.setdefault("notes", [])
        if note:
            status["notes"].append(f"[{patch_source}] {note}")

        changed_paths = self._compute_changed_paths(before, trip_intent)
        cleared, reasons = self._invalidate_caches(trip_intent, changed_paths)
        suggested_next = self._suggest_next_tools(trip_intent, changed_paths)

        output: ApplyPatchResult = {
            "trip_intent": trip_intent,
            "changed_paths": changed_paths,
            "invalidations": {"cleared": cleared, "reason": reasons},
            "suggested_next_tools": suggested_next,
        }
        return {"success": True, "input": dict(payload), "output": output, "stack": []}

    # -------------------------------------------------------------------------
    # Merge + diff
    # -------------------------------------------------------------------------

    def _deep_merge(self, dst: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                self._deep_merge(dst[k], v)
            else:
                dst[k] = v
        return dst

    def _compute_changed_paths(self, before: Any, after: Any, prefix: str = "") -> List[str]:
        """
        Best-effort changed path detection.
        For lists, if not equal, reports the list path rather than per-index diffs.
        """
        if type(before) != type(after):
            return [prefix or "$"]

        if isinstance(before, dict):
            keys = set(before.keys()) | set(after.keys())
            out: List[str] = []
            for k in keys:
                p = f"{prefix}.{k}" if prefix else k
                if k not in before or k not in after:
                    out.append(p)
                else:
                    out.extend(self._compute_changed_paths(before[k], after[k], p))
            return out

        if isinstance(before, list):
            return [prefix or "$"] if before != after else []

        return [prefix or "$"] if before != after else []

    # -------------------------------------------------------------------------
    # Invalidation rules (v1)
    # -------------------------------------------------------------------------

    def _invalidate_caches(self, trip_intent: Dict[str, Any], changed_paths: List[str]) -> Tuple[List[str], List[str]]:
        """
        Clears derived/volatile working_memory fields when upstream fields change.
        This prevents stale quotes/bundles/risk/holds after requirement changes.
        """
        wm = trip_intent.setdefault("working_memory", {})
        self._ensure_working_memory_defaults(wm)

        cleared: List[str] = []
        reasons: List[str] = []

        def any_startswith(prefixes: List[str]) -> bool:
            return any(any(cp.startswith(pref) for pref in prefixes) for cp in changed_paths)

        def clear_key(key: str, reason: str) -> None:
            if key not in wm:
                return
            wm[key] = [] if isinstance(wm[key], list) else None
            cleared.append(f"working_memory.{key}")
            reasons.append(reason)

        def clear_selection(reason: str) -> None:
            wm["selected"] = {"bundle_id": None, "flight_option_id": None, "hotel_option_id": None, "flight_option_ids": [], "hotel_option_ids": []}
            cleared.append("working_memory.selected")
            reasons.append(reason)

        # Flight inputs changed -> clear flight quotes and downstream
        if any_startswith(["itinerary.segments", "preferences.flight", "party.travelers"]):
            clear_key("flight_quotes", "Flight inputs changed → cleared flight quotes.")
            clear_key("flight_quotes_by_segment", "Flight inputs changed → cleared flight quotes by segment.")
            clear_key("ranked_bundles", "Flight inputs changed → cleared ranked bundles.")
            clear_key("risk_report", "Flight inputs changed → cleared risk report.")
            clear_key("holds", "Flight inputs changed → cleared holds.")
            clear_selection("Selection cleared because flight-derived artifacts are stale.")

        # Hotel inputs changed -> clear hotel quotes and downstream (includes itinerary.lodging.stays)
        if any_startswith(["itinerary.lodging", "preferences.hotel"]):
            clear_key("hotel_quotes", "Hotel inputs changed → cleared hotel quotes.")
            clear_key("hotel_quotes_by_stay", "Hotel inputs changed → cleared hotel quotes by stay.")
            clear_key("ranked_bundles", "Hotel inputs changed → cleared ranked bundles.")
            clear_key("risk_report", "Hotel inputs changed → cleared risk report.")
            clear_key("holds", "Hotel inputs changed → cleared holds.")
            clear_selection("Selection cleared because hotel-derived artifacts are stale.")

        # Policy/constraint changes -> clear risk; possibly holds if budget/refund changed
        if any_startswith(["policy", "constraints"]):
            if wm.get("risk_report") is not None:
                clear_key("risk_report", "Policy/constraints changed → cleared risk report.")

            # If the user changed budget/refundability preference, invalidate holds to prevent accidental purchase
            if any(
                cp.startswith("constraints.budget_total")
                or cp.startswith("constraints.refundable_preference")
                for cp in changed_paths
            ):
                if wm.get("holds"):
                    clear_key("holds", "Budget/refundability changed → cleared holds for safety.")

        return cleared, reasons

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

    # -------------------------------------------------------------------------
    # Suggest next tools (hints only)
    # -------------------------------------------------------------------------

    def _suggest_next_tools(self, trip_intent: Dict[str, Any], changed_paths: List[str]) -> List[str]:
        wm = trip_intent.get("working_memory", {})
        suggestions: List[str] = []

        def changed(prefix: str) -> bool:
            return any(cp.startswith(prefix) for cp in changed_paths)

        # If itinerary/party changed and quotes are missing, suggest quoting
        if changed("itinerary.") or changed("party.travelers"):
            if not wm.get("flight_quotes"):
                suggestions.append("flight_quote_search")

            lodging_needed = trip_intent.get("itinerary", {}).get("lodging", {}).get("needed", True)
            if lodging_needed and not wm.get("hotel_quotes"):
                suggestions.append("hotel_quote_search")

        # If quotes exist but bundles missing
        lodging_needed = trip_intent.get("itinerary", {}).get("lodging", {}).get("needed", True)
        if wm.get("flight_quotes") and (not lodging_needed or wm.get("hotel_quotes")) and not wm.get("ranked_bundles"):
            suggestions.append("trip_option_ranker")

        return suggestions

    @classmethod
    def run_tests(cls) -> bool:
        """Run a minimal battery of tests for this handler. Returns True on success, raises on failure."""
        handler = cls()
        trip_intent = {"working_memory": {}, "status": {}, "itinerary": {"lodging": {"needed": True}}, "party": {"travelers": {}}}
        payload = {"trip_intent": trip_intent, "patch": {"itinerary": {"trip_type": "round_trip"}}, "patch_source": "test", "note": "test"}
        out = handler.run(payload)
        assert out.get("success") is True, "expected success"
        assert "input" in out and "output" in out and "stack" in out, "expected success/input/output/stack"
        o = out["output"]
        assert "trip_intent" in o and "changed_paths" in o and "invalidations" in o and "suggested_next_tools" in o
        assert o["trip_intent"]["itinerary"].get("trip_type") == "round_trip", "patch should merge"
        assert out["stack"] == [], "no nested calls"
        return True