# Trip intent: user script and expected effects

Use this as a script to drive a **brand new** trip_intent conversation. For each user message, the table shows what should be updated in the trip_intent document (incremental assembly).

---

## Required fields for quoting (reducer)

Before the system can run flight/hotel quotes, trip_intent must have:

- **party.travelers.adults** ≥ 1  
- **itinerary.segments** (at least one segment)  
- For each segment: **origin.code**, **destination.code**, **depart_date**  
- **itinerary.lodging.check_in**, **itinerary.lodging.check_out** (when lodging is needed)

---

## User script and expected trip_intent effects

Assume a new trip (trip_id from document creation). Each row is one user message and the expected change in trip_intent after the extractor + applier run.

| # | User enters (message) | Expected effect in trip_intent |
|---|------------------------|---------------------------------|
| 1 | **Hello I'm going to San Francisco** | **itinerary.segments**: one segment with `destination.code = "SFO"` (origin still missing). **request.user_message** updated. Other fields unchanged. |
| 2 | **I'm flying from JFK** | **itinerary.segments[0].origin.code** = `"JFK"`. Segment now has origin + destination; **depart_date** still missing. |
| 3 | **I'm going on March 11** | **itinerary.segments[0].depart_date** = `"2026-03-11"` (or inferred YYYY-MM-DD). **itinerary.lodging.check_in** = `"2026-03-11"` (if LLM infers hotel same as flight date). |
| 4 | **I'm going to stay for 5 nights** | **itinerary.lodging.check_out** = `"2026-03-16"` (check_in + 5 nights). **lodging** may get **rooms** / **guests_per_room** if inferred. |
| 5 | **We are 4 adults** | **party.travelers.adults** = `4`. **party.travelers.children** / **infants** may stay 0 or be set. |
| 6 | **I would like to fly out on March 11 and get back on March 16** | **itinerary.segments[0].depart_date** = `"2026-03-11"`. If **trip_type** = round_trip: second segment with **origin** = SFO, **destination** = JFK, **depart_date** = `"2026-03-16"`. **itinerary.lodging.check_in** = `"2026-03-11"`, **check_out** = `"2026-03-16"`. |

After step 6 (or once all required fields are present), **status.missing_required** should be `[]` and the reducer should move to quoting (flight_quote_search / hotel_quote_search).

---

## Minimal script (shortest path to “ready to quote”)

| # | User enters | Expected effect |
|---|-------------|-----------------|
| 1 | **4 people going from Newark to Denver for 3 nights on January 30** | **party.travelers.adults** = 4. **itinerary.segments**: one segment origin EWR, destination DEN. **depart_date** = 2026-01-30 (or 2025-01-30). **itinerary.lodging.check_in** = 2026-01-30, **check_out** = 2026-02-02 (3 nights). **trip_type** may be set (e.g. round_trip). If all required fields are filled, **missing_required** = []. |
| 2 | (if something was missing) **We are checking in on February 12 2026** | **itinerary.lodging.check_in** = `"2026-02-12"`. **check_out** may be inferred or asked next. |
| 3 | (if needed) **We are 4 adult travelers** | **party.travelers.adults** = 4. |

---

## Field reference (where things live)

| Path | Meaning | Example |
|------|---------|---------|
| **request.user_message** | Last user message | `"Hello I'm going to San Francisco"` |
| **party.travelers.adults** | Number of adult travelers | `4` |
| **party.travelers.children** | Number of children | `0` |
| **party.travelers.infants** | Number of infants | `0` |
| **itinerary.trip_type** | one_way, round_trip, multi_city | `"round_trip"` |
| **itinerary.segments** | List of flight (or other) segments | See below |
| **itinerary.segments[i].origin.code** | IATA origin | `"JFK"` |
| **itinerary.segments[i].destination.code** | IATA destination | `"SFO"` |
| **itinerary.segments[i].depart_date** | Date of departure (YYYY-MM-DD) | `"2026-03-11"` |
| **itinerary.lodging.needed** | Whether hotel is needed | `true` |
| **itinerary.lodging.check_in** | Hotel check-in date (YYYY-MM-DD) | `"2026-03-11"` |
| **itinerary.lodging.check_out** | Hotel check-out date (YYYY-MM-DD) | `"2026-03-16"` |
| **itinerary.lodging.rooms** | Number of rooms | `1` |
| **itinerary.lodging.guests_per_room** | Guests per room | `2` |
| **status.missing_required** | Paths still missing for quoting | `[]` when ready |
| **status.phase** | intake / quote / book / completed | `"intake"` until ready |
| **status.state** | collecting_requirements / ready_to_quote / … | `"collecting_requirements"` until **missing_required** is empty |

---

## Example segment shape (after applier)

```json
{
  "segment_id": "seg_outbound",
  "origin": { "type": "airport", "code": "JFK" },
  "destination": { "type": "airport", "code": "SFO" },
  "depart_date": "2026-03-11",
  "transport_mode": "flight",
  "depart_time_window": { "start": null, "end": null }
}
```

Round-trip adds a second segment (e.g. SFO → JFK on **depart_date** = check_out or return date).

---

## How to use this script

1. Create a new trip document (your system provides **trip_id**).
2. Send messages in order (column “User enters”); each message is the next user turn.
3. After each turn, load the trip_intent from the store and check that the “Expected effect” is present (and that **status.missing_required** shrinks as required fields are filled).
4. When **status.missing_required** is `[]`, the flow should proceed to flight/hotel quoting.
