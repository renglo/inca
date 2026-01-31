# EXAMPLES

## Baseline TripIntent (before tools)

User says:

“4 people going from Newark to Denver for 3 nights on January 30.”

You start with a mostly-empty TripIntent (only the message is set):

```json
{
  "schema": "renglo.trip_intent.v1",
  "trip_id": "trip_8f3f2c9b",
  "status": {
    "phase": "intake",
    "state": "collecting_requirements",
    "missing_required": [],
    "assumptions": [],
    "notes": []
  },
  "request": {
    "user_message": "4 people going from Newark to Denver for 3 nights on January 30",
    "locale": "en-US",
    "timezone": "America/New_York"
  },
  "party": { "travelers": { "adults": 0, "children": 0, "infants": 0 }, "traveler_profile_ids": [] },
  "itinerary": { "trip_type": null, "segments": [], "lodging": { "needed": true, "stays": [] }, "ground": { "needed": false } },
  "preferences": { "flight": {}, "hotel": {} },
  "constraints": { "budget_total": null, "currency": "USD" },
  "working_memory": {
    "flight_quotes": [],
    "hotel_quotes": [],
    "flight_quotes_by_segment": [],
    "hotel_quotes_by_stay": [],
    "ranked_bundles": [],
    "risk_report": null,
    "selected": { "bundle_id": null, "flight_option_id": null, "hotel_option_id": null, "flight_option_ids": [], "hotel_option_ids": [] },
    "holds": [],
    "bookings": []
  }
}
```

For single-destination trips (Examples 1–8), `selected` and bundles use `flight_option_id` / `hotel_option_id`. For multi-city or multi-modal, the same structures also support `flight_option_ids` / `hotel_option_ids` and per-segment/per-stay quote lists (see Examples 9–10).


## Example 1 — trip_requirements_extract

### 1A) Tool input
```json
{
  "user_message": "4 people going from Newark to Denver for 3 nights on January 30",
  "context": {
    "home_airport_code": "EWR",
    "timezone": "America/New_York"
  }
}
```

### 1B) Tool output

(Here the extractor infers dates + travelers + origin/destination. It might infer “round trip” + return date as “start + 3 nights”.)
```json
{
  "trip_intent": {
    "origin": "EWR",
    "destination": "DEN",
    "trip_type": "round_trip",
    "dates": {
      "departure_date": "2026-01-30",
      "return_date": "2026-02-02"
    },
    "travelers": { "adults": 4, "children": 0, "infants": 0 },
    "cabin": "economy",
    "constraints": {
      "max_stops": 1,
      "avoid_red_eye": false
    },
    "lodging": {
      "needed": true,
      "rooms": 2,
      "guests_per_room": 2
    }
  },
  "missing_required_fields": [],
  "clarifying_questions": []
}
```

### 1C) TripIntent.v1 updates (effects)

You apply the result into the canonical structure and record assumptions:

```json
{
    "status": {
      "phase": "intake",
      "state": "ready_to_quote",
      "missing_required": [],
      "assumptions": [
        "Assumed round trip.",
        "Assumed return date is 3 nights after departure (2026-02-02).",
        "Assumed 2 hotel rooms, 2 guests per room."
      ]
    },
    "party": {
      "travelers": { "adults": 4, "children": 0, "infants": 0 }
    },
    "itinerary": {
      "trip_type": "round_trip",
      "segments": [
        {
          "segment_id": "seg_outbound",
          "origin": { "type": "airport", "code": "EWR" },
          "destination": { "type": "airport", "code": "DEN" },
          "depart_date": "2026-01-30",
          "transport_mode": "flight",
          "depart_time_window": { "start": null, "end": null }
        },
        {
          "segment_id": "seg_return",
          "origin": { "type": "airport", "code": "DEN" },
          "destination": { "type": "airport", "code": "EWR" },
          "depart_date": "2026-02-02",
          "transport_mode": "flight",
          "depart_time_window": { "start": null, "end": null }
        }
      ],
      "lodging": {
        "needed": true,
        "check_in": "2026-01-30",
        "check_out": "2026-02-02",
        "rooms": 2,
        "guests_per_room": 2,
        "location_hint": null,
        "stays": []
      }
    },
    "preferences": {
      "flight": { "cabin": "economy", "max_stops": 1, "avoid_red_eye": false },
      "hotel": { "refundable_only": false, "star_min": 3 }
    }
  }
  ```


At this point, the orchestrator can run quotes without asking the user anything.



## Example 2 — flight_quote_search using TripIntent (tool uses intent-derived inputs)

Assume your orchestrator reads TripIntent fields and builds the tool call.

### 2A) Tool input
```json
{
    "origin": "EWR",
    "destination": "DEN",
    "departure_date": "2026-01-30",
    "return_date": "2026-02-02",
    "trip_type": "round_trip",
    "travelers": { "adults": 4, "children": 0, "infants": 0 },
    "cabin": "economy",
    "constraints": {
      "max_stops": 1,
      "avoid_red_eye": false
    },
    "result_limit": 3
  }
```

### 2B) Tool output (sample canonical flight options)

```json
{
    "options": [
      {
        "option_id": "flt_opt_001",
        "total_price": { "amount": 1480.00, "currency": "USD" },
        "refundable": false,
        "cabin": "economy",
        "segments": [
          {
            "from": "EWR",
            "to": "DEN",
            "depart_at": "2026-01-30T08:05:00-05:00",
            "arrive_at": "2026-01-30T10:20:00-07:00",
            "airline": "UA",
            "flight_number": "UA123",
            "stops": 0
          },
          {
            "from": "DEN",
            "to": "EWR",
            "depart_at": "2026-02-02T14:10:00-07:00",
            "arrive_at": "2026-02-02T19:45:00-05:00",
            "airline": "UA",
            "flight_number": "UA456",
            "stops": 0
          }
        ],
        "fare_rules_summary": "Non-refundable. Changes allowed with fee.",
        "raw_provider_payload": { "provider": "example_gds", "rate_key": "abc" }
      },
      {
        "option_id": "flt_opt_002",
        "total_price": { "amount": 1320.00, "currency": "USD" },
        "refundable": false,
        "cabin": "economy",
        "segments": [
          {
            "from": "EWR",
            "to": "ORD",
            "depart_at": "2026-01-30T07:00:00-05:00",
            "arrive_at": "2026-01-30T08:35:00-06:00",
            "airline": "AA",
            "flight_number": "AA100",
            "stops": 0
          },
          {
            "from": "ORD",
            "to": "DEN",
            "depart_at": "2026-01-30T09:30:00-06:00",
            "arrive_at": "2026-01-30T11:05:00-07:00",
            "airline": "AA",
            "flight_number": "AA200",
            "stops": 0
          },
          {
            "from": "DEN",
            "to": "EWR",
            "depart_at": "2026-02-02T15:30:00-07:00",
            "arrive_at": "2026-02-02T22:10:00-05:00",
            "airline": "AA",
            "flight_number": "AA300",
            "stops": 0
          }
        ],
        "fare_rules_summary": "Non-refundable. 1 stop outbound.",
        "raw_provider_payload": { "provider": "example_gds", "rate_key": "def" }
      }
    ]
  }
```

### 2C) TripIntent.v1 updates

Store these in working memory, and update phase/state:

```json
{
    "status": { "phase": "quote", "state": "quoting_hotels" },
    "working_memory": {
      "flight_quotes": [
        { "option_id": "flt_opt_001", "total_price": { "amount": 1480, "currency": "USD" }, "segments": [/*...*/] },
        { "option_id": "flt_opt_002", "total_price": { "amount": 1320, "currency": "USD" }, "segments": [/*...*/] }
      ]
    },
    "audit": {
      "events": [
        { "ts": "2026-01-30T17:13:10Z", "type": "tool_result", "data": { "tool": "flight_quote_search", "count": 2 } }
      ]
    }
  }
```



## Example 3 — hotel_quote_search using TripIntent (tool uses intent-derived inputs)

### 3A) Tool input
```json
{
  "destination": "DEN",
  "dates": { "start_date": "2026-01-30", "end_date": "2026-02-02" },
  "rooms": 2,
  "guests_per_room": 2,
  "constraints": { "hotel_star_min": 3, "refundable_only": false },
  "result_limit": 2
}
```

### 3B) Tool output
```json
{
  "options": [
    {
      "option_id": "htl_opt_101",
      "hotel_name": "Downtown Denver Hotel",
      "star_rating": 4,
      "address": "123 Example St, Denver, CO",
      "nightly_price": { "amount": 210.00, "currency": "USD" },
      "total_price": { "amount": 1260.00, "currency": "USD" },
      "refundable": true,
      "room_description": "2 Queen Beds, Free cancellation until 48h before",
      "raw_provider_payload": { "provider": "example_hotel_api", "rate_id": "r101" }
    },
    {
      "option_id": "htl_opt_102",
      "hotel_name": "Airport Area Inn",
      "star_rating": 3,
      "address": "999 Example Ave, Denver, CO",
      "nightly_price": { "amount": 160.00, "currency": "USD" },
      "total_price": { "amount": 960.00, "currency": "USD" },
      "refundable": false,
      "room_description": "2 Queen Beds, Non-refundable",
      "raw_provider_payload": { "provider": "example_hotel_api", "rate_id": "r102" }
    }
  ]
}
```

### 3C) TripIntent.v1 updates
```json
{
  "status": { "phase": "quote", "state": "ranking_bundles" },
  "working_memory": {
    "hotel_quotes": [
      { "option_id": "htl_opt_101", "hotel_name": "Downtown Denver Hotel", "total_price": { "amount": 1260, "currency": "USD" } },
      { "option_id": "htl_opt_102", "hotel_name": "Airport Area Inn", "total_price": { "amount": 960, "currency": "USD" } }
    ]
  },
  "audit": {
    "events": [
      { "ts": "2026-01-30T17:13:40Z", "type": "tool_result", "data": { "tool": "hotel_quote_search", "count": 2 } }
    ]
  }
}
```




## Example 4 — trip_option_ranker producing bundles (tool consumes quotes + intent)

This tool takes the intent + both quote lists and produces “packages”.

### 4A) Tool input


```json
{
  "trip_intent": {
    "origin": "EWR",
    "destination": "DEN",
    "trip_type": "round_trip",
    "dates": { "departure_date": "2026-01-30", "return_date": "2026-02-02" },
    "travelers": { "adults": 4, "children": 0, "infants": 0 }
  },
  "flight_options": [
    { "option_id": "flt_opt_001", "total_price": { "amount": 1480, "currency": "USD" }, "segments": [/*...*/] },
    { "option_id": "flt_opt_002", "total_price": { "amount": 1320, "currency": "USD" }, "segments": [/*...*/] }
  ],
  "hotel_options": [
    { "option_id": "htl_opt_101", "total_price": { "amount": 1260, "currency": "USD" }, "refundable": true },
    { "option_id": "htl_opt_102", "total_price": { "amount": 960, "currency": "USD" }, "refundable": false }
  ],
  "ranking_policy": {
    "weights": { "price": 0.5, "duration": 0.2, "refundable": 0.2, "convenience": 0.1 }
  }
}
```

### 4B) Tool output (bundles)

```json
{
  "bundles": [
    {
      "bundle_id": "bndl_01",
      "flight_option_id": "flt_opt_002",
      "hotel_option_id": "htl_opt_102",
      "estimated_total": { "amount": 2280.00, "currency": "USD" },
      "why_this_bundle": "Lowest total price.",
      "tradeoffs": ["1 stop outbound flight", "Hotel is non-refundable"]
    },
    {
      "bundle_id": "bndl_02",
      "flight_option_id": "flt_opt_001",
      "hotel_option_id": "htl_opt_101",
      "estimated_total": { "amount": 2740.00, "currency": "USD" },
      "why_this_bundle": "Most convenient flights + refundable hotel option.",
      "tradeoffs": ["Higher price"]
    }
  ]
}
```

### 4C) TripIntent.v1 updates
```json
{
  "status": { "phase": "quote", "state": "presenting_options" },
  "working_memory": {
    "ranked_bundles": [
      { "bundle_id": "bndl_01", "flight_option_id": "flt_opt_002", "hotel_option_id": "htl_opt_102", "estimated_total": { "amount": 2280, "currency": "USD" } },
      { "bundle_id": "bndl_02", "flight_option_id": "flt_opt_001", "hotel_option_id": "htl_opt_101", "estimated_total": { "amount": 2740, "currency": "USD" } }
    ]
  }
}
```

At this point the orchestrator sends user-facing options, and waits for a selection.



## Example 5 — User selects bundle → reservation_hold_create

User says:

“Pick bndl_02 and hold it.”

### 5A) TripIntent updates (selection)

Before calling the hold tool, update selected fields:

```json
{
  "status": { "phase": "hold", "state": "creating_holds" },
  "working_memory": {
    "selected": {
      "bundle_id": "bndl_02",
      "flight_option_id": "flt_opt_001",
      "hotel_option_id": "htl_opt_101"
    }
  }
}
```

### 5B) Tool input
```json
{
  "idempotency_key": "hold_trip_8f3f2c9b_bndl_02",
  "items": [
    { "item_type": "flight", "option_id": "flt_opt_001", "traveler_profile_ids": [] },
    { "item_type": "hotel", "option_id": "htl_opt_101", "traveler_profile_ids": [] }
  ]
}
```

### 5C) Tool output
```json
{
  "holds": [
    { "hold_id": "hold_flt_9001", "item_type": "flight", "expires_at": "2026-01-30T17:33:00Z", "status": "held" },
    { "hold_id": "hold_htl_9002", "item_type": "hotel", "expires_at": "2026-01-30T18:13:00Z", "status": "held" }
  ]
}
```

### 5D) TripIntent.v1 updates
```json
{
  "status": { "phase": "book", "state": "awaiting_purchase_approval" },
  "working_memory": {
    "holds": [
      { "hold_id": "hold_flt_9001", "item_type": "flight", "expires_at": "2026-01-30T17:33:00Z", "status": "held" },
      { "hold_id": "hold_htl_9002", "item_type": "hotel", "expires_at": "2026-01-30T18:13:00Z", "status": "held" }
    ]
  },
  "audit": {
    "events": [
      { "ts": "2026-01-30T17:14:30Z", "type": "holds_created", "data": { "bundle_id": "bndl_02" } }
    ]
  }
}
```


Now your agent asks for explicit approval before purchase.



## Example 6 — Modification flow: user changes constraint → invalidate caches

User says:

“Actually, refundable hotel only.”

### 6A) TripIntent update (patch)

```json
{
  "preferences": { "hotel": { "refundable_only": true } },
  "status": {
    "notes": ["User changed hotel preference to refundable_only=true"]
  }
}
```

### 6B) Cache invalidation (deterministic state update, no tool call yet)

Because hotel preference affects hotel quotes and bundles, the patcher clears hotel-derived caches (and selection):

```json
{
  "working_memory": {
    "hotel_quotes": [],
    "hotel_quotes_by_stay": [],
    "ranked_bundles": [],
    "risk_report": null,
    "selected": { "bundle_id": null, "flight_option_id": null, "hotel_option_id": null, "flight_option_ids": [], "hotel_option_ids": [] },
    "holds": []
  },
  "status": { "phase": "quote", "state": "quoting_hotels" }
}
```

### 6C) Next tool call built from updated intent

```
Call hotel_quote_search again with refundable_only: true, then re-rank bundles.
```

This is exactly how you avoid needing unquote_hotel vs overwrite_hotel tools: TripIntent is truth; quotes are caches.


## Example 7 — policy_and_risk_check (non-blocking)

### 7A) Starting context in TripIntent

We’re at presenting_options with ranked bundles from earlier. Assume the user says:

“I like bndl_01, is there any risk?”

So you set the “candidate selection” (not a hold yet):

```json
{
  "status": { "phase": "quote", "state": "risk_checking" },
  "working_memory": {
    "selected": {
      "bundle_id": "bndl_01",
      "flight_option_id": "flt_opt_002",
      "hotel_option_id": "htl_opt_102"
    }
  }
}
```
(Recall: bndl_01 uses flt_opt_002 (1 stop outbound) + htl_opt_102 (non-refundable).)




### 7B) Tool input

You construct the call by pulling the selected flight/hotel objects from working memory.

```json
{
  "trip_intent": {
    "origin": "EWR",
    "destination": "DEN",
    "trip_type": "round_trip",
    "dates": { "departure_date": "2026-01-30", "return_date": "2026-02-02" },
    "travelers": { "adults": 4, "children": 0, "infants": 0 },
    "constraints": { "currency": "USD" }
  },
  "selected_flight": {
    "option_id": "flt_opt_002",
    "total_price": { "amount": 1320.0, "currency": "USD" },
    "refundable": false,
    "segments": [
      { "from": "EWR", "to": "ORD", "depart_at": "2026-01-30T07:00:00-05:00", "arrive_at": "2026-01-30T08:35:00-06:00", "airline": "AA", "flight_number": "AA100", "stops": 0 },
      { "from": "ORD", "to": "DEN", "depart_at": "2026-01-30T09:30:00-06:00", "arrive_at": "2026-01-30T11:05:00-07:00", "airline": "AA", "flight_number": "AA200", "stops": 0 },
      { "from": "DEN", "to": "EWR", "depart_at": "2026-02-02T15:30:00-07:00", "arrive_at": "2026-02-02T22:10:00-05:00", "airline": "AA", "flight_number": "AA300", "stops": 0 }
    ],
    "fare_rules_summary": "Non-refundable. 1 stop outbound."
  },
  "selected_hotel": {
    "option_id": "htl_opt_102",
    "hotel_name": "Airport Area Inn",
    "total_price": { "amount": 960.0, "currency": "USD" },
    "refundable": false,
    "room_description": "2 Queen Beds, Non-refundable"
  },
  "org_policy": {
    "max_budget_total": { "amount": 3000.0, "currency": "USD" },
    "require_refundable": false,
    "max_stops": 1
  }
}
```

### 7C) Tool output (non-blocking)
```json
{
  "risks": [
    "Hotel is non-refundable (htl_opt_102). If plans change, you may lose the full hotel amount.",
    "Outbound flight has a connection; connection increases risk of delay/missed flight compared to nonstop."
  ],
  "blocking_issues": [],
  "required_user_confirmations": [
    "Confirm you accept a non-refundable hotel.",
    "Confirm you accept an outbound itinerary with a connection."
  ]
}
```

### 7D) TripIntent.v1 updates (effects)

Write into working_memory.risk_report, and set state to presenting_options (or awaiting_bundle_selection depending on your flow).

```json
{
  "status": { "phase": "quote", "state": "presenting_options" },
  "working_memory": {
    "risk_report": {
      "bundle_id": "bndl_01",
      "risks": [
        "Hotel is non-refundable (htl_opt_102). If plans change, you may lose the full hotel amount.",
        "Outbound flight has a connection; connection increases risk of delay/missed flight compared to nonstop."
      ],
      "blocking_issues": [],
      "required_user_confirmations": [
        "Confirm you accept a non-refundable hotel.",
        "Confirm you accept an outbound itinerary with a connection."
      ],
      "generated_at": "2026-01-30T17:15:20Z"
    }
  },
  "audit": {
    "events": [
      {
        "ts": "2026-01-30T17:15:20Z",
        "type": "tool_result",
        "data": { "tool": "policy_and_risk_check", "bundle_id": "bndl_01", "blocking": false }
      }
    ]
  }
}
```

User-facing effect (what the agent says next):
	•	Presents the risks
	•	Asks for the confirmations (and/or suggests a safer alternative bundle)


## Example 8 — policy_and_risk_check (blocking)

Now let’s do a policy that requires refundable lodging AND a max budget, and show how this blocks proceeding to hold/book.

Assume org policy is:
	•	require refundable: true
	•	max budget total: $2,400

And the user picks bndl_01 again (which is non-refundable hotel + total $2,280, so budget is okay but refundable requirement fails).



### 8A) Tool input

Only differences are org_policy values.

```json
{
  "trip_intent": {
    "origin": "EWR",
    "destination": "DEN",
    "trip_type": "round_trip",
    "dates": { "departure_date": "2026-01-30", "return_date": "2026-02-02" },
    "travelers": { "adults": 4, "children": 0, "infants": 0 },
    "constraints": { "currency": "USD" }
  },
  "selected_flight": { "option_id": "flt_opt_002", "total_price": { "amount": 1320, "currency": "USD" }, "refundable": false },
  "selected_hotel": { "option_id": "htl_opt_102", "total_price": { "amount": 960, "currency": "USD" }, "refundable": false },
  "org_policy": {
    "max_budget_total": { "amount": 2400.0, "currency": "USD" },
    "require_refundable": true,
    "max_stops": 1
  }
}
```

### 8B) Tool output (blocking)

```json
{
  "risks": [
    "Hotel is non-refundable (htl_opt_102)."
  ],
  "blocking_issues": [
    "Policy requires refundable lodging but selected hotel is non-refundable."
  ],
  "required_user_confirmations": [
    "Choose a refundable hotel option or relax the refundable requirement."
  ]
}
```


### 8C) TripIntent.v1 updates (effects)

Key difference: you move to an error-like state (but not fatal), and you prevent holds/purchase until resolved.

```json
{
  "status": {
    "phase": "quote",
    "state": "collecting_requirements",
    "missing_required": ["policy_violation: refundable_lodging_required"],
    "notes": [
      "Blocking policy issue: refundable lodging required. Cannot proceed with holds/purchase for bndl_01."
    ]
  },
  "working_memory": {
    "risk_report": {
      "bundle_id": "bndl_01",
      "risks": ["Hotel is non-refundable (htl_opt_102)."],
      "blocking_issues": ["Policy requires refundable lodging but selected hotel is non-refundable."],
      "required_user_confirmations": ["Choose a refundable hotel option or relax the refundable requirement."],
      "generated_at": "2026-01-30T17:16:05Z"
    },
    "selected": { "bundle_id": "bndl_01", "flight_option_id": "flt_opt_002", "hotel_option_id": "htl_opt_102" }
  }
}
```

User-facing effect:
	•	“I can’t proceed with this hotel under policy. Here are refundable alternatives…”
	•	Then you either:
	•	re-run hotel_quote_search with refundable_only=true, OR
	•	ask user whether policy can be relaxed.



Where to put the  “risk check” 


Option 1: Risk check after ranking, before user selects
	•	Risk-check top 2–3 bundles and show risks upfront.
	•	Pros: fewer surprises
	•	Cons: more tool calls

Option 2: Risk check only after user selects a bundle
	•	Pros: cheaper/faster
	•	Cons: user may choose something that becomes blocked, then you re-rank


---

## Example 9 — Multi-city trip (multiple flights, hotel per city)

User says:

“2 adults, fly Newark to Denver Jan 15, then Denver to San Francisco Jan 18, then San Francisco back to Newark Jan 22. I need a hotel in Denver for 3 nights and a hotel in San Francisco for 4 nights.”

The system supports **multi-city** via `itinerary.segments` (one leg per flight) and `itinerary.lodging.stays` (one stay per city).

### 9A) trip_requirements_extract output (multi-city)

The extractor returns `trip_type: "multi_city"`, a `segments` list, and a `stays` list:

```json
{
  "trip_intent": {
    "trip_type": "multi_city",
    "travelers": { "adults": 2, "children": 0, "infants": 0 },
    "segments": [
      { "origin": "EWR", "destination": "DEN", "depart_date": "2026-01-15", "transport_mode": "flight" },
      { "origin": "DEN", "destination": "SFO", "depart_date": "2026-01-18", "transport_mode": "flight" },
      { "origin": "SFO", "destination": "EWR", "depart_date": "2026-01-22", "transport_mode": "flight" }
    ],
    "stays": [
      { "location_code": "DEN", "check_in": "2026-01-15", "check_out": "2026-01-18", "rooms": 1, "guests_per_room": 2 },
      { "location_code": "SFO", "check_in": "2026-01-18", "check_out": "2026-01-22", "rooms": 1, "guests_per_room": 2 }
    ],
    "lodging": { "needed": true }
  },
  "missing_required_fields": [],
  "clarifying_questions": []
}
```

### 9B) TripIntent.v1 itinerary (after applier)

```json
{
  "itinerary": {
    "trip_type": "multi_city",
    "segments": [
      {
        "segment_id": "seg_0",
        "origin": { "type": "airport", "code": "EWR" },
        "destination": { "type": "airport", "code": "DEN" },
        "depart_date": "2026-01-15",
        "transport_mode": "flight",
        "depart_time_window": { "start": null, "end": null }
      },
      {
        "segment_id": "seg_1",
        "origin": { "type": "airport", "code": "DEN" },
        "destination": { "type": "airport", "code": "SFO" },
        "depart_date": "2026-01-18",
        "transport_mode": "flight",
        "depart_time_window": { "start": null, "end": null }
      },
      {
        "segment_id": "seg_2",
        "origin": { "type": "airport", "code": "SFO" },
        "destination": { "type": "airport", "code": "EWR" },
        "depart_date": "2026-01-22",
        "transport_mode": "flight",
        "depart_time_window": { "start": null, "end": null }
      }
    ],
    "lodging": {
      "needed": true,
      "stays": [
        { "location_code": "DEN", "check_in": "2026-01-15", "check_out": "2026-01-18", "rooms": 1, "guests_per_room": 2 },
        { "location_code": "SFO", "check_in": "2026-01-18", "check_out": "2026-01-22", "rooms": 1, "guests_per_room": 2 }
      ]
    }
  }
}
```

### 9C) flight_quote_search — one call per flight segment (with segment_index)

Reducer emits three tool calls (one per segment). Each call is **one-way** and includes `segment_index` so the applier stores results in `working_memory.flight_quotes_by_segment[segment_index]`.

**Segment 0 (EWR → DEN):**
```json
{
  "origin": "EWR",
  "destination": "DEN",
  "departure_date": "2026-01-15",
  "trip_type": "one_way",
  "travelers": { "adults": 2, "children": 0, "infants": 0 },
  "cabin": "economy",
  "constraints": { "max_stops": 1, "avoid_red_eye": false, "preferred_airlines": [] },
  "result_limit": 10,
  "segment_index": 0
}
```

**Segment 1 (DEN → SFO):**
```json
{
  "origin": "DEN",
  "destination": "SFO",
  "departure_date": "2026-01-18",
  "trip_type": "one_way",
  "travelers": { "adults": 2, "children": 0, "infants": 0 },
  "cabin": "economy",
  "constraints": { "max_stops": 1, "avoid_red_eye": false, "preferred_airlines": [] },
  "result_limit": 10,
  "segment_index": 1
}
```

**Segment 2 (SFO → EWR):**
```json
{
  "origin": "SFO",
  "destination": "EWR",
  "departure_date": "2026-01-22",
  "trip_type": "one_way",
  "travelers": { "adults": 2, "children": 0, "infants": 0 },
  "cabin": "economy",
  "constraints": { "max_stops": 1, "avoid_red_eye": false, "preferred_airlines": [] },
  "result_limit": 10,
  "segment_index": 2
}
```

### 9D) hotel_quote_search — one call per stay (with stay_index)

Reducer emits two tool calls (one per stay). Each includes `stay_index` so the applier stores results in `working_memory.hotel_quotes_by_stay[stay_index]`.

**Stay 0 (Denver):**
```json
{
  "destination": "DEN",
  "dates": { "start_date": "2026-01-15", "end_date": "2026-01-18" },
  "rooms": 1,
  "guests_per_room": 2,
  "constraints": { "hotel_star_min": 3, "refundable_only": false, "location_hint": null },
  "result_limit": 10,
  "stay_index": 0
}
```

**Stay 1 (San Francisco):**
```json
{
  "destination": "SFO",
  "dates": { "start_date": "2026-01-18", "end_date": "2026-01-22" },
  "rooms": 1,
  "guests_per_room": 2,
  "constraints": { "hotel_star_min": 3, "refundable_only": false, "location_hint": null },
  "result_limit": 10,
  "stay_index": 1
}
```

### 9E) trip_option_ranker input (multi-city: by-segment / by-stay)

When all segment and stay quotes are present, the reducer calls the ranker with **flight_options_by_segment** and **hotel_options_by_stay** (lists of option lists). The ranker combines one option per flight segment and one per stay into each bundle.

```json
{
  "trip_intent": {
    "trip_type": "multi_city",
    "segments": [
      { "origin": "EWR", "destination": "DEN", "depart_date": "2026-01-15", "transport_mode": "flight" },
      { "origin": "DEN", "destination": "SFO", "depart_date": "2026-01-18", "transport_mode": "flight" },
      { "origin": "SFO", "destination": "EWR", "depart_date": "2026-01-22", "transport_mode": "flight" }
    ],
    "stays": [
      { "location_code": "DEN", "check_in": "2026-01-15", "check_out": "2026-01-18" },
      { "location_code": "SFO", "check_in": "2026-01-18", "check_out": "2026-01-22" }
    ],
    "travelers": { "adults": 2, "children": 0, "infants": 0 },
    "constraints": {}
  },
  "flight_options_by_segment": [
    [ { "option_id": "flt_ewr_den_01", "total_price": { "amount": 380, "currency": "USD" } }, { "option_id": "flt_ewr_den_02", "total_price": { "amount": 420, "currency": "USD" } } ],
    [ { "option_id": "flt_den_sfo_01", "total_price": { "amount": 220, "currency": "USD" } }, { "option_id": "flt_den_sfo_02", "total_price": { "amount": 260, "currency": "USD" } } ],
    [ { "option_id": "flt_sfo_ewr_01", "total_price": { "amount": 340, "currency": "USD" } }, { "option_id": "flt_sfo_ewr_02", "total_price": { "amount": 390, "currency": "USD" } } ]
  ],
  "hotel_options_by_stay": [
    [ { "option_id": "htl_den_01", "total_price": { "amount": 450, "currency": "USD" } }, { "option_id": "htl_den_02", "total_price": { "amount": 520, "currency": "USD" } } ],
    [ { "option_id": "htl_sfo_01", "total_price": { "amount": 720, "currency": "USD" } }, { "option_id": "htl_sfo_02", "total_price": { "amount": 880, "currency": "USD" } } ]
  ],
  "ranking_policy": { "weights": { "price": 0.5, "duration": 0.2, "refundable": 0.2, "convenience": 0.1 } }
}
```

### 9F) trip_option_ranker output — bundles with flight_option_ids and hotel_option_ids

Each bundle specifies **one option per segment** and **one per stay** using list fields:

```json
{
  "bundles": [
    {
      "bundle_id": "bndl_mc_01",
      "flight_option_ids": [ "flt_ewr_den_01", "flt_den_sfo_01", "flt_sfo_ewr_01" ],
      "hotel_option_ids": [ "htl_den_01", "htl_sfo_01" ],
      "estimated_total": { "amount": 2110.00, "currency": "USD" },
      "why_this_bundle": "Lowest total price across all legs and stays.",
      "tradeoffs": [ "Early morning DEN–SFO leg" ]
    },
    {
      "bundle_id": "bndl_mc_02",
      "flight_option_ids": [ "flt_ewr_den_02", "flt_den_sfo_02", "flt_sfo_ewr_02" ],
      "hotel_option_ids": [ "htl_den_02", "htl_sfo_02" ],
      "estimated_total": { "amount": 2470.00, "currency": "USD" },
      "why_this_bundle": "More convenient times and higher-rated hotels.",
      "tradeoffs": [ "Higher price" ]
    }
  ]
}
```

### 9G) User selects bundle → reservation_hold_create (multi-city)

Selected bundle uses **flight_option_ids** and **hotel_option_ids** (lists). Hold tool receives one item per flight and one per hotel:

```json
{
  "idempotency_key": "hold_trip_xyz_bndl_mc_01",
  "items": [
    { "item_type": "flight", "option_id": "flt_ewr_den_01", "traveler_profile_ids": [] },
    { "item_type": "flight", "option_id": "flt_den_sfo_01", "traveler_profile_ids": [] },
    { "item_type": "flight", "option_id": "flt_sfo_ewr_01", "traveler_profile_ids": [] },
    { "item_type": "hotel", "option_id": "htl_den_01", "traveler_profile_ids": [] },
    { "item_type": "hotel", "option_id": "htl_sfo_01", "traveler_profile_ids": [] }
  ]
}
```

---

## Example 10 — Multi-modal trip (flight + train, one hotel)

User says:

“2 adults: fly Newark to Boston Jan 10, then take the train from Boston to New York on Jan 12, and fly back from New York to Newark Jan 14. I need a hotel in New York for 2 nights (Jan 12–14).”

The system supports **multi-modal** via `transport_mode` on each segment. Only segments with `transport_mode: "flight"` get flight quotes; other modes (e.g. `"train"`) are stored in the itinerary but do not trigger flight_quote_search in the current implementation (you can extend with train_quote_search later).

### 10A) trip_requirements_extract output (multi-modal)

```json
{
  "trip_intent": {
    "trip_type": "multi_city",
    "travelers": { "adults": 2, "children": 0, "infants": 0 },
    "segments": [
      { "origin": "EWR", "destination": "BOS", "depart_date": "2026-01-10", "transport_mode": "flight" },
      { "origin": "BOS", "destination": "NYC", "depart_date": "2026-01-12", "transport_mode": "train" },
      { "origin": "NYC", "destination": "EWR", "depart_date": "2026-01-14", "transport_mode": "flight" }
    ],
    "stays": [
      { "location_code": "NYC", "check_in": "2026-01-12", "check_out": "2026-01-14", "rooms": 1, "guests_per_room": 2 }
    ],
    "lodging": { "needed": true }
  },
  "missing_required_fields": [],
  "clarifying_questions": []
}
```

### 10B) TripIntent.v1 itinerary (segments with transport_mode)

```json
{
  "itinerary": {
    "trip_type": "multi_city",
    "segments": [
      {
        "segment_id": "seg_0",
        "origin": { "type": "airport", "code": "EWR" },
        "destination": { "type": "airport", "code": "BOS" },
        "depart_date": "2026-01-10",
        "transport_mode": "flight",
        "depart_time_window": { "start": null, "end": null }
      },
      {
        "segment_id": "seg_1",
        "origin": { "type": "station", "code": "BOS" },
        "destination": { "type": "station", "code": "NYC" },
        "depart_date": "2026-01-12",
        "transport_mode": "train",
        "depart_time_window": { "start": null, "end": null }
      },
      {
        "segment_id": "seg_2",
        "origin": { "type": "airport", "code": "LGA" },
        "destination": { "type": "airport", "code": "EWR" },
        "depart_date": "2026-01-14",
        "transport_mode": "flight",
        "depart_time_window": { "start": null, "end": null }
      }
    ],
    "lodging": {
      "needed": true,
      "stays": [
        { "location_code": "NYC", "check_in": "2026-01-12", "check_out": "2026-01-14", "rooms": 1, "guests_per_room": 2 }
      ]
    }
  }
}
```

Note: segment 2 is NYC → EWR (return flight); origin is LGA (or JFK) and destination EWR. The reducer only runs **flight_quote_search** for segments where `transport_mode === "flight"` (segments 0 and 2). Segment 1 (train) is not quoted by flight_quote_search; a future **train_quote_search** could consume segment 1 and write into a `train_quotes_by_segment` or similar.

### 10C) flight_quote_search calls (only for flight segments)

Reducer emits two flight_quote_search calls: **segment_index 0** (EWR → BOS) and **segment_index 2** (NYC → EWR). No call for segment_index 1 (train).

**Segment 0 (EWR → BOS):**
```json
{
  "origin": "EWR",
  "destination": "BOS",
  "departure_date": "2026-01-10",
  "trip_type": "one_way",
  "travelers": { "adults": 2, "children": 0, "infants": 0 },
  "cabin": "economy",
  "constraints": { "max_stops": 1, "avoid_red_eye": false, "preferred_airlines": [] },
  "result_limit": 10,
  "segment_index": 0
}
```

**Segment 2 (LGA → EWR, return flight):**
```json
{
  "origin": "LGA",
  "destination": "EWR",
  "departure_date": "2026-01-14",
  "trip_type": "one_way",
  "travelers": { "adults": 2, "children": 0, "infants": 0 },
  "cabin": "economy",
  "constraints": { "max_stops": 1, "avoid_red_eye": false, "preferred_airlines": [] },
  "result_limit": 10,
  "segment_index": 2
}
```


### 10D) hotel_quote_search (single stay)

One call with `stay_index: 0` for the New York stay:

```json
{
  "destination": "NYC",
  "dates": { "start_date": "2026-01-12", "end_date": "2026-01-14" },
  "rooms": 1,
  "guests_per_room": 2,
  "constraints": { "hotel_star_min": 3, "refundable_only": false, "location_hint": null },
  "result_limit": 10,
  "stay_index": 0
}
```

### 10E) working_memory shape (multi-modal)

After flight and hotel tools run:

- **flight_quotes_by_segment**: `[ options_EWR_BOS, null, options_NYC_EWR ]` — index 1 is null (train segment; no flight quote).
- **hotel_quotes_by_stay**: `[ options_NYC ]`.

The ranker can be extended to accept optional train options per segment and combine flight + train + hotel into bundles; until then, bundles are built from flight options (for flight segments only) and hotel options.

