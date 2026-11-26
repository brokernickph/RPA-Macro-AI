# PMC as a UiPath coded MCP server

Packages `pmc-mcp` as a **coded MCP server** for UiPath Orchestrator with UiPath's own
[`uipath-mcp`](https://github.com/UiPath/uipath-mcp-python) SDK (MIT). After you publish it,
UiPath agents can use PMC's tools: build and edit macros, run them with inputs and outputs, play
and record in PMC, and observe or drive the Windows desktop.

## What's verified, and what isn't

| Step | Status |
|---|---|
| `uipath init` finds the server and creates an `mcpserver` entry point | ✅ run with uipath-mcp 0.3.2 (placeholder tenant) |
| `uipath pack` builds `pmc-rpa-mcp.0.1.0.nupkg` with `pmc_mcp` bundled inside | ✅ |
| The server starts from the unpacked package with only the declared dependencies and serves all 27 tools | ✅ (MCP SDK 1.x, which uipath-mcp pins) |
| `uipath publish`, and agents calling the tools in a real tenant | ⚠️ not tested: needs a UiPath account |
| Desktop tools (`desktop_*`, `macro_run`, `pmc_*`) when Orchestrator hosts the server | ⚠️ see below |

**Where it runs matters.** The macro-file tools (`macro_list_files`, `macro_read`, `macro_create`,
…) work anywhere. Everything that clicks, types or starts PMC must run on a **Windows machine
with a logged-in desktop session** that has PMC and AutoHotkey v1.1 installed.
UiPath's packer always marks MCP-server packages `requiresUserInteraction: false` and
`targetFramework: Portable`, so make sure your Orchestrator folder runs this process on such a
Windows robot, not on a serverless (Linux) robot. Check this on your tenant before relying on the
desktop tools.

## Build and publish

From this folder, on a machine with Python 3.10+:

```bash
pip install uipath-mcp
```

```bash
python prepare.py
```

```bash
uipath auth
```

```bash
uipath init
```

```bash
uipath pack
```

```bash
uipath publish
```

What each step does:
- `prepare.py` copies `Agent/pmc_mcp` here. UiPath's packer can't express a git or path dependency,
  so the code is bundled instead.
- `uipath auth` signs in through the browser and writes `.env`.
- `uipath init` creates `uipath.json` and `entry-points.json` (one `mcpserver` entry point, `pmc-rpa`).
- `uipath pack` builds `.uipath/pmc-rpa-mcp.<version>.nupkg`.
- `uipath publish` uploads it to Orchestrator.

To try it locally first, run `uipath run pmc-rpa`.

Configure the server with the same environment variables as everywhere else, set on the robot
machine or in the process settings: `PMC_EXE`, `PMC_AHK_EXE`, `PMC_MCP_ROOTS`, `PMC_MCP_LEVEL`,
`PMC_MCP_AUDIT_LOG`, `PMC_MCP_TIMEOUT` (see [Agent/README.md](../../README.md)).

## Files

| File | Purpose |
|---|---|
| `mcp.json` | The server UiPath launches: `python -m pmc_mcp` over stdio |
| `pyproject.toml` | Package name, description, authors and PyPI dependencies, which `uipath pack` requires |
| `prepare.py` | Copies `pmc_mcp` into this folder before packing |
| `.gitignore` | Keeps the copied code and the files the `uipath` CLI generates out of git |

Bump `version` in `pyproject.toml` for each new publish.
