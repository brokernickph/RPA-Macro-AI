"""Convert between PMC's flat step list and the block tree used by the visual board.

PMC stores a macro as ordered rows where If/ElseIf/Else/End If and LoopStart/LoopEnd
rows open and close blocks. The board shows the same macro as a tree:

    {"t": "step", "step": {...}}
    {"t": "if",   "branches": [{"head": step, "body": [...]}, ...],   # If, then ElseIf...
                  "else": {"head": step, "body": [...]} | None, "end": step}
    {"t": "loop", "head": step, "body": [...], "end": step}

Block heads and ends keep their original rows, so tree -> rows is lossless. If a
macro's blocks don't balance, the macro is shown flat (every row a plain step)
and still saves unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from . import steps as stepkit
from .pmcfile import Step


def _is_if_start(s: Step) -> bool:
    return s.type == "If_Statement" and s.action not in ("[Else]", "[End If]") and not s.action.startswith("[ElseIf]")


def _is_elseif(s: Step) -> bool:
    return s.type == "If_Statement" and s.action.startswith("[ElseIf]")


def _is_else(s: Step) -> bool:
    return s.type == "If_Statement" and s.action == "[Else]"


def _is_end_if(s: Step) -> bool:
    return s.type == "If_Statement" and s.action == "[End If]"


def _is_loop_start(s: Step) -> bool:
    return s.action == "[LoopStart]"


def _is_loop_end(s: Step) -> bool:
    return s.action == "[LoopEnd]"


class _Unbalanced(Exception):
    pass


def to_tree(rows: list[Step]) -> tuple[list[dict], bool]:
    """Return (tree, structured). structured=False means the blocks didn't balance."""
    try:
        tree, i = _parse(rows, 0, stop=None)
        if i != len(rows):
            raise _Unbalanced
        return tree, True
    except _Unbalanced:
        return [_leaf(s) for s in rows], False


def _leaf(s: Step) -> dict:
    return {"t": "step", "step": s.to_dict()}


def _parse(rows: list[Step], i: int, stop) -> tuple[list[dict], int]:
    out: list[dict] = []
    while i < len(rows):
        s = rows[i]
        if stop and stop(s):
            return out, i
        if _is_if_start(s):
            node = {"t": "if", "branches": [], "else": None, "end": None}
            head = s
            i += 1
            while True:
                body, i = _parse(rows, i, stop=lambda r: _is_elseif(r) or _is_else(r) or _is_end_if(r))
                if i >= len(rows):
                    raise _Unbalanced
                if node["else"] is None and head is not None:
                    node["branches"].append({"head": head.to_dict(), "body": body})
                else:
                    node["else"]["body"] = body
                nxt = rows[i]
                i += 1
                if _is_end_if(nxt):
                    node["end"] = nxt.to_dict()
                    break
                if node["else"] is not None:
                    raise _Unbalanced  # ElseIf/Else after Else
                if _is_else(nxt):
                    node["else"] = {"head": nxt.to_dict(), "body": []}
                    head = None
                else:
                    head = nxt
            out.append(node)
        elif _is_loop_start(s):
            body, j = _parse(rows, i + 1, stop=_is_loop_end)
            if j >= len(rows):
                raise _Unbalanced
            out.append({"t": "loop", "head": s.to_dict(), "body": body, "end": rows[j].to_dict()})
            i = j + 1
        elif _is_elseif(s) or _is_else(s) or _is_end_if(s) or _is_loop_end(s):
            raise _Unbalanced  # closer with no opener at this level
        else:
            out.append(_leaf(s))
            i += 1
    if stop:
        raise _Unbalanced
    return out, i


def to_rows(tree: list[dict]) -> list[Step]:
    """Flatten a board tree back into PMC rows (building new rows from specs)."""
    out: list[Step] = []
    for node in tree:
        t = node.get("t")
        if t == "step":
            out.append(node_step(node))
        elif t == "if":
            branches = node.get("branches") or []
            if not branches:
                raise ValueError("an If block needs at least one condition")
            for n, br in enumerate(branches):
                head = _block_step(br.get("head"), "if" if n == 0 else "elseif")
                out.append(head)
                out.extend(to_rows(br.get("body") or []))
            if node.get("else") is not None:
                out.append(_block_step(node["else"].get("head"), "else"))
                out.extend(to_rows(node["else"].get("body") or []))
            out.append(_block_step(node.get("end"), "end_if"))
        elif t == "loop":
            out.append(_block_step(node.get("head"), "loop"))
            out.extend(to_rows(node.get("body") or []))
            out.append(_block_step(node.get("end"), "loop_end"))
        else:
            raise ValueError(f"unknown node type {t!r}")
    return out


def node_step(node: dict) -> Step:
    """A leaf: a new/edited node has a spec; an untouched one keeps its raw step."""
    if node.get("spec"):
        return stepkit.build(node["spec"])
    if node.get("step"):
        return Step.from_dict(node["step"])
    raise ValueError("node has neither spec nor step")


def _block_step(raw: Any, role: str) -> Step:
    if isinstance(raw, dict) and raw.get("kind"):
        return stepkit.build(raw)
    if isinstance(raw, dict) and raw:
        return Step.from_dict(raw)
    defaults = {
        "if": lambda: stepkit.if_expr("true"),
        "elseif": lambda: Step(action="[ElseIf] Compare Variables", details="true", type="If_Statement"),
        "else": stepkit.else_,
        "end_if": stepkit.end_if,
        "loop": lambda: stepkit.loop(0),
        "loop_end": stepkit.loop_end,
    }
    return defaults[role]()


# ---- describing rows for the inspector ---------------------------------------

_CLICK_RE = re.compile(r"^\s*(-?\d+),\s*(-?\d+)\s+(Left|Right|Middle|X1|X2),\s*(\d+)\s*$")
_MOVE_RE = re.compile(r"^\s*(-?\d+),\s*(-?\d+),\s*0\s*$")


def unescape_text(v: str) -> str:
    """Inverse of pmcfile.escape_text."""
    return re.sub(r"`(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), v)


def describe(step: dict) -> dict | None:
    """Best-effort reverse of the step builders: {kind, ...params} or None (raw)."""
    s = Step.from_dict(step)
    if s.type == "Click":
        if m := _CLICK_RE.match(s.details):
            return {"kind": "click", "x": int(m[1]), "y": int(m[2]), "button": m[3], "count": int(m[4])}
        if m := _MOVE_RE.match(s.details):
            return {"kind": "mouse_move", "x": int(m[1]), "y": int(m[2])}
    if s.type == "Sleep" and s.action == "[Pause]":
        return {"kind": "sleep", "ms": int(s.delay or 0)}
    if s.type == "Send":
        return {"kind": "send_keys", "keys": s.details, "label": s.action, "delay": int(s.delay or 0)}
    if s.type == "ControlSend":
        return {"kind": "send_keys", "keys": s.details, "control": s.target, "window": s.window, "label": s.action,
                "delay": int(s.delay or 0)}
    if s.type in ("SendRaw", "ControlSendRaw", "ControlSetText") and s.action == "[Text]":
        d = {"kind": "type_text", "text": unescape_text(s.details)}
        if s.type != "SendRaw":
            d.update(control=s.target, window=s.window)
        if s.type == "ControlSetText":
            d["set_text"] = True
        return d
    if s.type == "Run" and "," not in s.details.replace("`,", ""):
        return {"kind": "run", "target": unescape_text(s.details)}
    if s.type in ("WinWait", "WinActivate", "WinClose") and not s.details:
        return {"kind": {"WinWait": "win_wait", "WinActivate": "win_activate", "WinClose": "win_close"}[s.type], "window": s.window}
    if s.type == "Variable" and " := " in s.details:
        var, val = s.details.split(" := ", 1)
        if not re.match(r"^[A-Za-z_#@$][A-Za-z0-9_#@$]*$", var):
            return None  # arrays / objects (N[1], obj.key): keep the raw row
        expr = s.target == "Expression"
        if not expr and re.search(r"(?<!`)`%", val):
            return None  # a literal-% value: keep the raw row so it saves unchanged
        return {"kind": "assign", "var": var, "value": val if expr else unescape_text(val), "expression": expr}
    if s.type == "MsgBox" and s.action == "[MsgBox]" and re.fullmatch(r"\d*", s.target) and not s.details.startswith("% ") \
            and not s.window.startswith("% "):
        return {"kind": "msgbox", "text": unescape_text(s.details), "options": int(s.target or 0), "title": unescape_text(s.window)}
    if s.type == "CommentBlock":
        return {"kind": "comment", "text": unescape_text(s.details)}
    if s.type == "Gosub":
        return {"kind": "gosub", "label": s.details}
    if s.type == "Break" and not s.details:
        return {"kind": "break"}
    return None


# ---- palette schema for the board ------------------------------------------

F = dict  # field helper for readability


def _f(name, label, type_="text", **kw):
    return F(name=name, label=label, type=type_, **kw)


PALETTE: list[dict] = [
    {"group": "Browser", "kinds": [
        {"kind": "browser_open", "label": "Open web page", "icon": "globe", "scope": "flow", "fields": [
            _f("url", "URL", default="https://example.com")]},
        {"kind": "browser_click", "label": "Click element", "icon": "cursor", "scope": "flow", "fields": [
            _f("selector", "Element (CSS, text=..., role=...)", default="text=Sign in")]},
        {"kind": "browser_fill", "label": "Fill field", "icon": "text", "scope": "flow", "fields": [
            _f("selector", "Field", default="#search"), _f("text", "Text", default=""),
            _f("submit", "Press Enter afterwards", "checkbox", default=False)]},
        {"kind": "browser_select", "label": "Choose option", "icon": "list", "scope": "flow", "fields": [
            _f("selector", "Drop-down", default="select"), _f("value", "Option value or label", default="")]},
        {"kind": "browser_press", "label": "Press key (web)", "icon": "key", "scope": "flow", "fields": [
            _f("key", "Key (Enter, Tab, Control+A)", default="Enter"), _f("selector", "On element (optional)", default="")]},
        {"kind": "browser_wait", "label": "Wait for element", "icon": "hourglass", "scope": "flow", "fields": [
            _f("selector", "Element", default=""), _f("text", "…or text", default=""),
            _f("timeout_s", "Timeout (s)", "number", default=15), _f("var", "Save found=true/false in (optional)", default="")]},
        {"kind": "browser_extract", "label": "Get text", "icon": "var", "scope": "flow", "fields": [
            _f("selector", "Element", default="h1"), _f("var", "Save in variable", default="Text"),
            _f("attribute", "Attribute (optional, e.g. href)", default=""), _f("all", "All matches (list)", "checkbox", default=False)]},
        {"kind": "browser_table", "label": "Read table", "icon": "table", "scope": "flow", "fields": [
            _f("selector", "Table", default="table"), _f("var", "Save rows in", default="Rows")]},
        {"kind": "browser_screenshot", "label": "Page screenshot", "icon": "camera", "scope": "flow", "fields": [
            _f("path", "Save as (.png)", default="page.png")]},
        {"kind": "browser_close", "label": "Close browser", "icon": "close", "scope": "flow", "fields": []},
    ]},
    {"group": "Mouse", "kinds": [
        {"kind": "click", "label": "Click", "icon": "cursor", "fields": [
            _f("x", "X", "number", default=0), _f("y", "Y", "number", default=0),
            _f("button", "Button", "select", options=["Left", "Right", "Middle"], default="Left"),
            _f("count", "Clicks", "number", default=1)]},
        {"kind": "mouse_move", "label": "Move mouse", "icon": "move", "fields": [
            _f("x", "X", "number", default=0), _f("y", "Y", "number", default=0)]},
    ]},
    {"group": "Keyboard", "kinds": [
        {"kind": "type_text", "label": "Type text", "icon": "text", "fields": [
            _f("text", "Text", "textarea", default=""),
            _f("window", "Window (optional, background)", default=""),
            _f("control", "Control (optional)", default="")]},
        {"kind": "send_keys", "label": "Press keys", "icon": "key", "fields": [
            _f("keys", "Keys (AHK syntax, e.g. ^s or {Enter})", default="{Enter}")]},
    ]},
    {"group": "Apps & windows", "kinds": [
        {"kind": "run", "label": "Open app / file / URL", "icon": "app", "fields": [
            _f("target", "Program, file or URL", default="notepad.exe")]},
        {"kind": "win_wait", "label": "Wait for window", "icon": "hourglass", "fields": [
            _f("window", "Window title", default="Untitled - Notepad")]},
        {"kind": "win_activate", "label": "Focus window", "icon": "window", "fields": [
            _f("window", "Window title", default="Untitled - Notepad")]},
        {"kind": "win_close", "label": "Close window", "icon": "close", "fields": [
            _f("window", "Window title", default="Untitled - Notepad")]},
    ]},
    {"group": "Flow", "kinds": [
        {"kind": "sleep", "label": "Wait", "icon": "clock", "fields": [_f("ms", "Milliseconds", "number", default=1000)]},
        {"kind": "if", "label": "If / else", "icon": "branch", "block": True, "fields": [
            _f("condition", "Condition (AHK expression)", default="ErrorLevel = 0")]},
        {"kind": "loop", "label": "Repeat", "icon": "loop", "block": True, "fields": [
            _f("count", "Times (0 = until Break)", "number", default=3)]},
        {"kind": "break", "label": "Break loop", "icon": "stop", "fields": []},
        {"kind": "foreach", "label": "For each", "icon": "loop", "block": True, "scope": "flow", "fields": [
            _f("items", "List (expression, e.g. Rows)", default="Rows"), _f("item", "Each item is called", default="Row")]},
        {"kind": "gosub", "label": "Run another macro", "icon": "call", "scope": "pmc", "fields": [_f("label", "Macro name", default="")]},
        {"kind": "run_macro", "label": "Run PMC macro", "icon": "call", "scope": "flow", "fields": [
            _f("path", "Macro file (.pmc)", default="my-macro.pmc"), _f("macro", "Macro name (optional)", default=""),
            _f("inputs", "Inputs (one per line: Name={{Variable}})", "kv", default={}),
            _f("outputs", "Outputs to keep (comma separated)", "list", default=[])]},
    ]},
    {"group": "Data", "kinds": [
        {"kind": "assign", "label": "Set variable", "icon": "var", "fields": [
            _f("var", "Variable", default="MyVar"), _f("value", "Value", default=""),
            _f("expression", "Value is an expression", "checkbox", default=False)]},
        {"kind": "msgbox", "label": "Show message", "icon": "message", "fields": [
            _f("text", "Message", "textarea", default="Done!"), _f("title", "Title (optional)", default="")]},
        {"kind": "file_append", "label": "Append to file", "icon": "file", "fields": [
            _f("text", "Text", default=""), _f("path", "File path", default="")]},
        {"kind": "log", "label": "Log", "icon": "note", "scope": "flow", "fields": [_f("message", "Message", default="Row {{index}} done")]},
        {"kind": "comment", "label": "Note", "icon": "note", "fields": [_f("text", "Note", "textarea", default="")]},
    ]},
    {"group": "Advanced", "kinds": [
        {"kind": "command", "label": "AHK command", "icon": "code", "fields": [
            _f("name", "Command", default="SoundBeep"), _f("params", "Parameters", default=""),
            _f("target", "Control", default=""), _f("window", "Window", default="")]},
    ]},
]
