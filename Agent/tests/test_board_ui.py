"""Drives the real board UI in Chromium. Skipped when Playwright/Chromium are unavailable.

Set PMC_TEST_CHROMIUM to a Chrome/Chromium binary if Playwright's own browser isn't installed.
"""

import os
import re
import shutil
import threading
import time
from pathlib import Path

import pytest

from pmc_mcp import board_server, pmcfile, steps
from pmc_mcp.bridge import BridgeClient
from pmc_mcp.config import Config

sync_api = pytest.importorskip("playwright.sync_api")
EXAMPLES = Path(__file__).resolve().parents[2] / "Documentation/MacroCreator_Help-doc/Examples"


@pytest.fixture
def page(tmp_path):
    root = (tmp_path / "macros").resolve()
    root.mkdir()
    shutil.copy(EXAMPLES / "CompareVars.pmc", root / "CompareVars.pmc")
    cfg = Config(roots=[root], audit_log=None)
    httpd, url = board_server.serve(cfg, open_browser=False, file=str(root / "CompareVars.pmc"),
                                    bridge=BridgeClient(directory=tmp_path / "nb", sender=lambda h, p: False))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=os.environ.get("PMC_TEST_CHROMIUM") or None)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {e}")
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.goto(url)
        pg.wait_for_selector(".node")
        yield pg, root
        assert errors == []
        browser.close()
    httpd.shutdown()


def test_renders_blocks(page):
    pg, _ = page
    assert pg.locator(".block").count() == 3
    assert pg.locator(".branch-label", has_text="Else if").count() == 1


def test_add_edit_undo_save(page):
    pg, root = page
    pg.locator(".node").first.click()
    pg.locator(".tile", has_text=re.compile(r"^.Wait$")).click()
    pg.fill("#f_ms", "750")
    pg.locator(".tile", has_text="Press keys").drag_to(pg.locator(".slot").first)
    pg.locator(".node", has_text="Good Afternoon").click()
    pg.fill("#f_text", "Hi %A_UserName%, lunch?")
    pg.locator(".node", has_text="A_Hour > 11").click()
    pg.fill("#f_c0", "A_Hour >= 12")
    pg.locator(".canvas-wrap").click(position={"x": 5, "y": 300})
    pg.keyboard.press("Control+z")
    assert pg.locator(".node", has_text="A_Hour > 11").count() == 1
    pg.keyboard.press("Control+y")
    pg.keyboard.press("Control+s")
    pg.wait_for_selector("#dirty[hidden]", state="attached")
    time.sleep(0.3)
    m = pmcfile.load(root / "CompareVars.pmc").macros[0]
    acts = [(s.action, s.details, s.delay) for s in m.steps]
    assert acts[0] == ("{Enter}", "{Enter}", "10")
    assert acts[1][1] == "A_Hour >= 12"
    assert acts[2][1] == "Hi %A_UserName%`, lunch?"
    assert ("[Pause]", "", "750") in acts


def test_reloads_when_agent_edits_file(page):
    pg, root = page
    proj = pmcfile.load(root / "CompareVars.pmc")
    proj.macros[0].steps.insert(0, steps.sleep(4321))
    time.sleep(0.05)
    pmcfile.dump(proj, root / "CompareVars.pmc")
    pg.wait_for_selector('.node:has-text("4321 ms")', timeout=8000)
