"""calendar_filter.py — Fiber EUR Cascade v1.2 Economic Calendar Filter
Fetches high-impact events from ForexFactory (auto-updating weekly feed).
Relevant currencies: USD and EUR only (matches EUR/USD pair).

Blackout window: 30 min before → 30 min after every high-impact event.
No manual updates needed — feed refreshes automatically each week.
"""
import logging
from datetime import datetime, timedelta

import pytz
import requests

log = logging.getLogger(__name__)
import json as _json_cal

def _load_settings_cal() -> dict:
    try:
        with open('settings.json') as _f: return _json_cal.load(_f)
    except Exception: return {}



class EconomicCalendar:
    _cls_cache       = None   # class-level: survives across instances
    _cls_cached_date = None
    _cls_cached_at   = None   # timestamp of last successful fetch

    def __init__(self):
        self.sg_tz  = pytz.timezone("Asia/Singapore")
        self.utc_tz = pytz.UTC

    def _fetch_events(self) -> list:
        """Fetch this week's high-impact events from ForexFactory.
        Cached at class level — persists across bot scan cycles.
        Re-fetches once per day or after a 6-hour TTL.
        """
        now_sg    = datetime.now(self.sg_tz)
        today_str = now_sg.strftime("%Y-%m-%d")

        # Return cached data if same day and within configured max age.
        settings = _load_settings_cal()
        max_age_hours = int(settings.get("calendar_cache_max_age_hours", 24))
        if (EconomicCalendar._cls_cached_date == today_str
                and EconomicCalendar._cls_cache is not None
                and EconomicCalendar._cls_cached_at is not None):
            age_hours = (now_sg - EconomicCalendar._cls_cached_at).total_seconds() / 3600
            if age_hours <= max_age_hours:
                return EconomicCalendar._cls_cache

        try:
            r = requests.get(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code != 200:
                log.warning("Calendar API returned %d", r.status_code)
                if EconomicCalendar._cls_cache is not None:
                    log.info("Calendar: using cached data (%d events)",
                             len(EconomicCalendar._cls_cache))
                    return EconomicCalendar._cls_cache
                if _load_settings_cal().get("news_fail_closed", True):
                    return None
                return []

            high_impacts = []
            for event in r.json():
                try:
                    impact   = event.get("impact", "").lower()
                    currency = event.get("currency", "")
                    title    = event.get("title", "")
                    date_str = event.get("date", "")

                    # EUR/USD: only high-impact USD and EUR events
                    if impact != "high":
                        continue
                    if currency not in ["USD", "EUR"]:
                        continue

                    high_impacts.append({
                        "date":     date_str,
                        "currency": currency,
                        "title":    title,
                        "impact":   "HIGH",
                    })
                except Exception as e:
                    log.warning("Event parse error: %s", e)
                    continue

            EconomicCalendar._cls_cache       = high_impacts
            EconomicCalendar._cls_cached_date = today_str
            EconomicCalendar._cls_cached_at   = now_sg

            log.info("Calendar loaded: %d high-impact events (USD/EUR) this week", len(high_impacts))
            for e in high_impacts:
                log.info("  %s %s @ %s", e["currency"], e["title"], e["date"])

            return high_impacts

        except Exception as e:
            log.warning("Calendar fetch failed: %s", e)
            if EconomicCalendar._cls_cache is not None:
                log.info("Calendar: using cached data after fetch failure (%d events)",
                         len(EconomicCalendar._cls_cache))
                return EconomicCalendar._cls_cache
            if _load_settings_cal().get("news_fail_closed", True):
                return None
            return []

    def _get_affected_currencies(self, instrument: str) -> list[str]:
        """Currencies relevant to this instrument."""
        affected = ["USD"]
        if "EUR" in instrument:
            affected.append("EUR")
        return affected

    def is_news_time(self, instrument: str = "EUR_USD") -> tuple[bool, str]:
        """Check if current time is within a news blackout window.

        Returns: (is_blackout: bool, reason: str)

        Timeline:
          T-30 min  — PAUSED (preparing for high-impact release)
          T+00 min  — NEWS RELEASED (high volatility)
          T+30 min  — PAUSED (market digesting)
          T+31 min  — RESUMED (safe to trade again)
        """
        now_utc  = datetime.utcnow().replace(tzinfo=self.utc_tz)
        if not _load_settings_cal().get("news_filter_enabled", True):
            return False, ""
        affected = self._get_affected_currencies(instrument)
        events   = self._fetch_events()

        if events is None:
            if _load_settings_cal().get("news_fail_closed", True):
                log.warning("Calendar unavailable — fail-closed news filter blocking trades")
                return True, "Calendar unavailable; fail-closed news protection active"
            log.warning("Calendar unavailable — trading without news filter")
            return False, ""

        if not events:
            return False, ""

        for event in events:
            if event["currency"] not in affected:
                continue

            try:
                date_str = event.get("date", "")
                if not date_str:
                    continue

                if "T" in date_str:
                    clean      = date_str[:19]
                    offset_str = date_str[19:]
                    event_dt   = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")

                    if "+" in offset_str or (offset_str.startswith("-") and len(offset_str) > 1):
                        sign       = 1 if "+" in offset_str else -1
                        offset_str = offset_str.replace("+", "").replace("-", "")
                        if ":" in offset_str:
                            h, m = offset_str.split(":")
                        else:
                            h = offset_str[:2]
                            m = offset_str[2:] if len(offset_str) > 2 else "00"
                        offset   = timedelta(hours=int(h), minutes=int(m)) * sign
                        event_dt = event_dt - offset

                    event_utc = event_dt.replace(tzinfo=self.utc_tz)
                else:
                    event_dt  = datetime.strptime(date_str[:10], "%Y-%m-%d")
                    event_utc = event_dt.replace(hour=12, tzinfo=self.utc_tz)

                _before = int(_load_settings_cal().get('news_block_before_min', 30))
                _after  = int(_load_settings_cal().get('news_block_after_min',  30))
                window_start = event_utc - timedelta(minutes=_before)
                window_end   = event_utc + timedelta(minutes=_after)

                if window_start <= now_utc <= window_end:
                    mins_to = int((event_utc - now_utc).total_seconds() / 60)
                    if mins_to > 0:
                        reason = f"{event['currency']} {event['title']} in {mins_to} min"
                    elif mins_to == 0:
                        reason = f"{event['currency']} {event['title']} releasing NOW"
                    else:
                        reason = f"{event['currency']} {event['title']} released {abs(mins_to)} min ago"

                    log.warning("NEWS BLACKOUT: %s", reason)
                    return True, reason

            except Exception as e:
                log.warning("News check error: %s", e)
                continue

        return False, ""

    def get_today_summary(self) -> str:
        """Today's high-impact events for session open alert."""
        now_sg    = datetime.now(self.sg_tz)
        today_str = now_sg.strftime("%Y-%m-%d")
        events    = self._fetch_events()

        if events is None:
            return "Calendar unavailable — fail-closed news protection active"

        today_events = [e for e in events if e.get("date", "")[:10] == today_str]

        if not today_events:
            return "No high-impact USD/EUR news today"

        lines = ["High-impact news today (EUR/USD):"]
        for e in today_events:
            try:
                date_str = e.get("date", "")
                if "T" in date_str:
                    event_dt = datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S")
                    sgt_dt   = event_dt + timedelta(hours=8)
                    time_str = sgt_dt.strftime("%H:%M SGT")
                else:
                    time_str = "time TBC"
                lines.append(f"  {e['currency']} {e['title']} @ {time_str}")
            except Exception:
                lines.append(f"  {e['currency']} {e['title']}")

        lines.append("Bot pauses 30 min before/after each event")
        return "\n".join(lines)

    def get_week_summary(self) -> str:
        """Full week high-impact events — useful for Monday alerts."""
        events = self._fetch_events()
        if events is None:
            return "Calendar unavailable this week"

        if not events:
            return "No high-impact USD/EUR events this week"

        lines = ["High-impact USD/EUR events this week:"]
        for e in events:
            try:
                date_str = e.get("date", "")[:10]
                lines.append(f"  {date_str}  {e['currency']} {e['title']}")
            except Exception:
                continue

        return "\n".join(lines) if len(lines) > 1 else "No high-impact events this week"
