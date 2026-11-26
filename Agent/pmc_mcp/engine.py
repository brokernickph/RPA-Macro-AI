"""Flow engine: runs .rflow automations that mix browser, desktop and PMC-macro steps.

An .rflow file is JSON::

    {"format": "rflow", "version": 1, "name": "Invoices",
     "flows": [{"name": "Main", "inputs": ["InvoiceNo"], "outputs": ["Total"], "tree": [...]}]}

``tree`` uses the same block format as the Visual Board (see flow.py): ``step``
nodes carry a ``spec`` ({"kind": ..., params}); ``if`` nodes have condition heads;
``loop`` nodes repeat ``count`` times or iterate ``foreach`` over a list.

In step parameters ``{{expr}}`` inserts a value: ``{{InvoiceNo}}``, ``{{row.Total}}``,
``{{rows[0]["Customer"]}}``. Conditions and expressions use a small, safe language
(comparisons, and/or/not, + - * /, len/int/float/str/round/min/max, string methods);
undefined names are empty strings, and a single ``=`` means equality as in PMC.
"""

from __future__ import annotations

import ast
import json
import operator
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import pmcfile, runner
from .config import Config

FORMAT = "rflow"
SUFFIX = ".rflow"


class FlowError(RuntimeError):
    pass


class _Break(Exception):
    pass


class Stopped(Exception):
    pass


# ---- files -------------------------------------------------------------------


def new_project(name: str = "Automation") -> dict:
    return {"format": FORMAT, "version": 1, "name": name, "flows": [{"name": "Main", "inputs": [], "outputs": [], "tree": []}]}


def loads(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise FlowError('not an .rflow file (expected {"format": "rflow", ...})')
    if not isinstance(data.get("flows"), list) or not data["flows"]:
        raise FlowError("an .rflow file needs at least one flow")
    for i, f in enumerate(data["flows"], 1):
        f.setdefault("name", f"Flow{i}")
        f.setdefault("inputs", [])
        f.setdefault("outputs", [])
        f.setdefault("tree", [])
    return data


def load(path: str | Path) -> dict:
    return loads(Path(path).read_text(encoding="utf-8-sig"))


def dump(project: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(project, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def find_flow(project: dict, flow: int | str | None) -> tuple[int, dict]:
    flows = project["flows"]
    if flow in (None, ""):
        return 1, flows[0]
    if isinstance(flow, int) or str(flow).isdigit():
        i = int(flow)
        if not 1 <= i <= len(flows):
            raise KeyError(f"flow index {i} out of range 1-{len(flows)}")
        return i, flows[i - 1]
    for i, f in enumerate(flows, 1):
        if f["name"] == flow:
            return i, f
    raise KeyError(f"flow not found: {flow!r}; available: {[f['name'] for f in flows]}")


# ---- safe expressions -----------------------------------------------------------

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
        ast.GtE: operator.ge, ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
_FUNCS = {"len": len, "int": int, "float": float, "str": str, "round": round, "abs": abs, "min": min, "max": max,
          "sum": sum, "bool": bool, "lower": lambda s: str(s).lower(), "upper": lambda s: str(s).upper()}
_STR_METHODS = {"lower", "upper", "strip", "lstrip", "rstrip", "startswith", "endswith", "replace", "split", "join",
                "find", "count", "title", "isdigit", "zfill"}
_LIST_METHODS = {"index", "count"}
_DICT_METHODS = {"get", "keys", "values", "items"}


def _num(v: Any) -> Any:
    """Treat numeric text as a number ('12' == 12), as AutoHotkey does."""
    if isinstance(v, str):
        t = v.strip()
        try:
            return float(t) if any(c in t for c in ".eE") else int(t)
        except ValueError:
            return v
    return v


_STRING = re.compile(r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")


def _ahkish(expr: str) -> str:
    """AHK habits -> Python, outside string literals: = is equality, <> is !=, && / ||."""
    parts = _STRING.split(expr)
    for i in range(0, len(parts), 2):  # even parts are outside quotes
        t = parts[i].replace("&&", " and ").replace("||", " or ").replace("<>", "!=")
        parts[i] = re.sub(r"(?<![=!<>])=(?!=)", "==", t)
    return "".join(parts)


def evaluate(expr: str, variables: dict[str, Any]) -> Any:
    expr = str(expr).strip()
    if not expr:
        return ""
    try:
        tree = ast.parse(_ahkish(expr), mode="eval")
    except SyntaxError as e:
        raise FlowError(f"cannot read expression {expr!r}: {e.msg}") from None
    return _ev(tree.body, variables)


def _ev(n: ast.AST, v: dict[str, Any]) -> Any:  # noqa: C901 (a small interpreter)
    if isinstance(n, ast.Constant):
        return n.value
    if isinstance(n, ast.Name):
        if n.id in ("true", "True"):
            return True
        if n.id in ("false", "False"):
            return False
        if n.id in ("none", "None", "null"):
            return None
        return v.get(n.id, "")
    if isinstance(n, ast.BoolOp):
        vals = (_ev(x, v) for x in n.values)
        if isinstance(n.op, ast.And):
            r: Any = True
            for x in vals:
                r = x
                if not x:
                    return x
            return r
        r = False
        for x in vals:
            r = x
            if x:
                return x
        return r
    if isinstance(n, ast.UnaryOp):
        x = _ev(n.operand, v)
        if isinstance(n.op, ast.Not):
            return not x
        if isinstance(n.op, ast.USub):
            return -_num(x)
        if isinstance(n.op, ast.UAdd):
            return +_num(x)
    if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
        a, b = _ev(n.left, v), _ev(n.right, v)
        if isinstance(n.op, ast.Add) and isinstance(a, list) and isinstance(b, list):
            return a + b
        na, nb = _num(a), _num(b)
        numeric = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (na, nb))
        if isinstance(n.op, ast.Add) and not numeric:
            return f"{a}{b}"  # text joins, as in AutoHotkey
        if not numeric:
            raise FlowError(f"cannot do arithmetic on {a!r} and {b!r}")
        return _BIN[type(n.op)](na, nb)
    if isinstance(n, ast.Compare):
        left = _ev(n.left, v)
        for op, right_n in zip(n.ops, n.comparators):
            right = _ev(right_n, v)
            if type(op) not in _CMP:
                raise FlowError("unsupported comparison")
            a, b = (left, right) if isinstance(op, (ast.In, ast.NotIn)) else (_num(left), _num(right))
            try:
                ok = _CMP[type(op)](a, b)
            except TypeError:
                ok = _CMP[type(op)](str(a), str(b))
            if not ok:
                return False
            left = right
        return True
    if isinstance(n, ast.IfExp):
        return _ev(n.body, v) if _ev(n.test, v) else _ev(n.orelse, v)
    if isinstance(n, (ast.List, ast.Tuple)):
        return [_ev(x, v) for x in n.elts]
    if isinstance(n, ast.Dict):
        return {_ev(k, v): _ev(x, v) for k, x in zip(n.keys, n.values) if k is not None}
    if isinstance(n, ast.Subscript):
        obj, key = _ev(n.value, v), _ev(n.slice, v)
        try:
            return obj[int(key) if isinstance(obj, (list, str)) else key]
        except (KeyError, IndexError, TypeError, ValueError):
            return ""
    if isinstance(n, ast.Attribute):
        obj = _ev(n.value, v)
        if isinstance(obj, dict):
            return obj.get(n.attr, "")
        raise FlowError(f"'.{n.attr}' only works on records (e.g. a table row)")
    if isinstance(n, ast.Call) and not n.keywords:
        args = [_ev(a, v) for a in n.args]
        if isinstance(n.func, ast.Name) and n.func.id in _FUNCS:
            return _FUNCS[n.func.id](*args)
        if isinstance(n.func, ast.Attribute):
            obj, name = _ev(n.func.value, v), n.func.attr
            if (isinstance(obj, str) and name in _STR_METHODS) or (isinstance(obj, list) and name in _LIST_METHODS) \
                    or (isinstance(obj, dict) and name in _DICT_METHODS):
                res = getattr(obj, name)(*args)
                return list(res) if name in ("keys", "values", "items") else res
        raise FlowError("unsupported function call in expression")
    raise FlowError(f"unsupported expression element: {type(n).__name__}")


_TPL = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def render(value: Any, variables: dict[str, Any]) -> Any:
    """Fill {{expr}} placeholders. A string that is exactly one placeholder keeps the value's type."""
    if not isinstance(value, str) or "{{" not in value:
        return value
    whole = _TPL.fullmatch(value.strip()) if value.count("{{") == 1 else None
    if whole:
        return evaluate(whole.group(1), variables)

    def sub(m: re.Match) -> str:
        r = evaluate(m.group(1), variables)
        return json.dumps(r) if isinstance(r, (list, dict)) else ("" if r is None else str(r))
    return _TPL.sub(sub, value)


# ---- engine -----------------------------------------------------------------------

_NO_RENDER = {"condition", "items"}


class Engine:
    """Runs one flow. Desktop and browser back-ends are injectable for testing."""

    def __init__(self, cfg: Config, desktop_mod: Any = None, browser_factory: Callable[[], Any] | None = None,
                 macro_runner: Callable[..., dict] | None = None, stop_event: threading.Event | None = None,
                 on_step: Callable[[dict], None] | None = None):
        if desktop_mod is None:
            from . import desktop as desktop_mod  # noqa: PLW0127
        self.cfg = cfg
        self.desktop = desktop_mod
        self.browser_factory = browser_factory or self._default_browser
        self.macro_runner = macro_runner or runner.run_macro_headless
        self.stop_event = stop_event or threading.Event()
        self.on_step = on_step
        self.browser = None
        self.vars: dict[str, Any] = {}
        self.log: list[dict] = []
        self.deadline = float("inf")

    def _default_browser(self):
        from .browser import BrowserSession

        return BrowserSession(allowed_file_roots=self.cfg.roots)  # PMC_BROWSER_* env vars apply

    # ---- public ------------------------------------------------------------
    def run(self, project: dict, flow: int | str | None = None, inputs: dict[str, Any] | None = None,
            outputs: list[str] | None = None, timeout_s: float | None = None, keep_browser: bool = False) -> dict:
        idx, f = find_flow(project, flow)
        self.vars = {"index": 0}
        for name in f.get("inputs") or []:
            self.vars.setdefault(name, "")
        self.vars.update(inputs or {})
        wanted = list(outputs) if outputs else list(f.get("outputs") or [])
        start = time.monotonic()
        self.deadline = start + float(timeout_s or self.cfg.default_timeout)
        error, completed, timed_out = None, False, False
        try:
            self._block(f.get("tree") or [], "")
            completed = True
        except _Break:
            completed = True
        except Stopped as e:
            timed_out = "timed out" in str(e)
            error = str(e)
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
        finally:
            if self.browser is not None and not keep_browser:
                try:
                    self.browser.close()
                except Exception:  # noqa: BLE001
                    pass
                self.browser = None
        return {
            "flow": {"index": idx, "name": f["name"]},
            "completed": completed,
            "timed_out": timed_out,
            "error": error,
            "seconds": round(time.monotonic() - start, 2),
            "outputs": {k: self.vars.get(k) for k in wanted},
            "log": self.log[-500:],
        }

    def stop(self) -> None:
        self.stop_event.set()

    # ---- tree walking --------------------------------------------------------
    def _check(self) -> None:
        if self.stop_event.is_set():
            raise Stopped("stopped by request")
        if time.monotonic() > self.deadline:
            raise Stopped("timed out")

    def _block(self, nodes: list[dict], path: str) -> None:
        for i, node in enumerate(nodes, 1):
            self._check()
            p = f"{path}{i}"
            t = node.get("t")
            if t == "step":
                self._step(node, p)
            elif t == "if":
                self._if(node, p)
            elif t == "loop":
                self._loop(node, p)
            else:
                raise FlowError(f"step {p}: unknown node type {t!r}")

    def _cond(self, head: dict | None) -> bool:
        if not head:
            return False
        return bool(evaluate(head.get("condition", head.get("details", "")), self.vars))

    def _if(self, node: dict, p: str) -> None:
        for br in node.get("branches") or []:
            if self._cond(br.get("head")):
                self._record(p, "if", True, 0, "branch taken")
                return self._block(br.get("body") or [], p + ".")
        if node.get("else"):
            self._record(p, "if", True, 0, "else")
            self._block(node["else"].get("body") or [], p + ".")

    def _loop(self, node: dict, p: str) -> None:
        head = node.get("head") or {}
        body = node.get("body") or []
        outer_index = self.vars.get("index", 0)
        try:
            if head.get("kind") == "foreach":
                items = evaluate(head.get("items", ""), self.vars)
                if isinstance(items, dict):
                    items = list(items.items())
                elif isinstance(items, str):
                    items = [x for x in items.splitlines() if x.strip()] if items else []
                item_var = pmcfile.check_var_name(head.get("item") or "item")
                for n, item in enumerate(items, 1):
                    self.vars.update({item_var: item, "index": n})
                    self._block(body, f"{p}[{n}].")
            else:
                count = int(_num(render(str(head.get("count", head.get("repeat", 0))), self.vars)) or 0)
                n = 0
                while count <= 0 or n < count:
                    n += 1
                    self.vars["index"] = n
                    self._block(body, f"{p}[{n}].")
        except _Break:
            pass
        finally:
            self.vars["index"] = outer_index

    def _record(self, p: str, kind: str, ok: bool, ms: float, note: str = "") -> None:
        entry = {"step": p, "kind": kind, "ok": ok, "ms": round(ms), "note": str(note)[:300]}
        self.log.append(entry)
        if self.on_step:
            self.on_step(entry)

    def _step(self, node: dict, p: str) -> None:
        spec = node.get("spec")
        if not spec:
            raise FlowError(f"step {p}: flows need steps with a 'spec' (raw PMC rows belong in .pmc macros)")
        if spec.get("enabled") is False:
            return
        kind = spec.get("kind")
        handler = getattr(self, "_k_" + str(kind), None)
        if handler is None:
            raise FlowError(f"step {p}: '{kind}' can't run in a flow")
        params = {k: (val if k in _NO_RENDER else render(val, self.vars))
                  for k, val in spec.items() if k not in ("kind", "enabled", "note")}
        t0 = time.monotonic()
        try:
            note = handler(**params)
        except (_Break, Stopped):
            raise
        except TypeError as e:
            self._record(p, kind, False, (time.monotonic() - t0) * 1000, str(e))
            raise FlowError(f"step {p} ({kind}): bad parameters: {e}") from None
        except Exception as e:
            self._record(p, kind, False, (time.monotonic() - t0) * 1000, str(e))
            raise FlowError(f"step {p} ({kind}): {e}") from None
        self._record(p, kind, True, (time.monotonic() - t0) * 1000, note or "")

    def _desk_ok(self, res: dict, what: str) -> str:
        if isinstance(res, dict) and res.get("ok") is False:
            raise FlowError(res.get("error") or f"{what} failed")
        return ""

    def _set(self, var: str, value: Any) -> None:
        self.vars[pmcfile.check_var_name(var)] = value

    def _b(self):
        if self.browser is None:
            self.browser = self.browser_factory()
        return self.browser

    def _path(self, path: str) -> Path:
        return self.cfg.safe_path(str(path), suffix=None)

    # ---- step kinds: general -----------------------------------------------------
    def _k_sleep(self, ms=1000):
        end = time.monotonic() + float(ms) / 1000
        while time.monotonic() < end:
            self._check()
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))

    def _k_assign(self, var, value="", expression=False, literal=False):
        self._set(var, evaluate(value, self.vars) if expression and isinstance(value, str) else value)
        return f"{var} = {str(self.vars[var])[:80]}"

    def _k_log(self, message=""):
        return str(message)

    def _k_comment(self, text=""):
        return ""

    def _k_break(self):
        raise _Break()

    def _k_file_append(self, text="", path="", encoding="UTF-8"):
        p = self._path(path)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(str(text))
        return str(p)

    # ---- desktop --------------------------------------------------------------
    def _k_click(self, x=0, y=0, button="Left", count=1, delay=None, relative_to="screen", window=""):
        return self._desk_ok(self.desktop.click(self.cfg, int(_num(x)), int(_num(y)), button, int(_num(count)), relative_to, window), "click")

    def _k_mouse_move(self, x=0, y=0, delay=None):
        return self._desk_ok(self.desktop.mouse_move(self.cfg, int(_num(x)), int(_num(y))), "mouse move")

    def _k_type_text(self, text="", window="", control="", set_text=False):
        return self._desk_ok(self.desktop.send(self.cfg, text=str(text), window=window, control=control), "type text")

    def _k_send_keys(self, keys="", window="", control="", delay=None, label=None):
        return self._desk_ok(self.desktop.send(self.cfg, keys=str(keys), window=window, control=control), "send keys")

    def _k_run(self, target="", args="", working_dir="", pid_var=""):
        res = self.desktop.run_program(self.cfg, str(target), str(args), str(working_dir))
        self._desk_ok(res, "run")
        if pid_var:
            self._set(pid_var, res.get("pid"))
        return str(target)

    def _win(self, window, action, timeout_s=10):
        if not window:
            raise FlowError("needs a window title")
        res = self.desktop.window_action(self.cfg, window, action, timeout_s=int(_num(timeout_s) or 10))
        if res.get("ok") is False:
            raise FlowError(res.get("error") or f"window '{window}' not found within {timeout_s}s")
        return window

    def _k_win_wait(self, window="", timeout_s=30):
        return self._win(window, "wait", timeout_s)

    def _k_win_activate(self, window=""):
        return self._win(window, "activate")

    def _k_win_close(self, window=""):
        return self._win(window, "close")

    def _k_msgbox(self, text="", title="", options=0):
        return self._desk_ok(self.desktop.msgbox(self.cfg, str(text), str(title)), "message")

    def _k_command(self, name="", params="", target="", window="", delay=None):
        return self._desk_ok(self.desktop.ahk_command(self.cfg, name, params, target, window), name)

    # ---- PMC macros ------------------------------------------------------------
    def _k_run_macro(self, path="", macro="", inputs=None, outputs=None, timeout_s=0):
        p = self.cfg.safe_path(str(path), must_exist=True)
        if isinstance(inputs, str):  # "Name=value" lines, as typed in the board
            inputs = dict(line.split("=", 1) for line in inputs.splitlines() if "=" in line)
        ins = {k.strip(): render(v, self.vars) for k, v in (inputs or {}).items()} if isinstance(inputs, dict) else {}
        outs = outputs if isinstance(outputs, list) else [o.strip() for o in str(outputs or "").split(",") if o.strip()]
        remaining = max(1.0, self.deadline - time.monotonic())
        res = self.macro_runner(self.cfg, p, macro or None, ins, outs, min(remaining, float(timeout_s or remaining)))
        for k, v in (res.get("outputs") or {}).items():
            self._set(k, v)
        if not res.get("completed"):
            raise FlowError(res.get("note") or f"macro {macro or 1} of {p.name} did not complete")
        return f"{p.name} → {', '.join(outs) or 'done'}"

    # ---- browser -------------------------------------------------------------
    def _k_browser_open(self, url=""):
        r = self._b().open(str(url))
        return r.get("url", "")

    def _k_browser_click(self, selector=""):
        return self._b().click(selector=selector).get("url", "")

    def _k_browser_fill(self, selector="", text="", submit=False):
        self._b().fill(str(text), selector=selector, submit=bool(submit))
        return selector

    def _k_browser_press(self, key="Enter", selector=""):
        self._b().press(key, selector=selector or None)
        return key

    def _k_browser_select(self, selector="", value=""):
        self._b().select(str(value), selector=selector)
        return str(value)

    def _k_browser_wait(self, selector="", text="", timeout_s=15, var=""):
        found = self._b().wait(selector=selector or None, text=text or None, timeout_s=float(_num(timeout_s) or 15))["found"]
        if var:
            self._set(var, found)
        elif not found:
            raise FlowError(f"'{selector or text}' did not appear within {timeout_s}s")
        return f"found={found}"

    def _k_browser_extract(self, selector="", var="", attribute="", all=False):
        val = self._b().extract(selector=selector, attribute=attribute, all=bool(all))
        self._set(var or "extracted", val)
        return f"{var} = {str(val)[:80]}"

    def _k_browser_table(self, selector="table", var="rows"):
        rows = self._b().table(selector or "table")
        self._set(var or "rows", rows)
        return f"{len(rows)} rows → {var}"

    def _k_browser_screenshot(self, path=""):
        p = self._path(path)
        self._b().screenshot(str(p))
        return str(p)

    def _k_browser_close(self):
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        return ""




# ---- compact authoring form (for agents) ---------------------------------------
#   [{"kind": "browser_open", "url": "..."},
#    {"kind": "if", "condition": "x > 1", "then": [...], "elif": [{"condition": "...", "then": [...]}], "else": [...]},
#    {"kind": "loop", "count": 3, "steps": [...]},
#    {"kind": "foreach", "items": "Rows", "item": "Row", "steps": [...]}]

KINDS = sorted(n[3:] for n in dir(Engine) if n.startswith("_k_"))


def steps_to_tree(steps: list[dict]) -> list[dict]:
    tree = []
    for i, s in enumerate(steps or [], 1):
        if not isinstance(s, dict) or "kind" not in s:
            raise FlowError(f"step {i}: each step needs a 'kind'")
        s = dict(s)
        kind = s.pop("kind")
        if kind == "if":
            branches = [{"head": {"kind": "if", "condition": s.get("condition", "")}, "body": steps_to_tree(s.get("then", []))}]
            for e in s.get("elif") or []:
                branches.append({"head": {"kind": "if", "condition": e.get("condition", "")}, "body": steps_to_tree(e.get("then", []))})
            els = {"head": None, "body": steps_to_tree(s["else"])} if s.get("else") is not None else None
            tree.append({"t": "if", "branches": branches, "else": els, "end": None})
        elif kind in ("loop", "foreach"):
            head = {"kind": kind, **{k: v for k, v in s.items() if k != "steps"}}
            if kind == "foreach" and not head.get("items"):
                raise FlowError(f"step {i}: foreach needs 'items' (an expression such as Rows)")
            tree.append({"t": "loop", "head": head, "body": steps_to_tree(s.get("steps", [])), "end": None})
        elif kind in KINDS:
            tree.append({"t": "step", "spec": {"kind": kind, **s}})
        else:
            raise FlowError(f"step {i}: unknown kind {kind!r}; valid: {KINDS + ['if', 'loop', 'foreach']}")
    return tree


def tree_to_steps(tree: list[dict]) -> list[dict]:
    out = []
    for n in tree or []:
        if n.get("t") == "step":
            out.append(dict(n.get("spec") or {"kind": "raw", **(n.get("step") or {})}))
        elif n.get("t") == "if":
            brs = n.get("branches") or []
            cond = lambda h: (h or {}).get("condition", (h or {}).get("details", ""))  # noqa: E731
            d = {"kind": "if", "condition": cond(brs[0]["head"]) if brs else "", "then": tree_to_steps(brs[0]["body"]) if brs else []}
            if len(brs) > 1:
                d["elif"] = [{"condition": cond(b["head"]), "then": tree_to_steps(b["body"])} for b in brs[1:]]
            if n.get("else") is not None:
                d["else"] = tree_to_steps(n["else"].get("body"))
            out.append(d)
        elif n.get("t") == "loop":
            head = dict(n.get("head") or {"kind": "loop"})
            kind = head.pop("kind", "loop")
            out.append({"kind": "foreach" if kind == "foreach" else "loop", **head, "steps": tree_to_steps(n.get("body"))})
    return out


STEP_KINDS_DOC = """Flow steps ({"kind": ..., params}); text params accept {{expressions}} such as {{Row.Total}}.
Browser: browser_open url | browser_click selector | browser_fill selector, text, [submit] | browser_press key, [selector]
  | browser_select selector, value | browser_wait selector or text, [timeout_s], [var: store found=true/false instead of failing]
  | browser_extract selector, var, [attribute], [all] | browser_table selector, var (list of row records) | browser_screenshot path | browser_close
  Selectors: CSS (#id, .class), text=Sign in, role=button[name="Save"], or xpath=...
Desktop (Windows): click x, y, [button, count, relative_to=screen|window|client, window] | mouse_move x, y | type_text text, [window, control]
  | send_keys keys (AHK syntax: ^s {Enter} !{F4}), [window, control] | run target, [args, working_dir, pid_var]
  | win_wait window, [timeout_s] | win_activate window | win_close window | msgbox text, [title] | command name, params (any AHK v1 command)
PMC macros: run_macro path (.pmc), [macro], [inputs {Name: "{{Var}}"}], [outputs [names] -> become flow variables], [timeout_s]
Data & control: assign var, value, [expression] | log message | file_append text, path | sleep ms | comment text | break
Blocks: {"kind": "if", "condition": "Total > 100", "then": [...], "elif": [{"condition": ..., "then": [...]}], "else": [...]}
  {"kind": "loop", "count": 3, "steps": [...]} (count 0 = until break; {{index}} is 1-based)
  {"kind": "foreach", "items": "Rows", "item": "Row", "steps": [...]}
Conditions/expressions: Total > 100 and Status = "Paid", len(Rows) > 0, Name.lower().startswith("inv"), Row.Customer
  (single = is equality; undefined names are ""). Every step may have "enabled": false and a "note"."""
