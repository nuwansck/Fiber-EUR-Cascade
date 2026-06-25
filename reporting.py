"""reporting.py — Fiber EUR Cascade v1.2 Telegram Performance Reports
Fiber EUR Cascade v1.2 reporting — three scheduled reports.

Three scheduled reports, all reading from /data/trade_history.json
on the Railway persistent volume. 90-day rolling retention window.

Schedule (Asia/Singapore timezone):
  Daily    — Mon–Fri at 07:50 SGT  (covers prior trading day)
  Weekly   — Every Monday at 08:00 SGT  (covers Mon–Fri prior week)
  Monthly  — First Monday of month at 08:10 SGT

Usage (called by scheduler.py):
    from reporting import send_daily_report, send_weekly_report, send_monthly_report
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from state_utils import TRADE_HISTORY_FILE
from telegram_alert import TelegramAlert
from telegram_templates import msg_daily_report, msg_weekly_report, msg_monthly_report

def _load_settings_rep() -> dict:
    """Load settings.json — avoids circular import with bot.py."""
    try:
        from pathlib import Path as _P
        import json as _j
        return _j.loads((_P(__file__).parent / 'settings.json').read_text())
    except Exception:
        return {}

log = logging.getLogger(__name__)
SGT = pytz.timezone("Asia/Singapore")


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_history() -> list:
    if not TRADE_HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(TRADE_HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("reporting: could not read trade_history.json: %s", exc)
        return []


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return SGT.localize(datetime.strptime(ts, fmt))
        except Exception:
            pass
    return None


def _filled(history: list) -> list:
    return [
        t for t in history
        if t.get("status") == "FILLED" and isinstance(t.get("realized_pnl_sgd"), (int, float))
    ]


def _trades_in_window(filled: list, start: datetime, end: datetime) -> list:
    result = []
    for t in filled:
        dt = _parse_ts(t.get("timestamp_sgt"))
        if dt and start <= dt < end:
            result.append(t)
    return result


# ── Stats builders ─────────────────────────────────────────────────────────────

def _stats(trades: list) -> dict:
    if not trades:
        return {
            "count": 0, "wins": 0, "losses": 0,
            "net_pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "win_rate": 0.0, "profit_factor": None,
            "avg_r": None, "max_win_streak": 0, "max_loss_streak": 0,
            "best_trade": None, "worst_trade": None,
            "instant_sl_count": 0,
        }

    wins   = [t for t in trades if t["realized_pnl_sgd"] > 0]
    losses = [t for t in trades if t["realized_pnl_sgd"] < 0]

    gross_profit = sum(t["realized_pnl_sgd"] for t in wins)
    gross_loss   = abs(sum(t["realized_pnl_sgd"] for t in losses))
    net_pnl      = gross_profit - gross_loss
    win_rate     = round(len(wins) / len(trades) * 100, 1) if trades else 0.0
    pf           = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    # R-multiple (uses estimated_risk_sgd stored on the trade record)
    r_vals = []
    for t in trades:
        risk = t.get("estimated_risk_sgd")
        if risk and risk > 0:
            r_vals.append(round(t["realized_pnl_sgd"] / risk, 2))
    avg_r = round(sum(r_vals) / len(r_vals), 2) if r_vals else None

    # Win / loss streaks
    outcomes   = ["W" if t["realized_pnl_sgd"] > 0 else "L" for t in trades]
    max_win_s  = max_loss_s = cur = 0
    prev = None
    for o in outcomes:
        cur = (cur + 1) if o == prev else 1
        prev = o
        if o == "W":
            max_win_s  = max(max_win_s, cur)
        else:
            max_loss_s = max(max_loss_s, cur)

    def _trade_summary(t: dict) -> dict:
        raw_time = t.get("closed_at_sgt") or t.get("timestamp_sgt") or ""
        hhmm     = raw_time[11:16] if len(raw_time) >= 16 else raw_time
        return {"pnl": round(t["realized_pnl_sgd"], 2), "time": hhmm}

    def _duration_min(t: dict) -> int | None:
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            d1  = datetime.strptime((t.get("timestamp_sgt") or "")[:19], fmt)
            d2  = datetime.strptime((t.get("closed_at_sgt") or "")[:19], fmt)
            return int((d2 - d1).total_seconds() / 60)
        except Exception:
            return None

    _isl_thresh = int(_load_settings_rep().get('instant_sl_max_min', 5))
    instant_sl_count = sum(
        1 for t in losses
        if (_duration_min(t) or 999) <= _isl_thresh
    )

    return {
        "count":           len(trades),
        "wins":            len(wins),
        "losses":          len(losses),
        "net_pnl":         round(net_pnl, 2),
        "gross_profit":    round(gross_profit, 2),
        "gross_loss":      round(gross_loss, 2),
        "win_rate":        win_rate,
        "profit_factor":   pf,
        "avg_r":           avg_r,
        "max_win_streak":  max_win_s,
        "max_loss_streak": max_loss_s,
        "best_trade":      _trade_summary(max(trades, key=lambda t: t["realized_pnl_sgd"])),
        "worst_trade":     _trade_summary(min(trades, key=lambda t: t["realized_pnl_sgd"])),
        "instant_sl_count": instant_sl_count,
    }


def _session_breakdown(trades: list) -> dict[str, dict]:
    buckets: dict[str, list] = defaultdict(list)
    for t in trades:
        sess = t.get("session") or "Unknown"
        buckets[sess].append(t)
    result = {}
    for sess, ts in sorted(buckets.items()):
        wins   = [t for t in ts if t["realized_pnl_sgd"] > 0]
        losses = [t for t in ts if t["realized_pnl_sgd"] <= 0]
        result[sess] = {
            "count":    len(ts),
            "wins":     len(wins),
            "losses":   len(losses),
            "win_rate": round(len(wins) / len(ts) * 100, 1),
            "net_pnl":  round(sum(t["realized_pnl_sgd"] for t in ts), 2),
        }
    return result


def _open_count() -> int:
    try:
        import os
        import requests
        from pathlib import Path as _Path
        _settings_path = _Path(__file__).parent / "settings.json"
        try:
            import json as _json
            demo = _json.loads(_settings_path.read_text()).get("demo_mode", True)
        except Exception:
            demo = True
        base_url   = "https://api-fxpractice.oanda.com" if demo else "https://api-fxtrade.oanda.com"
        api_key    = os.environ.get("OANDA_API_KEY", "")
        account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
        headers    = {"Authorization": "Bearer " + api_key}
        r = requests.get(
            f"{base_url}/v3/accounts/{account_id}/openPositions",
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            return len(r.json().get("positions", []))
    except Exception:
        pass
    return 0


# ── Report senders ─────────────────────────────────────────────────────────────

def send_daily_report() -> None:
    """Send daily P&L summary. Scheduled Mon–Fri at 07:50 SGT."""
    try:
        now    = datetime.now(SGT)
        # Report covers yesterday's trading day
        end    = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start  = end - timedelta(days=1)

        history = _load_history()
        filled  = _filled(history)

        day_trades = _trades_in_window(filled, start, end)
        mtd_start  = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        mtd_trades = _trades_in_window(filled, mtd_start, now)

        day_stats = _stats(day_trades)
        mtd_stats = _stats(mtd_trades)

        # WTD placeholder (unused in template but kept for forward compat)
        wtd_start  = now - timedelta(days=now.weekday())
        wtd_start  = wtd_start.replace(hour=0, minute=0, second=0, microsecond=0)
        wtd_trades = _trades_in_window(filled, wtd_start, now)
        wtd_stats  = _stats(wtd_trades)

        sess_stats = _session_breakdown(day_trades) if day_trades else None

        try:
            from database import Database
            db = Database()
            import pytz as _tz
            utc_prefix = datetime.now(_tz.utc).strftime("%Y-%m-%d")
            blocked    = db.query_blocked_cycles(utc_prefix)
        except Exception:
            blocked = {"spread_guard": 0, "signal_blocked": 0}

        msg = msg_daily_report(
            day_label      = start.strftime("%a %d %b %Y"),
            day_stats      = day_stats,
            wtd_stats      = wtd_stats,
            mtd_stats      = mtd_stats,
            open_count     = _open_count(),
            report_time    = now.strftime("%H:%M SGT"),
            blocked_spread = blocked.get("spread_guard", 0),
            blocked_signal = blocked.get("signal_blocked", 0),
            session_stats  = sess_stats,
        )
        TelegramAlert().send(msg)
        log.info("Daily report sent: %d trades, P&L $%.2f", day_stats["count"], day_stats["net_pnl"])

    except Exception as exc:
        log.exception("send_daily_report error: %s", exc)


def send_weekly_report() -> None:
    """Send weekly performance summary. Scheduled every Monday at 08:00 SGT."""
    try:
        now   = datetime.now(SGT)
        # Cover Mon–Fri of the prior week
        end   = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
        start = end - timedelta(days=7)

        history = _load_history()
        filled  = _filled(history)
        trades  = _trades_in_window(filled, start, end)

        stats    = _stats(trades)
        sessions = _session_breakdown(trades)

        week_label = f"{start.strftime('%d %b')} – {(end - timedelta(days=1)).strftime('%d %b %Y')}"

        msg = msg_weekly_report(
            week_label  = week_label,
            stats       = stats,
            sessions    = sessions,
            setups      = {},
            report_time = now.strftime("%H:%M SGT"),
        )
        TelegramAlert().send(msg)
        log.info("Weekly report sent: %d trades, P&L $%.2f", stats["count"], stats["net_pnl"])

    except Exception as exc:
        log.exception("send_weekly_report error: %s", exc)


def send_monthly_report() -> None:
    """Send monthly performance summary. Scheduled 1st Monday of month at 08:10 SGT."""
    try:
        now   = datetime.now(SGT)
        # Cover the previous calendar month
        first_this  = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        first_prior = (first_this - timedelta(days=1)).replace(day=1)
        end          = first_this
        start        = first_prior

        history = _load_history()
        filled  = _filled(history)
        trades  = _trades_in_window(filled, start, end)

        # Prior-prior month for MoM delta
        first_prior2 = (first_prior - timedelta(days=1)).replace(day=1)
        prior2_trades = _trades_in_window(filled, first_prior2, first_prior)

        stats       = _stats(trades)
        prior_stats = _stats(prior2_trades)
        sessions    = _session_breakdown(trades)

        mom_delta       = round(stats["net_pnl"] - prior_stats["net_pnl"], 2) if prior_stats["count"] > 0 else None
        prior_month_pnl = prior_stats["net_pnl"]                               if prior_stats["count"] > 0 else None

        month_label = start.strftime("%B %Y")

        msg = msg_monthly_report(
            month_label     = month_label,
            stats           = stats,
            sessions        = sessions,
            mom_delta       = mom_delta,
            prior_month_pnl = prior_month_pnl,
            report_time     = now.strftime("%H:%M SGT"),
        )
        TelegramAlert().send(msg)
        log.info("Monthly report sent: %d trades, P&L $%.2f", stats["count"], stats["net_pnl"])

    except Exception as exc:
        log.exception("send_monthly_report error: %s", exc)


def send_monthly_csv_export() -> None:
    """Send cumulative trade log as CSV via Telegram. Fires monthly at 08:30 SGT."""
    try:
        import requests as _req
        history = _load_history()
        trades  = _filled(history)

        if not trades:
            TelegramAlert().send("📎 Monthly CSV: no closed trades found.")
            return

        now      = datetime.now(SGT)
        fieldnames = [
            "date_sgt", "time_sgt", "day_of_week",
            "session", "direction", "score",
            "entry_price", "sl_price", "tp_price",
            "result", "pl_sgd", "balance",
            "duration_min", "spread_pips", "units",
        ]

        buf    = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for t in sorted(trades, key=lambda x: x.get("timestamp_sgt", "")):
            ts = t.get("timestamp_sgt", "")
            try:
                dt_obj   = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                date_str = dt_obj.strftime("%Y-%m-%d")
                time_str = dt_obj.strftime("%H:%M")
                dow_str  = dt_obj.strftime("%A")
            except Exception:
                date_str = ts[:10]
                time_str = ts[11:16]
                dow_str  = ""

            dur = None
            try:
                d1  = datetime.strptime((t.get("timestamp_sgt") or "")[:19], "%Y-%m-%d %H:%M:%S")
                d2  = datetime.strptime((t.get("closed_at_sgt") or "")[:19], "%Y-%m-%d %H:%M:%S")
                dur = int((d2 - d1).total_seconds() / 60)
            except Exception:
                pass

            pnl = t.get("realized_pnl_sgd") or 0
            writer.writerow({
                "date_sgt":    date_str,
                "time_sgt":    time_str,
                "day_of_week": dow_str,
                "session":     t.get("session", ""),
                "direction":   t.get("direction", ""),
                "score":       t.get("score", ""),
                "entry_price": t.get("entry_price") or t.get("fill_price", ""),
                "sl_price":    t.get("sl_price", ""),
                "tp_price":    t.get("tp_price", ""),
                "result":      "TP" if pnl > 0 else ("SL" if pnl < 0 else "BE"),
                "pl_sgd":      round(pnl, 2),
                "balance":     round(t.get("balance_after") or 0, 2),
                "duration_min": dur or "",
                "spread_pips": t.get("spread_pips", ""),
                "units":       t.get("units", ""),
            })

        csv_bytes = buf.getvalue().encode("utf-8")
        data_dir  = Path(os.getenv("DATA_DIR", "/data"))
        filename  = f"fiber_eur_trades_to_{now.strftime('%Y-%m-%d')}.csv"
        tmp_path  = data_dir / filename
        tmp_path.write_bytes(csv_bytes)

        wins    = sum(1 for t in trades if (t.get("realized_pnl_sgd") or 0) > 0)
        losses  = sum(1 for t in trades if (t.get("realized_pnl_sgd") or 0) < 0)
        net_pnl = round(sum(t.get("realized_pnl_sgd") or 0 for t in trades), 2)
        wr      = round(wins / len(trades) * 100, 1) if trades else 0

        caption = (
            f"📊 Fiber EUR Cascade v1.2 — Cumulative Trade Log\n"
            f"Trades: {len(trades)}  ({wins}W / {losses}L)  WR {wr}%\n"
            f"Net P&L: ${net_pnl:+.2f}\n"
            f"Generated: {now.strftime('%d %b %Y %H:%M SGT')}"
        )

        secrets = _load_secrets()
        token   = secrets.get("TELEGRAM_TOKEN", os.environ.get("TELEGRAM_TOKEN", ""))
        chat_id = secrets.get("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", ""))
        url     = f"https://api.telegram.org/bot{token}/sendDocument"

        with open(tmp_path, "rb") as fh:
            r = _req.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (filename, fh, "text/csv")},
                timeout=int(_load_settings_rep().get("http_timeout_sec", 15)),
            )

        if r.status_code == 200:
            log.info("Monthly CSV sent: %s (%d trades)", filename, len(trades))
        else:
            log.warning("Monthly CSV failed: HTTP %s", r.status_code)

        try:
            tmp_path.unlink()
        except Exception:
            pass

    except Exception as exc:
        log.exception("send_monthly_csv_export error: %s", exc)



def send_weekly_export() -> None:
    """Send a weekly CSV trade log every Monday at 08:05 SGT.
    Covers all trades in the prior week (Mon–Fri).
    """
    try:
        import requests as _req
        history = _load_history()
        now     = datetime.now(SGT)

        end     = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start   = end - __import__('datetime').timedelta(days=7)
        trades  = _trades_in_window(_filled(history), start, end)

        if not trades:
            TelegramAlert().send("📎 Weekly CSV: no closed trades last week.")
            return

        import csv as _csv, io as _io
        fieldnames = ["date_sgt", "time_sgt", "session", "direction", "score",
                      "entry_price", "result", "pl_sgd", "duration_min"]
        buf    = _io.StringIO()
        writer = _csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for t in sorted(trades, key=lambda x: x.get("timestamp_sgt", "")):
            ts = t.get("timestamp_sgt", "")
            try:
                dt  = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                ds  = dt.strftime("%Y-%m-%d")
                tms = dt.strftime("%H:%M")
            except Exception:
                ds  = ts[:10]
                tms = ts[11:16]
            pnl = t.get("realized_pnl_sgd") or 0
            dur = None
            try:
                d1  = datetime.strptime((t.get("timestamp_sgt") or "")[:19], "%Y-%m-%d %H:%M:%S")
                d2  = datetime.strptime((t.get("closed_at_sgt") or "")[:19], "%Y-%m-%d %H:%M:%S")
                dur = int((d2 - d1).total_seconds() / 60)
            except Exception:
                pass
            writer.writerow({
                "date_sgt":    ds,
                "time_sgt":    tms,
                "session":     t.get("session", ""),
                "direction":   t.get("direction", ""),
                "score":       t.get("score", ""),
                "entry_price": t.get("entry_price") or t.get("fill_price", ""),
                "result":      "TP" if pnl > 0 else ("SL" if pnl < 0 else "BE"),
                "pl_sgd":      round(pnl, 2),
                "duration_min": dur or "",
            })

        wins    = sum(1 for t in trades if (t.get("realized_pnl_sgd") or 0) > 0)
        losses  = sum(1 for t in trades if (t.get("realized_pnl_sgd") or 0) < 0)
        net_pnl = round(sum(t.get("realized_pnl_sgd") or 0 for t in trades), 2)
        wr      = round(wins / len(trades) * 100, 1) if trades else 0

        csv_bytes = buf.getvalue().encode("utf-8")
        data_dir  = Path(os.getenv("DATA_DIR", "/data"))
        filename  = f"fiber_eur_weekly_{start.strftime('%Y-%m-%d')}.csv"
        tmp_path  = data_dir / filename
        tmp_path.write_bytes(csv_bytes)

        caption = (
            f"📎 Fiber EUR Cascade v1.2 — Weekly Trade Log\n"
            f"Week: {start.strftime('%d %b')} – {(now - __import__('datetime').timedelta(days=1)).strftime('%d %b %Y')}\n"
            f"Trades: {len(trades)}  ({wins}W / {losses}L)  WR {wr}%\n"
            f"Net P&L: ${net_pnl:+.2f}\n"
            f"Generated: {now.strftime('%d %b %Y %H:%M SGT')}"
        )

        secrets = _load_secrets()
        token   = secrets.get("TELEGRAM_TOKEN", os.environ.get("TELEGRAM_TOKEN", ""))
        chat_id = secrets.get("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", ""))
        url     = f"https://api.telegram.org/bot{token}/sendDocument"

        with open(tmp_path, "rb") as fh:
            r = _req.post(url, data={"chat_id": chat_id, "caption": caption},
                          files={"document": (filename, fh, "text/csv")}, timeout=int(_load_settings_rep().get("http_timeout_sec", 15)))

        if r.status_code == 200:
            log.info("Weekly CSV sent: %s (%d trades)", filename, len(trades))
        else:
            log.warning("Weekly CSV failed: HTTP %s", r.status_code)

        try:
            tmp_path.unlink()
        except Exception:
            pass

    except Exception as exc:
        log.exception("send_weekly_export error: %s", exc)


def _load_secrets() -> dict:
    """Load secrets from secrets.json if present, else return empty dict."""
    try:
        p = Path(__file__).resolve().parent / "secrets.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}
