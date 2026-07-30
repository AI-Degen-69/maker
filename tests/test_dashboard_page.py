"""The dashboard page must actually PARSE.

Written after shipping a blank dashboard. The page had been "verified" by
checking that /api/state returned the right JSON and that the expected strings
were present in the HTML -- both of which passed while the page rendered
nothing at all. The cause was a duplicate `const bar` in the same function
scope: a SyntaxError, which aborts the entire <script> tag before a single
line runs. Every element stays empty and the browser logs nothing useful.

Neither an API check nor a string grep can catch that class of bug. Only
parsing the script can. This test does exactly that, and nothing else.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.kanban import PAGE  # noqa: E402

NODE = shutil.which("node")


from server.fleet_dash import PAGE as FLEET_PAGE  # noqa: E402

# Both dashboards get the same treatment: a SyntaxError in either renders a
# fully blank page, and neither an API check nor a string grep detects it.
PAGES = {"kanban": PAGE, "fleet": FLEET_PAGE}


def _script_blocks(page: str | None = None) -> list[str]:
    return re.findall(r"<script>([\s\S]*?)</script>",
                      PAGE if page is None else page)


def test_page_has_a_script_block():
    assert _script_blocks(), "the dashboard is inert without its <script>"


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("name", sorted(PAGES))
def test_dashboard_script_parses(tmp_path, name):
    """A parse error here is a fully blank page, not a degraded one."""
    for i, src in enumerate(_script_blocks(PAGES[name])):
        f = tmp_path / f"{name}{i}.js"
        # --check parses without executing, so no browser globals are needed.
        f.write_text(src, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)],
                           capture_output=True, text=True)
        assert r.returncode == 0, (
            f"dashboard script block {i} does not parse -- the page will render "
            f"BLANK:\n{r.stderr}")


@pytest.mark.parametrize("name", sorted(PAGES))
def test_no_duplicate_top_level_consts_all_pages(name):
    for src in _script_blocks(PAGES[name]):
        names = re.findall(r"^\s*const\s+([A-Za-z_$][\w$]*)\s*=", src, re.M)
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"{name}: duplicate const declarations: {sorted(dupes)}"


def test_no_duplicate_top_level_consts():
    """The specific mistake that caused the blank page, named so it stays fixed.

    Two `const x` in one function scope is legal-looking, greppable code that
    kills the whole file. Cheap to check, so it is checked.
    """
    for src in _script_blocks():
        names = re.findall(r"^\s*const\s+([A-Za-z_$][\w$]*)\s*=", src, re.M)
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"duplicate const declarations: {sorted(dupes)}"


def test_state_endpoint_exposes_what_the_page_reads():
    """The page reads s.rewards.*; the API must actually publish it."""
    from strategy import kpi
    rep = kpi.report()
    assert "rewards" in rep
    r = rep["rewards"]
    if r.get("samples"):
        for k in ("uptime", "avg_share", "two_sided_rate", "offset_cents"):
            assert k in r, f"dashboard reads rewards.{k}, report() omits it"
