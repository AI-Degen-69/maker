"""The fleet dashboard page must actually PARSE.

Written after shipping a blank dashboard. The page had been "verified" by
checking that /api/state returned the right JSON and that the expected strings
were present in the HTML -- both of which passed while the page rendered
nothing at all. The cause was a duplicate `const bar` in the same function
scope: a SyntaxError, which aborts the entire <script> tag before a single
line runs. Every element stays empty and the browser logs nothing useful.

Neither an API check nor a string grep can catch that class of bug. Only
parsing the script can. This test does exactly that for the live fleet
dashboard. The archived kanban / single-bot page validation lived alongside
this on the now-moved ``archive/legacy-bot-8788`` branch; the kanban page
itself is no longer importable on this branch.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Only the live dashboard is rendered on this branch; the legacy "kanban"
# page lived in server/kanban.py and is preserved on the
# archive/legacy-bot-8788 branch (alongside the rest of the port-8788
# single-bot pipeline). Importing it here would point at the archive
# snapshot, which is not what this regression test is for.
from server.fleet_dash import PAGE as FLEET_PAGE  # noqa: E402

NODE = shutil.which("node")

# One page, one flatten, one parse: SyntaxError in any <script> renders a
# fully blank dashboard, not a degraded one.
PAGES = {"fleet": FLEET_PAGE}


def _script_blocks(page: str | None = None) -> list[str]:
    return re.findall(r"<script>([\s\S]*?)</script>",
                      FLEET_PAGE if page is None else page)


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
        names = re.findall(r"^const\s+([A-Za-z_$][\w$]*)\s*=", src, re.M)
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"{name}: duplicate const declarations: {sorted(dupes)}"


def test_no_duplicate_top_level_consts():
    """Top-level duplicate `const` in the same script block.

    The blank-dashboard bug class is a SyntaxError that aborts the entire
    ``<script>`` tag and leaves every element empty. Two `const NAME` lines
    at the top level of one script is the input the engine rejects. This
    regex only catches top-level duplicates (no leading whitespace);
    duplicates inside separate function bodies are legal in JS and are
    caught by ``test_dashboard_script_parses`` (node --check will reject
    any actual scope violation).
    """
    for src in _script_blocks(PAGES["fleet"]):
        names = re.findall(r"^const\s+([A-Za-z_$][\w$]*)\s*=", src, re.M)
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"duplicate const declarations: {sorted(dupes)}"
