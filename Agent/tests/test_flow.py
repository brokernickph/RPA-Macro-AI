from pathlib import Path

import pytest

from pmc_mcp import flow, pmcfile, steps

REPO = Path(__file__).resolve().parents[2]
MACROS = [(p, i) for p in sorted((REPO / "Documentation").rglob("*.pmc")) for i in range(len(pmcfile.load(p).macros))]
COLS = ("action", "details", "type", "target", "window", "repeat", "delay", "comment", "enabled")


def rows(steps_):
    return [{c: getattr(s, c) for c in COLS} for s in steps_]


@pytest.mark.parametrize("path,i", MACROS, ids=lambda v: v.name if isinstance(v, Path) else str(v))
def test_examples_tree_round_trip(path, i):
    m = pmcfile.load(path).macros[i]
    tree, structured = flow.to_tree(m.steps)
    assert structured
    assert rows(flow.to_rows(tree)) == rows(m.steps)


@pytest.mark.parametrize("path,i", MACROS, ids=lambda v: v.name if isinstance(v, Path) else str(v))
def test_described_rows_rebuild_identically(path, i):
    """Editing a step through the board's form must not silently change other columns."""
    for s in pmcfile.load(path).macros[i].steps:
        spec = flow.describe(s.to_dict())
        if spec:
            b = steps.build(spec)
            assert rows([b])[0] | {"comment": s.comment, "enabled": s.enabled} == rows([s])[0], spec


def test_tree_shapes():
    rows_ = [steps.if_expr("x > 1"), steps.sleep(1),
             pmcfile.Step(action="[ElseIf] Compare Variables", details="x = 0", type="If_Statement"), steps.loop(2), steps.sleep(2), steps.loop_end(),
             steps.else_(), steps.sleep(3), steps.end_if()]
    tree, ok = flow.to_tree(rows_)
    assert ok and len(tree) == 1
    node = tree[0]
    assert node["t"] == "if" and len(node["branches"]) == 2
    assert node["branches"][1]["body"][0]["t"] == "loop"
    assert node["else"]["body"][0]["step"]["delay"] == "3"
    assert rows(flow.to_rows(tree)) == rows(rows_)


@pytest.mark.parametrize("bad", [[steps.end_if()], [steps.loop(1)], [steps.if_expr("a"), steps.loop_end()],
                                 [steps.if_expr("a"), steps.else_(), steps.else_(), steps.end_if()]])
def test_unbalanced_falls_back_to_flat(bad):
    tree, ok = flow.to_tree(bad)
    assert not ok and all(n["t"] == "step" for n in tree)
    assert rows(flow.to_rows(tree)) == rows(bad)


def test_new_nodes_from_specs():
    tree = [
        {"t": "step", "spec": {"kind": "click", "x": 5, "y": 6}},
        {"t": "if", "branches": [{"head": {"kind": "if", "condition": "ErrorLevel = 0"}, "body": []}], "else": {"head": None, "body": [
            {"t": "step", "spec": {"kind": "msgbox", "text": "fail", "enabled": False, "note": "why"}}]}, "end": None},
        {"t": "loop", "head": {"kind": "loop", "count": 3}, "body": [{"t": "step", "spec": {"kind": "sleep", "ms": 10}}], "end": None},
    ]
    out = flow.to_rows(tree)
    assert [s.action for s in out] == ["Left Move & Click", "Compare Variables", "[Else]", "[MsgBox]", "[End If]", "[LoopStart]", "[Pause]", "[LoopEnd]"]
    assert not out[3].enabled and out[3].comment == "why"
    assert out[5].repeat == "3"
    again, ok = flow.to_tree(out)
    assert ok and again[1]["t"] == "if" and again[2]["t"] == "loop"


def test_describe_keeps_variable_references():
    s = steps.type_text("Hello %Name%, bye")
    assert flow.describe(s.to_dict())["text"] == "Hello %Name%, bye"
    literal = steps.assign("X", "100%", literal=True)
    assert flow.describe(literal.to_dict()) is None  # kept raw so the literal % survives


def test_palette_kinds_are_runnable():
    """Each palette action works where the board offers it: as a PMC row (.pmc) and/or in the flow engine (.rflow)."""
    from pmc_mcp import engine

    for g in flow.PALETTE:
        for k in g["kinds"]:
            scope = k.get("scope", "both")
            spec = {"kind": k["kind"], **{f["name"]: f["default"] for f in k["fields"]}}
            if scope in ("both", "pmc") and not k.get("block"):
                steps.build(spec)  # valid PMC row
            if scope in ("both", "flow"):
                assert k["kind"] in engine.KINDS or k["kind"] in ("if", "loop", "foreach"), k["kind"]
