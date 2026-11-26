"""Configuration, permission levels and audit logging (all from environment variables)."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

LEVELS = ("readonly", "standard", "full")


def _default_roots() -> list[Path]:
    docs = Path.home() / "Documents" / "PMC Macros"
    return [docs]


def _find_ahk() -> str | None:
    if env := os.environ.get("PMC_AHK_EXE"):
        return env
    for name in ("AutoHotkeyU64.exe", "AutoHotkey.exe", "AutoHotkeyU32.exe"):
        if found := shutil.which(name):
            return found
    pf = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")]
    for base in filter(None, pf):
        root = Path(base) / "AutoHotkey"
        # AHK v1 standalone install, or v1 side-by-side inside an AHK v2 install.
        cands = [root / "AutoHotkeyU64.exe", root / "AutoHotkey.exe", *sorted(root.glob("v1.1*/AutoHotkeyU64.exe"), reverse=True)]
        for c in cands:
            if c.is_file():
                return str(c)
    return None


@dataclass
class Config:
    level: str = "standard"
    roots: list[Path] = field(default_factory=_default_roots)
    pmc: str | None = None  # MacroCreator.exe or MacroCreator.ahk
    ahk: str | None = None  # AutoHotkey v1.1 executable
    audit_log: Path | None = None
    default_timeout: int = 300

    @classmethod
    def from_env(cls) -> "Config":
        level = os.environ.get("PMC_MCP_LEVEL", "standard").lower()
        if level not in LEVELS:
            raise SystemExit(f"PMC_MCP_LEVEL must be one of {LEVELS}")
        roots_env = os.environ.get("PMC_MCP_ROOTS")
        roots = [Path(p).expanduser() for p in roots_env.split(os.pathsep) if p] if roots_env else _default_roots()
        audit = os.environ.get("PMC_MCP_AUDIT_LOG")
        audit_path = Path(audit).expanduser() if audit else (roots[0] / "pmc-mcp-audit.jsonl" if roots else None)
        return cls(
            level=level,
            roots=[r.resolve() for r in roots],
            pmc=os.environ.get("PMC_EXE"),
            ahk=_find_ahk(),
            audit_log=audit_path,
            default_timeout=int(os.environ.get("PMC_MCP_TIMEOUT", "300")),
        )

    # ---- permissions ------------------------------------------------------
    def require(self, needed: str) -> None:
        if LEVELS.index(self.level) < LEVELS.index(needed):
            raise PermissionError(
                f"this tool needs permission level '{needed}' but the server runs at '{self.level}'. "
                "The user can raise it with the PMC_MCP_LEVEL environment variable."
            )

    def safe_path(self, path: str, must_exist: bool = False, suffix: str | None = ".pmc") -> Path:
        """Resolve a path and make sure it lies inside one of the allowed roots."""
        if not self.roots:
            raise PermissionError("no macro directories configured (PMC_MCP_ROOTS)")
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.roots[0] / p
        p = p.resolve()
        if not any(p == r or r in p.parents for r in self.roots):
            raise PermissionError(f"{p} is outside the allowed macro directories: {[str(r) for r in self.roots]}")
        if suffix and p.suffix.lower() != suffix:
            raise ValueError(f"expected a {suffix} file: {p}")
        if must_exist and not p.is_file():
            raise FileNotFoundError(str(p))
        return p

    def audit(self, tool: str, **details) -> None:
        if not self.audit_log:
            return
        try:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, **details}
            with self.audit_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass
