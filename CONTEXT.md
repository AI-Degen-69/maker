# CONTEXT.md — Spread Hunter

The shared domain language for this repo. When a ticket, plan, test, or hypothesis names a concept, use the term defined here. If a term you need is missing, that is either invented language or a real gap — note it for `/domain-modeling`.

## Strategy

- **Maker** — the strategy rests bids on BOTH outcomes of a binary market rather than crossing, earning the spread and staying inventory-balanced, and holds to resolution. As a maker it pays no taker fee.
- **Reward market** — a market funded by the venue's liquidity-reward program: pays rent for RESTING size, sampled once a minute, filled or not. Priced off the reward window (`rewardsMaxSpread`, `rewardsMinSize`) and floored by the minimum payout.
- **Spread market** — an unfunded market that trades anyway: income is the spread captured on fills, priced off the live book. No distribution floor applies.
- **Fleet** — the multi-market engine: one process sweeping many markets, reallocating capital between them on a water-fill over each market's projected income.
- **Allocator** — the module that decides how much capital each market gets: first-dollar marginal return compared to a floor, with a per-market concentration cap.
- **Market sweep** (or **sweep**) — one pass of the fleet engine over ONE market: read the book, gate it, decide quotes, act, record. The per-market unit of engine work.
- **Book gate** — the filter that refuses a market whose book is too wide, too thin, or one-sided; the first decision a sweep makes.
- **Inventory balance** — the target state of holding both outcomes in proportion to their prices so the pair costs ~1.00 and exposure is hedged.
- **Fill** — a resting order that traded. **Verified fill** — one backed by a trade-tape print; **unverified** — inferred from a book delta.
- **Markout** — the P&L per share measured from fill price to a later mid, the maker's edge instrument.
- **Spread capture** — the theoretical income of a spread market: volume × spread × capture fraction, converted to the same $/day pot the allocator compares.
- **Settlement** — gasless redemption and merging of resolved conditional-token positions on-chain. ABI encoding, alt-bn128 collection/position id derivation, and EIP-712 batch signing live in `live/engine/settlement.py`; the relayer submit path stays in `live_exec` with the CLI verbs.

## Architecture (design decisions, 2026-08-10)

- **State reader** (module: `strategy/stats.py`) — the read side of the fleet DB. All SQL reads that produce KPIs or dashboard numbers live here and nowhere else; the dashboard page and the report module call it instead of writing SQL.
- **Write module** (module: `strategy/store.py`) — owns the schema, migrations, and every write; its interface is the only place the fleet records state.
- **Fetch seam** — the adapter slot where venue HTTP fetches happen (books, trades, universe listings). Fetchers take their HTTP session across the seam rather than opening connections inside scoring or decision logic.
- **Book adapter** — the parse half of the fetch seam (`strategy.markets.parse_book`): venue /book rows become typed levels in exactly one place. Contract: row-level garbage is skipped and counted in `malformed`; a structurally wrong payload raises as a fetch-shaped failure. Callers decide what the count means: the ranker fails closed (a skipped competitor under-counts `theirs` and inflates its income share), the fleet lets the book gate judge what is readable.
- **Sweep module** (planned) — the market sweep behind one interface (`sweep(state, ctx) → outcome`) with internal seams for the book-gate, decide, and act steps; the fleet loop keeps orchestration only.

## Conventions

- The payout floor and the selection gates (volume, horizon, depth, spread) are sourced from config so the ranker and the fleet cannot drift.
- Negative results are kept in `research/`; a strategy or server change ships with its research entry.
