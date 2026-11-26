"""Read and write Pulover's Macro Creator project files (.pmc).

The format (as written by ``SaveProject`` in LIB/Internal.ahk and read by
``PMC.Load`` in LIB/Class_PMC.ahk) is line based, UTF-8 with BOM::

    [PMC Globals]|<#If context>|<#If window>|<export icon>        (optional)
    [PMC Code v5.4.1]|<hotkey>|<manual key>|<repeat>|<modes>|<on finish>|<name>
    Context=<condition>|<context>                                  (optional)
    Groups=<name>:<row>,...
    1|<Action>|<Details>|<Repeat>|<Delay>|<Type>|<Target>|<Window>|<Comment>|<Color>|<CodeLine>|
    2|...
    <blank line ends the macro>

A row whose first field starts with ``0`` is disabled (unchecked). A literal ``|``
inside a field is stored as ``\\x03``.

Parsing is lossless: ``dumps(loads(text)) == text`` for files PMC writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PIPE_ESC = "\x03"
DEFAULT_VERSION = "5.4.1"
DEFAULT_MODES = "Window,2,Fast,0,1,Input,-1,-1,1"

# Column order of a step row after the index column.
COLUMNS = ("action", "details", "repeat", "delay", "type", "target", "window", "comment", "color", "codeline")

_HEADER_RE = re.compile(r"\[PMC Code(?:\sv)*(.*?)\]")


def _dec(value: str) -> str:
    return value.replace(PIPE_ESC, "|")


def _enc(value: Any) -> str:
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("raw newlines are not allowed in a step field; use the AHK escape `n instead")
    return text.replace("|", PIPE_ESC)


@dataclass
class Step:
    action: str = ""
    details: str = ""
    repeat: str = "1"
    delay: str = "0"
    type: str = ""
    target: str = ""
    window: str = ""
    comment: str = ""
    color: str = ""
    codeline: str = ""
    enabled: bool = True
    # Number of '|'-separated fields in the original line (for lossless round trips).
    _width: int = field(default=12, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        d = {c: getattr(self, c) for c in COLUMNS}
        d["enabled"] = self.enabled
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Step":
        unknown = set(d) - set(COLUMNS) - {"enabled"}
        if unknown:
            raise ValueError(f"unknown step field(s): {sorted(unknown)}; allowed: {list(COLUMNS) + ['enabled']}")
        kwargs = {c: str(d[c]) for c in COLUMNS if c in d and d[c] is not None}
        return cls(enabled=bool(d.get("enabled", True)), **kwargs)


@dataclass
class Macro:
    name: str = "Macro1"
    hotkey: str = ""
    manual_key: str = ""
    repeat: str = "1"
    modes: str = DEFAULT_MODES
    on_finish: str = "1"
    version: str = DEFAULT_VERSION
    context: str | None = None  # raw text after "Context=", None if absent
    groups: str | None = ""  # raw text after "Groups=", None if absent
    steps: list[Step] = field(default_factory=list)
    _header_extra: list[str] = field(default_factory=list, repr=False, compare=False)

    @property
    def is_function(self) -> bool:
        return "()" in self.name

    def summary(self, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "name": self.name,
            "hotkey": self.hotkey,
            "manual_key": self.manual_key,
            "repeat": self.repeat,
            "steps": len(self.steps),
            "is_function": self.is_function,
        }

    def to_dict(self, index: int | None = None) -> dict[str, Any]:
        d = {
            "name": self.name,
            "hotkey": self.hotkey,
            "manual_key": self.manual_key,
            "repeat": self.repeat,
            "modes": self.modes,
            "on_finish": self.on_finish,
            "context": self.context,
            "groups": self.groups,
            "steps": [s.to_dict() for s in self.steps],
        }
        if index is not None:
            d = {"index": index, **d}
        return d


@dataclass
class Project:
    macros: list[Macro] = field(default_factory=list)
    globals_line: str | None = None  # raw "[PMC Globals]|..." line, None if absent

    # ---- lookup -------------------------------------------------------------
    def find(self, macro: int | str | None) -> tuple[int, Macro]:
        """Return (1-based index, Macro) for an index or a name. None = first."""
        if not self.macros:
            raise KeyError("project has no macros")
        if macro is None or macro == "":
            return 1, self.macros[0]
        if isinstance(macro, int) or (isinstance(macro, str) and macro.isdigit()):
            i = int(macro)
            if not 1 <= i <= len(self.macros):
                raise KeyError(f"macro index {i} out of range 1-{len(self.macros)}")
            return i, self.macros[i - 1]
        for i, m in enumerate(self.macros, 1):
            if m.name == macro:
                return i, m
        raise KeyError(f"macro not found: {macro!r}; available: {[m.name for m in self.macros]}")

    def to_dict(self) -> dict[str, Any]:
        return {"globals": self.globals_line, "macros": [m.to_dict(i) for i, m in enumerate(self.macros, 1)]}


# ---- parsing ---------------------------------------------------------------


def loads(text: str) -> Project:
    if text.startswith("﻿"):
        text = text[1:]
    project = Project()
    current: Macro | None = None
    pending_close = False  # a blank line ended the current macro

    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("[PMC Globals]"):
            project.globals_line = line
            continue
        m = _HEADER_RE.match(line)
        if m:
            parts = line.split("|")
            current = Macro(
                version=m.group(1),
                hotkey=_get(parts, 1),
                manual_key=_get(parts, 2),
                repeat=_get(parts, 3),
                modes=_get(parts, 4),
                on_finish=_get(parts, 5),
                name=_get(parts, 6) or f"Macro{len(project.macros) + 1}",
                context=None,
                groups=None,
            )
            current._header_extra = parts[7:]
            project.macros.append(current)
            pending_close = False
            continue
        if current is None:
            # PMC tolerates headerless files (very old versions): treat as one macro.
            if re.match(r"^\d+\|", line):
                current = Macro(version="", context=None, groups=None)
                project.macros.append(current)
            else:
                continue
        if line.startswith("Context="):
            current.context = line[len("Context="):]
        elif line.startswith("Groups="):
            current.groups = line[len("Groups="):]
        elif re.match(r"^\d+\|", line):
            if pending_close:
                # Rows after a blank line without a new header: PMC would drop them; keep them.
                pending_close = False
            fields = line.split("|")
            vals = [_dec(f) for f in fields[1:]]
            vals += [""] * (len(COLUMNS) - len(vals))
            step = Step(**dict(zip(COLUMNS, vals[: len(COLUMNS)])), enabled=not fields[0].startswith("0"))
            step._width = len(fields)
            current.steps.append(step)
        elif line.strip() == "":
            pending_close = True
    return project


def _get(parts: list[str], i: int) -> str:
    return parts[i] if i < len(parts) else ""


def load(path: str | Path) -> Project:
    return loads(Path(path).read_bytes().decode("utf-8-sig"))


# ---- serialising -----------------------------------------------------------


def dumps(project: Project, bom: bool = True) -> str:
    out: list[str] = []
    if project.globals_line is not None:
        out.append(project.globals_line)
    for macro in project.macros:
        header = [
            f"[PMC Code v{macro.version}]" if macro.version else "[PMC Code]",
            macro.hotkey,
            macro.manual_key,
            macro.repeat,
            macro.modes,
            macro.on_finish,
            macro.name,
            *macro._header_extra,
        ]
        out.append("|".join(_enc(h) if i else h for i, h in enumerate(header)))
        if macro.context is not None:
            out.append("Context=" + macro.context)
        if macro.groups is not None:
            out.append("Groups=" + macro.groups)
        for n, step in enumerate(macro.steps, 1):
            vals = [_enc(getattr(step, c)) for c in COLUMNS]
            index = ("" if step.enabled else "0") + str(n)
            width = max(step._width, 1 + len(COLUMNS))
            # Pad / trim to the original width: drop only trailing empty columns.
            fields = [index, *vals]
            if width < len(fields):
                while len(fields) > width and fields[-1] == "":
                    fields.pop()
            fields += [""] * (width - len(fields))
            out.append("|".join(fields))
        out.append("")
    text = "\n".join(out) + "\n"
    return ("﻿" + text) if bom else text


def dump(project: Project, path: str | Path) -> None:
    Path(path).write_bytes(dumps(project).encode("utf-8"))


# ---- AHK / PMC escaping helpers -------------------------------------------


def escape_text(value: str, literal_percent: bool = False) -> str:
    """Escape a string for a PMC text field (AHK v1 command-argument rules).

    Backtick, comma and semicolon are escaped; newlines become `n, tabs `t.
    ``%Var%`` stays a variable reference, as in PMC's own editor, unless
    literal_percent=True (used for values that must arrive verbatim, e.g. run inputs).
    """
    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    out = value.replace("`", "``").replace(",", "`,").replace(";", "`;")
    if literal_percent:
        out = out.replace("%", "`%")
    return out.replace("\n", "`n").replace("\t", "`t")


_VAR_RE = re.compile(r"^[A-Za-z_#@$][A-Za-z0-9_#@$]{0,252}$")


def check_var_name(name: str) -> str:
    if not _VAR_RE.match(name or ""):
        raise ValueError(f"invalid AutoHotkey variable name: {name!r}")
    return name
