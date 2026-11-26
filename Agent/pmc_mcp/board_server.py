"""Local web server for the PMC Visual Board.

Serves the single-page board (pmc_mcp/board/) and a small JSON API over .pmc files,
the headless runner and the live-PMC bridge. Security: listens on 127.0.0.1 only,
every /api call needs the per-launch token (X-Board-Token header) and a localhost
Host header, so other web pages open in the browser cannot drive it.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import secrets
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import flow, pmcfile, runner
from .bridge import BridgeClient
from .config import Config

STATIC = Path(__file__).with_name("board")
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Board:
    """The API, independent of HTTP so it can be tested directly."""

    def __init__(self, cfg: Config, bridge: BridgeClient | None = None):
        self.cfg = cfg
        self.bridge = bridge or BridgeClient()
        self._lock = threading.Lock()

    # ---- files -------------------------------------------------------------
    def files(self) -> dict:
        out = []
        for root in self.cfg.roots:
            if root.is_dir():
                for p in sorted(root.rglob("*.pmc")):
                    out.append({"path": str(p), "name": str(p.relative_to(root)), "mtime": p.stat().st_mtime})
        return {"roots": [str(r) for r in self.cfg.roots], "files": out}

    def project(self, path: str) -> dict:
        p = self.cfg.safe_path(path, must_exist=True)
        proj = pmcfile.load(p)
        macros = []
        for i, m in enumerate(proj.macros, 1):
            tree, structured = flow.to_tree(m.steps)
            _annotate(tree)
            macros.append({"index": i, "name": m.name, "hotkey": m.hotkey, "repeat": m.repeat,
                           "is_function": m.is_function, "structured": structured, "tree": tree})
        return {"path": str(p), "mtime": p.stat().st_mtime, "macros": macros}

    def save(self, body: dict) -> dict:
        self.cfg.require("standard")
        p = self.cfg.safe_path(body.get("path", ""))
        with self._lock:
            if p.exists():
                if body.get("create"):
                    raise ApiError(HTTPStatus.CONFLICT, f"{p.name} already exists")
                old = pmcfile.load(p)
                mtime = body.get("mtime")
                if mtime is not None and abs(p.stat().st_mtime - float(mtime)) > 1e-3 and not body.get("force"):
                    raise ApiError(HTTPStatus.CONFLICT, "the file changed on disk since you opened it (PMC or an agent edited it); reload first")
            else:
                old = pmcfile.Project()
            new = pmcfile.Project(globals_line=old.globals_line)
            for spec in body.get("macros") or []:
                idx = spec.get("index")
                base = old.macros[idx - 1] if isinstance(idx, int) and 1 <= idx <= len(old.macros) else None
                m = base if base is not None else pmcfile.Macro(groups="", context=None)
                m.name = str(spec.get("name") or m.name)
                m.hotkey = str(spec.get("hotkey", m.hotkey) or "")
                m.repeat = str(spec.get("repeat", m.repeat) or "1")
                try:
                    m.steps = flow.to_rows(spec.get("tree") or [])
                except (ValueError, TypeError) as e:
                    raise ApiError(HTTPStatus.BAD_REQUEST, f"macro {m.name}: {e}") from None
                new.macros.append(m)
            if not new.macros:
                raise ApiError(HTTPStatus.BAD_REQUEST, "a project needs at least one macro")
            p.parent.mkdir(parents=True, exist_ok=True)
            pmcfile.dump(new, p)
        self.cfg.audit("board_save", path=str(p), macros=len(new.macros))
        return self.project(str(p))

    # ---- run ---------------------------------------------------------------
    def run(self, body: dict) -> dict:
        self.cfg.require("standard")
        p = self.cfg.safe_path(body.get("path", ""), must_exist=True)
        self.cfg.audit("board_run", path=str(p), macro=body.get("macro"))
        return runner.run_macro_headless(
            self.cfg, p, body.get("macro") or None, body.get("inputs") or {}, body.get("outputs") or [],
            float(body.get("timeout_s") or self.cfg.default_timeout),
        )

    def pmc(self, action: str, body: dict) -> Any:
        if action == "status":
            info = self.bridge.info()
            return {"running": False} if not info else {"running": True, **self.bridge.call("status")}
        self.cfg.require("standard")
        self.cfg.audit(f"board_pmc_{action}", **body)
        if action == "open":
            return self.bridge.call("open", path=str(self.cfg.safe_path(body["path"], must_exist=True)),
                                    discard_changes=bool(body.get("discard_changes")))
        if action == "play":
            return self.bridge.call("play", macro=body.get("macro") or None)
        if action == "stop":
            return self.bridge.call("stop")
        raise ApiError(HTTPStatus.NOT_FOUND, f"unknown action {action}")


def _annotate(tree: list[dict]) -> None:
    """Attach a friendly spec to each leaf the inspector can edit as a form."""
    for node in tree:
        if node["t"] == "step":
            if spec := flow.describe(node["step"]):
                node["view"] = spec
        elif node["t"] == "if":
            for br in node["branches"]:
                _annotate(br["body"])
            if node["else"]:
                _annotate(node["else"]["body"])
        elif node["t"] == "loop":
            _annotate(node["body"])


def make_handler(board: Board, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PMCBoard/1"

        def handle_one_request(self):
            self.server.last_request = time.monotonic()  # type: ignore[attr-defined]
            super().handle_one_request()

        def log_message(self, fmt, *args):  # quiet
            pass

        # -- plumbing --
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj, default=str).encode(), "application/json")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in _ALLOWED_HOSTS

        def _api(self, method: str) -> None:
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            if not secrets.compare_digest(self.headers.get("X-Board-Token", ""), token):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "missing or wrong board token"})
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            body = {}
            if method in ("POST", "PUT"):
                n = int(self.headers.get("Content-Length") or 0)
                if n > 8 * 1024 * 1024:
                    return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            route = url.path[len("/api/"):]
            try:
                if method == "GET" and route == "palette":
                    res = {"palette": flow.PALETTE, "level": board.cfg.level}
                elif method == "GET" and route == "files":
                    res = board.files()
                elif method == "GET" and route == "project":
                    res = board.project(q.get("path", ""))
                elif method == "PUT" and route == "project":
                    res = board.save(body)
                elif method == "POST" and route == "run":
                    res = board.run(body)
                elif route.startswith("pmc/"):
                    res = board.pmc(route[4:], body)
                else:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such endpoint"})
            except ApiError as e:
                return self._json(e.status, {"error": str(e)})
            except PermissionError as e:
                return self._json(HTTPStatus.FORBIDDEN, {"error": str(e)})
            except (FileNotFoundError, KeyError) as e:
                return self._json(HTTPStatus.NOT_FOUND, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(e).__name__}: {e}"})
            self._json(HTTPStatus.OK, res)

        def _static(self) -> None:
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, b"bad host", "text/plain")
            rel = urlparse(self.path).path.lstrip("/") or "index.html"
            f = (STATIC / rel).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") or "javascript" in ctype else ""))

        def do_GET(self):
            self._api("GET") if self.path.startswith("/api/") else self._static()

        def do_POST(self):
            self._api("POST")

        def do_PUT(self):
            self._api("PUT")

    return Handler


def serve(cfg: Config, port: int = 0, open_browser: bool = True, file: str | None = None,
          bridge: BridgeClient | None = None, token: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    token = token or secrets.token_urlsafe(24)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Board(cfg, bridge), token))
    httpd.last_request = time.monotonic()  # type: ignore[attr-defined]
    url = f"http://127.0.0.1:{httpd.server_address[1]}/#t={token}"
    if file:
        url += "&f=" + str(cfg.safe_path(file, must_exist=True)).replace("&", "%26")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    return httpd, url


def main() -> None:
    ap = argparse.ArgumentParser(description="Open the PMC Visual Board in your browser")
    ap.add_argument("file", nargs="?", help=".pmc file to open")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--idle-exit", type=int, default=120, metavar="SECONDS",
                    help="exit after this long without requests, i.e. once the board tab is closed (0 = never)")
    a = ap.parse_args()
    cfg = Config.from_env()
    if a.file:
        # A file the user opened explicitly (e.g. from PMC's File menu): allow its folder this session.
        folder = Path(a.file).expanduser().resolve().parent
        if not any(folder == r or r in folder.parents for r in cfg.roots):
            cfg.roots.append(folder)
    httpd, url = serve(cfg, a.port, not a.no_browser, a.file)
    print(f"PMC Visual Board: {url}\nPress Ctrl+C to stop.", flush=True)
    if a.idle_exit > 0:
        def watchdog():
            while time.monotonic() - httpd.last_request < a.idle_exit:  # type: ignore[attr-defined]
                time.sleep(5)
            httpd.shutdown()

        threading.Thread(target=watchdog, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
