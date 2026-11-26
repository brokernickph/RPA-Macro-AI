"""MCP server that turns Pulover's Macro Creator into an RPA backend for AI agents.

Tool groups
  * macro files  - list / read / create / edit .pmc projects (pure Python, any OS)
  * execution    - run a macro headless with input variables and get outputs back
  * live PMC     - drive a running PMC through LIB/AgentBridge.ahk (play, stop, record)
  * desktop      - screenshot, windows, mouse, keyboard, image/pixel search (Windows)
"""

from __future__ import annotations

import functools
import threading
import time
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

from . import desktop, engine, pmcfile, runner
from . import steps as stepkit
from .bridge import BridgeClient, BridgeError
from .config import Config

INSTRUCTIONS = """\
RPA Macro AI: browser + desktop automation built on Pulover's Macro Creator (PMC).
Pick the right layer:
- Web pages: browser_open, then browser_snapshot to see the page and element refs, then
  browser_click / browser_fill / browser_extract / browser_table.
- Windows apps: desktop_screenshot / desktop_list_windows to observe, then desktop_click /
  desktop_send_keys / desktop_window. Recorded or hand-built PMC macros (.pmc): macro_* tools,
  macro_run (headless, inputs -> outputs), pmc_record_start / pmc_record_stop to learn from a human.
- Repeatable automations that mix both: flows (.rflow). Call flow_step_kinds, then flow_create,
  flow_run (returns outputs and a step log), and fix with flow_read / flow_update. Flows can call
  PMC macros (run_macro), loop over scraped tables (foreach) and branch (if).
Verify results (screenshot / snapshot) before reporting success. pmc_stop / flow_stop abort runs.
board_open shows a macro or flow to the user in the Visual Board.
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
        png, meta = desktop.screenshot_png(cfg, window, (x, y, width, height) if width and height else None, max_width)
        return [Image(data=png, format="png"), f"origin={meta['origin']} scale={meta['scale']:.4f} size={meta['size']}"]

    @tool("readonly", READ, audit=False)
    def desktop_list_windows() -> dict:
        """Visible top-level windows with id, title, class, process, position and state."""
        return desktop.list_windows(cfg)

    @tool("standard", ACT)
    def desktop_window(window: str, action: str, x: int = 0, y: int = 0, width: int = 0, height: int = 0, timeout_s: int = 5) -> dict:
        """Act on a window: activate | minimize | maximize | restore | close | move | wait.
        window: title substring, "ahk_exe app.exe", "ahk_class X" or "ahk_id <id>"."""
        return desktop.window_action(cfg, window, action, x, y, width, height, timeout_s)

    @tool("standard", ACT)
    def desktop_click(x: int, y: int, button: str = "Left", count: int = 1, relative_to: str = "screen", window: str = "") -> dict:
        """Click at coordinates. relative_to: screen | window | client (window/client need `window`,
        which is activated first)."""
        return desktop.click(cfg, x, y, button, count, relative_to, window)

    @tool("standard", ACT)
    def desktop_send_keys(keys: str = "", text: str = "", window: str = "") -> dict:
        """Send keystrokes (AHK syntax in `keys`, e.g. "^s", "{Enter}", "!{F4}") and/or literal `text`.
        If window is given it is activated first."""
        return desktop.send(cfg, keys, text, window)

    @tool("readonly", READ, audit=False)
    def desktop_pixel_color(x: int, y: int) -> dict:
        """RGB colour (0xRRGGBB) of a screen pixel."""
        return desktop.pixel_color(cfg, x, y)

    @tool("readonly", READ, audit=False)
    def desktop_image_search(image_path: str, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0, variation: int = 30, center: bool = True) -> dict:
        """Find an image (PNG/BMP) on screen. Region defaults to the whole virtual screen.
        Returns screen coordinates (top-left, or center if center=true)."""
        img = cfg.safe_path(image_path, must_exist=True, suffix=None)
        return desktop.image_search(cfg, str(img), x1, y1, x2, y2, variation, center)

    # ======================================================================
    # Browser (live session for the agent)
    # ======================================================================

    live: dict[str, Any] = {}

    def br():
        if "browser" not in live:
            from .browser import BrowserSession

            live["browser"] = BrowserSession(allowed_file_roots=cfg.roots)
        return live["browser"]

    @tool("standard", ACT)
    def browser_open(url: str) -> dict:
        """Open a URL in the automation browser (Edge on Windows by default; logins persist when
        PMC_BROWSER_PROFILE is set). Starts the browser if needed."""
        return br().open(url)

    @tool("readonly", READ, audit=False)
    def browser_snapshot(limit: int = 150) -> dict:
        """Page title, URL, visible text (trimmed) and the interactive elements, each with a `ref`
        (e1, e2, ...) usable in browser_click / browser_fill instead of a selector."""
        return br().snapshot(limit)

    @tool("standard", ACT)
    def browser_click(selector: str = "", ref: str = "", button: str = "left", count: int = 1) -> dict:
        """Click an element by selector (CSS, text=..., role=...[name=...]) or by ref from browser_snapshot."""
        return br().click(selector or None, ref or None, button, count)

    @tool("standard", ACT)
    def browser_fill(text: str, selector: str = "", ref: str = "", submit: bool = False) -> dict:
        """Type into an input/textarea (replacing its value); submit=true presses Enter afterwards."""
        return br().fill(text, selector or None, ref or None, submit)

    @tool("standard", ACT)
    def browser_press(key: str, selector: str = "", ref: str = "") -> dict:
        """Press a key such as Enter, Tab, Control+A (on an element, or the page)."""
        return br().press(key, selector or None, ref or None)

    @tool("standard", ACT)
    def browser_select(value: str, selector: str = "", ref: str = "") -> dict:
        """Choose an option of a <select> by value or label."""
        return br().select(value, selector or None, ref or None)

    @tool("readonly", READ, audit=False)
    def browser_wait(selector: str = "", text: str = "", timeout_s: int = 15) -> dict:
        """Wait until an element (selector) or some text is visible. Returns found true/false."""
        return br().wait(selector or None, text or None, timeout_s)

    @tool("readonly", READ, audit=False)
    def browser_extract(selector: str = "", ref: str = "", attribute: str = "", all: bool = False) -> dict:
        """Read text (or an attribute such as href) of the first, or all, matching elements."""
        return {"value": br().extract(selector or None, ref or None, attribute, all)}

    @tool("readonly", READ, audit=False)
    def browser_table(selector: str = "table") -> dict:
        """Read an HTML table as a list of row records keyed by its header cells."""
        rows = br().table(selector)
        return {"rows": rows, "count": len(rows)}

    @tool("readonly", READ, audit=False)
    def browser_screenshot(full_page: bool = False) -> list:
        """Screenshot of the current page."""
        return [Image(data=br().screenshot(full_page=full_page), format="png"), str(br().info())]

    @tool("full", ACT)
    def browser_evaluate(script: str) -> dict:
        """Run JavaScript in the page and return its result (permission level 'full' only)."""
        return {"result": br().evaluate(script)}

    @tool("standard", ACT, audit=False)
    def browser_close() -> dict:
        """Close the automation browser."""
        if "browser" in live:
            live.pop("browser").close()
        return {"closed": True}

    # ======================================================================
    # Flows (.rflow): browser + desktop + PMC macros in one automation
    # ======================================================================

    @tool("readonly", READ, audit=False)
    def flow_step_kinds() -> str:
        """Describe the steps, blocks and expressions accepted by flow_create / flow_update."""
        return engine.STEP_KINDS_DOC

    @tool("standard", EDIT)
    def flow_create(path: str, flows: list[dict], overwrite: bool = False, name: str = "") -> dict:
        """Create an .rflow automation. flows: [{"name": "Main", "inputs": ["InvoiceNo"],
        "outputs": ["Total"], "steps": [<step>, ...]}]; see flow_step_kinds for steps and blocks."""
        p = cfg.safe_path(path, suffix=engine.SUFFIX)
        if p.exists() and not overwrite:
            raise FileExistsError(f"{p} exists; pass overwrite=true to replace it")
        proj = {"format": engine.FORMAT, "version": 1, "name": name or p.stem, "flows": [
            {"name": f.get("name") or f"Flow{i}", "inputs": list(f.get("inputs") or []), "outputs": list(f.get("outputs") or []),
             "tree": engine.steps_to_tree(f.get("steps") or [])} for i, f in enumerate(flows or [], 1)]}
        if not proj["flows"]:
            raise ValueError("at least one flow is required")
        p.parent.mkdir(parents=True, exist_ok=True)
        engine.dump(proj, p)
        return {"path": str(p), "flows": [{"name": f["name"], "steps": len(f["tree"])} for f in proj["flows"]]}

    @tool("readonly", READ)
    def flow_read(path: str, flow: str = "") -> dict:
        """Read an .rflow automation in the compact step form (or one flow by name/index)."""
        proj = engine.load(cfg.safe_path(path, must_exist=True, suffix=engine.SUFFIX))
        sel = [engine.find_flow(proj, flow)[1]] if flow else proj["flows"]
        return {"name": proj.get("name"), "flows": [
            {"name": f["name"], "inputs": f["inputs"], "outputs": f["outputs"], "steps": engine.tree_to_steps(f["tree"])} for f in sel]}

    @tool("standard", EDIT)
    def flow_update(path: str, steps: list[dict], flow: str = "", inputs: list[str] | None = None,
                    outputs: list[str] | None = None, new_flow: bool = False) -> dict:
        """Replace one flow's steps (read it first with flow_read), or add a flow with new_flow=true."""
        p = cfg.safe_path(path, must_exist=True, suffix=engine.SUFFIX)
        proj = engine.load(p)
        tree = engine.steps_to_tree(steps)
        if new_flow:
            if not flow or any(f["name"] == flow for f in proj["flows"]):
                raise ValueError("new_flow needs a new, unique flow name")
            proj["flows"].append({"name": flow, "inputs": list(inputs or []), "outputs": list(outputs or []), "tree": tree})
            i = len(proj["flows"])
        else:
            i, f = engine.find_flow(proj, flow or None)
            f["tree"] = tree
            if inputs is not None:
                f["inputs"] = list(inputs)
            if outputs is not None:
                f["outputs"] = list(outputs)
        engine.dump(proj, p)
        return {"path": str(p), "flow": i, "steps": len(tree)}

    @tool("standard", ACT)
    def flow_run(path: str, flow: str = "", inputs: dict[str, Any] | None = None, outputs: list[str] | None = None,
                 timeout_s: int = 0) -> dict:
        """Run an .rflow automation (browser + desktop + PMC macros) and wait for it.
        Returns completed, error (with the failing step), outputs and a step log."""
        p = cfg.safe_path(path, must_exist=True, suffix=engine.SUFFIX)
        eng = engine.Engine(cfg)
        live["engine"] = eng
        try:
            return eng.run(engine.load(p), flow or None, inputs or {}, outputs, timeout_s or cfg.default_timeout)
        finally:
            live.pop("engine", None)

    @tool("standard", ACT, audit=False)
    def flow_stop() -> dict:
        """Stop the flow that flow_run is running."""
        eng = live.get("engine")
        if eng:
            eng.stop()
        return {"stopping": bool(eng)}

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
