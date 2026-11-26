# PMC in the Robomotion MCP catalog

`servers/pulovers-macro-creator/` is an entry for
[robomotionio/robomotion-mcps](https://github.com/robomotionio/robomotion-mcps), the catalog of
MCP servers that Robomotion hub "Teams" agents can turn on in the Agent Editor's **MCP** tab.

It follows that repository's documented contract (`.robomotion/mcp.yaml`, `MCP.md`,
`env.optional`). It was checked by copying it into a clone of the catalog and running the
catalog's own tools:

```
python3 build-index.py   # wrote index.yaml: 80 server(s)
./validate.sh            # OK — 80 server(s) valid.
```

The launch command (`uvx --from git+…#subdirectory=Agent pmc-mcp`) was also tested: it builds the
package and serves all 27 tools over stdio.

## Using it

- **Your own hub:** add the folder to a catalog your hub reads, or propose it upstream by opening a
  pull request to `robomotionio/robomotion-mcps` that adds `servers/pulovers-macro-creator/` and
  the regenerated `index.yaml`.
- **The host:** Robomotion starts stdio servers host-side, so run it on the **Windows** machine
  (with its desktop robot) where PMC, AutoHotkey v1.1 and `uv` are installed, in a logged-in
  session.
- **Before merging to `master`:** the `args` pin the `feature/ai-agent-rpa-mcp` branch. Change it
  to `master` or to a release tag once this work is merged.

Not tested: running it inside a real Robomotion hub, which needs a Robomotion account.
