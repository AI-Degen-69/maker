# Task 1 & 2 Implementation Report: Profit-Take Supervisor

## Summary

Successfully implemented Task 1 (Config parameters) and Task 2 (Close decision) from the profit-take supervisor specification. All code is placed verbatim from the brief, and all 6 tests pass.

## Files Modified

### 1. `strategy/config.py`
- **Change**: Added two new fields to `MakerConfig` dataclass after the `gate_state` field:
  - `profit_take_fee_per_share: float = 0.017`
  - `profit_take_net_threshold: float = 0.020`
- **Location**: Lines 99-115 (inserted after `gate_state` at line 97)
- **Verification**: `python -c "from strategy.config import load; c=load(); print(c.profit_take_fee_per_share, c.profit_take_net_threshold)"` outputs `0.017 0.02` ✓

## Files Created

### 1. `strategy/profit_take.py`
- **Purpose**: Pure logic for close decision (no I/O)
- **Function**: `should_close(inv, up_bid, dn_bid, cfg) -> dict`
- **Key Logic**:
  - Identifies paired shares as `min(up_shares, down_shares)`
  - Returns early if no paired shares or missing bids (no two-sided book)
  - Calculates cost per share as `inv.avg("UP") + inv.avg("DOWN")`
  - Calculates exit per share as `up_bid + dn_bid`
  - Applies fee per share as `2.0 * cfg.profit_take_fee_per_share` (both legs)
  - Computes net per share and compares against threshold
  - Returns dict with keys: `take`, `shares`, `cost_basis`, `proceeds`, `fee`, `realized_pnl`, `why`

### 2. `tests/test_profit_take.py`
- **Contains**: 6 tests as specified in the brief
- **Test Cases**:
  1. `test_no_paired_shares_never_closes`: Verifies closure requires paired shares
  2. `test_missing_bid_never_closes`: Verifies both bids needed for two-sided book
  3. `test_move_that_only_covers_the_fees_does_not_close`: Verifies insufficient profit threshold
  4. `test_move_past_the_threshold_closes`: Verifies positive close decision
  5. `test_realized_pnl_is_proceeds_minus_cost_minus_fee`: Verifies arithmetic correctness
  6. `test_only_the_paired_portion_is_closed`: Verifies naked shares are ignored
- **Setup**: Added `sys.path.insert(0, ...)` to follow pattern of existing tests

## Test Results

### Task 2 Tests (test_profit_take.py)

```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0 -- C:\Users\Tiger\AppData\Local\Programs\Python\Python312\python.exe
cachedir: .pytest_cache
rootdir: C:\Users\Tiger\Agents\Projects\AI Trading\maker
plugins: anyio-4.14.2
collecting ... collected 6 items

tests/test_profit_take.py::test_no_paired_shares_never_closes PASSED     [ 16%]
tests/test_profit_take.py::test_missing_bid_never_closes PASSED          [ 33%]
tests/test_profit_take.py::test_move_that_only_covers_the_fees_does_not_close PASSED [ 50%]
tests/test_profit_take.py::test_move_past_the_threshold_closes PASSED    [ 66%]
tests/test_profit_take.py::test_realized_pnl_is_proceeds_minus_cost_minus_fee PASSED [ 83%]
tests/test_profit_take.py::test_only_the_paired_portion_is_closed PASSED [100%]

============================== 6 passed in 0.04s ==============================
```

### All Tests (python -m pytest tests/ -q)

```
........................................................................ [ 83%]
..............                                                           [100%]
86 passed in 1.09s
```

**Result**: 6 new tests pass, 80 existing tests still pass. Total 86 tests passing.

## Implementation Notes

### Ambiguities Resolved

1. **Path Setup in Tests**: The test file pattern from `test_gate.py` was used (explicit `sys.path.insert`) rather than assuming pytest would resolve module imports automatically.

2. **Fee Calculation**: The brief specifies "fee is 0.017 per leg and a close sells BOTH legs, so the fee per pair is 0.034". This is correctly implemented as `2.0 * cfg.profit_take_fee_per_share` applied per pair of shares.

3. **Bid vs Ask**: The implementation correctly uses BIDs (we are sellers) not ASKs, following the brief's statement: "A seller hits the bid."

4. **Dictionary Initialization**: The `NO` template dict is created once at module level for efficiency, with overrides via `dict(NO, why=why)`.

### Code Verbatim Verification

- Config fields: Lines 99-115 of `strategy/config.py` match brief exactly
- profit_take.py: Lines 1-70 match brief implementation exactly
- Test file: Lines 1-45 (after path setup) match brief exactly

## Concerns

None. The implementation:
- Follows the existing codebase patterns (seen in test_gate.py)
- Uses the Inventory.avg() interface correctly
- Correctly implements the fee and threshold logic from the brief
- All 6 tests pass and verify the required behavior
- No existing tests were broken
- No ambiguities remain in the specification

## Definition of Done Checklist

- [x] Task 1 Step 1 code is in place verbatim from the brief
- [x] Task 2 Step 3 code is in place verbatim from the brief
- [x] `tests/test_profit_take.py` exists with the six tests from the brief, verbatim
- [x] `python -m pytest tests/test_profit_take.py -v` shows 6 passed
- [x] `python -m pytest tests/ -q` shows all 86 tests passing (6 new + 80 existing)
- [x] Report written to `.superpowers/sdd/2026-07-29-profit-take-supervisor/task-A-report.md`
