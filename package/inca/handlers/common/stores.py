# travel_v1/common/stores.py
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Protocol


class TripIntentStore(Protocol):
    """
    Persistence for TripIntent documents.
    Implementations can be DynamoDB, Mongo, Postgres JSONB, etc.
    """

    def get(self, trip_id: str) -> Optional[Dict[str, Any]]:
        ...

    def save(self, trip_id: str, doc: Dict[str, Any]) -> None:
        ...


class ToolDefinitionsStore(Protocol):
    """
    Persistence for:
      - OpenAI tools array (function/tool definitions)
      - system prompt
      - developer prompt
    """

    def get_tools(self, registry_key: str) -> List[Dict[str, Any]]:
        ...

    def get_system_prompt(self, registry_key: str) -> str:
        ...

    def get_developer_prompt(self, registry_key: str) -> str:
        ...


# -----------------------------------------------------------------------------
# DataController-backed implementation (production: save trip to DB)
# -----------------------------------------------------------------------------

class DataControllerTripStore(TripIntentStore):
    """
    TripIntent persistence via DataController (DAC): get_a_b_c / put_a_b_c.
    Ring is 'noma_travels'. portfolio and org are fixed per store instance.
    """

    RING = "inca_intents"

    def __init__(self, dac: Any, portfolio: str, org: str) -> None:
        self._dac = dac
        self._portfolio = portfolio
        self._org = org

    def get(self, trip_id: str) -> Optional[Dict[str, Any]]:
        response = self._dac.get_a_b_c(self._portfolio, self._org, self.RING, trip_id)
        if isinstance(response, dict) and "error" in response:
            return None
        return copy.deepcopy(response) if isinstance(response, dict) else None

    def save(self, trip_id: str, doc: Dict[str, Any]) -> None:
        self._dac.put_a_b_c(self._portfolio, self._org, self.RING, trip_id, doc)


# -----------------------------------------------------------------------------
# In-memory implementations (useful for local tests / demos)
# -----------------------------------------------------------------------------

class InMemoryTripStore(TripIntentStore):
    def __init__(self) -> None:
        self._db: Dict[str, Dict[str, Any]] = {}

    def get(self, trip_id: str) -> Optional[Dict[str, Any]]:
        doc = self._db.get(trip_id)
        return copy.deepcopy(doc) if doc else None

    def save(self, trip_id: str, doc: Dict[str, Any]) -> None:
        self._db[trip_id] = copy.deepcopy(doc)


class InMemoryToolStore(ToolDefinitionsStore):
    """
    Simple in-memory tool registry.

    In production, you likely store these documents in a doc DB, versioned by registry_key:
      - tools: list[dict]
      - system_prompt: str
      - developer_prompt: str
    """

    def __init__(
        self,
        *,
        tools: List[Dict[str, Any]],
        system_prompt: str,
        developer_prompt: str,
    ) -> None:
        self._tools = copy.deepcopy(tools)
        self._system_prompt = system_prompt
        self._developer_prompt = developer_prompt

    def get_tools(self, registry_key: str) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._tools)

    def get_system_prompt(self, registry_key: str) -> str:
        return self._system_prompt

    def get_developer_prompt(self, registry_key: str) -> str:
        return self._developer_prompt
