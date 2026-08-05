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


def _resting(side, price, size, filled=0.0):
    return {"side": side, "price": price, "size": size, "filled": filled}


def test_resting_by_side_folds_both_legs():
    from server.fleet_dash import _resting_by_side
    r = _resting_by_side([_resting("UP", 0.48, 131.25),
                          _resting("DOWN", 0.48, 131.25)])
    assert r["up_shares"] == pytest.approx(131.25)
    assert r["dn_shares"] == pytest.approx(131.25)
    assert r["up_usd"] == pytest.approx(63.0)
    assert r["dn_usd"] == pytest.approx(63.0)


def test_resting_by_side_subtracts_filled():
    """`capital` bills `size - filled`; depth must use the same denominator.

    Billing `size` would show depth we no longer have on the book.
    """
    from server.fleet_dash import _resting_by_side
    r = _resting_by_side([_resting("UP", 0.50, 100.0, filled=40.0)])
    assert r["up_shares"] == pytest.approx(60.0)
    assert r["up_usd"] == pytest.approx(30.0)


def test_resting_by_side_drops_fully_filled_and_unknown_sides():
    from server.fleet_dash import _resting_by_side
    r = _resting_by_side([_resting("UP", 0.5, 10.0, filled=10.0),
                          _resting("SIDEWAYS", 0.5, 10.0)])
    assert r == {"up_shares": 0.0, "up_usd": 0.0,
                 "dn_shares": 0.0, "dn_usd": 0.0}


def test_resting_by_side_empty_is_zero_not_missing():
    """Downstream reads four keys unconditionally; missing would render NaN."""
    from server.fleet_dash import _resting_by_side
    assert _resting_by_side([]) == {"up_shares": 0.0, "up_usd": 0.0,
                                    "dn_shares": 0.0, "dn_usd": 0.0}
    assert _resting_by_side(None)["up_usd"] == 0.0


# --- depthCell: executed, not grepped -------------------------------------
# Parsing proves the page is not blank. It does NOT prove a market holding
# zero orders renders as empty rather than as a confident 50/50 bar. That
# bug is invisible on a busy fleet and only shows on markets nobody watches,
# so it gets executed against fixtures.

_DEPTH_HARNESS = """
%s
const usd=(v,dp)=>'$'+Number(v||0).toFixed(dp==null?2:dp);
console.log(JSON.stringify(depthCell(%s)));
"""


def _render_depth(tmp_path, market: dict) -> str:
    import json
    src = next(s for s in _script_blocks() if "function depthCell" in s)
    start = src.index("function depthCell")
    rest = src[start + 1:]
    body = (src[start:start + 1 + rest.index("\nfunction ")]
            if "\nfunction " in rest else src[start:])
    f = tmp_path / "depth.js"
    f.write_text(_DEPTH_HARNESS % (body, json.dumps(market)), encoding="utf-8")
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0, f"depthCell threw:\n{r.stderr}"
    return json.loads(r.stdout)


BALANCED = {"up_usd": 63.0, "dn_usd": 63.0, "up_shares": 131.0,
            "dn_shares": 131.0, "our_up": 0.48, "our_dn_as_up": 0.48,
            "mid_up": 0.48, "age": 3, "gate": "NORMAL"}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_depth_cell_balanced_splits_evenly(tmp_path):
    html = _render_depth(tmp_path, BALANCED)
    assert 'class="dc-up" style="width:50%"' in html
    assert 'class="dc-dn" style="width:50%"' in html
    assert "131 Sh @ 48.0" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_depth_cell_zero_orders_renders_empty_never_fifty_fifty(tmp_path):
    """THE regression this feature exists to avoid.

    A market with no resting orders must not look identical to a market
    with perfectly balanced ones. No segments, no fabricated split.
    """
    html = _render_depth(tmp_path, {**BALANCED, "up_usd": 0, "dn_usd": 0,
                                    "up_shares": 0, "dn_shares": 0})
    assert "dc-up" not in html and "dc-dn" not in html
    assert "no resting orders" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_depth_cell_exited_market_says_so(tmp_path):
    html = _render_depth(tmp_path, {**BALANCED, "up_usd": 0, "dn_usd": 0,
                                    "up_shares": 0, "dn_shares": 0,
                                    "gate": "EXITED"})
    assert "orders pulled on exit" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_depth_cell_one_sided_names_the_empty_leg(tmp_path):
    html = _render_depth(tmp_path, {**BALANCED, "dn_usd": 0, "dn_shares": 0})
    assert 'class="dc-up" style="width:100%"' in html
    assert "none resting" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("up_usd,dn_usd,tick_pct", [(1000.0, 1.0, 100),
                                                    (1.0, 1000.0, 0)])
def test_depth_cell_label_clamps_but_tick_stays_true(tmp_path, up_usd,
                                                     dn_usd, tick_pct):
    """Label clamps to 14-86% so it cannot leave the cell.

    The tick does NOT clamp -- a clamped label over a clamped tick would
    misreport where capital actually divides.
    """
    html = _render_depth(tmp_path, {**BALANCED, "up_usd": up_usd,
                                    "dn_usd": dn_usd})
    label = float(re.search(r'dc-mid" style="left:([\d.]+)%', html).group(1))
    tick = float(re.search(r'dc-tick" style="left:([\d.]+)%', html).group(1))
    assert 14 <= label <= 86
    assert round(tick) == tick_pct
    assert abs(tick - label) > 1, "tick must not have been clamped with label"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_depth_cell_null_mid_does_not_render_zero_cents(tmp_path):
    """0.0 cents is a real price. Unknown must not masquerade as it."""
    html = _render_depth(tmp_path, {**BALANCED, "mid_up": None})
    assert "0.0" not in html.split('class="dc-bar"')[0]
    assert "&mdash;" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_depth_cell_stale_feed_is_marked(tmp_path):
    html = _render_depth(tmp_path, {**BALANCED, "age": 360})
    assert "dc-stale" in html
    assert "stale 6m" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_depth_cell_fresh_feed_is_not_marked(tmp_path):
    assert "stale" not in _render_depth(tmp_path, BALANCED)


def test_committed_column_replaced_by_depth_cell():
    assert "Resting depth &amp; mid" in FLEET_PAGE
    assert "${depthCell(m)}" in FLEET_PAGE


def test_ladder_is_gone():
    """Deleted in favour of depthCell; must not return as a second renderer."""
    assert "function ladder(" not in FLEET_PAGE


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
