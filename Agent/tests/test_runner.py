import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from pmc_mcp import pmcfile, runner, steps
from pmc_mcp.config import Config

FAKE_PMC = textwrap.dedent(
    """\
    #!{python}
    # Stand-in for MacroCreator.exe: "plays" Variable and FileAppend rows of macro -s<n>.
    import re, sys, time
    sys.path.insert(0, {pkg!r})
    from pmc_mcp import pmcfile
    path, *flags = sys.argv[1:]
    assert "-new" in flags, flags
    n = int([f for f in flags if f.startswith("-s")][0][2:])
    m = pmcfile.load(path).macros[n - 1]
    env = {{}}
    unesc = lambda s: re.sub(r"`(.)", lambda x: "\\n" if x.group(1) == "n" else x.group(1), s)
    for st in m.steps:
        if st.type == "Sleep":
            time.sleep(int(st.delay) / 1000)
        elif st.type == "Variable":
            k, v = st.details.split(" := ", 1)
            env[k] = unesc(v)
        elif st.type == "FileAppend":
            text, target, _ = re.split(r"(?<!`), ", st.details)
            text = re.sub(r"%(\\w+)%", lambda x: env.get(x.group(1), ""), text)
            open(unesc(target).replace("\\\\", "/"), "a", encoding="utf-8").write(text)
    """
)


@pytest.fixture
def cfg(tmp_path):
    if sys.platform == "win32":
        pytest.skip("fake PMC uses a POSIX shebang")
    exe = tmp_path / "MacroCreator"
    exe.write_text(FAKE_PMC.format(python=sys.executable, pkg=str(Path(__file__).resolve().parents[1])))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    root = tmp_path / "macros"
    root.mkdir()
    return Config(roots=[root], pmc=str(exe))


def make(root, *macro_steps):
    p = root / "job.pmc"
    proj = pmcfile.Project([pmcfile.Macro(name=f"M{i}", steps=list(s)) for i, s in enumerate(macro_steps, 1)])
    pmcfile.dump(proj, p)
    return p


def test_inputs_flow_to_outputs(cfg):
    p = make(cfg.roots[0], [steps.sleep(1)], [steps.assign("Greeting", "Hi %Name%", expression=False)])
    # Macro 2 copies nothing itself; the output is the injected input, proving round trip.
    res = runner.run_macro_headless(cfg, p, "M2", {"Name": "Ada, Lovelace"}, ["Name"], timeout=20)
    assert res["completed"] and not res["timed_out"]
    assert res["macro"] == {"index": 2, "name": "M2"}
    assert res["outputs"] == {"Name": "Ada, Lovelace"}
    # the original file is untouched
    assert len(pmcfile.load(p).macros[1].steps) == 1


def test_timeout_kills_and_reports(cfg):
    p = make(cfg.roots[0], [steps.sleep(10_000)])
    res = runner.run_macro_headless(cfg, p, None, {}, ["X"], timeout=1)
    assert res["timed_out"] and not res["completed"]
    assert res["outputs"] == {"X": None} and res["note"]


def test_function_macro_refused(cfg):
    p = cfg.roots[0] / "f.pmc"
    pmcfile.dump(pmcfile.Project([pmcfile.Macro(name="Fn()")]), p)
    with pytest.raises(runner.RunnerError, match="function"):
        runner.run_macro_headless(cfg, p, None, {}, [], timeout=5)


def test_missing_pmc_exe(tmp_path):
    with pytest.raises(runner.RunnerError, match="PMC_EXE"):
        runner.pmc_command(Config(roots=[tmp_path]))


def test_ahk_string():
    assert runner.ahk_string('say "hi"\n`x') == '"say ""hi""`n``x"'
