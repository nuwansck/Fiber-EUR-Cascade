"""main.py — Fiber EUR Cascade v1.2 Railway Entry Point
================================================
EUR/USD Multi-Session Conservative Cascade Bot

Sessions (SGT = UTC+8):
  London      16:00–20:59 SGT
  US          21:00–23:59 SGT

Trade: SL 15 pip | TP 25 pip | R:R 1.67:1 | risk-based units ($75 risk/trade default)
Signal: 4/4 layers must pass (H4 → H1 → M15 → M5)
Goal: 2 wins/day · 4 trades/day · 2 per session · 3 loss/day · 2 loss/session

Scheduling (time-based inside polling loop — no APScheduler):
  Every 5 min   — run_bot()
  Mon–Fri 07:50 — send_daily_report()
  Monday  08:00 — send_weekly_report()
  Mon 1st  08:10 — send_monthly_report() (first Monday of month)
  Last day 08:30 — send_monthly_csv_export()
  00:15 daily   — DB retention cleanup
"""

import logging
import os
import time
import traceback
from datetime import datetime

import pytz

from bot            import run_bot, ASSETS, is_in_session, is_trading_day, load_settings
from database       import Database
from oanda_trader   import OandaTrader
from reporting      import (
    send_daily_report,
    send_weekly_report,
    send_weekly_export,
    send_monthly_report,
    send_monthly_csv_export,
)
from telegram_alert import TelegramAlert
from telegram_templates import msg_startup
from version        import VERSION
from startup_checks import run_startup_checks
from logging_utils   import install_secret_redaction

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

INTERVAL_MINUTES = 5   # default; overridden at runtime from settings["cycle_minutes"]
sg_tz            = pytz.timezone("Asia/Singapore")
STATE            = {}
_SCHEDULED       = {}   # dedup flags: {"daily_20260516": True, ...}


def get_today_key() -> str:
    return datetime.now(sg_tz).strftime("%Y%m%d")


def fresh_day_state(today_str: str, balance: float, settings: dict | None = None) -> dict:
    state = {
        "date":                    today_str,
        "trades":                  0,
        "start_balance":           balance,
        "daily_pnl":               0.0,
        "wins":                    0,
        "losses":                  0,
        "consec_losses":           0,
        "cooldowns":               {},
        "open_times":              {},
        "news_alerted":            {},
        "session_alerted":         {},
        "login_fail_alerted":      {},
        # Per-session counters are added dynamically below.
        "last_trade_direction":    "",
        "last_trade_score":        0,
        "last_session":            "",
        "last_sl_sgd":             0.0,
        "last_tp_sgd":             0.0,
        "last_risk_usd":           0.0,
        "last_trade_units":        0,
        "daily_risk_used_usd":     0.0,
        "has_open_trade":          False,
    }
    for label in (settings or load_settings()).get("sessions", {}).keys():
        state["session_trades_" + label] = 0
        state["session_wins_" + label] = 0
        state["session_losses_" + label] = 0
        state["session_pnl_" + label] = 0.0
    return state


def check_env_vars() -> bool:
    api_key    = os.environ.get("OANDA_API_KEY", "")
    account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
    tg_token   = os.environ.get("TELEGRAM_TOKEN", "")
    tg_chat    = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not api_key or not account_id:
        log.error("=" * 50)
        log.error("❌ MISSING OANDA ENV VARS!")
        log.error("   OANDA_API_KEY    : %s", "SET ✅" if api_key    else "MISSING ❌")
        log.error("   OANDA_ACCOUNT_ID : %s", "SET ✅" if account_id else "MISSING ❌")
        log.error("=" * 50)
        return False

    log.info("Env vars OK | Key: %s**** | Account: %s", api_key[:8], account_id)
    if not tg_token or not tg_chat:
        log.warning("Telegram not configured — no alerts will be sent")
    return True


def is_any_session_now() -> bool:
    now = datetime.now(sg_tz)
    if not is_trading_day(now, load_settings()):
        return False
    hour = now.hour
    return any(is_in_session(hour, cfg) for cfg in ASSETS.values())


def _is_first_monday_of_month(now: datetime) -> bool:
    return now.weekday() == 0 and now.day <= 7


def _last_day_of_month(now: datetime) -> bool:
    import calendar
    last = calendar.monthrange(now.year, now.month)[1]
    return now.day == last


def _fired(key: str) -> bool:
    return _SCHEDULED.get(key, False)


def _mark(key: str) -> None:
    _SCHEDULED[key] = True


def run_scheduled_tasks(now: datetime, settings: dict) -> None:
    """Time-based scheduler — checked every 5-min cycle.
    Uses _SCHEDULED dict to dedup: each job fires at most once per day/week/month.
    """
    h, m = now.hour, now.minute
    today = now.strftime("%Y%m%d")
    dow   = now.weekday()  # 0=Mon … 4=Fri

    dr_h  = int(settings.get("daily_report_hour_sgt", 7))
    dr_m  = int(settings.get("daily_report_minute_sgt", 50))
    wr_h  = int(settings.get("weekly_report_hour_sgt", 8))
    wr_m  = int(settings.get("weekly_report_minute_sgt", 0))
    mr_h  = int(settings.get("monthly_report_hour_sgt", 8))
    mr_m  = int(settings.get("monthly_report_minute_sgt", 10))
    cx_h  = int(settings.get("monthly_csv_export_hour_sgt", 8))
    cx_m  = int(settings.get("monthly_csv_export_minute_sgt", 30))
    db_h  = int(settings.get("db_cleanup_hour_sgt", 0))
    db_m  = int(settings.get("db_cleanup_minute_sgt", 15))

    # Daily report — Mon–Fri at dr_h:dr_m SGT
    key = f"daily_{today}"
    if h == dr_h and m >= dr_m and dow < 5 and not _fired(key):
        _mark(key)
        log.info("📊 Sending daily report...")
        try:
            send_daily_report()
        except Exception as e:
            log.exception("Daily report error: %s", e)

    # Weekly report — every Monday at wr_h:wr_m SGT
    key = f"weekly_{today}"
    if h == wr_h and m >= wr_m and dow == 0 and not _fired(key):
        _mark(key)
        log.info("📅 Sending weekly report...")
        try:
            send_weekly_report()
        except Exception as e:
            log.exception("Weekly report error: %s", e)

    # Weekly CSV export — every Monday at 08:05 SGT
    key = f"weekly_csv_{today}"
    if h == wr_h and m >= wr_m + int(settings.get('weekly_report_grace_min', 5)) and dow == 0 and not _fired(key):
        _mark(key)
        log.info("📎 Sending weekly CSV export...")
        try:
            send_weekly_export()
        except Exception as e:
            log.exception("Weekly CSV export error: %s", e)

    # Monthly report — first Monday of month at mr_h:mr_m SGT
    key = f"monthly_{now.strftime('%Y%m')}"
    if h == mr_h and m >= mr_m and dow == 0 and _is_first_monday_of_month(now) and not _fired(key):
        _mark(key)
        log.info("📆 Sending monthly report...")
        try:
            send_monthly_report()
        except Exception as e:
            log.exception("Monthly report error: %s", e)

    # Monthly CSV export — last day of month at cx_h:cx_m SGT
    key = f"csv_export_{now.strftime('%Y%m')}"
    if h == cx_h and m >= cx_m and _last_day_of_month(now) and not _fired(key):
        _mark(key)
        log.info("📎 Sending monthly CSV export...")
        try:
            send_monthly_csv_export()
        except Exception as e:
            log.exception("Monthly CSV export error: %s", e)

    # DB retention cleanup — daily at db_h:db_m SGT
    key = f"db_cleanup_{today}"
    if h == db_h and m >= db_m and not _fired(key):
        _mark(key)
        retention = int(settings.get("db_retention_days", 90))
        vacuum    = bool(settings.get("db_vacuum_weekly", True)) and dow == 0
        log.info("🗄 DB retention cleanup | %d days | vacuum=%s", retention, vacuum)
        try:
            db = Database()
            summary = db.purge_old_data(retention_days=retention, vacuum=vacuum)
            log.info("DB cleanup done: %s", summary)
        except Exception as e:
            log.exception("DB cleanup error: %s", e)


def main() -> None:
    global STATE
    install_secret_redaction()

    log.info("==" * 25)
    log.info("🚀 %s — Starting", VERSION)
    _s = load_settings()
    _sl_cfg  = _s.get("pair_sl_tp", {}).get("EUR_USD", {})
    _sess    = _s.get("sessions", {})
    log.info("Pair: EUR/USD | SL=%dpip | TP=%dpip | RR=%.2f:1",
             _sl_cfg.get("sl_pips", 15), _sl_cfg.get("tp_pips", 25),
             round(_sl_cfg.get("tp_pips", 25) / _sl_cfg.get("sl_pips", 15), 2))
    log.info("Signal: %d/%d layers | H4 → H1 → M15 → M5",
             _s.get("signal_threshold", 4), _s.get("signal_threshold", 4))
    _session_summary = " | ".join(
        f"{label} {int(sess.get('start', 0)):02d}:00–{(int(sess.get('end', 0)) - 1) % 24:02d}:59 SGT"
        for label, sess in _sess.items()
    )
    log.info("Sessions: %s", _session_summary)
    if _s.get("trade_weekdays_only", True):
        log.info("Trading days: Mon-Fri only (SGT) — weekend scans/session alerts disabled")
    log.info("Goal: %d wins/day | %d trades | %d/session | %d loss/day | %d loss/session",
             _s.get("max_wins_day", 2), _s.get("max_trades_day", 3),
             _s.get("max_trades_session", 2),
             _s.get("max_losing_trades_day", 3),
             _s.get("max_losing_trades_session", 2))
    log.info("Risk: %.1f%%/trade (fractional) | daily cap %.1f%% | max units %s",
             float(_s.get("risk_pct_per_trade", 0.02)) * 100,
             float(_s.get("daily_risk_cap_pct", 0.06)) * 100,
             _s.get("max_units", 75000))
    log.info("Reports: daily %02d:%02d | weekly Mon %02d:%02d | monthly %02d:%02d",
             _s.get("daily_report_hour_sgt", 7), _s.get("daily_report_minute_sgt", 50),
             _s.get("weekly_report_hour_sgt", 8), _s.get("weekly_report_minute_sgt", 0),
             _s.get("monthly_report_hour_sgt", 8), _s.get("monthly_report_minute_sgt", 10))
    log.info("DB retention: %d days | cleanup %02d:%02d SGT",
             _s.get("db_retention_days", 90),
             _s.get("db_cleanup_hour_sgt", 0), _s.get("db_cleanup_minute_sgt", 15))
    log.info("==" * 25)

    settings = load_settings()
    if not run_startup_checks(settings, settings_path=os.path.join(os.path.dirname(__file__), "settings.json")):
        log.error("Startup checks failed — sleeping 60s then exiting")
        time.sleep(60)
        return

    if not check_env_vars():
        log.error("Missing env vars — sleeping 60s then exiting")
        time.sleep(60)
        return

    settings = load_settings()
    alert    = TelegramAlert()

    # Fetch live balance for startup card
    try:
        _demo_boot = os.environ.get("OANDA_DEMO", "true").lower() != "false"
        _trader    = OandaTrader(demo=_demo_boot)
        _balance   = _trader.get_balance() if _trader.login() else 0.0
    except Exception:
        _balance = 0.0

    _capital_ref = float(settings.get("capital_sgd", settings.get("capital_usd", 0)) or 0)
    _risk_pct = float(settings.get("risk_pct_per_trade", 0.02) or 0)
    from risk import resolve_risk_amount, resolve_daily_cap
    _risk_amt = resolve_risk_amount(settings, _balance)
    _cap_amt  = resolve_daily_cap(settings, _balance)
    if _balance:
        log.info("Live risk: %.1f%%/trade = %.2f SGD | daily cap %.2f SGD",
                 _risk_pct * 100, _risk_amt, _cap_amt)
    if _capital_ref and _balance and abs(_balance - _capital_ref) / _capital_ref > 0.05:
        log.warning("Configured capital reference is %.2f SGD but broker balance is %.2f SGD", _capital_ref, _balance)

    _mode = "DEMO" if os.environ.get("OANDA_DEMO", "true").lower() != "false" else "LIVE"
    _sessions = settings.get("sessions", {})
    _sl   = settings.get("pair_sl_tp", {}).get("EUR_USD", {})

    alert.send(msg_startup(
        version              = VERSION,
        mode                 = _mode,
        balance              = _balance,
        signal_threshold     = settings.get("signal_threshold", 4),
        cycle_minutes        = settings.get("cycle_minutes", 5),
        sl_pips              = _sl.get("sl_pips", 15),
        tp_pips              = _sl.get("tp_pips", 25),
        units                = settings.get("max_units", settings.get("trade_units", 50000)),
        risk_pct_per_trade   = _risk_pct,
        daily_risk_cap_pct   = float(settings.get("daily_risk_cap_pct", 0.06)),
        risk_amount_sgd      = _risk_amt,
        daily_cap_sgd        = _cap_amt,
        max_units            = int(settings.get("max_units", 75000)),
        max_trades_day       = settings.get("max_trades_day", 3),
        max_wins_day         = settings.get("max_wins_day", 2),
        max_trades_session   = settings.get("max_trades_session", 2),
        max_losses_day       = settings.get("max_losing_trades_day", 3),
        max_losses_session   = settings.get("max_losing_trades_session", 2),
        max_losing_streak    = settings.get("circuit_breaker_streak", 2),
        sessions             = _sessions,
        trading_day_start_hour = int(settings.get("trading_day_start_hour_sgt", 0)),
    ))

    while True:
        try:
            settings = load_settings()
            now      = datetime.now(sg_tz)
            today    = now.strftime("%Y%m%d")
            log.info("⏰ %s", now.strftime("%Y-%m-%d %H:%M SGT"))

            # Day reset
            if STATE.get("date") != today:
                log.info("📅 New day — fetching balance...")
                try:
                    _demo   = os.environ.get("OANDA_DEMO", "true").lower() != "false"
                    trader  = OandaTrader(demo=_demo)
                    balance = trader.get_balance() if trader.login() else 0.0
                except Exception as e:
                    log.warning("Balance fetch error: %s", e)
                    balance = 0.0
                log.info("📅 Balance: $%.2f", balance)
                STATE = fresh_day_state(today, balance, settings)

            # Scheduled reports and DB retention
            run_scheduled_tasks(now, settings)

            # Trade cycle
            run_bot(state=STATE)

        except Exception as e:
            log.error("❌ Bot error: %s", e)
            log.error(traceback.format_exc())
            time.sleep(30)

        INTERVAL_MINUTES = int(settings.get("cycle_minutes", 5))
        log.info("💤 Sleeping %d min...", INTERVAL_MINUTES)
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
