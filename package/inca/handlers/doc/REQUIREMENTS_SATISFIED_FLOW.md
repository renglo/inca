# When Are Requirements Satisfied? Flow From User Message to Flight/Hotel Search

This document explains step by step how the system decides that trip requirements are complete and when it starts calling external tools (flight_quote_search, hotel_quote_search).

---

## 1. User sends a message

- **Runner.run()** receives the payload (portfolio, org, entity_id, thread, user_text).
- TripIntent is loaded from store (or created with `_new_trip_intent`).
- The message is routed to an **Event** (e.g. `USER_MESSAGE` with `data.text = user_text`).

---

## 2. First reducer call: event = USER_MESSAGE

- **Reducer.run()** is called with `event.type == "USER_MESSAGE"`.
- Reducer does **not** decide “requirements satisfied” here. It only:
  - Sets `status.phase = "intake"`, `status.state = "collecting_requirements"`.
  - Puts **one** tool call in the queue: **`trip_requirements_extract`** with `user_message` and `context` (timezone, current_intent summary).
- So on a new user message, the **only** tool returned is `trip_requirements_extract`. No flight/hotel tools yet.

---

## 3. Runner runs the tool queue: trip_requirements_extract

- **Runner._run_tool_queue_and_followups()** pops `trip_requirements_extract` and runs it **internally** (no `handler_call`):
  - **`_call_trip_requirements_extract(arguments)`** calls the LLM with a prompt that:
    - Merges the user message with `current_intent`.
    - Asks for structured output: `trip_intent`, `missing_required_fields`, `clarifying_questions`.
- **LLM returns** something like:
  - `trip_intent`: e.g. origin, destination, dates, travelers, lodging.
  - `missing_required_fields`: list of paths still needed for quoting (e.g. `["itinerary.segments[0].depart_date", "party.travelers.adults"]`) or **`[]`** when nothing is missing.
  - `clarifying_questions`: optional list of strings.
- **`_call_trip_requirements_extract`** returns:
  - `success: True` (or False on error/empty response).
  - `trip_intent`, `missing_required_fields`, `clarifying_questions`.

So **requirements are “satisfied” in the LLM’s view when it returns `missing_required_fields: []`**. The runner does not interpret this yet; it only passes the result to the applier.

---

## 4. Applier runs: trip_requirements_extract result

- **Applier._apply_requirements_extract()** receives the extract result and the current `trip_intent`.
- It **merges** the extracted `trip_intent` into the existing one (patcher):
  - Party travelers, itinerary segments (origin/destination/dates), lodging (check_in/check_out, stays), preferences, etc.
- It sets **`status.missing_required = result["missing_required_fields"]`** on the trip_intent.
- It sets **`status.state`**:
  - If `missing_required_fields` is non-empty → `"collecting_requirements"`.
  - If empty → **`"ready_to_quote"`**.

So the **first place that “declares” requirements satisfied** is the **applier**: when `missing_required_fields` is empty, it sets `status.state = "ready_to_quote"`. The mutated `trip_intent` (with segments, travelers, lodging, etc.) is saved.

---

## 5. Second reducer call: event = TOOL_RESULT (trip_requirements_extract)

- After the applier, the runner calls **reducer again** with:
  - `event.type = "TOOL_RESULT"`, `event.data = { "tool_name": "trip_requirements_extract", "result": result }`.
  - `trip_intent` = the **updated** intent (already patched by the applier).
- Reducer does **not** handle TOOL_RESULT in a special event block; it falls through to the **state-driven followups** section.

---

## 6. Reducer: “requirements satisfied” = missing list is empty

- Reducer computes **`missing = _required_fields_missing_for_quotes(trip_intent)`** and sets **`status["missing_required"] = missing`**.
- **`_required_fields_missing_for_quotes`** is the **authoritative** check. It looks at the **current** trip_intent (after the applier’s merge) and returns a list of paths that are still missing:
  - No segments → `["itinerary.segments"]`
  - Segment missing origin/destination/depart_date → e.g. `["itinerary.segments[0].origin.code", ...]`
  - `party.travelers.adults < 1` → `["party.travelers.adults"]`
  - Lodging needed but no check_in/check_out (or stays incomplete) → `["itinerary.lodging.check_in", "itinerary.lodging.check_out"]` or per-stay paths.

So **“requirements satisfied”** in the reducer means **`missing` is an empty list**: the trip_intent has everything needed to request quotes.

- **If `missing` is non-empty:**
  - Reducer sets `status.phase = "intake"`, `status.state = "collecting_requirements"`.
  - Because the event is TOOL_RESULT (not USER_MESSAGE), it returns **one** tool: **`generate_followup_questions`** with `missing` and `user_message`, so the LLM can ask the user for the missing fields. No flight/hotel tools.

- **If `missing` is empty:**
  - Reducer does **not** return early. It continues to the logic that decides **quote** tools.

---

## 7. Reducer: emitting flight_quote_search and hotel_quote_search

- When **`missing` is empty**, reducer continues with:
  - `lodging_needed`, `flight_segment_indices`, `effective_stays`, and working memory (`flight_quotes_by_segment`, `hotel_quotes_by_stay`, etc.).
- For each **flight segment** that **does not yet have quotes**:
  - Sets `status.phase = "quote"`, `status.state = "quoting_flights"`.
  - Appends **`flight_quote_search`** with args from `_build_flight_quote_args(trip_intent, segment_index)`.
  - Breaks after adding the first missing segment (runner will run it; next reducer call can add more).
- If no flight tool was added and lodging is needed and there are effective stays:
  - For the first stay **without quotes**, sets `status.state = "quoting_hotels"` and appends **`hotel_quote_search`** with args from `_build_hotel_quote_args(...)`.

So **the system “declares” that it’s time to run external tools** when:
1. **Reducer** has run with **TOOL_RESULT** for `trip_requirements_extract`.
2. **`_required_fields_missing_for_quotes(trip_intent)`** returns **[]**.
3. Reducer then adds **`flight_quote_search`** and/or **`hotel_quote_search`** to `tool_calls` for segments/stays that don’t have quotes yet.

---

## 8. Runner runs the new tool calls (external)

- The reducer’s `tool_calls` are **appended to the queue** in `_run_tool_queue_and_followups` (`queue.extend(followups)`).
- For **`flight_quote_search`** and **`hotel_quote_search`**, the runner does **not** call an internal function; it builds `tool` and `handler` from the tool name and calls **`self.SHC.handler_call(portfolio, org, tool, handler, tc.arguments)`**.
- So **schd_controller.handler_call** is reached only for these (and other external) tools, not for `trip_requirements_extract`.

---

## Summary table

| Step | Who | What decides “requirements satisfied”? | What happens next |
|------|-----|----------------------------------------|-------------------|
| 1 | Runner | — | User message → Event(USER_MESSAGE). |
| 2 | Reducer | — | Returns only `trip_requirements_extract`. |
| 3 | _call_trip_requirements_extract | LLM returns `missing_required_fields: []`. | Result passed to applier. |
| 4 | Applier | `missing_required_fields` empty → `status.state = "ready_to_quote"`. | TripIntent patched and saved. |
| 5 | Runner | — | Calls reducer with TOOL_RESULT(trip_requirements_extract). |
| 6 | Reducer | `_required_fields_missing_for_quotes(trip_intent)` returns `[]`. | Adds `flight_quote_search` / `hotel_quote_search` to tool_calls. |
| 7 | Runner | — | Runs those tools via **handler_call** (external). |

So: **requirements are satisfied** when both the **LLM** (via `missing_required_fields: []`) and the **reducer** (via `_required_fields_missing_for_quotes` returning `[]`) agree that the current trip_intent has enough to quote. The **reducer** is what actually decides to start flight/hotel search by adding those tools to the queue.
