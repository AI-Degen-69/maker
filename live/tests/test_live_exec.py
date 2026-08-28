import argparse
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from eth_account import Account

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine.live_exec as le


# Response shape MEASURED against live relayer 2026-08-16:
#   {"address":"0x6987f531981c95fc998ab20c0935154e9f509a87","nonce":"122"}
# `address` is a ROTATING RELAYER POOL WORKER, never our account. Deliberately
# set to an unrelated address so any code that trusts this field fails the test.
POOL_WORKER = "0x6987f531981c95fc998ab20c0935154e9f509a87"


class MockResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode("utf-8")

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def make_mock_urlopen(recorded_requests, pool_worker: str = POOL_WORKER, nonce: str = "121", submit_hash: str = "0xabcdef1234567890"):
    def mock_urlopen(req, timeout=30):
        recorded_requests.append(req)
        if "params" in req.full_url:
            return MockResponse({"address": pool_worker, "nonce": nonce})
        elif "submit" in req.full_url:
            return MockResponse({"transactionHash": submit_hash, "status": "PENDING"})
        return MockResponse({})
    return mock_urlopen


def make_live_env(acc: Account, funder: str) -> dict:
    return {
        "POLY_PRIVATE_KEY": acc.key.hex(),
        "POLY_FUNDER": funder,
        "RELAYER_API_KEY": "test_key",
        "RELAYER_API_KEY_ADDRESS": "0x1234567890123456789012345678901234567890",
        "RELAYER_URL": "https://relayer-v2.polymarket.com",
    }


def test_live_exec_arg_parsing():
    ap = argparse.ArgumentParser(description="LIVE Polymarket execution.")
    ap.add_argument("--live", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--series", default=None)
    p.add_argument("--token-id", default=None)
    p.add_argument("--cycles", type=int, default=30)
    p.add_argument("--min-time-remaining", type=float, default=90.0)
    p.add_argument("--max-complement-bid", type=float, default=0.85)
    p.add_argument("--max-loss", type=float, default=1.00)
    p.add_argument("--max-fills", type=int, default=1)
    p.add_argument("--live", action="store_true", default=argparse.SUPPRESS)

    r = sub.add_parser("redeem")
    r.add_argument("condition_id")
    r.add_argument("--skip-resolution-check", action="store_true")

    args = ap.parse_args(["probe", "--series", "btc-up-or-down-5m", "--cycles", "10", "--min-time-remaining", "120", "--max-complement-bid", "0.80"])
    assert args.cmd == "probe"
    assert args.series == "btc-up-or-down-5m"
    assert args.cycles == 10
    assert args.min_time_remaining == 120.0
    assert args.max_complement_bid == 0.80
    assert args.max_loss == 1.00
    assert args.max_fills == 1

    args_redeem = ap.parse_args(["redeem", "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f", "--skip-resolution-check"])
    assert args_redeem.cmd == "redeem"
    assert args_redeem.skip_resolution_check is True


def test_build_redeem_submit_payload():
    from_addr = "0xD2C7F5514580184d32C70F6FEA95B69C5Cd72fa0"
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    nonce = 121
    deadline = 1786855000
    signature = "0x" + "aa" * 65
    call_data = "0x01b7037c" + "00" * 224

    payload = le.build_redeem_submit_payload(from_addr, funder, nonce, deadline, signature, call_data)
    assert payload["type"] == "WALLET"
    assert payload["from"] == from_addr
    assert payload["to"] == le.DEPOSIT_WALLET_FACTORY
    assert payload["nonce"] == "121"
    assert isinstance(payload["nonce"], str)
    assert payload["signature"] == signature
    assert "metadata" not in payload

    params = payload["depositWalletParams"]
    assert params["depositWallet"] == funder
    assert params["deadline"] == str(deadline)
    assert isinstance(params["deadline"], str)
    assert len(params["calls"]) == 1
    assert params["calls"][0]["target"] == le.CTF_CONTRACT
    assert params["calls"][0]["value"] == "0"
    assert isinstance(params["calls"][0]["value"], str)
    assert params["calls"][0]["data"] == call_data


def test_redeem_dry_run(tmp_path):
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    with patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=1) as mock_denom, \
         patch("urllib.request.urlopen") as mock_url, \
         patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        le.redeem(cond_id, live=False)

    out = mock_stdout.getvalue()
    assert "resolved        yes" in out
    assert "submit_payload_preview" in out
    assert '"nonce": "0"' in out
    assert '"depositWalletParams"' in out
    assert '"signature"' in out
    assert "DRY RUN -- nothing sent" in out

    mock_url.assert_not_called()
    mock_denom.assert_called_once_with(cond_id)


def test_redeem_dry_run_rpc_unreachable(tmp_path):
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    # Guards run in dry-run too, so an unreachable RPC is reported as a pre-flight
    # failure rather than a clean preview: the dry run must match what --live does.
    with patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=None), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        with pytest.raises(SystemExit):
            le.redeem(cond_id, live=False)

    out = mock_stdout.getvalue()
    assert "resolved        unknown (RPC unreachable)" in out
    assert "PRE-FLIGHT FAILED -- --live would refuse:" in out
    assert "Cannot determine resolution status" in out

    # With the bypass flag the same dry run previews cleanly.
    with patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=None), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        le.redeem(cond_id, live=False, skip_resolution_check=True)

    out = mock_stdout.getvalue()
    assert "DRY RUN -- nothing sent" in out


def test_redeem_live_unresolved_raises(tmp_path):
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    with patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=0), \
         patch("urllib.request.urlopen") as mock_url:
        with pytest.raises(SystemExit) as exc_info:
            le.redeem(cond_id, live=True)
        assert f"Condition {cond_id} is not resolved yet (payoutDenominator == 0)" in str(exc_info.value)
    mock_url.assert_not_called()


def test_redeem_live_unresolved_skip_check_still_raises(tmp_path):
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    with patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=0), \
         patch("urllib.request.urlopen") as mock_url:
        with pytest.raises(SystemExit) as exc_info:
            le.redeem(cond_id, skip_resolution_check=True, live=True)
        assert f"Condition {cond_id} is not resolved yet (payoutDenominator == 0)" in str(exc_info.value)
    mock_url.assert_not_called()


def test_redeem_live_unknown_resolution_raises(tmp_path):
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    with patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=None), \
         patch("urllib.request.urlopen") as mock_url:
        with pytest.raises(SystemExit) as exc_info:
            le.redeem(cond_id, live=True)
        msg = str(exc_info.value)
        assert f"Cannot determine resolution status for {cond_id}" in msg
        assert "all RPC endpoints failed" in msg
        assert "pass --skip-resolution-check to bypass" in msg
    mock_url.assert_not_called()


def test_redeem_live_unknown_resolution_skip_check_proceeds(tmp_path):
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    env_vars = make_live_env(acc, funder)
    recorded_requests = []
    mock_urlopen = make_mock_urlopen(recorded_requests)

    with patch.object(le, "RUN", tmp_path), \
         patch.dict(os.environ, env_vars, clear=False), \
         patch.object(le, "get_payout_denominator", return_value=None), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen):
        le.redeem(cond_id, skip_resolution_check=True, live=True)

    assert len(recorded_requests) == 2
    assert "submit" in recorded_requests[1].full_url


def test_redeem_live_mock(tmp_path):
    """Verify gasless redemption request construction and wire types against
    official client schema @polymarket/builder-relayer-client@0.0.10 dist/types.d.ts:147-154.
    """
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    env_vars = make_live_env(acc, funder)
    recorded_requests = []
    mock_urlopen = make_mock_urlopen(recorded_requests)

    import time
    t_before = int(time.time())
    with patch.object(le, "RUN", tmp_path), \
         patch.dict(os.environ, env_vars, clear=False), \
         patch.object(le, "get_payout_denominator", return_value=1), \
         patch.object(le, "sign_redeem_transaction", wraps=le.sign_redeem_transaction) as mock_sign, \
         patch("urllib.request.urlopen", side_effect=mock_urlopen):
        le.redeem(cond_id, live=True)
    t_after = int(time.time())

    # Assert EIP-712 signer arguments remain integer types for typed data hashing
    mock_sign.assert_called_once()
    sign_args = mock_sign.call_args[0]
    assert isinstance(sign_args[2], int), "EIP-712 nonce passed to signer must be int"
    assert isinstance(sign_args[3], int), "EIP-712 deadline passed to signer must be int"

    assert len(recorded_requests) == 2
    req_nonce, req_submit = recorded_requests

    # Verify nonce request
    assert "params" in req_nonce.full_url
    assert req_nonce.headers["User-agent"] == "Mozilla/5.0"
    assert req_nonce.headers["Relayer_api_key"] == "test_key"
    assert req_nonce.headers["Relayer_api_key_address"] == "0x1234567890123456789012345678901234567890"

    # Verify submit request payload wire types against @polymarket/builder-relayer-client@0.0.10 dist/types.d.ts:147-154
    assert "submit" in req_submit.full_url
    body = json.loads(req_submit.data.decode("utf-8"))
    assert body["type"] == "WALLET"
    # Proves the submit body carries our EOA and not the worker address echoed by the params endpoint
    assert body["from"] == acc.address
    assert body["to"] == le.DEPOSIT_WALLET_FACTORY
    assert isinstance(body["nonce"], str)
    assert body["nonce"] == "121"
    assert body["signature"].startswith("0x")
    assert len(body["signature"]) == 132
    assert "metadata" not in body

    assert "depositWalletParams" in body
    params = body["depositWalletParams"]
    assert params["depositWallet"] == funder
    assert isinstance(params["deadline"], str)
    assert t_before + le.REDEEM_DEADLINE_SECONDS <= int(params["deadline"]) <= t_after + le.REDEEM_DEADLINE_SECONDS
    assert len(params["calls"]) == 1
    assert params["calls"][0]["target"] == le.CTF_CONTRACT
    assert isinstance(params["calls"][0]["value"], str)
    assert params["calls"][0]["value"] == "0"
    assert len(params["calls"][0]["data"]) == 458


def test_redeem_ignores_params_response_address(tmp_path):
    """Regression guard: verify that the pool-worker address in the params response
    appears nowhere in the submitted batch payload.
    """
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    env_vars = make_live_env(acc, funder)
    recorded_requests = []
    mock_urlopen = make_mock_urlopen(recorded_requests)

    with patch.object(le, "RUN", tmp_path), \
         patch.dict(os.environ, env_vars, clear=False), \
         patch.object(le, "get_payout_denominator", return_value=1), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen):
        le.redeem(cond_id, live=True)

    assert len(recorded_requests) == 2
    req_submit = recorded_requests[1]
    raw_json = req_submit.data.decode("utf-8")
    assert POOL_WORKER.lower() not in raw_json.lower(), "Pool worker address must never leak into submit payload"
    body = json.loads(raw_json)
    assert body["from"] != POOL_WORKER
    assert body["depositWalletParams"]["depositWallet"] != POOL_WORKER



def test_get_payout_denominator_failover():
    """First endpoint raises, second returns 0x01, third never consulted.
    POLYGON_RPC is cleared so the built-in list order is deterministic."""
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    calls = []

    def mock_failover_urlopen(req, timeout=5):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise OSError("RPC endpoint unreachable")
        return MockResponse({"jsonrpc": "2.0", "id": 1, "result": "0x0000000000000000000000000000000000000000000000000000000000000001"})

    with patch.dict(os.environ, {}, clear=False), \
         patch("urllib.request.urlopen", side_effect=mock_failover_urlopen):
        os.environ.pop("POLYGON_RPC", None)
        val = le.get_payout_denominator(cond_id)

    assert val == 1
    assert len(calls) == 2
    assert calls[0] == le.POLYGON_RPC_ENDPOINTS[0]
    assert calls[1] == le.POLYGON_RPC_ENDPOINTS[1]


def test_get_payout_denominator_env_override_tried_first():
    """POLYGON_RPC from env takes precedence over the built-in endpoint list."""
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    calls = []

    def mock_urlopen(req, timeout=5):
        calls.append(req.full_url)
        return MockResponse({"jsonrpc": "2.0", "id": 1, "result": "0x0000000000000000000000000000000000000000000000000000000000000001"})

    with patch.dict(os.environ, {"POLYGON_RPC": "https://private.example/rpc"}, clear=False), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen):
        val = le.get_payout_denominator(cond_id)

    assert val == 1
    assert len(calls) == 1
    assert calls[0] == "https://private.example/rpc"


def test_get_payout_denominator_empty_result_raises():
    """Empty eth_call return (0x) indicates contract misconfiguration or wrong chain, raising SystemExit."""
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"

    def mock_empty_urlopen(req, timeout=5):
        return MockResponse({"jsonrpc": "2.0", "id": 1, "result": "0x"})

    with patch.dict(os.environ, {}, clear=False), \
         patch("urllib.request.urlopen", side_effect=mock_empty_urlopen):
        os.environ.pop("POLYGON_RPC", None)
        with pytest.raises(SystemExit) as exc_info:
            le.get_payout_denominator(cond_id)
        msg = str(exc_info.value)
        assert le.CTF_CONTRACT in msg
        assert "returned empty data" in msg


def test_dry_run_probe():
    with patch("engine.markets.fetch_live_market") as mock_fetch:
        mock_fetch.return_value = MagicMock(
            market_slug="btc-updown-5m-12345",
            t_remaining=lambda: 180.0,
            up_token="token_up_123",
            down_token="token_down_456",
        )
        le.probe(series="btc-up-or-down-5m", cycles=5, live=False)


def test_redeem_submit_http_error_logs_unknown(tmp_path):
    """1. Submit raises HTTPError -> row exists with status='unknown', exception type and message recorded, non-zero exit."""
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    env_vars = make_live_env(acc, funder)

    import urllib.error
    def mock_urlopen_http_err(req, timeout=30):
        if "params" in req.full_url:
            return MockResponse({"address": POOL_WORKER, "nonce": "121"})
        elif "submit" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 504, "Gateway Timeout", {}, None)
        return MockResponse({})

    with patch.dict(os.environ, env_vars, clear=False), \
         patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=1), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen_http_err):
        with pytest.raises(SystemExit) as exc_info:
            le.redeem(cond_id, live=True)

    msg = str(exc_info.value)
    assert "signed and sent" in msg
    assert "HTTPError" in msg

    log_file = tmp_path / "live_orders.json"
    assert log_file.exists()
    entries = json.loads(log_file.read_text(encoding="utf-8"))
    assert len(entries) == 1
    rec = entries[0]
    assert rec["action"] == "REDEEM"
    assert rec["condition_id"] == cond_id
    assert rec["status"] == "unknown"
    assert rec["nonce"] == 121
    assert "error_type" in rec and rec["error_type"] == "HTTPError"
    assert "error" in rec and "504" in rec["error"]
    assert "payload" in rec


def test_redeem_submit_timeout_logs_unknown(tmp_path):
    """2. Submit raises a timeout -> same outcome, distinct exception detail."""
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    env_vars = make_live_env(acc, funder)

    import urllib.error
    def mock_urlopen_timeout(req, timeout=30):
        if "params" in req.full_url:
            return MockResponse({"address": POOL_WORKER, "nonce": "121"})
        elif "submit" in req.full_url:
            raise urllib.error.URLError("timed out")
        return MockResponse({})

    with patch.dict(os.environ, env_vars, clear=False), \
         patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=1), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen_timeout):
        with pytest.raises(SystemExit) as exc_info:
            le.redeem(cond_id, live=True)

    msg = str(exc_info.value)
    assert "signed and sent" in msg
    assert "URLError" in msg

    log_file = tmp_path / "live_orders.json"
    assert log_file.exists()
    entries = json.loads(log_file.read_text(encoding="utf-8"))
    assert len(entries) == 1
    rec = entries[0]
    assert rec["action"] == "REDEEM"
    assert rec["status"] == "unknown"
    assert rec["error_type"] == "URLError"
    assert "timed out" in rec["error"]


def test_redeem_submit_success_single_row_submitted(tmp_path):
    """3. Submit succeeds -> exactly one row, transitioning pending -> submitted, not two rows.
    Asserts status is 'submitted' against a mock relayer response whose body says PENDING."""
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    env_vars = make_live_env(acc, funder)
    recorded_requests = []
    # make_mock_urlopen returns {"transactionID": ..., "status": "PENDING"}
    mock_urlopen = make_mock_urlopen(recorded_requests, submit_hash="0xdeadbeef12345678")

    with patch.dict(os.environ, env_vars, clear=False), \
         patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=1), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen):
        le.redeem(cond_id, live=True)

    log_file = tmp_path / "live_orders.json"
    assert log_file.exists()
    entries = json.loads(log_file.read_text(encoding="utf-8"))
    assert len(entries) == 1
    rec = entries[0]
    assert rec["action"] == "REDEEM"
    assert rec["status"] == "submitted"
    assert "0xdeadbeef12345678" in rec["response"]
    assert rec["tx_hash"] == "0xdeadbeef12345678"
    assert len(rec["response"]) <= 400
    assert rec["nonce"] == 121
    assert "payload" in rec


def test_redeem_dry_run_writes_no_row(tmp_path):
    """4. Dry-run writes no row at all."""
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"

    def mock_urlopen_rpc(req, timeout=5):
        return MockResponse({"jsonrpc": "2.0", "id": 1, "result": "0x0000000000000000000000000000000000000000000000000000000000000001"})

    with patch.dict(os.environ, {}, clear=False), \
         patch.object(le, "RUN", tmp_path), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen_rpc):
        os.environ.pop("POLYGON_RPC", None)
        le.redeem(cond_id, live=False)

    log_file = tmp_path / "live_orders.json"
    assert not log_file.exists()


def test_audit_settlement_relayer_log_reader_finds_redeem_fixture(tmp_path):
    """5. audit_settlement.py's relayer-log reader finds a REDEEM record in a fixture written by _log_order itself."""
    import scripts.audit_settlement as audit  # live/scripts/, forked out of the repo root with the rest of live

    # Build the fixture using _log_order itself
    log_file = tmp_path / "live_orders.json"
    with patch.object(le, "RUN", tmp_path):
        le._log_order({
            "ts": 1723812345.67,
            "action": "REDEEM",
            "condition_id": "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f",
            "safe_funder": "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b",
            "signer": "0xD2C7F5514580184d32C70F6FEA95B69C5Cd72fa0",
            "target": le.CTF_CONTRACT,
            "call_data": "0x01b7037c...",
            "nonce": 121,
            "deadline": 1723812945,
            "payload": {"type": "WALLET"},
            "status": "submitted",
            "tx_hash": "0x9876543210fedcba",
            "response": json.dumps({"transactionHash": "0x9876543210fedcba", "status": "CONFIRMED"}),
        })

    def mock_relayer_get(req, timeout=5):
        return MockResponse({"transactionHash": "0x9876543210fedcba", "state": "MINED", "status": "CONFIRMED"})

    with patch("urllib.request.urlopen", side_effect=mock_relayer_get):
        res = audit.check_relayer_status(log_file=log_file)

    assert res.get("transactionHash") == "0x9876543210fedcba"
    assert res.get("status") == "CONFIRMED"


def test_redeem_submit_exception_log_update_failure_dumps_to_stderr(tmp_path, capsys):
    """R9 Item 1: When submit fails AND _update_order_log fails (returns False),
    SystemExit message states log update failed, and full transaction record reaches stderr."""
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    env_vars = make_live_env(acc, funder)

    import urllib.error
    def mock_urlopen_http_err(req, timeout=30):
        if "params" in req.full_url:
            return MockResponse({"address": POOL_WORKER, "nonce": "121"})
        elif "submit" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 504, "Gateway Timeout", {}, None)
        return MockResponse({})

    # Mock _update_order_log to simulate log file update failure
    with patch.dict(os.environ, env_vars, clear=False), \
         patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=1), \
         patch.object(le, "_update_order_log", return_value=False), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen_http_err):
        with pytest.raises(SystemExit) as exc_info:
            le.redeem(cond_id, live=True)

    msg = str(exc_info.value)
    assert "signed and sent" in msg
    assert "could NOT be updated" in msg

    captured = capsys.readouterr()
    assert "ERROR: Failed to update live_orders.json" in captured.err
    assert "REDEEM" in captured.err
    assert cond_id in captured.err
    assert "121" in captured.err


def test_log_order_corrupted_file_renamed_not_destroyed(tmp_path):
    """R9 Item 2: Malformed live_orders.json is preserved under a .corrupt. name
    and not overwritten on parse failure."""
    corrupt_content = '{"broken": json['
    log_file = tmp_path / "live_orders.json"
    log_file.write_text(corrupt_content, encoding="utf-8")

    with patch.object(le, "RUN", tmp_path):
        entry_id = le._log_order({
            "action": "REDEEM",
            "condition_id": "0x1234",
            "status": "pending",
        })

    # New log file exists and contains the new entry
    assert log_file.exists()
    entries = json.loads(log_file.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["id"] == entry_id

    # The corrupted file was renamed and preserved
    corrupt_files = list(tmp_path.glob("live_orders.corrupt.*.json"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == corrupt_content


def test_atomic_write_json_interrupted_leaves_file_intact(tmp_path):
    """R10 Item 1: Atomic write interrupted midway leaves the original file intact and valid."""
    log_file = tmp_path / "live_orders.json"
    original_data = [
        {"id": "entry-1", "action": "REDEEM", "status": "pending"},
        {"id": "entry-2", "action": "REDEEM", "status": "submitted"},
    ]
    log_file.write_text(json.dumps(original_data, indent=2), encoding="utf-8")

    # Patch os.fsync to raise an IOError simulating an interrupted write
    with patch("os.fsync", side_effect=IOError("Simulated disk error during fsync")):
        res = le._atomic_write_json(log_file, [{"id": "entry-3", "action": "NEW"}])

    assert res is False
    # Original file is intact, uncorrupted, and parses cleanly
    assert log_file.exists()
    recovered = json.loads(log_file.read_text(encoding="utf-8"))
    assert recovered == original_data


def test_redeem_submit_success_log_update_failure_dumps_to_stderr(tmp_path, capsys):
    """R10 Item 2: When submit succeeds but _update_order_log fails, SystemExit is raised,
    stderr carries the transaction record and tx_hash, and message names the ambiguity."""
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    env_vars = make_live_env(acc, funder)
    mock_urlopen = make_mock_urlopen([], submit_hash="0xabcdef1234567890")

    original_update = le._update_order_log

    def mock_update_order_log(entry_id, updates):
        # Allow pending write, fail submitted update
        if updates.get("status") == "submitted":
            return False
        return original_update(entry_id, updates)

    with patch.dict(os.environ, env_vars, clear=False), \
         patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=1), \
         patch.object(le, "_update_order_log", side_effect=mock_update_order_log), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen):
        with pytest.raises(SystemExit) as exc_info:
            le.redeem(cond_id, live=True)

    msg = str(exc_info.value)
    assert "Relayer accepted transaction" in msg
    assert "audit log update failed" in msg
    assert "0xabcdef1234567890" in msg

    captured = capsys.readouterr()
    assert "ERROR: Relayer accepted transaction" in captured.err
    assert "0xabcdef1234567890" in captured.err
    assert "submitted" in captured.err


def test_redeem_submit_keyboard_interrupt_logs_interrupted(tmp_path, capsys):
    """R10 Item 2: KeyboardInterrupt during submit stamps status='interrupted' and exits with warning."""
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    env_vars = make_live_env(acc, funder)

    def mock_urlopen_interrupt(req, timeout=30):
        if "params" in req.full_url:
            return MockResponse({"address": POOL_WORKER, "nonce": "121"})
        elif "submit" in req.full_url:
            raise KeyboardInterrupt()
        return MockResponse({})

    with patch.dict(os.environ, env_vars, clear=False), \
         patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=1), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen_interrupt):
        with pytest.raises(SystemExit) as exc_info:
            le.redeem(cond_id, live=True)

    msg = str(exc_info.value)
    assert "KeyboardInterrupt" in msg
    assert "may have been broadcast" in msg

    log_file = tmp_path / "live_orders.json"
    assert log_file.exists()
    entries = json.loads(log_file.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["status"] == "interrupted"
    assert entries[0]["error_type"] == "KeyboardInterrupt"


def test_log_order_corrupt_rename_failure_aborts_without_overwriting(tmp_path):
    """R10 Item 4.1: If corrupt log file cannot be renamed, _log_order aborts via SystemExit rather than overwriting."""
    corrupt_content = '{"broken": json['
    log_file = tmp_path / "live_orders.json"
    log_file.write_text(corrupt_content, encoding="utf-8")

    # Patch os.replace to raise OSError when renaming corrupt file
    with patch.object(le, "RUN", tmp_path), \
         patch("os.replace", side_effect=OSError("Access denied during rename")):
        with pytest.raises(SystemExit) as exc_info:
            le._log_order({
                "action": "REDEEM",
                "condition_id": "0x1234",
                "status": "pending",
            })

    assert "Refusing to overwrite corrupted log file" in str(exc_info.value)
    # The file still has its original corrupt content, NOT overwritten
    assert log_file.read_text(encoding="utf-8") == corrupt_content


def test_log_order_write_failure_aborts_without_submitting(tmp_path):
    """R11 Item 0a: If _atomic_write_json fails in _log_order, SystemExit is raised
    naming the path, and submit urlopen is never reached."""
    acc = Account.create()
    funder = "0xBa7c21Ac8968983e90BEcB989fe978889FEC266b"
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    env_vars = make_live_env(acc, funder)

    urls_called = []
    def mock_urlopen(req, timeout=30):
        urls_called.append(req.full_url)
        if "params" in req.full_url:
            return MockResponse({"address": POOL_WORKER, "nonce": "121"})
        return MockResponse({})

    with patch.dict(os.environ, env_vars, clear=False), \
         patch.object(le, "RUN", tmp_path), \
         patch.object(le, "get_payout_denominator", return_value=1), \
         patch.object(le, "_atomic_write_json", return_value=False), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen):
        with pytest.raises(SystemExit) as exc_info:
            le.redeem(cond_id, live=True)

    msg = str(exc_info.value)
    assert "Failed to record pending log entry" in msg
    assert "Nothing was submitted" in msg
    # Crucial invariant: submit endpoint is NEVER called when pending log write fails
    assert not any("submit" in u for u in urls_called)


# ===========================================================================
# Milestone 4 — Quote flags, Cancellation verbs, and Set B commands
# ===========================================================================


def test_quote_passes_post_only_default_true(tmp_path):
    """quote() defaults post_only=True and order_type=GTC via post_orders."""
    from py_clob_client_v2.clob_types import OrderType
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    dummy_market = MagicMock(
        up_token="tok_up", down_token="tok_dn",
        market_slug="btc-test-5m", tick_size=0.01, neg_risk=False
    )
    mock_client = MagicMock()
    mock_client.post_orders.return_value = [{"orderID": "venue-up"}, {"orderID": "venue-dn"}]
    mock_client.get_order.side_effect = lambda vid: {"asset_id": "tok_up"} if vid == "venue-up" else {"asset_id": "tok_dn"}
    db_path = tmp_path / "live.db"

    with patch("engine.markets.fetch_pinned_market", return_value=dummy_market), \
         patch.object(le, "client", return_value=mock_client), \
         patch.object(le, "open_notional", return_value=0.0), \
         patch.object(le, "RUN", tmp_path):
        le.quote(cond_id, price=0.50, size=10.0, live=True, db_path=db_path)

    assert mock_client.post_orders.call_count == 1
    args, kwargs = mock_client.post_orders.call_args
    assert kwargs.get("post_only") is True
    batch_args = args[0]
    assert len(batch_args) == 2
    assert batch_args[0].orderType == OrderType.GTC
    assert batch_args[1].orderType == OrderType.GTC


def test_quote_does_not_require_maker_rewards(tmp_path):
    """A market paying zero maker rewards is quotable.

    Every market the ranker graduates is source=spread with daily=0.00, and the
    income is the merge below $1.00, not the rebate. The fleet has passed
    require_rewards=False since spread capture landed; this asserts the CLI
    reaches the venue the same way instead of refusing its own universe.
    """
    cond_id = "0xebd7653a13838fa5838537370b9b09fe91169e02171b8c62f7ff4018ebee59c7"
    dummy_market = MagicMock(
        up_token="tok_up", down_token="tok_dn",
        market_slug="mlb-atl-min-2026-08-18", tick_size=0.01, neg_risk=False
    )
    seen = {}

    def _fetch(cid, require_rewards=True):
        seen["require_rewards"] = require_rewards
        return dummy_market

    mock_client = MagicMock()
    mock_client.post_orders.return_value = [{"orderID": "venue-up"}, {"orderID": "venue-dn"}]
    mock_client.get_order.side_effect = (
        lambda vid: {"asset_id": "tok_up"} if vid == "venue-up" else {"asset_id": "tok_dn"}
    )

    with patch("engine.markets.fetch_pinned_market", side_effect=_fetch),          patch.object(le, "client", return_value=mock_client),          patch.object(le, "open_notional", return_value=0.0),          patch.object(le, "RUN", tmp_path):
        le.quote(cond_id, price=0.50, size=5.0, live=True,
                 db_path=tmp_path / "live.db")

    assert seen["require_rewards"] is False
    assert mock_client.post_orders.call_count == 1


def test_quote_allows_explicit_no_post_only(tmp_path):
    """quote(..., post_only=False) passes post_only=False to SDK post_orders."""
    from py_clob_client_v2.clob_types import OrderType
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    dummy_market = MagicMock(
        up_token="tok_up", down_token="tok_dn",
        market_slug="btc-test-5m", tick_size=0.01, neg_risk=False
    )
    mock_client = MagicMock()
    mock_client.post_orders.return_value = [{"orderID": "venue-up"}, {"orderID": "venue-dn"}]
    mock_client.get_order.side_effect = lambda vid: {"asset_id": "tok_up"} if vid == "venue-up" else {"asset_id": "tok_dn"}
    db_path = tmp_path / "live.db"

    with patch("engine.markets.fetch_pinned_market", return_value=dummy_market), \
         patch.object(le, "client", return_value=mock_client), \
         patch.object(le, "open_notional", return_value=0.0), \
         patch.object(le, "RUN", tmp_path):
        le.quote(cond_id, price=0.50, size=10.0, live=True, post_only=False, db_path=db_path)

    assert mock_client.post_orders.call_count == 1
    args, kwargs = mock_client.post_orders.call_args
    assert kwargs.get("post_only") is False


def test_quote_tif_gtd_with_expiration(tmp_path):
    """quote(..., tif='GTD', expiration=1786855000) passes OrderType.GTD and expiration."""
    from py_clob_client_v2.clob_types import OrderType
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    dummy_market = MagicMock(
        up_token="tok_up", down_token="tok_dn",
        market_slug="btc-test-5m", tick_size=0.01, neg_risk=False
    )
    mock_client = MagicMock()
    mock_client.post_orders.return_value = [{"orderID": "venue-up"}, {"orderID": "venue-dn"}]
    mock_client.get_order.side_effect = lambda vid: {"asset_id": "tok_up"} if vid == "venue-up" else {"asset_id": "tok_dn"}
    db_path = tmp_path / "live.db"

    with patch("engine.markets.fetch_pinned_market", return_value=dummy_market), \
         patch.object(le, "client", return_value=mock_client), \
         patch.object(le, "open_notional", return_value=0.0), \
         patch.object(le, "RUN", tmp_path):
        le.quote(cond_id, price=0.50, size=10.0, live=True, tif="GTD", expiration=1786855000, db_path=db_path)

    assert mock_client.create_order.call_count == 2
    for call in mock_client.create_order.call_args_list:
        args, _ = call
        assert args[0].expiration == 1786855000

    assert mock_client.post_orders.call_count == 1
    args, kwargs = mock_client.post_orders.call_args
    batch_args = args[0]
    assert batch_args[0].orderType == OrderType.GTD
    assert batch_args[1].orderType == OrderType.GTD
    assert kwargs.get("post_only") is True


def test_batch_quote_happy_path_both_succeed(tmp_path):
    """Batch quote places both legs, verifies asset_id on both, and attaches venue IDs as open."""
    from engine.order_registry import OrderRegistry
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    dummy_market = MagicMock(
        up_token="tok_up_111", down_token="tok_dn_222",
        market_slug="btc-test-5m", tick_size=0.01, neg_risk=False
    )
    mock_client = MagicMock()
    mock_client.post_orders.return_value = [{"orderID": "v-up-1"}, {"orderID": "v-dn-2"}]
    mock_client.get_order.side_effect = lambda vid: {"asset_id": "tok_up_111"} if vid == "v-up-1" else {"asset_id": "tok_dn_222"}
    db_path = tmp_path / "live.db"

    with patch("engine.markets.fetch_pinned_market", return_value=dummy_market), \
         patch.object(le, "client", return_value=mock_client), \
         patch.object(le, "open_notional", return_value=0.0), \
         patch.object(le, "RUN", tmp_path):
        le.quote(cond_id, price=0.60, size=10.0, live=True, db_path=db_path)

    registry = OrderRegistry(db_path=db_path)
    up_ord = registry.get_order_by_venue_id("v-up-1")
    dn_ord = registry.get_order_by_venue_id("v-dn-2")
    assert up_ord is not None
    assert dn_ord is not None
    assert up_ord.status == "open"
    assert dn_ord.status == "open"
    assert up_ord.token_id == "tok_up_111"
    assert dn_ord.token_id == "tok_dn_222"
    assert up_ord.pair_id == dn_ord.pair_id


def test_batch_quote_partial_failure_auto_cancels_naked_leg(tmp_path, capsys):
    """If one leg succeeds and one fails, immediately auto-cancel the resting leg to prevent naked exposure."""
    from engine.order_registry import OrderRegistry
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    dummy_market = MagicMock(
        up_token="tok_up_111", down_token="tok_dn_222",
        market_slug="btc-test-5m", tick_size=0.01, neg_risk=False
    )
    mock_client = MagicMock()
    # UP succeeds, DOWN fails with rejection error
    mock_client.post_orders.return_value = [{"orderID": "v-up-survivor"}, {"errorMsg": "balance error", "success": False}]
    mock_client.cancel_order.return_value = {"canceled": ["v-up-survivor"]}
    db_path = tmp_path / "live.db"

    with patch("engine.markets.fetch_pinned_market", return_value=dummy_market), \
         patch.object(le, "client", return_value=mock_client), \
         patch.object(le, "open_notional", return_value=0.0), \
         patch.object(le, "RUN", tmp_path):
        with pytest.raises(SystemExit) as exc:
            le.quote(cond_id, price=0.60, size=10.0, live=True, db_path=db_path)
        assert "Batch quote failed partially" in str(exc.value)

    assert mock_client.cancel_order.call_count == 1
    cancel_arg = mock_client.cancel_order.call_args[0][0]
    assert cancel_arg.orderID == "v-up-survivor"

    err = capsys.readouterr().err
    assert "CRITICAL: Batch quote partial failure!" in err
    assert "Issuing emergency cancel for surviving leg v-up-survivor" in err

    registry = OrderRegistry(db_path=db_path)
    # Neither row should remain open
    with registry._conn() as conn:
        rows = conn.execute("SELECT * FROM orders").fetchall()
        assert len(rows) == 2
        for r in rows:
            assert r["status"] == "cancelled"
            assert r["order_id"] is None


def test_batch_quote_reverse_response_attribution_and_half_price(tmp_path):
    """At price=0.50 (identical amounts), reversed response array is caught by get_order verification and fails closed."""
    from engine.order_registry import OrderRegistry
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    dummy_market = MagicMock(
        up_token="tok_up_111", down_token="tok_dn_222",
        market_slug="btc-test-5m", tick_size=0.01, neg_risk=False
    )
    mock_client = MagicMock()
    # Swapped responses: [DOWN, UP] instead of [UP, DOWN]
    mock_client.post_orders.return_value = [{"orderID": "v-dn-2"}, {"orderID": "v-up-1"}]
    mock_client.get_order.side_effect = lambda vid: {"asset_id": "tok_dn_222"} if vid == "v-dn-2" else {"asset_id": "tok_up_111"}
    mock_client.cancel_order.return_value = {}
    db_path = tmp_path / "live.db"

    with patch("engine.markets.fetch_pinned_market", return_value=dummy_market), \
         patch.object(le, "client", return_value=mock_client), \
         patch.object(le, "open_notional", return_value=0.0), \
         patch.object(le, "RUN", tmp_path):
        with pytest.raises(SystemExit) as exc:
            # price = 0.50: UP cost = 5.0, DOWN cost = 5.0 (amounts identical)
            le.quote(cond_id, price=0.50, size=10.0, live=True, db_path=db_path)
        assert "FAIL CLOSED: Order verification mismatch" in str(exc.value)

    # Fail closed: cancel both orders
    assert mock_client.cancel_order.call_count == 2
    cancelled_ids = {call[0][0].orderID for call in mock_client.cancel_order.call_args_list}
    assert cancelled_ids == {"v-dn-2", "v-up-1"}

    registry = OrderRegistry(db_path=db_path)
    with registry._conn() as conn:
        rows = conn.execute("SELECT * FROM orders").fetchall()
        assert len(rows) == 2
        for r in rows:
            assert r["status"] == "cancelled"
            assert r["order_id"] is None


def test_batch_quote_verification_mismatch_fails_closed(tmp_path):
    """Venue returning mismatched asset_id triggers fail-closed cancellation of all batch orders."""
    from engine.order_registry import OrderRegistry
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    dummy_market = MagicMock(
        up_token="tok_up_111", down_token="tok_dn_222",
        market_slug="btc-test-5m", tick_size=0.01, neg_risk=False
    )
    mock_client = MagicMock()
    mock_client.post_orders.return_value = [{"orderID": "v-1"}, {"orderID": "v-2"}]
    # get_order returns completely unrelated token
    mock_client.get_order.return_value = {"asset_id": "tok_foreign_999"}
    mock_client.cancel_order.return_value = {}
    db_path = tmp_path / "live.db"

    with patch("engine.markets.fetch_pinned_market", return_value=dummy_market), \
         patch.object(le, "client", return_value=mock_client), \
         patch.object(le, "open_notional", return_value=0.0), \
         patch.object(le, "RUN", tmp_path):
        with pytest.raises(SystemExit) as exc:
            le.quote(cond_id, price=0.60, size=10.0, live=True, db_path=db_path)
        assert "FAIL CLOSED: Order verification mismatch" in str(exc.value)

    assert mock_client.cancel_order.call_count == 2
    registry = OrderRegistry(db_path=db_path)
    with registry._conn() as conn:
        rows = conn.execute("SELECT * FROM orders").fetchall()
        assert len(rows) == 2
        for r in rows:
            assert r["status"] == "cancelled"
            assert r["order_id"] is None


def test_batch_quote_both_fail(tmp_path, capsys):
    """If both legs return no order ID at the venue, both rows stay pending for orphan adoption with zero cancel calls."""
    from engine.order_registry import OrderRegistry
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    dummy_market = MagicMock(
        up_token="tok_up_111", down_token="tok_dn_222",
        market_slug="btc-test-5m", tick_size=0.01, neg_risk=False
    )
    mock_client = MagicMock()
    mock_client.post_orders.return_value = [{"errorMsg": "err1"}, {"errorMsg": "err2"}]
    db_path = tmp_path / "live.db"

    with patch("engine.markets.fetch_pinned_market", return_value=dummy_market), \
         patch.object(le, "client", return_value=mock_client), \
         patch.object(le, "open_notional", return_value=0.0), \
         patch.object(le, "RUN", tmp_path):
        le.quote(cond_id, price=0.60, size=10.0, live=True, db_path=db_path)

    assert mock_client.cancel_order.call_count == 0
    err = capsys.readouterr().err
    assert "WARNING: no order IDs in batch quote response" in err

    registry = OrderRegistry(db_path=db_path)
    with registry._conn() as conn:
        rows = conn.execute("SELECT * FROM orders").fetchall()
        assert len(rows) == 2
        for r in rows:
            assert r["status"] == "pending"
            assert r["order_id"] is None


def test_quote_rejects_post_only_with_fok_or_fak():
    """quote() fails at parse-time if post_only is combined with FOK or FAK."""
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"

    with pytest.raises(SystemExit) as exc_fok:
        le.quote(cond_id, price=0.50, size=10.0, live=False, post_only=True, tif="FOK")
    assert "--post-only is valid only for GTC and GTD orders" in str(exc_fok.value)

    with pytest.raises(SystemExit) as exc_fak:
        le.quote(cond_id, price=0.50, size=10.0, live=False, post_only=True, tif="FAK")
    assert "--post-only is valid only for GTC and GTD orders" in str(exc_fak.value)


def test_quote_rejects_gtd_without_expiration():
    """quote() fails at parse-time if tif=GTD is passed without expiration."""
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"

    with pytest.raises(SystemExit) as exc:
        le.quote(cond_id, price=0.50, size=10.0, live=False, tif="GTD", expiration=None)
    assert "--expiration (UTC epoch seconds) is required when --tif GTD" in str(exc.value)


def test_cancel_single_order_dry_run_and_live(tmp_path, capsys):
    """cancel_single_order prints dry-run message when live=False, calls SDK and updates DB when live=True."""
    from engine.order_registry import OrderRegistry, OrderRecord
    db_path = tmp_path / "live.db"
    registry = OrderRegistry(db_path=db_path)
    now_ms = 1_000_000
    registry.create_order(OrderRecord(
        id="local-1", order_id="venue-order-99", condition_id="0xcond",
        token_id="tok-1", side="BUY", price=0.50, original_size=10.0,
        status="open", posted_ts=now_ms, last_polled_ts=now_ms,
        pair_id="pair-1", max_pair_cost_at_post=0.995,
    ))

    # 1. Dry run
    le.cancel_single_order("venue-order-99", live=False, db_path=db_path)
    out_dry = capsys.readouterr().out
    assert "DRY RUN -- would cancel order venue-order-99" in out_dry
    assert registry.get_order("local-1").status == "open"

    # 2. Live execution
    mock_client = MagicMock()
    mock_client.cancel_order.return_value = {"canceled": ["venue-order-99"]}
    with patch.object(le, "client", return_value=mock_client):
        le.cancel_single_order("venue-order-99", live=True, db_path=db_path)

    out_live = capsys.readouterr().out
    assert "venue-order-99" in out_live
    assert registry.get_order("local-1").status == "cancelled"


def test_cancel_single_order_handles_venue_rejection(tmp_path):
    """cancel_single_order raises SystemExit naming refusal when venue rejects."""
    db_path = tmp_path / "live.db"
    mock_client = MagicMock()
    mock_client.cancel_order.side_effect = RuntimeError("Order not found or already closed")

    with patch.object(le, "client", return_value=mock_client):
        with pytest.raises(SystemExit) as exc:
            le.cancel_single_order("venue-order-missing", live=True, db_path=db_path)

    msg = str(exc.value)
    assert "CANCEL REFUSED" in msg
    assert "Order not found or already closed" in msg


def test_cancel_market_dry_run_and_live(tmp_path, capsys):
    """cancel_market prints dry-run message when live=False, calls SDK and cancels active orders when live=True."""
    from engine.order_registry import OrderRegistry, OrderRecord
    db_path = tmp_path / "live.db"
    registry = OrderRegistry(db_path=db_path)
    now_ms = 1_000_000
    registry.create_order(OrderRecord(
        id="local-1", order_id="venue-1", condition_id="0xmarketA",
        token_id="tok-1", side="BUY", price=0.50, original_size=10.0,
        status="open", posted_ts=now_ms, last_polled_ts=now_ms,
        pair_id="pair-1", max_pair_cost_at_post=0.995,
    ))
    registry.create_order(OrderRecord(
        id="local-2", order_id="venue-2", condition_id="0xmarketB",
        token_id="tok-2", side="BUY", price=0.50, original_size=10.0,
        status="open", posted_ts=now_ms, last_polled_ts=now_ms,
        pair_id="pair-2", max_pair_cost_at_post=0.995,
    ))

    # 1. Dry run
    le.cancel_market("0xmarketA", live=False, db_path=db_path)
    out_dry = capsys.readouterr().out
    assert "DRY RUN -- would cancel all active orders for market 0xmarketA" in out_dry
    assert registry.get_order("local-1").status == "open"
    assert registry.get_order("local-2").status == "open"

    # 2. Live execution
    mock_client = MagicMock()
    mock_client.cancel_market_orders.return_value = {"canceled": ["venue-1"]}
    with patch.object(le, "client", return_value=mock_client):
        le.cancel_market("0xmarketA", live=True, db_path=db_path)

    assert registry.get_order("local-1").status == "cancelled"
    assert registry.get_order("local-2").status == "open"


def test_cancel_market_handles_venue_error(tmp_path):
    """cancel_market raises SystemExit naming refusal when venue rejects."""
    db_path = tmp_path / "live.db"
    mock_client = MagicMock()
    mock_client.cancel_market_orders.side_effect = RuntimeError("Market cancel failed")

    with patch.object(le, "client", return_value=mock_client):
        with pytest.raises(SystemExit) as exc:
            le.cancel_market("0xmarket-fail", live=True, db_path=db_path)

    msg = str(exc.value)
    assert "CANCEL-MARKET REFUSED" in msg
    assert "Market cancel failed" in msg


def test_cancel_all_cmd_dry_run_and_live(capsys):
    """cancel_all handles dry run, live invocation, and venue failure."""
    # 1. Dry run
    le.cancel_all(live=False)
    assert "DRY RUN -- would cancel ALL open orders" in capsys.readouterr().out

    # 2. Live execution
    mock_client = MagicMock()
    mock_client.cancel_all.return_value = {"canceled": ["ord-1", "ord-2"]}
    with patch.object(le, "client", return_value=mock_client):
        le.cancel_all(live=True)
    assert "ord-1" in capsys.readouterr().out

    # 3. Live failure
    mock_client.cancel_all.side_effect = RuntimeError("Unauthorized")
    with patch.object(le, "client", return_value=mock_client):
        with pytest.raises(SystemExit) as exc:
            le.cancel_all(live=True)
    assert "CANCEL-ALL REFUSED" in str(exc.value)


def test_status_cmd_reports_auth_and_balances(capsys):
    """status() prints address, funder, and open order summary."""
    mock_client = MagicMock()
    mock_client.get_address.return_value = "0x1111222233334444555566667777888899990000"
    mock_client.get_open_orders.return_value = [
        {"side": "BUY", "original_size": "100", "price": "0.50", "id": "venue-ord-1"}
    ]

    with patch.object(le, "client", return_value=mock_client), \
         patch.dict(os.environ, {"POLY_FUNDER": "0xfunder1234"}, clear=False):
        le.status()

    out = capsys.readouterr().out
    assert "0x1111222233334444555566667777888899990000" in out
    assert "0xfunder1234" in out
    assert "open orders    1" in out
    assert "venue-ord-1" in out


def test_balance_cmd_queries_funder_and_collateral(capsys):
    """balance() parses 6dp USDC balance and allowances correctly."""
    mock_client = MagicMock()
    mock_client.get_balance_allowance.return_value = {
        "balance": "50000000",  # $50.00 USDC
        "allowance": "100000000",  # $100.00 USDC
        "allowances": {"0xexchange_contract": "100000000"}
    }

    with patch.object(le, "client", return_value=mock_client):
        le.balance("0xfunder_addr")

    out = capsys.readouterr().out
    assert "0xfunder_addr" in out
    assert "$50.00 USDC" in out
    assert "$100.00" in out


def test_pairs_cmd_lists_registry_records(tmp_path, capsys):
    """pairs() outputs table of all registered pairs and held sizes."""
    from engine.order_registry import OrderRegistry, OrderRecord
    db_path = tmp_path / "live.db"
    registry = OrderRegistry(db_path=db_path)
    now_ms = 1_000_000

    registry.create_order(OrderRecord(
        id="loc-1", order_id="v-1", condition_id="0xcondition1234",
        token_id="tok-up-1", side="BUY", price=0.60, original_size=10.0,
        status="open", posted_ts=now_ms, last_polled_ts=now_ms,
        pair_id="pair-abc-1", max_pair_cost_at_post=0.995,
    ))

    le.pairs(db_path=db_path)
    out = capsys.readouterr().out
    assert "pair-abc-1" in out
    assert "0xcondition1" in out


def test_quote_rejects_unknown_tif(tmp_path):
    """quote() raises SystemExit on unknown tif rather than falling back to GTC."""
    cond_id = "0x26b64228a9fb13e5c2221cd5879fa0f235cee8ab254c0f094977cc86beeb6a2f"
    dummy_market = MagicMock(
        up_token="tok_up", down_token="tok_dn",
        market_slug="btc-test-5m", tick_size=0.01, neg_risk=False
    )
    mock_client = MagicMock()
    db_path = tmp_path / "live.db"

    with patch("engine.markets.fetch_pinned_market", return_value=dummy_market), \
         patch.object(le, "client", return_value=mock_client), \
         patch.object(le, "open_notional", return_value=0.0), \
         patch.object(le, "RUN", tmp_path):
        with pytest.raises(SystemExit) as exc:
            le.quote(cond_id, price=0.50, size=10.0, live=True, post_only=False, tif="INVALID", db_path=db_path)
        assert "unknown --tif 'INVALID'" in str(exc.value)
        assert "expected one of GTC, GTD, FOK, FAK" in str(exc.value)

    assert mock_client.create_order.call_count == 0
    assert mock_client.post_orders.call_count == 0


def test_probe_posts_with_post_only_true():
    """probe() in live mode must always pass post_only=True to client.post_order."""
    from py_clob_client_v2.clob_types import OrderType
    mock_client = MagicMock()
    mock_client.create_order.return_value = "signed_order_obj"
    mock_client.post_order.return_value = {"orderID": "probe-ord-1"}
    mock_client.get_order.return_value = {"size_matched": "0"}
    mock_client.get_order_book.return_value = None

    class FakeWSApp:
        def __init__(self, url, on_open=None, on_message=None):
            self.on_open = on_open
            self.on_message = on_message
        def run_forever(self):
            if self.on_open:
                self.on_open(MagicMock())
        def close(self):
            pass

    with patch("websocket.WebSocketApp", FakeWSApp), \
         patch.object(le, "client", return_value=mock_client), \
         patch("time.sleep", return_value=None):
        le.probe(token_id="tok_fixed_123", cycles=1, live=True)

    assert mock_client.post_order.call_count == 1
    args, kwargs = mock_client.post_order.call_args
    assert args[0] == "signed_order_obj"
    assert (len(args) > 1 and args[1] == OrderType.GTC) or kwargs.get("order_type") == OrderType.GTC
    assert kwargs.get("post_only") is True


def test_probe_requires_either_series_or_token_id():
    """probe() with neither --series nor --token-id exits non-zero naming both flags."""
    with pytest.raises(SystemExit) as exc:
        le.probe(series=None, token_id=None, live=False)
    msg = str(exc.value)
    assert "--series" in msg
    assert "--token-id" in msg
    assert "probe requires exactly one" in msg


def test_probe_with_token_id_alone_works():
    """probe() with --token-id alone passes dry-run execution validation."""
    le.probe(token_id="tok_fixed_456", cycles=1, live=False)


def test_probe_with_both_flags_rejected():
    """probe() with both --series and --token-id exits non-zero naming mutual exclusivity."""
    with pytest.raises(SystemExit) as exc:
        le.probe(series="btc-up-or-down-5m", token_id="tok_fixed_456", live=False)
    assert "probe accepts either --series or --token-id, not both" in str(exc.value)
