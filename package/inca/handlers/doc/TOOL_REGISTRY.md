# TOOL REGISTRY

```json
{
  "schema": "renglo.tool_registry.v1",
  "domain": "travel_booking",
  "tools": [
    {
      "name": "trip_requirements_extract",
      "description": "Extract a normalized TripIntentFields object from a user message and identify missing required fields.",
      "input_schema": {
        "type": "object",
        "properties": {
          "user_message": { "type": "string" },
          "context": {
            "type": "object",
            "properties": {
              "known_traveler_profile_ids": { "type": "array", "items": { "type": "string" } },
              "home_airport_code": { "type": "string" },
              "timezone": { "type": "string" }
            },
            "additionalProperties": true
          }
        },
        "required": ["user_message"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "trip_intent": {
            "type": "object",
            "properties": {
              "origin": { "type": "string", "description": "IATA airport/city code" },
              "destination": { "type": "string", "description": "IATA airport/city code" },
              "dates": {
                "type": "object",
                "properties": {
                  "departure_date": { "type": "string", "format": "date" },
                  "return_date": { "type": "string", "format": "date" }
                },
                "additionalProperties": false
              },
              "trip_type": { "type": "string", "enum": ["one_way", "round_trip", "multi_city"] },
              "travelers": {
                "type": "object",
                "properties": {
                  "adults": { "type": "integer", "minimum": 1 },
                  "children": { "type": "integer", "minimum": 0, "default": 0 },
                  "infants": { "type": "integer", "minimum": 0, "default": 0 }
                },
                "required": ["adults"],
                "additionalProperties": false
              },
              "cabin": { "type": "string", "enum": ["basic_economy", "economy", "premium_economy", "business", "first"] },
              "constraints": {
                "type": "object",
                "properties": {
                  "budget_total": {
                    "type": "object",
                    "properties": {
                      "amount": { "type": "number" },
                      "currency": { "type": "string", "minLength": 3, "maxLength": 3 }
                    },
                    "required": ["amount", "currency"],
                    "additionalProperties": false
                  },
                  "refundable_only": { "type": "boolean" },
                  "max_stops": { "type": "integer", "minimum": 0, "maximum": 3 },
                  "preferred_airlines": { "type": "array", "items": { "type": "string" } },
                  "avoid_red_eye": { "type": "boolean" }
                },
                "additionalProperties": false
              },
              "lodging": {
                "type": "object",
                "properties": {
                  "needed": { "type": "boolean", "default": true },
                  "rooms": { "type": "integer", "minimum": 1, "default": 1 },
                  "guests_per_room": { "type": "integer", "minimum": 1, "default": 2 },
                  "hotel_star_min": { "type": "integer", "minimum": 1, "maximum": 5 },
                  "max_price_per_night": {
                    "type": "object",
                    "properties": {
                      "amount": { "type": "number" },
                      "currency": { "type": "string", "minLength": 3, "maxLength": 3 }
                    },
                    "required": ["amount", "currency"],
                    "additionalProperties": false
                  },
                  "location_hint": { "type": "string" }
                },
                "additionalProperties": false
              }
            },
            "required": ["trip_type", "travelers"],
            "additionalProperties": false
          },
          "missing_required_fields": { "type": "array", "items": { "type": "string" } },
          "clarifying_questions": { "type": "array", "items": { "type": "string" } }
        },
        "required": ["trip_intent", "missing_required_fields", "clarifying_questions"],
        "additionalProperties": false
      }
    },

    {
      "name": "flight_quote_search",
      "description": "Search for flight quote options (no hold, no purchase). Returns canonical flight options.",
      "input_schema": {
        "type": "object",
        "properties": {
          "origin": { "type": "string" },
          "destination": { "type": "string" },
          "departure_date": { "type": "string", "format": "date" },
          "return_date": { "type": "string", "format": "date" },
          "trip_type": { "type": "string", "enum": ["one_way", "round_trip"] },
          "travelers": {
            "type": "object",
            "properties": {
              "adults": { "type": "integer", "minimum": 1 },
              "children": { "type": "integer", "minimum": 0 },
              "infants": { "type": "integer", "minimum": 0 }
            },
            "required": ["adults"],
            "additionalProperties": false
          },
          "cabin": { "type": "string", "enum": ["basic_economy", "economy", "premium_economy", "business", "first"] },
          "constraints": {
            "type": "object",
            "properties": {
              "max_stops": { "type": "integer", "minimum": 0, "maximum": 3 },
              "preferred_airlines": { "type": "array", "items": { "type": "string" } },
              "avoid_red_eye": { "type": "boolean" },
              "max_price_total": {
                "type": "object",
                "properties": { "amount": { "type": "number" }, "currency": { "type": "string" } },
                "required": ["amount", "currency"],
                "additionalProperties": false
              }
            },
            "additionalProperties": false
          },
          "result_limit": { "type": "integer", "minimum": 1, "maximum": 50, "default": 10 }
        },
        "required": ["origin", "destination", "departure_date", "trip_type", "travelers"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "options": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "option_id": { "type": "string" },
                "total_price": {
                  "type": "object",
                  "properties": { "amount": { "type": "number" }, "currency": { "type": "string" } },
                  "required": ["amount", "currency"],
                  "additionalProperties": false
                },
                "refundable": { "type": "boolean" },
                "cabin": { "type": "string" },
                "segments": {
                  "type": "array",
                  "items": {
                    "type": "object",
                    "properties": {
                      "from": { "type": "string" },
                      "to": { "type": "string" },
                      "depart_at": { "type": "string" },
                      "arrive_at": { "type": "string" },
                      "airline": { "type": "string" },
                      "flight_number": { "type": "string" },
                      "stops": { "type": "integer", "minimum": 0 }
                    },
                    "required": ["from", "to", "depart_at", "arrive_at", "airline"],
                    "additionalProperties": true
                  }
                },
                "fare_rules_summary": { "type": "string" },
                "raw_provider_payload": { "type": "object", "additionalProperties": true }
              },
              "required": ["option_id", "total_price", "segments"],
              "additionalProperties": false
            }
          }
        },
        "required": ["options"],
        "additionalProperties": false
      }
    },

    {
      "name": "hotel_quote_search",
      "description": "Search for hotel quote options (no hold, no purchase). Returns canonical hotel options.",
      "input_schema": {
        "type": "object",
        "properties": {
          "destination": { "type": "string", "description": "City code or geo area" },
          "dates": {
            "type": "object",
            "properties": {
              "start_date": { "type": "string", "format": "date" },
              "end_date": { "type": "string", "format": "date" }
            },
            "required": ["start_date", "end_date"],
            "additionalProperties": false
          },
          "rooms": { "type": "integer", "minimum": 1, "default": 1 },
          "guests_per_room": { "type": "integer", "minimum": 1, "default": 2 },
          "constraints": {
            "type": "object",
            "properties": {
              "max_price_per_night": {
                "type": "object",
                "properties": { "amount": { "type": "number" }, "currency": { "type": "string" } },
                "required": ["amount", "currency"],
                "additionalProperties": false
              },
              "refundable_only": { "type": "boolean" },
              "hotel_star_min": { "type": "integer", "minimum": 1, "maximum": 5 },
              "location_hint": { "type": "string" }
            },
            "additionalProperties": false
          },
          "result_limit": { "type": "integer", "minimum": 1, "maximum": 50, "default": 10 }
        },
        "required": ["destination", "dates"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "options": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "option_id": { "type": "string" },
                "hotel_name": { "type": "string" },
                "star_rating": { "type": "number" },
                "address": { "type": "string" },
                "nightly_price": {
                  "type": "object",
                  "properties": { "amount": { "type": "number" }, "currency": { "type": "string" } },
                  "required": ["amount", "currency"],
                  "additionalProperties": false
                },
                "total_price": {
                  "type": "object",
                  "properties": { "amount": { "type": "number" }, "currency": { "type": "string" } },
                  "required": ["amount", "currency"],
                  "additionalProperties": false
                },
                "refundable": { "type": "boolean" },
                "room_description": { "type": "string" },
                "raw_provider_payload": { "type": "object", "additionalProperties": true }
              },
              "required": ["option_id", "hotel_name", "total_price"],
              "additionalProperties": false
            }
          }
        },
        "required": ["options"],
        "additionalProperties": false
      }
    },

    {
      "name": "trip_option_ranker",
      "description": "Combine flight and hotel options into a small set of ranked bundles with tradeoffs.",
      "input_schema": {
        "type": "object",
        "properties": {
          "trip_intent": { "type": "object", "additionalProperties": true },
          "flight_options": { "type": "array", "items": { "type": "object" } },
          "hotel_options": { "type": "array", "items": { "type": "object" } },
          "ranking_policy": {
            "type": "object",
            "properties": {
              "weights": {
                "type": "object",
                "properties": {
                  "price": { "type": "number", "default": 0.5 },
                  "duration": { "type": "number", "default": 0.2 },
                  "refundable": { "type": "number", "default": 0.2 },
                  "convenience": { "type": "number", "default": 0.1 }
                },
                "additionalProperties": false
              }
            },
            "additionalProperties": false
          }
        },
        "required": ["trip_intent", "flight_options"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "bundles": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "bundle_id": { "type": "string" },
                "flight_option_id": { "type": "string" },
                "hotel_option_id": { "type": "string" },
                "estimated_total": {
                  "type": "object",
                  "properties": { "amount": { "type": "number" }, "currency": { "type": "string" } },
                  "required": ["amount", "currency"],
                  "additionalProperties": false
                },
                "why_this_bundle": { "type": "string" },
                "tradeoffs": { "type": "array", "items": { "type": "string" } }
              },
              "required": ["bundle_id", "flight_option_id", "estimated_total", "why_this_bundle"],
              "additionalProperties": false
            }
          }
        },
        "required": ["bundles"],
        "additionalProperties": false
      }
    },

    {
      "name": "policy_and_risk_check",
      "description": "Evaluate a selected flight/hotel against org policy and common travel risks. Returns risks, blockers, and required confirmations.",
      "input_schema": {
        "type": "object",
        "properties": {
          "trip_intent": { "type": "object", "additionalProperties": true },
          "selected_flight": { "type": "object", "additionalProperties": true },
          "selected_hotel": { "type": "object", "additionalProperties": true },
          "org_policy": {
            "type": "object",
            "properties": {
              "max_budget_total": { "type": "object", "additionalProperties": true },
              "require_refundable": { "type": "boolean" },
              "max_stops": { "type": "integer" }
            },
            "additionalProperties": true
          }
        },
        "required": ["trip_intent"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "risks": { "type": "array", "items": { "type": "string" } },
          "blocking_issues": { "type": "array", "items": { "type": "string" } },
          "required_user_confirmations": { "type": "array", "items": { "type": "string" } }
        },
        "required": ["risks", "blocking_issues", "required_user_confirmations"],
        "additionalProperties": false
      }
    },

    {
      "name": "reservation_hold_create",
      "description": "Create temporary holds for selected options (flight/hotel). Must be idempotent.",
      "input_schema": {
        "type": "object",
        "properties": {
          "idempotency_key": { "type": "string" },
          "items": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "item_type": { "type": "string", "enum": ["flight", "hotel"] },
                "option_id": { "type": "string" },
                "traveler_profile_ids": { "type": "array", "items": { "type": "string" } }
              },
              "required": ["item_type", "option_id"],
              "additionalProperties": false
            }
          }
        },
        "required": ["idempotency_key", "items"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "holds": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "hold_id": { "type": "string" },
                "item_type": { "type": "string", "enum": ["flight", "hotel"] },
                "expires_at": { "type": "string" },
                "status": { "type": "string", "enum": ["held", "failed"] }
              },
              "required": ["hold_id", "item_type", "status"],
              "additionalProperties": false
            }
          }
        },
        "required": ["holds"],
        "additionalProperties": false
      }
    },

    {
      "name": "booking_confirm_and_purchase",
      "description": "Finalize booking/purchase after explicit user approval. Must be idempotent.",
      "input_schema": {
        "type": "object",
        "properties": {
          "idempotency_key": { "type": "string" },
          "approval_token": { "type": "string" },
          "hold_ids": { "type": "array", "items": { "type": "string" } },
          "payment_method_id": { "type": "string" },
          "contact_email": { "type": "string" }
        },
        "required": ["idempotency_key", "approval_token", "hold_ids", "payment_method_id"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "confirmation": {
            "type": "object",
            "properties": {
              "record_locator": { "type": "string" },
              "status": { "type": "string", "enum": ["confirmed", "failed"] },
              "tickets": { "type": "array", "items": { "type": "object" } }
            },
            "required": ["status"],
            "additionalProperties": true
          }
        },
        "required": ["confirmation"],
        "additionalProperties": false
      }
    }
  ]
}
```