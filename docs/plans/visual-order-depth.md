# Implementation Plan: Visual Order Depth & Mid-Price Telemetry UI

## Overview
Replace the static `Committed` column in the Fleet table with a live capital-split cell
showing the mid price, the YES/NO split of resting capital, and per-side share counts and
prices. The mid-price label rides the boundary between the two colored segments, so as
capital shifts between sides the number physically tracks the move.

**Stack correction (design review, 2026-08-05).** The original draft targeted
`frontend/src/components/*.jsx` and `telemetry/dashboard_bridge.py`. Neither path exists.
The dashboard is a single FastAPI module, `server/fleet_dash.py` (1362 lines): a `PAGE`
HTML string literal (line 758) with vanilla-JS template rendering (line 1322), served at
`/`. There is no React, no build step, no `frontend/` directory, and no `telemetry/`
package. All work below lands in `server/fleet_dash.py`.

---

## What already exists

`server/fleet_dash.py` carries an unwritten but strict design system. Reuse it; do not
introduce new values.

| Need | Existing token | Line |
|---|---|---|
| YES / gain | `var(--up)` `#33c9b5` | 768 |
| NO / loss / risk | `var(--down)` `#f0684d` | 769 |
| Bar trough | `var(--line-soft)` `#1a2029` | 766 |
| Dim text | `var(--tx-dim)` `#8792a6` | 767 |
| Faint text / ticks | `var(--tx-faint)` `#535e70` | 767 |
| Numeric typeface | `var(--mono)` IBM Plex Mono | 775 |
| Small radius | `var(--r-sm)` 5px | 773 |
| Progress bar idiom | `.gauge-track` / `.gauge-fill` | 841-842 |
| Ranked bar idiom | `.rank-track` / `.rank-fill` | 832-833 |
| Currency formatter | `usd(v, dp)` | in `<script>` |
| Tabular numerals | `.num` | 786 |

The page already uses a horizontal fill bar three times (wallet gauge, sample gauge, market
rank rail). The depth bar is the fourth instance of an established idiom, not a new one.
Match `.gauge-fill`'s `transition:width .4s ease`.

---

## 1. Data pipeline

**File:** `server/fleet_dash.py` — the per-market row builder that currently emits
`"committed"` (around line 497).

Extend each market dict in the snapshot with resting-order detail. Both `yes` and `no` are
nullable — a market with no resting order on a side emits `null`, never a zero-filled object.

```json
{
  "market_id": "string",
  "mid_price": 0.48,
  "mid_stale_s": 0,
  "committed": 126.0,
  "yes": { "price": 0.48, "shares": 131.25, "usd": 63.0 },
  "no":  { "price": 0.48, "shares": 131.25, "usd": 63.0 }
}
```

- `mid_price` is `null` when unknown, never `0`. Zero is a real price on a prediction
  market; using it as a sentinel renders a market as `0.0¢` on every first paint.
- `mid_stale_s` is seconds since the mid was last refreshed. The page already distinguishes
  LIVE from STALE at the fleet level (`healthy`, line ~1317); this extends it per market.
- `committed` is unchanged and still feeds the wallet gauge and the Capital Deployed KPI.
  Do not repurpose or rename it.

---

## 2. The cell

Three tiers, top to bottom. Column width target **≤190px**; row height target **≤62px**.

```
            Mid 48.0¢                <- rides the YES/NO boundary
  [======= green =======|== red ==]  <- capital split, 9px
  Y $63: 131 Sh @ 48.0¢              <- YES leg
  N $63: 131 Sh @ 48.0¢              <- NO leg
```

**Leg format (locked):** `$<usd>: <shares> Sh @ <price>¢`, IBM Plex Mono, 11px,
`font-variant-numeric: tabular-nums`. `Y` prefix in `var(--up)`, `N` prefix in `var(--down)`.
One line per side, stacked. Side-by-side was measured at ~300px and pushes Realized P&L,
Score share, Uptime and Fills into horizontal scroll on a 1440px viewport.

**Mid label positioning (locked).** The label sits above the point where the green segment
ends and the red begins, and moves with it.

```js
const yesUsd = m.yes ? m.yes.usd : 0;
const noUsd  = m.no  ? m.no.usd  : 0;
const total  = yesUsd + noUsd;
const yesPct = total > 0 ? (yesUsd / total) * 100 : null;   // null, never 50

// label clamps so it cannot overflow the cell; the tick stays truthful
const labelPct = yesPct === null ? 50 : Math.min(Math.max(yesPct, 14), 86);
```

- Label: `position:absolute; left:<labelPct>%; transform:translateX(-50%)`.
- A 1px `var(--tx-faint)` tick sits at the **true** `yesPct`, unclamped. When the split is
  extreme enough to clamp the label, the tick still marks the real boundary.
- Both label and tick carry `transition:left .4s ease`, matching `.gauge-fill`.

**Why the label moves.** A price centered over a dollar-split bar implies the price marks a
point on that bar, which it does not. Anchoring the label to the boundary makes the position
mean something: it points at where the capital divides, and shows the mid at that moment.

---

## 3. Interaction states

Every state below renders on the live dashboard today. The cell must specify all of them.

| State | Bar | Mid label | Legs |
|---|---|---|---|
| Both sides resting | green + red at true ratio | at boundary, full brightness | both legs |
| One side only | single full-width segment | clamped to 14% or 86%, tick at edge | funded leg; other reads `N none resting` in `--tx-dim` |
| **No resting orders** | **empty trough, no segments** | centered, `--tx-dim` | single line: `no resting orders` |
| EXITED market | empty trough | centered, `--tx-dim` | single line: `orders pulled on exit` |
| Stale mid (`mid_stale_s > 120`) | segments at `opacity:.35` | `stale Nm` in `--alert` beside the price | legs dimmed |
| First paint / `mid_price === null` | empty trough | `Mid —` | `—` |

The zero-order state is the one the original draft got wrong. `yesPct = total > 0 ? … : 50`
renders a confident 50/50 green-and-red bar for a market holding no orders at all —
identical to a perfectly balanced market. Markets sit in this state routinely (spread too
thin, not scoring, post-exit). A capital display that shows capital which does not exist is
worse than no display. `yesPct` must be `null` and the bar must render empty.

---

## 4. Rendering: in-place updates required

`server/fleet_dash.py:1322` rebuilds the entire table body every tick:

```js
$('rows').innerHTML = s.markets.map(m => { ... }).join('');
// line 1357: setInterval(tick, 4000)
```

Every node is destroyed and recreated on a 4-second cadence, so **no CSS transition on any
cell can ever fire**. A moving mid label is the point of this feature; it cannot work under
`innerHTML` replacement.

Change the tick to update in place:

1. Key each row: `<tr data-mid="${esc(m.market_id)}">`.
2. On tick, diff `s.markets` against existing rows. Build/remove rows only when the market
   set changes.
3. For existing rows, assign to `.textContent` and `.style.width` / `.style.left` directly.
4. Then `transition: width .4s ease, left .4s ease` animates, matching the wallet gauge.

This is a prerequisite, not a nice-to-have. Ship it in the same change or the bar snaps.

---

## 5. Column change

Remove the `Committed` header (line 938) and its cell (line 1346); add
`Capital split & mid price`.

Header wording: **not** "Order depth". Order depth means the market's book. This shows our
own resting orders. Mislabeling a trading concept on a trading dashboard costs trust that is
expensive to earn back.

`committed` remains in the payload and keeps feeding the wallet gauge and the Capital
Deployed KPI. What is lost is the ability to scan committed dollars straight down a
right-aligned tabular column. The per-side dollar figures in the legs partly replace it, but
they do not right-align across rows. Accepted, and recorded as unresolved below.

---

## 6. Accessibility

- Share counts and prices are **always-visible text**, never `title=` only. The original
  draft put them exclusively in tooltips, unreachable by touch, keyboard, and screen reader.
- Leg text at 11px is the numeric exception already used across this table (`.legs`,
  `.rank-row`). Contrast: `var(--up)` on `var(--panel)` = 7.4:1, `var(--down)` on
  `var(--panel)` = 5.1:1. Both pass.
- No text inside the colored segments. `#000` on `#33c9b5` at 10px bold fails contrast, and
  a 20%-wide segment is ~40px against a ~60px label, so it clips.
- The bar is decorative given the legs carry every number: `aria-hidden="true"` on the bar,
  and the row's numbers are readable in DOM order without it.
- No new focusable elements, so keyboard order is unchanged.

---

## NOT in scope

- **Real order-book depth.** This shows our own resting orders only. Rendering the market's
  book is a separate feature with a separate data source.
- **Click-to-cancel or any order interaction.** The cell is read-only. Making orders
  actionable from the fleet table is a distinct change with its own confirmation design.
- **Mobile layout.** The fleet table is already `overflow-x:auto` (`.wrap`, line 864) with
  ten columns and has never had a mobile treatment. Not regressing it; not fixing it here.
- **Sparkline of mid over time.** Interesting, doubles the row height, no stated need.
- **Writing DESIGN.md.** The tokens are real and consistent but undocumented. Worth doing;
  not part of this change.

---

## Eng review corrections (2026-08-05)

**Read this before T1. Three of the plan's premises are wrong.**

**1. `ladder()` already exists and is dead code.** `fleet_dash.py:959-985` defines a
per-market visualization that renders the mid, the bid/ask, and *our own resting orders*
positioned along a price axis. It uses the exact `left:${pct}%; transform:translateX(-50%)`
technique §2 specifies. It is defined and **never called**. Decide what to do with it before
building a second one:

```js
function ladder(m){
  const mid=m.mid_up, bid=m.up_bid, ask=m.up_ask;
  if(mid==null||bid==null||ask==null) return '<span class="dim">No two-sided book</span>';
```

Note it already answers the "price axis vs capital split" question the opposite way — it
places our orders at their true prices. That was a deliberate prior decision; this plan
reverses it. Reversing is allowed, but do it knowingly.

**2. Most of the payload already ships.** `fleet_dash.py:519-521`:

```python
"up_bid": live.get("up_bid"), "up_ask": live.get("up_ask"),
"mid_up": live.get("mid_up"), "our_up": live.get("our_up"),
"our_dn_as_up": live.get("our_dn_as_up"),
```

`mid_price` in §1 is `mid_up`. Our per-side order *prices* are `our_up` and `our_dn_as_up`.
Do not invent new field names for existing data. T1 shrinks to: add per-side **share counts
and USD**, plus `mid_stale_s`. Prices and mid are already there.

**3. This codebase says UP/DOWN, not YES/NO.** Every field is `up_*` / `dn_*`;
`naked_side` holds `"UP"` / `"DOWN"` (`:527`, `:540`). The plan's YES/NO naming is foreign
to the file. Use UP/DOWN in code and payload. The user-facing leg labels stay whatever reads
best on the dashboard, but the data layer must match its neighbors.

**Decisions locked this review:**
- **T2 is cancelled.** `innerHTML` at `:1322` stays. No render-loop rewrite, no hand-rolled
  keyed differ, no new JS dependency. Ten rows every four seconds was never a bottleneck,
  and nobody should later sell a render rewrite as a performance win.
- **Drop `transition: left .4s ease` from §2.** With `innerHTML` the node is recreated each
  tick, so the label snaps to its new boundary position like every other number on the page.
  The boundary anchoring survives; only the slide goes.
- **Extract `depthCell(m)`** as a named function above `tick()`. The row template at
  `:1343-1354` is already a template literal with nested ternaries; six more states inline
  would make it unmaintainable.

---

## Implementation Tasks

- [ ] **T0 (P1, human: ~30min / CC: ~5min)** — `server/fleet_dash.py` — Decide the fate of `ladder()`
  - Surfaced by: Architecture — `:959-985` is an unreferenced implementation of adjacent functionality
  - Files: `server/fleet_dash.py:959`
  - Verify: either it is wired up, deleted, or the plan states why a second visualization is right
- [ ] **T1 (P1, human: ~1h / CC: ~10min)** — `server/fleet_dash.py` — Add per-side share counts + USD + `mid_stale_s` (prices and mid already ship)
  - Surfaced by: §1 — payload must distinguish "no order" from "zero-value order"
  - Files: `server/fleet_dash.py` (row builder, ~line 497)
  - Verify: hit the state endpoint; a non-scoring market returns `"yes": null`, not `{"usd": 0}`
- [ ] ~~**T2** — Convert row rendering to keyed in-place update~~ — **CANCELLED** (eng review D3). `innerHTML` stays; animation dropped instead of rewriting the render loop.
- [ ] **T3 (P1, human: ~2h / CC: ~15min)** — `server/fleet_dash.py` — Build the cell with all six states from §3
  - Surfaced by: §3 — five of six states unspecified; the zero-order state rendered a false 50/50 bar
  - Files: `server/fleet_dash.py` (PAGE `<style>` + row template)
  - Verify: force a market to zero resting orders; the bar renders empty, not 50/50
- [ ] **T4 (P2, human: ~1h / CC: ~10min)** — `server/fleet_dash.py` — Boundary-anchored mid label with clamp + truthful tick
  - Surfaced by: §2 — a centered price over a dollar bar implies a position it does not have
  - Files: `server/fleet_dash.py` (PAGE `<style>`, row template)
  - Verify: a 95/5 split clamps the label inside the cell while the tick sits at 95%
- [ ] **T5 (P2, human: ~30min / CC: ~5min)** — `server/fleet_dash.py` — Swap column header, remove `Committed` cell
  - Surfaced by: §5 — "Order depth" names the market's book, not our resting orders
  - Files: `server/fleet_dash.py:938`, `:1346`
  - Verify: wallet gauge and Capital Deployed KPI still read correctly after removal
- [ ] **T6 (P3, human: ~2h / CC: ~15min)** — repo — Write DESIGN.md from the tokens at `fleet_dash.py:765-873`
  - Surfaced by: "What already exists" — the design system is real but unwritten, so every new component re-derives it
  - Files: `DESIGN.md` (new)
  - Verify: a new contributor can pick correct colors without reading `fleet_dash.py`

---

- [ ] **T7 (P1, human: ~3h / CC: ~20min)** — `tests/` — Cover the payload contract and all six render states
  - Surfaced by: Test review — 0/10 paths covered, in a repo with `test_dashboard_page.py`
  - Files: `tests/test_depth_cell.py` (new)
  - Verify: parametrized over the §3 matrix; the zero-order case asserts an empty bar, never 50/50; clamp asserted at 0% and 100%

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES_OPEN | 4 issues, 1 critical gap |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | ISSUES_OPEN | score: 3/10 → 8/10, 3 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

Eng review: Architecture 1 issue, Code quality 1 issue, Tests 10 gaps (1 critical),
Performance 0 issues. Scope reduced — T2 cancelled, T1 shrunk, T0 and T7 added.
Outside voice (Codex) NOT run: session context exhausted.

**VERDICT:** DESIGN CLEARED at 8/10. ENG NOT CLEAR — three plan premises were wrong
(`ladder()` duplication, fields that already ship, UP/DOWN vs YES/NO naming) and are
corrected above but unverified against a build. Re-run eng review after T0 is decided.

**UNRESOLVED DECISIONS:**
- `ladder()` at `:959-985` is dead code implementing adjacent functionality with the
  opposite price-axis philosophy. Wire up, delete, or justify a second visualization — T0.
- Per-side share counts and USD are still unconfirmed in the quoter. Prices and mid are
  verified present; the share/USD split is not.
- Losing the right-aligned `Committed` column removes the down-column dollar scan. No
  replacement chosen.
- Mobile/tablet behavior for a ten-column table remains unaddressed.
- Outside voice never ran, so no cross-model check exists on any of the above.
