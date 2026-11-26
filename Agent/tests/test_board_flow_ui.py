"""Builds and runs an .rflow flow in the real board UI (Chromium). Skipped without Playwright/Chromium."""

import os
import re
import threading

import pytest

from pmc_mcp import board_server, engine
from pmc_mcp.bridge import BridgeClient
from pmc_mcp.config import Config

sync_api = pytest.importorskip("playwright.sync_api")

PAGE = """<!doctype html><title>Orders</title><table id="orders"><tr><th>Order</th><th>Total</th></tr>
<tr><td>A-1</td><td>12</td></tr><tr><td>A-2</td><td>99</td></tr><tr><td>A-3</td><td>250</td></tr></table>"""


@pytest.fixture
def board(tmp_path, monkeypatch):
    exe = os.environ.get("PMC_TEST_CHROMIUM")
    if exe:
        monkeypatch.setenv("PMC_BROWSER_EXECUTABLE", exe)
    monkeypatch.setenv("PMC_BROWSER_HEADLESS", "1")
    root = (tmp_path / "work").resolve()
    root.mkdir()
    (root / "orders.html").write_text(PAGE, encoding="utf-8")
    cfg = Config(roots=[root], audit_log=None)
    httpd, url = board_server.serve(cfg, open_browser=False, bridge=BridgeClient(directory=tmp_path / "nb", sender=lambda h, p: False))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=exe or None)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {e}")
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        yield pg, url, root
        assert errors == []
        b.close()
    httpd.shutdown()


def tile(pg, label):
    return pg.locator(".tile", has_text=re.compile("^." + re.escape(label) + "$"))


def test_build_and_run_flow(board):
    pg, url, root = board
    pg.goto(url)
    pg.once("dialog", lambda d: d.accept("Big orders"))
    pg.click("#newFile")
    pg.wait_for_selector('.tab.active:has-text("Main")')
    assert tile(pg, "Open web page").count() == 1 and tile(pg, "Run another macro").count() == 0  # flow palette
    # flow settings
    pg.fill("#f_flowInputs", "Limit")
    pg.fill("#f_flowOutputs", "Big")
    tile(pg, "Open web page").click(); pg.fill("#f_url", (root / "orders.html").as_uri())
    tile(pg, "Read table").click(); pg.fill("#f_selector", "#orders")
    tile(pg, "Set variable").click(); pg.fill("#f_var", "Big"); pg.fill("#f_value", "[]"); pg.check("#f_expression")
    tile(pg, "For each").click()                 # selected For each: next steps go inside
    tile(pg, "If / else").click(); pg.fill("#f_c0", "Row.Total > Limit")
    tile(pg, "Set variable").click(); pg.fill("#f_var", "Big"); pg.fill("#f_value", "Big + [Row.Order]"); pg.check("#f_expression")
    pg.keyboard.press("Control+s")
    pg.wait_for_selector("#dirty[hidden]", state="attached")
    proj = engine.load(root / "Big orders.rflow")
    steps = engine.tree_to_steps(proj["flows"][0]["tree"])
    assert [s["kind"] for s in steps] == ["browser_open", "browser_table", "assign", "foreach"]
    assert steps[3]["steps"][0]["kind"] == "if" and steps[3]["steps"][0]["condition"] == "Row.Total > Limit"
    assert proj["flows"][0]["inputs"] == ["Limit"]
    # run it from the board
    pg.click("#run")
    pg.fill("#runInputs", "Limit=50")
    pg.click("#runGo")
    pg.wait_for_selector("#runResult:has-text('Completed')", timeout=60000)
    text = pg.inner_text("#runResult")
    assert 'Big = ["A-2","A-3"]' in text and "browser_table" in text
