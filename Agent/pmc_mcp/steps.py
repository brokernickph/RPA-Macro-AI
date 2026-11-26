"""Builders that turn simple agent-friendly step specs into PMC rows.

Agents describe steps as small dicts, e.g. ``{"kind": "click", "x": 10, "y": 20}``.
Every builder produces exactly the row shape PMC's own command windows write,
so the result opens, edits and plays normally in the PMC GUI.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any, Callable

from .pmcfile import Macro, Step, check_var_name, escape_text

_BUTTONS = {"left": "Left", "right": "Right", "middle": "Middle", "x1": "X1", "x2": "X2"}


def _button(b: str) -> str:
    try:
        return _BUTTONS[str(b).lower()]
    except KeyError:
        raise ValueError(f"button must be one of {sorted(_BUTTONS)}") from None


def _int(v: Any, name: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {v!r}") from None


def send_keys(keys: str, delay: int = 10, control: str = "", window: str = "") -> Step:
    """AHK Send syntax, e.g. "^c", "{Enter}", "{Ctrl Down}a{Ctrl Up}"."""
    if control or window:
        return Step(action=keys, details=keys, delay=str(delay), type="ControlSend", target=control, window=window)
    return Step(action=keys, details=keys, delay=str(delay), type="Send")


def type_text(text: str, control: str = "", window: str = "", set_text: bool = False) -> Step:
    """Type literal text. With control+window it goes to a background control."""
    details = escape_text(text)
    if set_text:
        if not control:
            raise ValueError("set_text requires control")
        return Step(action="[Text]", details=details, type="ControlSetText", target=control, window=window)
    if control or window:
        return Step(action="[Text]", details=details, type="ControlSendRaw", target=control, window=window)
    return Step(action="[Text]", details=details, type="SendRaw")


def click(x: int, y: int, button: str = "Left", count: int = 1, delay: int = 10) -> Step:
    b = _button(button)
    x, y, count = _int(x, "x"), _int(y, "y"), _int(count, "count")
    return Step(action=f"{b} Move & Click", details=f"{x}, {y} {b}, {count}", delay=str(delay), type="Click")


def mouse_move(x: int, y: int, delay: int = 10) -> Step:
    return Step(action="Move", details=f"{_int(x, 'x')}, {_int(y, 'y')}, 0", delay=str(delay), type="Click")


def sleep(ms: int) -> Step:
    return Step(action="[Pause]", details="", delay=str(_int(ms, "ms")), type="Sleep")


def run(target: str, args: str = "", working_dir: str = "", pid_var: str = "") -> Step:
    t = escape_text(target + (" " + args if args else ""))
    details = t
    if working_dir or pid_var:
        details = f"{t}, {escape_text(working_dir)}, , {check_var_name(pid_var) if pid_var else ''}".rstrip(", ")
    return Step(action="Run", details=details, type="Run")


def _win(cmd: str, window: str, delay: int = 333, details: str = "") -> Step:
    if not window:
        raise ValueError(f"{cmd} needs a window title, e.g. 'Untitled - Notepad' or 'ahk_exe notepad.exe'")
    return Step(action=cmd, details=details, delay=str(delay), type=cmd, window=window)


def win_wait(window: str, timeout_s: int | None = None) -> Step:
    return _win("WinWait", window, details="" if timeout_s is None else f", {_int(timeout_s, 'timeout_s')}")


def win_activate(window: str) -> Step:
    return _win("WinActivate", window)


def win_close(window: str) -> Step:
    return _win("WinClose", window)


def assign(var: str, value: str, expression: bool = False) -> Step:
    check_var_name(var)
    if expression:
        return Step(action="[Assign Variable]", details=f"{var} := {value}", type="Variable", target="Expression")
    return Step(action="[Assign Variable]", details=f"{var} := {escape_text(value)}", type="Variable")


def msgbox(text: str, options: int = 0) -> Step:
    return Step(action="[MsgBox]", details=escape_text(text), type="MsgBox", target=str(_int(options, "options")))


def if_expr(condition: str) -> Step:
    """Start an If block, condition is an AHK expression, e.g. "ErrorLevel = 0"."""
    return Step(action="Compare Variables", details=condition, type="If_Statement")


def else_() -> Step:
    return Step(action="[Else]", details="Else", type="If_Statement")


def end_if() -> Step:
    return Step(action="[End If]", details="EndIf", type="If_Statement")


def loop(count: int | str = 0) -> Step:
    """Start a Loop block; count 0 = infinite (use break)."""
    return Step(action="[LoopStart]", details="LoopStart", repeat=str(count), type="Loop")


def loop_end() -> Step:
    return Step(action="[LoopEnd]", details="LoopEnd", type="Loop")


def break_() -> Step:
    return Step(action="Break", type="Break")


def gosub(label: str) -> Step:
    return Step(action="[Gosub]", details=label, type="Gosub")


def file_append(text: str, path: str, encoding: str = "UTF-8") -> Step:
    return Step(action="FileAppend", details=f"{text}, {escape_text(path)}, {encoding}", type="FileAppend")


def command(name: str, params: str = "", target: str = "", window: str = "", delay: int = 0) -> Step:
    """Any AutoHotkey v1 command PMC supports, e.g. ("CoordMode", "Mouse, Screen")."""
    return Step(action=name, details=params, delay=str(delay), type=name, target=target, window=window)


def comment(text: str) -> Step:
    return Step(action="[CommentBlock]", details=escape_text(text), repeat="0", delay="1", type="CommentBlock")


BUILDERS: dict[str, Callable[..., Step]] = {
    "send_keys": send_keys,
    "type_text": type_text,
    "click": click,
    "mouse_move": mouse_move,
    "sleep": sleep,
    "run": run,
    "win_wait": win_wait,
    "win_activate": win_activate,
    "win_close": win_close,
    "assign": assign,
    "msgbox": msgbox,
    "if": if_expr,
    "else": else_,
    "end_if": end_if,
    "loop": loop,
    "loop_end": loop_end,
    "break": break_,
    "gosub": gosub,
    "file_append": file_append,
    "command": command,
    "comment": comment,
}

STEP_KINDS_DOC = """Step specs: {"kind": <kind>, ...params}. Kinds:
- send_keys: keys (AHK Send syntax: "^s", "{Enter}", "{Ctrl Down}a{Ctrl Up}"), [delay], [control, window]
- type_text: text (literal), [control, window] (background control), [set_text=true]
- click: x, y, [button=Left|Right|Middle], [count=1]   (coordinates follow the macro's CoordMode, default Window)
- mouse_move: x, y
- sleep: ms
- run: target, [args], [working_dir], [pid_var]
- win_wait / win_activate / win_close: window (e.g. "Untitled - Notepad", "ahk_exe notepad.exe"), [timeout_s for win_wait]
- assign: var, value, [expression=false]  (literal text unless expression=true)
- msgbox: text
- if: condition (AHK expression) ... else ... end_if
- loop: [count=0 infinite] ... loop_end ; break
- gosub: label (another macro's name)
- file_append: text, path
- command: name, params, [target, window]  (any AHK v1 command PMC supports, e.g. CoordMode "Mouse, Screen")
- comment: text
- raw: action, details, type, [repeat, delay, target, window, comment, enabled] (exact PMC row)"""


def build(spec: dict[str, Any]) -> Step:
    spec = dict(spec)
    kind = spec.pop("kind", None)
    if kind == "raw":
        return Step.from_dict(spec)
    enabled = spec.pop("enabled", True)
    note = spec.pop("note", "")
    if kind not in BUILDERS:
        raise ValueError(f"unknown step kind {kind!r}; valid kinds: {sorted(BUILDERS) + ['raw']}")
    try:
        step = BUILDERS[kind](**spec)
    except TypeError as e:
        raise ValueError(f"bad parameters for {kind}: {e}") from None
    step.enabled = bool(enabled)
    step.comment = note
    return step


# ---- run-time parameter injection ------------------------------------------


def inject_io(macro: Macro, inputs: dict[str, Any], outputs: list[str], out_dir: str) -> None:
    """Prepend input assignments and append rows that dump output variables.

    Each output variable is written to ``<out_dir>/<name>.txt``; a final
    ``__done__.txt`` marker proves the macro reached its end.
    """
    pre = [assign(check_var_name(k), "" if v is None else str(v)) for k, v in inputs.items()]
    post = []
    for name in outputs:
        check_var_name(name)
        post.append(file_append(f"%{name}%", str(PureWindowsPath(out_dir) / f"{name}.txt")))
    post.append(file_append("done", str(PureWindowsPath(out_dir) / "__done__.txt")))
    for s in pre + post:
        s.comment = "pmc-mcp"
    macro.steps[:0] = pre
    macro.steps.extend(post)
