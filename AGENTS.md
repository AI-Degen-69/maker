# AGENTS.md — polymarket-maker

## What this repo is

A maker strategy on the same market. It rests bids on BOTH outcomes rather than crossing, aiming to earn the spread and stay inventory-balanced, and holds to resolution. As a maker it pays no taker fee.

**Measured against:** spread capture versus adverse selection, fill rate against real queue depth, and inventory balance — hedged markets have measured +\0.70/market versus -\0.95 when badly unbalanced.

Simulation only. It never places a real order.

## Non-negotiable: keep the research log current

Any commit touching `strategy/` or `server/` MUST also update `research/`
in the same commit. A pre-commit hook enforces this — it is not a convention
you can quietly skip.

Run once after cloning (git does not install hooks automatically):

    bash scripts/setup-hooks.sh

Update all four files together:

| file | content |
|---|---|
| `research/RESEARCH_LOG.md` | Question → Method → Result → Verdict |
| `research/RESEARCH_SUMMARY.md` | one dated bullet per concrete thing done |
| `research/he_RESEARCH_LOG.md` | Hebrew mirror of the log |
| `research/he_RESEARCH_SUMMARY.md` | Hebrew mirror of the summary |

Conventions:
- Verdict is a decision, not a summary: `DEAD` / `PARKED` / `LIVE` / `OPEN`
- Negative results are kept, never deleted — most of what this project learned
  came from things that did not work
- Numbers are measured, not estimated; if a figure is an estimate, say so
- Instrumentation bugs get their own entry. On this project they have
  repeatedly been the difference between a real finding and a fake one
- Hebrew mirrors the English; it is not an independent document

Escape hatch for typos and formatting: `git commit --no-verify`.

## Layout

    strategy/   engine
    server/     dashboard.py (API + app) + kanban.py (page)
    research/   the five files above
    deploy/     container entrypoint + preflight

The sibling repo `polymarket-taker` uses the SAME layout. Keep it that way — the only
difference between the repos should be strategy-specific.

## Safety

- **Never place a real order.** Paper simulation only.
- Hosted credentials are placeholders. Never deploy a real `PRIVATE_KEY`.
- Deploy only to a **non-US region**: Binance returns HTTP 451 to US IPs, and
  the preflight will refuse to start rather than run blind.
- **Changing strategy parameters invalidates the current sample.** Archive the
  database and start a fresh run rather than mixing configs in one dataset.
- Run one instance at a time. Concurrent bots writing one database sum their
  independent inventories into silently invalid data.
