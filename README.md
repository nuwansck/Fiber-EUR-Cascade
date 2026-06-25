# Fiber EUR Cascade v1.2

EUR/USD Multi-Session Conservative Cascade Bot — **base release**.

This is the foundational version of the Fiber line. Rebased from the prior
`Fiber EUR v1.6` engine with the position-sizing model reworked to fractional
risk and the entire codebase normalised to SGD.

## Strategy
- **Pair:** EUR/USD only ("Fiber")
- **Signal:** 4-Layer Cascade — all 4 layers + 2 vetoes must pass (H4 → H1 → M15 → M5)
- **Trade:** SL 15 pip | TP 25 pip | RR 1.67:1
- **Sizing:** **fractional — 2.0% of live balance per trade** (SGD-denominated)
- **Daily cap:** 6.0% of balance (3 trades × 2%)
- **Sessions (SGT):** London 16:00–20:59 | US 21:00–23:59
- **Goal:** 2 wins/day · 3 trades max · 2 per session
- **Concurrency:** single position only — hard-enforced at startup

## Sizing model
Risk is sized off the **live account balance** every scan, not a fixed dollar
figure. After a loss the next bet shrinks (2% of the smaller balance); after a
win it grows. At a 5,000 SGD balance, 15-pip SL, pip value 1.35 SGD/pip/10k:

| Risk | Risk (SGD) | Units | TP win | SL loss |
|---|---|---|---|---|
| 2.0% | ~100 | ~49,300 | ~+166 SGD (+1.67R) | ~−100 SGD (−1R) |

`max_units` is set to 75,000 so fractional sizing keeps scaling as the balance
grows past ~5,062 SGD (above which a 50k cap would have silently under-risked).

Track results in **R**, not SGD — a win is +1.67R and a loss is −1R regardless
of account size.

## Protection
- Chaos filter (daily range > 150 pips)
- Gap filter (open gaps > 50 pips)
- ATR gate (H1 ATR < 4.0 pips)
- News filter (economic calendar block window)
- 30-min cooldown after SL hit
- Circuit breaker — 2 consecutive SL → 2-day pause (with smart H4 flip override)
- Margin guard with auto-scale-down

## Deployment
Railway container with persistent volume at `/data`.

```
OANDA_API_KEY=...
OANDA_ACCOUNT_ID=101-003-XXXXXXX-001
OANDA_DEMO=true
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Files
| File | Purpose |
|---|---|
| `main.py` | Railway entry point, polling loop, scheduled reports |
| `bot.py` | Trade engine, signal dispatch, SL/TP detection |
| `signals.py` | 4-Layer Cascade signal engine |
| `oanda_trader.py` | OANDA v20 API: login, orders, trade-level close |
| `risk.py` | Fractional sizing, margin guard, daily risk cap |
| `reconcile_state.py` | Sync local state with broker on startup/redeploy |
| `reporting.py` | Daily/weekly/monthly Telegram reports |
| `database.py` | SQLite trade history |
| `calendar_filter.py` | Economic calendar news filter |
| `settings.json` | All configuration — single source of truth |

## Changes
See [CHANGELOG.md](CHANGELOG.md).
