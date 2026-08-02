"""The headline number must equal the bridge printed directly beneath it.

CodeRabbit caught this on PR #3: the bridge summed Realized + Earned Rebates +
Paired + Unhedged and labelled the result "Total Liquidation P&L", while the
big `heroValue` above it showed `liquidate_now_pnl` WITHOUT the rebate. The two
disagreed by exactly the rebate -- $8.03 live -- on a page whose entire purpose
is to make that arithmetic checkable by eye.

Nothing in the suite could catch it. `test_dashboard_page` parses the script
but never evaluates it, and the Python tests never touch the rendering. So this
runs the actual `tick()` arithmetic under node against a synthetic payload and
asserts the identity that was broken.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.fleet_dash import PAGE  # noqa: E402

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# Every term the hero bridge reads, with values chosen so no two sums collide
# by coincidence -- a test that passes because 0 == 0 proves nothing.
TOTALS = {
    "realized": 68.80,
    "rent_reward": 1.25,
    "maker_rebate": 8.03,
    "locked_pair": -59.35,
    "naked_exit": 100.00,
    "at_risk": 42.01,
}
# Derived, not typed in. The backend defines this as realized + locked_pair +
# (naked_exit - at_risk), so hardcoding it lets the fixture drift out of step
# with its own components -- which it promptly did, by a cent, and the first
# assertion below caught the fixture rather than the code.
TOTALS["liquidate_now_pnl"] = (TOTALS["realized"] + TOTALS["locked_pair"]
                               + TOTALS["naked_exit"] - TOTALS["at_risk"])


def _hero_expressions() -> str:
    """The two lines under test, lifted from the page's own source.

    Extracted rather than restated: a copy of the formula here would keep
    passing after the page's copy drifted, which is precisely the failure
    this file exists to prevent.
    """
    src = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", PAGE))
    wanted = ("const rent =", "const liquidation =")
    lines = [ln.strip() for ln in src.splitlines()
             if any(ln.strip().startswith(w) for w in wanted)]
    assert len(lines) == 2, f"expected rent+liquidation declarations, got {lines}"
    return "\n".join(lines)


def _run(js: str) -> dict:
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


def test_headline_equals_the_bridge_total_it_sits_above():
    js = f"""
    const t = {json.dumps(TOTALS)};
    {_hero_expressions()}
    const bridge = t.realized + rent + t.locked_pair + (t.naked_exit - t.at_risk);
    console.log(JSON.stringify({{headline: liquidation, bridge, rent}}));
    """
    out = _run(js)
    # The bridge is built from position terms and the headline from the
    # backend's own liquidation figure; they are two routes to one number.
    assert out["headline"] == pytest.approx(out["bridge"]), (
        f"headline {out['headline']} != bridge {out['bridge']} -- the big "
        f"number and the sum printed under it disagree")


def test_headline_includes_the_rebate_rather_than_dropping_it():
    """The specific regression: `liquidation` must not equal the bare backend
    figure while a rebate is outstanding."""
    js = f"""
    const t = {json.dumps(TOTALS)};
    {_hero_expressions()}
    console.log(JSON.stringify({{liquidation, raw: t.liquidate_now_pnl, rent}}));
    """
    out = _run(js)
    assert out["rent"] > 0, "fixture must exercise a non-zero rebate"
    assert out["liquidation"] == pytest.approx(out["raw"] + out["rent"])
    assert out["liquidation"] != pytest.approx(out["raw"])


def test_rent_sums_both_venue_programs():
    """Liquidity rewards and maker rebates are disjoint; both belong in `rent`."""
    js = f"""
    const t = {json.dumps(TOTALS)};
    {_hero_expressions()}
    console.log(JSON.stringify({{rent}}));
    """
    assert _run(js)["rent"] == pytest.approx(
        TOTALS["rent_reward"] + TOTALS["maker_rebate"])


def test_missing_rebate_keys_do_not_produce_nan():
    """An older backend, or a failed read, must degrade to the bare figure.

    `undefined + number` is NaN in JS, and a NaN headline renders as "$NaN"
    across the whole hero -- worse than the wrong number it replaced.
    """
    js = f"""
    const t = {json.dumps({k: v for k, v in TOTALS.items()
                           if k not in ("rent_reward", "maker_rebate")})};
    {_hero_expressions()}
    console.log(JSON.stringify({{liquidation, rent}}));
    """
    out = _run(js)
    assert out["rent"] == 0
    assert out["liquidation"] == pytest.approx(TOTALS["liquidate_now_pnl"])
