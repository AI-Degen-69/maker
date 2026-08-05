
# Implementation Plan: Visual Order Depth & Mid-Price Telemetry UI (v2 — Eng Reviewed)## Overview
Refactor the Fleet Table dashboard (`server/fleet_dash.py`) to replace the static `COMMITTED` column with an active market order depth component (`depthCell`). 

This refactoring:1. Adapts directly to the existing codebase domain conventions (`UP`/`DOWN` instead of `YES`/`NO`).2. Leverages existing row building and live telemetry structures (`mid_up`, `up_bid`, `up_ask`, `our_up`, `our_dn_as_up`).3. Retains the existing vanilla JS `innerHTML` tick rendering mechanism without adding external dependencies or complex DOM diffing loops.4. Cleans up legacy dead code (`ladder()`) and adds comprehensive test coverage.

---## Technical Scope & Decisions### Key Architectural & Engineering Decisions- **Domain Alignment:** Use `UP` / `DOWN` nomenclature throughout telemetry schemas and UI functions to match `fleet_dash.py`.- **Render Strategy (D3):** Retain `innerHTML` updates on the 4-second tick loop. Avoid DOM diffing frameworks or manual DOM mutations.- **Dead Code Cleanup (T0):** Delete the uncalled legacy `ladder()` function at line 959 in `server/fleet_dash.py` to prevent structural ambiguity.- **Component Isolation (T3):** Extract row cell rendering out of the 12-line inline string template into a modular helper function `depthCell(m)`.- **Test Matrix (T7):** Implement Python integration and logic tests in `test_dashboard_page.py` covering all telemetry states.

---## 1. Data Pipeline Extension (T1)- **File:** `server/fleet_dash.py`- **Location:** Lines 497-521 (Row Builder Dictionary)- **Task:** Extend the per-market telemetry payload dictionary to expose explicit per-side share counts and committed USD amounts alongside existing prices.### Extended Payload Schema:```python
{
    "market_id": market_id,
    "mid_up": mid_up,  # existing
    "up_bid": up_bid,  # existing
    "up_ask": up_ask,  # existing
    "our_up": our_up,  # existing
    "our_dn_as_up": our_dn_as_up,  # existing
    "committed": total_committed_usd,  # existing
    # NEW EXTENSIONS:
    "up_shares": float(our_up_shares),
    "up_usd": float(our_up_usd),
    "dn_shares": float(our_dn_shares),
    "dn_usd": float(our_dn_usd),
    "mid_stale_s": int(stale_seconds)
}
2. Frontend Implementation & Refactoring (T3 & T4)
File: server/fleet_dash.py
Location: Embedded Client Script (<script> block)
Dead Code Cleanup (T0):
Remove unused function ladder() (~line 959).
Modular Cell Rendering Function (depthCell):
Extract and append depthCell(m) above the tick() function:
JavaScript

function depthCell(m) {
  const upUsd = m.up_usd || 0;
  const dnUsd = m.dn_usd || 0;
  const totalUsd = upUsd + dnUsd;

  // State 1: Zero Allocation / Exited
  if (totalUsd === 0) {
    return `<div class="depth-cell-empty"><span class="dim">EMPTY</span></div>`;
  }

  const upPct = Math.min(Math.max((upUsd / totalUsd) * 100, 0), 100);
  const dnPct = 100 - upPct;
  const midDisplay = (m.mid_up !== null && m.mid_up !== undefined) 
    ? (m.mid_up * 100).toFixed(1) + '¢' 
    : 'N/A';
  const isStale = m.mid_stale_s > 120;

  return `
    <div class="depth-cell-container" style="min-width: 210px; font-family: monospace; font-size: 11px;">
      <!-- Meta Header: Mid Price & Total Capital -->
      <div style="display: flex; justify-content: space-between; margin-bottom: 2px;">
        <span class="${isStale ? 'dim' : ''}">Mid: <strong>${midDisplay}</strong></span>
        <span style="font-weight: bold; color: #fff;">$${m.committed}</span>
      </div>

      <!-- Depth Bar -->
      <div style="display: flex; height: 12px; border-radius: 2px; overflow: hidden; background: #111; border: 1px solid #333;">
        <div style="width: ${upPct}%; background-color: #10B981; color: #000; font-weight: bold; font-size: 9px; display: flex; align-items: center; justify-content: center;"
             title="UP: ${m.up_shares || 0} sh @ $${m.our_up || 0}">
          ${upPct >= 20 ? 'UP @ ' + (m.our_up || 0) : ''}
        </div>
        <div style="width: ${dnPct}%; background-color: #EF4444; color: #fff; font-weight: bold; font-size: 9px; display: flex; align-items: center; justify-content: center;"
             title="DOWN: ${m.dn_shares || 0} sh @ $${m.our_dn_as_up || 0}">
          ${dnPct >= 20 ? 'DN @ ' + (m.our_dn_as_up || 0) : ''}
        </div>
      </div>

      <!-- Sub Meta: Shares & USD Details -->
      <div style="display: flex; justify-content: space-between; margin-top: 2px; font-size: 10px;">
        <span style="color: #34D399;">UP: ${m.up_shares || 0} sh ($${upUsd})</span>
        <span style="color: #F87171;">DN: ${m.dn_shares || 0} sh ($${dnUsd})</span>
      </div>
    </div>
  `;
}
Table Column Replacement:
Replace the existing COMMITTED <th> and <td> with ORDER DEPTH & MID PRICE:
Header: <th>ORDER DEPTH & MID PRICE</th>
Body Row Cell: <td>${depthCell(m)}</td>
3. Test Suite Integration (T7)
File: tests/test_dashboard_page.py
Task: Add explicit test cases for telemetry payload generation and row rendering edge cases.
Test Matrix:
Balanced Market: Verifies 50/50 split rendering between UP and DOWN.
Single-Sided (UP only / DOWN only): Verifies 100% single color bar without layout overflow.
Empty / Exited Market (totalUsd == 0): Verifies EMPTY label fallback.
Null Mid-Price: Validates 'N/A' fallback without crashing rendering script.
Stale Mid-Price (mid_stale_s > 120): Verifies dimming CSS class application.
Execution Steps
Step 0 — Cleanup: Delete ladder() function in server/fleet_dash.py.
Step 1 — Backend Data: Update row builder in server/fleet_dash.py to extract and output up_shares, up_usd, dn_shares, dn_usd, and mid_stale_s.
Step 2 — Frontend Component: Add depthCell(m) function and update table columns inside the HTML template string.
Step 3 — Tests & Verification: Run pytest tests/test_dashboard_page.py to validate all 5 state scenarios.