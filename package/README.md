# Inca Package

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
