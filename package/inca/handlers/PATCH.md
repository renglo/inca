1) Internal Tool: apply_patch_to_trip_intent

1A) Contract

Input schema (internal)

```
{
  "type": "object",
  "properties": {
    "trip_intent": { "type": "object", "additionalProperties": true },
    "patch": { "type": "object", "additionalProperties": true },
    "patch_source": { "type": "string", "enum": ["user_message", "system_policy", "agent_assumption", "operator_override"] },
    "note": { "type": "string" }
  },
  "required": ["trip_intent", "patch", "patch_source"],
  "additionalProperties": false
}
```

Output schema (internal)

```
{
  "type": "object",
  "properties": {
    "trip_intent": { "type": "object", "additionalProperties": true },
    "changed_paths": { "type": "array", "items": { "type": "string" } },
    "invalidations": {
      "type": "object",
      "properties": {
        "cleared": { "type": "array", "items": { "type": "string" } },
        "reason": { "type": "array", "items": { "type": "string" } }
      },
      "required": ["cleared", "reason"],
      "additionalProperties": false
    },
    "suggested_next_tools": { "type": "array", "items": { "type": "string" } }
  },
  "required": ["trip_intent", "changed_paths", "invalidations", "suggested_next_tools"],
  "additionalProperties": false
}
```


1B) Reference implementation (Python)

```
import copy
from typing import Any, Dict, List, Tuple

def deep_merge(dst: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst

def compute_changed_paths(before: Any, after: Any, prefix: str = "") -> List[str]:
    changes: List[str] = []
    if type(before) != type(after):
        return [prefix or "$"]

    if isinstance(before, dict):
        keys = set(before.keys()) | set(after.keys())
        for k in keys:
            p = f"{prefix}.{k}" if prefix else k
            if k not in before or k not in after:
                changes.append(p)
            else:
                changes.extend(compute_changed_paths(before[k], after[k], p))
        return changes

    if isinstance(before, list):
        if before != after:
            return [prefix or "$"]
        return []

    if before != after:
        return [prefix or "$"]
    return []

def invalidate_caches(trip_intent: Dict[str, Any], changed_paths: List[str]) -> Tuple[List[str], List[str]]:
    """
    Clears working_memory caches based on changed paths.
    Returns (cleared_keys, reasons).
    """
    wm = trip_intent.setdefault("working_memory", {})
    cleared: List[str] = []
    reasons: List[str] = []

    # Ensure keys exist
    wm.setdefault("flight_quotes", [])
    wm.setdefault("hotel_quotes", [])
    wm.setdefault("ranked_bundles", [])
    wm.setdefault("risk_report", None)
    wm.setdefault("holds", [])
    wm.setdefault("selected", {"bundle_id": None, "flight_option_id": None, "hotel_option_id": None})

    def any_startswith(prefixes: List[str]) -> bool:
        return any(any(cp.startswith(pref) for pref in prefixes) for cp in changed_paths)

    def clear(*keys: str, reason: str):
        nonlocal cleared, reasons
        for k in keys:
            if k in wm:
                wm[k] = [] if isinstance(wm[k], list) else None
                cleared.append(f"working_memory.{k}")
        reasons.append(reason)

    # Flight invalidation
    if any_startswith(["itinerary.segments", "preferences.flight", "party.travelers"]):
        clear("flight_quotes", "ranked_bundles", "risk_report", "holds", reason="Flight-related inputs changed → invalidate flight quotes and downstream caches.")
        wm["selected"] = {"bundle_id": None, "flight_option_id": None, "hotel_option_id": None}
        cleared.append("working_memory.selected")
        reasons.append("Selection cleared because bundles/quotes may no longer match.")

    # Hotel invalidation
    if any_startswith(["itinerary.lodging", "preferences.hotel"]):
        clear("hotel_quotes", "ranked_bundles", "risk_report", "holds", reason="Hotel-related inputs changed → invalidate hotel quotes and downstream caches.")
        wm["selected"] = {"bundle_id": None, "flight_option_id": None, "hotel_option_id": None}
        cleared.append("working_memory.selected")
        reasons.append("Selection cleared because bundles/quotes may no longer match.")

    # Policy invalidation
    if any_startswith(["policy", "constraints"]):
        # keep quotes but clear risk and holds if policy affects booking constraints
        if wm.get("risk_report") is not None:
            wm["risk_report"] = None
            cleared.append("working_memory.risk_report")
            reasons.append("Policy/constraints changed → risk report invalid.")
        # optional: if constraints affect pricing/budget strictness, holds might be invalid
        # (you can keep holds if policy changes don't matter to holds)
        # We'll conservatively clear holds when budget/refundable constraints change.
        if any(cp.startswith("constraints.budget_total") or cp.startswith("constraints.refundable_preference") for cp in changed_paths):
            if wm.get("holds"):
                wm["holds"] = []
                cleared.append("working_memory.holds")
                reasons.append("Budget/refundability constraint changed → holds cleared to avoid accidental purchase of now-disallowed items.")

    return cleared, reasons

def suggest_next_tools(trip_intent: Dict[str, Any], changed_paths: List[str]) -> List[str]:
    wm = trip_intent.get("working_memory", {})
    suggestions: List[str] = []

    # If extraction changed core itinerary, likely need (re)quotes
    if any(p.startswith("itinerary.") or p.startswith("party.travelers") for p in changed_paths):
        if not wm.get("flight_quotes"):
            suggestions.append("flight_quote_search")
        # lodging needed?
        lodging_needed = trip_intent.get("itinerary", {}).get("lodging", {}).get("needed", True)
        if lodging_needed and not wm.get("hotel_quotes"):
            suggestions.append("hotel_quote_search")

    # If we have quotes but no bundles
    if wm.get("flight_quotes") and (not trip_intent.get("itinerary", {}).get("lodging", {}).get("needed", True) or wm.get("hotel_quotes")):
        if not wm.get("ranked_bundles"):
            suggestions.append("trip_option_ranker")

    return suggestions

def apply_patch_to_trip_intent(
    trip_intent: Dict[str, Any],
    patch: Dict[str, Any],
    patch_source: str,
    note: str = ""
) -> Dict[str, Any]:
    before = copy.deepcopy(trip_intent)

    deep_merge(trip_intent, patch)

    # Status/audit bookkeeping
    status = trip_intent.setdefault("status", {})
    status.setdefault("notes", [])
    if note:
        status["notes"].append(f"[{patch_source}] {note}")

    changed = compute_changed_paths(before, trip_intent)

    cleared, reasons = invalidate_caches(trip_intent, changed)
    suggested = suggest_next_tools(trip_intent, changed)

    return {
        "trip_intent": trip_intent,
        "changed_paths": changed,
        "invalidations": {"cleared": cleared, "reason": reasons},
        "suggested_next_tools": suggested
    }
```


