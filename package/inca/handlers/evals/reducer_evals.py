#!/usr/bin/env python3
"""
Reducer evals: corner cases for confirmation detection, intent classification, error recovery.

Run with: python -m inca.handlers.evals.reducer_evals
Or from extensions/inca: python -m package.inca.handlers.evals.reducer_evals
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict

# Ensure package root is on path when run as script
def _ensure_path() -> None:
    # evals -> handlers -> inca -> package; add package to path for absolute imports
    _file = os.path.abspath(__file__)
    for _ in range(4):
        _file = os.path.dirname(_file)
    if _file not in sys.path:
        sys.path.insert(0, _file)


def _make_trip_intent_awaiting_confirmation() -> Dict[str, Any]:
    """Trip intent with full requirements, state=awaiting_confirmation."""
    return {
        "working_memory": {},
        "status": {"phase": "intake", "state": "awaiting_confirmation", "missing_required": []},
        "request": {"user_message": "yes"},
        "itinerary": {
            "trip_type": "round_trip",
            "segments": [
                {"origin": {"code": "EWR"}, "destination": {"code": "DEN"}, "depart_date": "2025-06-01", "transport_mode": "flight"},
                {"origin": {"code": "DEN"}, "destination": {"code": "EWR"}, "depart_date": "2025-06-05", "transport_mode": "flight"},
            ],
            "lodging": {"needed": True, "check_in": "2025-06-01", "check_out": "2025-06-05"},
        },
        "party": {"travelers": {"adults": 2, "children": 0}},
    }


# -----------------------------------------------------------------------------
# Eval cases: (user_message, expected_is_confirmation, description)
# -----------------------------------------------------------------------------
CONFIRMATION_EVALS = [
    # Clear confirmations
    ("yes", True, "Simple yes"),
    ("Yes, go ahead", True, "Explicit go ahead"),
    ("looks good", True, "Looks good"),
    ("sounds good to me", True, "Sounds good"),
    ("ok proceed", True, "Ok proceed"),
    ("that's right", True, "That's right"),
    ("perfect", True, "Perfect"),
    ("book it", True, "Book it (implicit confirm)"),
    # Clear non-confirmations (change requests)
    ("actually, I need to change the return date", False, "Change request: dates"),
    ("add one more traveler first", False, "Change request: travelers"),
    ("wait, the destination is wrong", False, "Change request: destination"),
    ("can we do June 10 instead?", False, "Question/change: dates"),
    # Ambiguous - programmatic may get wrong
    ("what about refundable options?", False, "Question about options"),
    ("that's too expensive", False, "Rejection + implicit change"),
    ("hold on", False, "Hold on - not confirm"),
    ("maybe", False, "Unclear - not confirm"),
    # Edge: confirmation + change in one (LLM should detect primary intent)
    ("yes, and add my spouse", True, "Confirmation with addition (primary: confirm)"),
    ("that works, but can you find something cheaper?", True, "Confirm + refinement (primary: confirm)"),
]


def run_confirmation_evals(use_llm: bool = False) -> Dict[str, Any]:
    """
    Run confirmation detection evals.
    use_llm=False: programmatic (NoOpReducerLLMClient)
    use_llm=True: requires OpenAI client (skip if not available)
    """
    _ensure_path()
    from ..reducer import Reducer
    from ..common.reducer_llm import NoOpReducerLLMClient, ReducerLLMClientFromOpenAI

    llm_client: Any = NoOpReducerLLMClient()
    if use_llm:
        try:
            from renglo.common import load_config
            from renglo.agent.agent_utilities import AgentUtilities
            config = load_config()
            agu = AgentUtilities(config=config)
            from inca.handlers.common.openai_adapter import AgentUtilitiesOpenAIResponsesClient
            openai_client = AgentUtilitiesOpenAIResponsesClient(get_agu=lambda: agu)
            llm_client = ReducerLLMClientFromOpenAI(create_response=openai_client.create_response)
        except ImportError:
            return {"skipped": True, "reason": "renglo not installed", "use_llm": True}
        except Exception as e:
            return {"skipped": True, "reason": str(e), "use_llm": True}

    reducer = Reducer(llm_client=llm_client)
    trip_intent = _make_trip_intent_awaiting_confirmation()
    summary = reducer._format_trip_summary(trip_intent)

    results = []
    passed = 0
    failed = 0
    for user_message, expected, desc in CONFIRMATION_EVALS:
        got = reducer._is_confirmation(user_message, summary)
        ok = got == expected
        if ok:
            passed += 1
        else:
            failed += 1
        results.append({
            "user_message": user_message,
            "expected": expected,
            "got": got,
            "ok": ok,
            "description": desc,
        })

    return {
        "use_llm": use_llm,
        "passed": passed,
        "failed": failed,
        "total": len(CONFIRMATION_EVALS),
        "results": results,
    }


def run_full_reducer_evals() -> Dict[str, Any]:
    """
    Run full reducer flow evals (USER_MESSAGE -> tool_calls, TOOL_RESULT confirmation flow).
    Uses programmatic client only (deterministic).
    """
    _ensure_path()
    from ..reducer import Reducer

    reducer = Reducer()
    results = []

    # Eval 1: USER_MESSAGE returns trip_requirements_extract
    trip_intent = {"working_memory": {}, "status": {}, "itinerary": {}, "party": {}}
    out = reducer.run({
        "trip_intent": trip_intent,
        "event": {"type": "USER_MESSAGE", "data": {"text": "fly EWR to DEN next week"}},
    })
    tool_names = [tc.get("name", "") for tc in out["output"].get("tool_calls", [])]
    ok1 = "trip_requirements_extract" in str(tool_names)
    results.append({"eval": "USER_MESSAGE_returns_extract", "ok": ok1})

    # Eval 2: TOOL_RESULT trip_requirements_extract, awaiting_confirmation, user says "yes" -> ready_to_quote + tool_calls
    trip_intent = _make_trip_intent_awaiting_confirmation()
    out = reducer.run({
        "trip_intent": trip_intent,
        "event": {
            "type": "TOOL_RESULT",
            "data": {
                "tool_name": "trip_requirements_extract",
                "user_message": "yes",
                "result": {"trip_intent": trip_intent, "missing_required_fields": []},
            },
        },
    })
    state = out["output"]["trip_intent"].get("status", {}).get("state", "")
    tool_calls = out["output"].get("tool_calls", [])
    # After confirm, reducer proceeds to quote phase (ready_to_quote -> quoting_flights)
    ok2 = state in ("ready_to_quote", "quoting_flights") and len(tool_calls) >= 1
    results.append({"eval": "confirm_yes_proceeds_to_quote", "ok": ok2})

    # Eval 3: User says "change the date" + extractor returns clarifying_questions -> surface them
    trip_intent = _make_trip_intent_awaiting_confirmation()
    clarifying = ["What date would you like to return?"]
    out = reducer.run({
        "trip_intent": trip_intent,
        "event": {
            "type": "TOOL_RESULT",
            "data": {
                "tool_name": "trip_requirements_extract",
                "user_message": "actually I need to change the return date",
                "result": {"clarifying_questions": clarifying, "missing_required_fields": []},
            },
        },
    })
    ui_messages = out["output"].get("ui_messages", [])
    ok3 = (
        out["output"]["trip_intent"].get("status", {}).get("state") == "awaiting_confirmation"
        and clarifying[0] in str(ui_messages)
    )
    results.append({"eval": "change_request_surfaces_clarifying", "ok": ok3})

    # Eval 3b: Extractor returns empty -> LLM fallback asks (mock simulates LLM returning question)
    from ..common.reducer_llm import NoOpReducerLLMClient
    class _MockAsksClient(NoOpReducerLLMClient):
        def infer_clarifying_question(self, user_message, conv, trip_summary):
            return "What date would you like to return?"
    reducer_3b = Reducer(llm_client=_MockAsksClient())
    trip_intent = _make_trip_intent_awaiting_confirmation()
    out = reducer_3b.run({
        "trip_intent": trip_intent,
        "event": {
            "type": "TOOL_RESULT",
            "data": {
                "tool_name": "trip_requirements_extract",
                "user_message": "Can I change the return date?",
                "result": {"clarifying_questions": [], "missing_required_fields": []},
            },
        },
    })
    ui_messages = out["output"].get("ui_messages", [])
    ok3b = "What date would you like to return?" in str(ui_messages)
    results.append({"eval": "change_request_fallback_clarifying", "ok": ok3b})

    # Eval 3c: User specifies date -> LLM returns None (no question needed)
    class _MockNoQuestionClient(NoOpReducerLLMClient):
        def infer_clarifying_question(self, user_message, conv, trip_summary):
            return None
    reducer_3c = Reducer(llm_client=_MockNoQuestionClient())
    trip_intent = _make_trip_intent_awaiting_confirmation()
    out = reducer_3c.run({
        "trip_intent": trip_intent,
        "event": {
            "type": "TOOL_RESULT",
            "data": {
                "tool_name": "trip_requirements_extract",
                "user_message": "Change the departure date to March 10",
                "result": {"clarifying_questions": [], "missing_required_fields": [], "trip_intent": {}},
            },
        },
    })
    ui_messages = out["output"].get("ui_messages", [])
    ok3c = "What date would you like to depart?" not in str(ui_messages)
    results.append({"eval": "user_specified_date_no_clarifying", "ok": ok3c})

    # Eval 4: TOOL_ERROR sets phase=error, state=retryable
    trip_intent = {"working_memory": {}, "status": {}, "itinerary": {}, "party": {}}
    out = reducer.run({
        "trip_intent": trip_intent,
        "event": {
            "type": "TOOL_ERROR",
            "data": {"tool_name": "flight_quote_search", "error": "API timeout"},
        },
    })
    status = out["output"]["trip_intent"].get("status", {})
    ok4 = status.get("phase") == "error" and status.get("state") == "retryable"
    results.append({"eval": "TOOL_ERROR_sets_retryable", "ok": ok4})

    passed = sum(1 for r in results if r["ok"])
    return {"passed": passed, "total": len(results), "results": results}


def main() -> int:
    _ensure_path()
    print("Reducer evals\n" + "=" * 40)

    # 1. Confirmation evals (programmatic)
    print("\n1. Confirmation detection (programmatic)")
    r = run_confirmation_evals(use_llm=False)
    if r.get("skipped"):
        print(f"   Skipped: {r.get('reason', 'unknown')}")
    else:
        print(f"   Passed: {r['passed']}/{r['total']}")
        for res in r["results"]:
            if not res["ok"]:
                print(f"   FAIL: {res['description']!r} | msg={res['user_message']!r} | expected={res['expected']} got={res['got']}")

    # 2. Confirmation evals (LLM) - optional
    print("\n2. Confirmation detection (LLM)")
    r_llm = run_confirmation_evals(use_llm=True)
    if r_llm.get("skipped"):
        print(f"   Skipped: {r_llm.get('reason', 'unknown')}")
    else:
        print(f"   Passed: {r_llm['passed']}/{r_llm['total']}")
        for res in r_llm["results"]:
            if not res["ok"]:
                print(f"   FAIL: {res['description']!r} | msg={res['user_message']!r} | expected={res['expected']} got={res['got']}")

    # 3. Full reducer flow evals
    print("\n3. Full reducer flow")
    r_flow = run_full_reducer_evals()
    print(f"   Passed: {r_flow['passed']}/{r_flow['total']}")
    for res in r_flow["results"]:
        status = "ok" if res["ok"] else "FAIL"
        print(f"   {status}: {res['eval']}")

    all_ok = (
        (r.get("skipped") or r["failed"] == 0)
        and (r_llm.get("skipped") or r_llm["failed"] == 0)
        and r_flow["passed"] == r_flow["total"]
    )
    print("\n" + ("All evals passed." if all_ok else "Some evals failed."))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
