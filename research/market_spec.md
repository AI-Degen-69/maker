# Market Spec — Polymarket BTC 5-min "Up or Down"

Only mechanics we independently verified against live data during this work.
Nothing here is inherited from third-party analysis.

## Series

- `series_slug = btc-up-or-down-5m`, on Polygon (chain_id 137)
- A new market opens every 5 minutes, continuously
- Slug encodes the window open time: `btc-updown-5m-<unix_ts>`, closing at `<unix_ts> + 300`
- Two outcome tokens per market: UP and DOWN. A winning share redeems at
  $1.00, a losing one at $0.00, so **one UP + one DOWN always pays exactly $1.00**

## Resolution

- `close >= open` on the **Chainlink BTC/USD stream**, snapshotted at the
  window open and again 300s later. Ties resolve UP.
- Not Binance, not Coinbase — the market description states this explicitly.
- **Measured lag:** the `closed` flag and `outcomePrices` settle roughly
  60–120s after the window ends. Positions can legitimately sit unresolved for
  up to ~2.5 minutes.

## Fees (`crypto_fees_v2`)

```
taker_fee = shares * 0.07 * p * (1 - p)     # USDC
maker_fee = 0                                # takerOnly: true
```

- Verified against real fills.
- Fees are **convex in price**: they vanish at the extremes and peak at 0.50.
- Breakeven for a taker buying at `p` and holding to resolution:
  `win_rate = p + 0.07 * p * (1 - p)`
  → 0.90 needs 90.63%, 0.95 needs 95.33%, 0.98 needs 98.14%
- Because the fee is convex, the fee on an averaged position is **not** the sum
  of per-fill fees. Fills must be stored individually for the maths to hold.
- Maker rebates: 20% of the taker fee pool, shared pro-rata among qualifying
  makers. Quotes must be **>=50 shares** and **within 4.5c of mid** to qualify.

## API behaviour (measured)

- **Resolution lookups must use `/events?slug=<slug>`.** `/markets?slug=` returns
  an empty list once a 5-min market ages out of that index — verified 584/584
  successful on `/events` where `/markets` silently returned nothing.
- `data-api /trades` reports each **participant's own side**, not the
  aggressor's. A maker whose bid is lifted appears as a "BUY". Aggressor
  direction is therefore **not recoverable** from that feed.
- Tick size is 0.01; `/tick-size?token_id=...` confirms `minimum_tick_size: 0.01`.
- Order book `/book?token_id=` returns full depth (50+ levels), which is what
  makes queue-position modelling possible.

## Geography

Binance returns **HTTP 451 to US IPs**. Any host running strategy code that
touches Binance must sit in a non-US region, or the feed dies silently while
health checks stay green.
