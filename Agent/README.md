# PMC Agent Bridge and MCP Server

This turns Pulover's Macro Creator into an **RPA engine that AI agents can drive** through the
[Model Context Protocol](https://modelcontextprotocol.io). Any MCP client (Claude Desktop,
Claude Code, Cursor, custom agents built on an MCP SDK, …) can:

* **build** macros as `.pmc` projects that open, edit and play normally in the PMC GUI,
* **run** a macro headless with **input variables** and get **output variables** back,
* **drive the live PMC app**: open, play, stop, and **record a human demonstration** and read it back,
* **observe and act on the desktop directly**: screenshots, window list, click, type, keys,
  image and pixel search.

```
 AI agent ──MCP (stdio)──► pmc-mcp (Python)
                              │
                              ├─ .pmc files ───────────── read/write (pure Python, lossless)
                              ├─ MacroCreator -new -s<n> ─ headless run, inputs/outputs injected
                              ├─ WM_COPYDATA "PMCA" ─────► LIB\AgentBridge.ahk inside the running PMC
                              │                            (play / stop / record / open / save / status)
                              └─ AutoHotkey v1 snippets ── desktop primitives (click, keys, windows, search)
```

## Visual Board

A drag-and-drop flow editor for PMC macros that runs in your browser:

* **Build visually:** drag actions (click, type, keys, open app, wait for window, if/else, repeat,
  variables…) from the palette onto the **+** slots, then fill in the settings panel. If/Else-if/Else
  branches sit side by side and loops wrap their body.
* **Same files as PMC:** the board edits `.pmc` projects directly. Anything built on the board opens
  in classic PMC and the other way round; rows the board has no form for are shown as raw PMC rows,
  and steps you don't touch are saved exactly as they were.
* **Run from the board:** **▶ Run** runs the macro headless with input and output variables;
  **Play in PMC** plays it in the open PMC window; **■ Stop** aborts.
* **Works alongside AI agents:** when an agent edits the open file through MCP, the board reloads
  it live (or warns you if you have unsaved edits). Agents can open the board for you with the
  `board_open` tool.
* Undo/redo, Ctrl+S, Delete, Ctrl+D (duplicate), zoom, multiple macros as tabs, dark mode.

Open it from PMC with **File → Visual Board…**, or from a terminal:

```bash
pmc-board "C:\Users\me\Documents\PMC Macros\invoice.pmc"
```

It listens on `127.0.0.1` only, and the link it opens carries a per-launch access token, so other
web pages can't drive it. It exits on its own about two minutes after you close the tab.

## Install (Windows)

1. Python 3.10+ and, for the desktop tools, **AutoHotkey v1.1** (<https://www.autohotkey.com>).
   The AHK v2 installer can install v1 side by side; the server finds both layouts.
2. From this folder:

   ```bash
   pip install .
   ```

3. Register it with your MCP client. For Claude Desktop, add this to `claude_desktop_config.json`:

   ```json
   {
     "mcpServers": {
       "pmc": {
         "command": "pmc-mcp",
         "env": {
           "PMC_EXE": "C:\\Program Files\\MacroCreator\\MacroCreator.exe",
           "PMC_MCP_ROOTS": "C:\\Users\\me\\Documents\\PMC Macros",
           "PMC_MCP_LEVEL": "standard"
         }
       }
     }
   }
   ```

   For Claude Code:

   ```bash
   claude mcp add pmc -e PMC_EXE="C:\Program Files\MacroCreator\MacroCreator.exe" -- pmc-mcp
   ```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PMC_EXE` | – | `MacroCreator.exe`, or `MacroCreator.ahk` when running PMC from source (then AHK is used to start it) |
| `PMC_AHK_EXE` | auto-detected | AutoHotkey **v1.1** executable used for the desktop tools |
| `PMC_MCP_ROOTS` | `~/Documents/PMC Macros` | `;`-separated folders the agent may read and write `.pmc` files and images in. Relative paths resolve to the first |
| `PMC_MCP_LEVEL` | `standard` | `readonly` · `standard` · `full` (see below) |
| `PMC_MCP_AUDIT_LOG` | `<first root>/pmc-mcp-audit.jsonl` | JSON-lines log of every state-changing tool call |
| `PMC_MCP_TIMEOUT` | `300` | Default seconds before a running macro is stopped |

### Permission levels

| Level | Allows |
|---|---|
| `readonly` | reading macros, PMC status, screenshots, window list, pixel/image search |
| `standard` | everything above, plus editing macros, running/playing/recording them, mouse/keyboard/window actions |
| `full` | everything above, plus `desktop_run_ahk` (arbitrary AutoHotkey scripts) |

> **Be clear about the risk:** a macro can contain any command, including `Run`. At `standard`, an
> agent can effectively control this computer as you. Use a dedicated Windows account or VM for
> unattended automation, keep `PMC_MCP_ROOTS` narrow, and review the audit log.
> `pmc_stop` (or PMC's abort hotkey) stops a running macro at any time.

## Tools

| Group | Tool | Purpose |
|---|---|---|
| Files | `macro_list_files`, `macro_read`, `macro_step_kinds` | discover and inspect macros |
| | `macro_create`, `macro_add_steps`, `macro_update_step`, `macro_delete_steps` | build and edit macros from simple step specs |
| Run | `macro_run` | run one macro headless with `inputs` → `outputs`, with a timeout |
| Live PMC | `pmc_launch`, `pmc_status`, `pmc_open`, `pmc_save`, `pmc_window` | control the app |
| | `pmc_play`, `pmc_stop` | play (optionally waiting) / abort |
| | `pmc_record_start`, `pmc_record_stop`, `pmc_get_live_macro` | capture a human demonstration as steps |
| Board | `board_open` | open the Visual Board on a file, so the user can see or edit a macro |
| Desktop | `desktop_screenshot`, `desktop_list_windows`, `desktop_window` | observe and manage windows |
| | `desktop_click`, `desktop_send_keys`, `desktop_pixel_color`, `desktop_image_search` | act and verify |
| | `desktop_run_ahk` | arbitrary AHK v1 (`full` only) |

### Step specs

Agents describe steps as small JSON objects. They are turned into exactly the rows PMC's own
command windows write:

```json
[
  {"kind": "run", "target": "notepad.exe", "pid_var": "Np"},
  {"kind": "win_wait", "window": "ahk_pid %Np%", "timeout_s": 10},
  {"kind": "type_text", "text": "Hello, %UserName%"},
  {"kind": "send_keys", "keys": "^s"},
  {"kind": "raw", "action": "SoundBeep", "details": "750, 200", "type": "SoundBeep"}
]
```

Call `macro_step_kinds` for the full list: keys, text, click, move, sleep, run, window
commands, variables, if/else, loops, gosub, file_append, any AHK command, and raw rows.

### Inputs and outputs (`macro_run`)

`macro_run` copies the project to a temp folder, adds `[Assign Variable]` rows for each input at
the top of the macro and `FileAppend` rows for each output at the end, then runs
`MacroCreator.exe <copy> -new -s<n>` and waits. The result reports `completed` (whether the last
step was reached), `timed_out`, `exit_code` and the output values. Your original file is never
modified.

## Agent bridge protocol (for other integrations)

The bridge is inside PMC itself (`LIB/AgentBridge.ahk`), so any language can use it, not only
this server.

* Enable it with `MacroCreator.exe -agent` or `[Options] AgentBridge=1` in `MacroCreator.ini`.
* PMC writes `%TEMP%\PMC_Agent\bridge.json`: `{"hwnd", "pid", "version", "protocol": 1}`.
* Send `WM_COPYDATA` to `hwnd` with `dwData = 0x504D4341`, payload UTF-16 JSON
  `{"id": "<[A-Za-z0-9_-]{1,64}>", "cmd": "...", "args": {...}}`. PMC returns 1 if it queued the request.
* The reply appears atomically as `%TEMP%\PMC_Agent\<id>.json`:
  `{"id", "ok": 1, "result": {...}}` or `{"id", "ok": 0, "error": "..."}`.

| cmd | args | result |
|---|---|---|
| `ping` | – | version, pid |
| `status` | – | file, unsaved, playing, recording, active_macro, macros[] |
| `open` | `path`, `discard_changes` | status (refuses if there are unsaved changes, unless discarded) |
| `save` | `path?` | file |
| `get_macro` | `macro?` (index or name) | rows (raw PMC columns) |
| `select` | `macro` | status |
| `play` / `stop` | `macro?` / – | started / stopped |
| `record_start` / `record_stop` | `macro?` / – | recording state, row count |
| `show` / `hide` | – | visible |

New PMC command-line switches: `-agent` (enable the bridge) and `-new` (always start a separate
instance, so headless `-s` runs never get handed to a PMC window you have open).

## Development

```bash
pip install -e .[test]
pytest
```

The tests run on any OS. They cover lossless round trips of every bundled example `.pmc` (as rows
and as board trees), that every step the board shows as a form rebuilds to the identical row, the
step builders, path confinement and permission levels, the board's HTTP API and security checks,
and the MCP tools end to end over an in-process client against a simulated bridge and a fake PMC
executable. `tests/test_board_ui.py` drives the real board in Chromium via Playwright (skipped when
Playwright isn't installed; set `PMC_TEST_CHROMIUM` to use an existing Chrome binary).

**Not yet verified on Windows:** `LIB/AgentBridge.ahk`, the File → Visual Board menu item, the
Win32 message sending, and the AHK snippets behind the `desktop_*` tools were written against the PMC source but still need a test
pass on a real Windows machine.
