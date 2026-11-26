from pathlib import Path

import pytest

from pmc_mcp import pmcfile, steps

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = sorted((REPO / "Documentation").rglob("*.pmc"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_round_trip_is_lossless(path):
    raw = path.read_bytes()
    proj = pmcfile.loads(raw.decode("utf-8-sig"))
    assert pmcfile.dumps(proj, bom=raw.startswith(b"\xef\xbb\xbf")).encode("utf-8") == raw


def test_examples_found():
    assert len(EXAMPLES) >= 5


def test_parse_demo():
    proj = pmcfile.load(REPO / "Documentation/MacroCreator_Help-doc/Examples/Demo.pmc")
    m = proj.macros[0]
    assert m.name == "BasicDemo" and m.hotkey == "F3" and m.version == "5.0.1"
    assert m.steps[2].action == "Run" and m.steps[2].details == "Notepad, , , Npid"
    assert m.steps[3].window == "ahk_pid %Npid%"


def test_disabled_rows_and_pipe_escape():
    text = "[PMC Code v5.4.1]|||1|Window|1|M\nGroups=\n1|A|x\x03y|1|0|Send|||||\n02|B||1|0|Sleep|||||\n\n"
    proj = pmcfile.loads(text)
    a, b = proj.macros[0].steps
    assert a.details == "x|y" and a.enabled
    assert not b.enabled
    assert pmcfile.dumps(proj, bom=False) == text


def test_find_by_name_and_index():
    proj = pmcfile.loads("[PMC Code v5]|||1||1|One\n\n[PMC Code v5]|||1||1|Two\n\n")
    assert proj.find("Two")[0] == 2
    assert proj.find(1)[1].name == "One"
    assert proj.find("2")[1].name == "Two"
    with pytest.raises(KeyError):
        proj.find("Nope")


def test_new_rows_use_current_width():
    proj = pmcfile.Project([pmcfile.Macro(name="M", steps=[steps.sleep(500)])])
    line = pmcfile.dumps(proj, bom=False).splitlines()[2]
    assert line == "1|[Pause]||1|500|Sleep||||||"
    assert pmcfile.loads(pmcfile.dumps(proj)).macros[0].steps[0].delay == "500"


def test_newline_in_field_rejected():
    proj = pmcfile.Project([pmcfile.Macro(steps=[pmcfile.Step(action="x", details="a\nb")])])
    with pytest.raises(ValueError):
        pmcfile.dumps(proj)


def test_escape_text():
    assert pmcfile.escape_text("a, b; 100%\nnext `x`") == "a`, b`; 100`%`nnext ``x``"


def test_builders_match_pmc_rows():
    # Shapes taken from rows PMC itself wrote in the bundled examples.
    assert steps.click(-6, -122).to_dict() | {} == {
        **steps.click(-6, -122).to_dict(),
        "action": "Left Move & Click", "details": "-6, -122 Left, 1", "type": "Click", "delay": "10",
    }
    assert steps.type_text("Hello,\n").details == "Hello`,`n"
    assert steps.type_text("hi", control="Edit1", window="ahk_class Notepad").type == "ControlSendRaw"
    assert steps.win_activate("ahk_pid %Npid%").window == "ahk_pid %Npid%"
    assert (steps.loop(3).repeat, steps.loop(3).type) == ("3", "Loop")
    assert steps.assign("N", "[1,2]", expression=True).target == "Expression"
    assert steps.run("notepad.exe", pid_var="P").details == "notepad.exe, , , P"


def test_build_validation():
    with pytest.raises(ValueError, match="unknown step kind"):
        steps.build({"kind": "teleport"})
    with pytest.raises(ValueError, match="bad parameters"):
        steps.build({"kind": "click", "x": 1})
    with pytest.raises(ValueError):
        steps.build({"kind": "assign", "var": "bad name", "value": "x"})
    raw = steps.build({"kind": "raw", "action": "Beep", "details": "", "type": "SoundBeep", "enabled": False})
    assert raw.type == "SoundBeep" and not raw.enabled


def test_inject_io():
    m = pmcfile.Macro(name="M", steps=[steps.sleep(1)])
    steps.inject_io(m, {"User": "a,b"}, ["Result"], r"C:\T\out")
    assert m.steps[0].details == "User := a`,b"
    assert m.steps[-2].details == r"%Result%, C:\T\out\Result.txt, UTF-8"
    assert m.steps[-1].details.startswith("done, ")
    with pytest.raises(ValueError):
        steps.inject_io(m, {"1x y": "v"}, [], "C:\\")
