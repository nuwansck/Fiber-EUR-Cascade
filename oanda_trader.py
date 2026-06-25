"""oanda_trader.py — Fiber EUR Cascade v1.2 OANDA Trade Executor
Handles login, pricing, position management, order placement, and position close.
SL and TP are set automatically on every market order.
Supports both demo (fxpractice) and live (fxtrade) environments.
"""
import json
import logging
import os

import requests

log = logging.getLogger(__name__)

def _http_timeout() -> int:
    try:
        with open('settings.json') as _f:
            return int(json.load(_f).get('http_timeout_sec', 15))
    except Exception:
        return 15



class OandaTrader:
    def __init__(self, demo: bool = True):
        self.api_key    = os.environ.get("OANDA_API_KEY", "")
        self.account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
        self.demo       = demo
        self.base_url   = "https://api-fxpractice.oanda.com" if demo else "https://api-fxtrade.oanda.com"
        self.headers    = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }
        log.info("OANDA | Mode: %s", "DEMO" if demo else "LIVE")
        log.info("Account: '%s'", self.account_id)
        log.info("API key: '%s****'  (len=%d)", self.api_key[:8], len(self.api_key))
        log.info("Base URL: %s", self.base_url)

    def login(self) -> bool:
        """Verify API key and account ID by fetching account details.
        Returns True on success, False with detailed log on failure.
        """
        if not self.api_key:
            log.error("Login FAILED: OANDA_API_KEY is empty or not set")
            return False
        if not self.account_id:
            log.error("Login FAILED: OANDA_ACCOUNT_ID is empty or not set")
            return False
        try:
            r = requests.get(
                f"{self.base_url}/v3/accounts/{self.account_id}",
                headers=self.headers, timeout=_http_timeout(),
            )
            if r.status_code == 200:
                bal = float(r.json()["account"]["balance"])
                log.info("Login OK — balance: $%.2f", bal)
                return True
            log.error("Login FAILED: HTTP %d — %s", r.status_code, r.text[:300])
            if r.status_code == 401:
                log.error("→ 401: API key wrong or expired — check OANDA_API_KEY in Railway Variables")
            elif r.status_code == 403:
                log.error("→ 403: Account ID mismatch or key has no access to this account")
            elif r.status_code == 404:
                log.error("→ 404: Account not found — check OANDA_ACCOUNT_ID format (e.g. 101-003-XXXXXXX-001)")
            return False
        except requests.exceptions.Timeout:
            log.error("Login FAILED: request timed out — OANDA API unreachable from Railway")
            return False
        except Exception as e:
            log.error("Login error: %s", e)
            return False


    def get_account_summary(self) -> dict:
        """Return OANDA account summary fields used for risk/margin checks."""
        try:
            r = requests.get(
                f"{self.base_url}/v3/accounts/{self.account_id}/summary",
                headers=self.headers, timeout=_http_timeout(),
            )
            if r.status_code == 200:
                return r.json().get("account", {})
            log.warning("get_account_summary failed: HTTP %d — %s", r.status_code, r.text[:200])
            return {}
        except Exception as e:
            log.error("get_account_summary error: %s", e)
            return {}

    def get_balance(self) -> float:
        try:
            r   = requests.get(
                f"{self.base_url}/v3/accounts/{self.account_id}",
                headers=self.headers, timeout=10,
            )
            bal = float(r.json()["account"]["balance"])
            log.info("Balance: $%.2f", bal)
            return bal
        except Exception as e:
            log.error("get_balance error: %s", e)
            return 0.0

    def get_price(self, instrument: str) -> tuple[float | None, float | None, float | None]:
        try:
            r     = requests.get(
                f"{self.base_url}/v3/accounts/{self.account_id}/pricing",
                headers=self.headers,
                params={"instruments": instrument},
                timeout=10,
            )
            price = r.json()["prices"][0]
            bid   = float(price["bids"][0]["price"])
            ask   = float(price["asks"][0]["price"])
            return (bid + ask) / 2, bid, ask
        except Exception as e:
            log.error("get_price error: %s", e)
            return None, None, None

    def get_position(self, instrument: str) -> dict | None:
        try:
            r = requests.get(
                f"{self.base_url}/v3/accounts/{self.account_id}/positions/{instrument}",
                headers=self.headers, timeout=10,
            )
            if r.status_code == 200:
                pos         = r.json()["position"]
                long_units  = int(float(pos["long"]["units"]))
                short_units = int(float(pos["short"]["units"]))
                if long_units != 0 or short_units != 0:
                    return pos
            return None
        except Exception as e:
            log.error("get_position error: %s", e)
            return None

    def get_open_trade_id(self, instrument: str) -> tuple[str | None, str | None]:
        try:
            r = requests.get(
                f"{self.base_url}/v3/accounts/{self.account_id}/trades",
                headers=self.headers,
                params={"instrument": instrument, "state": "OPEN"},
                timeout=10,
            )
            if r.status_code == 200:
                trades = r.json().get("trades", [])
                if trades:
                    trade = trades[0]
                    return trade.get("id"), trade.get("openTime", "")
            return None, None
        except Exception as e:
            log.error("get_open_trade_id error: %s", e)
            return None, None

    def check_pnl(self, position: dict) -> float:
        try:
            long_pnl  = float(position["long"].get("unrealizedPL", 0))
            short_pnl = float(position["short"].get("unrealizedPL", 0))
            return long_pnl + short_pnl
        except Exception:
            return 0.0

    def place_order(
        self,
        instrument: str,
        direction: str,
        size: int,
        stop_distance: int,
        limit_distance: int,
    ) -> dict:
        try:
            units = size if direction == "BUY" else -size

            price, bid, ask = self.get_price(instrument)
            if price is None:
                return {"success": False, "error": "Cannot get price"}

            pip       = 0.01 if ("JPY" in instrument or instrument in ["XAU_USD", "XAG_USD"]) else 0.0001
            precision = 2 if instrument in ["XAU_USD", "XAG_USD"] else (3 if "JPY" in instrument else 5)

            entry    = ask if direction == "BUY" else bid
            sl_price = round(entry - stop_distance  * pip if direction == "BUY" else entry + stop_distance  * pip, precision)
            tp_price = round(entry + limit_distance * pip if direction == "BUY" else entry - limit_distance * pip, precision)

            log.info(
                "Placing %s %s | units=%d | entry=%.5f | SL=%.5f | TP=%.5f",
                direction, instrument, units, entry, sl_price, tp_price,
            )

            payload = {"order": {
                "type":        "MARKET",
                "instrument":  instrument,
                "units":       str(units),
                "timeInForce": "FOK",
                "stopLossOnFill":   {"price": str(sl_price), "timeInForce": "GTC"},
                "takeProfitOnFill": {"price": str(tp_price), "timeInForce": "GTC"},
            }}

            r    = requests.post(
                f"{self.base_url}/v3/accounts/{self.account_id}/orders",
                headers=self.headers, json=payload, timeout=_http_timeout(),
            )
            data = r.json()
            log.info("Order response: %d %s", r.status_code, str(data)[:300])

            if r.status_code in [200, 201]:
                if "orderFillTransaction" in data:
                    trade_id = data["orderFillTransaction"].get("id", "N/A")
                    log.info("Trade placed — ID: %s", trade_id)
                    return {"success": True, "trade_id": trade_id,
                            "sl_price": sl_price, "tp_price": tp_price}
                elif "orderCancelTransaction" in data:
                    reason = data["orderCancelTransaction"].get("reason", "Unknown")
                    return {"success": False, "error": f"Cancelled: {reason}"}
                return {"success": True, "sl_price": sl_price, "tp_price": tp_price}
            return {"success": False, "error": data.get("errorMessage", str(data))}

        except Exception as e:
            log.error("place_order error: %s", e)
            return {"success": False, "error": str(e)}

    def close_trade(self, trade_id: str) -> dict:
        """Close a specific trade by ID — cancels attached TP/SL automatically.

        This avoids the MARKET_ORDER_REJECT storm that happens when
        close_position() is called on a trade with active TP/SL orders.
        OANDA's trade-close endpoint handles cancellation of dependent orders.
        """
        try:
            r = requests.put(
                f"{self.base_url}/v3/accounts/{self.account_id}/trades/{trade_id}/close",
                headers=self.headers,
                json={"units": "ALL"},
                timeout=_http_timeout(),
            )
            data = r.json()
            if r.status_code == 200:
                log.info("Trade %s closed successfully", trade_id)
                return {"success": True}
            error_msg = data.get("errorMessage", str(data)[:200])
            log.warning("close_trade %s failed: HTTP %d — %s", trade_id, r.status_code, error_msg)
            return {"success": False, "error": f"HTTP {r.status_code}: {error_msg}"}
        except Exception as e:
            log.error("close_trade error: %s", e)
            return {"success": False, "error": str(e)}

    def close_position(self, instrument: str) -> dict:
        """Close by instrument — tries trade-level close first (avoids TP/SL reject),
        falls back to position-level close.
        """
        try:
            # Prefer trade-level close to avoid MARKET_ORDER_REJECT on positions
            # with active TP/SL orders attached.
            trade_id, _ = self.get_open_trade_id(instrument)
            if trade_id:
                return self.close_trade(trade_id)

            # Fallback: position-level close (no open trade found by ID)
            r = requests.put(
                f"{self.base_url}/v3/accounts/{self.account_id}/positions/{instrument}/close",
                headers=self.headers,
                json={"longUnits": "ALL", "shortUnits": "ALL"},
                timeout=_http_timeout(),
            )
            if r.status_code == 200:
                return {"success": True}
            data = r.json()
            error_msg = data.get("errorMessage", str(data)[:200])
            return {"success": False, "error": f"HTTP {r.status_code}: {error_msg}"}
        except Exception as e:
            log.error("close_position error: %s", e)
            return {"success": False, "error": str(e)}
