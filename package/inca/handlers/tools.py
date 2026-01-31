# travel_v1/tools.py
from __future__ import annotations

from typing import Any, Dict

from .common.types import Handler, RunSpecialistFn, SpecialistToolHandlerReturn, SpecialistToolPayload, SpecialistToolResult


class Tools(Handler):
    """
    Thin wrapper: "tool as handler".

    It calls:
      run_specialist(tool_name, arguments)

    Input payload:
      {
        "run_specialist": callable(tool_name, args)->dict,
        "arguments": dict
      }

    Output:
      {
        "tool_name": str,
        "result": dict
      }

    Note:
      In the v1 runner we can call run_specialist directly and skip these wrappers.
      But these are useful if you want every tool to be invokable uniformly as a handler.
    """

    name: str = "specialist_tool_handler"
    tool_name: str = ""

    def run(self, payload: SpecialistToolPayload | Dict[str, Any]) -> SpecialistToolHandlerReturn:
        run_specialist: RunSpecialistFn = payload["run_specialist"]
        args = payload.get("arguments", {}) or {}
        try:
            result = run_specialist(self.tool_name, args)
            output: SpecialistToolResult = {"tool_name": self.tool_name, "result": result}
            return {"success": True, "input": dict(payload), "output": output, "stack": []}
        except Exception as e:
            output_err: SpecialistToolResult = {"tool_name": self.tool_name, "error": str(e)}
            return {"success": False, "input": dict(payload), "output": output_err, "stack": []}

    @classmethod
    def run_tests(cls) -> bool:
        """Run a minimal battery of tests for Tools handlers. Returns True on success, raises on failure."""
        return _run_tools_tests()


class TripRequirementsExtract(Tools):
    name = "trip_requirements_extract_handler"
    tool_name = "trip_requirements_extract"


class FlightQuoteSearch(Tools):
    name = "flight_quote_search_handler"
    tool_name = "flight_quote_search"


class HotelQuoteSearch(Tools):
    name = "hotel_quote_search_handler"
    tool_name = "hotel_quote_search"


class TripOptionRanker(Tools):
    name = "trip_option_ranker_handler"
    tool_name = "trip_option_ranker"


class PolicyAndRiskCheck(Tools):
    name = "policy_and_risk_check_handler"
    tool_name = "policy_and_risk_check"


class ReservationHoldCreate(Tools):
    name = "reservation_hold_create_handler"
    tool_name = "reservation_hold_create"


class BookingConfirmAndPurchase(Tools):
    name = "booking_confirm_and_purchase_handler"
    tool_name = "booking_confirm_and_purchase"


def _run_tools_tests() -> bool:
    """Run a minimal battery of tests for Tools handlers. Returns True on success, raises on failure."""
    def mock_specialist(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"options": [{"option_id": "m1"}]}

    payload: SpecialistToolPayload = {"run_specialist": mock_specialist, "arguments": {}}
    h = FlightQuoteSearch()
    out = h.run(payload)
    assert out.get("success") is True
    assert out["output"]["tool_name"] == "flight_quote_search"
    assert out["output"]["result"]["options"][0]["option_id"] == "m1"
    assert out["stack"] == []

    def failing_specialist(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        raise ValueError("simulated failure")
    payload2: SpecialistToolPayload = {"run_specialist": failing_specialist, "arguments": {}}
    out2 = h.run(payload2)
    assert out2.get("success") is False
    assert "error" in out2["output"]
    return True