"""telegram_templates.py — Fiber EUR Cascade v1.2
AtomicFX-style: clean, state-change only, minimal noise.
Visual format: richer cards, ascii bars,
session/setup breakdowns, verdict system.
Mobile-safe: all lines ≤42 chars to prevent Telegram wrapping.
"""
from __future__ import annotations

_DIV    = "─" * 22
_BANNER = "🇪🇺 Fiber EUR Cascade v1.2 | EUR/USD"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dir_icon(d: str) -> str:
    return "📈" if d == "BUY" else ("📉" if d == "SELL" else "")

def _session_icon(s: str) -> str:
    u = s.upper()
    if "EARLY" in u or "ASIA" in u: return "✈️"
    if "TOKYO" in u: return "🗼"
    if "LONDON" in u: return "🇬🇧"
    if "US CONT" in u: return "🌙"
    if "US" in u or "NY" in u: return "🚫"
    return "📊"

def _pnl_icon(v: float) -> str:
    return "🟢" if v > 0 else ("🔴" if v < 0 else "⬜")

def _mini_stats(s: dict) -> str:
    if s["count"] == 0:
        return "No closed trades"
    return (f"{s['count']} trades  {s['wins']}W/{s['losses']}L"
            f"  ${s['net_pnl']:+.2f}  WR {s['win_rate']:.0f}%")

def _clean_pair(s: str) -> str:
    return s.replace("_", "/")

def _clean_session(s: str) -> str:
    mapping = {
        "Early Asia":    "Early Asia",
        "Tokyo":         "Tokyo",
        "London":        "London",
        "London Window": "London",
        "NY":            "US",
        "NY Window":     "US",
        "US":            "US",
        "US Session":    "US",
        "US Cont.":      "US Cont.",
    }
    return mapping.get(s, s)

def _ascii_bar(v: float, mx: float, w: int = 6) -> str:
    """6-char bar keeps report lines mobile-safe."""
    if mx <= 0:
        return "░" * w
    f = int(round(v / mx * w))
    return "█" * f + "░" * (w - f)

def _ps(dp: int) -> float:
    return 10 ** -(dp - 1)

def _ai_stats_section(ai_stats) -> str:
    return ""


# ── 1. Signal update ──────────────────────────────────────────────────────────

def msg_signal_update(
    session: str,
    direction: str,
    score: int,
    decision: str = "WATCHING",
    reason: str = "",
    cycle_minutes: int = 5,
    signal_threshold: int = 4,
    h4_trend: str = "UNKNOWN",
    h4_aligned: bool = True,
) -> str:
    s_str = f"{score}/{signal_threshold}"
    di    = _dir_icon(direction)
    si    = _session_icon(session)

    def _h4_line() -> str:
        if h4_trend in ("UNKNOWN", "DISABLED"):
            return ""
        icon  = "🟢" if h4_trend == "BULLISH" else ("🔴" if h4_trend == "BEARISH" else "⬜")
        align = "aligned" if h4_aligned else "counter ⚠️"
        return f"H4: {icon} {h4_trend} ({align})\n"

    if decision == "WATCHING":
        return (
            f"{_BANNER}\n{_DIV}\n"
            f"EUR/USD  {di} {direction}"
            f"  Score {s_str}  👁\n"
            f"Reason: {reason or 'Waiting for setup'}\n"
            f"{_h4_line()}"
            f"{_DIV}\n"
            f"{si} {_clean_session(session)}\n"
            f"Next cycle in {cycle_minutes} min"
        )

    if decision == "BLOCKED":
        return (
            f"{_BANNER}\n{_DIV}\n"
            f"EUR/USD  {di} {direction}"
            f"  Score {s_str}  ❌\n"
            f"Reason: {reason}\n"
            f"{_h4_line()}"
            f"Next cycle in {cycle_minutes} min"
        )

    # READY
    return (
        f"{_BANNER}\n{_DIV}\n"
        f"EUR/USD  {di} {direction}"
        f"  Score {s_str}  ✅\n"
        f"Window: {si} {_clean_session(session)}\n"
        f"{_h4_line()}"
        f"{_DIV}\n"
        f"Next cycle in {cycle_minutes} min"
    )


# ── 2. Trade opened ───────────────────────────────────────────────────────────

def msg_trade_opened(
    direction: str,
    session: str,
    fill_price: float,
    sl_price: float,
    tp_price: float,
    sl_pips: int,
    tp_pips: int,
    units: int,
    rr_ratio: float,
    spread_pips: float,
    score: int,
    balance: float,
    demo: bool,
    signal_threshold: int = 4,
    price_dp: int = 5,
    tp2_rr: float = 2.5,
    setup: str = "",
    h4_trend: str = "UNKNOWN",
    h4_aligned: bool = True,
    free_margin=None,
    required_margin=None,
    margin_usage_pct=None,
) -> str:
    mode      = "DEMO" if demo else "LIVE"
    di        = _dir_icon(direction)
    si        = _session_icon(session)
    s_str     = f"{score}/{signal_threshold}"
    units_fmt = f"{int(units):,}"
    pip       = _ps(price_dp)
    tp2_pips  = round(sl_pips * tp2_rr)
    tp2_price = round(
        fill_price + sl_pips * pip * tp2_rr if direction == "BUY"
        else fill_price - sl_pips * pip * tp2_rr,
        price_dp,
    )
    setup_line = f"Setup:  {setup}\n" if setup else ""
    h4_line    = ""
    if h4_trend not in ("UNKNOWN", "DISABLED"):
        icon    = "🟢" if h4_trend == "BULLISH" else ("🔴" if h4_trend == "BEARISH" else "⬜")
        align   = "aligned" if h4_aligned else "counter ⚠️"
        h4_line = f"H4:     {icon} {h4_trend} ({align})\n"

    return (
        f"{_BANNER}\n{_DIV}\n"
        f"{di} {direction} EUR/USD"
        f" — {si} {_clean_session(session)}\n"
        f"{_DIV}\n"
        f"◆ Entry  {fill_price:.{price_dp}f}\n\n"
        f"✅ TP1   {tp_price:.{price_dp}f}"
        f"  +{tp_pips}p | {rr_ratio:.1f}R\n"
        f"◻  TP2   {tp2_price:.{price_dp}f}"
        f"  +{tp2_pips}p | {tp2_rr:.1f}R\n"
        f"✗  SL    {sl_price:.{price_dp}f}  -{sl_pips}p\n"
        f"{_DIV}\n"
        f"{setup_line}"
        f"Score:  {s_str}  |  Spread: {spread_pips:.1f}p\n"
        f"{h4_line}"
        f"Units:  {units_fmt}  |  {mode}\n"
        f"Bal:    ${balance:,.2f}"
    )


# ── 3. Breakeven ──────────────────────────────────────────────────────────────

def msg_breakeven(trade_id, direction, entry, current_price,
                  unrealized_pnl, demo, price_dp=5):
    mode = "DEMO" if demo else "LIVE"
    return (
        f"🔒 Break-Even Activated\n{_DIV}\n"
        f"{direction}  Trade #{trade_id}\n"
        f"Entry:  {entry:.{price_dp}f} → SL at entry\n"
        f"Now:    {current_price:.{price_dp}f}\n"
        f"PnL:    ${unrealized_pnl:+.2f}  |  {mode}"
    )


# ── 4. Trade closed ───────────────────────────────────────────────────────────

def msg_trade_closed(trade_id, direction, entry, close_price, pnl,
                     session, demo, duration_str="", price_dp=5,
                     max_pips_reached=None):
    mode = "DEMO" if demo else "LIVE"
    di   = _dir_icon(direction)
    pip  = _ps(price_dp)
    pips = abs(close_price - entry) / pip

    if pnl > 0:
        outcome, pip_str = "TP ✅", f"+{pips:.0f}p"
    elif pnl < 0:
        outcome, pip_str = "SL ✗",  f"-{pips:.0f}p"
    else:
        outcome, pip_str = "BE ➡️",  "0p"

    dur      = f"  {duration_str}" if duration_str else ""
    max_line = (f"Peak:   +{max_pips_reached:.1f}p reached\n"
                if max_pips_reached is not None and max_pips_reached > 0 else "")
    return (
        f"{di} {direction} {outcome}\n{_DIV}\n"
        f"Entry:  {entry:.{price_dp}f}"
        f" → {close_price:.{price_dp}f}\n"
        f"Move:   {pip_str}\n"
        f"PnL:    ${pnl:+.2f}{dur}\n"
        f"{max_line}"
        f"Session: {_clean_session(session)}  |  {mode}"
    )


# ── 5. News block ─────────────────────────────────────────────────────────────

def msg_news_block(event_name, event_time_sgt, before_min, after_min):
    return (
        f"📰 News Block\n{_DIV}\n"
        f"Event:  {event_name}\n"
        f"Time:   {event_time_sgt} SGT\n"
        f"Window: -{before_min}min → +{after_min}min\n"
        f"No new entries — resuming after"
    )


# ── 6. News penalty ───────────────────────────────────────────────────────────

def msg_news_penalty(event_names, penalty, score_after, score_before,
                     position_after, position_before):
    names  = ", ".join(event_names) if event_names else "Medium event"
    status = "Reduced size" if position_after > 0 else "Below threshold — watching"
    return (
        f"📰 News Penalty\n{_DIV}\n"
        f"Event:  {names}\n"
        f"Score:  {score_before} → {score_after}  ({penalty:+d})\n"
        f"{status}"
    )


# ── 7. Cooldown ───────────────────────────────────────────────────────────────

def msg_cooldown_started(streak, cooldown_until_sgt, session_name="",
                         day_losses=0, day_limit=0):
    sline     = f"Session: {session_name}\n" if session_name else ""
    loss_line = (f"Day:  {day_losses}/{day_limit} losses"
                 f"  ({max(0, day_limit - day_losses)} left)\n"
                 if day_limit > 0 else "")
    return (
        f"🧊 Cooldown Started\n{_DIV}\n"
        f"Reason:  {streak} consecutive SL hits\n"
        f"{sline}"
        f"Resumes: {cooldown_until_sgt} SGT\n"
        f"{loss_line}"
    ).rstrip()


# ── 8. Circuit breaker ────────────────────────────────────────────────────────

def msg_circuit_breaker(streak, resume_date_sgt, smart_flip=False,
                        new_direction="", pause_days=2):
    if smart_flip and new_direction:
        return (
            f"⚡ Circuit Breaker — Smart Flip\n{_DIV}\n"
            f"Reason: {streak} consecutive SL hits\n"
            f"H4 flipped → {new_direction}\n"
            f"Resuming immediately in {new_direction}\n"
            f"No pause needed — trend confirmed"
        )
    return (
        f"🛑 Circuit Breaker\n{_DIV}\n"
        f"Reason:  {streak} consecutive SL hits\n"
        f"Paused:  {pause_days} trading days\n"
        f"Resumes: {resume_date_sgt} SGT\n"
        f"H4 not flipped — choppy market"
    )


# ── 9. Daily cap ──────────────────────────────────────────────────────────────

def msg_daily_cap(daily_pnl=None, reset_time_sgt=""):
    pline = f"Day P&L: ${daily_pnl:+.2f}\n" if daily_pnl is not None else ""
    rline = f"Resets:  {reset_time_sgt}\n"   if reset_time_sgt else ""
    return (
        f"🎯 Daily Goal Reached\n{_DIV}\n"
        f"1 win — stopping for the day\n"
        f"{pline}{rline}"
        f"Resuming next session"
    )


# ── 10. New day ───────────────────────────────────────────────────────────────

def msg_new_day_resume(prev_day_pnl=None, prev_day_trades=0,
                       london_open_sgt="15:00"):
    prev = (f"Yesterday: {prev_day_trades} trade(s)"
            f"  ${prev_day_pnl:+.2f}\n"
            if prev_day_trades > 0 and prev_day_pnl is not None else "")
    return (
        f"✅ New Trading Day\n{_DIV}\n"
        f"Daily limits reset\n"
        f"{prev}"
        f"Next: London {london_open_sgt} SGT"
    )


# ── 11. Session open ──────────────────────────────────────────────────────────

def msg_session_open(session_name, session_hours_sgt, trades_today, daily_pnl):
    icon    = _session_icon(session_name)
    pnl_str = f"${daily_pnl:+.2f}" if trades_today > 0 else "—"
    return (
        f"{icon} {session_name} Open"
        f"  {session_hours_sgt}\n"
        f"{_DIV}\n"
        f"Today:  {trades_today} trade(s)  {pnl_str}\n"
        f"Scanning EUR/USD for setup..."
    )


# ── 12. Spread skip ───────────────────────────────────────────────────────────

def msg_spread_skip(session_label, spread_pips, limit_pips):
    over = spread_pips - limit_pips
    return (
        f"⚠️  Spread Too Wide\n{_DIV}\n"
        f"EUR/USD  |  {session_label}\n"
        f"Spread: {spread_pips:.1f}p"
        f"  Limit: {limit_pips}p  (+{over:.1f})\n"
        f"Waiting for spread to normalise"
    )


# ── 13. Order failed ─────────────────────────────────────────────────────────

def msg_order_failed(direction, units, error, retry_attempted=False,
                     free_margin=None, required_margin=None):
    mline = (f"Margin: ${free_margin:.0f} free"
             f"  ${required_margin:.0f} req\n"
             if free_margin is not None and required_margin is not None else "")
    return (
        f"❌ Order Failed\n{_DIV}\n"
        f"{direction}  EUR/USD  {units:,} units\n"
        f"Error:  {error}\n"
        f"{mline}"
        f"Retry: {'yes' if retry_attempted else 'no'}\n"
        f"Check OANDA account and logs"
    )


# ── 13b. Margin adjustment ────────────────────────────────────────────────────

def msg_margin_adjustment(requested_units, adjusted_units, free_margin,
                          required_margin, reason):
    action = "Skipping trade" if adjusted_units <= 0 else "Using smaller size"
    return (
        f"⚠️  Margin Protection\n{_DIV}\n"
        f"Pair:      EUR/USD\n"
        f"Requested: {int(requested_units):,}\n"
        f"Adjusted:  {int(adjusted_units):,}\n"
        f"Free:      ${free_margin:.2f}\n"
        f"Required:  ${required_margin:.2f}\n"
        f"{_DIV}\n"
        f"{action}"
    )


# ── 14. Error ─────────────────────────────────────────────────────────────────

def msg_error(error_type, detail=""):
    dline = f"Detail: {detail}\n" if detail else ""
    return f"❌ Error\n{_DIV}\n{error_type}\n{dline}Check logs"


# ── 15. Friday cutoff ─────────────────────────────────────────────────────────

def msg_friday_cutoff(cutoff_hour_sgt):
    return (
        f"📅 Friday Cutoff\n{_DIV}\n"
        f"After {cutoff_hour_sgt:02d}:00 SGT — no new entries\n"
        f"Resuming Monday London open"
    )


# ── 16. Startup ───────────────────────────────────────────────────────────────

def msg_startup(
    version,
    mode,
    balance,
    signal_threshold=4,
    cycle_minutes=5,
    sl_pips=15,
    tp_pips=25,
    units=50_000,
    max_trades_day=4,
    max_wins_day=2,
    max_trades_session=2,
    max_losses_day=3,
    max_losses_session=2,
    max_losing_streak=2,
    london_start=7,
    london_end=15,
    ny_start=15,
    ny_end=23,
    sessions=None,
    trading_day_start_hour=0,
    risk_pct_per_trade=0.02,
    daily_risk_cap_pct=0.06,
    risk_amount_sgd=100.0,
    daily_cap_sgd=300.0,
    max_units=75_000,
):
    rr = round(tp_pips / sl_pips, 2)
    risk_pct = risk_pct_per_trade * 100
    day_pct = daily_risk_cap_pct * 100
    if sessions is None:
        sessions = {
            "London": {"start": 16, "end": 21},
            "US": {"start": 21, "end": 24},
        }
    session_lines = []
    for label, sess in sessions.items():
        start = int(sess.get("start", 0))
        end = int(sess.get("end", 0))
        display_end = (end - 1) % 24
        icon = _session_icon(label)
        session_lines.append(
            f"{icon} {start:02d}:00–{display_end:02d}:59  {label:<11} max {max_trades_session}"
        )
    sessions_text = "\n".join(session_lines)
    return (
        f"🇪🇺 {version}\n"
        f"{_DIV}\n"
        f"🚀 {version} started\n"
        f"{_DIV}\n"
        f"Mode:     {mode}  |  Balance: ${balance:,.2f}\n"
        f"Pair:     EUR/USD (Fiber)\n"
        f"Strategy: 4-Layer Cascade  |  Cycle: {cycle_minutes} min\n"
        f"          H4 → H1 → M15 → M5\n"
        f"Signal:   {signal_threshold}/{signal_threshold} — all layers pass\n"
        f"Trade:    SL {sl_pips}p  |  TP {tp_pips}p  |  RR {rr}:1\n"
        f"Risk:     {risk_pct:.1f}%/trade (~${risk_amount_sgd:,.0f})\n"
        f"          Cap {day_pct:.1f}%/day (~${daily_cap_sgd:,.0f})\n"
        f"Sizing:   fractional · 2% of live balance\n"
        f"Size:     auto up to {max_units:,} units\n"
        f"{_DIV}\n"
        f"Sessions (SGT = UTC+8)\n"
        f"{sessions_text}\n"
        f"Trading days: Mon–Fri only\n"
        f"{_DIV}\n"
        f"Day reset: {trading_day_start_hour:02d}:00 SGT\n"
        f"Max: {max_trades_day} trades  ·  {max_wins_day} wins/day\n"
        f"Cap: {max_losses_day} losses/day  ·  {max_losses_session}/session\n"
        f"Circuit: {max_losing_streak} SL streak → 2-day pause"
    )


# ── 17. Daily report ─────────────────────────────────────────────────────────

def msg_daily_report(day_label, day_stats, wtd_stats, mtd_stats, open_count,
                     report_time, blocked_spread=0, blocked_signal=0,
                     session_stats=None):
    if day_stats["count"] == 0:
        oline = f"Open: {open_count} position(s)\n" if open_count > 0 else ""
        return (
            f"📊 Daily Summary — {day_label}\n{_DIV}\n"
            f"No trades closed today\n"
            f"{_DIV}\n"
            f"Month-to-date\n  {_mini_stats(mtd_stats)}\n"
            f"{_DIV}\n"
            f"{oline}"
            f"Report: {report_time}"
        )

    icon  = _pnl_icon(day_stats["net_pnl"])
    oline = f"Open: {open_count} position(s)\n" if open_count > 0 else ""
    parts = []
    if blocked_spread:  parts.append(f"{blocked_spread} spread")
    if blocked_signal:  parts.append(f"{blocked_signal} signal")
    bline   = f"Blocked: {', '.join(parts)}\n" if parts else ""
    best    = day_stats.get("best_trade")
    worst   = day_stats.get("worst_trade")
    bst     = f"  Best:  ${best['pnl']:+.2f}  ({best['time']} SGT)\n"   if best  else ""
    wst     = f"  Worst: ${worst['pnl']:+.2f}  ({worst['time']} SGT)\n" if worst else ""
    isl     = day_stats.get("instant_sl_count", 0)
    islline = f"  ⚡ Instant SL: {isl} trade(s)\n" if isl > 0 else ""
    fire    = " 🔥" if day_stats.get("wins", 0) >= 2 else ""

    # Session breakdown — 6-char bar, no P&L on same line
    sess_block = ""
    if session_stats:
        sess_block = f"{_DIV}\nSessions\n"
        mx = max((s["win_rate"] for s in session_stats.values()
                  if s["count"] > 0), default=1) or 1
        for name, s in session_stats.items():
            if s["count"] == 0:
                continue
            bar = _ascii_bar(s["win_rate"], mx)
            sess_block += (
                f"  {_session_icon(name)} {name:<7}"
                f" {bar} {s['win_rate']:.0f}%"
                f"  {s['wins']}W/{s['losses']}L"
                f"  ${s['net_pnl']:+.2f}\n"
            )

    return (
        f"📊 Daily Summary — {day_label}\n"
        f"{sess_block}"
        f"{_DIV}\n"
        f"Day total\n"
        f"  Trades: {day_stats['count']}"
        f"  {day_stats['wins']}W{fire}/{day_stats['losses']}L\n"
        f"  WR:     {day_stats['win_rate']:.0f}%\n"
        f"  P&L:    ${day_stats['net_pnl']:+.2f}  {icon}\n"
        f"{bst}{wst}{islline}{bline}"
        f"{_DIV}\n"
        f"Month-to-date\n  {_mini_stats(mtd_stats)}\n"
        f"{_DIV}\n"
        f"{oline}"
        f"Report: {report_time}"
    )


# ── 18. Weekly report ─────────────────────────────────────────────────────────

def msg_weekly_report(week_label, stats, sessions, setups, report_time):
    if stats["count"] == 0:
        return (f"📅 Weekly Report — {week_label}\n"
                f"{_DIV}\nNo closed trades.\nReport: {report_time}")

    icon   = _pnl_icon(stats["net_pnl"])
    pf_str = f"{stats['profit_factor']}" if stats["profit_factor"] is not None else "n/a"
    rline  = f"Avg R:    {stats['avg_r']}R\n" if stats.get("avg_r") is not None else ""
    bline  = (f"Best:     ${stats['best_trade']['pnl']:+.2f}"
              f"  ({stats['best_trade']['time']} SGT)\n"
              if stats.get("best_trade") else "")
    wline  = (f"Worst:    ${stats['worst_trade']['pnl']:+.2f}"
              f"  ({stats['worst_trade']['time']} SGT)\n"
              if stats.get("worst_trade") else "")

    def _sec(data, w=8):
        if not data: return ""
        mx = max(s["win_rate"] for s in data.values()) or 1
        return "".join(
            f"  {n[:w]:<{w}} {_ascii_bar(s['win_rate'], mx)}"
            f" {s['win_rate']:>5.1f}%"
            f"  {s['wins']}W/{s['losses']}L\n"
            for n, s in data.items()
        )

    pf, wr, n = stats["profit_factor"] or 0, stats["win_rate"], stats["count"]
    if n < 10:              verdict = f"⚠️ Small sample ({n} trades)"
    elif pf >= 1.3 and wr >= 45: verdict = f"✅ Healthy — PF {pf}  WR {wr}%"
    elif pf >= 1.0:         verdict = f"🟡 Marginal — PF {pf}  WR {wr}%"
    else:                   verdict = f"🔴 Negative — PF {pf}  WR {wr}%"

    setup_block = f"{_DIV}\nBy Setup\n{_sec(setups)}" if setups else ""
    return (
        f"📅 Weekly — {week_label}\n{_DIV}\n"
        f"{icon} {stats['count']} trades"
        f"  {stats['wins']}W / {stats['losses']}L\n"
        f"Net P&L:  ${stats['net_pnl']:+.2f}\n"
        f"Win rate: {wr}%\n"
        f"P.Factor: {pf_str}\n"
        f"{rline}"
        f"Streaks:  {stats['max_win_streak']}W / {stats['max_loss_streak']}L\n"
        f"{bline}{wline}"
        f"{_DIV}\nBy Session\n{_sec(sessions)}"
        f"{setup_block}"
        f"{_DIV}\n{verdict}\nReport: {report_time}"
    )


# ── 19. Monthly report ────────────────────────────────────────────────────────

def msg_monthly_report(month_label, stats, sessions, mom_delta,
                       prior_month_pnl, report_time, setups=None):
    if stats["count"] == 0:
        return (f"📆 Monthly Report — {month_label}\n"
                f"{_DIV}\nNo closed trades.\nReport: {report_time}")

    icon   = _pnl_icon(stats["net_pnl"])
    pf_str = f"{stats['profit_factor']}" if stats["profit_factor"] is not None else "n/a"
    rline  = f"Avg R:      {stats['avg_r']}R\n" if stats.get("avg_r") is not None else ""
    mline  = ""
    if mom_delta is not None and prior_month_pnl is not None:
        di    = "🟢" if mom_delta >= 0 else "🔴"
        mline = (f"vs prior:   ${prior_month_pnl:+.2f}"
                 f"  {di} {mom_delta:+.2f}\n")
    bline = (f"Best:       ${stats['best_trade']['pnl']:+.2f}"
             f"  ({stats['best_trade']['time']} SGT)\n"
             if stats.get("best_trade") else "")
    wline = (f"Worst:      ${stats['worst_trade']['pnl']:+.2f}"
             f"  ({stats['worst_trade']['time']} SGT)\n"
             if stats.get("worst_trade") else "")

    def _sec(data, w=8):
        if not data: return ""
        mx = max(s["win_rate"] for s in data.values()) or 1
        return "".join(
            f"  {n[:w]:<{w}} {_ascii_bar(s['win_rate'], mx)}"
            f" {s['win_rate']:>5.1f}%"
            f"  {s['wins']}W/{s['losses']}L\n"
            for n, s in data.items()
        )

    pf, wr, n = stats["profit_factor"] or 0, stats["win_rate"], stats["count"]
    if n < 20:              verdict, rec = f"⚠️ Small sample ({n})", "Need more data."
    elif pf >= 1.3 and wr >= 45: verdict, rec = f"✅ Healthy PF {pf} WR {wr}%", "No changes needed."
    elif pf >= 1.0:         verdict, rec = f"🟡 Marginal PF {pf} WR {wr}%", "Tighten filters."
    else:                   verdict, rec = f"🔴 Negative PF {pf} WR {wr}%", "Pause worst session."

    setup_block = f"{_DIV}\nBy Setup\n{_sec(setups)}" if setups else ""
    return (
        f"📆 Monthly — {month_label}\n{_DIV}\n"
        f"{icon} {stats['count']} trades"
        f"  {stats['wins']}W / {stats['losses']}L\n"
        f"Net P&L:    ${stats['net_pnl']:+.2f}\n"
        f"{mline}"
        f"Win rate:   {wr}%\n"
        f"P.Factor:   {pf_str}\n"
        f"{rline}"
        f"Gross P:    ${stats['gross_profit']:.2f}\n"
        f"Gross L:    ${stats['gross_loss']:.2f}\n"
        f"Streaks:    {stats['max_win_streak']}W / {stats['max_loss_streak']}L\n"
        f"{bline}{wline}"
        f"{_DIV}\nBy Session\n{_sec(sessions)}"
        f"{setup_block}"
        f"{_DIV}\n{verdict}\n💡 {rec}\nReport: {report_time}"
    )
