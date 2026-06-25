"""telegram_alert.py — Fiber EUR Cascade v1.2 Telegram Dispatcher
Sends all Telegram alerts. Uses telegram_templates.py for message formatting.
send() passes messages through as-is — templates own the banner and structure.
"""
import logging
import os

import requests

from telegram_templates import (
    msg_trade_opened,
    msg_trade_closed,
    msg_session_open,
    msg_spread_skip,
    msg_cooldown_started,
    msg_error,
)

log = logging.getLogger(__name__)


class TelegramAlert:
    def __init__(self):
        self.token   = os.environ.get("TELEGRAM_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    def send(self, message: str) -> bool:
        """Send a pre-formatted message directly — no prefix or banner added."""
        if not self.token or not self.chat_id:
            log.warning("Telegram not configured — TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing")
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
            if r.status_code == 200:
                log.info("Telegram sent (%d chars)", len(message))
                return True
            log.warning("Telegram error %d: %s", r.status_code, r.text[:200])
            return False
        except Exception as e:
            log.error("Telegram error: %s", e)
            return False

    # ── Trade events ──────────────────────────────────────────────────────────

    def send_trade_open(
        self,
        direction: str,
        entry_price: float,
        sl_pips: int,
        tp_pips: int,
        sl_sgd: float,
        tp_sgd: float,
        spread: float,
        score: int,
        session_label: str,
        balance_sgd: float,
        trades_today: int,
        demo: bool = True,
        units: int | None = None,
    ) -> bool:
        from bot import load_settings as _ls
        _cfg      = _ls()
        pip       = _cfg.get("pip_size", 0.0001)
        _units    = int(units if units is not None else _cfg.get("trade_units", 50000))
        _dp       = int(_cfg.get("price_decimal_places", 5))
        sl_price  = round(entry_price - sl_pips * pip if direction == "BUY" else entry_price + sl_pips * pip, _dp)
        tp_price  = round(entry_price + tp_pips * pip if direction == "BUY" else entry_price - tp_pips * pip, _dp)
        rr_ratio  = round(tp_pips / sl_pips, 2)
        msg = msg_trade_opened(
            direction   = direction,
            session     = session_label,
            fill_price  = entry_price,
            sl_price    = sl_price,
            tp_price    = tp_price,
            sl_pips     = sl_pips,
            tp_pips     = tp_pips,
            units       = _units,
            rr_ratio    = rr_ratio,
            spread_pips = round(spread, 2),
            score       = score,
            balance     = balance_sgd,
            demo        = demo,
        )
        return self.send(msg)

    def send_tp_hit(
        self,
        pnl_usd: float,
        pnl_sgd: float,
        balance_sgd: float,
        wins: int,
        losses: int,
        open_price: float,
        close_price: float,
        session: str = "",
        demo: bool = True,
    ) -> bool:
        direction = "BUY" if close_price > open_price else "SELL"
        msg = msg_trade_closed(
            trade_id    = "",
            direction   = direction,
            entry       = open_price,
            close_price = close_price,
            pnl         = pnl_usd,
            session     = session or "Unknown",
            demo        = demo,
        )
        return self.send(msg)

    def send_sl_hit(
        self,
        pnl_usd: float,
        pnl_sgd: float,
        balance_sgd: float,
        wins: int,
        losses: int,
        open_price: float,
        close_price: float,
        session: str = "",
        demo: bool = True,
    ) -> bool:
        direction = "BUY" if open_price > close_price else "SELL"
        msg = msg_trade_closed(
            trade_id    = "",
            direction   = direction,
            entry       = open_price,
            close_price = close_price,
            pnl         = pnl_usd,
            session     = session or "Unknown",
            demo        = demo,
        )
        return self.send(msg)

    def send_timeout_close(
        self,
        minutes: float,
        pnl_usd: float,
        pnl_sgd: float,
        balance_sgd: float,
        demo: bool = True,
    ) -> bool:
        mode = "DEMO" if demo else "LIVE"
        return self.send(
            f"⏱ Timeout Close\n{'─' * 22}\n"
            f"Trade open {round(minutes):.0f} min — force-closed\n"
            f"PnL: ${pnl_usd:+.2f}  |  Balance: ${balance_sgd:,.2f}\n"
            f"Mode: {mode}"
        )

    # ── Session events ────────────────────────────────────────────────────────

    def send_session_open(
        self,
        session_label: str,
        session_hours: str,
        balance_sgd: float,
        trades_today: int,
        wins: int,
        losses: int,
    ) -> bool:
        daily_pnl = 0.0  # passed as 0 — actual P&L tracked in state
        msg = msg_session_open(
            session_name      = session_label,
            session_hours_sgt = session_hours,
            trades_today      = trades_today,
            daily_pnl         = daily_pnl,
        )
        return self.send(msg)

    # ── Guard events ──────────────────────────────────────────────────────────

    def send_news_block(self, instrument: str, reason: str) -> bool:
        return self.send(
            f"📰 News Block\n{'─' * 22}\n"
            f"EUR/USD — entries paused\n"
            f"{reason}\n"
            f"Resuming after event window"
        )

    def send_spread_skip(self, session_label: str, spread: float, limit: float) -> bool:
        return self.send(msg_spread_skip(
            session_label = session_label,
            spread_pips   = spread,
            limit_pips    = limit,
        ))

    def send_cooldown(self, streak: int, cooldown_until_sgt: str, session_name: str = "") -> bool:
        return self.send(msg_cooldown_started(
            streak             = streak,
            cooldown_until_sgt = cooldown_until_sgt,
            session_name       = session_name,
        ))

    # ── System events ─────────────────────────────────────────────────────────

    def send_login_fail(self, api_key_hint: str, account_id: str) -> bool:
        return self.send(
            f"❌ OANDA Login Failed\n{'─' * 22}\n"
            f"API key: {api_key_hint}\n"
            f"Account: {account_id or 'MISSING'}\n"
            f"Check Railway Variables → OANDA_API_KEY / OANDA_ACCOUNT_ID"
        )

    def send_error(self, error_type: str, detail: str = "") -> bool:
        return self.send(msg_error(error_type, detail))
