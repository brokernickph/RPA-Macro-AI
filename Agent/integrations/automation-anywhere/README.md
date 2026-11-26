# Running PMC macros from Automation Anywhere

Automation Anywhere (Automation 360) bots can run PMC macros (`.pmc`) and browser + desktop flows
(`.rflow`) as a step: pass input variables in and read output variables back. This uses the generic `pmc-run` command line, so the same approach
works from Power Automate Desktop, Blue Prism, Windows Task Scheduler or any script.

> **Status:** `pmc-run` and `run_json` are covered by automated tests. The Automation Anywhere
> steps below follow AA's standard actions but haven't been tried in a Control Room yet. We found
> no published MCP support in AA's public repositories, so this integration doesn't depend on it.

## Setup on the bot runner (Windows)

1. Install Python 3.10+, then the package from the repository's `Agent` folder:

   ```bash
   pip install ./Agent
   ```

2. Set `PMC_EXE` to `MacroCreator.exe` (as a system environment variable on the bot runner).
3. Put your `.pmc` macros somewhere the bot user can read.

## Option A: Python Script package (returns the result directly)

1. **Python Script › Open**: choose *Manual input* and use this code (no file needed):

   ```python
   from pmc_mcp.cli import run_json
   ```

2. **Python Script › Execute function**:
   - *Function name:* `run_json`
   - *Arguments:* one string variable containing a JSON request, for example:

     ```json
     {"path": "C:\\Macros\\invoice.rflow", "macro": "Main",
      "inputs": {"InvoiceNo": "INV-42"}, "outputs": ["ExportPath"], "timeout_s": 300}
     ```

   - *Assign the output to:* a string variable, e.g. `sResult`.
3. **Python Script › Close**.
4. Parse `sResult` with the **JSON** package (or String actions). The fields are:
   - `ok`: `true` if the macro reached its last step.
   - `outputs.ExportPath`: the value of the output variable.
   - `timed_out`, `seconds`, and `error`, which is present only when something went wrong.

`run_json` never throws; failures come back as `{"ok": false, "error": "..."}`.

## Option B: Run a program and read a result file

1. **Application › Open program/file**:
   - *Program:* `pmc-run` (or the full path to `pmc-run.exe` in Python's `Scripts` folder)
   - *Arguments:*

     ```
     "C:\Macros\invoice.pmc" --macro Export --input InvoiceNo=INV-42 --output ExportPath --result-file "C:\Temp\pmc-result.json"
     ```

2. Wait for it to finish. Use **Wait for condition** until the result file exists, or start it
   through a cmd/PowerShell step that waits.
3. Read `C:\Temp\pmc-result.json` with **File › Read** and parse it as in Option A.

Exit codes for steps that can check them: `0` completed, `2` didn't reach the last step,
`3` timed out (the macro was stopped), `1` error.

## Notes

- The macro runs in a separate, hidden PMC instance and really uses the runner's mouse and
  keyboard, so give it an unlocked, logged-in session, as for any attended or unattended desktop
  bot.
- A file passed to `pmc-run` is always allowed, even outside `PMC_MCP_ROOTS`. `PMC_MCP_LEVEL=readonly`
  refuses to run macros.
- To build or edit macros visually, use the Visual Board (`pmc-board`, or File → Visual Board… in
  PMC).
