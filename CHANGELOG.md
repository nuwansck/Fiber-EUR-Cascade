# Changelog — Fiber EUR Cascade

## v1.2 — 2026-06-25 — Settings centralization + config cleanup

Configuration plumbing pass. **No strategy or trading-logic change** — every
effective value is identical to before (verified by parity check). Strategy
items (gap/chaos filters) are intentionally deferred to a later step.

- **Recursive settings merge.** `load_settings()` now deep-merges `settings.json`
  over `_DEFAULT_SETTINGS` instead of a top-level `update()`. Partial nested
  blocks (e.g. a `pair_sl_tp` with one sub-key omitted) now backfill missing
  sub-keys from defaults rather than being dropped wholesale.
- **`min_atr_pips` now reads from its real home.** It lives in the `filters`
  block but was being read from `signal_params` (via `cfg`), so it only ever used
  the hardcoded `4.0` fallback. Added `_filters_cfg()` and pointed the ATR-veto
  read at `filters`. Value is unchanged (4.0), but the setting now actually takes
  effect when edited.
- **Dead/ignored config removed or wired.** Removed vestigial keys that no code
  read: `min_rr_ratio` (RR is fixed at 25/15 = 1.67 by construction, so the gate
  could never fire) and `news_relevant_currencies` (EUR/USD relevance is fixed for
  a single-pair bot). Wired `news_filter_enabled` — the news blackout can now be
  toggled via settings (honours the existing `true`, behaviour-neutral).
- **Gap filter dropped; chaos guard reserved.** `filters.max_gap_pips` removed and
  the gap line removed from the docstring — intraday EUR/USD does not gap and the
  bot already avoids the weekend. `filters.chaos_pips` is retained as a reserved
  threshold for a planned volatility guard (not yet implemented); its docstring
  line was removed so the bot no longer advertises a protection it does not run.
- **Fallback defaults reconciled to the file.** Aligned code defaults that
  disagreed with `settings.json`: `max_trades_day` (4→3, bot.py/main.py) and
  `http_timeout_sec` (30→15, reporting.py). Behavior-neutral (the file values
  already won); removes the trap where a missing key would change behavior.
  Intentional fail-safe sentinels (the `0` defaults in risk/startup validation)
  were deliberately left as-is.
- Cleanup: removed unused `msg_order_failed` import; corrected stale docstring
  ("4 trades max" → "3 trades max", matching `max_trades_day: 3`).

---

## v1.1 — 2026-06-25 — Currency display revert (cosmetic)

- **Telegram + report displays reverted from `S$` back to plain `$`.** Account
  remains SGD-denominated and all values are unchanged; this is a label-only
  change per preference (the `$` glyph is treated as the account-currency symbol).
  No sizing, risk, or logic change from v1.0.
- Version bumped to **Fiber EUR Cascade v1.1** across module headers, banner,
  startup card, `version.py`, and `settings.json` (`config_version: "1.1"`).

---

## v1.0 — 2026-06-25 — Base release (Fiber EUR Cascade)

Foundational release. Rebased from `Fiber EUR v1.6`; the 4-Layer Cascade signal
engine, vetoes, session logic, circuit breaker, news filter, and reporting are
carried over unchanged. The changes below are the rebase delta.

### Position sizing — switched to fractional risk
- **2.0% of live balance per trade** (`risk_model: "fractional"`,
  `risk_pct_per_trade: 0.02`). Sizing now reads the live OANDA balance each scan
  via `build_risk_plan(..., equity=...)`, so risk auto-de-risks after a loss and
  scales up after a win. Flat `risk_per_trade_sgd` retained as a fallback only.
- **Daily cap is now fractional too** — `daily_risk_cap_pct: 0.06` (3 × 2%),
  resolved from live balance each scan (`resolve_daily_cap`).
- **`max_units` raised 50,000 → 75,000** so 2% sizing keeps scaling past
  ~5,062 SGD balance instead of silently clipping at the old cap.

### SGD normalisation
- Account is SGD-denominated; all risk/PnL keys, CSV columns, and Telegram
  displays relabelled from USD to SGD (`S$` glyph). Renamed: `risk_per_trade_usd`
  → `risk_per_trade_sgd`, `daily_risk_cap_usd` → `daily_risk_cap_sgd`,
  `capital_usd` → `capital_sgd`, history keys `realized_pnl_usd` →
  `realized_pnl_sgd`, `estimated_risk_usd` → `estimated_risk_sgd`, CSV `pl_usd`
  → `pl_sgd`, state `daily_risk_used_usd` → `daily_risk_used_sgd` (old keys read
  as fallback for continuity).
- `pip_value_per_10k` confirmed at **1.35** (SGD pip value per 10k units).

### Config reconciliation
- **`max_trades_day` 4 → 3** to match the 3-entry daily risk cap (the old "4"
  was unreachable fiction once the cap bound at 3).
- Startup validation now checks `risk_pct_per_trade` (0–5% hard ceiling) and
  `daily_risk_cap_pct` (≤15% hard ceiling) instead of flat USD amounts.

### Cosmetic
- Renamed to **Fiber EUR Cascade v1.0** across all module headers, the Telegram
  banner, startup card, reports, `version.py`, and `settings.json`.
- Startup card now shows fractional risk (`2.0%/trade (~S$100)`), the sizing
  model line, and the 75k unit ceiling.

---

## Lineage (pre-rebase)

### Fiber EUR v1.6 — 2026-06-24
- Fixed timeout-close reject storm (trade-level close cancels attached TP/SL).
- Fixed local state loss on Railway redeploy (reconcile rebuilds trade context).
- Closed-trade detection without local state; single OandaTrader per cycle;
  login-failure backoff; SGD account denomination corrected.

### Fiber EUR v1.5 — 2026-05-17
- Initial release: 4-Layer Cascade engine, risk-based sizing, London+US
  sessions, circuit breaker with smart H4 flip, news filter, scheduled reports,
  SQLite history, margin guard.
