"""Windows desktop primitives, implemented as small AutoHotkey v1.1 scripts.

Shared by the MCP desktop_* tools and the flow engine. Each function runs one
short-lived AHK process (see runner.run_ahk) and returns parsed JSON.
"""

from __future__ import annotations

from typing import Any

from . import runner
from . import steps as stepkit
from .config import Config

_q = runner.ahk_string
WINDOW_ACTIONS = ("activate", "minimize", "maximize", "restore", "close", "move", "wait")


def _activate(window: str) -> str:
    if not window:
        return ""
    return (f"t := {_q(window)}\nWinActivate, %t%\nWinWaitActive, %t%,, 3\n"
            "if ErrorLevel\n{\n_out(\"{\"\"ok\"\":false,\"\"error\"\":\"\"window not active\"\"}\")\nExitApp\n}\n")


def window_rect(cfg: Config, window: str) -> dict:
    return runner.run_ahk(cfg, f"""
        t := {_q(window)}
        WinGetPos, x, y, w, h, %t%
        if (w = "")
            _out("{{}}")
        else
            _out("{{""x"":" x ",""y"":" y ",""w"":" w ",""h"":" h "}}")
    """)


def list_windows(cfg: Config) -> dict:
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


def window_action(cfg: Config, window: str, action: str, x: int = 0, y: int = 0, width: int = 0, height: int = 0,
                  timeout_s: int = 5) -> dict:
    cmds = {
        "activate": "WinActivate, %t%\nWinWaitActive, %t%,, " + str(int(timeout_s)),
        "minimize": "WinMinimize, %t%",
        "maximize": "WinMaximize, %t%",
        "restore": "WinRestore, %t%",
        "close": "WinClose, %t%,, " + str(int(timeout_s)),
        "move": f"WinMove, %t%,, {int(x)}, {int(y)}, {int(width) or ''}, {int(height) or ''}",
        "wait": "WinWait, %t%,, " + str(int(timeout_s)),
    }
    if action not in cmds:
        raise ValueError(f"action must be one of {sorted(cmds)}")
    return runner.run_ahk(cfg, f"""
        t := {_q(window)}
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
    """, timeout=int(timeout_s) + 10)


def click(cfg: Config, x: int, y: int, button: str = "Left", count: int = 1, relative_to: str = "screen",
          window: str = "") -> dict:
    modes = {"screen": "Screen", "window": "Window", "client": "Client"}
    if relative_to.lower() not in modes:
        raise ValueError(f"relative_to must be one of {sorted(modes)}")
    mode = modes[relative_to.lower()]
    if mode != "Screen" and not window:
        raise ValueError("relative_to window/client requires window")
    return runner.run_ahk(cfg, f"""
        {_activate(window)}
        CoordMode, Mouse, {mode}
        Click, {int(x)}, {int(y)}, {stepkit._button(button)}, {int(count)}
        _out("{{""ok"":true}}")
    """)


def mouse_move(cfg: Config, x: int, y: int) -> dict:
    return runner.run_ahk(cfg, f'CoordMode, Mouse, Screen\nMouseMove, {int(x)}, {int(y)}, 0\n_out("{{""ok"":true}}")')


def send(cfg: Config, keys: str = "", text: str = "", window: str = "", control: str = "") -> dict:
    """Keys in AHK Send syntax and/or literal text, to the active window, a window, or a control."""
    if not keys and not text:
        raise ValueError("give keys and/or text")
    if control:
        if not window:
            raise ValueError("control requires window")
        body = f"t := {_q(window)}\nc := {_q(control)}\n"
        if text:
            body += f"_txt := {_q(text)}\nControlSendRaw, %c%, %_txt%, %t%\n"
        if keys:
            body += f"_k := {_q(keys)}\nControlSend, %c%, %_k%, %t%\n"
        return runner.run_ahk(cfg, body + '_out("{""ok"":" (ErrorLevel ? "false" : "true") "}")')
    body = _activate(window)
    if text:
        body += f"_txt := {_q(text)}\nSendInput, {{Text}}%_txt%\n"
    if keys:
        body += f"_k := {_q(keys)}\nSendInput, %_k%\n"
    return runner.run_ahk(cfg, body + '_out("{""ok"":true}")')


def run_program(cfg: Config, target: str, args: str = "", working_dir: str = "") -> dict:
    full = target + (" " + args if args else "")
    return runner.run_ahk(cfg, f"""
        _t := {_q(full)}
        _wd := {_q(working_dir)}
        Run, %_t%, %_wd%, UseErrorLevel, _pid
        _out("{{""ok"":" (ErrorLevel ? "false" : "true") ",""pid"":" (_pid + 0) "}}")
    """)


def msgbox(cfg: Config, text: str, title: str = "", timeout_s: int = 0) -> dict:
    return runner.run_ahk(cfg, f"""
        MsgBox, 0, % {_q(title)}, % {_q(text)}{", " + str(int(timeout_s)) if timeout_s else ""}
        _out("{{""ok"":true}}")
    """, timeout=(timeout_s or 3600) + 10)


def pixel_color(cfg: Config, x: int, y: int) -> dict:
    return runner.run_ahk(cfg, f"""
        CoordMode, Pixel, Screen
        PixelGetColor, c, {int(x)}, {int(y)}, RGB
        _out("{{""color"":" _q(c) "}}")
    """)


def image_search(cfg: Config, image: str, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0, variation: int = 30,
                 center: bool = True) -> dict:
    region = (
        f"x1 := {int(x1)}, y1 := {int(y1)}, x2 := {int(x2)}, y2 := {int(y2)}"
        if (x2 and y2)
        else "SysGet, x1, 76\nSysGet, y1, 77\nSysGet, vw, 78\nSysGet, vh, 79\nx2 := x1 + vw, y2 := y1 + vh"
    )
    return runner.run_ahk(cfg, f"""
        CoordMode, Pixel, Screen
        {region}
        f := {_q(str(image))}
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


def ahk_command(cfg: Config, name: str, params: str = "", target: str = "", window: str = "") -> dict:
    """One raw AHK v1 command line, as PMC's 'AHK command' step."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"bad command name {name!r}")
    line = f"{name}, {params}" if params else name
    return runner.run_ahk(cfg, f'{line}\n_out("{{""ok"":true,""errorlevel"":" _q(ErrorLevel) "}}")')


def screenshot_png(cfg: Config, window: str = "", region: tuple[int, int, int, int] | None = None,
                   max_width: int = 0) -> tuple[bytes, dict[str, Any]]:
    """PNG bytes plus {origin, scale, size}."""
    try:
        from PIL import ImageGrab
    except ImportError:
        raise RuntimeError("desktop screenshots need Pillow: pip install pillow") from None
    import io

    bbox = None
    if window:
        r = window_rect(cfg, window)
        if not r:
            raise LookupError(f"window not found: {window}")
        bbox = (r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"])
    elif region:
        x, y, w, h = region
        bbox = (x, y, x + w, y + h)
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    scale = 1.0
    if max_width and img.width > max_width:
        scale = img.width / max_width
        img = img.resize((max_width, round(img.height / scale)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    origin = (bbox[0], bbox[1]) if bbox else "virtual-screen origin (0,0 on single-monitor setups)"
    return buf.getvalue(), {"origin": origin, "scale": scale, "size": f"{img.width}x{img.height}"}
