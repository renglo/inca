SPEC.md


V1 Spec: “Mission Runner + Reducer + Applier + Patcher + Tool Handlers”

A) File layout (recommended)

handlers/
  __init__.py
  common/
    __init__.py
    types.py
    stores.py
    defaults.py
    openai_adapter.py
  patcher.py
  applier.py
  reducer.py
  tools.py
  runner.py
  README.md


B) Core contracts / interfaces

1) Handler contract (every tool + core component)
	•	A handler is a class with:
	•	name: str
	•	run(payload: dict) -> dict  (single output dict)
	•	Naming convention: the handler file name (snake_case) and the class name (PascalCase) match. E.g. applier.py → class Applier; generate_something.py → class GenerateSomething.
	•	Return convention: every handler returns {"success": bool, "input": <payload>, "output": <canonical result>, "stack": [<handler results from every call made in run>]}. The canonical output is what the caller expects (e.g. trip_intent, tool_calls, ui_messages). The stack accumulates the full return of each handler invoked inside run() for troubleshooting and execution graphs. Use handler_output(result) in types.py to unwrap the canonical output from a handler result.
	•	Testing: each handler class has a class method run_tests() that runs a minimal battery of tests for that handler (mocked payloads, assert on return shape and invariants). No separate test files in the handlers folder. Run all handler batteries from the parent folder: python run_handler_tests.py (or python -m extensions.inca.run_handler_tests from repo root). "Batteries included" — tests live next to the handler and stay in sync.

Hard rule: No handler prints directly except the mission runner.
Everything else returns data.

2) External hooks assumed (provided by host runtime)
	•	Runner uses self.AGU.print_chat for UI output (no constructor param; requires AGU context)
	•	Runner uses self.SHC.handler_call(portfolio, org, tool, handler, args) for tool execution
	•	TripIntent persistence is abstracted behind TripIntentStore
	•	Tool registry persistence behind ToolDefinitionsStore

⸻

C) TripIntent.v1 minimal schema slice (what the system assumes exists)

TripIntent root:

```
{
  "schema": "renglo.trip_intent.v1",
  "trip_id": "string",
  "created_at": 123,
  "updated_at": 456,
  "request": { "user_message": "string", "locale": "en-US", "timezone": "America/New_York" },
  "status": { "phase": "...", "state": "...", "missing_required": [], "assumptions": [], "notes": [] },
  "party": { "travelers": { "adults": 0, "children": 0, "infants": 0 }, "traveler_profile_ids": [], "contact": { "email": null, "phone": null } },
  "itinerary": { "trip_type": null, "segments": [], "lodging": { "needed": true, "check_in": null, "check_out": null, "rooms": 1, "guests_per_room": 2, "location_hint": null }, "ground": { "needed": false } },
  "preferences": { "flight": {}, "hotel": {} },
  "constraints": { "budget_total": null, "currency": "USD", "refundable_preference": "either" },
  "policy": { "rules": { "require_user_approval_to_purchase": true, "holds_allowed_without_approval": true } },
  "working_memory": {
    "flight_quotes": [],
    "hotel_quotes": [],
    "ranked_bundles": [],
    "risk_report": null,
    "selected": { "bundle_id": null, "flight_option_id": null, "hotel_option_id": null },
    "holds": [],
    "bookings": []
  },
  "audit": { "events": [] }
}
```

Important invariant: patcher must ensure working_memory keys exist before invalidations.


D) Events + tool calls

1) Event types (V1)
	•	USER_MESSAGE — freeform user text
	•	USER_SELECTED_BUNDLE — user picked a bundle id
	•	USER_REQUEST_HOLD — user says “hold”
	•	USER_APPROVED_PURCHASE — user authorizes purchase with approval_token and payment_method_id
	•	TOOL_RESULT — tool completed
	•	TOOL_ERROR — tool failed
	•	INTENT_READY — intent document is complete; skip extraction and proceed to quote/search (used by Sprinter)

2) ToolCall shape

```
{ "name": "tool_name", "arguments": { ... }, "call_id": "optional" }
```

E) Module responsibilities

Common (handlers/common/ — not handlers themselves):

1) common/types.py
	•	EventType literal union
	•	Event dataclass
	•	ToolCall dataclass
	•	Handler Protocol (run(payload) -> dict)
	•	RunSpecialistFn type alias
	•	TypedDict payload/result types per handler (ReduceTripPayload/Result, ApplyToolResultPayload/Result, ApplyPatchPayload/Result, RunnerPayload/Result, SpecialistToolPayload/Result): optional; handlers accept Payload | Dict[str, Any] and return Result for backwards compatibility; callers can pass plain dicts or typed dicts

2) common/stores.py
	•	TripIntentStore Protocol: get(trip_id), save(trip_id, doc)
	•	ToolDefinitionsStore Protocol: get_tools(registry_key), get_system_prompt, get_developer_prompt
	•	In-memory implementations for testing: InMemoryTripStore, InMemoryToolStore

3) common/defaults.py
	•	default system prompt
	•	default developer prompt
	•	default tools list (placeholder) for the in-memory ToolStore

4) common/openai_adapter.py
	•	OpenAIResponsesClient Protocol
	•	extract_output_text(resp) -> str
	•	extract_tool_calls(resp) -> List[ToolCall]
	•	(Optional) OpenAIClientWrapper if you want to wrap the official SDK.

Handlers (handlers/*.py):

5) patcher.py

Handler: Patcher (class name matches file name patcher.py)
	•	deep merge patch into TripIntent
	•	compute changed_paths
	•	invalidate caches based on changed_paths:
	•	changes in itinerary/party/preferences → clear quotes/bundles/risk/holds + clear selected
	•	changes in policy/constraints → clear risk; possibly holds if refundability/budget changes
	•	returns:
	•	updated trip_intent
	•	changed_paths
	•	invalidations
	•	suggested_next_tools (hint list)

6) applier.py

Handler: Applier(patcher)
	•	applies tool output into TripIntent:
	•	trip_requirements_extract: translate extracted fields into a patch then call patcher
	•	flight_quote_search: writes working_memory.flight_quotes
	•	hotel_quote_search: writes working_memory.hotel_quotes
	•	trip_option_ranker: writes working_memory.ranked_bundles
	•	policy_and_risk_check: writes working_memory.risk_report
	•	reservation_hold_create: writes working_memory.holds
	•	booking_confirm_and_purchase: appends to working_memory.bookings
	•	updates status.phase/state accordingly

7) reducer.py

Handler: Reducer
	•	Computes missing required fields for quoting
	•	Emits next tool calls deterministically:
	•	if missing → UI questions (max 3), no tools
	•	else if no flight quotes → flight_quote_search
	•	else if lodging needed and no hotel quotes → hotel_quote_search
	•	else if quotes exist and no bundles → trip_option_ranker
	•	Handles direct events:
	•	USER_SELECTED_BUNDLE → policy_and_risk_check using selected option ids
	•	USER_REQUEST_HOLD → reservation_hold_create if selected bundle exists & risk not blocking
	•	USER_APPROVED_PURCHASE → booking_confirm_and_purchase if holds exist
	•	Outputs:
	•	tool_calls
	•	ui_messages

8) tools.py

Handler: Tools (base); optional wrapper classes per tool (if you want “everything is a handler”):
	•	TripRequirementsExtract, FlightQuoteSearch, HotelQuoteSearch, TripOptionRanker, PolicyAndRiskCheck, ReservationHoldCreate, BookingConfirmAndPurchase
These are thin wrappers that call run_specialist and return {tool_name, result}

(Strictly speaking the runner can call run_specialist directly; wrappers are optional.)

9) runner.py

Handler: Runner
	•	Entry point
	•	Loads TripIntent; initializes if missing
	•	Routes user message to event (bundle id / hold / approve) using a tiny regex router
	•	Calls reducer
	•	Executes reducer tool_calls in a deterministic queue:
	•	SHC.handler_call
	•	applier
	•	reducer TOOL_RESULT follow-ups appended immediately (mission feel)
	•	Optionally calls OpenAI Responses for extra tool calls after deterministic queue
	•	Prints UI messages via self.AGU.print_chat

⸻

