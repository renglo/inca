#!/usr/bin/env python3
"""
Run all handler batteries. No extra files in handlers/ — each handler defines its own run_tests().

Usage (from repo root or extensions/inca):
  python -m extensions.inca.run_handler_tests
  # or from extensions/inca:
  python run_handler_tests.py
"""
from __future__ import annotations

import os
import sys

def _ensure_path() -> None:
    _dir = os.path.dirname(os.path.abspath(__file__))
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

def main() -> int:
    _ensure_path()
    from handlers.patcher import Patcher
    from handlers.applier import Applier
    from handlers.reducer import Reducer
    from handlers.runner import Runner
    from handlers.sprinter import Sprinter
    from handlers.tools import Tools

    ran = 0
    failed = []
    for name, run_tests in [
        ("Patcher", Patcher.run_tests),
        ("Applier", Applier.run_tests),
        ("Reducer", Reducer.run_tests),
        ("Tools", Tools.run_tests),
        ("Runner", Runner.run_tests),
        ("Sprinter", Sprinter.run_tests),
    ]:
        try:
            run_tests()
            ran += 1
            print(f"  {name}: ok")
        except AssertionError as e:
            failed.append((name, e))
            print(f"  {name}: FAIL — {e}")
        except Exception as e:
            failed.append((name, e))
            print(f"  {name}: ERROR — {e}")

    if failed:
        print(f"\n{len(failed)} failed, {ran} passed")
        return 1
    print(f"\nAll {ran} handler batteries passed.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
