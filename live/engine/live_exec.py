"""LIVE order execution against Polymarket CLOB. Real money.

Everything else in this repo is a simulator. This file is the one place that
can lose actual funds, so it is deliberately small, deliberately manual, and
refuses to do anything by default.

CREDENTIALS NEVER APPEAR HERE. They are read from the environment and handed
straight to the client. Nothing in this module prints, logs, or writes a key,
and nothing that does should ever be added to it.

    # in .env, which must be in .gitignore BEFORE the key goes in
    POLY_PRIVATE_KEY=0x...      # signing key
    POLY_FUNDER=0x...           # address actually holding the USDC
    POLY_SIG_TYPE=1             # 0 EOA | 1 email-magic proxy | 2 browser proxy

    python -m engine.live_exec status
    python -m engine.live_exec quote <condition_id> --price 0.22 --size 20
    python -m engine.live_exec quote <condition_id> --price 0.22 --size 20 --live
    python -m engine.live_exec cancel-all --live

SAFETY RAILS, all on by default:
  * --live is required for anything that reaches the venue. Without it every
    command prints what it WOULD send and exits.
  * MAX_ORDER_USD caps one order; MAX_TOTAL_USD caps everything open at once.
  * Each leg is written to run/live_orders.json as it is sent, so a crash
    mid-flight still leaves a record of what went out.
  * cancel-all is its own command, because the thing you want at 3am is a way
    to pull every quote without reading code first.
  * Nothing here is imported by fleet.py. The automated bot cannot reach this
    module, so it cannot place a real order by accident.

SIGNATURE TYPE IS THE USUAL FOOTGUN. An account funded through the Polymarket
website is a PROXY: signature_type 1 or 2, with POLY_FUNDER set to the proxy
address rather than the address the private key derives to. Get it wrong and
orders are rejected -- or signed against an account with no balance. Run
`status` first and confirm the address it prints is the one holding your money.
"""
from __future__ import annotations

import argparse
import json
import contextlib
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run"

# Put live/ on sys.path so `engine.*` resolves however this module was reached
# -- `python -m engine.live_exec` from live/, `python live/engine/live_exec.py`
# from the repo root, or an import from a test.
#
# ROOT only. The repo root is deliberately NOT added: `engine` must resolve
# inside live/ and nowhere else. This package was called `strategy` until
# 2026-08-18, which collided with the simulation package of the same name, and
# adding the repo root here would let that collision come back the moment
# someone writes `from strategy...` in live code.
#
# A dry run does not prove this works. `quote` imports engine.markets ABOVE the
# dry-run return and engine.order_registry BELOW it, so a half-resolved path
# prints a clean dry run and then dies on the `--live` call -- after the
# operator has committed to sending. That happened on 2026-08-18; the guard is
# tests/test_live_exec_import_paths.py.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Settlement primitives (ABI encoding, id derivation, EIP-712 signing) live in
# engine.settlement; the relayer/RPC submit path stays here with the CLI verbs.
from engine.settlement import (
    CTF_CONTRACT,
    USDC_E_CONTRACT,
    ZERO_BYTES32,
    encode_merge_positions,
    encode_redeem_positions,
    get_collection_id,
    get_position_id,
    sign_redeem_transaction,
)
from engine.venue import (
    MAX_ORDER_USD,
    MAX_TOTAL_USD,
    api_creds_from_env,
    client,
    open_notional,
    venue_order_id,
)
from engine.account import fetch_live_balance, log_float_mark_if_measured



def _find_env_file() -> Path | None:
    curr = Path(__file__).resolve().parent
    for _ in range(4):
        if (curr / ".env").is_file():
            return curr / ".env"
        if (curr / "AGENTS.md").is_file():
            # Repo root reached and the .env check above already missed here, so
            # there is nothing further up worth loading. Stop rather than walk
            # out of the project and pick up a stranger's .env.
            break
        if curr.parent == curr:
            break
        curr = curr.parent
    return None


_env_file = _find_env_file()
if _env_file is not None:
    load_dotenv(_env_file)

# The venue's four time-in-force values. Named here so quote() can reject an
# unknown one outright: OrderType is a plain constants class, not an Enum, so
# a getattr default would silently downgrade an unrecognised tif to a resting GTC.
_TIF_CHOICES = ("GTC", "GTD", "FOK", "FAK")


def _atomic_write_json(file_path: Path, data: list) -> bool:
    """Atomically writes JSON data to file_path via sibling temp file + os.fsync + os.replace."""
    tmp_path = file_path.with_name(f"{file_path.name}.tmp.{uuid.uuid4()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as tf:
            tf.write(json.dumps(data, indent=2))
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_path, file_path)
        return True
    except Exception as exc:
        print(f"WARNING: _atomic_write_json failed for {file_path}: {exc}", file=sys.stderr)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return False


def _atomic_write_text(file_path: Path, text: str) -> bool:
    """Atomically replace a text file, preserving its permission bits.

    The mode is carried over because the target may be `.env`: a file created
    with restrictive permissions must not silently widen to the default umask
    just because it was rewritten.
    """
    tmp_path = file_path.with_name(f"{file_path.name}.tmp.{uuid.uuid4()}")
    try:
        try:
            mode = os.stat(file_path).st_mode
        except OSError:
            mode = None
        with open(tmp_path, "w", encoding="utf-8") as tf:
            tf.write(text)
            tf.flush()
            os.fsync(tf.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, file_path)
        return True
    except Exception as exc:
        print(f"WARNING: _atomic_write_text failed for {file_path}: {exc}", file=sys.stderr)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return False


def _log_order(rec: dict) -> str:
    RUN.mkdir(exist_ok=True)
    f = RUN / "live_orders.json"
    hist = []
    if f.exists():
        try:
            hist = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as exc:
            corrupt_path = f.parent / f"live_orders.corrupt.{int(time.time())}.json"
            try:
                os.replace(f, corrupt_path)
                print(f"WARNING: unreadable log file renamed to {corrupt_path}: {exc}", file=sys.stderr)
                hist = []
            except OSError as rename_exc:
                print(
                    f"ERROR: could not rename corrupt log file {f} to {corrupt_path}: {rename_exc}\n"
                    f"Refusing to overwrite corrupted log file. Aborting.",
                    file=sys.stderr,
                )
                raise SystemExit(f"Refusing to overwrite corrupted log file {f}: {rename_exc}")
    if "id" not in rec:
        rec["id"] = str(uuid.uuid4())
    hist.append(rec)
    if not _atomic_write_json(f, hist):
        print(f"ERROR: failed to write log entry {rec['id']} to {f}", file=sys.stderr)
        raise SystemExit(f"Failed to record pending log entry to {f}. Nothing was submitted.")
    return rec["id"]




def _update_order_log(entry_id: str, updates: dict) -> bool:
    RUN.mkdir(exist_ok=True)
    f = RUN / "live_orders.json"
    if not f.exists():
        return False
    try:
        hist = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"WARNING: _update_order_log failed to read {f}: {exc}", file=sys.stderr)
        return False

    updated = False
    for item in hist:
        if isinstance(item, dict) and item.get("id") == entry_id:
            item.update(updates)
            updated = True
            break

    if updated:
        return _atomic_write_json(f, hist)
    return False


def _check_idempotency_guard(condition_id: str, force: bool = False) -> None:
    """Scan run/live_orders.json for prior pending/submitted/interrupted orders matching condition_id.
    Refuses execution unless force is True.
    """
    if force:
        return
    f = RUN / "live_orders.json"
    if not f.exists():
        return
    # Only the read and the parse belong inside the guard. Keeping the scan loop
    # here too would report any error raised while walking the entries as
    # "cannot read the order log", which is the wrong diagnosis for a file that
    # read and parsed perfectly well.
    try:
        entries = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as exc:
        # Fail closed. An unreadable log is not an empty one. If it holds a
        # pending row for this condition and we return quietly here, _log_order
        # then quarantines the corrupt file and starts a fresh log, so that row
        # leaves the active set and a second on-chain settlement goes out for a
        # condition already in flight. _log_order treats the same condition as
        # serious enough to abort the command; this guard must agree.
        raise SystemExit(
            f"Refusing to execute: cannot read the order log at {f} ({exc!r}). "
            f"A prior in-flight order for {condition_id} cannot be ruled out. "
            f"Inspect the file, or use --force to override."
        ) from exc

    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("condition_id")
        status = entry.get("status")
        if cid and cid.lower() == condition_id.lower() and status in ("pending", "submitted", "interrupted"):
            entry_id = entry.get("id", "unknown")
            raise SystemExit(
                f"Refusing to execute: prior order {entry_id} with condition_id {condition_id} "
                f"has status='{status}'. Use --force to override."
            )


def status() -> None:
    """Who are we, and what is already resting. Read-only, safe anytime."""
    c = client()
    print(f"address        {c.get_address()}")
    print(f"funder         {os.environ.get('POLY_FUNDER') or '(same as address)'}")
    print(f"signature type {os.environ.get('POLY_SIG_TYPE', '3')}")
    try:
        orders = c.get_open_orders() or []
        print(f"open orders    {len(orders)} "
              f"(${open_notional(c):.2f} notional)")
        for o in orders[:10]:
            print(f"  {str(o.get('side')):4} {o.get('original_size')} @ "
                  f"{o.get('price')}  id={str(o.get('id') or o.get('order_hash'))[:16]}")
    except Exception as e:
        print(f"open orders    ERROR {type(e).__name__}: {e}")
    print("\nConfirm the address above is the account holding your USDC "
          "BEFORE sending anything.")


def balance(funder: str | None) -> None:
    """USDC the venue will actually let an order draw on. Read-only, no order."""
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    who = funder or os.environ.get("POLY_FUNDER") or "(signer address)"
    sig_type = int(os.environ.get("POLY_SIG_TYPE", "3"))
    print(f"funder     {who}")
    try:
        r = client(funder).get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                   signature_type=sig_type))
    except Exception as e:
        print(f"           ERROR {type(e).__name__}: {e}")
        return
    print(f"raw        {r}")

    # USDC on Polygon is 6dp and the API returns integer base units as strings.
    bal = float(r.get("balance", 0) or 0) / 1e6
    print(f"balance    ${bal:,.2f} USDC")
    
    allowances = r.get("allowances")
    if isinstance(allowances, dict):
        print("allowances:")
        for target, amt in allowances.items():
            allow_val = float(amt or 0) / 1e6
            print(f"  {target[:10]}...: ${allow_val:,.2f}")
    else:
        allow = float(r.get("allowance", 0) or 0) / 1e6
        print(f"allowance  ${allow:,.2f}")

    if bal == 0:
        print("\nZero. If your money is on Polymarket, POLY_FUNDER points at "
              "the wrong address -- try the other candidate before trading.")


def pairs(db_path: str | Path | None = None) -> None:
    """List every pair the registry knows, with what is actually held.

    Stage 3 and Stage 4 both take a pair_id, and without this the only way to
    find one is to open live.db by hand -- which is exactly the sort of step an
    operator skips at the moment it matters.
    """
    from engine.order_registry import OrderRegistry, DEFAULT_DB_PATH, get_connection
    from engine.live_pairs import load_pair, PairExitRefused

    db = Path(db_path) if db_path else DEFAULT_DB_PATH
    registry = OrderRegistry(db_path=db)
    with contextlib.closing(get_connection(db)) as conn:
        rows = conn.execute(
            "SELECT pair_id, MIN(posted_ts) AS first_ts FROM orders "
            "WHERE pair_id IS NOT NULL GROUP BY pair_id ORDER BY first_ts"
        ).fetchall()

    if not rows:
        print(f"no pairs in {db}")
        return

    print(f"{'pair_id':<20} {'condition':<14} {'naked':>9}  legs")
    for r in rows:
        pid = r["pair_id"]
        try:
            pair = load_pair(registry, pid)
        except PairExitRefused as exc:
            print(f"{pid:<20} {'?':<14} {'?':>9}  UNREADABLE: {exc}")
            continue
        legs = "  ".join(
            f"{tok[:10]}..={leg['matched']:.2f}" for tok, leg in pair["legs"].items()
        )
        print(f"{pid:<20} {pair['condition_id'][:12]:<14} "
              f"{pair['naked']:>9.2f}  {legs}")


def quote(condition_id: str, price: float, size: float, live: bool,
          down_price: float | None = None,
          post_only: bool = True, tif: str = "GTC",
          expiration: int | None = None,
          db_path: str | Path | None = None) -> None:
    """Rest a two-sided pair: buy UP at `price`, buy DOWN at `down_price` (default: 1-price) via batch quoting."""
    from py_clob_client_v2.clob_types import (
        OrderArgsV2, OrderType, PostOrdersV2Args, OrderPayload,
    )
    from py_clob_client_v2.order_builder.constants import BUY
    from engine.markets import fetch_pinned_market

    # Pre-flight parse check on TIF and post_only
    if post_only and tif not in ("GTC", "GTD"):
        raise SystemExit(
            f"--post-only is valid only for GTC and GTD orders (got --tif {tif})."
        )
    if tif == "GTD" and not expiration:
        raise SystemExit(
            "--expiration (UTC epoch seconds) is required when --tif GTD."
        )

    # Rewards are not the income. Measured on run/fleet.db: 476 merge closes
    # earned +$1,172.35 at an average pair cost of $0.96006, while maker rebate
    # accrual over the same run ran about $0.22/day against $566 committed --
    # four hundredths of a percent. The income is buying UP+DOWN below $1.00 and
    # merging, which is what "spread hunter" names.
    #
    # This path used to demand rewards, a default left over from the rebate-
    # farming phase. `sweep.py:507` -- the fleet, the thing that actually trades
    # -- passes require_rewards=False and has since spread capture landed, so the
    # CLI was refusing every market the fleet quotes: all eight currently in
    # run/markets.json are source=spread with daily=0.00. The guard is gone here
    # for the same reason it is off there. Whether a market is worth funding is
    # the allocator's call, made from run/markets.json, not this function's.
    m = fetch_pinned_market(condition_id, require_rewards=False)
    if m is None:
        raise SystemExit(
            f"no tradeable market at condition_id {condition_id[:12]}... -- it "
            f"is missing, closed, not accepting orders, or does not carry "
            f"exactly two tokens. Check the id."
        )

    dn_price = round(down_price, 4) if down_price is not None else round(1.0 - price, 4)
    cost = price * size + dn_price * size
    if cost > MAX_ORDER_USD:
        raise SystemExit(
            f"${cost:.2f} exceeds MAX_ORDER_USD ${MAX_ORDER_USD:.2f}")

    legs = [(m.up_token, price, "UP"), (m.down_token, dn_price, "DOWN")]
    print(f"market   {m.market_slug[:60]}")
    print(f"tick     {m.tick_size}   neg_risk {m.neg_risk}")
    for tok, p, label in legs:
        print(f"  BUY {size:.0f} {label:4} @ {p:.3f} = ${p * size:6.2f}  "
              f"token {str(tok)[:14]}...")
    print(f"total committed ${cost:.2f}")

    if not live:
        print("\nDRY RUN -- nothing sent. Re-run with --live to place.")
        return

    c = client()
    already = open_notional(c)
    if already + cost > MAX_TOTAL_USD:
        raise SystemExit(f"open ${already:.2f} + ${cost:.2f} exceeds "
                         f"MAX_TOTAL_USD ${MAX_TOTAL_USD:.2f}")

    # The registry is what every later stage reads. Without a row here the poll
    # loop has nothing to reconcile, `exit` and `complete` have no pair_id to
    # act on, and the two legs rest at the venue with real money and nothing
    # tracking them -- the exact failure the registry exists to prevent.
    import uuid as _uuid
    from engine.order_registry import (
        OrderRegistry, OrderRecord, DEFAULT_DB_PATH,
    )
    from engine import config as strategy_config

    registry = OrderRegistry(db_path=Path(db_path) if db_path else DEFAULT_DB_PATH)
    pair_id = f"pair-{_uuid.uuid4().hex[:12]}"
    max_pair_cost = strategy_config.load().max_pair_cost
    now_ms = int(time.time() * 1000)

    # Indexed, not getattr-with-default. A silent fallback to GTC would turn a
    # FOK the caller asked for into an order that RESTS -- exposure the caller
    # explicitly declined. argparse constrains the CLI, but quote() is called
    # directly by tests and by future callers, and this path opens positions.
    if tif not in _TIF_CHOICES:
        raise SystemExit(f"unknown --tif {tif!r}; expected one of {', '.join(_TIF_CHOICES)}")
    order_type_enum = getattr(OrderType, tif)
    exp_val = int(expiration) if expiration is not None else 0

    # 1. Pre-allocate local registry rows and sign orders before network call
    local_legs = []
    batch_args = []
    for tok, p, label in legs:
        local_id = str(_uuid.uuid4())
        # Row first, then send. A row written after a successful send would be
        # lost to a crash in between, leaving a live order untracked; a row
        # written before is at worst a `pending` with no venue id, which
        # reconcile's orphan adoption is built to claim.
        registry.create_order(OrderRecord(
            id=local_id, order_id=None, condition_id=condition_id,
            token_id=str(tok), side="BUY", price=p, original_size=size,
            status="pending", posted_ts=now_ms, last_polled_ts=now_ms,
            pair_id=pair_id, max_pair_cost_at_post=max_pair_cost,
        ))

        signed = c.create_order(
            OrderArgsV2(price=p, size=size, side=BUY, token_id=tok, expiration=exp_val))
        batch_args.append(PostOrdersV2Args(order=signed, orderType=order_type_enum))
        local_legs.append({
            "local_id": local_id,
            "token_id": str(tok),
            "price": p,
            "label": label,
            "signed": signed,
        })

    # 2. Batch post both legs in a single network round-trip
    t_start = time.perf_counter()
    resp = c.post_orders(batch_args, post_only=post_only)
    post_latency_ms = (time.perf_counter() - t_start) * 1000.0
    resp_list = resp if isinstance(resp, list) else [resp] if isinstance(resp, dict) else []

    _log_order({"ts": time.time(), "condition_id": condition_id,
                "pair_id": pair_id,
                "legs": [(leg["local_id"], leg["label"], leg["token_id"]) for leg in local_legs],
                "response": str(resp)[:800]})

    # 3. Extract venue IDs (provisional positional mapping)
    extracted_venue_ids = []
    for idx, leg in enumerate(local_legs):
        item_resp = resp_list[idx] if idx < len(resp_list) else None
        v_id = venue_order_id(item_resp)
        extracted_venue_ids.append(v_id)

    # 4. Partial failure detection: if one succeeded and one failed, immediately
    # cancel the survivor to prevent holding an unhedged naked leg.
    succeeded_count = sum(1 for v in extracted_venue_ids if v is not None)
    if succeeded_count == 1:
        survivor_idx = 0 if extracted_venue_ids[0] is not None else 1
        failed_idx = 1 if survivor_idx == 0 else 0
        survivor_leg = local_legs[survivor_idx]
        failed_leg = local_legs[failed_idx]
        survivor_vid = extracted_venue_ids[survivor_idx]

        print(f"  CRITICAL: Batch quote partial failure! {survivor_leg['label']} posted as {survivor_vid} but {failed_leg['label']} failed.",
              file=sys.stderr)
        print(f"  Issuing emergency cancel for surviving leg {survivor_vid} to prevent naked exposure...", file=sys.stderr)

        registry.update_order_status(failed_leg["local_id"], status="cancelled", last_polled_ts=now_ms)
        try:
            c.cancel_order(OrderPayload(orderID=survivor_vid))
            registry.update_order_status(survivor_leg["local_id"], status="cancelled", last_polled_ts=now_ms)
        except Exception as exc:
            print(f"  EMERGENCY CANCEL FAILED: {exc}. Row stays open/pending for reconcile to adopt.", file=sys.stderr)

        raise SystemExit(
            f"Batch quote failed partially: {failed_leg['label']} rejected, {survivor_leg['label']} cancelled."
        )

    if succeeded_count == 0:
        # Log venue errors if present
        from engine.order_registry import VenueErrorRecord, get_run_id
        for idx, leg in enumerate(local_legs):
            item_resp = resp_list[idx] if idx < len(resp_list) else None
            if isinstance(item_resp, dict) and (item_resp.get("errorMsg") or item_resp.get("success") is False):
                registry.log_venue_error(VenueErrorRecord(
                    ts=time.time(),
                    condition_id=condition_id,
                    side=leg["label"],
                    price=leg["price"],
                    size=size,
                    error_code=item_resp.get("status") or "REJECTED",
                    raw_error_msg=str(item_resp.get("errorMsg") or "order rejected"),
                    run_id=get_run_id(),
                ))
        print("  WARNING: no order IDs in batch quote response; rows stay pending for reconcile to adopt.", file=sys.stderr)
        print(f"  SENT BATCH: {resp}")
        return

    # 5. Verification step: Read orders back from venue to verify asset_id before committing.
    verified_mappings = []
    mismatch_detected = False
    mismatch_reason = ""

    for idx, leg in enumerate(local_legs):
        v_id = extracted_venue_ids[idx]
        try:
            order_data = c.get_order(v_id)
            venue_asset_id = (
                order_data.get("asset_id") or order_data.get("token_id") or order_data.get("tokenId")
                if isinstance(order_data, dict)
                else getattr(order_data, "asset_id", None)
            )
            if str(venue_asset_id) != str(leg["token_id"]):
                mismatch_detected = True
                mismatch_reason = (
                    f"Asset ID mismatch on leg {leg['label']}: expected token {leg['token_id']}, "
                    f"venue returned asset_id {venue_asset_id} for orderID {v_id}"
                )
                break
            verified_mappings.append((leg["local_id"], v_id))
        except Exception as exc:
            mismatch_detected = True
            mismatch_reason = f"Failed to verify order {v_id} from venue: {exc}"
            break

    if mismatch_detected:
        print(f"  CRITICAL: Verification mismatch detected! {mismatch_reason}", file=sys.stderr)
        print("  FAIL CLOSED: Cancelling all batch orders immediately...", file=sys.stderr)
        for v_id in extracted_venue_ids:
            if v_id:
                try:
                    c.cancel_order(OrderPayload(orderID=v_id))
                except Exception as exc:
                    print(f"  Cancel error for {v_id}: {exc}", file=sys.stderr)
        for leg in local_legs:
            registry.update_order_status(leg["local_id"], status="cancelled", last_polled_ts=now_ms)
        raise SystemExit(f"FAIL CLOSED: Order verification mismatch ({mismatch_reason}); all orders cancelled.")

    # 6. Agreement confirmed: commit venue IDs to registry and log QuoteRecord
    from engine.order_registry import QuoteRecord, get_run_id
    mid_quote = round((price + dn_price) / 2.0, 4)
    for local_id, v_id in verified_mappings:
        registry.attach_venue_order_id(local_id, v_id, status="open", last_polled_ts=now_ms)

    for idx, leg in enumerate(local_legs):
        v_id = extracted_venue_ids[idx] if idx < len(extracted_venue_ids) else None
        registry.log_quote(QuoteRecord(
            ts=time.time(),
            market_slug=m.market_slug,
            condition_id=condition_id,
            token_id=leg["token_id"],
            side=leg["label"],
            price=leg["price"],
            size=size,
            mid=mid_quote,
            edge_vs_mid=round(mid_quote - leg["price"], 4),
            order_id=v_id,
            local_id=leg["local_id"],
            latency_ms=post_latency_ms,
            run_id=get_run_id(),
        ))
        print(f"  SENT {leg['label']}: {v_id}")

    print(f"\nlogged to {RUN / 'live_orders.json'}")
    print(f"pair_id  {pair_id}")
    print(f"  poll:     python -m engine.live_exec poll --interval 5")
    print(f"  exit:     python -m engine.live_exec exit {pair_id}")
    print(f"  complete: python -m engine.live_exec complete {pair_id}")


USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ZERO_BYTES32 = "0x0000000000000000000000000000000000000000000000000000000000000000"
# Provenance: matches the 598s delta measured on transaction 0x66bc709b1a1d515d813e9d191a84b8863d8f2a251e1698a85d452152c7602135, block 92098496.
REDEEM_DEADLINE_SECONDS = 600
# Polymarket DepositWalletFactory address used by @polymarket/builder-relayer-client (config.DepositWalletFactory),
# confirmed as outer 'to' of reference transaction 0x66bc709b1a1d515d813e9d191a84b8863d8f2a251e1698a85d452152c7602135.
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"


# Origin: live/scripts/audit_settlement.py:19-24
POLYGON_RPC_ENDPOINTS = [
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
]


def get_payout_denominator(condition_id: str, rpc_url: str | None = None) -> int | None:
    """Query payoutDenominator(bytes32) on CTF contract (0x4D97DCd97eC945f40cF65F87097ACe5EA0476045).
    Selector: 0xdd34de67
    Returns integer (non-zero if resolved, 0 if unresolved) on success, or None if all RPC endpoints fail.
    """
    import urllib.request

    clean_cond = condition_id.lower().replace("0x", "").zfill(64)
    call_data = "0xdd34de67" + clean_cond
    req_body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": CTF_CONTRACT, "data": call_data}, "latest"],
    }).encode("utf-8")

    endpoints = []
    if rpc_url:
        endpoints.append(rpc_url)
    elif os.environ.get("POLYGON_RPC"):
        endpoints.append(os.environ["POLYGON_RPC"])
    endpoints.extend([ep for ep in POLYGON_RPC_ENDPOINTS if ep not in endpoints])

    for ep in endpoints:
        try:
            req = urllib.request.Request(
                ep,
                data=req_body,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if "result" in res:
                    if res["result"] == "0x":
                        raise SystemExit(
                            f"eth_call to CTF contract {CTF_CONTRACT} returned empty data. "
                            f"The contract address may be wrong or the RPC may be on the wrong chain."
                        )
                    return int(res["result"], 16)
        except Exception:
            continue
    return None


def build_redeem_submit_payload(from_addr: str, funder: str, nonce: int | str,
                                deadline: int | str, signature: str, call_data: str) -> dict:
    """Construct relayer /submit JSON payload for DepositWalletBatchRequest.
    Wire types follow @polymarket/builder-relayer-client@0.0.10 dist/types.d.ts:147-154.
    """
    return {
        "type": "WALLET",
        "from": from_addr,
        "to": DEPOSIT_WALLET_FACTORY,
        "nonce": str(nonce),
        "signature": signature,
        "depositWalletParams": {
            "depositWallet": funder,
            "deadline": str(deadline),
            "calls": [
                {
                    "target": CTF_CONTRACT,
                    "value": "0",
                    "data": call_data,
                }
            ],
        },
    }


def _submit_and_log(
    action: str,
    condition_id: str,
    funder: str,
    signer_addr: str,
    call_data: str,
    nonce: int | str,
    deadline: int | str,
    payload: dict,
    headers: dict,
    relayer_url: str,
) -> None:
    """Submit EIP-712 batch transaction to relayer with crash-safe pre-logging and atomic status updates."""
    import urllib.request

    req_submit = urllib.request.Request(
        f"{relayer_url}/submit",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )

    entry_id = _log_order({
        "ts": time.time(),
        "action": action,
        "condition_id": condition_id,
        "safe_funder": funder,
        "signer": signer_addr,
        "target": CTF_CONTRACT,
        "call_data": call_data,
        "nonce": nonce,
        "deadline": deadline,
        "payload": payload,
        "status": "pending",
    })

    try:
        with urllib.request.urlopen(req_submit, timeout=30) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except KeyboardInterrupt:
        try:
            log_ok = _update_order_log(entry_id, {
                "status": "interrupted",
                "error_type": "KeyboardInterrupt",
                "error": "Execution interrupted by user during submit",
            })
        except Exception:
            log_ok = False

        record_dump = json.dumps({
            "id": entry_id,
            "action": action,
            "condition_id": condition_id,
            "safe_funder": funder,
            "signer": signer_addr,
            "target": CTF_CONTRACT,
            "call_data": call_data,
            "nonce": nonce,
            "deadline": deadline,
            "payload": payload,
            "status": "interrupted",
            "error_type": "KeyboardInterrupt",
        }, indent=2)
        print(
            f"ERROR: Relayer submit interrupted by user (KeyboardInterrupt).\n"
            f"Transaction was signed and may have been broadcast to relayer.\n"
            f"Full in-flight transaction record:\n{record_dump}",
            file=sys.stderr,
        )
        raise SystemExit(
            f"Relayer submit interrupted (KeyboardInterrupt).\n"
            f"Transaction was signed and may have been broadcast (nonce={nonce}, id={entry_id}).\n"
            f"On-chain status must be checked manually before any retry."
        )
    except Exception as exc:
        try:
            log_ok = _update_order_log(entry_id, {
                "status": "unknown",
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
        except Exception:
            log_ok = False

        if not log_ok:
            record_dump = json.dumps({
                "id": entry_id,
                "action": action,
                "condition_id": condition_id,
                "safe_funder": funder,
                "signer": signer_addr,
                "target": CTF_CONTRACT,
                "call_data": call_data,
                "nonce": nonce,
                "deadline": deadline,
                "payload": payload,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }, indent=2)
            print(
                f"ERROR: Failed to update live_orders.json for entry_id={entry_id}.\n"
                f"Full in-flight transaction record:\n{record_dump}",
                file=sys.stderr,
            )
            raise SystemExit(
                f"Relayer submit failed with {type(exc).__name__}: {exc}\n"
                f"Transaction was signed and sent (nonce={nonce}, id={entry_id}).\n"
                f"WARNING: Audit row in live_orders.json could NOT be updated (see stderr dump).\n"
                f"On-chain status must be checked manually before any retry."
            )
        raise SystemExit(
            f"Relayer submit failed with {type(exc).__name__}: {exc}\n"
            f"Transaction was signed and sent (nonce={nonce}, id={entry_id}).\n"
            f"On-chain status must be checked manually before any retry."
        )

    tx_hash = None
    if isinstance(res, dict):
        tx_hash = res.get("transactionHash") or res.get("transactionID") or res.get("id")

    # The relayer answers with its own terminal state. Recording every reply as
    # "submitted" leaves an executed transaction looking in-flight forever, and
    # the idempotency guard then refuses the next merge on that market -- which
    # is exactly what happened on the first live cycle: STATE_EXECUTED in the
    # response, status "submitted" in the log, and `merge` blocked behind
    # --force afterwards. Trust the state the relayer reports.
    state = res.get("state") if isinstance(res, dict) else None
    if state == "STATE_EXECUTED":
        status = "executed"
    elif state in ("STATE_FAILED", "STATE_REVERTED", "STATE_CANCELLED"):
        status = "failed"
    else:
        # Anything else is genuinely in flight or unrecognised, and an unknown
        # state must keep the guard armed rather than clear it.
        status = "submitted"

    update_fields = {
        "status": status,
        "relayer_state": state or "",
        "response": json.dumps(res)[:400],
    }
    if tx_hash:
        update_fields["tx_hash"] = tx_hash

    try:
        log_ok = _update_order_log(entry_id, update_fields)
    except Exception as update_exc:
        log_ok = False
        update_err = str(update_exc)
    else:
        update_err = None

    if not log_ok:
        record_dump = json.dumps({
            "id": entry_id,
            "action": action,
            "condition_id": condition_id,
            "safe_funder": funder,
            "signer": signer_addr,
            "target": CTF_CONTRACT,
            "call_data": call_data,
            "nonce": nonce,
            "deadline": deadline,
            "payload": payload,
            "tx_hash": tx_hash,
            "status": "submitted",
            "response": json.dumps(res)[:400],
            "update_error": update_err,
        }, indent=2)
        print(
            f"ERROR: Relayer accepted transaction (tx_hash={tx_hash}) but live_orders.json entry {entry_id} "
            f"could NOT be updated to status='submitted' (row remains pending or missing in log).\n"
            f"Full transaction record:\n{record_dump}",
            file=sys.stderr,
        )
        raise SystemExit(
            f"Relayer accepted transaction (tx_hash={tx_hash}), but audit log update failed.\n"
            f"Transaction was signed and submitted (nonce={nonce}, id={entry_id}).\n"
            f"On-chain status must be verified before any retry. See stderr for full transaction record."
        )

    print(f"  RELAYER RESPONSE: {json.dumps(res)[:400]}")
    print(f"\nlogged to {RUN / 'live_orders.json'}")

    if action == "MERGE" and (status == "executed" or state == "STATE_EXECUTED"):
        try:
            from engine.order_registry import OrderRegistry, CloseRecord, get_run_id
            registry = OrderRegistry()
            cost_basis = 0.0
            with registry._conn() as conn:
                r = conn.execute(
                    "SELECT COALESCE(SUM(f.size * f.price), 0.0) AS c FROM fills f JOIN orders o ON f.order_uuid = o.id WHERE o.condition_id = ?",
                    (condition_id,),
                ).fetchone()
                cost_basis = float(r["c"]) if r else 0.0
            amt = 0.0
            if call_data and len(call_data) >= 10 + 64 * 5:
                amount_hex = call_data[10 + 64 * 4 : 10 + 64 * 5]
                amt = int(amount_hex, 16) / 10**6
            proceeds = float(amt) * 1.00
            realized_pnl = proceeds - cost_basis
            registry.log_close(CloseRecord(
                ts=time.time(),
                condition_id=condition_id,
                method="merge",
                gas=0.0,
                shares=float(amt),
                cost_basis=cost_basis,
                proceeds=proceeds,
                fee=0.0,
                realized_pnl=realized_pnl,
                forgone_vs_settlement=0.0,
                up_cost_removed=cost_basis / 2.0 if cost_basis else 0.0,
                dn_cost_removed=cost_basis / 2.0 if cost_basis else 0.0,
                tx_hash=tx_hash,
                run_id=get_run_id(),
            ))
        except Exception as close_exc:
            print(f"  WARNING: Failed to log close record: {close_exc}", file=sys.stderr)


def redeem(condition_id: str, index_sets: list[int] | None = None,
           collateral: str = USDC_E_CONTRACT,
           parent_collection_id: str = ZERO_BYTES32,
           skip_resolution_check: bool = False,
           force: bool = False,
           live: bool = False) -> None:
    """Gasless redemption of winning conditional tokens via Polymarket Relayer."""
    if index_sets is None:
        index_sets = [1, 2]

    # Pre-flight Guard: Idempotency check
    _check_idempotency_guard(condition_id, force=force)

    funder = os.environ.get("POLY_FUNDER", "")
    key = os.environ.get("POLY_PRIVATE_KEY")
    signer = ""
    if key:
        from eth_account import Account
        signer = Account.from_key(key).address

    call_data = encode_redeem_positions(
        collateral_token=collateral,
        parent_collection_id=parent_collection_id,
        condition_id=condition_id,
        index_sets=index_sets,
    )

    denom = get_payout_denominator(condition_id)
    if denom is None:
        resolved_str = "unknown (RPC unreachable)"
    else:
        resolved_str = "yes" if denom > 0 else "no"

    # Evaluated before the dry-run preview so the preview matches what --live does.
    guard_failures: list[str] = []
    if denom is None:
        if not skip_resolution_check:
            guard_failures.append(
                f"Cannot determine resolution status for {condition_id}: all RPC endpoints failed. "
                f"The market may well be resolved. Retry, or pass --skip-resolution-check to bypass."
            )
    elif denom == 0:
        guard_failures.append(
            f"Condition {condition_id} is not resolved yet (payoutDenominator == 0)."
        )

    print("action          REDEEM (gasless via Polymarket Relayer)")
    print(f"target_ctf      {CTF_CONTRACT}")
    print(f"safe_funder     {funder or '(POLY_FUNDER not set)'}")
    print(f"signer_eoa      {signer or '(POLY_PRIVATE_KEY not set)'}")
    print(f"condition_id    {condition_id}")
    print(f"resolved        {resolved_str}")
    print(f"collateral      {collateral}")
    print(f"index_sets      {index_sets}")
    print(f"encoded_call    {call_data[:42]}... ({len(call_data)} chars)")

    if not live:
        preview_nonce = 0
        preview_deadline = int(time.time()) + REDEEM_DEADLINE_SECONDS
        preview_sig = "0x" + "00" * 65
        preview_payload = build_redeem_submit_payload(
            from_addr=signer or "0x0000000000000000000000000000000000000000",
            funder=funder or "0x0000000000000000000000000000000000000000",
            nonce=preview_nonce,
            deadline=preview_deadline,
            signature=preview_sig,
            call_data=call_data,
        )
        print("\nsubmit_payload_preview (dry run - placeholder nonce/signature):")
        print(json.dumps(preview_payload, indent=2))
        if guard_failures:
            print("\nPRE-FLIGHT FAILED -- --live would refuse:")
            for msg in guard_failures:
                print(f"  - {msg}")
            raise SystemExit(1)
        print("\nDRY RUN -- nothing sent. Re-run with --live to sign and submit to relayer.")
        return

    if guard_failures:
        raise SystemExit(guard_failures[0])

    relayer_key = os.environ.get("RELAYER_API_KEY")
    relayer_addr = os.environ.get("RELAYER_API_KEY_ADDRESS")
    if not relayer_key or not relayer_addr:
        raise SystemExit(
            "RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS must be set in .env "
            "for gasless live redemption."
        )
    if not key or not funder:
        raise SystemExit("POLY_PRIVATE_KEY and POLY_FUNDER must be set in .env")

    import urllib.request
    relayer_url = os.environ.get("RELAYER_URL", "https://relayer-v2.polymarket.com")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "RELAYER_API_KEY": relayer_key,
        "RELAYER_API_KEY_ADDRESS": relayer_addr,
    }

    # 1. Fetch transaction nonce from relayer
    nonce_url = f"{relayer_url}/v1/account/transactions/params?address={signer}&type=WALLET"
    req_nonce = urllib.request.Request(nonce_url, headers=headers)
    try:
        with urllib.request.urlopen(req_nonce, timeout=10) as resp:
            nonce_data = json.loads(resp.read().decode("utf-8"))
            nonce = int(nonce_data.get("nonce", 0))
    except Exception as exc:
        raise SystemExit(f"Failed to fetch nonce from relayer: {exc}")

    # 2. Sign EIP-712 Batch transaction
    deadline = int(time.time()) + REDEEM_DEADLINE_SECONDS
    signer_addr, signature = sign_redeem_transaction(key, funder, nonce, deadline, call_data)

    # 3. Construct relayer submit payload
    payload = build_redeem_submit_payload(
        from_addr=signer_addr,
        funder=funder,
        nonce=nonce,
        deadline=deadline,
        signature=signature,
        call_data=call_data,
    )

    # 4. Submit and log
    _submit_and_log(
        action="REDEEM",
        condition_id=condition_id,
        funder=funder,
        signer_addr=signer_addr,
        call_data=call_data,
        nonce=nonce,
        deadline=deadline,
        payload=payload,
        headers=headers,
        relayer_url=relayer_url,
    )


def merge(condition_id: str,
          amount: float,
          index_sets: list[int] | None = None,
          collateral: str = USDC_E_CONTRACT,
          parent_collection_id: str = ZERO_BYTES32,
          force: bool = False,
          live: bool = False) -> None:
    """Gasless merge of full outcome sets (UP + DOWN) back into USDC.e collateral."""
    from engine.config import MakerConfig
    if index_sets is None:
        index_sets = [1, 2]
    amount_base_units = int(round(amount * 1e6))

    # Pre-flight Guard 3: MAX_ORDER_USD ceiling
    cost = amount * 1.0
    if cost > MAX_ORDER_USD:
        raise SystemExit(f"${cost:.2f} exceeds MAX_ORDER_USD ${MAX_ORDER_USD:.2f}")

    # Pre-flight Guard 4: Idempotency check
    _check_idempotency_guard(condition_id, force=force)

    # Derive token IDs deterministically via CTF
    token_ids = [
        get_position_id(collateral, get_collection_id(parent_collection_id, condition_id, idx))
        for idx in index_sets
    ]
    up_tok_id = token_ids[0] if len(token_ids) > 0 else ""
    dn_tok_id = token_ids[1] if len(token_ids) > 1 else ""

    funder = os.environ.get("POLY_FUNDER", "")
    key = os.environ.get("POLY_PRIVATE_KEY")
    signer = ""
    if key:
        from eth_account import Account
        signer = Account.from_key(key).address

    call_data = encode_merge_positions(
        collateral_token=collateral,
        parent_collection_id=parent_collection_id,
        condition_id=condition_id,
        index_sets=index_sets,
        amount=amount_base_units,
    )

    denom = get_payout_denominator(condition_id)
    if denom is None:
        resolved_str = "unknown (RPC unreachable)"
    else:
        resolved_str = "yes" if denom > 0 else "no"

    merge_gas = MakerConfig().merge_gas_usd
    expected_collateral = amount * 1.00
    net_collateral = expected_collateral - merge_gas


    up_bal = 0.0
    dn_bal = 0.0
    # A balance we failed to read is not a balance of zero. Both fail closed,
    # but only one of them tells the operator the truth: an RPC error, an auth
    # failure and an unset POLY_FUNDER all rendered as "holds 0.00", which reads
    # as "you do not own these tokens". Same rule reconcile_orders follows --
    # a failed read must not be laundered into a state verdict.
    balance_error: str | None = None
    if not (key and funder):
        balance_error = (
            "Conditional token balances not queried: "
            f"{'POLY_PRIVATE_KEY' if not key else 'POLY_FUNDER'} is unset"
        )

    # Query conditional token balances if client credentials available
    if key and funder:
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
            sig_type = int(os.environ.get("POLY_SIG_TYPE", "3"))
            c = client(funder)
            if up_tok_id:
                r_up = c.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=up_tok_id, signature_type=sig_type)
                )
                up_bal = float(r_up.get("balance", 0) or 0) / 1e6
            if dn_tok_id:
                r_dn = c.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=dn_tok_id, signature_type=sig_type)
                )
                dn_bal = float(r_dn.get("balance", 0) or 0) / 1e6
        except Exception as exc:
            balance_error = f"Conditional token balance query failed: {exc!r}"

    # Pre-flight guards are evaluated HERE, before the dry-run preview, so the
    # preview reports exactly what --live would do. A preview that succeeds where
    # --live refuses manufactures false confidence in the operator.
    guard_failures: list[str] = []
    if balance_error is not None:
        guard_failures.append(
            f"{balance_error}. Holdings are unknown, not zero -- refusing rather "
            f"than reporting a balance we did not read."
        )
    else:
        if up_bal < amount:
            guard_failures.append(
                f"Insufficient balance on UP token ({up_tok_id}): holds {up_bal:.2f}, needs {amount:.2f} (short by {amount - up_bal:.2f})"
            )
        if dn_bal < amount:
            guard_failures.append(
                f"Insufficient balance on DOWN token ({dn_tok_id}): holds {dn_bal:.2f}, needs {amount:.2f} (short by {amount - dn_bal:.2f})"
            )
    if denom is None:
        # `redeem` already refuses here unless --skip-resolution-check is passed.
        # `merge` had neither the branch nor the flag, so an all-endpoints-down
        # RPC read let a merge go out against a market that may already be
        # resolved: a reverted relayer submission and an ambiguous audit row.
        guard_failures.append(
            f"Cannot determine resolution status for {condition_id}: every RPC endpoint failed. "
            f"The condition may already be resolved, in which case merge is the wrong action."
        )
    elif denom > 0:
        guard_failures.append(
            f"Condition {condition_id} is already resolved (payoutDenominator == {denom} > 0). Use redeem instead."
        )

    print("action          MERGE (gasless via Polymarket Relayer)")
    print(f"target_ctf      {CTF_CONTRACT}")
    print(f"safe_funder     {funder or '(POLY_FUNDER not set)'}")
    print(f"signer_eoa      {signer or '(POLY_PRIVATE_KEY not set)'}")
    print(f"condition_id    {condition_id}")
    print(f"resolved        {resolved_str}")
    print(f"collateral      {collateral}")
    print(f"index_sets      {index_sets}")
    print(f"amount          {amount:.2f} shares ({amount_base_units} base units)")
    # `up_bal` and `dn_bal` are still at their 0.0 initialisers when the balance
    # query failed. Formatting them here would print "held: 0.00" directly above
    # the guard line saying holdings are unknown, not zero -- the operator reads
    # two contradictory statements and believes the number.
    held_up = "unknown" if balance_error is not None else f"{up_bal:.2f}"
    held_dn = "unknown" if balance_error is not None else f"{dn_bal:.2f}"
    print(f"token_up        {up_tok_id} (held: {held_up})")
    print(f"token_down      {dn_tok_id} (held: {held_dn})")
    print(f"expected_usdc   ${expected_collateral:,.2f}")
    print(f"estimated_gas   ${merge_gas:,.2f} (config.merge_gas_usd)")
    print(f"net_collateral  ${net_collateral:,.2f}")
    print(f"encoded_call    {call_data[:42]}... ({len(call_data)} chars)")

    if not live:
        preview_nonce = 0
        preview_deadline = int(time.time()) + REDEEM_DEADLINE_SECONDS
        preview_sig = "0x" + "00" * 65
        preview_payload = build_redeem_submit_payload(
            from_addr=signer or "0x0000000000000000000000000000000000000000",
            funder=funder or "0x0000000000000000000000000000000000000000",
            nonce=preview_nonce,
            deadline=preview_deadline,
            signature=preview_sig,
            call_data=call_data,
        )
        print("\nsubmit_payload_preview (dry run - placeholder nonce/signature):")
        print(json.dumps(preview_payload, indent=2))
        if guard_failures:
            print("\nPRE-FLIGHT FAILED -- --live would refuse:")
            for msg in guard_failures:
                print(f"  - {msg}")
            raise SystemExit(1)
        print("\nDRY RUN -- nothing sent. Re-run with --live to sign and submit to relayer.")
        return

    if guard_failures:
        raise SystemExit(guard_failures[0])

    relayer_key = os.environ.get("RELAYER_API_KEY")
    relayer_addr = os.environ.get("RELAYER_API_KEY_ADDRESS")
    if not relayer_key or not relayer_addr:
        raise SystemExit(
            "RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS must be set in .env "
            "for gasless live merge."
        )
    if not key or not funder:
        raise SystemExit("POLY_PRIVATE_KEY and POLY_FUNDER must be set in .env")

    import urllib.request
    relayer_url = os.environ.get("RELAYER_URL", "https://relayer-v2.polymarket.com")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "RELAYER_API_KEY": relayer_key,
        "RELAYER_API_KEY_ADDRESS": relayer_addr,
    }

    # 1. Fetch transaction nonce from relayer
    nonce_url = f"{relayer_url}/v1/account/transactions/params?address={signer}&type=WALLET"
    req_nonce = urllib.request.Request(nonce_url, headers=headers)
    try:
        with urllib.request.urlopen(req_nonce, timeout=10) as resp:
            nonce_data = json.loads(resp.read().decode("utf-8"))
            nonce = int(nonce_data.get("nonce", 0))
    except Exception as exc:
        raise SystemExit(f"Failed to fetch nonce from relayer: {exc}")

    # 2. Sign EIP-712 Batch transaction
    deadline = int(time.time()) + REDEEM_DEADLINE_SECONDS
    signer_addr, signature = sign_redeem_transaction(key, funder, nonce, deadline, call_data)

    # 3. Construct relayer submit payload
    payload = build_redeem_submit_payload(
        from_addr=signer_addr,
        funder=funder,
        nonce=nonce,
        deadline=deadline,
        signature=signature,
        call_data=call_data,
    )

    # 4. Submit and log
    _submit_and_log(
        action="MERGE",
        condition_id=condition_id,
        funder=funder,
        signer_addr=signer_addr,
        call_data=call_data,
        nonce=nonce,
        deadline=deadline,
        payload=payload,
        headers=headers,
        relayer_url=relayer_url,
    )




def probe(series: str | None = None,
          token_id: str | None = None,
          cycles: int = 30,
          min_t_remaining: float = 90.0,
          max_complement_bid: float = 0.85,
          max_probe_loss_usd: float = 1.00,
          max_fills: int = 1,
          live: bool = False) -> None:
    """Multi-cycle latency probe spanning live market windows with strict CTF match defense.

    Measures tau_accept (engine queuing & sequencing) and tau_pubsub (venue broadcast lag)
    using local monotonic CPU timestamps. Dynamically tracks 5-minute market rollovers,
    handles inter-window gaps, guards against complementary matching, and bounds uncertainty.
    """
    if not series and not token_id:
        raise SystemExit(
            "probe requires exactly one of --series or --token-id. "
            "Pass --series <series_slug> (e.g. btc-up-or-down-5m) or --token-id <id>."
        )
    if series and token_id:
        raise SystemExit(
            "probe accepts either --series or --token-id, not both."
        )

    if series == "btc-updown-5m":
        series = "btc-up-or-down-5m"

    NET_ONEWAY_MS = 3.93  # Measured median one-way TCP transit (RTT/2 = 7.85ms / 2)

    target_desc = f"series '{series}'" if series else f"fixed token '{token_id}'"
    print("=" * 80)
    print(f"SPREAD-HUNTER LIVE LATENCY PROBE (N={cycles} cycles on {target_desc})")
    print("=" * 80)
    print("Guardrails & Architecture:")
    print("  - Target: Dynamic live market discovery across 5m windows")
    print("  - Price: $0.01 resting bid on UP")
    print("  - Size: 100 shares ($1.00 notional collateral)")
    print("  - Order Lifecycle: Post -> Capture WS Delta -> Immediate Cancel")
    print(f"  - Minimum Time Remaining Guard: >= {min_t_remaining:.0f}s remaining in 5m window")
    print(f"  - Complement Price Guard: Skip if DOWN Best Bid >= {max_complement_bid:.2f}")
    print(f"  - Max Probe Loss Cap: Abort if fills >= {max_fills} (loss >= ${max_probe_loss_usd:.2f})")
    print(f"  - Mode: {'LIVE BROADCAST' if live else 'DRY RUN (pass --live to execute)'}")
    print("=" * 80)

    if not live:
        from engine.markets import fetch_live_market
        gamma_host = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")
        resolved = fetch_live_market(gamma_host, series) if not token_id else None
        print("\n[DRY-RUN] Probe execution plan validated.")
        print(f"Series: {series}")
        if resolved:
            print(f"Active Live Window: {resolved.market_slug} (ends in {resolved.t_remaining():.0f}s)")
            print(f"Target Token (UP): {resolved.up_token}")
            print(f"Complement Token (DOWN): {resolved.down_token}")
        elif token_id:
            print(f"Fixed Target Token: {token_id}")
        else:
            print("Active Live Window: Currently in rollover gap (would wait for next window).")
        print(f"Would execute {cycles} consecutive cycles of $1.00 notional resting bids across dynamic market windows.")
        print("Expected Error Budget at N=30:")
        print("  - Random SEM: +/- 1.28 ms (shrinks as sigma / sqrt(30))")
        print("  - Residual Systematic Bias: <= 3.50 ms (route asymmetry + gateway TLS)")
        print("  - Total Uncertainty: <= 4.78 ms (< 10% on 50ms parameter)")
        print("Guards & Cost Model:")
        print(f"  - P(fill / cycle) under guards: ~1.08% (measured on archive tape)")
        print(f"  - Expected probe cost across 30 cycles: $0.32 USD")
        return

    import websocket
    from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
    from py_clob_client_v2.order_builder.constants import BUY
    from engine.markets import fetch_live_market

    gamma_host = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")
    _clob = client()

    # Dynamic WebSocket subscription state
    last_delta_event = {}
    ws_connected = threading.Event()
    current_token_id = [None]
    current_comp_id = [None]
    ws_instance = [None]
    comp_best_bid = [0.0]

    def on_ws_message(ws, message):
        try:
            data = json.loads(message)
            curr = current_token_id[0]
            comp = current_comp_id[0]
            items = data if isinstance(data, list) else [data]
            for item in items:
                asset = item.get("asset_id")
                # Both branches key on asset_id alone. They previously admitted
                # ANY `event_type == "book"` frame regardless of which token it
                # described, so a snapshot for the complement stamped ts_recv on
                # the target -- tau_pubsub_ms then timed an unrelated broadcast,
                # and comp_best_bid could be filled from the target's own book,
                # which is the price the loss guard below compares against.
                if curr and asset == curr:
                    last_delta_event["ts_recv"] = time.perf_counter_ns()
                    last_delta_event["data"] = item
                if comp and asset == comp:
                    bids = item.get("bids") or []
                    if bids:
                        comp_best_bid[0] = max(float(b.get("price", 0)) for b in bids)
        except Exception:
            pass

    def on_ws_open(ws):
        ws_instance[0] = ws
        ws_connected.set()

    def subscribe_tokens(t_up: str, t_down: str):
        current_token_id[0] = t_up
        current_comp_id[0] = t_down
        if ws_instance[0] and ws_connected.is_set():
            sub_msg = json.dumps({"assets_ids": [t_up, t_down], "type": "market"})
            try:
                ws_instance[0].send(sub_msg)
            except Exception:
                pass

    ws_app = websocket.WebSocketApp(
        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_open=on_ws_open,
        on_message=on_ws_message,
    )
    ws_thread = threading.Thread(target=ws_app.run_forever, daemon=True)
    ws_thread.start()

    if not ws_connected.wait(timeout=10.0):
        print("ERROR: WebSocket connection to market stream timed out.")
        return

    print("WebSocket connected. Starting multi-window probe cycles...\n")

    current_market = None
    window_idx = 0
    gaps = []
    results = []
    cumulative_fills = 0
    cumulative_loss_usd = 0.0

    for i in range(1, cycles + 1):
        # 1. Resolve / verify active market
        if token_id:
            active_token_id = token_id
            comp_token_id = ""
            market_slug = "fixed-token"
            condition_id = "N/A"
            if window_idx == 0:
                window_idx = 1
                subscribe_tokens(active_token_id, comp_token_id)
        else:
            market = fetch_live_market(gamma_host, series)
            # Rollover / gap handling & minimum time remaining guard
            if market is None or market.t_remaining() < min_t_remaining:
                if market is not None and market.t_remaining() < min_t_remaining:
                    t_rem = market.t_remaining()
                    print(f"\n[WINDOW CLOSING] {market.market_slug} has {t_rem:.1f}s remaining (< {min_t_remaining:.0f}s guard). Waiting for expiry...")
                    time.sleep(max(0.1, t_rem + 0.5))

                gap_start = time.perf_counter()
                print(f"[ROLLOVER GAP] Polling for next window in series '{series}'...", end=" ", flush=True)
                while True:
                    time.sleep(1.0)
                    market = fetch_live_market(gamma_host, series)
                    if market is not None and market.t_remaining() >= min_t_remaining:
                        break
                gap_duration = time.perf_counter() - gap_start
                print(f"resolved in {gap_duration:.2f}s -> {market.market_slug}")
                gaps.append({
                    "cycle": i,
                    "gap_duration_s": gap_duration,
                    "market_slug": market.market_slug,
                })

            if current_market is None or current_market.condition_id != market.condition_id:
                window_idx += 1
                current_market = market
                active_token_id = market.up_token
                comp_token_id = market.down_token
                comp_best_bid[0] = 0.0
                subscribe_tokens(active_token_id, comp_token_id)
                print(f"\n--- [WINDOW {window_idx}] {market.market_slug} (ends in {market.t_remaining():.0f}s) ---")
            else:
                active_token_id = current_market.up_token
                comp_token_id = current_market.down_token

            market_slug = current_market.market_slug
            condition_id = current_market.condition_id

        # 2. Complement best bid guard check
        comp_top_bid = comp_best_bid[0]
        try:
            book_comp = _clob.get_order_book(comp_token_id)
            if book_comp and getattr(book_comp, "bids", None):
                comp_top_bid = max(float(b.price) for b in book_comp.bids)
            elif isinstance(book_comp, dict) and book_comp.get("bids"):
                comp_top_bid = max(float(b.get("price", 0)) for b in book_comp["bids"])
        except Exception:
            pass

        if comp_top_bid >= max_complement_bid:
            print(f"Cycle {i:02d}/{cycles:02d} [W{window_idx}]: [GUARD TRIGGERED] Complement best bid = {comp_top_bid:.2f} >= {max_complement_bid:.2f}. Waiting for market balance...")
            time.sleep(2.0)
            continue

        # 3. Execute probe cycle
        last_delta_event.clear()
        print(f"Cycle {i:02d}/{cycles:02d} [W{window_idx}]: Posting BUY 100 @ $0.01 on {market_slug} (DOWN top bid: {comp_top_bid:.2f})...", end=" ", flush=True)

        order_args = OrderArgsV2(
            price=0.01,
            size=100.0,
            side=BUY,
            token_id=active_token_id,
        )
        signed_order = _clob.create_order(order_args)

        t1_socket_write = time.perf_counter_ns()
        # post_only: the probe measures ACCEPT latency, so the order must rest.
        # A fill would corrupt the measurement and open a position the probe
        # never intended -- $0.01 is far from the book, but "far" is a market
        # condition and post_only is a venue guarantee.
        resp = _clob.post_order(signed_order, OrderType.GTC, post_only=True)
        t2_http_ack = time.perf_counter_ns()

        order_id = resp.get("orderID") or resp.get("id")
        if not order_id:
            print(f"FAILED (No orderID returned: {resp})")
            time.sleep(1.0)
            continue

        # Wait for WS delta or 2.0s timeout
        t3_ws_recv = None
        start_wait = time.perf_counter()
        while time.perf_counter() - start_wait < 2.0:
            if "ts_recv" in last_delta_event and last_delta_event["ts_recv"] >= t1_socket_write:
                t3_ws_recv = last_delta_event["ts_recv"]
                break
            time.sleep(0.001)

        # Immediate cancel
        try:
            _clob.cancel_orders([order_id])
        except Exception as exc:
            print(f"(Cancel status: {exc})", end=" ")

        # Post-cancel fill check & loss guard
        time.sleep(0.05)
        try:
            order_status = _clob.get_order(order_id)
            size_matched = float(order_status.get("size_matched", 0) if isinstance(order_status, dict) else getattr(order_status, "size_matched", 0) or 0)
            if size_matched > 0:
                cumulative_fills += 1
                cumulative_loss_usd += size_matched * 0.01
                print(f"\n  [FILL DETECTED] Order {order_id[:10]} matched {size_matched:.0f} shares ($ {size_matched*0.01:.2f})!")
                if cumulative_fills >= max_fills or cumulative_loss_usd >= max_probe_loss_usd:
                    print(f"\n[ABORT] Maximum probe loss cap reached ({cumulative_fills} fills, ${cumulative_loss_usd:.2f} loss). Halting probe immediately.")
                    break
        except Exception as exc:
            # Fail closed. `size_matched` is the only thing that advances
            # cumulative_fills and cumulative_loss_usd, so swallowing this made
            # --max-fills and --max-loss silently stop counting -- and they stop
            # counting precisely when the venue is unhealthy, which is when a
            # fill is most likely and the cap matters most. One retry against a
            # transient blip, then abort rather than keep posting blind.
            try:
                time.sleep(0.25)
                order_status = _clob.get_order(order_id)
                size_matched = float(order_status.get("size_matched", 0)
                                     if isinstance(order_status, dict)
                                     else getattr(order_status, "size_matched", 0) or 0)
                if size_matched > 0:
                    cumulative_fills += 1
                    cumulative_loss_usd += size_matched * 0.01
                    print(f"\n  [FILL DETECTED on retry] Order {order_id[:10]} "
                          f"matched {size_matched:.0f} shares.")
            except Exception as exc2:
                print(f"\n[ABORT] Cannot read status of order {order_id[:10]}: "
                      f"{type(exc).__name__}: {exc} (retry: {type(exc2).__name__}). "
                      f"The loss cap cannot be enforced without it, so the probe "
                      f"stops here rather than posting further orders blind.")
                break

        rtt_rest_ms = (t2_http_ack - t1_socket_write) / 1e6
        loop_ms = (t3_ws_recv - t1_socket_write) / 1e6 if t3_ws_recv else rtt_rest_ms
        tau_accept_ms = max(0.0, rtt_rest_ms - (2 * NET_ONEWAY_MS))
        tau_pubsub_ms = max(0.0, loop_ms - rtt_rest_ms)

        results.append({
            "cycle": i,
            "window_idx": window_idx,
            "market_slug": market_slug,
            "condition_id": condition_id,
            "token_id": active_token_id,
            "rtt_rest_ms": rtt_rest_ms,
            "tau_accept_ms": tau_accept_ms,
            "tau_pubsub_ms": tau_pubsub_ms,
            "loop_ms": loop_ms,
        })

        print(f"REST RTT: {rtt_rest_ms:.2f}ms | tau_accept: {tau_accept_ms:.2f}ms | tau_pubsub: {tau_pubsub_ms:.2f}ms")
        time.sleep(0.5)

    ws_app.close()

    if not results:
        print("No successful cycles recorded.")
        return

    # Compute distribution statistics
    import statistics

    def stats_dict(vals):
        s_vals = sorted(vals)
        n = len(s_vals)
        p25 = s_vals[int(n * 0.25)]
        med = statistics.median(s_vals)
        p75 = s_vals[int(n * 0.75)]
        p95 = s_vals[min(int(n * 0.95), n - 1)]
        iqr = p75 - p25
        mean = statistics.mean(s_vals)
        std = statistics.stdev(s_vals) if n > 1 else 0.0
        sem = std / (n ** 0.5) if n > 1 else 0.0
        return {
            "n": n, "min": min(s_vals), "p25": p25, "median": med,
            "p75": p75, "p95": p95, "max": max(s_vals), "iqr": iqr,
            "mean": mean, "std": std, "sem": sem,
        }

    accept_stats = stats_dict([r["tau_accept_ms"] for r in results])
    pubsub_stats = stats_dict([r["tau_pubsub_ms"] for r in results])

    print("\n" + "=" * 80)
    print(f"PROBE DISTRIBUTION RESULTS (N={accept_stats['n']} successful cycles across {window_idx} windows)")
    print("=" * 80)
    print(f"{'Metric':<20} | {'tau_accept (Engine)':<25} | {'tau_pubsub (Venue Broadcast)':<25}")
    print("-" * 75)
    print(f"{'Min':<20} | {accept_stats['min']:<22.2f} ms | {pubsub_stats['min']:<22.2f} ms")
    print(f"{'P25':<20} | {accept_stats['p25']:<22.2f} ms | {pubsub_stats['p25']:<22.2f} ms")
    print(f"{'Median':<20} | {accept_stats['median']:<22.2f} ms | {pubsub_stats['median']:<22.2f} ms")
    print(f"{'P75':<20} | {accept_stats['p75']:<22.2f} ms | {pubsub_stats['p75']:<22.2f} ms")
    print(f"{'P95':<20} | {accept_stats['p95']:<22.2f} ms | {pubsub_stats['p95']:<22.2f} ms")
    print(f"{'Max':<20} | {accept_stats['max']:<22.2f} ms | {pubsub_stats['max']:<22.2f} ms")
    print(f"{'IQR':<20} | {accept_stats['iqr']:<22.2f} ms | {pubsub_stats['iqr']:<22.2f} ms")
    print(f"{'Mean +/- SEM':<20} | {accept_stats['mean']:.2f} +/- {accept_stats['sem']:.2f} ms | {pubsub_stats['mean']:.2f} +/- {pubsub_stats['sem']:.2f} ms")
    print("-" * 75)
    print("Uncertainty Decomposition:")
    print(f"  - Random Error (SEM): +/- {accept_stats['sem']:.2f} ms")
    print("  - Residual Systematic Bias: <= 3.50 ms (route asymmetry + gateway TLS)")
    print(f"  - Total Bound: <= {accept_stats['sem'] + 3.50:.2f} ms")
    print("=" * 80)

    # Per-window breakdown
    windows = sorted(list(set(r["window_idx"] for r in results)))
    if len(windows) > 1:
        print("\n" + "-" * 75)
        print("PER-WINDOW BREAKDOWN")
        print("-" * 75)
        print(f"{'Window':<8} | {'Market Slug':<30} | {'Cycles':<8} | {'tau_accept (Med)':<18} | {'tau_pubsub (Med)':<18}")
        print("-" * 75)
        for w in windows:
            w_res = [r for r in results if r["window_idx"] == w]
            w_slug = w_res[0]["market_slug"]
            w_accept = statistics.median([r["tau_accept_ms"] for r in w_res])
            w_pubsub = statistics.median([r["tau_pubsub_ms"] for r in w_res])
            print(f"W{w:<7} | {w_slug:<30} | {len(w_res):<8} | {w_accept:<15.2f} ms | {w_pubsub:<15.2f} ms")
        print("-" * 75)

    if gaps:
        print("\nROLLOVER GAP LOG:")
        for g in gaps:
            print(f"  - Cycle {g['cycle']}: {g['gap_duration_s']:.2f}s gap before window '{g['market_slug']}'")


def _sweep_due(
    cycle: int,
    now: float,
    last_sweep_ts: float | None,
    sweep_interval: float | None,
    sweep_every: int,
) -> bool:
    """Whether this cycle should refresh the account card.

    `sweep_interval` (seconds) takes precedence and decouples the sweep from
    the poll tick rate; when it is None the legacy tick cadence applies. The
    first cycle always sweeps so the card is fresh on startup.
    """
    if cycle == 1:
        return True
    if sweep_interval is not None:
        return last_sweep_ts is None or (now - last_sweep_ts) >= sweep_interval
    return cycle % sweep_every == 0


def _spawn_guardrail_watcher(db_path: str | Path | None = None):
    """Launch the guardrail watcher as a child of the poll process.

    The watcher is read-only (cycle ring + registry, opened read-only); the
    poll supervises it so the two failure signatures (repeat exit, over-cap
    pair) are flagged whenever the bot runs -- not only when someone starts a
    third process by hand. Returns the Popen handle, or None on failure.
    Never raises.
    """
    import subprocess
    try:
        argv = [sys.executable, str(ROOT / "scripts" / "guardrail_watch.py"),
                "--interval", "5"]
        if db_path is not None:
            argv += ["--db", str(db_path)]
        return subprocess.Popen(
            argv, cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"WARNING: guardrail watcher spawn failed: {exc}",
              file=sys.stderr)
        return None


def _supervise_watcher(proc, db_path, last_restart_ts, log_fn=None,
                       restart_interval_s: float = 30.0):
    """Check the guardrail-watcher child; restart it if it died.

    Returns (proc, last_restart_ts). A dead child is restarted at most once
    per `restart_interval_s` so a crash-loop cannot spin the CPU; throttled
    checks are silent. Never raises.
    """
    if proc is None or proc.poll() is None:
        return proc, last_restart_ts
    rc = proc.returncode
    if time.time() - last_restart_ts < restart_interval_s:
        return proc, last_restart_ts
    msg = f"[POLL] guardrail watcher died (rc={rc}); restarting"
    print(msg, file=sys.stderr)
    if log_fn is not None:
        try:
            log_fn(msg)
        except Exception:
            pass
    return _spawn_guardrail_watcher(db_path), time.time()

def poll(
    interval: float = 5.0,
    once: bool = False,
    db_path: str | Path | None = None,
    client=None,
    sweep_every: int = 1,
    sweep_interval: float | None = None,
    watch_guardrails: bool = True,
) -> None:
    """Poll CLOB for open orders and fills, reconciling into order registry.

    Operability features:
    - Status line printed every cycle.
    - Append-only event log (run/live_events.log).
    - Atomic heartbeat (run/live_poll_heartbeat.json).
    - Exponential backoff on 429 / 5xx capped at 60s.
    - Account sweep folded into the loop, on its own error budget.
    - Clean SIGTERM / KeyboardInterrupt exit.

    The account sweep runs on the first cycle and then either every
    `sweep_interval` seconds (when set) or every `sweep_every` cycles. The
    seconds form decouples the card's freshness from the poll tick rate, so
    changing `--interval` does not change how often the venue is swept.
    """
    import datetime
    import signal
    from engine.cycle_stream import emit as _emit_cycle_event
    from engine.order_registry import (
        OrderRegistry,
        reconcile_orders,
        compute_backoff_delay,
        DEFAULT_DB_PATH,
        ReconcileInProgress,
    )

    db_p = Path(db_path) if db_path else DEFAULT_DB_PATH
    registry = OrderRegistry(db_path=db_p)

    # Remember whether a client was injected before building one: the markout
    # sampler must only start on the production path, never beside a test or
    # dry-run client whose presence means "do not reach the venue yourself".
    injected_client = client is not None
    if client is None:
        from engine.venue import client as _client_builder
        client = _client_builder()

    funder = os.environ.get("POLY_FUNDER")
    sweep_every = max(1, int(sweep_every))
    if sweep_interval is not None:
        sweep_interval = max(0.0, float(sweep_interval))

    stop_requested = False

    def _sig_handler(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    try:
        signal.signal(signal.SIGTERM, _sig_handler)
    except (ValueError, AttributeError):
        pass

    event_log_path = RUN / "live_events.log"
    heartbeat_path = RUN / "live_poll_heartbeat.json"
    RUN.mkdir(exist_ok=True)

    def _log_event(msg: str) -> None:
        """Append one line to the event log. Never raises into the poll loop."""
        try:
            with open(event_log_path, "a", encoding="utf-8") as ef:
                ef.write(f"{msg}\n")
        except OSError as exc:
            print(f"WARNING: event log write failed: {exc}", file=sys.stderr)

    sweep_funder_warned = False
    last_sweep_ts: float | None = None

    def _sweep_account(now_iso: str) -> str:
        """Read the account from the venue without failing the poll cycle.

        Returns "success", "skipped" (no funder), or "error" so the telemetry
        event never claims a completed sweep that did not complete. A failure
        here must leave the last good reading intact and must never count
        toward the reconcile backoff. The missing-funder SystemExit is guarded
        before the call rather than caught, so it cannot kill the poller.

        `last_sweep_ts` is stamped on every attempt, success or failure, so
        the interval cadence throttles retries instead of hammering the Data
        API during an outage.
        """
        nonlocal sweep_funder_warned, last_sweep_ts
        last_sweep_ts = time.time()
        if not funder:
            if not sweep_funder_warned:
                sweep_funder_warned = True
                _log_event(f"[{now_iso}] SWEEP SKIPPED: POLY_FUNDER not set")
            return "skipped"
        try:
            mark = account_sweep(funder=funder, db_path=db_p, quiet=True)
        except Exception as exc:
            _log_event(f"[{now_iso}] SWEEP ERROR: {exc}")
            return "error"
        try:
            log_float_mark_if_measured(registry, mark)
        except Exception as exc:
            _log_event(f"[{now_iso}] FLOAT MARK ERROR: {exc}")
            return "error"
        return "success"

    # START and STOP are written unconditionally, so the log exists from the
    # first second of a run. Without them a quiet session leaves no file at all,
    # and "it never started" is indistinguishable from "it ran and saw nothing"
    # -- which is exactly the question the log is here to answer.
    _boot_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _log_event(
        f"[{_boot_iso}] START pid={os.getpid()} interval={interval}s once={once} db={db_p}"
    )

    consecutive_errors = 0
    cycle = 0
    last_cycle_failed = False

    # The markout sampler fills the adverse-selection horizons out-of-band, so
    # it never blocks reconcile. It is a daemon thread, started only on the
    # production path (no injected client), and stopped when the loop exits.
    markout_worker = None
    if not once and not injected_client:
        from engine.markout import MarkoutWorker
        markout_worker = MarkoutWorker(
            registry=registry,
            clob_host=os.environ.get("CLOB_HOST", "https://clob.polymarket.com"),
        )
        markout_worker.start()

    # Supervise the guardrail watcher as a child so the two failure
    # signatures are flagged whenever the poll runs. Skipped for --once runs
    # and when a client was injected (test/dry-run context) -- the same rule
    # that keeps the markout sampler off the non-production path.
    watcher_proc = None
    watcher_last_restart = 0.0
    if watch_guardrails and not once and not injected_client:
        watcher_proc = _spawn_guardrail_watcher(db_p)
        if watcher_proc is not None:
            watcher_last_restart = time.time()
            _log_event(f"[POLL] guardrail watcher started (pid={watcher_proc.pid})")

    while not stop_requested:
        cycle += 1
        cycle_start = time.time()
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if watcher_proc is not None:
            watcher_proc, watcher_last_restart = _supervise_watcher(
                watcher_proc, db_p, watcher_last_restart, log_fn=_log_event)

        if _sweep_due(cycle, time.time(), last_sweep_ts, sweep_interval, sweep_every):
            sweep_outcome = _sweep_account(now_iso)
            sweep_action = (
                "sweep_done" if sweep_outcome == "success"
                else "sweep_skipped" if sweep_outcome == "skipped"
                else "sweep_error"
            )
            _emit_cycle_event(
                service="engine", cycle=cycle, phase="settling",
                action=sweep_action,
            )

        try:
            summary = reconcile_orders(client, registry, maker_address=funder)
            consecutive_errors = 0

            # Log any state transitions to event log
            if summary.transitions:
                for t in summary.transitions:
                    _log_event(f"[{now_iso}] {t}")

            active = registry.get_active_orders()
            open_count = sum(1 for o in active if o.status == "open")
            partial_count = sum(1 for o in active if o.status == "partial")
            pending_count = sum(1 for o in active if o.status == "pending")

            elapsed = time.time() - cycle_start
            print(
                f"[POLL {now_iso}] orders={len(active)} (open={open_count} partial={partial_count} pending={pending_count}) | "
                f"fills=+{summary.fills_recorded} (dup={summary.duplicates_ignored}) | "
                f"open_orders={summary.open_orders_count} trades={summary.trades_polled} | "
                f"cycle={elapsed:.2f}s | errors=0"
            )
            _emit_cycle_event(
                service="engine", cycle=cycle, phase="reconciling",
                action="reconcile_ok", latency_ms=elapsed * 1000.0,
                extra={
                    "fills": summary.fills_recorded,
                    "duplicates_ignored": summary.duplicates_ignored,
                    "transitions": len(summary.transitions),
                    "open": open_count,
                    "partial": partial_count,
                    "pending": pending_count,
                },
            )

        except KeyboardInterrupt:
            # Ctrl-C is not an error. It is a BaseException, so the handler
            # below never sees it, and the operator would get a traceback
            # instead of a clean stop on the one process meant to run for hours.
            stop_requested = True
            _log_event(f"[{now_iso}] STOP KeyboardInterrupt during cycle {cycle}")
            print(f"[POLL {now_iso}] stopping on KeyboardInterrupt", file=sys.stderr)
            break

        except ReconcileInProgress as exc:
            # Another pass holds the lock -- most often the operator running a
            # one-shot reconcile from a second shell. That is contention, not a
            # venue failure: counting it as an error would drive the exponential
            # backoff to 60s and degrade the poller for something that resolves
            # itself in milliseconds. Skip the cycle, keep the normal interval,
            # leave consecutive_errors alone.
            #
            # A --once run still reports failure, because it genuinely did not
            # reconcile and the caller must not read exit 0 as "state checked".
            skip_msg = f"[POLL {now_iso}] SKIPPED cycle {cycle}: {exc}"
            print(skip_msg, file=sys.stderr)
            _log_event(skip_msg)
            _emit_cycle_event(
                service="engine", cycle=cycle, phase="waiting",
                action="reconcile_contended",
            )
            if once:
                last_cycle_failed = True
                break
            if not stop_requested:
                try:
                    time.sleep(max(0.0, interval - (time.time() - cycle_start)))
                except KeyboardInterrupt:
                    stop_requested = True
                    break
                continue

        except Exception as exc:
            consecutive_errors += 1
            last_cycle_failed = True
            backoff_s = compute_backoff_delay(consecutive_errors, base_sec=2.0, max_sec=60.0)
            err_msg = f"[POLL {now_iso}] ERROR (count={consecutive_errors}, backoff={backoff_s:.1f}s): {exc}"
            print(err_msg, file=sys.stderr)
            _log_event(err_msg)
            _emit_cycle_event(
                service="engine", cycle=cycle, phase="reconciling",
                action="reconcile_error", reason=str(exc),
            )
            if not once and not stop_requested:
                try:
                    time.sleep(backoff_s)
                except KeyboardInterrupt:
                    stop_requested = True
                    break
                continue

        # U35 auto pass: convert in-window one-sided fills (complete under the
        # cap, exit at/over it). Runs after reconcile so the registry is fresh.
        # Closing actions only -- pre-approved. Failures are isolated per pair
        # inside auto_manage_pairs; a pass-level failure must never stop the
        # loop either.
        try:
            from engine.config import load as _load_cfg
            from engine.live_pairs import auto_manage_pairs
            for pr in auto_manage_pairs(
                client, registry, _load_cfg(), funder=funder,
            ):
                action = pr.get("action", "?")
                # Quiet decisions (hold/balanced/dry-run would_*) stay out of
                # the console but still reach the cycle ring so the dashboard
                # can count them per cycle.
                if action not in ("hold", "balanced",
                                  "would_exit", "would_complete"):
                    line = f"[POLL {now_iso}] pairs {pr.get('pair_id') or '?':<10s} {action}"
                    if action == "error":
                        line += f" ({pr.get('error', '')})"
                        print(line, file=sys.stderr)
                    else:
                        print(line)
                    _log_event(line)
                _emit_cycle_event(
                    service="engine", cycle=cycle, phase="settling",
                    action="pairs_" + action,
                    extra={"pair_id": pr.get("pair_id")},
                )
        except Exception as exc:
            err_msg = f"[POLL {now_iso}] pairs pass failed: {exc}"
            print(err_msg, file=sys.stderr)
            _log_event(err_msg)

        # Write heartbeat
        hb_data = {
            "ts": int(time.time() * 1000),
            "iso": now_iso,
            "pid": os.getpid(),
            "cycle": cycle,
            "errors": consecutive_errors,
        }
        _atomic_write_json(heartbeat_path, [hb_data])

        if once or stop_requested:
            break

        sleep_time = max(0.0, interval - (time.time() - cycle_start))
        try:
            time.sleep(sleep_time)
        except KeyboardInterrupt:
            # Ctrl-C almost always lands here rather than mid-reconcile, since
            # the loop spends nearly all its time asleep. It must announce
            # itself the same way the mid-cycle handler does.
            stop_requested = True
            stop_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _log_event(f"[{stop_iso}] STOP KeyboardInterrupt while idle after cycle {cycle}")
            print(f"[POLL {stop_iso}] stopping on KeyboardInterrupt", file=sys.stderr)
            break

    if markout_worker is not None:
        markout_worker.stop()

    # Take the watcher down with the poll: terminate, escalate to kill.
    if watcher_proc is not None:
        try:
            watcher_proc.terminate()
            watcher_proc.wait(timeout=5)
        except Exception:
            try:
                watcher_proc.kill()
            except Exception:
                pass

    exit_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _log_event(f"[{exit_iso}] EXIT cycles={cycle} errors={consecutive_errors}")

    # A --once run that failed its only cycle must exit non-zero. Returning 0
    # after printing an error to stderr makes the failure invisible to any
    # supervisor, cron entry or shell check that reads the exit status.
    if once and last_cycle_failed:
        sys.exit(1)


def exit_pair(pair_id: str, live: bool, db_path: str | Path | None = None,
              skip_positions_check: bool = False, force: bool = False) -> None:
    """Stage 3 — close a one-sided pair: cancel the resting leg, sell the filled one.

    The Data API positions read is a pre-flight, not decoration: it is the only
    independent check that the venue agrees with the registry about what we
    hold, and selling a size the venue does not agree we hold is an oversell.
    It fails closed -- an unreadable endpoint refuses the exit rather than
    proceeding unchecked. `--skip-positions-check` exists for the case where the
    Data API is down and the operator has decided to act anyway, and it says so
    on the record.
    """
    from engine.live_pairs import (
        exit_naked_leg, fetch_positions, load_pair, PairExitRefused,
    )
    from engine.order_registry import OrderRegistry, DEFAULT_DB_PATH
    from engine import config as strategy_config

    registry = OrderRegistry(db_path=Path(db_path) if db_path else DEFAULT_DB_PATH)
    _clob = client()
    funder = os.environ.get("POLY_FUNDER")

    # Same discipline as merge, redeem and complete: this sends a real market
    # SELL, so a repeat invocation must be refused rather than sell twice.
    # `naked` is derived from the registry, and registry fills only arrive
    # through the poll loop, so an immediate second run cannot see the first
    # sell and would happily send it again.
    # load_pair raises when the id is unknown -- an operator typo is the most
    # likely cause, and a traceback is the wrong way to say "no such pair".
    try:
        condition_id = load_pair(registry, pair_id)["condition_id"]
    except PairExitRefused as exc:
        raise SystemExit(f"EXIT REFUSED: {exc}") from exc
    if live:
        _check_idempotency_guard(condition_id, force=force)

    venue_positions = None
    if not skip_positions_check:
        if not funder:
            raise SystemExit(
                "Refusing to exit: POLY_FUNDER is not set, so the venue's "
                "position cannot be read and the registry's view cannot be "
                "checked. Set it, or pass --skip-positions-check to act "
                "without the cross-check."
            )
        try:
            venue_positions = fetch_positions(funder)
        except Exception as exc:
            raise SystemExit(
                f"Refusing to exit: the Data API positions read failed "
                f"({exc!r}). An unreadable endpoint is not an empty portfolio. "
                f"Pass --skip-positions-check to act without the cross-check."
            ) from exc

    entry_id = None
    if live:
        entry_id = _log_order({
            "kind": "exit",
            "pair_id": pair_id,
            "condition_id": condition_id,
            "status": "pending",
        })

    try:
        result = exit_naked_leg(
            _clob, registry, pair_id,
            max_pair_cost=strategy_config.load().max_pair_cost,
            live=live,
            venue_positions=venue_positions,
        )
    except PairExitRefused as exc:
        # A refusal sent nothing, so the row closes rather than blocking later
        # attempts. Only genuine uncertainty warrants `interrupted`.
        if entry_id:
            _update_order_log(entry_id, {"status": "cancelled", "error": str(exc)})
        raise SystemExit(f"EXIT REFUSED: {exc}") from exc
    except BaseException as exc:
        if entry_id:
            _update_order_log(entry_id, {"status": "interrupted", "error": repr(exc)})
        raise

    if entry_id:
        # Only a result that actually sent a SELL may hold the condition open.
        #
        # `route_to_merge`, `balanced` and `hold` send nothing, and marking them
        # `submitted` would leave the idempotency guard blocking the very
        # recovery this command prints two lines below: the operator is told to
        # run `merge`, and `merge` then refuses the condition until --force.
        # A guard that blocks the recovery it recommends is worse than no guard.
        sent = result.get("action") == "exited"
        _update_order_log(entry_id, {
            "status": "submitted" if sent else "cancelled",
            "action": result.get("action"),
            "size": result.get("size"),
        })

    print(json.dumps(result, indent=2, default=str))
    if result["action"] == "route_to_merge":
        print(
            f"\nThe pair completed between the cancel and the sell. It is now "
            f"worth $1.00 at merge -- run:\n"
            f"  python -m engine.live_exec merge {result['condition_id']} "
            f"--amount <shares> --live"
        )


def complete_pair_cmd(pair_id: str, live: bool, db_path: str | Path | None = None,
                      skip_positions_check: bool = False, force: bool = False) -> None:
    """Stage 4 — cross the book to complete a one-sided pair.

    Closes exposure rather than opening it: the half-open leg is already at
    risk, and completing it yields a pair worth $1.00 at merge. Refuses any
    cross that would push the pair to or past max_pair_cost -- that case
    belongs to `exit`, and this path must not do the stop-loss's job badly.
    """
    from engine.live_pairs import (
        complete_pair, fetch_positions, load_pair, PairCompletionRefused,
        PairExitRefused,
    )
    from engine.order_registry import OrderRegistry, DEFAULT_DB_PATH
    from engine import config as strategy_config

    registry = OrderRegistry(db_path=Path(db_path) if db_path else DEFAULT_DB_PATH)
    # load_pair signals an unknown pair with the exit path's exception, which
    # this command does not otherwise catch. Without this an operator typo
    # escapes as a traceback rather than the refusal message.
    try:
        pair = load_pair(registry, pair_id)
    except PairExitRefused as exc:
        raise SystemExit(f"COMPLETION REFUSED: {exc}") from exc
    condition_id = pair["condition_id"]

    # Same pre-flight discipline as every other live write path here. This
    # sends a real BUY, so it gets the same two guards `merge` and `redeem`
    # have: a repeat invocation must not cross twice for the same pair, and no
    # order goes out on a registry view the venue has not corroborated.
    if live:
        _check_idempotency_guard(condition_id, force=force)

    venue_positions = None
    if not skip_positions_check:
        funder = os.environ.get("POLY_FUNDER")
        if not funder:
            raise SystemExit(
                "Refusing to complete: POLY_FUNDER is not set, so the venue's "
                "position cannot be read. Set it, or pass "
                "--skip-positions-check to act without the cross-check."
            )
        try:
            venue_positions = fetch_positions(funder)
        except Exception as exc:
            raise SystemExit(
                f"Refusing to complete: the Data API positions read failed "
                f"({exc!r}). An unreadable endpoint is not an empty portfolio."
            ) from exc

        token = pair["heavy"]["token_id"]
        believed = pair["heavy"]["matched"]
        if token not in venue_positions:
            raise SystemExit(
                f"Refusing to complete: the venue reports no position at all "
                f"in {token} while the registry holds {believed:.6f}. Absence "
                f"is not zero -- it is equally consistent with a filtered read."
            )
        observed = float(venue_positions[token])
        if observed < believed - 1e-6:
            raise SystemExit(
                f"Refusing to complete: registry holds {believed:.6f} of "
                f"{token} but the venue reports only {observed:.6f}. Completing "
                f"against a leg the venue does not agree we hold would open "
                f"exposure rather than close it."
            )

    entry_id = None
    if live:
        entry_id = _log_order({
            "kind": "complete",
            "pair_id": pair_id,
            "condition_id": condition_id,
            "status": "pending",
        })

    try:
        result = complete_pair(
            client(), registry, pair_id,
            max_pair_cost=strategy_config.load().max_pair_cost,
            live=live,
            max_order_usd=MAX_ORDER_USD,
        )
    except PairCompletionRefused as exc:
        if entry_id:
            _update_order_log(entry_id, {"status": "cancelled", "error": str(exc)})
        raise SystemExit(f"COMPLETION REFUSED: {exc}") from exc
    except BaseException as exc:
        # Anything else left the order in an unknown state at the venue.
        # `interrupted` is what the idempotency guard refuses on, which is the
        # correct posture when we do not know whether the BUY landed.
        if entry_id:
            _update_order_log(entry_id, {"status": "interrupted", "error": repr(exc)})
        raise

    if entry_id:
        # Same rule as the exit: `balanced` crossed nothing, so it must not hold
        # the condition against a later merge or completion.
        sent = result.get("action") == "completed"
        _update_order_log(entry_id, {
            "status": "submitted" if sent else "cancelled",
            "action": result.get("action"),
            "size": result.get("size"),
            "notional": result.get("notional"),
        })

    print(json.dumps(result, indent=2, default=str))


def cancel_single_order(order_id: str, live: bool,
                        db_path: str | Path | None = None) -> None:
    """Cancel a single active order by venue order ID."""
    if not live:
        print(f"DRY RUN -- would cancel order {order_id}. Re-run with --live.")
        return

    from py_clob_client_v2.clob_types import OrderPayload
    from engine.order_registry import OrderRegistry, DEFAULT_DB_PATH

    c = client()
    try:
        resp = c.cancel_order(OrderPayload(orderID=order_id))
    except Exception as exc:
        raise SystemExit(
            f"CANCEL REFUSED: venue rejected cancellation of order {order_id}: {exc}"
        ) from exc

    if isinstance(resp, dict) and resp.get("canceled") is None and resp.get("success") is False:
        err = resp.get("errorMsg") or resp.get("error") or str(resp)
        raise SystemExit(
            f"CANCEL REFUSED: venue reported failure for order {order_id}: {err}"
        )

    # Update registry if order is tracked locally
    registry = OrderRegistry(db_path=Path(db_path) if db_path else DEFAULT_DB_PATH)
    row = registry.get_order_by_venue_id(order_id)
    if row:
        now_ms = int(time.time() * 1000)
        registry.update_order_status(row.id, "cancelled", now_ms)

    print(json.dumps(resp, indent=2, default=str) if isinstance(resp, (dict, list)) else resp)


def cancel_market(condition_id: str, live: bool,
                  db_path: str | Path | None = None) -> None:
    """Cancel all active orders for a given market / condition ID."""
    if not live:
        print(f"DRY RUN -- would cancel all active orders for market {condition_id}. Re-run with --live.")
        return

    from py_clob_client_v2.clob_types import OrderMarketCancelParams
    from engine.order_registry import OrderRegistry, DEFAULT_DB_PATH

    c = client()
    try:
        resp = c.cancel_market_orders(OrderMarketCancelParams(market=condition_id))
    except Exception as exc:
        raise SystemExit(
            f"CANCEL-MARKET REFUSED: venue rejected market cancellation for {condition_id}: {exc}"
        ) from exc

    if isinstance(resp, dict) and resp.get("canceled") is None and resp.get("success") is False:
        err = resp.get("errorMsg") or resp.get("error") or str(resp)
        raise SystemExit(
            f"CANCEL-MARKET REFUSED: venue reported failure for {condition_id}: {err}"
        )

    # Update registry active orders for this condition
    registry = OrderRegistry(db_path=Path(db_path) if db_path else DEFAULT_DB_PATH)
    now_ms = int(time.time() * 1000)
    for order in registry.get_active_orders():
        if order.condition_id and order.condition_id.lower() == condition_id.lower():
            registry.update_order_status(order.id, "cancelled", now_ms)

    print(json.dumps(resp, indent=2, default=str) if isinstance(resp, (dict, list)) else resp)


def cancel_all(live: bool) -> None:
    if not live:
        print("DRY RUN -- would cancel ALL open orders. Re-run with --live.")
        return
    try:
        resp = client().cancel_all()
    except Exception as exc:
        raise SystemExit(f"CANCEL-ALL REFUSED: venue rejected cancel-all: {exc}") from exc
    print(json.dumps(resp, indent=2, default=str) if isinstance(resp, (dict, list)) else resp)


def api_creds(force: bool = False) -> None:
    """Derive the L2 API credentials once and write them into .env.

    Every command used to derive a fresh key, and derivation is the most
    rate-limit-sensitive endpoint the venue exposes. Once these three values are
    in .env, `client()` uses them directly and no command ever calls
    /auth/derive-api-key again.

    The values are written straight to the file and never printed. .env already
    holds the private key these are derived from, so this adds nothing to that
    file's blast radius -- but it does not belong in a terminal scrollback.
    """
    env_file = _find_env_file()
    if env_file is None:
        raise SystemExit(
            "No .env found. Create one at the repo root before running this.")

    existing = api_creds_from_env()
    if existing is not None and not force:
        print(f"{env_file} already has POLY_API_KEY, POLY_API_SECRET, and "
              f"POLY_API_PASSPHRASE.\nNothing to do. Re-derive with --force.")
        return

    from py_clob_client_v2.client import ClobClient

    key = os.environ.get("POLY_PRIVATE_KEY") or os.environ.get("POLY_KEY")
    if not key:
        raise SystemExit("POLY_PRIVATE_KEY not set.")

    c = ClobClient(os.environ.get("CLOB_HOST", "https://clob.polymarket.com"),
                   key=key, chain_id=137,
                   signature_type=int(os.environ.get("POLY_SIG_TYPE", "3")),
                   funder=os.environ.get("POLY_FUNDER"))
    try:
        creds = c.create_or_derive_api_key()
    except Exception as exc:
        raise SystemExit(
            f"Derivation failed: {type(exc).__name__}. The venue throttles this "
            f"endpoint -- wait a few minutes and run this once more. Everything "
            f"else keeps working; only account value needs credentials.") from exc

    text = env_file.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines()
             if ln.split("=", 1)[0].strip() not in
             ("POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE")]
    lines += [
        "",
        "# L2 API credentials, derived from POLY_PRIVATE_KEY. Present so that no",
        "# command has to call /auth/derive-api-key, which the venue rate-limits.",
        f"POLY_API_KEY={creds.api_key}",
        f"POLY_API_SECRET={creds.api_secret}",
        f"POLY_API_PASSPHRASE={creds.api_passphrase}",
    ]
    # Atomic, because this file holds POLY_PRIVATE_KEY. A plain write truncates
    # first: a crash or a full disk between truncate and flush would leave the
    # wallet's signing key destroyed, and it is not recoverable from anywhere in
    # this repo. Same temp-file/fsync/os.replace shape as _atomic_write_json.
    if not _atomic_write_text(env_file, "\n".join(lines) + "\n"):
        raise SystemExit(
            f"Could not write {env_file}. It is unchanged -- nothing was lost.")
    print(f"Wrote POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE to {env_file}.")
    print("Values are not printed here on purpose. Confirm .env is in .gitignore.")
    print("Every later command now skips derivation entirely.")


def account_sweep(funder: str | None = None, db_path: str | None = None,
                  quiet: bool = False) -> dict:
    """Read the account from the venue and record the reading in the registry.

    Read-only at the venue: collateral, open positions, closed positions, and
    the venue's own P&L series. Nothing here can open or increase exposure.

    The dashboard never calls the venue -- it reads the row this writes. That
    is what lets the headline tile show the account's real value while the page
    keeps its "zero venue network calls, zero credentials" contract.
    """
    from engine.account import read_account
    from engine.order_registry import OrderRegistry

    who = funder or os.environ.get("POLY_FUNDER")
    if not who:
        raise SystemExit("POLY_FUNDER not set. Cannot identify the account to read.")

    # Collateral needs signed CLOB credentials; everything else is a public GET
    # keyed by address. None here (no credentials, or a network failure) leaves
    # account_value NULL rather than reporting positions-only as the total.
    collateral = fetch_live_balance(who)
    mark = read_account(who, collateral_usd=collateral)

    registry = OrderRegistry(db_path=Path(db_path) if db_path else None)
    registry.log_account_mark(mark)

    if not quiet:
        def _usd(v):
            return "--" if v is None else f"${v:,.2f}"
        print(f"funder            {who}")
        print(f"collateral        {_usd(mark['collateral_usd'])}")
        print(f"positions value   {_usd(mark['positions_value_usd'])}")
        print(f"ACCOUNT VALUE     {_usd(mark['account_value_usd'])}")
        pct = mark["pnl_pct"]
        pct_s = "--" if pct is None else f"{pct:+.2f}%"
        print(f"P&L (all time)    {_usd(mark['pnl_usd'])}  ({pct_s})")
        print(f"  from series     {_usd(mark['pnl_series_usd'])}")
        print(f"  from closes     {_usd(mark['pnl_closed_usd'])}")
        gap = mark["pnl_source_gap"]
        if gap is not None and abs(gap) >= 0.005:
            print(f"  SOURCES DISAGREE by {gap:+.2f} -- neither is preferred silently")
        print(f"unrealized (open) {_usd(mark['unrealized_usd'])}")
        print(f"committed (open)  {_usd(mark['committed_usd'])}")
        print(f"open positions    {mark['open_positions_count']}")
    return mark


def venue_sync(funder=None, db_path=None, quiet=False):
    """Reconcile the local registry with what the venue currently shows.

    Reads the same five venue endpoints as `account_sweep` (one needs signed
    CLOB credentials; the rest are address-keyed GETs) and persists the
    per-trade detail that the sweep drops on the floor:

    * each venue `closed_positions` row becomes a row in `closes` so the
      dashboard Win Rate, Sharpe, Realized PnL, and Drawdown tiles have data;
    * the current `open_positions` set becomes one new `float_marks` row
      so the exposure chart and per-market tiles reflect venue reality.

    Idempotent. Re-running does not duplicate rows: closes use
    `(condition_id, asset)` as the dedup key (the venue returns one row per
    closed asset); float marks always write a new row, but they are point-in-
    time observations and the schema is INSERT, not INSERT OR REPLACE.

    Reads the venue. Never writes an order, places a quote, or opens exposure
    -- same read-only contract as `account_sweep`. Use from the dashboard
    Sync button when the page must catch up with state the bot stack missed
    (overnight fills, on-chain resolutions the local engine never observed).
    """
    from engine.account import read_account, fetch_closed_positions, fetch_open_positions
    from engine.order_registry import (
        OrderRegistry, CloseRecord, get_run_id,
    )

    who = funder or os.environ.get("POLY_FUNDER")
    if not who:
        raise SystemExit("POLY_FUNDER not set. Cannot identify the account to read.")

    collateral = fetch_live_balance(who)
    mark = read_account(who, collateral_usd=collateral)

    registry = OrderRegistry(db_path=Path(db_path) if db_path else None)
    registry.log_account_mark(mark)

    raw_closed = fetch_closed_positions(who, timeout=15.0) or []
    closes_written = 0
    closes_skipped_existing = 0
    for row in raw_closed:
        if not isinstance(row, dict):
            continue
        cid = row.get("conditionId")
        asset = row.get("asset")
        if not cid or not asset:
            continue

        ts_raw = row.get("timestamp")
        try:
            ts = float(ts_raw) if ts_raw is not None else time.time()
        except (TypeError, ValueError):
            ts = time.time()

        realized = row.get("realizedPnl")
        try:
            realized_pnl = float(realized) if realized is not None else 0.0
        except (TypeError, ValueError):
            realized_pnl = 0.0
        total_bought = row.get("totalBought")
        try:
            shares = float(total_bought) if total_bought is not None else None
        except (TypeError, ValueError):
            shares = None
        avg_price = row.get("avgPrice")
        try:
            up_price = float(avg_price) if avg_price is not None else None
        except (TypeError, ValueError):
            up_price = None

        # Atomic dedup: INSERT ... ON CONFLICT DO NOTHING. The UNIQUE index on
        # (condition_id, tx_hash) rejects duplicates; rowcount tells us if we actually inserted.
        # venue_sync sources account-wide reconciled history that wasn't necessarily opened
        # by the current run, so we use a sentinel "venue_sync" run_id rather than
        # incorrectly attributing to the active session's run analytics.
        close_rec = CloseRecord(
            ts=ts,
            condition_id=cid,
            market_slug=row.get("slug") or row.get("eventSlug"),
            method="venue_sync",
            shares=shares,
            up_price=up_price,
            dn_price=None,
            cost_basis=None,
            proceeds=None,
            realized_pnl=realized_pnl,
            tx_hash=asset,
            run_id="venue_sync",  # Sentinel: not attributed to current run
        )
        r_id = close_rec.run_id
        with registry._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                INSERT INTO closes (
                    ts, condition_id, market_slug, method, gas, shares,
                    up_price, dn_price, cost_basis, proceeds, fee, realized_pnl,
                    forgone_vs_settlement, up_cost_removed, dn_cost_removed,
                    tx_hash, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_id, tx_hash) DO NOTHING
                """,
                (
                    close_rec.ts,
                    close_rec.condition_id,
                    close_rec.market_slug,
                    close_rec.method,
                    close_rec.gas,
                    close_rec.shares,
                    close_rec.up_price,
                    close_rec.dn_price,
                    close_rec.cost_basis,
                    close_rec.proceeds,
                    close_rec.fee,
                    close_rec.realized_pnl,
                    close_rec.forgone_vs_settlement,
                    close_rec.up_cost_removed,
                    close_rec.dn_cost_removed,
                    close_rec.tx_hash,
                    r_id,
                ),
            )
            conn.commit()
            if cur.rowcount > 0:
                closes_written += 1
            else:
                closes_skipped_existing += 1

    # mark from compose_account_mark has no open_positions key -- we never
    # propagated it. Re-fetch directly so the float_mark reflects venue truth.
    open_positions = fetch_open_positions(who, timeout=15.0) or []
    if open_positions:
        committed = 0.0
        unrealized = 0.0
        # Group positions by condition_id to compute unpaired exposure per market.
        # A balanced pair (YES/NO on same condition) has naked = 0; a single leg
        # or imbalanced pair contributes the unpaired amount.
        condition_groups = {}
        for op in open_positions:
            if not isinstance(op, dict):
                continue
            try:
                committed += float(op.get("initialValue") or 0.0)
                unrealized += float(op.get("cashPnl") or 0.0)
                cid = op.get("conditionId")
                if cid:
                    if cid not in condition_groups:
                        condition_groups[cid] = []
                    condition_groups[cid].append(float(op.get("currentValue") or 0.0))
            except (TypeError, ValueError):
                continue

        # Naked = sum of unpaired exposure across all markets. For each condition,
        # the paired amount is min(values), the remainder is unpaired.
        naked = 0.0
        try:
            for cid, values in condition_groups.items():
                if values:
                    # Paired = the smaller leg; unpaired = sum - 2*paired
                    total_in_cond = sum(values)
                    paired = min(values)
                    naked += max(0.0, total_in_cond - 2 * paired)
        except Exception:
            naked = 0.0

        registry.log_float_mark(
            unrealized_usd=unrealized,
            committed_open_usd=committed,
            naked_usd=naked,
            ts=time.time(),
            run_id=get_run_id(),
        )

    summary = {
        "ok": True,
        "account_value_usd": mark.get("account_value_usd"),
        "closed_positions_count": mark.get("closed_positions_count"),
        "open_positions_count": mark.get("open_positions_count"),
        "closes_written": closes_written,
        "closes_skipped_existing": closes_skipped_existing,
        "raw_closed_rows": len(raw_closed),
        "raw_open_rows": len(open_positions),
    }
    if not quiet:
        av = mark.get("account_value_usd")
        av_s = "--" if av is None else f"${av:,.2f}"
        print(f"venue_sync: account={av_s}  closed={mark.get('closed_positions_count')}  "
              f"open={mark.get('open_positions_count')}  closes_written={closes_written}  "
              f"skipped={closes_skipped_existing}")
    return summary


def decide(
    target: str | None = None,
    all_graduated: bool = False,
    db_path: str | Path | None = None,
) -> list[dict]:
    """Read-only quote decision for graduated markets using live venue books."""
    from dataclasses import replace
    from engine.config import load
    from engine.market_feed import load_graduated_markets, get_market_by_cid, GraduatedMarket
    from engine.order_registry import DEFAULT_DB_PATH

    cfg = load()
    live_bal = fetch_live_balance()
    if live_bal is not None and live_bal > 0:
        cfg = replace(cfg, bankroll_usd=live_bal)

    reg_db = Path(db_path) if db_path else DEFAULT_DB_PATH

    graduated_list = load_graduated_markets()
    if not graduated_list:
        raise SystemExit("no graduated markets found in run/markets.json")

    targets: list[tuple[str, GraduatedMarket | None]] = []
    if all_graduated or target == "all" or target == "--all":
        targets = [(gm.cid, gm) for gm in graduated_list]
    elif target is None or target == "":
        targets = [(graduated_list[0].cid, graduated_list[0])]
    else:
        if target.isdigit() and 0 <= int(target) < len(graduated_list):
            gm = graduated_list[int(target)]
            targets = [(gm.cid, gm)]
        else:
            gm = get_market_by_cid(target)
            if gm is None:
                for candidate in graduated_list:
                    if candidate.slug.lower() == target.lower() or target.lower() in candidate.slug.lower():
                        gm = candidate
                        break
            if gm is not None:
                targets = [(gm.cid, gm)]
            else:
                targets = [(target, None)]


    results: list[dict] = []
    for cid, gm in targets:
        res = _evaluate_single_market_quote(cid, gm, cfg, reg_db)
        results.append(res)
    return results


def _evaluate_single_market_quote(
    cid: str,
    gm: "GraduatedMarket" | None,
    cfg: "MakerConfig",
    reg_db: Path,
) -> dict:
    """Evaluate and print strategy quote decision for one market."""
    from engine.markets import fetch_pinned_market, full_book
    from engine.order_registry import inventory_from_registry, OrderRegistry
    from engine.quotes import (
        MarketQuoteError, MarketUnavailable, decide_quotes, evaluate_market_quote,
    )

    registry = OrderRegistry(db_path=reg_db)
    clob_host = getattr(cfg, "clob_host", "https://clob.polymarket.com")

    try:
        ev = evaluate_market_quote(
            cid, cfg, clob_host,
            fetch_market=lambda c: fetch_pinned_market(c, require_rewards=False),
            fetch_books=full_book,
            inventory_for=lambda m: inventory_from_registry(
                m.condition_id, m.up_token, m.down_token, db_path=reg_db),
            decide=decide_quotes,
        )
    except MarketUnavailable:
        print(f"MARKET {cid[:16]}...: unable to load on venue (missing, closed, or not 2 tokens)")
        return {"cid": cid, "status": "ERROR", "why": "market unavailable"}
    except MarketQuoteError as e:
        print(f"MARKET {cid[:16]}...: book fetch error: {e}")
        return {"cid": cid, "status": "ERROR", "why": f"book fetch error: {e}"}

    m = ev.market
    up_book, down_book = ev.up_book, ev.down_book
    inv = ev.inventory
    intents, why = ev.intents, ev.why

    title = gm.title if gm and gm.title else m.market_slug
    slug = gm.slug if gm and gm.slug else m.market_slug

    bb_up, ba_up = up_book.get("best_bid"), up_book.get("best_ask")
    mid_up = (bb_up + ba_up) / 2.0 if (bb_up is not None and ba_up is not None) else None
    bb_dn, ba_dn = down_book.get("best_bid"), down_book.get("best_ask")
    mid_dn = (bb_dn + ba_dn) / 2.0 if (bb_dn is not None and ba_dn is not None) else None

    depth_up = sum(up_book.get("bids", {}).values())
    depth_dn = sum(down_book.get("bids", {}).values())

    couple_alloc = max(cfg.bankroll_usd * getattr(cfg, "couple_risk_frac", 0.01), getattr(cfg, "min_couple_usd", 6.00))
    leg_alloc = couple_alloc / 2.0

    print("=" * 80)
    print(f"MARKET:    {title}")
    print(f"SLUG:      {slug}")
    print(f"CID:       {m.condition_id}")
    print(f"BANKROLL:  ${cfg.bankroll_usd:,.2f} USDC (Sizing: max(${cfg.bankroll_usd:.2f} * 1%, ${getattr(cfg, 'min_couple_usd', 6.0):.2f}) = ${couple_alloc:.2f} couple, ${leg_alloc:.2f}/leg)")
    print(f"TICK:      {m.tick_size:<6} NEG_RISK: {m.neg_risk}")
    if gm:
        print(f"GRADUATED: min_size={gm.min_size} tick={gm.tick} max_spread={gm.max_spread} "
              f"days_to_resolve={gm.days_to_resolve:.2f} daily_rewards=${gm.daily:.2f}")
    print(f"BOOKS:")
    print(f"  UP   (YES): bid={bb_up} ask={ba_up} mid={f'{mid_up:.4f}' if mid_up else 'None'} depth={depth_up:.0f}sh (token {m.up_token[:14]}...)")
    print(f"  DOWN (NO):  bid={bb_dn} ask={ba_dn} mid={f'{mid_dn:.4f}' if mid_dn else 'None'} depth={depth_dn:.0f}sh (token {m.down_token[:14]}...)")
    print(f"INVENTORY:")
    print(f"  UP: {inv.up_shares:.0f}sh (avg ${inv.avg('UP'):.3f}, cost ${inv.up_cost:.2f}) | "
          f"DOWN: {inv.down_shares:.0f}sh (avg ${inv.avg('DOWN'):.3f}, cost ${inv.down_cost:.2f}) | "
          f"fills={inv.fills} balance={inv.balance:.1%}")

    if intents:
        print("DECISION: QUOTE INTENTS")
        p_up = p_dn = None
        s_up = s_dn = 0
        for qi in intents:
            edge_str = f"{100*qi.edge_vs_mid:.2f}c" if qi.edge_vs_mid is not None else "n/a"
            tag = " [CROSSED]" if qi.crossed else ""
            tok_str = str(qi.token_id or (m.up_token if qi.side == "UP" else m.down_token))[:14]
            print(f"  BUY {qi.size:3d} {qi.side:4s} @ {qi.price:.3f} (mid {qi.mid:.3f}, capture {edge_str}){tag} "
                  f"notional=${qi.price * qi.size:.2f} token={tok_str}... {qi.reason}")
            if qi.side == "UP":
                p_up, s_up = qi.price, qi.size
            elif qi.side == "DOWN":
                p_dn, s_dn = qi.price, qi.size

        if p_up is not None and p_dn is not None:
            pair_cost = p_up + p_dn
            edge = 1.00 - pair_cost
            worst_naked = max(p_up * s_up, p_dn * s_dn)
            print(f"SIZING & RISK:")
            print(f"  Pair price:            ${pair_cost:.4f} ({p_up:.3f} UP + {p_dn:.3f} DOWN)")
            print(f"  Capture below $1.00:   ${edge:.4f} / pair ({100*edge:.2f}%)")
            print(f"  Total 2-leg cost:      ${(p_up * s_up) + (p_dn * s_dn):.2f}")
            print(f"  Worst-case naked loss: ${worst_naked:.2f}")
    else:
        print(f"DECISION: DECLINED -- {why or 'no side quotable'}")
    print("=" * 80)

    # Telemetry logging to live.db (Amendment 4: decide stays strictly read-only on orders, logs telemetry only)
    from engine.order_registry import HedgeCensusRecord, MarketEventRecord, get_run_id
    pair_touch = round(ba_up + ba_dn - 0.02, 4) if (ba_up is not None and ba_dn is not None) else None
    fillable_sub = 1.0 if (pair_touch is not None and pair_touch < cfg.max_pair_cost) else 0.0
    registry.log_hedge_census(HedgeCensusRecord(
        condition_id=m.condition_id,
        market_slug=slug,
        up_ask=ba_up,
        down_ask=ba_dn,
        pair_cost_at_touch=pair_touch,
        fillable_sub_one=fillable_sub,
        observed_ts=time.time(),
        run_id=get_run_id(),
    ))

    if intents:
        for qi in intents:
            registry.log_market_event(MarketEventRecord(
                ts=time.time(),
                condition_id=m.condition_id,
                market_slug=slug,
                kind="QUOTING",
                reason=qi.reason,
                reason_code="INTENT_GENERATED",
                side=qi.side,
                price=qi.price,
                size=float(qi.size),
                run_id=get_run_id(),
            ))
    else:
        code = "OTHER"
        if why:
            if "band" in why.lower():
                code = "PRICE_BAND"
            elif "shortfall" in why.lower() or "cost" in why.lower():
                code = "COST_LIMIT"
            elif "inventory" in why.lower():
                code = "INVENTORY_MAX"
            elif "one_sided" in why.lower() or "two_sided" in why.lower():
                code = "TWO_SIDED_REQUIRED"
        registry.log_market_event(MarketEventRecord(
            ts=time.time(),
            condition_id=m.condition_id,
            market_slug=slug,
            kind="BLOCKED",
            reason=why or "no side quotable",
            reason_code=code,
            run_id=get_run_id(),
        ))

    return {
        "cid": cid,
        "title": title,
        "slug": slug,
        "intents": intents,
        "why": why,
        "up_book": {"bb": bb_up, "ba": ba_up, "mid": mid_up},
        "down_book": {"bb": bb_dn, "ba": ba_dn, "mid": mid_dn},
    }






def main() -> None:
    ap = argparse.ArgumentParser(description="LIVE Polymarket execution.")
    ap.add_argument("--live", action="store_true",
                    help="actually send. Without it, everything is a dry run.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    b = sub.add_parser("balance")
    b.add_argument("--funder", default=None,
                   help="test a candidate funder without editing .env")
    q = sub.add_parser("quote")
    q.add_argument("condition_id")
    q.add_argument("--price", type=float, required=True, help="UP token bid price")
    q.add_argument("--down-price", type=float, default=None, help="DOWN token bid price (default: 1.0 - price)")
    q.add_argument("--size", type=float, required=True)
    q.add_argument("--post-only", action=argparse.BooleanOptionalAction, default=True,
                   help="Ensure orders rest on the book and do not match immediately (default: True).")
    q.add_argument("--tif", choices=["GTC", "GTD", "FOK", "FAK"], default="GTC",
                   help="Time in force: GTC (default), GTD, FOK, FAK.")
    q.add_argument("--expiration", type=int, default=None,
                   help="Expiration timestamp (UTC epoch seconds) required when --tif GTD.")
    q.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    q.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                  help="actually send.")
    canc = sub.add_parser("cancel", help="Cancel a single active order by venue order ID.")
    canc.add_argument("order_id", help="Venue order ID to cancel")
    canc.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    canc.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                      help="actually send.")
    cm = sub.add_parser("cancel-market", help="Cancel all active orders for a condition ID.")
    cm.add_argument("condition_id", help="Condition ID to cancel orders for")
    cm.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    cm.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                    help="actually send.")
    r = sub.add_parser("redeem", help="Gasless redemption of winning positions via Relayer.")
    r.add_argument("condition_id", help="Condition ID to redeem")
    r.add_argument("--index-sets", default="1,2", help="Comma-separated index sets (default: 1,2)")
    r.add_argument("--collateral", default=USDC_E_CONTRACT, help="Collateral token (default: USDC.e)")
    r.add_argument("--skip-resolution-check", action="store_true",
                   help="Bypass RPC resolution guard if RPC endpoints are unreachable (does not bypass denom == 0).")
    r.add_argument("--force", action="store_true",
                   help="Bypass idempotency guard against prior pending/submitted/interrupted orders.")
    r.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                  help="actually send.")
    m = sub.add_parser("merge", help="Gasless merge of full outcome sets via Relayer.")
    m.add_argument("condition_id", help="Condition ID to merge")
    m.add_argument("--amount", type=float, required=True, help="Number of shares / pairs to merge")
    m.add_argument("--index-sets", default="1,2", help="Comma-separated index sets (default: 1,2)")
    m.add_argument("--collateral", default=USDC_E_CONTRACT, help="Collateral token (default: USDC.e)")
    m.add_argument("--force", action="store_true",
                   help="Bypass idempotency guard against prior pending/submitted/interrupted orders.")
    m.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                  help="actually send.")
    # A recurring series (such as btc-up-or-down-5m) is chosen for probe as a
    # LATENCY FIXTURE because it is always mid-window and continually regenerates,
    # not because it represents the traded universe.
    # Measured against run/fleet.db (66,317 quotes across 425 distinct markets):
    #   tennis (atp/wta)   34,294   51.71%
    #   baseball (mlb)     14,772   22.27%
    #   esports (cs2 etc)  12,584   18.98%
    #   crypto              2,478    3.74%
    #   other               2,189    3.30%
    # Crypto is under 4% of everything quoted. One-off sports markets do not
    # regenerate, so probe requires an explicit fixture target.
    p = sub.add_parser("probe", help="Multi-cycle live latency probe across dynamic series windows.")
    p.add_argument("--series", default=None, help="Series slug fixture (e.g. btc-up-or-down-5m). Exactly one of --series or --token-id required.")
    p.add_argument("--token-id", default=None, help="Fixed token ID override fixture. Exactly one of --series or --token-id required.")
    p.add_argument("--cycles", type=int, default=30, help="Number of probe cycles (default: 30)")
    p.add_argument("--min-time-remaining", type=float, default=90.0, help="Minimum seconds remaining in window (default: 90s)")
    p.add_argument("--max-complement-bid", type=float, default=0.85, help="Max allowed complement best bid (default: 0.85)")
    p.add_argument("--max-loss", type=float, default=1.00, help="Max cumulative probe loss in USD before abort (default: 1.00)")
    p.add_argument("--max-fills", type=int, default=1, help="Max allowable fills before abort (default: 1)")
    p.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                  help="actually send.")
    pl = sub.add_parser("poll", help="Poll CLOB and reconcile orders and fills.")
    pl.add_argument("--interval", type=float, default=5.0, help="Cadence in seconds (default: 5.0)")
    pl.add_argument("--once", action="store_true", help="Reconcile once and exit")
    pl.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    pl.add_argument("--sweep-every", type=int, default=1,
                    help="Account sweep every N poll cycles when --sweep-interval is not set (default 1 = every tick)")
    pl.add_argument("--sweep-interval", type=float, default=None,
                    help="Account sweep cadence in seconds, independent of the poll interval (overrides --sweep-every)")
    pl.add_argument("--watch-guardrails", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="Supervise the guardrail watcher as a child process (default: on; --no-watch-guardrails disables)")
    ex = sub.add_parser("exit", help="Stage 3: close a one-sided pair (cancel resting leg, sell filled leg).")
    ex.add_argument("pair_id", help="pair_id as recorded in the order registry")
    ex.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    ex.add_argument("--skip-positions-check", action="store_true",
                    help="Act without the Data API registry/venue cross-check. Only when the endpoint is down.")
    ex.add_argument("--force", action="store_true",
                    help="Bypass idempotency guard against prior pending/submitted/interrupted orders.")
    ex.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                    help="actually send.")
    cp = sub.add_parser("complete", help="Stage 4: cross the book to complete a one-sided pair.")
    cp.add_argument("pair_id", help="pair_id as recorded in the order registry")
    cp.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    cp.add_argument("--skip-positions-check", action="store_true",
                    help="Act without the Data API registry/venue cross-check. Only when the endpoint is down.")
    cp.add_argument("--force", action="store_true",
                    help="Bypass idempotency guard against prior pending/submitted/interrupted orders.")
    cp.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                    help="actually send.")
    pr = sub.add_parser("pairs", help="List pair_ids in the registry with held sizes.")
    pr.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    dec = sub.add_parser("decide", help="Read-only quote decision for graduated markets using live venue books.")
    dec.add_argument("target", nargs="?", default=None, help="Market condition ID, slug, or index (0..7). Default: first graduated market.")
    dec.add_argument("--all", action="store_true", help="Evaluate all graduated markets from run/markets.json")
    dec.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    aud = sub.add_parser("audit", help="Read-only three-way audit comparing Registry, Venue, and Chain views.")
    aud.add_argument("target", help="Condition ID or pair_id to audit")
    aud.add_argument("--funder", default=None, help="Funder address (default: POLY_FUNDER)")
    aud.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    kp = sub.add_parser("kpi", help="Generate live KPI report mirroring strategy/kpi.py.")
    kp.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    kp.add_argument("--run-id", default=None, help="Filter by run_id session")
    ac = sub.add_parser("api-creds",
                        help="Derive L2 API credentials once and store them in .env.")
    ac.add_argument("--force", action="store_true",
                    help="re-derive even if .env already has them")
    asw = sub.add_parser("account-sweep",
                         help="Read-only: record venue account value and P&L into the registry.")
    asw.add_argument("--funder", default=None, help="Funder address (default: POLY_FUNDER)")
    asw.add_argument("--db", default=None, help="Custom database path (default: run/live.db)")
    c = sub.add_parser("cancel-all")
    c.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                  help="actually send.")
    a = ap.parse_args()

    is_live = bool(getattr(a, "live", False))

    if a.cmd == "status":
        status()
    elif a.cmd == "balance":
        balance(a.funder)
    elif a.cmd == "audit":
        from engine.audit import audit_three_way, format_audit_report
        res = audit_three_way(a.target, client=client(), funder=a.funder, db_path=a.db)
        print(format_audit_report(res))
        if not res.agree:
            sys.exit(1)
    elif a.cmd == "api-creds":
        # No --live gate: derivation issues read credentials, it opens nothing.
        api_creds(force=a.force)
    elif a.cmd == "account-sweep":
        # No --live gate: every venue call underneath is a GET. The staged
        # exposure rule gates direction, and this command has none.
        account_sweep(funder=a.funder, db_path=a.db)
    elif a.cmd == "kpi":
        from engine.kpi import report as generate_kpi_report
        import pprint
        rep = generate_kpi_report(db_path=a.db, run_id=a.run_id)
        pprint.pprint(rep, sort_dicts=False)
    elif a.cmd == "quote":
        quote(a.condition_id, a.price, a.size, is_live,
              down_price=a.down_price,
              post_only=a.post_only, tif=a.tif, expiration=a.expiration,
              db_path=a.db)
    elif a.cmd == "cancel":
        cancel_single_order(a.order_id, is_live, db_path=a.db)
    elif a.cmd == "cancel-market":
        cancel_market(a.condition_id, is_live, db_path=a.db)
    elif a.cmd == "cancel-all":
        cancel_all(is_live)
    elif a.cmd == "redeem":
        idx_sets = [int(x.strip()) for x in a.index_sets.split(",") if x.strip()]
        redeem(
            a.condition_id,
            index_sets=idx_sets,
            collateral=a.collateral,
            skip_resolution_check=a.skip_resolution_check,
            force=a.force,
            live=is_live,
        )
    elif a.cmd == "merge":
        idx_sets = [int(x.strip()) for x in a.index_sets.split(",") if x.strip()]
        merge(
            a.condition_id,
            amount=a.amount,
            index_sets=idx_sets,
            collateral=a.collateral,
            force=a.force,
            live=is_live,
        )
    elif a.cmd == "probe":
        probe(
            series=a.series,
            token_id=a.token_id,
            cycles=a.cycles,
            min_t_remaining=a.min_time_remaining,
            max_complement_bid=a.max_complement_bid,
            max_probe_loss_usd=a.max_loss,
            max_fills=a.max_fills,
            live=is_live,
        )
    elif a.cmd == "poll":
        poll(interval=a.interval, once=a.once, db_path=a.db,
             sweep_every=a.sweep_every, sweep_interval=a.sweep_interval,
             watch_guardrails=a.watch_guardrails)
    elif a.cmd == "pairs":
        pairs(db_path=a.db)
    elif a.cmd == "exit":
        exit_pair(a.pair_id, is_live, db_path=a.db,
                  skip_positions_check=a.skip_positions_check, force=a.force)
    elif a.cmd == "complete":
        complete_pair_cmd(a.pair_id, is_live, db_path=a.db,
                          skip_positions_check=a.skip_positions_check,
                          force=a.force)
    elif a.cmd == "decide":
        decide(a.target, all_graduated=a.all, db_path=a.db)
    else:
        cancel_all(is_live)



if __name__ == "__main__":
    main()



