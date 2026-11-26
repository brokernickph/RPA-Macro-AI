"""`pmc-run`: run a PMC macro from any RPA platform, scheduler or script.

    pmc-run invoice.pmc --macro Export --input InvoiceNo=INV-42 --output ExportPath
    pmc-run invoice.pmc --json '{"macro": "Export", "inputs": {"InvoiceNo": "INV-42"}, "outputs": ["ExportPath"]}'

Prints one JSON object (completed, timed_out, exit_code, seconds, outputs, ...) and
optionally writes it to --result-file, for tools that can't read console output
(e.g. an Automation Anywhere "Open program/file" step). Exit codes:
0 = macro reached its last step, 2 = did not complete, 3 = timed out, 1 = error.

For platforms that call a Python function with one string (e.g. Automation
Anywhere's Python Script package), use ``run_json(request_json) -> result_json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import runner
from .config import Config

EXIT_OK, EXIT_ERROR, EXIT_INCOMPLETE, EXIT_TIMEOUT = 0, 1, 2, 3


def _cfg_for(path: str) -> tuple[Config, Path]:
    cfg = Config.from_env()
    cfg.require("standard")
    p = Path(path).expanduser().resolve()
    # The caller named this file explicitly, so its folder is allowed for this run.
    if not any(p.parent == r or r in p.parent.parents for r in cfg.roots):
        cfg.roots.append(p.parent)
    return cfg, cfg.safe_path(str(p), must_exist=True)


def run(path: str, macro: str | int | None = None, inputs: dict[str, Any] | None = None,
        outputs: list[str] | None = None, timeout_s: float | None = None) -> dict[str, Any]:
    cfg, p = _cfg_for(path)
    cfg.audit("pmc_run_cli", path=str(p), macro=macro, inputs=sorted((inputs or {}).keys()))
    return runner.run_macro_headless(cfg, p, macro or None, {k: "" if v is None else str(v) for k, v in (inputs or {}).items()},
                                     list(outputs or []), float(timeout_s or cfg.default_timeout))


def run_json(request: str) -> str:
    """One string in, one string out: {"path", "macro"?, "inputs"?, "outputs"?, "timeout_s"?} -> result JSON.

    Never raises: errors come back as {"ok": false, "error": "..."}.
    """
    try:
        req = json.loads(request)
        if not isinstance(req, dict) or not req.get("path"):
            raise ValueError('request must be a JSON object with at least "path"')
        res = run(req["path"], req.get("macro"), req.get("inputs"), req.get("outputs"), req.get("timeout_s"))
        return json.dumps({"ok": bool(res["completed"]), **res})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})


def _kv(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--input expects Name=value, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pmc-run", description="Run a Pulover's Macro Creator macro headless and print the result as JSON.")
    ap.add_argument("file", help=".pmc project")
    ap.add_argument("--macro", "-m", help="macro name or 1-based index (default: first)")
    ap.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE", help="input variable (repeatable)")
    ap.add_argument("--output", "-o", action="append", default=[], metavar="NAME", help="output variable to return (repeatable)")
    ap.add_argument("--json", dest="json_req", metavar="JSON", help="macro/inputs/outputs/timeout_s as one JSON object")
    ap.add_argument("--timeout", type=float, help="seconds before the macro is stopped")
    ap.add_argument("--result-file", help="also write the JSON result to this file")
    a = ap.parse_args(argv)

    macro, inputs, outputs, timeout = a.macro, _kv(a.input), list(a.output), a.timeout
    if a.json_req:
        extra = json.loads(a.json_req)
        macro = extra.get("macro", macro)
        inputs = {**inputs, **(extra.get("inputs") or {})}
        outputs = outputs + list(extra.get("outputs") or [])
        timeout = extra.get("timeout_s", timeout)
    try:
        res = run(a.file, macro, inputs, outputs, timeout)
        result, code = {"ok": bool(res["completed"]), **res}, (
            EXIT_OK if res["completed"] else EXIT_TIMEOUT if res["timed_out"] else EXIT_INCOMPLETE)
    except Exception as e:  # noqa: BLE001
        result, code = {"ok": False, "error": f"{type(e).__name__}: {e}"}, EXIT_ERROR
    text = json.dumps(result, indent=2)
    print(text)
    if a.result_file:
        Path(a.result_file).write_text(text, encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
