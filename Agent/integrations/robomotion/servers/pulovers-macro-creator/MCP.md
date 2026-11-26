# RPA Macro AI (browser + desktop, Pulover's Macro Creator)

Lets an agent automate web pages and Windows desktop apps, built on
[Pulover's Macro Creator](https://github.com/brokernickph/rpa-macro-ai) (PMC), a free
AutoHotkey-based macro recorder:

- **Browser:** open pages, list clickable elements, click, fill, select, wait, read text and
  whole tables, and take screenshots (Edge or Chrome through Playwright).
- **Flows (`.rflow`):** repeatable automations that mix browser steps, desktop steps and PMC
  macros, with variables, If/Else, Repeat and For each. `flow_run` returns outputs and a step log.

- **Build macros:** create and edit `.pmc` projects from simple step specs (click, type, keys,
  open app, wait for window, if/else, loops, variables). They open and play in the PMC app too.
- **Run with parameters:** `macro_run` runs a macro in a hidden PMC instance with input
  variables and returns output variables, with a timeout.
- **Live PMC:** launch, open, play, stop, and record a human demonstration, then read the
  recorded steps back.
- **Desktop:** screenshots, window list and actions, click, send keys, pixel colour, image
  search.
- **Visual Board:** `board_open` opens a drag-and-drop editor of a macro for the user.

## Transport

**stdio**: `uvx --from git+https://github.com/brokernickph/rpa-macro-ai@feature/ai-agent-rpa-mcp#subdirectory=Agent pmc-mcp`.
It must run **host-side on Windows**, on the desktop it automates. Requirements on that host:

- [uv](https://docs.astral.sh/uv/) (for `uvx`) and Python 3.10+
- Microsoft Edge (preinstalled on Windows) for the browser tools; set `PMC_BROWSER_PROFILE` to
  keep logins between runs
- Pulover's Macro Creator from this fork (for `macro_run` and the `pmc_*` tools)
- AutoHotkey v1.1 (for the `desktop_*` tools)
- an interactive, unlocked user session (mouse and keyboard input needs a real desktop)

## Credentials

None. The server needs no token or API key.

## Configuration (optional)

| Variable | Default | Notes |
| --- | --- | --- |
| `PMC_EXE` | – | Path to `MacroCreator.exe` (or `MacroCreator.ahk`). Needed to run or launch macros. |
| `PMC_AHK_EXE` | auto-detected | AutoHotkey v1.1 executable for the desktop tools. |
| `PMC_MCP_ROOTS` | `~/Documents/PMC Macros` | `;`-separated folders the agent may read and write macros in. |
| `PMC_MCP_LEVEL` | `standard` | `readonly`, `standard`, or `full` (`full` adds arbitrary AutoHotkey scripts). |
| `PMC_MCP_AUDIT_LOG` | `<first root>/pmc-mcp-audit.jsonl` | Log of every state-changing call. |
| `PMC_MCP_TIMEOUT` | `300` | Seconds before a running macro or flow is stopped. |
| `PMC_BROWSER_PROFILE` | – | Folder for a persistent browser profile (keeps logins). |
| `PMC_BROWSER_CHANNEL` | `msedge` | `msedge` or `chrome`. |
| `PMC_BROWSER_HEADLESS` | `0` on Windows | `1` runs the browser without a window. |

## Safety

At `standard`, the agent can effectively control the host desktop as the signed-in user,
because macros can start programs. Use a dedicated Windows account or VM for unattended runs,
keep `PMC_MCP_ROOTS` narrow, and use `readonly` when the agent only needs to observe.
`pmc_stop` aborts a running macro.

## Source

- Repository: https://github.com/brokernickph/rpa-macro-ai (see `Agent/README.md`)
