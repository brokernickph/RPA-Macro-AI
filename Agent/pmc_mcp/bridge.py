"""Client for LIB/AgentBridge.ahk: talks to a running PMC over WM_COPYDATA.

Protocol (see AgentBridge.ahk): send a JSON request with dwData=0x504D4341 to the
hwnd listed in %TEMP%\\PMC_Agent\\bridge.json, then wait for the response file
%TEMP%\\PMC_Agent\\<id>.json.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

PMCA = 0x504D4341
WM_COPYDATA = 0x004A


class BridgeError(RuntimeError):
    pass


class BridgeUnavailable(BridgeError):
    pass


def bridge_dir() -> Path:
    return Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "PMC_Agent"


def _win32_sender(hwnd: int, payload: str, timeout_ms: int = 5000) -> bool:
    """Send a WM_COPYDATA message. Returns True if PMC accepted it."""
    import ctypes
    from ctypes import wintypes

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]

    data = payload.encode("utf-16-le")
    buf = ctypes.create_string_buffer(data + b"\0\0")
    cds = COPYDATASTRUCT(PMCA, len(data), ctypes.cast(buf, ctypes.c_void_p))
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    result = ctypes.c_size_t(0)
    SMTO_ABORTIFHUNG = 0x0002
    ok = user32.SendMessageTimeoutW(
        hwnd, WM_COPYDATA, 0, ctypes.addressof(cds), SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)
    )
    return bool(ok) and result.value == 1


def _win32_is_window(hwnd: int, pid: int | None = None) -> bool:
    """True if hwnd is a live window (owned by pid, when given: handles get reused)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        return False
    if pid:
        owner = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        return owner.value == pid
    return True


class BridgeClient:
    def __init__(
        self,
        directory: Path | None = None,
        sender: Callable[[int, str], bool] | None = None,
        is_window: Callable[..., bool] | None = None,
    ):
        self.dir = directory or bridge_dir()
        on_windows = sys.platform == "win32"
        self._send = sender or (_win32_sender if on_windows else None)
        self._is_window = is_window or (_win32_is_window if on_windows else (lambda h, pid=None: True))

    def info(self) -> dict[str, Any] | None:
        try:
            info = json.loads((self.dir / "bridge.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not info.get("hwnd") or not self._is_window(int(info["hwnd"]), info.get("pid")):
            return None
        return info

    def available(self) -> bool:
        return self._send is not None and self.info() is not None

    def call(self, cmd: str, timeout: float = 30.0, **args: Any) -> Any:
        if self._send is None:
            raise BridgeUnavailable("the PMC agent bridge only works on Windows")
        info = self.info()
        if info is None:
            raise BridgeUnavailable(
                "PMC is not running with the agent bridge. Use pmc_launch, or start "
                "MacroCreator with the -agent switch (or set [Options] AgentBridge=1 in MacroCreator.ini)."
            )
        req_id = uuid.uuid4().hex
        reply = self.dir / f"{req_id}.json"
        payload = json.dumps({"id": req_id, "cmd": cmd, "args": {k: v for k, v in args.items() if v is not None}})
        if not self._send(int(info["hwnd"]), payload):
            raise BridgeError("PMC did not accept the request (busy, hung, or bridge disabled)")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if reply.exists():
                try:
                    resp = json.loads(reply.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    time.sleep(0.05)  # still being renamed into place
                    continue
                try:
                    reply.unlink()
                except OSError:
                    pass
                if not resp.get("ok"):
                    raise BridgeError(resp.get("error") or "PMC reported an error")
                return resp.get("result")
            time.sleep(0.05)
        raise BridgeError(f"timed out after {timeout}s waiting for PMC to answer '{cmd}'")
