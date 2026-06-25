"""reconcile_state.py — sync local runtime state with broker positions.

v1.6: On reconcile, also rebuild last_trade_direction, last_sl_price,
      last_tp_price, and last_trade_units from OANDA trade data so that
      timeout-close and SL/TP detection work correctly after a Railway
      restart that wipes ephemeral state.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging

log = logging.getLogger(__name__)


def reconcile_state_with_broker(trader, state: dict, instrument: str = "EUR_USD") -> dict:
    """Ensure local open_times reflects OANDA open position status.

    When a trade exists on OANDA but not locally (e.g. after redeploy),
    this rebuilds the essential state keys so timeout-close and SL/TP
    detection don't break.
    """
    state.setdefault("open_times", {})
    pos = trader.get_position(instrument)
    if pos:
        if instrument not in state["open_times"]:
            trade_id, open_time = trader.get_open_trade_id(instrument)
            state["open_times"][instrument] = open_time or datetime.now(timezone.utc).isoformat()
            log.warning("State reconciled: broker has open %s trade; local state was missing it", instrument)

            # Rebuild trade metadata from OANDA so timeout-close and
            # detect_sl_tp_hits work after a container restart.
            _rebuild_trade_state(trader, state, instrument, trade_id)
    else:
        if instrument in state["open_times"]:
            state["open_times"].pop(instrument, None)
            log.warning("State reconciled: local %s open trade removed; broker has none", instrument)
    state["has_open_trade"] = bool(state.get("open_times"))
    return state


def _rebuild_trade_state(trader, state: dict, instrument: str, trade_id: str | None) -> None:
    """Fetch open trade details from OANDA and populate state keys."""
    if not trade_id:
        return
    try:
        import requests
        url = f"{trader.base_url}/v3/accounts/{trader.account_id}/trades/{trade_id}"
        r = requests.get(url, headers=trader.headers, timeout=10)
        if r.status_code != 200:
            return
        trade = r.json().get("trade", {})
        units = int(float(trade.get("currentUnits", trade.get("initialUnits", 0))))
        direction = "BUY" if units > 0 else "SELL"

        state["last_trade_direction"] = direction
        state["last_trade_units"] = abs(units)
        state["last_reconciled_trade_id"] = trade_id

        # Extract SL and TP prices from attached orders
        sl_order = trade.get("stopLossOrder", {})
        tp_order = trade.get("takeProfitOrder", {})
        if sl_order:
            state["last_sl_price"] = float(sl_order.get("price", 0))
        if tp_order:
            state["last_tp_price"] = float(tp_order.get("price", 0))

        log.info("Reconcile rebuilt: %s %s %d units | SL=%.5f TP=%.5f | trade_id=%s",
                 instrument, direction, abs(units),
                 state.get("last_sl_price", 0), state.get("last_tp_price", 0),
                 trade_id)
    except Exception as e:
        log.warning("Reconcile rebuild error for %s: %s", instrument, e)
