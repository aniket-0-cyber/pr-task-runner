#!/usr/bin/env python3
"""Entry point. Everything actually lives in runner/ — see runner/README.md.

Kept here so `python3 run.py <command>` works from the folder holding the repo
checkouts, which is where you want to be anyway.
"""

import runpy
import sys
from pathlib import Path

target = Path(__file__).resolve().parent / "runner" / "run.py"
if not target.exists():
    sys.exit(f"runner/run.py is missing (looked in {target.parent})")

sys.argv[0] = str(target)
runpy.run_path(str(target), run_name="__main__")
