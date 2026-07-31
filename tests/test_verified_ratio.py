"""Store-level coverage for `strategy.store.verified_ratio()`.

`verified_ratio()` is the Phase A decision-gate number: the share-weighted
fraction of fills the trade tape actually confirmed. The ratio lives on the
edge of three categories whose semantics differ:

  - `reason='tape'`           in `fills`            -> verified
  - `reason IN ('queue','sweep')` in `fills`        -> legacy (pre-U1)
  - `reason LIKE 'unverified_%'`  in `unverified_fills` -> unverified

The engine-level read in `tests/test_fills.py` covers the happy path, but
what reads on a real DB are the boundary cases: empty (nothing observed),
legacy-only (delta-credited fills predate the tape gate), and mixed (real
verification happening alongside speculation). The number consumers decide
on is `ratio`, and the cases that distinguish it are at `verified_*.shares == 0`,
`unverified_*.shares == 0`, and both > 0.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fresh(monkeypatch, tmp_path, name="verified_ratio.db"):
    """Point the store at an empty DB and hand back the module.

    Canonical pattern: setenv MAKER_DB before the `from strategy import store`
    so that the module-level `_cfg = load_cfg()` reads our tmp path. Any later
    call into `store.<fn>` (including `verified_ratio()`) opens that DB.
    """
    monkeypatch.setenv("MAKER_DB", str(tmp_path / name))
    from strategy import store
    return store


def test_an_empty_db_returns_none_and_zero_counts(monkeypatch, tmp_path):
    """Nothing observed -> ratio must read as None, not a confident 0.0.

    An empty run is not a measurement. A confident zero would invite anyone
    downstream to act on a pay-for-nothing headline; the dataset hasn't even
    begun to discriminate between the strategy working and the engine
    over-crediting phantom fills.
    """
    store = _fresh(monkeypatch, tmp_path)
    out = store.verified_ratio()
    assert out == {
        "verified_fills": 0, "verified_shares": 0.0,
        "unverified_fills": 0, "unverified_shares": 0.0,
        "unverified_sweep_shares": 0.0,
        "legacy_fills": 0, "legacy_shares": 0.0,
        "ratio": None,
    }


def test_a_legacy_only_db_returns_none_and_reports_legacy_separately(
        monkeypatch, tmp_path):
    """Pre-U1 `queue`/`sweep` rows are NOT verification.

    They are deltas the engine credited before the trade tape existed --
    exactly what the ratio measures. Counting them as anything other than
    `legacy_` lets an old database force a post-U1 verdict. Naive
    `SUM(reason != 'cross')` over 302 queue + 37 sweep + 3 tape reads
    `verified = 339/342 ~= 0.991` -- a perfect score, on the data whose
    unreliability motivated the unit.
    """
    store = _fresh(monkeypatch, tmp_path, name="legacy.db")
    for i, reason in enumerate(("queue",) * 12 + ("sweep",) * 4):
        store.log_fill(market_slug="l", condition_id="c", token_id="t",
                       side="UP", price=0.50, size=10, reason=reason)
    out = store.verified_ratio()

    # No tape fills were ever written.
    assert out["verified_fills"] == 0
    assert out["verified_shares"] == 0.0
    # Nothing in `unverified_fills` either -- log_unverified_fill was never called.
    assert out["unverified_fills"] == 0
    assert out["unverified_shares"] == 0.0
    assert out["unverified_sweep_shares"] == 0.0
    # All 16 rows are correctly bucketed as legacy.
    assert out["legacy_fills"] == 16
    assert out["legacy_shares"] == 160.0
    # ratio is None because `verified + unverified == 0`. Legacy rows do NOT
    # enter the denominator; that's the whole point of separating them out.
    assert out["ratio"] is None


def test_a_mixed_run_computes_ratio_from_tape_against_unverified_only(
        monkeypatch, tmp_path):
    """`ratio = verified_shares / (verified_shares + unverified_shares)`.

    The mix chosen here exercises every branch the SQL has to discriminate
    `reason` on, while keeping the arithmetic trivial (the denominator is
    small enough to verify by hand). Legacy rows are included in the writes
    to confirm they STILL don't enter the ratio: 80 legacy shares are
    written but the expected numerator/denominator sees only `tape` and
    `unverified_*`.
    """
    store = _fresh(monkeypatch, tmp_path, name="mixed.db")

    # 60 tape shares across 3 rows. Tested as 3 separate rows to confirm the
    # SUM groups correctly.
    for size in (10, 20, 30):
        store.log_fill(market_slug="m", condition_id="c", token_id="t",
                       side="UP", price=0.50, size=size, reason="tape")
    # 40 unverified shares, half from `unverified_sweep` and half from
    # `unverified_queue` -- both bucket into `unverified_shares`, but the
    # sweep subcategory is reported separately.
    store.log_unverified_fill(
        ts=1.0, market_slug="m", condition_id="c", token_id="t",
        side="UP", price=0.50, size=20, queue_waited=0,
        reason="unverified_sweep")
    store.log_unverified_fill(
        ts=2.0, market_slug="m", condition_id="c", token_id="t",
        side="UP", price=0.50, size=20, queue_waited=0,
        reason="unverified_queue")
    # 80 legacy shares that MUST be ignored by the ratio.
    for size in (40, 40):
        store.log_fill(market_slug="m", condition_id="c", token_id="t",
                       side="UP", price=0.50, size=size, reason="queue")

    out = store.verified_ratio()

    assert out["verified_fills"] == 3
    assert out["verified_shares"] == 60.0
    assert out["unverified_fills"] == 2
    assert out["unverified_shares"] == 40.0
    # Subcategory: only the sweep half shows up in `unverified_sweep_shares`.
    assert out["unverified_sweep_shares"] == 20.0
    # Legacy kept separate so the ratio cannot be skewed by pre-U1 history.
    assert out["legacy_fills"] == 2
    assert out["legacy_shares"] == 80.0
    # 60 / (60 + 40) = 0.6. Legacy's 80 are NOT in the denominator.
    assert out["ratio"] == 60.0 / (60.0 + 40.0)


def test_dashboard_renders_real_verified_ratio_when_db_has_tape_fills(
        monkeypatch, tmp_path):
    """REGRESSION. server.fleet_dash.py calls `store.verified_ratio()` inside
    a try/except, but the file used to have NO `from strategy import store` at
    module top -- the except caught a NameError every request, so the live
    Verified KPI tile on the dashboard was rendering the all-zeros fallback
    even with real tape-backed fills in the fleet DB.

    This test points the dashboard's module-level `DB` and `RUN` at a tmp
    dir whose DB has a tape-backed fill, calls `dash.fleet()` directly, and
    asserts the totals carry the seeded share count -- not the zeros the
    except branch would produce.
    """
    store = _fresh(monkeypatch, tmp_path, name="dash.db")
    # A tape-backed fill is the only kind `verified_ratio()` counts as
    # confirmed by the trade tape; one row is enough to populate the tile.
    store.log_fill(market_slug="d", condition_id="c", token_id="t",
                   side="UP", price=0.50, size=10, reason="tape")
    # server.fleet_dash binds `DB` and `RUN` at module load -- patch them
    # before calling functions, so the dashboard reads the same tmp DB I
    # just seeded.
    import server.fleet_dash as dash
    monkeypatch.setattr(dash, "DB", tmp_path / "dash.db")
    monkeypatch.setattr(dash, "RUN", tmp_path)
    # Defense-in-depth: dash.DB (used by the dashboard's stdlib sqlite3
    # readers) and `store._cfg.db_path()` (used by store.verified_ratio())
    # MUST resolve to the same file, or the regression test passes for the
    # wrong reason -- a write to one file and a read from another. Lock
    # the coupling here so a future refactor of either module fails loudly.
    assert dash.DB.resolve() == store._cfg.db_path().resolve()
    # dash.fleet() short-circuits with an error payload if it can't find the
    # fleet-state file; an empty spec list drives it through to the totals
    # where the verified tile is computed.
    (tmp_path / "fleet_state.json").write_text("[]", encoding="utf-8")

    payload = dash.fleet()

    # The early-return path was bypassed, so the real verified_ratio() ran
    # -- and the import it needs is now in place.
    assert "error" not in payload, payload
    vr = payload["totals"]["verified"]
    assert vr["verified_fills"] == 1
    assert vr["verified_shares"] == 10.0
    # 10 / (10 + 0) -- no unverified rows in this test, so the ratio is 1.0.
    assert vr["ratio"] == 1.0
