"""Tests for the live execution monitor (live/dash/live_dash.py).

Verifies the single-cycle dashboard behavior across all essential operational states:
1. Empty database (graceful zero state)
2. RESTING pair (open bids, 0 fills)
3. NAKED pair (imbalanced fills, live dollar risk and timer)
4. BALANCED pair (both legs matched, inventory neutral)
5. Stale poll detection (>30s delay)
6. Unattributed order flagging
7. Reconcile lock status
8. Read-only SQLite URI enforcement (no writes possible)
9. FastAPI HTML and JSON endpoint integration
"""
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.order_registry import SCHEMA
from dash.live_dash import (
    PAGE_HTML,
    app,
    compute_scan_state,
    resolve_db_path,
    set_db_override,
    set_guardrail_heartbeat_override,
    set_heartbeat_override,
    set_ring_override,
    _cycle_stream_sse,
)

NODE = shutil.which("node")


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary SQLite database initialized with the real registry schema."""
    db_file = tmp_path / "live.db"
    con = sqlite3.connect(str(db_file))
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return db_file


@pytest.fixture
def client(temp_db):
    """Test client configured to query the temporary test database."""
    set_db_override(temp_db)
    yield TestClient(app)
    set_db_override(None)


def test_api_and_html_endpoints(client, temp_db):
    """Test the FastAPI / and /api/state endpoints."""
    # Test HTML index
    res_html = client.get("/")
    assert res_html.status_code == 200
    assert "Spread Hunter — Live Cycle Monitor" in res_html.text
    assert "<script>" in res_html.text

    # Test /api/state
    res_json = client.get("/api/state")
    assert res_json.status_code == 200
    data = res_json.json()
    assert "empty" in data
    assert "capital" in data
    assert "pairs" in data


def test_pairs_activity_endpoint_aggregates_ring(client, tmp_path):
    """/api/pairs-activity counts pairs_* ring actions per cycle and per pair,
    ignoring non-pairs events and keeping each pair's most recent action.
    """
    ring = tmp_path / "cycle_events.jsonl"
    rows = [
        {"ts": "2026-08-21T02:00:01Z", "service": "engine", "cycle": 1,
         "phase": "settling", "action": "pairs_completed",
         "market_slug": "", "reason": "", "latency_ms": 0.0, "pid": 1,
         "extra": {"pair_id": "pair-A"}},
        {"ts": "2026-08-21T02:00:06Z", "service": "engine", "cycle": 1,
         "phase": "settling", "action": "pairs_would_complete",
         "market_slug": "", "reason": "", "latency_ms": 0.0, "pid": 1,
         "extra": {"pair_id": "pair-B"}},
        {"ts": "2026-08-21T02:00:11Z", "service": "engine", "cycle": 2,
         "phase": "settling", "action": "pairs_exited",
         "market_slug": "", "reason": "", "latency_ms": 0.0, "pid": 1,
         "extra": {"pair_id": "pair-A"}},
        {"ts": "2026-08-21T02:00:16Z", "service": "engine", "cycle": 2,
         "phase": "settling", "action": "pairs_error",
         "market_slug": "", "reason": "", "latency_ms": 0.0, "pid": 1,
         "extra": {"pair_id": "pair-C"}},
        {"ts": "2026-08-21T02:00:21Z", "service": "engine", "cycle": 1,
         "phase": "quoting", "action": "decide",
         "market_slug": "x", "reason": "", "latency_ms": 0.0, "pid": 1,
         "extra": {}},
    ]
    ring.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    set_ring_override(ring)
    try:
        res = client.get("/api/pairs-activity")
        assert res.status_code == 200
        data = res.json()
        assert data["last_cycle"] == 2
        assert data["last_cycle_counts"] == {"exited": 1, "error": 1}
        assert data["totals"] == {
            "completed": 1, "would_complete": 1,
            "exited": 1, "error": 1,
        }
        by_id = {p["pair_id"]: p for p in data["per_pair"]}
        assert by_id["pair-A"]["action"] == "exited"   # most recent wins
        assert by_id["pair-B"]["action"] == "would_complete"
        assert by_id["pair-C"]["action"] == "error"
        assert by_id["pair-A"]["cycle"] == 2
    finally:
        set_ring_override(None)


def test_guardrail_health_endpoint_reports_alive_watcher(client, tmp_path):
    """/api/guardrail-health reads the watcher's heartbeat: a fresh one means
    running, with pid/cycle from the file and the alert total from the ring.
    """
    ring = tmp_path / "cycle_events.jsonl"
    hb = tmp_path / "guardrail_watch_heartbeat.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    hb.write_text(json.dumps([{
        "pid": 4242, "started_at": now_iso, "ts": now_iso, "cycle": 9,
    }]), encoding="utf-8")
    rows = [
        {"ts": "2026-08-21T02:00:05Z", "service": "engine", "cycle": 3,
         "phase": "settling", "action": "guardrail_alert",
         "market_slug": "", "reason": "REPEAT-EXIT", "latency_ms": 0.0,
         "pid": 1, "extra": {}},
        {"ts": "2026-08-21T02:00:10Z", "service": "engine", "cycle": 4,
         "phase": "settling", "action": "guardrail_alert",
         "market_slug": "", "reason": "OVER-CAP-PAIR", "latency_ms": 0.0,
         "pid": 1, "extra": {}},
    ]
    ring.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    set_ring_override(ring)
    set_guardrail_heartbeat_override(hb)
    try:
        res = client.get("/api/guardrail-health")
        assert res.status_code == 200
        h = res.json()
        assert h["running"] is True
        assert h["pid"] == 4242
        assert h["cycle"] == 9
        assert h["age_s"] is not None and h["age_s"] <= 30.0
        assert h["alerts_total"] == 2
        assert h["last_alert_kind"] == "OVER-CAP-PAIR"  # newest first
    finally:
        set_ring_override(None)
        set_guardrail_heartbeat_override(None)


def test_guardrail_health_endpoint_flags_stale_watcher(client, tmp_path):
    """A heartbeat older than the stale threshold reads as DOWN -- the
    silent-failure case the chip exists to surface.
    """
    hb = tmp_path / "guardrail_watch_heartbeat.json"
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hb.write_text(json.dumps([{
        "pid": 99, "started_at": old, "ts": old, "cycle": 3,
    }]), encoding="utf-8")
    set_guardrail_heartbeat_override(hb)
    try:
        res = client.get("/api/guardrail-health")
        assert res.status_code == 200
        h = res.json()
        assert h["running"] is False
        assert h["age_s"] is not None and h["age_s"] > 30.0
        assert h["pid"] == 99   # pid still readable; the chip says DOWN
    finally:
        set_guardrail_heartbeat_override(None)


def test_guardrail_health_endpoint_absent_heartbeat(client, tmp_path):
    """No heartbeat file at all (watcher never ran / old code) -> DOWN, empty
    fields, no crash.
    """
    empty_ring = tmp_path / "empty_ring.jsonl"
    empty_ring.write_text("", encoding="utf-8")
    set_ring_override(empty_ring)
    set_guardrail_heartbeat_override(tmp_path / "missing.json")
    try:
        res = client.get("/api/guardrail-health")
        assert res.status_code == 200
        h = res.json()
        assert h["running"] is False
        assert h["pid"] is None
        assert h["age_s"] is None
        assert h["alerts_total"] == 0
    finally:
        set_ring_override(None)
        set_guardrail_heartbeat_override(None)


def test_guardrail_alerts_endpoint_returns_newest_first(client, tmp_path):
    """/api/guardrail-alerts surfaces guardrail_alert ring events newest-first
    with kind/subject/detail mapped from reason/extra, ignoring other events.
    """
    ring = tmp_path / "cycle_events.jsonl"
    rows = [
        {"ts": "2026-08-21T02:00:05Z", "service": "engine", "cycle": 3,
         "phase": "settling", "action": "guardrail_alert",
         "market_slug": "", "reason": "REPEAT-EXIT", "latency_ms": 0.0,
         "pid": 1, "extra": {"subject": "pair-0xAAA exited twice",
                              "detail": "2 exits in window"}},
        {"ts": "2026-08-21T02:00:10Z", "service": "engine", "cycle": 4,
         "phase": "settling", "action": "guardrail_alert",
         "market_slug": "", "reason": "OVER-CAP-PAIR", "latency_ms": 0.0,
         "pid": 1, "extra": {"subject": "0x3bae865f pair $1.12",
                              "detail": "over 0.995 cap"}},
        {"ts": "2026-08-21T02:00:15Z", "service": "engine", "cycle": 4,
         "phase": "quoting", "action": "decide",
         "market_slug": "x", "reason": "", "latency_ms": 0.0, "pid": 1,
         "extra": {}},
    ]
    ring.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    set_ring_override(ring)
    try:
        res = client.get("/api/guardrail-alerts")
        assert res.status_code == 200
        alerts = res.json()["alerts"]
        assert len(alerts) == 2
        # newest first (the decide event is not an alert and is ignored)
        assert alerts[0]["kind"] == "OVER-CAP-PAIR"
        assert alerts[0]["subject"] == "0x3bae865f pair $1.12"
        assert alerts[0]["detail"] == "over 0.995 cap"
        assert alerts[0]["cycle"] == 4
        assert alerts[1]["kind"] == "REPEAT-EXIT"
        assert alerts[1]["subject"] == "pair-0xAAA exited twice"
    finally:
        set_ring_override(None)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_dashboard_script_parses(tmp_path):
    """Ensures no syntax errors in the inline JS block."""
    scripts = re.findall(r"<script>([\s\S]*?)</script>", PAGE_HTML)
    assert scripts, "Dashboard HTML must contain a <script> block"
    for i, src in enumerate(scripts):
        f = tmp_path / f"live_dash_{i}.js"
        f.write_text(src, encoding="utf-8")
        res = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert res.returncode == 0, f"Script parse error: {res.stderr}"


def test_api_state_ignores_a_request_supplied_db_path(client, tmp_path):
    """The database is chosen by CLI or env only, never by the caller.

    A query parameter here let anything that could reach the port read an
    arbitrary SQLite file and probe local paths through the error text.
    """
    other = tmp_path / "somewhere_else.db"
    con = sqlite3.connect(str(other))
    con.executescript(SCHEMA)
    con.commit()
    con.close()

    res = client.get("/api/state", params={"db": str(other)})
    assert res.status_code == 200
    assert str(other) not in res.json()["db_path"]


def test_database_values_are_escaped_before_innerhtml():
    """Token ids and statuses reach innerHTML, and the venue writes some of them."""
    assert "function esc(v)" in PAGE_HTML
    for interpolation in (
        "${esc(o.side || 'BUY')}",
        "${esc((o.token_id || '').slice(0, 10))}",
        "${esc((f.trade_id || '').slice(0, 12))}",
        "${esc(lock.holder)}",
    ):
        assert interpolation in PAGE_HTML, interpolation
    # A status from the database must not become a CSS class unchecked.
    assert "KNOWN_STATUSES.includes(o.status)" in PAGE_HTML


def test_dashboard_reads_exactly_where_the_registry_writes():
    """One live registry, one path. A dashboard aimed elsewhere reports a calm lie.

    The repo root still carries a run/live.db from before the live path was
    extracted. Preferring it pointed this page at a dead file that never
    receives a fill, which reads identically to a healthy idle cycle.
    """
    from engine.order_registry import DEFAULT_DB_PATH

    assert resolve_db_path() == DEFAULT_DB_PATH
    assert resolve_db_path().parent.parent.name == "live"


def test_milestone8_html_contains_required_sections():
    """Milestone 8 requirement: UI components for 3 levels, exposure chart, and run selector exist in HTML."""
    assert "sec-run-kpis" in PAGE_HTML
    assert "sec-exposure" in PAGE_HTML
    assert "sec-funnel" in PAGE_HTML
    assert "sec-mechanics" in PAGE_HTML
    assert "modal-drilldown" in PAGE_HTML
    assert "modal-dist" in PAGE_HTML
    assert "run-selector" in PAGE_HTML
    assert "tip-wrap" in PAGE_HTML
    assert "bellCurveSvg" in PAGE_HTML


def test_level1_trade_analytics_tiles_are_present():
    """The new win-rate/expectancy, distribution, Sharpe, and drawdown tiles exist."""
    for marker in (
        "Win Rate & Expectancy",
        "Trade PnL Distribution",
        "Sharpe & Risk/Reward",
        "Drawdown & Inventory",
        "histogramSvg",
        "trade_analytics",
        "openDistModal('pnl')",
    ):
        assert marker in PAGE_HTML, marker


def test_api_kpi_endpoint_returns_3_levels_and_run_isolation(client, temp_db):
    """Test /api/kpi provides Level 1 (Strategy), Level 2 (Market), Level 3 (Mechanics), and run isolation."""
    from engine.order_registry import (
        OrderRegistry, OrderRecord, FillRecord, QuoteRecord,
        MarketEventRecord, MarkoutRecord, CloseRecord, FloatMarkRecord,
        VenueErrorRecord, DivergenceEventRecord
    )
    reg = OrderRegistry(temp_db)
    t_now = time.time()

    # Run 1: run-first-cycle
    r1 = "run-first-cycle"
    reg.create_order(OrderRecord(
        id="ord-up", order_id="clob-up", condition_id="0xmarket1", token_id="tok-up",
        side="BUY", price=0.62, original_size=5.0, status="filled",
        posted_ts=int((t_now - 120) * 1000), last_polled_ts=int(t_now * 1000),
        pair_id="pair-1", max_pair_cost_at_post=0.95, run_id=r1
    ))
    reg.create_order(OrderRecord(
        id="ord-dn", order_id="clob-dn", condition_id="0xmarket1", token_id="tok-dn",
        side="BUY", price=0.32, original_size=5.0, status="filled",
        posted_ts=int((t_now - 120) * 1000), last_polled_ts=int(t_now * 1000),
        pair_id="pair-1", max_pair_cost_at_post=0.95, run_id=r1
    ))
    reg.log_quote(QuoteRecord(
        ts=t_now - 120, condition_id="0xmarket1", token_id="tok-up", side="UP",
        price=0.62, size=5.0, queue_ahead=8.0, mid=0.63, edge_vs_mid=0.01,
        filled=5.0, latency_ms=25.0, local_id="ord-up", run_id=r1
    ))
    reg.log_quote(QuoteRecord(
        ts=t_now - 120, condition_id="0xmarket1", token_id="tok-dn", side="DOWN",
        price=0.32, size=5.0, queue_ahead=5.0, mid=0.33, edge_vs_mid=0.01,
        filled=5.0, latency_ms=25.0, local_id="ord-dn", run_id=r1
    ))
    reg.record_fill(FillRecord(
        trade_id="tr-up", order_uuid="ord-up", size=5.0, price=0.62,
        venue_ts=int((t_now - 100) * 1000), recorded_ts=int((t_now - 99) * 1000), run_id=r1
    ))
    reg.record_fill(FillRecord(
        trade_id="tr-dn", order_uuid="ord-dn", size=5.0, price=0.32,
        venue_ts=int((t_now - 90) * 1000), recorded_ts=int((t_now - 89) * 1000), run_id=r1
    ))
    reg.log_markout(MarkoutRecord(
        ts=t_now - 100, condition_id="0xmarket1", side="UP", fill_price=0.62, size=5.0,
        ref_mid=0.63, mid_h0=0.635, mid_h1=0.64, mid_h2=0.645, mid_h3=0.638, done=1, run_id=r1
    ))
    reg.log_close(CloseRecord(
        ts=t_now - 30, condition_id="0xmarket1", method="merge", shares=5.0,
        cost_basis=4.70, proceeds=5.00, realized_pnl=0.30, tx_hash="0xhash123", run_id=r1
    ))
    reg.log_market_event(MarketEventRecord(
        ts=t_now - 130, condition_id="0xmarket1", kind="QUOTING", reason="both sides placed",
        reason_code="INTENT_GENERATED", run_id=r1
    ))
    reg.log_market_event(MarketEventRecord(
        ts=t_now - 140, condition_id="0xmarket_blocked", kind="BLOCKED", reason="outside price band",
        reason_code="PRICE_BAND", run_id=r1
    ))
    reg.log_float_mark(unrealized_usd=0.0, committed_open_usd=4.70, naked_usd=0.0, ts=t_now - 90, run_id=r1)
    reg.log_venue_error(VenueErrorRecord(
        ts=t_now - 150, condition_id="0xmarket1", side="BUY", price=0.62, size=5.0,
        error_code="INVALID_POST", raw_error_msg="post-only cross rejected", run_id=r1
    ))
    reg.log_divergence_event(DivergenceEventRecord(
        ts=t_now - 20, condition_id="0xmarket1", pair_id="pair-1",
        registry_diff=0.0, venue_diff=0.0, chain_diff=0.0, divergence_msg="all matched", run_id=r1
    ))

    # Test /api/kpi
    res = client.get("/api/kpi", params={"run_id": r1})
    assert res.status_code == 200
    data = res.json()

    # Level 1: Run level
    assert data["fills"] == 2
    assert data["filled_shares"] == 10.0
    assert data["realized_pnl"] == 0.30
    assert data["fill_rate"] == 1.0
    assert data["spread_capture"] > 0
    assert data["runs"][0]["run_id"] == r1

    # Level 2: Market level & Drilldown
    assert "0xmarket1" in data["by_market"]
    mkt = data["by_market"]["0xmarket1"]
    assert mkt["up_sh"] == 5.0
    assert mkt["dn_sh"] == 5.0
    assert mkt["pair_cost"] == 0.94
    assert mkt["balance"] == 1.0
    assert len(mkt["markouts"]) == 1
    assert mkt["markouts"][0]["mid_h0"] == 0.635
    assert mkt["markouts"][0]["mid_h1"] == 0.64
    assert mkt["markouts"][0]["mid_h2"] == 0.645
    assert mkt["markouts"][0]["mid_h3"] == 0.638

    # Funnel
    assert "funnel" in data
    assert any(f["cause"] == "PRICE_BAND" for f in data["funnel"]["filters"])

    # Level 3: Mechanics
    assert data["order_latency_ms"]["median"] == 25.0
    assert data["reconcile_lag_ms"]["median"] == 1000.0
    assert data["venue_rejects"]["total"] == 1
    assert data["venue_rejects"]["by_code"]["INVALID_POST"] == 1
    assert data["three_way_divergences"]["total"] == 1

    # Req 4: Float marks series
    assert len(data["float_marks"]) >= 1



def test_kpi_endpoint_survives_launch_by_file_path(tmp_path):
    """`python live/dash/live_dash.py` must still serve /api/kpi.

    Launching the file by path puts live/dash/ on sys.path instead of live/, so the
    lazy `from engine.kpi import report` inside the endpoint raised
    ModuleNotFoundError and every poll came back 500 -- invisible to this suite,
    which runs with live/ as the working directory.
    """
    repo_root = Path(__file__).resolve().parents[2]
    dash_file = repo_root / "live" / "dash" / "live_dash.py"
    db_file = tmp_path / "live.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    snippet = (
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(dash_file.parent)!r})\n"
        f"mod = runpy.run_path({str(dash_file)!r})\n"
        "from fastapi.testclient import TestClient\n"
        f"mod['set_db_override']({str(db_file)!r})\n"
        "r = TestClient(mod['app']).get('/api/kpi')\n"
        "print(r.status_code)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("200"), proc.stdout + proc.stderr


def test_page_html_contains_status_bar_and_bot_buttons():
    """Verify HTML contains Supervisor, 4 sub-service pills, and Start/Stop/Reset buttons."""
    from dash.live_dash import PAGE_HTML

    assert "id=\"supervisor-status\"" in PAGE_HTML or "id=\"sup-status\"" in PAGE_HTML
    assert "SUPERVISOR" in PAGE_HTML
    assert "id=\"sub-services\"" in PAGE_HTML or "class=\"sub-services\"" in PAGE_HTML
    assert "id=\"pill-fleet\"" in PAGE_HTML
    assert "btn-start-bot" in PAGE_HTML
    assert "btn-stop-bot" in PAGE_HTML
    assert "btn-reset-db" in PAGE_HTML
    assert "dot-online" in PAGE_HTML or "status-dot" in PAGE_HTML


def test_system_status_endpoint(client):
    """GET /api/system/status returns supervisor, 4 sub-services (screener, engine, fleet, dash), and bot state."""
    res = client.get("/api/system/status")
    assert res.status_code == 200
    data = res.json()
    assert "supervisor" in data
    assert "running" in data["supervisor"]
    assert "services" in data
    assert "screener" in data["services"]
    assert "engine" in data["services"]
    assert "fleet" in data["services"]
    assert "dash" in data["services"]
    assert data["services"]["dash"]["running"] is True
    assert "bot_state" in data


def test_system_start_and_stop_endpoints(client, monkeypatch, tmp_path):
    """POST /api/system/start and POST /api/system/stop control bot state safely.

    start_bot() Popens the real screener and the real `live_exec poll` loop. A
    child process does not inherit conftest's socket guard, and live_exec loads
    dotenv at import, so an unmocked call here signs real requests to the venue
    from a test run -- and nothing in the test would ever stop the loops. The
    spawn is stubbed; what is under test is the endpoint contract.
    """
    import dash.live_dash as dash_mod

    spawned = []
    monkeypatch.setattr(
        dash_mod, "start_bot",
        lambda: (spawned.append("start"), {"ok": True, "message": "stubbed"})[1],
    )
    monkeypatch.setattr(
        dash_mod, "stop_bot",
        lambda: (spawned.append("stop"), {"ok": True, "message": "stubbed"})[1],
    )

    res_stop = client.post("/api/system/stop", headers=_control(client))
    assert res_stop.status_code == 200
    assert res_stop.json().get("ok") is True

    res_start = client.post("/api/system/start", headers=_control(client))
    assert res_start.status_code == 200
    assert res_start.json().get("ok") is True

    assert spawned == ["stop", "start"]


def test_start_endpoint_never_spawns_a_process_under_test(client, monkeypatch):
    """A regression guard: no test may Popen the live bot stack.

    Fails against the unpatched suite, where /api/system/start reached
    subprocess.Popen and left `scripts.rerank_loop` and `engine.live_exec poll`
    running against the venue after pytest exited.
    """
    import subprocess

    import dash.live_dash as dash_mod

    def _forbidden(*args, **kwargs):
        raise AssertionError(f"test spawned a live process: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(
        dash_mod, "start_bot", lambda: {"ok": True, "message": "stubbed"}
    )

    res = client.post("/api/system/start", headers=_control(client))
    assert res.status_code == 200


def test_reset_db_refuses_to_destroy_an_archived_run(tmp_path, monkeypatch):
    """Reset must not delete an archive opened for reading.

    reset_database archives-then-unlinks whatever --db points at. Launched
    against a past cycle for a post-mortem, the unguarded version destroyed the
    record the operator opened the page to read, and nested a fresh archive/
    inside the archive directory on the way out.
    """
    import dash.live_dash as dash_mod
    # Isolate the bot-running gate (see test_system_reset_db_endpoint): point
    # LIVE_ROOT at tmp_path so the global live_procs.json is not consulted.
    monkeypatch.setattr(dash_mod, "LIVE_ROOT", tmp_path)
    from dash.live_dash import reset_database

    archive_dir = tmp_path / "run" / "archive"
    archive_dir.mkdir(parents=True)
    archived = archive_dir / "live_20260819_090708.db"
    con = sqlite3.connect(str(archived))
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    size_before = archived.stat().st_size

    result = reset_database(archived)

    assert result["ok"] is False
    assert "archived run" in result["message"]
    assert archived.exists(), "the archived cycle was deleted"
    assert archived.stat().st_size == size_before
    assert not (archive_dir / "archive").exists(), "nested archive/ was created"


def test_system_restart_dash_endpoint(client, monkeypatch):
    """POST /api/system/restart-dash launches a replacement and exits this one.

    Two things must be neutralised or the suite dies: the handler's background
    thread really calls os._exit(0) after 0.8s, which would kill pytest itself,
    and it really Popens a second dashboard. Both are patched at the module the
    handler resolves them through. `threading.Thread.start` is deliberately NOT
    patched -- TestClient runs each request on its own portal thread, so a no-op
    start deadlocks the client instead of testing anything.
    """
    import subprocess

    import dash.live_dash as dash_mod

    launched = []
    exited = []

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: launched.append(a))
    monkeypatch.setattr(dash_mod.os, "_exit", lambda code: exited.append(code))

    res = client.post("/api/system/restart-dash", headers=_control(client))
    assert res.status_code == 200
    assert res.json().get("ok") is True
    assert "restart-dash" in PAGE_HTML

    # The handler's daemon thread sleeps 0.8s before acting. Wait for it here:
    # if monkeypatch tore down first, the real os._exit(0) would kill pytest.
    deadline = time.time() + 5.0
    while not exited and time.time() < deadline:
        time.sleep(0.05)

    assert launched, "no replacement dashboard was launched"
    assert exited == [0], "the old instance did not exit to release the port"


def test_restart_relaunches_by_absolute_path(monkeypatch):
    """The replacement dashboard must be launched by absolute path.

    sys.argv[0] is whatever the operator typed, and .claude/launch.json types it
    relative ("live/dash/live_dash.py"). Replaying that under cwd=live/ looks for
    live/live/dash/live_dash.py, so the replacement dies on startup and the page
    never comes back -- while the current instance has already os._exit(0)'d.
    """
    from dash.live_dash import LIVE_ROOT, relaunch_argv

    # Exactly how launch.json invokes it, from the repo root.
    monkeypatch.setattr(sys, "argv", ["live/dash/live_dash.py", "--port", "8799"])

    argv = relaunch_argv()
    script = Path(argv[1])

    assert script.is_absolute(), f"relaunched by relative path: {script}"
    assert script.exists(), f"relaunch target does not exist: {script}"
    assert script.name == "live_dash.py"
    # The restart runs with cwd=LIVE_ROOT; the command must still resolve there.
    assert (LIVE_ROOT / script).exists()
    # Port and database flags survive the restart.
    assert argv[2:] == ["--port", "8799"]


def test_restart_preserves_the_database_flag(monkeypatch):
    """A restart must come back on the same database it was reading."""
    from dash.live_dash import relaunch_argv

    monkeypatch.setattr(sys, "argv", ["live_dash.py", "--db", "run/archive/live_x.db"])
    assert relaunch_argv()[2:] == ["--db", "run/archive/live_x.db"]


def test_system_reset_db_endpoint(client, temp_db, tmp_path, monkeypatch):
    """POST /api/system/reset-db archives the existing DB and initializes a clean fresh DB."""
    import sqlite3
    import dash.live_dash as dash_mod
    # Isolate the bot-running gate: reset_database() consults the global
    # LIVE_ROOT/run/live_procs.json to decide if the bot is RUNNING. Point
    # LIVE_ROOT at the test tmp so the gate sees no procs file and treats the
    # bot as STOPPED, otherwise a live bot on the operator's machine short-
    # circuits the reset before it ever touches temp_db.
    monkeypatch.setattr(dash_mod, "LIVE_ROOT", tmp_path)
    # Put a dummy row in temp_db first
    conn = sqlite3.connect(temp_db)
    conn.execute("INSERT INTO orders (id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts) VALUES ('dummy-1', '0x1', '0x2', 'BUY', 0.5, 10, 'open', 1000, 1000)")
    conn.commit()
    conn.close()

    res = client.post("/api/system/reset-db", headers=_control(client))
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True
    assert "archived_to" in data
    archived_path = data["archived_to"]

    # Verify orders table in freshly created DB is empty
    conn2 = sqlite3.connect(temp_db)
    cursor = conn2.cursor()
    cursor.execute("SELECT COUNT(*) FROM orders")
    count = cursor.fetchone()[0]
    conn2.close()
    assert count == 0

    # Verify the archive file is a valid sqlite3 db containing the original pre-reset data.
    # reset_database() returns only the archive *filename*; the file lives under
    # <target_db.parent>/archive/, and target_db resolves to temp_db (tmp_path/live.db).
    archive_dir = tmp_path / "archive"
    arch_conn = sqlite3.connect(archive_dir / archived_path)
    arch_cursor = arch_conn.cursor()
    arch_cursor.execute("SELECT id FROM orders WHERE id = 'dummy-1'")
    archived_row = arch_cursor.fetchone()
    arch_conn.close()
    assert archived_row is not None, "Original dummy-1 row must be preserved in archive"
    assert archived_row[0] == "dummy-1"




# --------------------------------------------------------------------------
# CodeRabbit review round on PR #44
# --------------------------------------------------------------------------

def test_active_orders_panel_js_filter_present():
    """The 'Active Pair Orders' panel must hide filled/cancelled rows client-side.

    The state half of this regression moved to test_registry_state.py
    (test_state_returns_all_orders_including_terminal); here only the
    template contract is pinned: the JS filter exists, the empty-state copy
    points at the Fills Timeline, and the server does NOT pre-filter.
    """
    assert "Active Pair Orders" in PAGE_HTML
    assert "ACTIVE_STATUSES" in PAGE_HTML
    assert "open" in PAGE_HTML and "pending" in PAGE_HTML and "partial" in PAGE_HTML
    assert "Fills Timeline" in PAGE_HTML or "Fills timeline" in PAGE_HTML


def test_api_state_does_not_pre_filter_orders(client, temp_db):
    """The server emits ALL orders; the JS filter is what renders only
    active ones. A server-side filter would be brittle (anyone calling
    /api/state from a tool would see a partial picture).
    """
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(temp_db))
    con.executemany(
        """INSERT INTO orders (id, condition_id, token_id, side, price, original_size, status, posted_ts, last_polled_ts)
           VALUES (?, 'cond-1', 'tok-1', 'BUY', 0.5, 10.0, ?, ?, ?)""",
        [
            ("ord-active-1", "open", now_ms, now_ms),
            ("ord-active-2", "pending", now_ms, now_ms),
            ("ord-active-3", "partial", now_ms, now_ms),
            ("ord-cancelled", "cancelled", now_ms, now_ms),
            ("ord-filled", "filled", now_ms, now_ms),
        ],
    )
    con.commit()
    con.close()

    res = client.get("/api/state")
    assert res.status_code == 200
    data = res.json()
    ids = {o["id"] for o in data["orders"]}
    # All five rows on the wire; the JS hides three of them in the table.
    assert ids == {"ord-active-1", "ord-active-2", "ord-active-3",
                   "ord-cancelled", "ord-filled"}


def _control(client):
    """Headers that authorize a machine-state change from this process's page."""
    from dash.live_dash import CONTROL_TOKEN
    return {"X-Control-Token": CONTROL_TOKEN}


def test_control_endpoints_reject_untokened_posts(client):
    """A POST without the page's token must not change machine state.

    Loopback binding is not a defence: a page in the operator's browser can
    submit a cross-origin form POST to 127.0.0.1:8799 with no CORS preflight,
    and /api/system/start spawns the loop that signs real venue requests.
    """
    for path in (
        "/api/system/start",
        "/api/system/stop",
        "/api/system/reset-db",
        "/api/system/restart-dash",
        "/api/system/sweep-interval",
    ):
        assert client.post(path).status_code == 403, f"{path} accepted an untokened POST"


def test_control_endpoints_reject_foreign_origin(client, monkeypatch):
    """Even with a token, a request claiming a foreign Origin is refused."""
    import dash.live_dash as dash_mod

    monkeypatch.setattr(dash_mod, "start_bot", lambda: {"ok": True})
    res = client.post(
        "/api/system/start",
        headers={**_control(client), "Origin": "https://evil.example"},
    )
    assert res.status_code == 403


def test_page_carries_a_live_token_but_the_constant_does_not(client):
    """The served page gets a real token; PAGE_HTML keeps only the placeholder."""
    from dash.live_dash import CONTROL_TOKEN, CONTROL_TOKEN_PLACEHOLDER

    assert CONTROL_TOKEN_PLACEHOLDER in PAGE_HTML
    assert CONTROL_TOKEN not in PAGE_HTML

    body = client.get("/").text
    assert CONTROL_TOKEN in body
    assert CONTROL_TOKEN_PLACEHOLDER not in body


def test_start_refuses_a_second_bot_stack(client, monkeypatch):
    """Two stacks on one database sum independent inventories into invalid data.

    live_procs.json only remembers the newest PIDs, so a second start would also
    strand the first pair beyond the reach of stop_bot.
    """
    import subprocess

    import dash.live_dash as dash_mod

    monkeypatch.setattr(
        dash_mod, "get_system_status", lambda: {"bot_state": "RUNNING"}
    )
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **kw: pytest.fail("a second bot stack was spawned"),
    )

    res = client.post("/api/system/start", headers=_control(client))
    assert res.status_code == 200
    assert res.json()["ok"] is False
    assert "already running" in res.json()["message"]


def test_reset_db_refuses_while_the_bot_is_running(monkeypatch, temp_db):
    """Unlinking the registry under a live writer loses every later write."""
    import dash.live_dash as dash_mod

    monkeypatch.setattr(dash_mod, "get_system_status", lambda: {"bot_state": "RUNNING"})
    result = dash_mod.reset_database(temp_db)

    assert result["ok"] is False
    assert "running" in result["message"].lower()
    assert temp_db.exists(), "the live registry was deleted under the bot"


def test_status_reports_the_port_actually_bound(monkeypatch):
    """--port moves the dashboard, and the status payload must follow it."""
    import dash.live_dash as dash_mod

    monkeypatch.setattr(dash_mod, "_ACTIVE_PORT", 9123)
    assert dash_mod.get_system_status()["services"]["dash"]["port"] == 9123
    # The page reads the reported port instead of a literal.
    assert "':8799'" not in PAGE_HTML


def test_sweep_interval_is_configurable_from_env(monkeypatch):
    """LIVE_SWEEP_INTERVAL sets the card's cadence; absent/bad values don't."""
    from dash.live_dash import resolve_sweep_interval

    monkeypatch.setenv("LIVE_SWEEP_INTERVAL", "30")
    assert resolve_sweep_interval() == 30.0

    monkeypatch.delenv("LIVE_SWEEP_INTERVAL")
    assert resolve_sweep_interval() is None

    monkeypatch.setenv("LIVE_SWEEP_INTERVAL", "garbage")
    assert resolve_sweep_interval() is None

    monkeypatch.setenv("LIVE_SWEEP_INTERVAL", "-5")
    assert resolve_sweep_interval() is None


def test_status_surfaces_engine_sweep_cadence(monkeypatch, tmp_path):
    """The system-status payload reports how often the engine sweeps."""
    import dash.live_dash as dash_mod

    monkeypatch.setattr(dash_mod, "LIVE_ROOT", tmp_path)
    monkeypatch.setenv("LIVE_SWEEP_INTERVAL", "30")
    assert dash_mod.get_system_status()["services"]["engine"]["sweep_interval_sec"] == 30.0

    monkeypatch.delenv("LIVE_SWEEP_INTERVAL")
    assert dash_mod.get_system_status()["services"]["engine"]["sweep_interval_sec"] is None


def test_start_bot_passes_sweep_interval_to_poll(monkeypatch, tmp_path):
    """start_bot launches poll with --sweep-interval when one is configured."""
    import subprocess

    import dash.live_dash as dash_mod

    spawned = []

    class _FakePopen:
        def __init__(self, args, **kwargs):
            spawned.append(args)
            self.pid = 12345

    monkeypatch.setattr(dash_mod, "LIVE_ROOT", tmp_path)
    monkeypatch.setattr(dash_mod, "get_system_status", lambda: {"bot_state": "STOPPED"})
    monkeypatch.setattr(dash_mod, "resolve_sweep_interval", lambda: 30.0)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    result = dash_mod.start_bot()
    assert result["ok"] is True

    engine_cmd = next(a for a in spawned if "poll" in a)
    assert "--sweep-interval" in engine_cmd
    assert engine_cmd[engine_cmd.index("--sweep-interval") + 1] == "30.0"


def test_start_bot_spawns_screener_engine_and_fleet(monkeypatch, tmp_path):
    """Start Bot launches the full hands-off stack: rerank, poll, and fleet.

    The fleet loop must not reconcile or sweep -- poll owns both -- so its
    command line carries --no-reconcile and --no-sweep alongside --live.
    """
    import subprocess

    import dash.live_dash as dash_mod

    spawned = []

    class _FakePopen:
        def __init__(self, args, **kwargs):
            spawned.append(args)
            self.pid = 12345

    monkeypatch.setattr(dash_mod, "LIVE_ROOT", tmp_path)
    monkeypatch.setattr(dash_mod, "get_system_status", lambda: {"bot_state": "STOPPED"})
    monkeypatch.setattr(dash_mod, "resolve_sweep_interval", lambda: None)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    result = dash_mod.start_bot()
    assert result["ok"] is True

    cmds = [" ".join(a) for a in spawned]
    assert any("scripts.rerank_loop" in c for c in cmds), cmds
    assert any("engine.live_exec" in c and "poll" in c for c in cmds), cmds

    fleet_cmd = next(c for c in cmds if "engine.live_fleet" in c)
    assert "--live" in fleet_cmd
    assert "--no-reconcile" in fleet_cmd
    assert "--no-sweep" in fleet_cmd


def test_set_sweep_interval_persists_and_applies(monkeypatch, tmp_path):
    """The control writes LIVE_SWEEP_INTERVAL into .env and the status payload."""
    import os

    import dash.live_dash as dash_mod

    env_file = tmp_path / ".env"
    env_file.write_text("POLY_PRIVATE_KEY=do-not-load\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(dash_mod, "LIVE_ROOT", tmp_path)
    monkeypatch.delenv("LIVE_SWEEP_INTERVAL", raising=False)

    result = dash_mod.set_sweep_interval("45")
    assert result["ok"] is True
    assert result["sweep_interval_sec"] == 45.0
    assert os.environ["LIVE_SWEEP_INTERVAL"] == "45.0"

    saved = env_file.read_text(encoding="utf-8")
    assert "LIVE_SWEEP_INTERVAL=45" in saved
    # Everything else in the file survives, credentials included.
    assert "POLY_PRIVATE_KEY=do-not-load" in saved
    assert "OTHER=1" in saved
    assert result["status"]["services"]["engine"]["sweep_interval_sec"] == 45.0


def test_set_sweep_interval_rejects_bad_values(monkeypatch, tmp_path):
    """A non-numeric or non-positive cadence is refused before touching .env."""
    import dash.live_dash as dash_mod

    monkeypatch.setattr(dash_mod, "LIVE_ROOT", tmp_path)

    assert dash_mod.set_sweep_interval("abc")["ok"] is False
    assert dash_mod.set_sweep_interval("0")["ok"] is False
    assert dash_mod.set_sweep_interval("-3")["ok"] is False


def test_sweep_interval_endpoint_sets_and_clears(client, monkeypatch, tmp_path):
    """POST /api/system/sweep-interval persists and clears the cadence."""
    import dash.live_dash as dash_mod

    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(dash_mod, "LIVE_ROOT", tmp_path)
    monkeypatch.delenv("LIVE_SWEEP_INTERVAL", raising=False)

    res = client.post("/api/system/sweep-interval?seconds=60", headers=_control(client))
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["sweep_interval_sec"] == 60.0
    assert "LIVE_SWEEP_INTERVAL=60" in env_file.read_text(encoding="utf-8")

    res = client.post("/api/system/sweep-interval", headers=_control(client))
    data = res.json()
    assert data["ok"] is True
    assert data["sweep_interval_sec"] is None
    assert "LIVE_SWEEP_INTERVAL" not in env_file.read_text(encoding="utf-8")


def test_status_distinguishes_configured_from_running_sweep_cadence(monkeypatch, tmp_path):
    """Configured cadence is what the control set; running is what poll launched with."""
    import json
    import os
    import time

    import dash.live_dash as dash_mod

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "live_procs.json").write_text(json.dumps({
        "engine": {"pid": os.getpid(), "started_at": time.time(), "sweep_interval_sec": 30.0},
    }), encoding="utf-8")

    monkeypatch.setattr(dash_mod, "LIVE_ROOT", tmp_path)
    monkeypatch.setattr(dash_mod, "_is_pid_alive", lambda pid, started_at=None: pid is not None)
    monkeypatch.setenv("LIVE_SWEEP_INTERVAL", "60")

    engine = dash_mod.get_system_status()["services"]["engine"]
    assert engine["sweep_interval_sec"] == 60.0           # configured
    assert engine["running_sweep_interval_sec"] == 30.0   # running process's launch value


def test_cycle_stream_route_registered():
    """GET /api/cycle-stream is served as an SSE endpoint."""
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/cycle-stream" in paths


def test_cycle_stream_sse_replays_tail_and_follows_appends(tmp_path):
    """The SSE generator replays the ring tail, then follows new appends."""
    ring = tmp_path / "cycle_events.jsonl"
    ring.write_text(
        json.dumps({"service": "engine", "phase": "scanning", "action": "tick"}) + "\n",
        encoding="utf-8",
    )
    gen = _cycle_stream_sse(ring, tail=50, poll_sec=0.01)
    first = next(gen)
    assert "data:" in first
    assert '"action": "tick"' in first

    with ring.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"service": "fleet", "phase": "quoting", "action": "decide"}) + "\n"
        )

    deadline = time.time() + 3.0
    saw_follow = False
    while time.time() < deadline:
        if '"action": "decide"' in next(gen):
            saw_follow = True
            break
    gen.close()
    assert saw_follow


def test_cycle_stream_sse_detects_rotation_by_file_identity(tmp_path):
    """A rotation that replaces the ring with a LARGER file is still detected."""
    ring = tmp_path / "cycle_events.jsonl"
    ring.write_text(
        json.dumps({"service": "engine", "phase": "scanning", "action": "a"}) + "\n",
        encoding="utf-8",
    )
    gen = _cycle_stream_sse(ring, tail=50, poll_sec=0.01)
    next(gen)  # consume the initial replay

    # Simulate the engine's os.replace rotation with a larger-byte file.
    tmp = tmp_path / "new.jsonl"
    tmp.write_text(
        json.dumps({"service": "fleet", "phase": "quoting", "action": "b",
                    "reason": "x" * 200}) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, ring)

    deadline = time.time() + 3.0
    saw_rotate = False
    saw_new = False
    while time.time() < deadline:
        frame = next(gen)
        if frame.startswith("event: rotate"):
            saw_rotate = True
        if '"action": "b"' in frame:
            saw_new = True
        if saw_rotate and saw_new:
            break
    gen.close()
    assert saw_rotate
    assert saw_new


def test_page_html_contains_bot_brains_panel():
    """The page ships the Bot Brains panel shell and its SSE hookup."""
    assert "Bot Brains" in PAGE_HTML
    assert 'id="bb-active-pills"' in PAGE_HTML
    assert 'id="bb-decision-log"' in PAGE_HTML
    assert 'id="bb-sparkline"' in PAGE_HTML
    assert "/api/cycle-stream" in PAGE_HTML


def test_compute_scan_state_stalled_when_heartbeat_missing():
    state, age = compute_scan_state(None, None, 1_000_000.0, {"scanning"})
    assert state == "STALLED"
    assert age is None


def test_compute_scan_state_stalled_when_heartbeat_stale():
    now = 1_000_000.0
    state, age = compute_scan_state(now - 5, now - 120, now, {"quoting"})
    assert state == "STALLED"
    assert age == 120.0


def test_compute_scan_state_scanning_vs_idle():
    now = 1_000_000.0
    state, _ = compute_scan_state(now - 5, now - 5, now, {"quoting"})
    assert state == "SCANNING"
    state, _ = compute_scan_state(now - 5, now - 5, now, {"idle", "waiting"})
    assert state == "IDLE"


def test_scan_state_endpoint_reports_rationale_and_stall(client, temp_db, tmp_path):
    """/api/scan-state derives state from ring+heartbeat and skip/pass from cycle_intent."""
    con = sqlite3.connect(str(temp_db))
    con.execute(
        "INSERT INTO cycle_intent (ts, cycle, market_slug, intent_count, "
        "top_skip_reason, top_pass_reason, run_id) VALUES (?,?,?,?,?,?,?)",
        (time.time(), 1, "mkt-a", 0, "price_band", None, "live"),
    )
    con.execute(
        "INSERT INTO cycle_intent (ts, cycle, market_slug, intent_count, "
        "top_skip_reason, top_pass_reason, run_id) VALUES (?,?,?,?,?,?,?)",
        (time.time(), 2, "mkt-b", 2, None, "edge_ok", "live"),
    )
    con.commit()
    con.close()

    ring = tmp_path / "cycle_events.jsonl"
    ring.write_text(
        json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "service": "screener", "cycle": 1, "phase": "scanning",
            "action": "rerank_done", "market_slug": "", "reason": "",
            "latency_ms": 1.0,
        }) + "\n",
        encoding="utf-8",
    )
    hb = tmp_path / "heartbeat.json"
    hb.write_text(
        json.dumps([{"ts": int(time.time() * 1000), "cycle": 1, "errors": 0}]),
        encoding="utf-8",
    )

    set_ring_override(ring)
    set_heartbeat_override(hb)
    try:
        res = client.get("/api/scan-state")
    finally:
        set_ring_override(None)
        set_heartbeat_override(None)

    assert res.status_code == 200
    data = res.json()
    assert data["scan_state"] in {"SCANNING", "IDLE", "STALLED"}
    assert data["seconds_since_heartbeat"] is not None
    assert {"reason": "price_band", "count": 1} in data["skip_reasons"]
    assert {"reason": "edge_ok", "count": 1} in data["pass_reasons"]
