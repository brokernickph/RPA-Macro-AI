"""Browser automation (Playwright) for agents and flows.

Playwright's sync API is bound to the thread that started it, while MCP tools and
the board run on worker threads. ``BrowserSession`` therefore owns one dedicated
thread and runs every operation there through a queue.

Elements can be addressed by any Playwright selector (``#id``, ``text=Sign in``,
``role=button[name="Save"]``, CSS, XPath) or by a ``ref`` from ``snapshot()``
(e.g. ``e12``), which lists the page's interactive elements for an agent.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

_SNAPSHOT_JS = r"""
(limit) => {
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=option],[contenteditable=true],[onclick]';
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || st.visibility === 'hidden' || st.display === 'none') continue;
    const ref = 'e' + (++n);
    el.setAttribute('data-pmc-ref', ref);
    const label = el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('placeholder') || el.getAttribute('title') || '';
    out.push({
      ref, tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
      type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
      label: label.trim().slice(0, 80),
      text: (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 80),
      href: el.getAttribute('href') || '',
    });
    if (out.length >= limit) break;
  }
  return {title: document.title, url: location.href, elements: out,
          text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 4000)};
}
"""

_TABLE_JS = r"""
(t) => {
  const rows = [...t.querySelectorAll('tr')];
  if (!rows.length) return [];
  const cells = (r) => [...r.querySelectorAll('th,td')].map(c => c.innerText.trim());
  let header = cells(rows[0]);
  const hasTh = rows[0].querySelector('th') !== null;
  const body = hasTh ? rows.slice(1) : rows;
  if (!hasTh) header = header.map((_, i) => 'col' + (i + 1));
  return body.map(r => { const c = cells(r); const o = {}; header.forEach((h, i) => o[h || ('col' + (i + 1))] = c[i] ?? ''); return o; });
}
"""


class BrowserError(RuntimeError):
    pass


def _target(selector: str | None, ref: str | None) -> str:
    if ref:
        ref = ref.strip()
        if not ref.replace("e", "", 1).isdigit():
            raise BrowserError(f"bad ref {ref!r}: use a ref from browser_snapshot such as e12")
        return f'[data-pmc-ref="{ref}"]'
    if not selector:
        raise BrowserError("give a selector (CSS, text=..., role=...) or a ref from browser_snapshot")
    return selector


class BrowserSession:
    """One browser + page, driven from a private thread."""

    def __init__(self, headless: bool | None = None, channel: str | None = None, executable: str | None = None,
                 profile_dir: str | Path | None = None, allowed_file_roots: list[Path] | None = None):
        on_windows = sys.platform == "win32"
        env_headless = os.environ.get("PMC_BROWSER_HEADLESS")
        if headless is None:
            headless = (env_headless == "1") if env_headless is not None else not (on_windows or os.environ.get("DISPLAY"))
        self.headless = headless
        self.channel = channel or os.environ.get("PMC_BROWSER_CHANNEL") or ("msedge" if on_windows else None)
        self.executable = executable or os.environ.get("PMC_BROWSER_EXECUTABLE") or None
        prof = profile_dir or os.environ.get("PMC_BROWSER_PROFILE")
        self.profile_dir = Path(prof).expanduser() if prof else None
        self.allowed_file_roots = [Path(r).resolve() for r in (allowed_file_roots or [])]
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._pw = self._ctx = self._browser = self.page = None

    # ---- thread plumbing --------------------------------------------------
    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, fut = item
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except BaseException as e:  # noqa: BLE001
                    fut.set_exception(e)
        self._shutdown()

    def call(self, fn: Callable[[], Any], timeout: float = 120) -> Any:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="pmc-browser", daemon=True)
            self._thread.start()
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result(timeout=timeout)

    # ---- lifecycle (run on the browser thread) -----------------------------
    def _ensure(self):
        if self.page is not None and not self.page.is_closed():
            return self.page
        if self._pw is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise BrowserError("browser steps need Playwright: pip install playwright") from None
            self._pw = sync_playwright().start()
        opts: dict[str, Any] = {"headless": self.headless}
        if self.executable:
            opts["executable_path"] = self.executable
        elif self.channel:
            opts["channel"] = self.channel
        try:
            if self.profile_dir:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self._ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **opts)
                self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            else:
                self._browser = self._pw.chromium.launch(**opts)
                self._ctx = self._browser.new_context()
                self.page = self._ctx.new_page()
        except Exception as e:
            raise BrowserError(
                f"could not start the browser ({e}). On Windows Edge is used by default; otherwise run "
                "`playwright install chromium` or set PMC_BROWSER_EXECUTABLE / PMC_BROWSER_CHANNEL."
            ) from None
        self.page.set_default_timeout(15000)
        return self.page

    def _shutdown(self) -> None:
        for obj in (self._ctx, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._ctx = self._browser = self.page = None

    def close(self) -> None:
        if self._thread and self._thread.is_alive():
            self._q.put(None)
            self._thread.join(15)
        self._thread = None

    @property
    def is_open(self) -> bool:
        return self.page is not None

    # ---- operations ---------------------------------------------------------
    def _check_url(self, url: str) -> str:
        u = urlparse(url)
        if u.scheme in ("http", "https") or url == "about:blank":
            return url
        if u.scheme == "file":
            p = Path(u.path.lstrip("/") if sys.platform == "win32" else u.path).resolve()
            if any(p == r or r in p.parents for r in self.allowed_file_roots):
                return url
            raise BrowserError("file:// URLs are only allowed inside the macro directories")
        if not u.scheme and "." in url:
            return "https://" + url
        raise BrowserError(f"unsupported URL {url!r}: use http(s)://")

    def open(self, url: str) -> dict:
        url = self._check_url(url)

        def go():
            p = self._ensure()
            p.goto(url, wait_until="domcontentloaded")
            return {"url": p.url, "title": p.title()}
        return self.call(go)

    def snapshot(self, limit: int = 150) -> dict:
        return self.call(lambda: self._ensure().evaluate(_SNAPSHOT_JS, limit))

    def click(self, selector: str | None = None, ref: str | None = None, button: str = "left", count: int = 1) -> dict:
        t = _target(selector, ref)

        def do():
            p = self._ensure()
            p.locator(t).first.click(button=button.lower(), click_count=int(count))
            p.wait_for_load_state("domcontentloaded")
            return {"url": p.url, "title": p.title()}
        return self.call(do)

    def fill(self, text: str, selector: str | None = None, ref: str | None = None, submit: bool = False) -> dict:
        t = _target(selector, ref)

        def do():
            p = self._ensure()
            loc = p.locator(t).first
            loc.fill(str(text))
            if submit:
                loc.press("Enter")
                p.wait_for_load_state("domcontentloaded")
            return {"url": p.url}
        return self.call(do)

    def press(self, key: str, selector: str | None = None, ref: str | None = None) -> dict:
        def do():
            p = self._ensure()
            if selector or ref:
                p.locator(_target(selector, ref)).first.press(key)
            else:
                p.keyboard.press(key)
            return {"url": p.url}
        return self.call(do)

    def select(self, value: str, selector: str | None = None, ref: str | None = None) -> dict:
        t = _target(selector, ref)
        return self.call(lambda: {"selected": self._ensure().locator(t).first.select_option(str(value))})

    def wait(self, selector: str | None = None, text: str | None = None, timeout_s: float = 15, state: str = "visible") -> dict:
        def do():
            p = self._ensure()
            target = selector or (f"text={text}" if text else None)
            if not target:
                raise BrowserError("wait needs a selector or text")
            try:
                p.locator(target).first.wait_for(state=state, timeout=float(timeout_s) * 1000)
                return {"found": True}
            except Exception:  # noqa: BLE001
                return {"found": False}
        return self.call(do, timeout=float(timeout_s) + 30)

    def extract(self, selector: str | None = None, ref: str | None = None, attribute: str = "", all: bool = False) -> Any:
        t = _target(selector, ref)

        def do():
            loc = self._ensure().locator(t)
            items = loc.all() if all else [loc.first]
            vals = [(i.get_attribute(attribute) if attribute else i.inner_text()) or "" for i in items]
            return [v.strip() for v in vals] if all else (vals[0].strip() if vals else "")
        return self.call(do)

    def table(self, selector: str = "table") -> list[dict]:
        return self.call(lambda: self._ensure().locator(selector).first.evaluate(_TABLE_JS))

    def screenshot(self, path: str | None = None, full_page: bool = False) -> bytes:
        def do():
            data = self._ensure().screenshot(full_page=full_page, type="png")
            if path:
                Path(path).write_bytes(data)
            return data
        return self.call(do)

    def evaluate(self, script: str) -> Any:
        return self.call(lambda: self._ensure().evaluate(script))

    def info(self) -> dict:
        if self.page is None:
            return {"open": False}
        return self.call(lambda: {"open": True, "url": self.page.url, "title": self.page.title()})
