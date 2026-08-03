"""The gate was unreachable, and lowering the threshold alone does not fix it.

`markout_min_sample` was 20. Measured on run/fleet.db over the 2026-08-02 run:
47 markout rows across 19 markets, and the BEST sampled market matured 7. So
`per_market_stats` returned `insufficient_sample` for every market on every
cycle, `next_state` returns the state unchanged on that verdict, and
`gate_state` stayed NORMAL for the whole run -- `market_gate` finished with
zero rows. No market was ever widened and none was ever exited.

Dropping the threshold to 8 still leaves 0/19 qualifying. Markets here rotate
daily and individually never accumulate a sample; what actually makes the gate
reachable is the POOLED verdict, which reaches n=43 on the same data.

The cap on that fallback is the other half. EXITED is terminal, and the pooled
mean is -4.75c/share -- past the catastrophic threshold -- so an uncapped
fallback would permanently blacklist all 19 markets at once, including the
three that were individually earning (+4.4c, +5.0c, +5.3c).
"""
import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy import gate, markout                          # noqa: E402
from strategy.config import load as load_cfg                # noqa: E402
from strategy.fleet import _gate_with_fleet_fallback        # noqa: E402


def _cfg():
    return dataclasses.replace(load_cfg(), markout_min_sample=8,
                               markout_fleet_min_sample=25,
                               markout_widen_threshold=-0.005,
                               markout_catastrophic_threshold=-0.020)


def _mk(drift, cid="c1", ref_mid=0.50, source="venue_clean"):
    """One markouts row whose LONGEST matured horizon carries `drift`.

    Mirrors the live schema: `_matured` reads ref_mid plus mid_h0..h2 and takes
    the last non-null, so h1 is the horizon under test and h2 is still open.
    """
    return {"condition_id": cid, "market_slug": "s", "side": "UP",
            "fill_price": 0.48, "size": 100.0, "ref_mid": ref_mid,
            "ref_mid_source": source,
            "mid_h0": ref_mid, "mid_h1": ref_mid + drift, "mid_h2": None}


@pytest.fixture
def rows(monkeypatch):
    """Swap the store for an in-memory list. No DB, no MAKER_DB, no fixtures on
    disk -- these tests are about the aggregation, not about SQLite."""
    box = []
    monkeypatch.setattr(markout.store, "markout_rows", lambda: list(box))
    return box


# --- fleet_stats() aggregation -----------------------------------------------

def test_fleet_stats_pools_across_markets(rows):
    """The whole point: five markets of six fills each is n=30 pooled, where
    every one of them reads insufficient_sample on its own."""
    for cid in ("a", "b", "c", "d", "e"):
        rows.extend(_mk(-0.02, cid=cid) for _ in range(6))
    stats = markout.fleet_stats(25)
    assert stats["n"] == 30
    assert stats["mean_per_share"] == pytest.approx(-0.02)
    assert stats["verdict"] == "losing"
    # ...and confirm the per-market path really does see nothing.
    assert all(v["verdict"] == "insufficient_sample"
               for v in markout.per_market_stats(8).values())


def test_fleet_stats_measures_drift_not_total_markout(rows):
    """Same correctness constraint as the per-market path: the verdict follows
    the market's move, never the offset we quoted under mid. A fill at 0.48
    into a 0.50 ref that never moved is 0c of drift, not +2c of edge."""
    rows.extend(_mk(0.0) for _ in range(30))
    assert markout.fleet_stats(25)["mean_per_share"] == pytest.approx(0.0)


def test_fleet_stats_holds_out_under_its_own_minimum(rows):
    """The pooled sample gets a noise guard too -- it gates every market at
    once, so acting early is the more expensive mistake."""
    rows.extend(_mk(-0.05) for _ in range(24))
    assert markout.fleet_stats(25)["verdict"] == "insufficient_sample"
    rows.append(_mk(-0.05))
    assert markout.fleet_stats(25)["verdict"] == "losing"


def test_fleet_stats_excludes_contaminated_rows(rows):
    """A live run that cannot subtract our own resting size from the reference
    mid measures our own footprint. Those rows must not reach the pool."""
    rows.extend(_mk(-0.05, source="contaminated") for _ in range(40))
    assert markout.fleet_stats(25)["verdict"] == "insufficient_sample"


def test_fleet_stats_ignores_fills_with_no_matured_horizon(rows):
    """A fill whose horizons are all still open contributes nothing -- it is
    not a zero, it is an absence."""
    rows.extend(_mk(-0.02) for _ in range(30))
    rows.extend({**_mk(0.0), "mid_h0": None, "mid_h1": None, "mid_h2": None}
                for _ in range(50))
    assert markout.fleet_stats(25)["n"] == 30


def test_fleet_stats_reads_earning_when_the_pool_is_positive(rows):
    rows.extend(_mk(+0.03) for _ in range(30))
    assert markout.fleet_stats(25)["verdict"] == "earning"


# --- the borrowed verdict is capped at WIDENED -------------------------------

def test_an_unsampled_market_inherits_the_fleet_verdict(rows):
    """Before the fallback this market held NORMAL forever, however badly the
    fleet was being picked off."""
    rows.extend(_mk(-0.01) for _ in range(30))
    own = {"verdict": "insufficient_sample", "mean_per_share": None, "n": 3}
    nxt, used = _gate_with_fleet_fallback(gate.NORMAL, own, _cfg())
    assert nxt == gate.WIDENED
    assert used["n"] == 30          # judged on the pool, and says so


def test_a_catastrophic_fleet_reading_stops_at_widened_not_exited(rows):
    """THE CAP. -4.75c/share is the real pooled mean off run/fleet.db and it is
    past the catastrophic threshold, so `next_state` alone returns EXITED --
    which is terminal, and would blacklist 19 markets on evidence about none of
    them in particular."""
    rows.extend(_mk(-0.0475) for _ in range(30))
    own = {"verdict": "insufficient_sample", "mean_per_share": None, "n": 0}
    assert gate.next_state(gate.NORMAL, markout.fleet_stats(25),
                           _cfg()) == gate.EXITED          # uncapped
    nxt, _ = _gate_with_fleet_fallback(gate.NORMAL, own, _cfg())
    assert nxt == gate.WIDENED                             # capped


def test_the_cap_holds_from_the_widened_state_too(rows):
    """A market already WIDENED by the pool must not then be exited by it on the
    next cycle -- that is the same terminal sentence one tick later."""
    rows.extend(_mk(-0.0475) for _ in range(60))
    own = {"verdict": "insufficient_sample", "mean_per_share": None, "n": 2}
    nxt, _ = _gate_with_fleet_fallback(gate.WIDENED, own, _cfg())
    assert nxt == gate.WIDENED


def test_an_earning_fleet_leaves_an_unsampled_market_normal(rows):
    rows.extend(_mk(+0.02) for _ in range(30))
    own = {"verdict": "insufficient_sample", "mean_per_share": None, "n": 1}
    nxt, _ = _gate_with_fleet_fallback(gate.NORMAL, own, _cfg())
    assert nxt == gate.NORMAL


def test_a_thin_pool_changes_nothing(rows):
    """Both samples short: the market keeps its state, exactly as before the
    fallback existed. The fallback supplies a verdict, it never invents one."""
    rows.extend(_mk(-0.05) for _ in range(5))
    own = {"verdict": "insufficient_sample", "mean_per_share": None, "n": 2}
    nxt, used = _gate_with_fleet_fallback(gate.NORMAL, own, _cfg())
    assert nxt == gate.NORMAL
    assert used["n"] == 2           # never borrowed, so its own stats stand


def test_an_already_exited_market_is_never_readmitted(rows):
    """EXITED is terminal and the pooled reading must not be a route back into
    the book -- not even a positive one."""
    rows.extend(_mk(+0.05) for _ in range(30))
    own = {"verdict": "insufficient_sample", "mean_per_share": None, "n": 0}
    nxt, _ = _gate_with_fleet_fallback(gate.EXITED, own, _cfg())
    assert nxt == gate.EXITED


# --- a market's OWN sample still reaches terminal EXITED ---------------------

def test_a_market_with_its_own_sample_can_still_exit(rows):
    """The cap applies to BORROWED evidence only. A market that measured its own
    catastrophic markout is judged on its own reading, and that verdict is
    allowed to be final. n=8 is the lowered per-market minimum."""
    rows.extend(_mk(+0.05) for _ in range(30))       # fleet is fine
    own = {"verdict": "losing", "mean_per_share": -0.03, "n": 8}
    nxt, used = _gate_with_fleet_fallback(gate.NORMAL, own, _cfg())
    assert nxt == gate.EXITED
    assert used is own              # never consulted the pool


def test_own_sample_takes_the_graduated_path_for_a_smaller_loss(rows):
    """-1c clears the widen threshold but not the catastrophic one, so the
    NORMAL -> WIDENED -> EXITED path is intact on own evidence."""
    rows.extend(_mk(-0.05) for _ in range(30))
    own = {"verdict": "losing", "mean_per_share": -0.01, "n": 8}
    assert _gate_with_fleet_fallback(gate.NORMAL, own, _cfg())[0] == gate.WIDENED
    second = {"verdict": "losing", "mean_per_share": -0.01, "n": 16}
    assert _gate_with_fleet_fallback(gate.WIDENED, second,
                                     _cfg())[0] == gate.EXITED


def test_an_earning_market_is_not_dragged_down_by_a_toxic_fleet(rows):
    """The three markets an uncapped fallback would have killed: val-var-drx1
    +4.4c, cs2-sashi-blackp +5.0c, lol-bro1-foxy +5.3c. With a sample of their
    own they keep their own verdict and the pool is never consulted."""
    rows.extend(_mk(-0.0475) for _ in range(60))
    for own_mean in (0.044, 0.050, 0.053):
        own = {"verdict": "earning", "mean_per_share": own_mean, "n": 8}
        assert _gate_with_fleet_fallback(gate.NORMAL, own,
                                         _cfg())[0] == gate.NORMAL


# --- the reachability claim itself ------------------------------------------

def test_the_threshold_drop_alone_would_not_have_helped(rows):
    """Pins the finding that motivated the fleet path: at the observed
    per-market depth of 7, lowering `markout_min_sample` from 20 to 8 still
    yields no verdict anywhere. The pool is what makes the gate reachable."""
    for i in range(19):
        rows.extend(_mk(-0.05, cid=f"m{i}") for _ in range(7))
    assert all(v["verdict"] == "insufficient_sample"
               for v in markout.per_market_stats(8).values())
    assert markout.fleet_stats(25)["verdict"] == "losing"
