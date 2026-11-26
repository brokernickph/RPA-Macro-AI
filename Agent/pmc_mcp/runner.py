"""Process helpers: launch PMC, run macros headless, run small AHK v1 scripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import pmcfile
from .config import Config
from .steps import inject_io


class RunnerError(RuntimeError):
    pass


def pmc_command(cfg: Config, *args: str) -> list[str]:
    if not cfg.pmc:
        raise RunnerError("PMC_EXE is not set: point it at MacroCreator.exe (or MacroCreator.ahk)")
    if cfg.pmc.lower().endswith(".ahk"):
        if not cfg.ahk:
            raise RunnerError("running MacroCreator.ahk needs AutoHotkey v1.1: set PMC_AHK_EXE")
        return [cfg.ahk, cfg.pmc, *args]
    return [cfg.pmc, *args]


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_macro_headless(
    cfg: Config,
    path: Path,
    macro: int | str | None,
    inputs: dict[str, Any],
    outputs: list[str],
    timeout: float,
) -> dict[str, Any]:
    """Run one macro of a .pmc in a fresh hidden PMC instance (-new -s<n>) and wait.

    Inputs are injected as variable assignments at the top of the macro; outputs
    are written back to files by rows appended at the end.
    """
    project = pmcfile.load(path)
    index, m = project.find(macro)
    if m.is_function:
        raise RunnerError(f"macro {m.name!r} is a function and cannot be run directly")
    work = Path(tempfile.mkdtemp(prefix="pmc_run_"))
    out_dir = work / "out"
    out_dir.mkdir()
    inject_io(m, inputs, outputs, str(out_dir))
    run_file = work / path.name
    pmcfile.dump(project, run_file)

    cmd = pmc_command(cfg, str(run_file), "-new", f"-s{index}")
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=str(path.parent), creationflags=_no_window())
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        proc.wait(10)
    values = {}
    for name in outputs:
        f = out_dir / f"{name}.txt"
        values[name] = f.read_text(encoding="utf-8-sig") if f.exists() else None
    completed = (out_dir / "__done__.txt").exists()
    return {
        "macro": {"index": index, "name": m.name},
        "completed": completed,
        "timed_out": timed_out,
        "exit_code": proc.returncode,
        "seconds": round(time.monotonic() - start, 2),
        "outputs": values,
        "note": None if completed else (
            "the macro did not reach its last step (it was stopped, timed out, returned early, or failed)"
        ),
    }


def launch_pmc(cfg: Config, file: Path | None = None) -> subprocess.Popen:
    args = ["-agent"]
    if file:
        args.insert(0, str(file))
    return subprocess.Popen(pmc_command(cfg, *args))


# ---- AHK v1 snippets for desktop primitives ---------------------------------

AHK_PRELUDE = "\n".join([
    "#NoEnv",
    "#NoTrayIcon",
    "#SingleInstance Off",
    "SetBatchLines, -1",
    "SetTitleMatchMode, 2",
    "SetWorkingDir, %A_ScriptDir%",
    "FileEncoding, UTF-8",
    "_q(s) {",  # JSON-quote a string
    "    q := Chr(34), bs := Chr(92)",
    "    s := StrReplace(s, bs, bs bs)",
    "    s := StrReplace(s, q, bs q)",
    "    s := StrReplace(s, \"`n\", bs \"n\")",
    "    s := StrReplace(s, \"`r\", bs \"r\")",
    "    s := StrReplace(s, \"`t\", bs \"t\")",
    "    return q s q",
    "}",
    "_out(json) {",  # write the JSON result to stdout
    "    FileAppend, %json%, *, UTF-8",
    "}",
]) + "\n"


def ahk_string(value: str) -> str:
    """Quote a Python string as an AHK v1 expression string literal."""
    v = str(value).replace("`", "``").replace('"', '""')
    v = v.replace("\r", "`r").replace("\n", "`n").replace("\t", "`t")
    return f'"{v}"'


def run_ahk(cfg: Config, body: str, timeout: float = 30.0) -> dict[str, Any]:
    """Run an AHK v1 script. JSON written with _out() is parsed and returned."""
    if not cfg.ahk:
        raise RunnerError(
            "AutoHotkey v1.1 was not found. Install it from autohotkey.com or set PMC_AHK_EXE to AutoHotkeyU64.exe"
        )
    fd, script = tempfile.mkstemp(prefix="pmc_mcp_", suffix=".ahk")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(AHK_PRELUDE + "\n" + body + "\nExitApp\n")
    try:
        p = subprocess.run(
            [cfg.ahk, "/ErrorStdOut", script],
            capture_output=True,
            timeout=timeout,
            creationflags=_no_window(),
        )
    except subprocess.TimeoutExpired:
        raise RunnerError(f"AutoHotkey script timed out after {timeout}s") from None
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass
    out = p.stdout.decode("utf-8-sig", "replace").strip()
    err = p.stderr.decode("utf-8", "replace").strip()
    if p.returncode != 0 or err:
        raise RunnerError(f"AutoHotkey error (exit {p.returncode}): {err or out}")
    if not out:
        return {}
    try:
        return json.loads(out)
    except ValueError:
        return {"output": out}
