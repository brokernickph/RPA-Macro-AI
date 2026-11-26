import os
import threading
import time
from pathlib import Path

import pytest

from pmc_mcp import engine
from pmc_mcp.config import Config
from pmc_mcp.engine import Engine, FlowError, evaluate, render

SHOP = """<!doctype html><html><head><title>Test Shop</title></head><body>
<form onsubmit="event.preventDefault(); document.getElementById('out').innerText='Searched: '+document.getElementById('q').value">
<input id="q" placeholder="Order number"><button type="submit">Find</button></form><p id="out"></p>
<table id="orders"><tr><th>Order</th><th>Customer</th><th>Total</th></tr>
<tr><td>A-1</td><td>Ada</td><td>12.50</td></tr><tr><td>A-2</td><td>Grace</td><td>99.00</td></tr>
<tr><td>A-3</td><td>Linus</td><td>250.00</td></tr></table></body></html>"""


class FakeDesktop:
    """Records desktop calls instead of running AutoHotkey."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def f(cfg, *a, **k):
            self.calls.append((name, a, k))
            return {"ok": True, "pid": 42}
        return f


def step(kind, **params):
    return {"t": "step", "spec": {"kind": kind, **params}}


@pytest.fixture
def cfg(tmp_path):
    root = (tmp_path / "work").resolve()
    root.mkdir()
    return Config(roots=[root], audit_log=None, default_timeout=60)


# ---- expressions -------------------------------------------------------------

@pytest.mark.parametrize("expr,expected", [
    ("Total > 50", True), ("row.Total = 12.5", True), ('name.lower().startswith("inv")', True),
    ("Missing = ''", True), ("n + 2", 5), ('name + "-" + n', "Invoice-3"), ("n >= 3 && Total < 100", True),
    ('url = "a=b"', True), ("rows[0]['a'] + 1", 2), ("len(rows)", 1), ("not flag", True),
])
def test_evaluate(expr, expected):
    v = {"Total": "99.00", "row": {"Total": "12.5"}, "n": 3, "name": "Invoice", "url": "a=b", "rows": [{"a": 1}], "flag": ""}
    assert evaluate(expr, v) == expected


@pytest.mark.parametrize("bad", ['__import__("os")', "name.__class__", 'open("x")', "(lambda: 1)()", "[x for x in rows]"])
def test_evaluate_blocks_escapes(bad):
    with pytest.raises(FlowError):
        evaluate(bad, {"name": "x", "rows": []})


def test_render():
    v = {"row": {"Customer": "Ada"}, "rows": [1, 2]}
    assert render("Hi {{row.Customer}}!", v) == "Hi Ada!"
    assert render("{{rows}}", v) == [1, 2]  # a lone placeholder keeps its type
    assert render("n={{rows}}", v) == "n=[1, 2]"
    assert render("{{row.Customer}}: {{rows[1]}}", v) == "Ada: 2"  # two placeholders stay text


# ---- engine with fakes -------------------------------------------------------------

def test_control_flow_desktop_and_macro(cfg):
    desk = FakeDesktop()
    macro_calls = []

    def fake_macro(cfg_, path, macro, inputs, outputs, timeout):
        macro_calls.append((path.name, macro, inputs, outputs))
        return {"completed": True, "outputs": {"Confirmation": f"OK-{inputs['Order']}"}}

    (cfg.roots[0] / "erp.pmc").write_text("[PMC Code v5.4.1]|||1||1|Post\n\n", encoding="utf-8")
    project = engine.new_project("t")
    project["flows"][0]["tree"] = [
        step("assign", var="Orders", value='["A-1", "A-2", "A-3"]', expression=True),
        step("assign", var="Posted", value="0", expression=True),
        {"t": "loop", "head": {"kind": "foreach", "items": "Orders", "item": "Order"}, "body": [
            {"t": "if", "branches": [{"head": {"kind": "if", "condition": "Order = 'A-3'"}, "body": [step("break")]}],
             "else": None, "end": None},
            step("run_macro", path="erp.pmc", macro="Post", inputs={"Order": "{{Order}}"}, outputs=["Confirmation"]),
            step("type_text", text="{{Confirmation}}", window="ERP"),
            step("assign", var="Posted", value="Posted + 1", expression=True),
        ]},
        {"t": "loop", "head": {"kind": "loop", "count": 2}, "body": [step("click", x="{{index * 10}}", y=5)]},
        step("file_append", text="posted {{Posted}}", path="log.txt"),
        step("sleep", ms=5, enabled=False),
    ]
    res = Engine(cfg, desktop_mod=desk, macro_runner=fake_macro).run(project, outputs=["Posted", "Confirmation"])
    assert res["completed"], res
    assert res["outputs"] == {"Posted": 2, "Confirmation": "OK-A-2"}
    assert [c[2]["Order"] for c in macro_calls] == ["A-1", "A-2"]
    assert [c for c in desk.calls if c[0] == "send"][1][2]["text"] == "OK-A-2"
    assert [c[1][:2] for c in desk.calls if c[0] == "click"] == [(10, 5), (20, 5)]
    assert (cfg.roots[0] / "log.txt").read_text() == "posted 2"


def test_errors_stop_with_step_path(cfg):
    p = engine.new_project()
    p["flows"][0]["tree"] = [step("log", message="a"), {"t": "loop", "head": {"kind": "loop", "count": 1}, "body": [
        step("win_wait", window="")]}]
    res = Engine(cfg, desktop_mod=FakeDesktop()).run(p)
    assert not res["completed"] and "step 2[1].1 (win_wait)" in res["error"]
    assert res["log"][0] == {"step": "1", "kind": "log", "ok": True, "ms": res["log"][0]["ms"], "note": "a"}


def test_failed_macro_and_desktop_errors_surface(cfg):
    (cfg.roots[0] / "m.pmc").write_text("[PMC Code v5]|||1||1|M\n\n", encoding="utf-8")
    p = engine.new_project()
    p["flows"][0]["tree"] = [step("run_macro", path="m.pmc")]
    res = Engine(cfg, desktop_mod=FakeDesktop(), macro_runner=lambda *a: {"completed": False, "note": "timed out", "outputs": {}}).run(p)
    assert not res["completed"] and "timed out" in res["error"]

    class Bad(FakeDesktop):
        def window_action(self, cfg, *a, **k):
            return {"ok": False, "error": "window not found"}
    p["flows"][0]["tree"] = [step("win_activate", window="Nope")]
    assert "window not found" in Engine(cfg, desktop_mod=Bad()).run(p)["error"]


def test_paths_confined(cfg):
    p = engine.new_project()
    p["flows"][0]["tree"] = [step("file_append", text="x", path="../escape.txt")]
    assert "outside the allowed" in Engine(cfg, desktop_mod=FakeDesktop()).run(p)["error"]


def test_timeout_and_stop(cfg):
    p = engine.new_project()
    p["flows"][0]["tree"] = [{"t": "loop", "head": {"kind": "loop", "count": 0}, "body": [step("sleep", ms=50)]}]
    res = Engine(cfg, desktop_mod=FakeDesktop()).run(p, timeout_s=0.3)
    assert res["timed_out"] and not res["completed"]
    eng = Engine(cfg, desktop_mod=FakeDesktop())
    threading.Timer(0.2, eng.stop).start()
    res = eng.run(p, timeout_s=30)
    assert res["error"] == "stopped by request" and res["seconds"] < 5


def test_file_round_trip_and_validation(tmp_path):
    p = engine.new_project("Demo")
    engine.dump(p, tmp_path / "a.rflow")
    assert engine.load(tmp_path / "a.rflow") == p
    with pytest.raises(FlowError):
        engine.loads('{"format": "something"}')
    assert engine.find_flow(p, "Main")[0] == 1


# ---- real browser ----------------------------------------------------------------

@pytest.fixture
def browser_ok():
    pytest.importorskip("playwright.sync_api")
    exe = os.environ.get("PMC_TEST_CHROMIUM")
    if exe:
        os.environ["PMC_BROWSER_EXECUTABLE"] = exe
    os.environ["PMC_BROWSER_HEADLESS"] = "1"
    from pmc_mcp.browser import BrowserSession

    b = BrowserSession()
    try:
        b.open("about:blank")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no browser: {e}")
    finally:
        b.close()


def test_browser_scrape_loop_and_desktop(cfg, browser_ok):
    site = cfg.roots[0] / "shop.html"
    site.write_text(SHOP, encoding="utf-8")
    desk = FakeDesktop()
    p = engine.new_project()
    p["flows"][0].update(inputs=["Limit"], outputs=["Big", "Searched"], tree=[
        step("browser_open", url=site.as_uri()),
        step("browser_fill", selector="#q", text="A-2", submit=True),
        step("browser_wait", text="Searched"),
        step("browser_extract", selector="#out", var="Searched"),
        step("browser_table", selector="#orders", var="Rows"),
        step("assign", var="Big", value="[]", expression=True),
        {"t": "loop", "head": {"kind": "foreach", "items": "Rows", "item": "Row"}, "body": [
            {"t": "if", "branches": [{"head": {"kind": "if", "condition": "Row.Total > Limit"}, "body": [
                step("assign", var="Big", value="Big + [Row.Order]", expression=True),
                step("type_text", text="{{Row.Customer}}: {{Row.Total}}", window="Notepad")]}],
             "else": None, "end": None}]},
        step("browser_screenshot", path="shot.png"),
    ])
    res = Engine(cfg, desktop_mod=desk).run(p, inputs={"Limit": "50"})
    assert res["completed"], res
    assert res["outputs"] == {"Big": ["A-2", "A-3"], "Searched": "Searched: A-2"}
    assert [c[2]["text"] for c in desk.calls if c[0] == "send"] == ["Grace: 99.00", "Linus: 250.00"]
    assert (cfg.roots[0] / "shot.png").read_bytes()[:4] == b"\x89PNG"


def test_browser_missing_element_fails_cleanly(cfg, browser_ok):
    site = cfg.roots[0] / "shop.html"
    site.write_text(SHOP, encoding="utf-8")
    p = engine.new_project()
    p["flows"][0]["tree"] = [step("browser_open", url=site.as_uri()), step("browser_wait", selector="#nope", timeout_s=1)]
    t = time.monotonic()
    res = Engine(cfg, desktop_mod=FakeDesktop()).run(p)
    assert not res["completed"] and "did not appear" in res["error"] and time.monotonic() - t < 30
