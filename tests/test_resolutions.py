"""Settlement pass tests.

`resolutions` was empty in every database this project has produced. The
standing explanation was the universe -- markets resolving in 2027 cannot
settle inside a run measured in days (strategy/config.py
select_max_days_to_resolve). That stopped being true once the ranker was
capped at 7 days, and the table stayed empty anyway: `store.record_resolution`
had no caller. The pass that filled it was dropped in the pre-fleet rewrite
and survives only on the archive branch
(archive/legacy-bot-8788/strategy/main.py).

The archived pass asked Gamma `/events?slug=`. That endpoint is the wrong
index for this question, in two ways that both showed up on live data:

  - An event is not a market. `atp-zheng-kecmano-2026-08-02` carries 16 --
    the match, completed-match, first-set-winner, totals, handicaps -- each
    with its own condition_id and its own winner. Reading `markets[0]` scored
    7 of our first 8 live settlements against the wrong market.
  - Several market slugs are not event slugs at all, so the lookup returns
    nothing: game-2 markets, handicaps, and
    `will-bitcoin-dip-to-62k-july-27-august-2-2026` were all unsettleable.

The CLOB market endpoint is keyed on condition_id -- the identity we already
hold -- and states the winner outright instead of leaving it to be inferred
from prices. Settlement is the only ground truth this strategy has, and an
unsettled market holds its capital forever, so these tests pin down the pass
rather than the symptom.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLOB = "https://clob.polymarket.com"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    """Replays a payload per condition_id and records what was asked for."""

    def __init__(self, by_cond, raises=False):
        self.by_cond = by_cond
        self.raises = raises
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        cond = url.rsplit("/", 1)[-1]
        self.calls.append(cond)
        if self.raises:
            raise OSError("clob unreachable")
        return _Resp(self.by_cond.get(cond, {}))


def _market(closed=True, winner_index=0, token_ids=("tok-up", "tok-down")):
    """One CLOB market. `winner` is a flag on the token, not a price."""
    return {
        "condition_id": "cond-1",
        "closed": closed,
        "tokens": [
            {"token_id": token_ids[0], "outcome": "Up",
             "winner": winner_index == 0, "price": 1 if winner_index == 0 else 0},
            {"token_id": token_ids[1], "outcome": "Down",
             "winner": winner_index == 1, "price": 1 if winner_index == 1 else 0},
        ],
    }


def _seed_fill(store, cond="cond-1", slug="slug-1"):
    """A market only becomes resolvable once we hold a fill in it."""
    qid = store.log_quote(
        market_slug=slug, condition_id=cond, token_id="tok-up", side="UP",
        price=0.50, size=100.0, queue_ahead=0.0, mid=0.50,
        edge_vs_mid=0.01, t_remaining=None,
    )
    store.log_fill(
        quote_id=qid, market_slug=slug, condition_id=cond, token_id="tok-up",
        side="UP", price=0.50, size=100.0, mid_at_post=0.50,
        edge_vs_mid=0.01, queue_waited=0.0, seconds_to_fill=1.0,
        crossed=False, reason="tape",
    )


def test_closed_market_records_the_winning_token(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import resolve, store

    _seed_fill(store)
    monkeypatch.setattr(resolve, "_SESSION",
                        _Session({"cond-1": _market(winner_index=0)}))

    assert resolve.resolve_finished(CLOB) == 1
    with store.db() as c:
        assert c.execute(
            "SELECT condition_id, winning_token FROM resolutions").fetchone() \
            == ("cond-1", "tok-up")


def test_the_losing_side_is_never_recorded(monkeypatch, tmp_path):
    """A market can settle DOWN. Recording the first token unconditionally
    would invert the sign of every settled P&L."""
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import resolve, store

    _seed_fill(store)
    monkeypatch.setattr(resolve, "_SESSION",
                        _Session({"cond-1": _market(winner_index=1)}))

    resolve.resolve_finished(CLOB)
    with store.db() as c:
        assert c.execute("SELECT winning_token FROM resolutions").fetchone() \
            == ("tok-down",)


def test_the_market_is_fetched_by_condition_id(monkeypatch, tmp_path):
    """The whole point of moving off the slug lookup. condition_id is exact:
    it cannot collide with a sibling market inside the same event, and it
    works for the game-2 and handicap slugs that are not event slugs at all."""
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import resolve, store

    _seed_fill(store, cond="0xabc", slug="lol-sk-g2-game-handicap-home-1pt5")
    session = _Session({"0xabc": _market()})
    monkeypatch.setattr(resolve, "_SESSION", session)

    assert resolve.resolve_finished(CLOB) == 1
    assert session.calls == ["0xabc"]


def test_open_market_is_not_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import resolve, store

    _seed_fill(store)
    monkeypatch.setattr(resolve, "_SESSION",
                        _Session({"cond-1": _market(closed=False)}))

    assert resolve.resolve_finished(CLOB) == 0
    with store.db() as c:
        assert c.execute("SELECT count(*) FROM resolutions").fetchone()[0] == 0


def test_closed_market_with_no_declared_winner_is_skipped(monkeypatch, tmp_path):
    """A market can report closed before the winner flag is set. Recording
    now would write a settlement no later pass revisits -- the row is keyed by
    condition_id and would simply be found already present."""
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import resolve, store

    _seed_fill(store)
    m = _market()
    for t in m["tokens"]:
        t["winner"] = False
    monkeypatch.setattr(resolve, "_SESSION", _Session({"cond-1": m}))

    assert resolve.resolve_finished(CLOB) == 0
    assert store.unresolved() == [("cond-1", "slug-1")]


def test_two_declared_winners_is_refused(monkeypatch, tmp_path):
    """Ambiguity is not a coin flip. Skip and let a later pass see a fixed
    payload."""
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import resolve, store

    _seed_fill(store)
    m = _market()
    for t in m["tokens"]:
        t["winner"] = True
    monkeypatch.setattr(resolve, "_SESSION", _Session({"cond-1": m}))

    assert resolve.resolve_finished(CLOB) == 0
    assert store.unresolved() == [("cond-1", "slug-1")]


def test_recorded_market_leaves_the_unresolved_set(monkeypatch, tmp_path):
    """A settled market must stop being asked about, or every cycle re-fetches
    every market we have ever filled."""
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import resolve, store

    _seed_fill(store)
    assert store.unresolved() == [("cond-1", "slug-1")]

    session = _Session({"cond-1": _market()})
    monkeypatch.setattr(resolve, "_SESSION", session)
    resolve.resolve_finished(CLOB)

    assert store.unresolved() == []
    session.calls.clear()
    assert resolve.resolve_finished(CLOB) == 0
    assert session.calls == []


def test_transport_failure_does_not_raise(monkeypatch, tmp_path):
    """This runs inside the trading loop. A raising settlement pass would take
    the fleet down over a settlement lookup."""
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import resolve, store

    _seed_fill(store)
    monkeypatch.setattr(resolve, "_SESSION", _Session({}, raises=True))

    assert resolve.resolve_finished(CLOB) == 0
    assert store.unresolved() == [("cond-1", "slug-1")]


def test_malformed_payloads_are_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import resolve, store

    _seed_fill(store)
    session = _Session({"cond-1": {}})
    monkeypatch.setattr(resolve, "_SESSION", session)
    assert resolve.resolve_finished(CLOB) == 0

    # Closed, one token, winner set but no id to record.
    session.by_cond["cond-1"] = {
        "closed": True, "tokens": [{"winner": True, "outcome": "Up"}]}
    assert resolve.resolve_finished(CLOB) == 0

    # An error body instead of a market.
    session.by_cond["cond-1"] = {"error": "not found"}
    assert resolve.resolve_finished(CLOB) == 0

    with store.db() as c:
        assert c.execute("SELECT count(*) FROM resolutions").fetchone()[0] == 0


def test_one_bad_market_does_not_stop_the_rest(monkeypatch, tmp_path):
    """The pass walks every filled market. One malformed payload must not
    strand the markets behind it in the loop."""
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import resolve, store

    _seed_fill(store, cond="cond-bad", slug="slug-bad")
    _seed_fill(store, cond="cond-good", slug="slug-good")
    monkeypatch.setattr(resolve, "_SESSION", _Session({
        "cond-bad": {"closed": True, "tokens": "not-a-list"},
        "cond-good": _market(),
    }))

    assert resolve.resolve_finished(CLOB) == 1
    assert store.unresolved() == [("cond-bad", "slug-bad")]


# --- fleet wiring ---------------------------------------------------------
# A correct pass nobody calls is the bug being fixed, so the wiring is tested,
# not assumed.

def test_fleet_resolution_pass_respects_its_interval(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import fleet

    calls = []
    monkeypatch.setattr(fleet.resolve, "resolve_finished",
                        lambda host: calls.append(host) or 0)

    class _Cfg:
        clob_host = CLOB

    last = 1000.0
    assert fleet._maybe_resolve(_Cfg(), last, last + 1.0) == last
    assert calls == []

    now = last + fleet.RESOLVE_INTERVAL_SEC
    assert fleet._maybe_resolve(_Cfg(), last, now) == now
    assert calls == [CLOB]


def test_fleet_resolution_failure_still_advances_the_deadline(
        monkeypatch, tmp_path):
    """Otherwise a persistently failing venue is retried on every iteration of
    the trading loop instead of once per interval."""
    monkeypatch.setenv("MAKER_DB", str(tmp_path / "res.db"))
    from strategy import fleet

    def _boom(host):
        raise RuntimeError("clob down")

    monkeypatch.setattr(fleet.resolve, "resolve_finished", _boom)

    class _Cfg:
        clob_host = CLOB

    now = 1000.0 + fleet.RESOLVE_INTERVAL_SEC
    assert fleet._maybe_resolve(_Cfg(), 1000.0, now) == now
