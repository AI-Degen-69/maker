```markdown
# Implementation Plan: Visual Order Depth & Mid-Price Telemetry UI

## Overview
Refactor the Fleet Table dashboard to replace the static `COMMITTED` column with an active market order depth component (`MarketOrdersCell`). This will expose real-time Mid-Prices, active order limits (YES/NO split), order quantities (shares), and allocated capital via a dynamic visual bar.

---

## Technical Scope

### 1. Data Pipeline & Telemetry Updates
- **File:** `telemetry/dashboard_bridge.py`
- **Task:** Extend the per-market telemetry payload sent via WebSocket/SSE to include live order details and mid-price metrics.
- **Payload Schema Spec:**
  ```json
  {
    "market_id": "string",
    "mid_price": 0.50,
    "committed_usd": 126.0,
    "yes_bid": {
      "price": 0.48,
      "shares": 131.25,
      "amount_usd": 63.0
    },
    "no_bid": {
      "price": 0.48,
      "shares": 131.25,
      "amount_usd": 63.0
    }
  }

```

---

### 2. UI Component Implementation

* **File:** `frontend/src/components/MarketOrdersCell.jsx`
* **Task:** Create the visual depth cell component.

```jsx
import React from 'react';

export function MarketOrdersCell({ market }) {
  const { mid_price = 0, yes_bid, no_bid, committed_usd = 0 } = market;
  
  const yesUsd = yes_bid?.amount_usd || 0;
  const noUsd = no_bid?.amount_usd || 0;
  const totalUsd = yesUsd + noUsd;

  const yesPct = totalUsd > 0 ? (yesUsd / totalUsd) * 100 : 50;
  const noPct = 100 - yesPct;

  return (
    <div style={{ minWidth: '220px', fontFamily: 'monospace', fontSize: '12px' }}>
      {/* Top Meta: Mid Price & Total Allocation */}
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
        <span style={{ color: '#aaa' }}>
          Mid: <strong style={{ color: '#fff' }}>{(mid_price * 100).toFixed(1)}¢</strong>
        </span>
        <span style={{ color: '#fff', fontWeight: 'bold' }}>${committed_usd}</span>
      </div>

      {/* Visual Depth Bar */}
      <div style={{ 
        display: 'flex', 
        height: '14px', 
        borderRadius: '3px', 
        overflow: 'hidden', 
        backgroundColor: '#111',
        border: '1px solid #333'
      }}>
        <div 
          style={{ 
            width: `${yesPct}%`, 
            backgroundColor: '#10B981', 
            display: 'flex', 
            alignItems: 'center', 
            justifyContent: 'center',
            color: '#000',
            fontWeight: 'bold',
            fontSize: '10px',
            transition: 'width 0.3s ease'
          }}
          title={`YES Bid: ${yes_bid?.shares || 0} shares @ $${yes_bid?.price || 0}`}
        >
          {yesPct > 20 ? `YES @ ${yes_bid?.price}` : ''}
        </div>

        <div 
          style={{ 
            width: `${noPct}%`, 
            backgroundColor: '#EF4444', 
            display: 'flex', 
            alignItems: 'center', 
            justifyContent: 'center',
            color: '#fff',
            fontWeight: 'bold',
            fontSize: '10px',
            transition: 'width 0.3s ease'
          }}
          title={`NO Bid: ${no_bid?.shares || 0} shares @ $${no_bid?.price || 0}`}
        >
          {noPct > 20 ? `NO @ ${no_bid?.price}` : ''}
        </div>
      </div>

      {/* Bottom Shares & USD Breakdown */}
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '4px', fontSize: '11px' }}>
        <span style={{ color: '#34D399' }}>
          YES: {yes_bid?.shares || 0} sh (${yesUsd})
        </span>
        <span style={{ color: '#F87171' }}>
          NO: {no_bid?.shares || 0} sh (${noUsd})
        </span>
      </div>
    </div>
  );
}

```

---

### 3. Fleet Table Integration

* **File:** `frontend/src/components/FleetTable.jsx`
* **Task:**
1. Remove legacy `COMMITTED` column header and rendering logic.
2. Add `ORDER DEPTH & MID PRICE` column header.
3. Render `<MarketOrdersCell market={market} />` within the table row.



---

## Execution Steps

1. **Backend Telemetry:** Update `telemetry/dashboard_bridge.py` to aggregate active order limits directly from `quoter` instances and push the payload via WebSocket.
2. **Frontend Build:** Add `MarketOrdersCell.jsx` and refactor `FleetTable.jsx` to replace the old `COMMITTED` column.
3. **Validation & Verification:**
* Verify WebSocket telemetry emits correct mid-price and bid/ask state.
* Confirm red/green bar dynamically scales with balance shifts (e.g. 50/50 vs skewed allocations).
* Ensure performance remains smooth without UI re-render lags.