import json
import shutil
import threading
import urllib.request
from pathlib import Path

import pytest

from pmc_mcp import board_server, pmcfile
from pmc_mcp.bridge import BridgeClient
from pmc_mcp.config import Config

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "Documentation/MacroCreator_Help-doc/Examples"
TOKEN = "test-token"


@pytest.fixture
def board(tmp_path):
    root = (tmp_path / "macros").resolve()
    root.mkdir()
    shutil.copy(EXAMPLES / "CompareVars.pmc", root / "CompareVars.pmc")
    cfg = Config(roots=[root], audit_log=tmp_path / "audit.jsonl")
    httpd, url = board_server.serve(cfg, open_browser=False, token=TOKEN,
                                    bridge=BridgeClient(directory=tmp_path / "nobridge", sender=lambda h, p: False))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", root, cfg
    httpd.shutdown()


def req(base, method, path, body=None, token=TOKEN, host=None):
    r = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode())
    if token:
        r.add_header("X-Board-Token", token)
    if host:
        r.add_header("Host", host)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_security(board):
    base, root, _ = board
    assert req(base, "GET", "/api/files", token=None)[0] == 403
    assert req(base, "GET", "/api/files", token="wrong")[0] == 403
    assert req(base, "GET", "/api/files", host="evil.example")[0] == 403  # DNS rebinding
    assert req(base, "GET", "/api/project?path=../../etc/x.pmc")[0] == 403
    for evil in ("/../pyproject.toml", "/%2e%2e/pyproject.toml", "/..%2fbridge.py"):  # static path traversal
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + evil)
        assert e.value.code == 404


def test_static_served(board):
    base, _, _ = board
    with urllib.request.urlopen(base + "/") as r:
        assert b"RPA Macro AI" in r.read()
    with urllib.request.urlopen(base + "/board.js") as r:
        assert "javascript" in r.headers["Content-Type"]


def test_open_edit_save(board):
    base, root, cfg = board
    status, files = req(base, "GET", "/api/files")
    assert status == 200 and files["files"][0]["name"] == "CompareVars.pmc"
    status, proj = req(base, "GET", "/api/project?path=CompareVars.pmc")
    assert status == 200
    m = proj["macros"][0]
    assert m["structured"] and m["tree"][0]["t"] == "if"
    assert m["tree"][0]["branches"][0]["body"][0]["view"]["kind"] == "msgbox"
    # add a step at the top and save
    m["tree"].insert(0, {"t": "step", "spec": {"kind": "sleep", "ms": 250}})
    status, saved = req(base, "PUT", "/api/project", {"path": proj["path"], "mtime": proj["mtime"], "macros": [m]})
    assert status == 200, saved
    on_disk = pmcfile.load(root / "CompareVars.pmc").macros[0]
    assert on_disk.steps[0].delay == "250" and on_disk.steps[1].details == "A_Hour > 11"
    assert on_disk.hotkey == pmcfile.load(EXAMPLES / "CompareVars.pmc").macros[0].hotkey  # metadata kept
    # stale mtime -> conflict
    status, err = req(base, "PUT", "/api/project", {"path": proj["path"], "mtime": proj["mtime"] - 100, "macros": [m]})
    assert status == 409 and "changed on disk" in err["error"]
    assert "board_save" in cfg.audit_log.read_text()


def test_create_and_bad_tree(board):
    base, root, _ = board
    status, r = req(base, "PUT", "/api/project", {"path": "new.pmc", "create": True, "macros": [{"name": "Macro1", "tree": []}]})
    assert status == 200 and (root / "new.pmc").exists()
    assert req(base, "PUT", "/api/project", {"path": "new.pmc", "create": True, "macros": [{"name": "M", "tree": []}]})[0] == 409
    status, err = req(base, "PUT", "/api/project", {"path": "new.pmc", "macros": [{"name": "M", "tree": [{"t": "step", "spec": {"kind": "teleport"}}]}]})
    assert status == 400 and "teleport" in err["error"]


def test_readonly_level_blocks_writes(board):
    base, _, cfg = board
    cfg.level = "readonly"
    status, err = req(base, "PUT", "/api/project", {"path": "x.pmc", "create": True, "macros": [{"name": "M", "tree": []}]})
    assert status == 403 and "standard" in err["error"]
    assert req(base, "POST", "/api/run", {"path": "CompareVars.pmc"})[0] == 403


def test_pmc_status_offline(board):
    base, _, _ = board
    assert req(base, "GET", "/api/pmc/status") == (200, {"running": False})


def test_cli_idle_exit_and_file_folder_allowed(tmp_path):
    import subprocess
    import sys

    other = tmp_path / "elsewhere"
    other.mkdir()
    shutil.copy(EXAMPLES / "Demo.pmc", other / "Demo.pmc")
    env = {**__import__("os").environ, "PMC_MCP_ROOTS": str(tmp_path / "roots"), "PMC_MCP_AUDIT_LOG": str(tmp_path / "a.jsonl")}
    p = subprocess.Popen([sys.executable, "-m", "pmc_mcp.board_server", str(other / "Demo.pmc"), "--no-browser", "--idle-exit", "1"],
                         stdout=subprocess.PIPE, text=True, env=env)
    line = p.stdout.readline()
    assert "PMC Visual Board: http://127.0.0.1:" in line and "Demo.pmc" in line  # opened despite being outside roots
    assert p.wait(timeout=20) == 0  # exits on its own once idle


def test_board_opens_flow_files(tmp_path):
    from pmc_mcp import engine

    root = (tmp_path / "m").resolve()
    root.mkdir()
    engine.dump(engine.new_project("F"), root / "f.rflow")
    (root / "notes.txt").write_text("x")
    cfg = Config(roots=[root], audit_log=None)
    httpd, url = board_server.serve(cfg, open_browser=False, file=str(root / "f.rflow"), token=TOKEN,
                                    bridge=BridgeClient(directory=tmp_path / "nb", sender=lambda h, p: False))
    assert url.endswith("f.rflow")
    httpd.server_close()
    with pytest.raises(ValueError):
        board_server.serve(cfg, open_browser=False, file=str(root / "notes.txt"))
