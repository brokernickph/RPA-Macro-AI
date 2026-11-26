"""Copy the pmc_mcp package next to mcp.json so `uipath pack` bundles it.

Run before `uipath init` / `uipath pack`:  python prepare.py
"""

import shutil
from pathlib import Path

here = Path(__file__).resolve().parent
src = here.parents[1] / "pmc_mcp"
dst = here / "pmc_mcp"
if dst.exists():
    shutil.rmtree(dst)
shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "board"))
print(f"copied {src} -> {dst}")
