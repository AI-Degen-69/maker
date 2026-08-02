"""Sizing a market that pays no rewards at all (U6).

Six runs produced 9 tape-backed fills in 74 hours and zero settled
resolutions. The universe was the cause: the ranker funds only markets with
`rewards.rates != null`, the allocator values a market at `daily x share`, and
a market with `clobRewards: 0` therefore scores exactly zero however much it
trades. bitcoin-up-or-down-* turns ~$92k in 24 hours and resolves the same day
-- it is precisely the market a fill-based measurement needs, and it was
unfundable by construction.

Spread capture gives that market a pot in the same units, so it competes in the
same water-fill. The $1.50/day payout floor does NOT follow it across: that is
the venue's minimum reward DISTRIBUTION, and nothing is distributed on a market
that pays no rewards.
"""
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.config import load as load_cfg          # noqa: E402
from strategy.fleet import MarketState, reallocate    # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "spread.db"))


def _spec(cid="btc", daily=0.0, volume=92_000.0, spread=0.01, min_size=100):
    """A liquid short-dated market with no reward funding -- the shape of
    bitcoin-up-or-down-*. `daily` is 0 because the venue pays it nothing for
    resting; `volume_24h` and `spread` are what it does pay."""
    s = {"cid": cid, "title": "Bitcoin Up or Down", "slug": "btc-up-or-down",
         "daily": daily, "min_size": min_size, "max_spread": 4.5,
         "tick": 0.001, "shares": 120, "volume_24h": volume,
         "days_to_resolve": 0.5, "est_income": 0.0, "est_capital": 120.0,
         "return_pct_day": 0.0, "their_score": 100.0}
    if spread is not None:
        s["spread"] = spread
    return s


def _measured(spec, theirs=612.0, base=None):
    """Sampled but not currently quoting -- the state every market is in on the
    sweep after a restart."""
    st = MarketState(spec, base or load_cfg())
    st.cfg = replace(st.cfg, quote_shares=0)
    st.observe_theirs(0.0, theirs, window_sec=1800.0)
    return st


def test_a_zero_reward_market_is_funded_on_its_spread():
    """The whole point. Same market, same competition: unfundable while its
    only income is the reward pot, funded once the spread counts."""
    base = load_cfg()
    st = _measured(_spec(), base=base)
    assert reallocate([st], base).get("btc", 0) > 0
    assert st.cfg.quote_shares > 0


def test_the_payout_floor_no_longer_reaches_a_spread_market():
    """The floor scoping, isolated: the SAME income, funded as spread and
    refused as rewards.

    $8.5k/24h at 1c is a ~$42/day pot; against 612 of competing score the
    water-fill funds it to roughly $70, which earns about $1.45/day -- under
    the $1.50 minimum distribution and above the 2%/day marginal floor, so it
    is the payout rule and only the payout rule that decides it.
    """
    base = load_cfg()
    spread_mkt = _measured(_spec(cid="by_spread", volume=8_500.0, min_size=20),
                           base=base)
    assert reallocate([spread_mkt], base).get("by_spread", 0) > 0

    # Identical income, arriving as a reward distribution instead. The venue
    # pays nothing under $1/day, so this one stays unfunded.
    reward_mkt = _measured(_spec(cid="by_rewards", daily=42.5, volume=0.0,
                                 min_size=20), base=base)
    assert reallocate([reward_mkt], base).get("by_rewards", 0) == 0


def test_a_zero_reward_market_with_no_volume_is_not_funded():
    """No pot and no tape is an UNKNOWN market, not a free one. It must not
    size as though it faced no competition -- that is the single most
    attractive-looking input the allocator can be handed."""
    base = load_cfg()
    st = _measured(_spec(cid="dead", volume=0.0), base=base)
    assert reallocate([st], base).get("dead", 0) == 0
    assert st.cfg.quote_shares == 0


def test_a_rerank_re_reads_the_pot_from_the_fresh_spec():
    """For a spread market the pot IS the volume estimate, so it must move.

    A market surviving a re-rank keeps its MarketState object -- the loop only
    constructs one for a cid it has not seen. The pot was computed once in
    __init__, so a market whose 24h volume halved kept sizing and reporting
    against the volume observed at process start, for the life of the process.
    """
    base = load_cfg()
    st = MarketState(_spec(cid="fading", volume=100_000.0), base)
    rich = st.pot
    assert rich > 0

    st.refresh_pot(_spec(cid="fading", volume=10_000.0), base)
    assert st.pot == pytest.approx(rich / 10.0)

    # And the other direction: a market that finds volume must be re-funded.
    st.refresh_pot(_spec(cid="fading", volume=100_000.0), base)
    assert st.pot == pytest.approx(rich)


def test_a_rerank_can_flip_a_market_between_reward_and_spread_funding():
    """`source` is derived from the fresh spec too, not frozen at startup."""
    base = load_cfg()
    st = MarketState(_spec(cid="flip", daily=0.0, volume=50_000.0), base)
    assert st.source == "spread"

    st.refresh_pot(_spec(cid="flip", daily=40.0, volume=50_000.0), base)
    assert st.source == "rewards"
    assert st.pot == 40.0, "a funded pot is the venue's number, not the model's"


def test_a_funded_market_whose_volume_dies_is_driven_back_to_zero():
    """THE CASE THE TEST ABOVE CANNOT SEE.

    `_measured` sets quote_shares=0 before calling, so that assertion holds
    whatever reallocate does. The reachable failure is the opposite state: a
    spread market funded on an earlier sweep whose volume LATER reads 0. It
    still has avg_theirs, so it is measured, and it used to be skipped -- which
    the sizing loop reads as "never sampled" and honours by keeping the
    previous size. The market went on quoting at its funded size with no pot
    behind it.
    """
    base = load_cfg()
    st = _measured(_spec(cid="was_live", volume=0.0), base=base)
    # Funded last sweep, back when the tape still showed volume.
    st.cfg = replace(st.cfg, quote_shares=120)
    assert st.pot == 0.0, "fixture must present a market with nothing behind it"

    assert reallocate([st], base).get("was_live") == 0, (
        "a measured market with no pot must be reported at 0, not omitted")
    assert st.cfg.quote_shares == 0, (
        "it kept quoting its previously funded size with no pot behind it")


def test_an_unsampled_market_still_keeps_its_size():
    """The other half of the contract, which the fix must not break.

    Absence from the allocation means "never measured" and only that. Such a
    market keeps its current size, because sizing it off a guess is worse than
    leaving it alone.
    """
    base = load_cfg()
    st = MarketState(_spec(cid="unseen", volume=0.0), base)
    st.cfg = replace(st.cfg, quote_shares=120)   # no observe_theirs call
    assert reallocate([st], base).get("unseen") is None
    assert st.cfg.quote_shares == 120


def test_a_missing_spread_falls_back_to_the_configured_default():
    """The spec is written by the ranker and may not carry a spread yet. The
    market is still fundable off volume alone, at the 1c book the up-or-down
    series is observed to run."""
    base = load_cfg()
    st = _measured(_spec(cid="nospread", spread=None), base=base)
    assert reallocate([st], base).get("nospread", 0) > 0


def test_reward_markets_are_unaffected_by_the_spread_path():
    """A funded market keeps its reward pot and its payout floor: a $50/day pot
    against 400,000 of competing score pays under $1.50/day at any size the
    budget reaches, and must still be refused."""
    base = load_cfg()
    ok = _measured(_spec(cid="rewarded", daily=50.0, volume=0.0), base=base)
    crowded = _measured(_spec(cid="crowded", daily=50.0, volume=0.0),
                        theirs=400_000.0, base=base)
    out = reallocate([ok, crowded], base)
    assert out.get("rewarded", 0) > 0
    assert out.get("crowded", 0) == 0


def test_the_two_income_sources_share_one_budget():
    """Reward and spread markets are sized in one water-fill, so the budget
    still binds across both. A separate pass for the new path would double the
    fleet's committed capital without anything saying so."""
    base = load_cfg()
    states = [_measured(_spec(cid=f"s{i}"), base=base) for i in range(4)]
    states += [_measured(_spec(cid=f"r{i}", daily=50.0, volume=0.0), base=base)
               for i in range(4)]
    out = reallocate(states, base)
    assert sum(out.values()) <= base.allocation_budget
    assert any(v > 0 for k, v in out.items() if k.startswith("s"))
