# Incremental Agent (IncA)

Travel/trip intent handlers (Runner, Reducer, Applier, Patcher, Tools), packaged as a proper Python library.

## Overview

This package provides the inca handlers for trip intent flows: event reduction, tool application, patching, and the main Runner that orchestrates the flow and persists trip state.

## Installation

### For Local Development

```bash
cd /path/to/extensions/inca
pip install -e package/
```

### With renglo (for Runner)

The Runner depends on renglo (load_config, DataController, AgentUtilities, SchdController). Install renglo locally if needed:

```bash
pip install -e dev/renglo-lib
```

## Usage

### Basic usage

```python
from inca.handlers import Runner, Reducer, Applier, Patcher
from inca.handlers.runner import Runner
from inca.handlers.common.stores import InMemoryTripStore, DataControllerTripStore

runner = Runner()
payload = {
    "portfolio": "p1",
    "org": "o1",
    "entity_type": "trip",
    "entity_id": "trip_123",
    "thread": "th1",
    "data": "4 people Newark to Denver for 3 nights on Jan 30",
}
result = runner.run(payload)
```

### Handler interface

Handlers implement a standard `run(payload)` interface and return `{success, input, output, stack}`.

## How the pieces fit together

At a high level the flow is:

1. **Runner** receives a user message and trip identifier.
2. **Runner** loads or initializes the canonical `TripIntent` document from a **Trip store**.
3. **Runner** converts the user message into an `Event` and passes it to the **Reducer**.
4. **Reducer**:
   - Mutates `TripIntent` in a **pure/deterministic** way (no I/O).
   - Emits a **tool queue** (list of tool calls) that should be executed next.
5. **Runner** executes the tool queue as a *mission loop*:
   - For each tool call:
     - It calls external handlers via `SchdController.handler_call(...)` (or `_call_trip_requirements_extract` for the built-in extractor).
     - Passes the tool result into the **Applier**.
     - Applier uses the **Patcher** to apply structured updates to `TripIntent` (quotes, bundles, holds, etc).
     - Runner immediately saves the new `TripIntent` via the configured **Trip store**.
     - Runner calls **Reducer** again with a `TOOL_RESULT` event; the reducer may enqueue follow‑up tools and/or `ui_messages`.
     - Runner sends `ui_messages` to the user through `AgentUtilities.save_chat(...)`.

This loop continues until there are no more tool calls, or safety limits are hit.

### Runner

- **Entrypoint**: `inca.handlers.Runner` (`responses_mission_runner`).
- **Responsibilities**:
  - Validate the incoming payload (`portfolio`, `org`, `entity_type`, `entity_id`, `thread`, `data`/`user_text`).
  - Set up request‑scoped `RunnerContext` (portfolio/org/entity/thread/trip_id/user_text).
  - Wire up dependencies from renglo:
    - `load_config()` → shared config object.
    - `DataController(config=self.config)` → `self.DAC`.
    - `SchdController(config=self.config)` → `self.SHC`.
    - `AgentUtilities(config, portfolio, org, entity_type, entity_id, thread, connection_id)` → `self.AGU`.
  - Choose and configure the **Trip store**:
    - `InMemoryTripStore` by default for tests/demos.
    - `DataControllerTripStore(self.DAC, portfolio, org)` for production persistence.
  - Ensure each user turn has a backing chat message document via `self.AGU.new_chat_message_document(user_text)`.
  - Load or initialize `TripIntent`, stamp `request.now_iso` / `request.now_date`, and route the user message into an `Event` for the reducer.
  - Drive the tool mission loop described above and stream UI output via `self.AGU.save_chat(...)`.

### Reducer

- Pure function (`Reducer.run(...)`) that:
  - Consumes `{ "trip_intent": ..., "event": ... }`.
  - Produces an updated `trip_intent`, optional `ui_messages`, and a **tool queue** (list of `ToolCall` dicts).
- Event types:
  - `USER_MESSAGE` — reacts to a new user message.
  - `TOOL_RESULT` / `TOOL_ERROR` — reacts to results/errors from tools.
  - `INTENT_READY` — intent document is complete; skip extraction and proceed to quote/search (used by Sprinter).
- Typical responsibilities:
  - Decide when to call tools like:
    - `trip_requirements_extract` (LLM extractor).
    - `noma/flight_quote_search`, `noma/hotel_quote_search`.
    - `noma/trip_option_ranker`, `noma/policy_and_risk_check`.
    - `noma/reservation_hold_create`, etc.
  - Advance `status.phase` and `status.state` (e.g. `intake → quote → hold → book`).
  - Generate human‑readable `ui_messages` for the chat interface.

### Patcher

- Applies **deterministic, schema‑aware mutations** to `TripIntent`.
- Designed to keep all derived/cached fields in sync when important inputs change.
- Typical responsibilities:
  - Apply partial updates safely (deep merges, list updates, etc).
  - Handle cache invalidation rules; for example, changing hotel preferences clears hotel quotes and bundles:
    - Clears `working_memory.hotel_quotes` / `hotel_quotes_by_stay`.
    - Clears `working_memory.ranked_bundles`, `working_memory.selected`, and `working_memory.holds`.
    - Moves `status.phase/state` back to a quoting state so tools are re‑run with the new preferences.
- The **Applier** delegates most TripIntent mutations to the Patcher, so every tool result is normalized and applied consistently.

### Applier

- Handles **canonical tool outputs** and writes them into `TripIntent` via the Patcher.
- For each supported tool, it knows:
  - Where to put the result (e.g. `working_memory.flight_quotes`, `working_memory.hotel_quotes_by_stay`, `working_memory.risk_report`, etc).
  - How to translate *tool‑specific* shapes into the canonical TripIntent schema.
- Examples:
  - `trip_requirements_extract`:
    - Merges extracted `trip_intent` into the existing `TripIntent` (itinerary/party/preferences/constraints).
    - Updates `status.missing_required` and `status.assumptions`.
  - `noma/flight_quote_search`:
    - Writes options into `working_memory.flight_quotes` or `working_memory.flight_quotes_by_segment[idx]`.
  - `noma/hotel_quote_search`:
    - Writes options into `working_memory.hotel_quotes` or `working_memory.hotel_quotes_by_stay[idx]`.
  - `noma/trip_option_ranker`:
    - Writes ranked bundles into `working_memory.ranked_bundles`.
  - `noma/reservation_hold_create`:
    - Writes holds into `working_memory.holds` and advances `status.phase/state`.

### Stores

- **TripIntentStore interface**:
  - `get(trip_id) -> dict | None`
  - `save(trip_id, trip_intent) -> None`
- **InMemoryTripStore**
  - Pure in‑process dict used for local tests and demos.
  - Nothing is persisted between processes.
- **DataControllerTripStore**
  - Production implementation backed by renglo’s `DataController`.
  - Uses `DataController.get_a_b_c` / `put_a_b_c` with:
    - `portfolio`, `org` from the Runner context.
    - Ring `"inca_intents"` (see `DataControllerTripStore.RING`).
  - This is how trip state is **persisted to your DB** and later retrieved.
- **InMemoryToolStore**
  - Simple registry for the tool list + prompts:
    - `default_tools()`, `default_system_prompt()`, `default_developer_prompt()`.
  - In production you can replace this with a DB‑backed implementation if you want versioned tool registries.

## Integration with renglo

The Inca Runner is tightly integrated with the renglo libraries to:

- **Load configuration**
  - `load_config()` reads the shared renglo configuration for the current environment.
- **Persist TripIntents**
  - `DataController(config=self.config)` provides `get_a_b_c` / `put_a_b_c`.
  - `DataControllerTripStore(self.DAC, portfolio, org)` uses:
    - `ring = "inca_intents"`.
    - `portfolio` / `org` from the Runner payload.
  - Every time the Patcher or Applier changes `TripIntent`, the Runner calls `trip_store.save(trip_id, trip_intent)`, so:
    - Trip state survives retries and restarts.
    - Other services (analytics, admin tools, etc.) can read the same canonical document.
- **Call tools/handlers**
  - `SchdController(config=self.config)` is used as `self.SHC`.
  - For each tool call `tc` emitted by the reducer:
    - The Runner splits `tc.name` into `extension` and `handler`.
    - Calls `self.SHC.handler_call(portfolio, org, extension, handler, tc.arguments)`.
    - That is how you plug in other renglo‑style extensions (e.g. flight/hotel quote services) into this flow.
- **Save chat messages and widgets**
  - `AgentUtilities` (AGU) is created per request:
    - `self.AGU = AgentUtilities(self.config, portfolio, org, entity_type, entity_id, thread, connection_id=...)`.
  - On each turn:
    - Runner first calls `self.AGU.new_chat_message_document(user_text)` so the turn exists in the renglo chat store.
    - When the reducer returns `ui_messages`, Runner calls:
      - `self.AGU.save_chat({"role": "assistant", "content": text})` for plain messages.
      - `self.AGU.save_chat(ranked_bundles, interface="bundle", msg_type="widget")` for bundle widgets.
  - This is how **all user‑visible output** is written; no handler prints directly to stdout.
- **Use OpenAI / LLMs**
  - `AgentUtilitiesOpenAIResponsesClient(get_agu=lambda: self.AGU)` is used by the Runner to:
    - Implement `trip_requirements_extract` via the OpenAI Responses API.
    - Optionally generate follow‑up questions or extra tool calls.

## End‑to‑end flow examples

This section summarizes how the building blocks above are exercised in typical flows. The full JSON examples live in `inca/handlers/EXAMPLES.md`.

### Example A — From raw message to ready‑to‑quote TripIntent

User says:

> “4 people going from Newark to Denver for 3 nights on January 30.”

1. **Runner input**

   ```python
   payload = {
       "portfolio": "p1",
       "org": "o1",
       "entity_type": "trip",
       "entity_id": "trip_8f3f2c9b",
       "thread": "th1",
       "data": "4 people going from Newark to Denver for 3 nights on January 30",
   }
   result = runner.run(payload)
   ```

2. **Reducer emits `trip_requirements_extract`**
   - Event: `USER_MESSAGE` with the user text.
   - Reducer enqueues a single tool call to the built‑in extractor.

3. **Runner calls extractor + Applier/Patcher update TripIntent**
   - The extractor returns:
     - Origin/destination (`EWR` → `DEN`).
     - Dates (`2026‑01‑30` to `2026‑02‑02`).
     - Travelers (`adults: 4`).
     - Lodging needs (e.g. 2 rooms).
   - Applier merges this into `TripIntent`, and Patcher sets:
     - `status.phase = "intake"`, `status.state = "ready_to_quote"`.
     - `party.travelers.adults = 4`.
     - `itinerary.segments` (outbound + return).
     - `itinerary.lodging.check_in/check_out/rooms`.
   - This corresponds to **“Example 1 — trip_requirements_extract”** in `EXAMPLES.md`.

### Example B — Quote search + ranking + bundles

Starting from the ready‑to‑quote TripIntent above:

1. **Reducer schedules quote tools**
   - Reads TripIntent and enqueues:
     - `noma/flight_quote_search` for flights.
     - `noma/hotel_quote_search` for hotels.

2. **Runner executes tools via `SchdController`**
   - For each tool:
     - Calls `self.SHC.handler_call("noma", "flight_quote_search", args)` (or hotel equivalent).
     - Passes the canonical tool result into the Applier.

3. **Applier writes quotes**
   - Flight results go into `working_memory.flight_quotes`.
   - Hotel results go into `working_memory.hotel_quotes`.
   - Patcher ensures `status.phase/state` move through `quote → ranking_bundles`.

4. **Reducer calls bundle ranker**
   - When both quote lists are present, reducer enqueues `noma/trip_option_ranker`.

5. **Applier stores bundles + Runner sends UI**
   - Applier writes bundles into `working_memory.ranked_bundles`.
   - Reducer returns `ui_messages` explaining the options.
   - Runner calls:
     - `self.AGU.save_chat(ranked_bundles, interface="bundle", msg_type="widget")`.
     - Additional explanatory text messages.
   - This aligns with **Examples 2–4** in `EXAMPLES.md`.

### Example C — User selects a bundle and holds options

1. **User selection**
   - User says: “Pick `bndl_02` and hold it.”
   - Reducer updates:
     - `working_memory.selected.bundle_id`, `flight_option_id`, `hotel_option_id`.
     - `status.phase/state` → `hold/creating_holds`.

2. **Reducer schedules hold tool**
   - Enqueues `noma/reservation_hold_create` with items constructed from `working_memory.selected` and the quote lists.

3. **Runner executes hold tool and Applier updates TripIntent**
   - Tool result includes hold IDs and expiry times.
   - Applier writes them into `working_memory.holds` and advances:
     - `status.phase = "book"`, `status.state = "awaiting_purchase_approval"`.
   - Reducer emits UI text like “I’ve placed holds on your flight and hotel”, which Runner saves via `self.AGU.save_chat(...)`.
   - See **Example 5 — reservation_hold_create** in `EXAMPLES.md`.

### Example D — Preference change and cache invalidation

1. **User changes hotel policy**

   > “Actually, refundable hotel only.”

   - Reducer emits an event that sets `preferences.hotel.refundable_only = true`.

2. **Patcher invalidates derived state**
   - Clears hotel‑dependent caches:
     - `working_memory.hotel_quotes`, `hotel_quotes_by_stay`.
     - `working_memory.ranked_bundles`, `selected`, `holds`.
   - Sets `status.phase/state` back to a quoting state (e.g. `quote/quoting_hotels`).

3. **Reducer re‑enqueues quote + rank tools**
   - New `noma/hotel_quote_search` and `noma/trip_option_ranker` calls are scheduled using the updated intent.
   - This matches **Example 6 — Modification flow** in `EXAMPLES.md`.

### Example E — Multi‑city and multi‑modal trips

The same Runner/Reducer/Applier/Patcher structure supports:

- **Multi‑city** itineraries:
  - Extractor and Applier populate `itinerary.segments` and `itinerary.lodging.stays`.
  - Reducer emits one `flight_quote_search` per segment and one `hotel_quote_search` per stay.
  - Applier stores results in `working_memory.flight_quotes_by_segment` and `working_memory.hotel_quotes_by_stay`.
  - Ranker produces bundles with `flight_option_ids` / `hotel_option_ids` (lists).
  - See **Example 9** in `EXAMPLES.md`.
- **Multi‑modal** itineraries (e.g. flight + train):
  - Each segment has a `transport_mode` (flight/train/etc.).
  - Reducer only calls flight tools for `transport_mode == "flight"` segments today.
  - Future tools (e.g. `train_quote_search`) can plug into the same pattern, with Applier writing into additional working‑memory slots.
  - See **Example 10** in `EXAMPLES.md`.

## Package layout

- `inca/` — top-level package
- `inca/handlers/` — Runner, Reducer, Applier, Patcher, Tools
- `inca/handlers/common/` — types, stores, defaults, openai_adapter

## Development

### Running handler tests

From repo root (with inca package on path or installed):

```bash
cd extensions/inca
python run_handler_tests.py
```

Or install the package and run tests that import from `inca.handlers`.

## License

See main repository LICENSE files.
