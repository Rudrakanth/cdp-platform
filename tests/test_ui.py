"""The UI is one HTML file with an inline script. A single syntax error silently blanks the whole page
(the API tests cannot see that), so the script must at least parse."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "cdp" / "ui" / "index.html"


def script() -> str:
    m = re.search(r"<script>(.*)</script>", UI.read_text(), re.S)
    assert m, "no inline <script> found"
    return m.group(1)


def test_script_has_balanced_brackets():
    s = re.sub(r"`(?:\\.|[^`\\])*`|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|/\*.*?\*/|//[^\n]*", "", script(), flags=re.S)
    for o, c in ("()", "[]", "{}"):
        assert s.count(o) == s.count(c), f"unbalanced {o}{c} in UI script"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_script_parses_with_node(tmp_path):
    f = tmp_path / "ui.js"
    f.write_text(script())
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
