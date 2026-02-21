#!/usr/bin/env python3
"""
Run reducer evals (confirmation detection, full flow, corner cases).

Usage:
  python run_reducer_evals.py
  python -m extensions.inca.run_reducer_evals
"""
from __future__ import annotations

import os
import sys

def _ensure_path() -> None:
    _dir = os.path.dirname(os.path.abspath(__file__))
    if _dir not in sys.path:
        sys.path.insert(0, _dir)
    pkg_dir = os.path.join(_dir, "package")
    if os.path.isdir(pkg_dir) and pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)

if __name__ == "__main__":
    _ensure_path()
    from inca.handlers.evals.reducer_evals import main
    sys.exit(main())
