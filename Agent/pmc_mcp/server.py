"""MCP server that turns Pulover's Macro Creator into an RPA backend for AI agents.

Tool groups
  * macro files  - list / read / create / edit .pmc projects (pure Python, any OS)
  * execution    - run a macro headless with input variables and get outputs back
  * live PMC     - drive a running PMC through LIB/AgentBridge.ahk (play, stop, record)
  * desktop      - screenshot, windows, mouse, keyboard, image/pixel search (Windows)
"""

from __future__ import annotations

import functools
import io
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import anyio

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer, Image  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]
try:
    from mcp_types import ToolAnnotations
except ImportError:
    from mcp.types import ToolAnnotations  # type: ignore[no-redef]

from . import pmcfile, runner
from . import steps as stepkit
from .bridge import BridgeClient, BridgeError
from .config import Config

INSTRUCTIONS = """\
Pulover's Macro Creator (PMC) RPA server. Typical workflow:
1. Observe: desktop_screenshot / desktop_list_windows to see the screen.
2. Build: macro_create / macro_add_steps write a .pmc macro (see macro_step_kinds), or
   pmc_record_start / pmc_record_stop capture a human demonstration in the PMC app.
3. Execute: macro_run runs a macro headless with input variables and returns output
   variables; pmc_play plays it in the visible PMC app.
4. Verify with another screenshot. Use pmc_stop to abort a running macro.
Prefer window-relative targets (window titles, controls) over raw screen coordinates.
Files are confined to the configured macro directories; relative paths resolve there.
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


def create_server(cfg: Config | None = None, bridge: BridgeClient | None = None) -> MCPServer:
    cfg = cfg or Config.from_env()
    bridge = bridge or BridgeClient()
    mcp = MCPServer("pmc-rpa", instructions=INSTRUCTIONS)

    def tool(level: str, annotations: ToolAnnotations, audit: bool = True):
        """Register an async tool that checks the permission level, audits, and runs blocking work in a thread."""

        def deco(fn: Callable[..., Any]):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                try:
                    cfg.require(level)
                    if audit and level != "readonly":
                        cfg.audit(fn.__name__, args=kwargs)
                    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
                except ToolError:
                    raise
                except Exception as e:  # surface a readable message to the agent
                    raise ToolError(f"{type(e).__name__}: {e}") from e

            return mcp.tool(annotations=annotations)(wrapper)

        return deco

    # ======================================================================
    # Macro files
    # ======================================================================

    @tool("readonly", READ)
    def macro_list_files(directory: str = "") -> dict:
        """List .pmc macro projects in the allowed macro directories (or one sub-directory)."""
        bases = [cfg.safe_path(directory, suffix=None)] if directory else cfg.roots
        files = []
        for base in bases:
            if base.is_dir():
                for p in sorted(base.rglob("*.pmc")):
                    try:
                        proj = pmcfile.load(p)
                        names = [m.name for m in proj.macros]
                    except Exception as e:  # noqa: BLE001
                        names = [f"<unreadable: {e}>"]
                    files.append({"path": str(p), "macros": names, "modified": time.ctime(p.stat().st_mtime)})
        return {"roots": [str(r) for r in cfg.roots], "files": files}

    @tool("readonly", READ)
    def macro_read(path: str, macro: str = "", include_steps: bool = True) -> dict:
        """Read a .pmc project. macro = index (1-based) or name to return just one macro."""
        proj = pmcfile.load(cfg.safe_path(path, must_exist=True))
        if macro:
            i, m = proj.find(macro)
            d = m.to_dict(i)
            if not include_steps:
                d.pop("steps")
            return d
        if not include_steps:
            return {"macros": [m.summary(i) for i, m in enumerate(proj.macros, 1)]}
        return proj.to_dict()

    @tool("readonly", READ, audit=False)
    def macro_step_kinds() -> str:
        """Describe the step specs accepted by macro_create and macro_add_steps."""
        return stepkit.STEP_KINDS_DOC

    @tool("standard", EDIT)
    def macro_create(path: str, macros: list[dict], overwrite: bool = False) -> dict:
        """Create a .pmc project.

        macros: [{"name": "Login", "hotkey": "", "repeat": 1, "steps": [<step spec>, ...]}, ...]
        Step specs are documented by macro_step_kinds. Use a name ending in "()" for a function.
        """
        p = cfg.safe_path(path)
        if p.exists() and not overwrite:
            raise FileExistsError(f"{p} exists; pass overwrite=true to replace it")
        if not macros:
            raise ValueError("at least one macro is required")
        proj = pmcfile.Project()
        for i, spec in enumerate(macros, 1):
            m = pmcfile.Macro(
                name=str(spec.get("name") or f"Macro{i}"),
                hotkey=str(spec.get("hotkey", "")),
                manual_key=str(spec.get("manual_key", "")),
                repeat=str(spec.get("repeat", 1)),
                groups="",
                context=None,
            )
            m.steps = [stepkit.build(s) for s in spec.get("steps", [])]
            proj.macros.append(m)
        p.parent.mkdir(parents=True, exist_ok=True)
        pmcfile.dump(proj, p)
        return {"path": str(p), "macros": [m.summary(i) for i, m in enumerate(proj.macros, 1)]}

    @tool("standard", EDIT)
    def macro_add_steps(path: str, steps: list[dict], macro: str = "", position: int = 0) -> dict:
        """Insert steps into a macro. position = 1-based row to insert before; 0 = append."""
        p = cfg.safe_path(path, must_exist=True)
        proj = pmcfile.load(p)
        i, m = proj.find(macro or None)
        new = [stepkit.build(s) for s in steps]
        at = len(m.steps) if position <= 0 else min(position - 1, len(m.steps))
        m.steps[at:at] = new
        pmcfile.dump(proj, p)
        return {"macro": i, "inserted_at": at + 1, "count": len(new), "total_steps": len(m.steps)}

    @tool("standard", EDIT)
    def macro_update_step(path: str, row: int, fields: dict, macro: str = "") -> dict:
        """Change fields of one step (row is 1-based). fields: any of action, details, repeat,
        delay, type, target, window, comment, color, enabled."""
        p = cfg.safe_path(path, must_exist=True)
        proj = pmcfile.load(p)
        i, m = proj.find(macro or None)
        if not 1 <= row <= len(m.steps):
            raise IndexError(f"row {row} out of range 1-{len(m.steps)}")
        step = m.steps[row - 1]
        merged = {**step.to_dict(), **fields}
        new = pmcfile.Step.from_dict(merged)
        new._width = step._width
        m.steps[row - 1] = new
        pmcfile.dump(proj, p)
        return {"macro": i, "row": row, "step": new.to_dict()}

    @tool("standard", EDIT)
    def macro_delete_steps(path: str, rows: list[int], macro: str = "") -> dict:
        """Delete steps by 1-based row numbers."""
        p = cfg.safe_path(path, must_exist=True)
        proj = pmcfile.load(p)
        i, m = proj.find(macro or None)
        bad = [r for r in rows if not 1 <= r <= len(m.steps)]
        if bad:
            raise IndexError(f"rows out of range 1-{len(m.steps)}: {bad}")
        for r in sorted(set(rows), reverse=True):
            del m.steps[r - 1]
        pmcfile.dump(proj, p)
        return {"macro": i, "deleted": len(set(rows)), "total_steps": len(m.steps)}

    # ======================================================================
    # Execution
    # ======================================================================

    @tool("standard", ACT)
    def macro_run(
        path: str,
        macro: str = "",
        inputs: dict[str, str] | None = None,
        outputs: list[str] | None = None,
        timeout_s: int = 0,
    ) -> dict:
        """Run one macro headless in a separate hidden PMC instance and wait for it to finish.

        inputs: variables set before the first step, e.g. {"UserName": "alice"} (literal text).
        outputs: variable names whose final values are returned, e.g. ["OrderId"].
        The macro really controls this computer's mouse/keyboard while it runs.
        """
        p = cfg.safe_path(path, must_exist=True)
        return runner.run_macro_headless(
            cfg, p, macro or None, inputs or {}, outputs or [], timeout_s or cfg.default_timeout
        )

    # ======================================================================
    # Live PMC (AgentBridge)
    # ======================================================================

    @tool("readonly", READ)
    def pmc_status() -> dict:
        """State of the running PMC app: open file, macros, playing/recording flags."""
        info = bridge.info()
        if not info:
            return {"running": False, "hint": "call pmc_launch to start PMC with the agent bridge"}
        return {"running": True, "bridge": info, **bridge.call("status")}

    @tool("standard", ACT)
    def pmc_launch(path: str = "", timeout_s: int = 30) -> dict:
        """Start PMC with the agent bridge enabled (optionally opening a .pmc)."""
        if bridge.available():
            return {"already_running": True, **bridge.call("status")}
        f = cfg.safe_path(path, must_exist=True) if path else None
        proc = runner.launch_pmc(cfg, f)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if bridge.available():
                return {"started": True, **bridge.call("status")}
            if proc.poll() is not None:
                raise BridgeError(
                    "PMC exited right away. It is probably already running without the agent bridge: "
                    "close it, or set [Options] AgentBridge=1 in MacroCreator.ini and restart it."
                )
            time.sleep(0.25)
        raise BridgeError(f"PMC did not start its agent bridge within {timeout_s}s")

    @tool("standard", EDIT)
    def pmc_open(path: str, discard_changes: bool = False) -> dict:
        """Open a .pmc project in the running PMC app (replaces the current project)."""
        return bridge.call("open", path=str(cfg.safe_path(path, must_exist=True)), discard_changes=discard_changes)

    @tool("standard", EDIT)
    def pmc_save(path: str = "") -> dict:
        """Save the project open in PMC (to path, or to its current file)."""
        return bridge.call("save", path=str(cfg.safe_path(path)) if path else None)

    @tool("readonly", READ)
    def pmc_get_live_macro(macro: str = "") -> dict:
        """Steps of a macro as they currently are in the PMC editor (including unsaved edits)."""
        res = bridge.call("get_macro", macro=_macro_arg(macro))
        rows = [_row_to_step(r) for r in res.get("rows") or []]
        return {"macro": res.get("macro"), "name": res.get("name"), "steps": rows}

    @tool("standard", ACT)
    def pmc_play(macro: str = "", wait: bool = True, timeout_s: int = 0) -> dict:
        """Play a macro in the PMC app. With wait=true, block until it finishes (or timeout, then stop)."""
        res = bridge.call("play", macro=_macro_arg(macro))
        if not wait:
            return res
        timeout = timeout_s or cfg.default_timeout
        start = time.monotonic()
        seen_playing = False
        while time.monotonic() - start < timeout:
            st = bridge.call("status")
            if st.get("playing"):
                seen_playing = True
            elif seen_playing or time.monotonic() - start > 3:
                return {"finished": True, "seconds": round(time.monotonic() - start, 2), "macro": res.get("macro")}
            time.sleep(0.3)
        bridge.call("stop")
        return {"finished": False, "timed_out": True, "stopped": True, "seconds": timeout}

    @tool("standard", ACT)
    def pmc_stop() -> dict:
        """Abort playback or recording in the PMC app immediately."""
        return bridge.call("stop")

    @tool("standard", ACT)
    def pmc_record_start(macro: str = "") -> dict:
        """Start recording the user's mouse/keyboard into a macro in the PMC app."""
        return bridge.call("record_start", macro=_macro_arg(macro))

    @tool("standard", EDIT)
    def pmc_record_stop() -> dict:
        """Stop recording and return the recorded steps."""
        res = bridge.call("record_stop")
        live = bridge.call("get_macro", macro=res.get("macro"))
        return {**res, "steps": [_row_to_step(r) for r in live.get("rows") or []]}

    @tool("standard", EDIT)
    def pmc_window(visible: bool) -> dict:
        """Show or hide the PMC main window."""
        return bridge.call("show" if visible else "hide")

    # ======================================================================
    # Desktop primitives (Windows)
    # ======================================================================

    @tool("readonly", READ, audit=False)
    def desktop_screenshot(window: str = "", x: int = 0, y: int = 0, width: int = 0, height: int = 0, max_width: int = 1600) -> list:
        """Screenshot the whole desktop, a window (title / ahk_exe x.exe), or a screen region.

        The image may be downscaled; the returned 'scale' maps image pixels to screen pixels:
        screen_x = origin_x + image_x * scale.
        """
        try:
            from PIL import ImageGrab
        except ImportError:
            raise RuntimeError("desktop_screenshot needs Pillow: pip install pillow") from None
        bbox = None
        if window:
            r = runner.run_ahk(cfg, f"""
                t := {runner.ahk_string(window)}
                WinGetPos, x, y, w, h, %t%
                if (w = "")
                    _out("{{}}")
                else
                    _out("{{""x"":" x ",""y"":" y ",""w"":" w ",""h"":" h "}}")
            """)
            if not r:
                raise LookupError(f"window not found: {window}")
            bbox = (r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"])
        elif width and height:
            bbox = (x, y, x + width, y + height)
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
        scale = 1.0
        if max_width and img.width > max_width:
            scale = img.width / max_width
            img = img.resize((max_width, round(img.height / scale)))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        origin = (bbox[0], bbox[1]) if bbox else ("virtual-screen origin (0,0 on single-monitor setups)")
        return [
            Image(data=buf.getvalue(), format="png"),
            f"origin={origin} scale={scale:.4f} size={img.width}x{img.height}",
        ]

    @tool("readonly", READ, audit=False)
    def desktop_list_windows() -> dict:
        """Visible top-level windows with id, title, class, process, position and state."""
        return runner.run_ahk(cfg, r"""
            WinGet, ids, List
            s := ""
            Loop, %ids%
            {
                id := ids%A_Index%
                WinGetTitle, t, ahk_id %id%
                if (t = "")
                    continue
                WinGetClass, c, ahk_id %id%
                WinGet, exe, ProcessName, ahk_id %id%
                WinGet, mm, MinMax, ahk_id %id%
                WinGetPos, x, y, w, h, ahk_id %id%
                s .= (s = "" ? "" : ",") "{""id"":" (id + 0) ",""title"":" _q(t) ",""class"":" _q(c)
                    . ",""process"":" _q(exe) ",""state"":" mm ",""x"":" x ",""y"":" y ",""w"":" w ",""h"":" h "}"
            }
            WinGet, active, ID, A
            _out("{""active"":" (active + 0) ",""windows"":[" s "]}")
        """)

    @tool("standard", ACT)
    def desktop_window(window: str, action: str, x: int = 0, y: int = 0, width: int = 0, height: int = 0, timeout_s: int = 5) -> dict:
        """Act on a window: activate | minimize | maximize | restore | close | move | wait.
        window: title substring, "ahk_exe app.exe", "ahk_class X" or "ahk_id <id>"."""
        cmds = {
            "activate": "WinActivate, %t%\nWinWaitActive, %t%,, " + str(timeout_s),
            "minimize": "WinMinimize, %t%",
            "maximize": "WinMaximize, %t%",
            "restore": "WinRestore, %t%",
            "close": "WinClose, %t%,, " + str(timeout_s),
            "move": f"WinMove, %t%,, {x}, {y}, {width or ''}, {height or ''}",
            "wait": "WinWait, %t%,, " + str(timeout_s),
        }
        if action not in cmds:
            raise ValueError(f"action must be one of {sorted(cmds)}")
        return runner.run_ahk(cfg, f"""
            t := {runner.ahk_string(window)}
            IfWinNotExist, %t%
            {{
                if ("{action}" != "wait")
                {{
                    _out("{{""ok"":false,""error"":""window not found""}}")
                    ExitApp
                }}
            }}
            {cmds[action]}
            _out("{{""ok"":" (ErrorLevel ? "false" : "true") "}}")
        """, timeout=timeout_s + 10)

    @tool("standard", ACT)
    def desktop_click(x: int, y: int, button: str = "Left", count: int = 1, relative_to: str = "screen", window: str = "") -> dict:
        """Click at coordinates. relative_to: screen | window | client (window/client need `window`,
        which is activated first)."""
        modes = {"screen": "Screen", "window": "Window", "client": "Client"}
        if relative_to.lower() not in modes:
            raise ValueError(f"relative_to must be one of {sorted(modes)}")
        mode = modes[relative_to.lower()]
        b = stepkit._button(button)
        activate = ""
        if window:
            activate = f"t := {runner.ahk_string(window)}\nWinActivate, %t%\nWinWaitActive, %t%,, 3\n"
        elif mode != "Screen":
            raise ValueError("relative_to window/client requires window")
        return runner.run_ahk(cfg, f"""
            {activate}
            CoordMode, Mouse, {mode}
            Click, {int(x)}, {int(y)}, {b}, {int(count)}
            _out("{{""ok"":true}}")
        """)

    @tool("standard", ACT)
    def desktop_send_keys(keys: str = "", text: str = "", window: str = "") -> dict:
        """Send keystrokes (AHK syntax in `keys`, e.g. "^s", "{Enter}", "!{F4}") and/or literal `text`.
        If window is given it is activated first."""
        if not keys and not text:
            raise ValueError("give keys and/or text")
        body = ""
        if window:
            body += f"t := {runner.ahk_string(window)}\nWinActivate, %t%\nWinWaitActive, %t%,, 3\nif ErrorLevel\n{{\n_out(\"{{\"\"ok\"\":false,\"\"error\"\":\"\"window not active\"\"}}\")\nExitApp\n}}\n"
        if text:
            body += f"_txt := {runner.ahk_string(text)}\nSendInput, {{Text}}%_txt%\n"
        if keys:
            body += f"_k := {runner.ahk_string(keys)}\nSendInput, %_k%\n"
        body += '_out("{""ok"":true}")'
        return runner.run_ahk(cfg, body)

    @tool("readonly", READ, audit=False)
    def desktop_pixel_color(x: int, y: int) -> dict:
        """RGB colour (0xRRGGBB) of a screen pixel."""
        return runner.run_ahk(cfg, f"""
            CoordMode, Pixel, Screen
            PixelGetColor, c, {int(x)}, {int(y)}, RGB
            _out("{{""color"":" _q(c) "}}")
        """)

    @tool("readonly", READ, audit=False)
    def desktop_image_search(image_path: str, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0, variation: int = 30, center: bool = True) -> dict:
        """Find an image (PNG/BMP) on screen. Region defaults to the whole virtual screen.
        Returns screen coordinates (top-left, or center if center=true)."""
        img = cfg.safe_path(image_path, must_exist=True, suffix=None)
        region = (
            f"x1 := {int(x1)}, y1 := {int(y1)}, x2 := {int(x2)}, y2 := {int(y2)}"
            if (x2 and y2)
            else "SysGet, x1, 76\nSysGet, y1, 77\nSysGet, vw, 78\nSysGet, vh, 79\nx2 := x1 + vw, y2 := y1 + vh"
        )
        return runner.run_ahk(cfg, f"""
            CoordMode, Pixel, Screen
            {region}
            f := {runner.ahk_string(str(img))}
            ImageSearch, fx, fy, %x1%, %y1%, %x2%, %y2%, *{int(variation)} %f%
            if (ErrorLevel = 2)
                _out("{{""found"":false,""error"":""could not search (bad image file?)""}}")
            else if (ErrorLevel = 1)
                _out("{{""found"":false}}")
            else
            {{
                if ({1 if center else 0})
                {{
                    Gui, Add, Picture, hwndh, %f%
                    GuiControlGet, _p, Pos, %h%
                    fx += _pW // 2, fy += _pH // 2
                }}
                _out("{{""found"":true,""x"":" fx ",""y"":" fy "}}")
            }}
        """)

    # ======================================================================
    # Visual board
    # ======================================================================

    board_state: dict[str, Any] = {}

    @tool("readonly", READ, audit=False)
    def board_open(path: str = "", open_browser: bool = True) -> dict:
        """Open the PMC Visual Board (drag-and-drop flow editor) in the user's browser,
        optionally on a .pmc file, so they can see or edit a macro visually. Returns its URL."""
        from . import board_server

        if "httpd" not in board_state:
            httpd, url = board_server.serve(cfg, open_browser=False, bridge=bridge)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            board_state.update(httpd=httpd, base=url.split("&f=")[0])
        url = board_state["base"]
        if path:
            url += "&f=" + str(cfg.safe_path(path, must_exist=True)).replace("&", "%26")
        if open_browser:
            import webbrowser

            webbrowser.open(url)
        return {"url": url, "note": "local only (127.0.0.1); the link contains an access token, do not share it"}

    @tool("full", ACT)
    def desktop_run_ahk(script: str, timeout_s: int = 30) -> dict:
        """Run an arbitrary AutoHotkey v1.1 script (permission level 'full' only).
        Call _out(jsonText) to return data; _q(str) JSON-quotes a string."""
        return runner.run_ahk(cfg, script, timeout=timeout_s)

    return mcp


def _macro_arg(macro: str) -> Any:
    if macro in ("", None):
        return None
    return int(macro) if str(macro).isdigit() else macro


def _row_to_step(row: list) -> dict:
    """Convert a bridge row [index, action, details, ...] into a step dict."""
    vals = [str(v) for v in row]
    idx = vals[0] if vals else ""
    d = dict(zip(pmcfile.COLUMNS, vals[1:] + [""] * (len(pmcfile.COLUMNS) - len(vals[1:]))))
    d["enabled"] = not idx.startswith("0")
    return d


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
