import json
import threading
from pathlib import Path

import anyio
import pytest
from mcp.client import Client

from pmc_mcp import pmcfile
from pmc_mcp.bridge import BridgeClient, BridgeError
from pmc_mcp.config import Config
from pmc_mcp.server import create_server


class FakePMC:
    """Simulates LIB/AgentBridge.ahk: answers requests by writing <id>.json."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "bridge.json").write_text(json.dumps({"hwnd": 1234, "pid": 1, "version": "5.4.1"}))
        self.playing = 0
        self.requests = []

    def send(self, hwnd, payload):
        req = json.loads(payload)
        self.requests.append(req)
        cmd, args = req["cmd"], req["args"]
        if cmd == "status":
            resp = {"ok": 1, "result": {"playing": self.playing, "recording": 0, "macros": []}}
            self.playing = 0  # finishes after the first poll
        elif cmd == "play":
            self.playing = 1
            resp = {"ok": 1, "result": {"started": 1, "macro": args.get("macro", 1)}}
        elif cmd == "get_macro":
            resp = {"ok": 1, "result": {"macro": 1, "name": "M", "rows": [["1", "Run", "notepad", "1", "0", "Run"], ["02", "[Pause]", "", "1", "500", "Sleep"]]}}
        elif cmd == "boom":
            resp = {"ok": 0, "error": "kaboom"}
        else:
            resp = {"ok": 1, "result": {"cmd": cmd, **args}}
        resp["id"] = req["id"]

        def later():
            (self.dir / f"{req['id']}.json").write_text(json.dumps(resp))

        threading.Timer(0.05, later).start()
        return True


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "macros"
    root.mkdir()
    cfg = Config(level="standard", roots=[root.resolve()], audit_log=tmp_path / "audit.jsonl")
    fake = FakePMC(tmp_path / "bridge")
    bridge = BridgeClient(directory=fake.dir, sender=fake.send, is_window=lambda h, pid=None: True)
    return cfg, fake, bridge, root


def call(server, name, **args):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args)

    return anyio.run(go)


def payload(result):
    assert not result.is_error, result.content[0].text
    sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if sc is not None:
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def test_tools_registered(env):
    cfg, _, bridge, _ = env
    server = create_server(cfg, bridge)

    async def go():
        async with Client(server) as c:
            return [t.name for t in (await c.list_tools()).tools]

    names = anyio.run(go)
    for n in ["macro_create", "macro_run", "pmc_play", "pmc_record_stop", "desktop_screenshot", "desktop_run_ahk"]:
        assert n in names


def test_create_edit_read_cycle(env):
    cfg, _, bridge, root = env
    s = create_server(cfg, bridge)
    r = payload(call(s, "macro_create", path="notepad.pmc", macros=[{
        "name": "Hello",
        "steps": [
            {"kind": "run", "target": "notepad.exe"},
            {"kind": "win_wait", "window": "ahk_exe notepad.exe"},
            {"kind": "type_text", "text": "Hello, world"},
        ],
    }]))
    assert r["macros"][0]["steps"] == 3
    payload(call(s, "macro_add_steps", path="notepad.pmc", steps=[{"kind": "send_keys", "keys": "^s"}], position=0))
    payload(call(s, "macro_update_step", path="notepad.pmc", row=3, fields={"enabled": False}))
    payload(call(s, "macro_delete_steps", path="notepad.pmc", rows=[2]))
    d = payload(call(s, "macro_read", path="notepad.pmc", macro="Hello"))
    assert [st["action"] for st in d["steps"]] == ["Run", "[Text]", "^s"]
    assert d["steps"][1]["enabled"] is False
    # the file stays a valid PMC project
    proj = pmcfile.load(root / "notepad.pmc")
    assert proj.macros[0].steps[1].details == "Hello`, world"
    assert (root / "notepad.pmc").read_bytes().startswith(b"\xef\xbb\xbf[PMC Code v")
    listed = payload(call(s, "macro_list_files"))
    assert listed["files"][0]["macros"] == ["Hello"]
    assert (cfg.audit_log).read_text().count("macro_") >= 4


def test_path_confinement(env):
    cfg, _, bridge, _ = env
    s = create_server(cfg, bridge)
    r = call(s, "macro_create", path="../escape.pmc", macros=[{"name": "x"}])
    assert r.is_error and "outside the allowed" in r.content[0].text
    r = call(s, "macro_create", path="x.txt", macros=[{"name": "x"}])
    assert r.is_error


def test_permission_levels(env):
    cfg, _, bridge, _ = env
    s = create_server(cfg, bridge)
    r = call(s, "desktop_run_ahk", script="MsgBox hi")
    assert r.is_error and "full" in r.content[0].text
    cfg.level = "readonly"
    r = call(s, "macro_create", path="a.pmc", macros=[{"name": "x"}])
    assert r.is_error and "standard" in r.content[0].text
    assert not call(s, "macro_list_files").is_error


def test_bridge_play_wait_and_record(env):
    cfg, fake, bridge, _ = env
    s = create_server(cfg, bridge)
    r = payload(call(s, "pmc_play", macro="2", timeout_s=10))
    assert r["finished"] is True
    assert fake.requests[0] == {"id": fake.requests[0]["id"], "cmd": "play", "args": {"macro": 2}}
    r = payload(call(s, "pmc_get_live_macro"))
    assert r["steps"][0]["action"] == "Run" and r["steps"][1]["enabled"] is False


def test_bridge_error_and_unavailable(env, tmp_path):
    _, fake, bridge, _ = env
    with pytest.raises(BridgeError, match="kaboom"):
        bridge.call("boom", timeout=5)
    gone = BridgeClient(directory=tmp_path / "nothing", sender=fake.send)
    assert not gone.available()
    with pytest.raises(BridgeError, match="-agent"):
        gone.call("status")


def test_status_when_not_running(env, tmp_path):
    cfg, fake, _, _ = env
    s = create_server(cfg, BridgeClient(directory=tmp_path / "none", sender=fake.send))
    assert payload(call(s, "pmc_status"))["running"] is False


def test_screenshot_returns_image_and_scale(env, monkeypatch):
    PIL = pytest.importorskip("PIL")
    from PIL import Image as PILImage, ImageGrab

    monkeypatch.setattr(ImageGrab, "grab", lambda bbox=None, all_screens=False: PILImage.new("RGB", (3200, 1800), "white"))
    cfg, _, bridge, _ = env
    r = call(create_server(cfg, bridge), "desktop_screenshot")
    assert not r.is_error
    kinds = [c.type for c in r.content]
    assert "image" in kinds
    text = next(c.text for c in r.content if c.type == "text")
    assert "scale=2.0000" in text and "size=1600x900" in text
