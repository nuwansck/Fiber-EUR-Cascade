"""bot.py — Fiber EUR Cascade v1.2 Trade Engine
========================================
Pair:      EUR/USD only
Strategy:  4-Layer Cascade (H4 macro → H1 stack → M15 impulse → M5 pullback)
Size:      Fractional risk-based sizing (2.0% of live balance, SGD)
SL:        15 pips
TP:        25 pips  R:R 1.67:1
Max dur:   45 minutes — then force-close

Sessions (SGT = UTC+8):
  London      16:00–20:59  max spread 1.3 pip
  US          21:00–23:59  max spread 1.3 pip

Daily goal: 2 wins max · 3 trades max · 2 per session · 3 losses/day · 2 losses/session

Protection:
  ATR gate        — skip if H1 ATR < 4.0 pips (market too quiet)
  News filter     — economic calendar block window (toggle: news_filter_enabled)
  30-min cooldown — after every SL hit
  Circuit breaker — 2 consecutive SL hits → 2-day pause
  Smart flip      — after circuit breaker, if H4 trend reversed, resume immediately
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pytz
import requests

from signals         import SignalEngine
from oanda_trader    import OandaTrader
from telegram_alert  import TelegramAlert
from calendar_filter import EconomicCalendar as CalendarFilter
from risk            import build_risk_plan, can_take_risk, reserve_daily_risk
from reconcile_state import reconcile_state_with_broker

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

sg_tz   = pytz.timezone("Asia/Singapore")
signals = SignalEngine()

# Pip value: EUR/USD 1 pip ≈ USD 1/10k per unit (account currency: USD)
SGD_PER_PIP_PER_10K = 1.35  # legacy name; USD pip value per 10k units — overridden from settings['sgd_per_pip_per_10k']

_SETTINGS_PATH    = Path(__file__).parent / "settings.json"
_DEFAULT_SETTINGS = {
    "signal_threshold":           4,
    "demo_mode":                  True,
    "trade_units":                50000,  # fallback only; fractional risk drives sizing
    "risk_model":                 "fractional",
    "risk_pct_per_trade":         0.02,
    "daily_risk_cap_pct":         0.06,
    "risk_per_trade_sgd":         100,    # 2% of 5,000 — flat fallback only
    "daily_risk_cap_sgd":         300,    # 3 x 100 — flat fallback only
    "account_currency":           "SGD",
    "pip_value_per_10k":          1.35,
    "min_trade_units":            1000,
    "max_units":                  75000,
    "margin_safety_factor":       0.6,
    "auto_scale_on_margin_reject": True,
    "loss_streak_cooldown_min":   30,
    "max_trades_day":             3,
    "max_wins_day":               2,
    "max_trades_session":         2,
    "max_losing_trades_day":      3,
    "max_losing_trades_session":  2,
    "pair_sl_tp": {
        "EUR_USD": {"sl_pips": 15, "tp_pips": 25, "max_duration_min": 45}
    },
    "sessions": {
        "London": {"start": 16, "end": 21, "max_spread": 1.3},
        "US":     {"start": 21, "end": 24, "max_spread": 1.3},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively overlay override onto base. Nested dicts merge key-by-key so
    a partial block in settings.json (e.g. pair_sl_tp with one sub-key omitted)
    backfills the missing sub-keys from _DEFAULT_SETTINGS instead of dropping
    them. Scalars and lists: override wins.
    """
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_settings() -> dict:
    """Return a fresh merged settings dict on every call.

    Defaults from _DEFAULT_SETTINGS are overridden by settings.json values via a
    recursive merge, so nested blocks backfill missing sub-keys rather than being
    replaced wholesale. _DEFAULT_SETTINGS is never mutated, so keys absent from
    settings.json always fall back to their coded defaults.
    """
    try:
        with open(_SETTINGS_PATH) as f:
            file_settings = json.load(f)
    except FileNotFoundError:
        with open(_SETTINGS_PATH, "w") as f:
            json.dump(_DEFAULT_SETTINGS, f, indent=2)
        file_settings = {}
    return _deep_merge(_DEFAULT_SETTINGS, file_settings)


def _build_assets(settings: dict) -> dict:
    """Build ASSETS from settings.json — single source of truth for sessions/SL/TP."""
    pair_cfg     = settings.get("pair_sl_tp", {}).get("EUR_USD", {})
    sl           = pair_cfg.get("sl_pips", 15)
    tp           = pair_cfg.get("tp_pips", 25)
    sessions_cfg = settings.get("sessions", _DEFAULT_SETTINGS["sessions"])
    sessions     = [
        {"label": label, "start": v["start"], "end": v["end"], "max_spread": v["max_spread"]}
        for label, v in sessions_cfg.items()
    ]
    return {
        "EUR_USD": {
            "instrument": "EUR_USD",
            "asset":      "EURUSD",
            "emoji":      "🇪🇺",
            "pip":        float(settings.get("pip_size", 0.0001)),
            "precision":  int(settings.get("price_decimal_places", 5)),
            "stop_pips":  sl,
            "tp_pips":    tp,
            "sessions":   sessions,
        }
    }


# Initialised at import; refreshed each run_bot() via load_settings()
ASSETS = _build_assets(_DEFAULT_SETTINGS)

def usd_to_sgd(amount: float) -> float:
    """Round to 2 dp.  Account currency is USD; no FX conversion is applied.
    Function name retained for call-site compatibility — values passed to
    Telegram are USD, not SGD.
    """
    return round(amount, 2)


def get_h4_direction() -> str | None:
    """Check current H4 trend direction for smart flip detection.
    Returns 'BUY', 'SELL', or None if unclear.
    Requires 3 consecutive H4 bars on the same side of EMA50.
    """
    try:
        api_key  = os.environ.get("OANDA_API_KEY", "")
        _demo    = os.environ.get("OANDA_DEMO", "true").lower() != "false"
        base_url = "https://api-fxpractice.oanda.com" if _demo else "https://api-fxtrade.oanda.com"
        headers  = {"Authorization": "Bearer " + api_key}
        r        = requests.get(
            base_url + "/v3/instruments/EUR_USD/candles",
            headers=headers,
            params={"count": "55", "granularity": "H4", "price": "M"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        candles = [x for x in r.json()["candles"] if x["complete"]]
        closes  = [float(x["mid"]["c"]) for x in candles]
        if len(closes) < 52:
            return None

        _h4_slow = int(load_settings().get('signal_params', {}).get('h4_ema_slow', 50))
        seed = sum(closes[:_h4_slow]) / _h4_slow
        ema  = seed
        mult = 2 / (_h4_slow + 1)
        for c in closes[_h4_slow:]:
            ema = (c - ema) * mult + ema

        last3 = closes[-3:]
        if all(c > ema for c in last3):
            return "BUY"
        elif all(c < ema for c in last3):
            return "SELL"
        return None
    except Exception as e:
        log.warning("get_h4_direction error: %s", e)
        return None


def get_active_session(hour: int) -> dict | None:
    for s in ASSETS["EUR_USD"]["sessions"]:
        if s["start"] <= hour < s["end"]:
            return s
    return None


def is_in_session(hour: int, cfg: dict) -> bool:
    return any(s["start"] <= hour < s["end"] for s in cfg["sessions"])


def is_trading_day(now: datetime, settings: dict | None = None) -> bool:
    """Return True only on allowed SGT weekdays. Default: Mon-Fri.

    This prevents Sunday/weekend session-open alerts and scans even when
    the clock matches a configured session window.
    """
    settings = settings or load_settings()
    if not bool(settings.get("trade_weekdays_only", True)):
        return True
    allowed = settings.get("trading_weekdays_sgt", [0, 1, 2, 3, 4])
    return now.weekday() in [int(x) for x in allowed]


def set_cooldown(state: dict, name: str) -> None:
    if "cooldowns" not in state:
        state["cooldowns"] = {}
    state["cooldowns"][name] = datetime.now(timezone.utc).isoformat()
    log.info("%s: cooldown set (30 min)", name)


def in_cooldown(state: dict, name: str, cooldown_min: int = 30) -> bool:
    cd = state.get("cooldowns", {}).get(name)
    if not cd:
        return False
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(cd)).total_seconds() / 60
        return elapsed < cooldown_min
    except Exception:
        return False


def cooldown_remaining(state: dict, name: str, cooldown_min: int = 30) -> int | str:
    cd = state.get("cooldowns", {}).get(name)
    if not cd:
        return 0
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(cd)).total_seconds() / 60
        return max(0, int(cooldown_min - elapsed))
    except Exception:
        return "?"


def _login_fail_key(now: datetime) -> str:
    slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
    return now.strftime("%Y%m%d") + "_" + str(slot)


def detect_sl_tp_hits(state: dict, trader: OandaTrader, alert: TelegramAlert) -> None:
    """Detect closed trades and dispatch TP/SL alerts.

    v1.6: Also checks OANDA for recently closed trades even when open_times
    has no entry (e.g. after Railway redeploy wiped local state). This fixes
    the underreporting of closed trades in Telegram daily summaries.
    """
    if "open_times" not in state:
        state["open_times"] = {}

    # Build list of instruments to check: anything in open_times,
    # plus all ASSETS (catches closures missed during reboot).
    instruments_to_check = set(state["open_times"].keys()) | set(ASSETS.keys())

    for name in list(instruments_to_check):
        if trader.get_position(name):
            continue  # still open — nothing to detect

        # If not in open_times, this was likely closed while bot was down
        was_tracked = name in state.get("open_times", {})

        try:
            url  = (
                trader.base_url + "/v3/accounts/" + trader.account_id
                + "/trades?state=CLOSED&instrument=" + name + "&count=1"
            )
            data = requests.get(url, headers=trader.headers, timeout=10).json().get("trades", [])
            if data:
                trade       = data[0]
                trade_id    = trade.get("id", "")

                # Dedup: skip if we already processed this trade closure
                _processed = state.get("_processed_closures", set())
                if isinstance(_processed, list):
                    _processed = set(_processed)
                if trade_id in _processed:
                    if not was_tracked:
                        continue  # already handled
                state.setdefault("_processed_closures_list", [])
                if trade_id in state["_processed_closures_list"]:
                    if not was_tracked:
                        continue
                state["_processed_closures_list"].append(trade_id)

                pnl_usd     = float(trade.get("realizedPL", "0"))
                pnl_sgd     = usd_to_sgd(pnl_usd)
                open_price  = float(trade.get("price", 0))
                close_price = float(trade.get("averageClosePrice", open_price))
                balance_sgd = usd_to_sgd(trader.get_balance())
                wins        = state.get("wins", 0)
                losses      = state.get("losses", 0)

                state["daily_pnl"] = state.get("daily_pnl", 0.0) + pnl_usd
                # Daily risk cap is cumulative for the trading day.
                # Do NOT subtract closed-trade risk here; it resets only on the next day.
                # This keeps $225/day as a true max planned risk for a $75/trade setup.
                _reserved = float(state.get("risk_reserved_" + name, 0.0))
                if _reserved:
                    state.pop("risk_reserved_" + name, None)

                if pnl_usd < 0:
                    set_cooldown(state, name)
                    state["losses"]        = losses + 1
                    consec                 = state.get("consec_losses", 0) + 1
                    state["consec_losses"] = consec
                    # Track session losses
                    sess = state.get("last_session", "")
                    if sess:
                        state["session_losses_" + sess] = state.get("session_losses_" + sess, 0) + 1
                    alert.send_sl_hit(pnl_usd, pnl_sgd, balance_sgd,
                                      state["wins"], state["losses"],
                                      open_price, close_price)

                    # Circuit breaker with smart flip detection
                    if consec >= int(load_settings().get('circuit_breaker_streak', 2)):
                        from datetime import timedelta
                        last_dir   = state.get("last_trade_direction", "")
                        h4_dir_now = get_h4_direction()
                        log.info("Smart flip check — last=%s  H4=%s", last_dir, h4_dir_now)

                        if h4_dir_now and last_dir and h4_dir_now != last_dir:
                            # H4 trend flipped — resume immediately in new direction
                            state["consec_losses"] = 0
                            state.pop("pause_until", None)
                            log.info("H4 FLIPPED %s → %s — resuming immediately", last_dir, h4_dir_now)
                            from telegram_templates import msg_circuit_breaker
                            alert.send(msg_circuit_breaker(
                                streak        = state.get("circuit_breaker_streak",
                                               load_settings().get("circuit_breaker_streak", 2)),
                                resume_date_sgt = "",
                                smart_flip    = True,
                                new_direction = h4_dir_now,
                            ))
                        else:
                            # Same direction = choppy market — pause 2 days
                            pause_days = int(load_settings().get("circuit_breaker_pause_days", 2))
                            pause_dt = datetime.now(timezone.utc) + timedelta(days=pause_days)
                            state["pause_until"]   = pause_dt.isoformat()
                            state["consec_losses"] = 0
                            resume_sgt = (pause_dt.astimezone(sg_tz)).strftime("%a %d %b %H:%M")
                            log.warning("Circuit breaker — H4 unchanged (%s) — pausing 2 days", h4_dir_now)
                            from telegram_templates import msg_circuit_breaker
                            alert.send(msg_circuit_breaker(
                                streak          = load_settings().get("circuit_breaker_streak", 2),
                                resume_date_sgt = resume_sgt,
                                smart_flip      = False,
                                pause_days      = int(load_settings().get("circuit_breaker_pause_days", 2)),
                            ))
                else:
                    state["wins"]          = wins + 1
                    state["consec_losses"] = 0
                    # Track session wins
                    sess = state.get("last_session", "")
                    if sess:
                        state["session_wins_" + sess] = state.get("session_wins_" + sess, 0) + 1
                    alert.send_tp_hit(pnl_usd, pnl_sgd, balance_sgd,
                                      state["wins"], state["losses"],
                                      open_price, close_price)

                # ── Write to trade_history.json (read by reporting.py) ──────
                try:
                    from state_utils import TRADE_HISTORY_FILE, load_json, save_json
                    _hist_now = datetime.now(sg_tz)   # `now` is not in scope here; define locally
                    history = load_json(TRADE_HISTORY_FILE, [])
                    entry_price_rec = float(trade.get("price", 0))
                    history.append({
                        "status":          "FILLED",
                        "timestamp_sgt":   _hist_now.strftime("%Y-%m-%d %H:%M:%S"),
                        "closed_at_sgt":   _hist_now.strftime("%Y-%m-%d %H:%M:%S"),
                        "session":         state.get("last_session", "Unknown"),
                        "direction":       state.get("last_trade_direction", ""),
                        "score":           state.get("last_trade_score", 0),
                        "entry_price":     entry_price_rec,
                        "sl_price":        state.get("last_sl_price", 0.0),
                        "tp_price":        state.get("last_tp_price", 0.0),
                        "realized_pnl_sgd": pnl_usd,
                        "balance_after":   trader.get_balance(),
                        "units":           int(state.get("last_trade_units", load_settings().get("trade_units", 50000))),
                        "estimated_risk_sgd": abs(state.get("last_risk_sgd", state.get("last_sl_sgd", 0))),
                    })
                    save_json(TRADE_HISTORY_FILE, history)
                    log.info("Trade history written (%d records)", len(history))
                except Exception as hist_e:
                    log.warning("Trade history write error: %s", hist_e)

                state["open_times"].pop(name, None)

        except Exception as e:
            log.warning("SL/TP detect error %s: %s", name, e)


def check_session_open_alerts(state: dict, alert: TelegramAlert, trader: OandaTrader, now: datetime, today: str) -> None:
    """Send session open alert once per window per day from settings.json."""
    _settings = load_settings()
    if not is_trading_day(now, _settings):
        log.info("Weekend/non-trading day — session open alerts disabled (%s)", now.strftime("%A"))
        return
    _sess_cfg = _settings.get("sessions", {})
    windows = []
    for label, sess in _sess_cfg.items():
        start = int(sess.get("start", 0))
        end = int(sess.get("end", 0))
        display_end = (end - 1) % 24
        windows.append({
            "start": start,
            "label": label,
            "hours": f"{start:02d}:00–{display_end:02d}:59 SGT",
        })

    for w in windows:
        if now.hour == w["start"]:
            akey = "session_open_" + today + "_" + w["label"]
            if not state.get("session_alerted", {}).get(akey):
                if "session_alerted" not in state:
                    state["session_alerted"] = {}
                state["session_alerted"][akey]                = True
                state["session_trades_"  + w["label"]] = 0
                state["session_wins_"    + w["label"]] = 0
                state["session_losses_"  + w["label"]] = 0
                state["session_pnl_"     + w["label"]] = 0.0

                try:
                    balance_sgd = usd_to_sgd(trader.get_balance() if trader.login() else state.get("start_balance", 0))
                except Exception:
                    balance_sgd = usd_to_sgd(state.get("start_balance", 0))

                alert.send_session_open(
                    session_label=w["label"],
                    session_hours=w["hours"],
                    balance_sgd=balance_sgd,
                    trades_today=state.get("trades", 0),
                    wins=state.get("wins", 0),
                    losses=state.get("losses", 0),
                )


def run_bot(state: dict) -> None:
    global ASSETS
    settings = load_settings()
    ASSETS   = _build_assets(settings)   # refresh sessions/SL/TP from settings

    # Pull trade parameters from settings (single source of truth)
    pair_cfg         = settings.get("pair_sl_tp", {}).get("EUR_USD", {})
    # Trade size is calculated per trade from 2% fractional risk (risk.py).
    FALLBACK_TRADE_SIZE = int(settings.get("trade_units", 50000))
    MAX_DURATION     = int(pair_cfg.get("max_duration_min", 45))
    COOLDOWN_MIN     = int(settings.get("loss_streak_cooldown_min", 30))
    MAX_TRADES_DAY   = int(settings.get("max_trades_day", 3))
    MAX_WINS_DAY     = int(settings.get("max_wins_day", 2))
    MAX_TRADES_SESS  = int(settings.get("max_trades_session", 2))
    MAX_LOSSES_DAY   = int(settings.get("max_losing_trades_day", 3))
    MAX_LOSSES_SESS  = int(settings.get("max_losing_trades_session", 2))

    now      = datetime.now(sg_tz)
    hour     = now.hour
    today    = now.strftime("%Y%m%d")
    alert    = TelegramAlert()
    calendar = CalendarFilter()

    log.info("Scan at %s SGT", now.strftime("%H:%M:%S"))

    trader = OandaTrader(demo=settings["demo_mode"])

    if not is_trading_day(now, settings):
        log.info("Weekend/non-trading day (%s SGT) — no scan / no session alert", now.strftime("%A"))
        return

    check_session_open_alerts(state, alert, trader, now, today)

    session = get_active_session(hour)
    if not session:
        log.info("Outside trading windows (%dh SGT)", hour)
        return

    log.info("Window: %s | Max spread: %s pip", session["label"], session["max_spread"])

    if not trader.login():
        # Track consecutive login failures for backoff
        state["_consec_login_fails"] = state.get("_consec_login_fails", 0) + 1
        fails = state["_consec_login_fails"]

        fail_key = _login_fail_key(now)
        if not state.get("login_fail_alerted", {}).get(fail_key):
            if "login_fail_alerted" not in state:
                state["login_fail_alerted"] = {}
            state["login_fail_alerted"][fail_key] = True
            api_key    = os.environ.get("OANDA_API_KEY", "")
            account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
            alert.send_login_fail(
                api_key_hint=api_key[:8] + "****" if api_key else "MISSING",
                account_id=account_id,
            )
        else:
            log.warning("Login failed — alert already sent this 30-min window")

        if fails >= 6:
            log.error("Login failed %d consecutive times — backing off 30 min", fails)
            import time
            time.sleep(1800)
        return

    state["_consec_login_fails"] = 0  # reset on successful login
    current_balance_sgd = usd_to_sgd(trader.get_balance())
    if "start_balance" not in state or state["start_balance"] == 0.0:
        state["start_balance"] = trader.get_balance()

    reconcile_state_with_broker(trader, state, "EUR_USD")
    detect_sl_tp_hits(state, trader, alert)

    # Duration guard — force-close any trade open beyond MAX_DURATION
    for name in ASSETS:
        if name not in state.get("open_times", {}):
            continue
        pos = trader.get_position(name)
        if not pos:
            state.get("open_times", {}).pop(name, None)
            continue
        try:
            trade_id, open_str = trader.get_open_trade_id(name)
            if not trade_id or not open_str:
                state.get("open_times", {}).pop(name, None)
                continue
            open_utc = datetime.fromisoformat(open_str.replace("Z", "+00:00"))
            mins     = (datetime.now(pytz.utc) - open_utc).total_seconds() / 60
            log.info("%s: open %.1f min", name, mins)

            if mins >= MAX_DURATION:
                pnl_usd = trader.check_pnl(pos)
                pnl_sgd = usd_to_sgd(pnl_usd)

                # Use trade-level close (avoids MARKET_ORDER_REJECT on positions
                # with active TP/SL orders).
                result = trader.close_trade(trade_id)
                state.get("open_times", {}).pop(name, None)

                if result.get("success"):
                    alert.send_timeout_close(
                        minutes=mins, pnl_usd=pnl_usd, pnl_sgd=pnl_sgd,
                        balance_sgd=current_balance_sgd,
                    )
                else:
                    log.warning("%s: timeout close failed — %s", name, result.get("error", "unknown"))
        except Exception as e:
            state.get("open_times", {}).pop(name, None)
            log.warning("Duration check %s: %s — open_times cleared", name, e)

    # Circuit breaker
    pause_until = state.get("pause_until")
    if pause_until:
        try:
            remaining = (datetime.fromisoformat(pause_until) - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                log.info("Circuit breaker active — %.1f days remaining", remaining / 86400)
                return
            else:
                state.pop("pause_until", None)
                log.info("Circuit breaker expired — resuming")
        except Exception:
            state.pop("pause_until", None)

    # Friday cutoff — no new entries after configured SGT hour
    _friday_cutoff = int(settings.get("friday_cutoff_hour_sgt", 23))
    if now.weekday() == 4 and hour >= _friday_cutoff:
        log.info("Friday %02d:00 SGT+ — no new entries (weekend risk)", _friday_cutoff)
        return

    # ── Daily caps ────────────────────────────────────────────────────────
    trades_today = state.get("trades", 0)
    wins_today   = state.get("wins",   0)
    losses_today = state.get("losses", 0)

    if wins_today >= MAX_WINS_DAY:
        log.info("WIN-STOP: %d/%d wins today — done for the day", wins_today, MAX_WINS_DAY)
        return

    if trades_today >= MAX_TRADES_DAY:
        log.info("TRADE CAP: %d/%d trades today — done for the day", trades_today, MAX_TRADES_DAY)
        return

    if losses_today >= MAX_LOSSES_DAY:
        log.info("LOSS CAP: %d/%d losses today — done for the day", losses_today, MAX_LOSSES_DAY)
        return

    # Daily cumulative risk cap (fractional: cap = balance * daily_risk_cap_pct)
    from risk import resolve_daily_cap
    _equity = current_balance_sgd
    _daily_cap = resolve_daily_cap(settings, _equity)
    settings["daily_risk_cap_sgd"] = _daily_cap   # resolved value for can_take_risk()
    _daily_used = float(state.get("daily_risk_used_sgd", state.get("daily_risk_used_usd", 0.0)))
    if _daily_cap > 0 and _daily_used >= _daily_cap:
        log.info("RISK CAP: %.2f/%.2f SGD used — done for the day", _daily_used, _daily_cap)
        return

    # ── Signal scan and trade entry ────────────────────────────────────────
    threshold = settings.get("signal_threshold", 4)

    for name, cfg in ASSETS.items():
        pos = trader.get_position(name)
        if pos:
            dirn    = "BUY" if int(float(pos.get("long", {}).get("units", 0))) > 0 else "SELL"
            pnl_sgd = usd_to_sgd(trader.check_pnl(pos))
            log.info("%s: %s open | unrealised $%s", name, dirn, pnl_sgd)
            continue

        if in_cooldown(state, name, COOLDOWN_MIN):
            log.info("%s: cooldown %s min", name, cooldown_remaining(state, name, COOLDOWN_MIN))
            continue

        price, bid, ask = trader.get_price(name)
        if price is None:
            log.warning("%s: price error", name)
            continue

        spread = (ask - bid) / cfg["pip"]
        if spread > session["max_spread"] + 0.05:
            log.info("%s: spread %.2fp — skip (max %sp)", name, spread, session["max_spread"])
            continue

        # ── Per-session caps ───────────────────────────────────────────────
        sess_label     = session["label"]
        sess_trades    = state.get("session_trades_" + sess_label, 0)
        sess_losses    = state.get("session_losses_" + sess_label, 0)
        sess_wins      = state.get("session_wins_"   + sess_label, 0)

        if sess_trades >= MAX_TRADES_SESS:
            log.info("%s: %s session trade cap %d/%d — skip", name, sess_label, sess_trades, MAX_TRADES_SESS)
            continue

        if sess_losses >= MAX_LOSSES_SESS:
            log.info("%s: %s session loss cap %d/%d — skip", name, sess_label, sess_losses, MAX_LOSSES_SESS)
            continue

        MAX_WINS_SESS = max(1, MAX_WINS_DAY // 2)   # derived: wins spread evenly across sessions
        if sess_wins >= MAX_WINS_SESS:
            log.info("%s: %s session already won — skip", name, sess_label)
            continue

        # News filter
        news_active, news_reason = calendar.is_news_time(name)
        if news_active:
            alert_key = name + "_news_" + now.strftime("%Y%m%d%H")
            if not state.get("news_alerted", {}).get(alert_key):
                if "news_alerted" not in state:
                    state["news_alerted"] = {}
                state["news_alerted"][alert_key] = True
                alert.send_news_block(name, news_reason)
            log.info("%s: news block — %s", name, news_reason)
            continue

        # Signal cascade
        result    = signals.analyze(asset=cfg["asset"], state=state)
        score, direction, details = result[0], result[1], result[2]

        log.info("%s: score=%d/%d dir=%s | %s", name, score, threshold, direction, details)

        if score < threshold or direction == "NONE":
            continue

        # Place trade — risk-based sizing, daily risk cap, and margin guard
        use_sl = cfg["stop_pips"]
        use_tp = cfg["tp_pips"]
        account_summary = trader.get_account_summary()
        risk_plan = build_risk_plan(settings, sl_pips=use_sl, price=price,
                                    account_summary=account_summary, equity=current_balance_sgd)

        ok_risk, risk_reason = can_take_risk(state, settings, risk_plan.risk_amount)
        if not ok_risk:
            log.info("%s: %s — skip", name, risk_reason)
            continue

        if risk_plan.final_units <= 0:
            log.warning("%s: margin guard blocked trade | requested=%s | available=%s | required=%s | %s",
                        name, risk_plan.requested_units, risk_plan.margin_available,
                        risk_plan.estimated_required_margin, risk_plan.reason)
            alert.send_error("Margin guard blocked trade: " + risk_plan.reason)
            continue

        TRADE_SIZE = int(risk_plan.final_units)
        if risk_plan.adjusted:
            log.warning("%s: units adjusted %d → %d (%s)", name, risk_plan.requested_units, TRADE_SIZE, risk_plan.reason)

        _pip_value = float(settings.get('pip_value_per_10k', settings.get('sgd_per_pip_per_10k', SGD_PER_PIP_PER_10K)))
        sl_sgd    = round((TRADE_SIZE / 10000) * use_sl * _pip_value, 2)
        tp_sgd    = round((TRADE_SIZE / 10000) * use_tp * _pip_value, 2)

        result_order = trader.place_order(
            instrument=name, direction=direction, size=TRADE_SIZE,
            stop_distance=use_sl, limit_distance=use_tp,
        )

        if result_order["success"]:
            reserve_daily_risk(state, risk_plan.risk_amount)
            state["risk_reserved_" + name] = risk_plan.risk_amount
            state["trades"] = state.get("trades", 0) + 1
            if "open_times" not in state:
                state["open_times"] = {}
            state["open_times"][name]        = now.isoformat()
            state["last_trade_direction"]    = direction
            state["last_trade_score"]        = score
            state["last_session"]            = session["label"]
            state["last_sl_sgd"]             = sl_sgd
            state["last_tp_sgd"]             = tp_sgd
            state["last_risk_sgd"]           = risk_plan.risk_amount
            state["last_trade_units"]        = TRADE_SIZE
            state["last_sl_price"]           = result_order.get("sl_price", 0.0)
            state["last_tp_price"]           = result_order.get("tp_price", 0.0)
            sess_key                         = "session_trades_" + session["label"]
            state[sess_key]                  = state.get(sess_key, 0) + 1

            price_now, _, _ = trader.get_price(name)
            entry_price     = price_now if price_now else price

            alert.send_trade_open(
                direction=direction,
                entry_price=entry_price,
                sl_pips=use_sl,
                tp_pips=use_tp,
                sl_sgd=sl_sgd,
                tp_sgd=tp_sgd,
                spread=spread,
                score=score,
                session_label=session["label"],
                balance_sgd=current_balance_sgd,
                trades_today=state["trades"],
                units=TRADE_SIZE,
            )
            log.info("%s: PLACED %s | est risk=$%s  est TP=$%s", name, direction, sl_sgd, tp_sgd)
        else:
            set_cooldown(state, name)
            log.warning("%s: order failed — %s", name, result_order.get("error", ""))

    log.info("Scan complete.")
