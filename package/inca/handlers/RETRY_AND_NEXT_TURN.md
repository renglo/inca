# Retry and Next Turn After a Tool Failure

When a tool fails (e.g. `trip_requirements_extract` or `flight_quote_search`), the reducer handles **TOOL_ERROR** and sets:

- **status.phase** = `"error"`
- **status.state** = `"retryable"`
- **status.last_tool_error** = `{ "tool_name": "...", "error": "...", "at": <timestamp> }`
- **status.notes** = append `"[tool_error] <tool_name> failed: <error>. Say 'try again' or send a new message to re-run."`
- **ui_messages** = user-visible message like `"Tool error: trip_requirements_extract — ..."`

Memorialization (the last user message and current trip intent) is already saved; only the failed tool’s result was not applied.

---

## What Happens on the Next Run?

1. User sends **any** new message (e.g. **"try again"**, **"retry"**, or a new instruction).
2. Runner loads the saved trip_intent (status was `retryable`; see below it gets reset).
3. Event is **USER_MESSAGE**.
4. Reducer handles USER_MESSAGE:
   - Clears **status.last_tool_error** (new turn).
   - Sets **status.phase** = `"intake"`, **status.state** = `"collecting_requirements"`.
   - Returns **one** tool: **trip_requirements_extract** (same as every user message).
5. Runner runs **trip_requirements_extract** (e.g. merges "try again" with current intent; usually no change to requirements).
6. Applier applies the result; if **missing_required_fields** is empty, status becomes **ready_to_quote**.
7. Reducer runs again with **TOOL_RESULT(trip_requirements_extract)**; if **missing** is empty, it adds **flight_quote_search** and/or **hotel_quote_search** again.
8. Runner runs those tools (external handlers). If they succeed this time, the flow continues; if they fail again, status goes back to **retryable** and **last_tool_error** is updated.

So the **next turn is a full new turn**: extraction runs again, then quote tools are re-queued. There is no special “retry same tool only” path; saying **"try again"** (or any message) safely re-initiates the flow and will re-run extraction and then quote tools.

---

## Safe Way to Re-initiate

- **"try again"** or **"retry"** is a safe and explicit way to start the next turn. The system will re-run extraction (which will typically leave intent unchanged) and then re-queue flight/hotel search.
- Any other message (e.g. “search again”, “continue”, or a change like “actually 3 adults”) also starts a new turn the same way; extraction merges the message with current intent, then quote tools are added if requirements are satisfied.

---

## Status and Notes After a Failure

- **status.last_tool_error** gives the last failure in a structured way: `tool_name`, `error`, `at` (timestamp). UI or logs can show “Last run: &lt;tool_name&gt; failed: &lt;error&gt;”.
- **status.notes** gets an explicit line: `"[tool_error] <tool_name> failed: <error>. Say 'try again' or send a new message to re-run."` so the reason and suggested next step are visible in trip_intent.
