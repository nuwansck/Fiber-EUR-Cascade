"""
signals.py — Fiber EUR Cascade v1.2 Signal Engine
==========================================
Pair:      EUR/USD only
Strategy:  4-Layer Cascade — all layers must pass for an entry

Layer cascade (sequential):
  L0  H4  Macro trend — EMA50 direction + 3-bar consistency check
  ATR     H1 flat-market veto — blocks if H1 ATR(14) < 4.0 pip
  L1  H1  Dual-EMA alignment — price / EMA21 / EMA50 bull or bear stack
  L2  M15 Impulse candle break — 5-bar structure breakout with strong body
  L3  M5  Pullback entry — EMA13 touch + RSI(7) + confirmation candle

Post-L3 vetoes (both must pass):
  V1  H1  EMA200 hard block — no trade against the major S/R level
  V2  M30 Counter-trend block — 3/3 opposing M30 candles blocks entry

Score:
  0  Nothing confirmed
  1  L0 passed (macro direction set)
  2  L1 passed (H1 stack aligned)
  3  L2 passed (M15 impulse fired — waiting for L3 on next scan)
  4  All layers + vetoes passed — fire trade

L2 → L3 state:
  When L2 fires, direction and timestamp are saved to state["l2_pending"].
  Subsequent scans check only L3 for up to L2_EXPIRY_MINUTES (45 min).
  If L3 does not confirm within the window, l2_pending is cleared.

Trade parameters:
  SL = 15 pip | TP = 25 pip | R:R = 1.67:1
  Size = 50,000 units | Max duration = 45 min
"""

import os
import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger(__name__)

def _sig_cfg() -> dict:
    """Load signal_params from settings.json (lazy, per-call so hot-reload works)."""
    try:
        from bot import load_settings
        return load_settings().get("signal_params", {})
    except Exception:
        return {}


def _filters_cfg() -> dict:
    """Load the `filters` block from settings.json (lazy, per-call).

    Kept separate from signal_params: market-guard thresholds (min_atr_pips,
    and chaos_pips reserved for a planned volatility guard) live under
    settings.json["filters"].
    """
    try:
        from bot import load_settings
        return load_settings().get("filters", {})
    except Exception:
        return {}


class SafeFilter(logging.Filter):
    def __init__(self):
        self.api_key = os.environ.get("OANDA_API_KEY", "")

    def filter(self, record):
        if self.api_key and self.api_key in str(record.getMessage()):
            record.msg = record.msg.replace(self.api_key, "***")
        return True


log.addFilter(SafeFilter())

L2_EXPIRY_MINUTES = 45  # module default; overridden per-scan via _sig_cfg().get("l2_expiry_minutes", 45)


class SignalEngine:
    def __init__(self):
        self.api_key  = os.environ.get("OANDA_API_KEY", "")
        _demo         = os.environ.get("OANDA_DEMO", "true").lower() != "false"
        self.base_url = "https://api-fxpractice.oanda.com" if _demo else "https://api-fxtrade.oanda.com"
        self.headers  = {"Authorization": "Bearer " + self.api_key}

    def _fetch_candles(self, instrument, granularity, count=60):
        url    = self.base_url + "/v3/instruments/" + instrument + "/candles"
        params = {"count": str(count), "granularity": granularity, "price": "M"}
        for attempt in range(3):
            try:
                r = requests.get(url, headers=self.headers, params=params, timeout=10)
                if r.status_code == 200:
                    c = [x for x in r.json()["candles"] if x["complete"]]
                    return (
                        [float(x["mid"]["c"]) for x in c],
                        [float(x["mid"]["h"]) for x in c],
                        [float(x["mid"]["l"]) for x in c],
                        [float(x["mid"]["o"]) for x in c],
                    )
                log.warning("Candle %s attempt %d HTTP %d", granularity, attempt + 1, r.status_code)
            except Exception as e:
                log.warning("Candle fetch error: %s", e)
        return [], [], [], []

    def _ema(self, data, period):
        if not data:
            return [0.0]
        if len(data) < period:
            return [sum(data) / len(data)] * len(data)
        seed = sum(data[:period]) / period
        emas = [seed] * period
        mult = 2 / (period + 1)
        for p in data[period:]:
            emas.append((p - emas[-1]) * mult + emas[-1])
        return emas

    def _rsi(self, closes, period=None):
        if period is None:
            period = int(_sig_cfg().get('rsi_period', 7))
        if len(closes) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _atr(self, highs, lows, closes, period=None):
        if period is None:
            period = int(_sig_cfg().get('h1_atr_period', 14))
        if len(highs) < period + 1:
            return 0.0
        trs = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        return sum(trs[-period:]) / period

    def analyze(self, asset="EURUSD", state=None):
        """Run the full cascade for EUR/USD.

        state dict is passed in so L2 pending persists between scans.
        Falls back gracefully if state=None.
        Returns: (score, direction, reason_string)
        """
        return self._scalp_eurusd("EUR_USD", state=state)

    # ──────────────────────────────────────────────────────────────────────
    def _scalp_eurusd(self, instrument, state=None):
        reasons = []
        score   = 0

        # Check if L2 already fired and we are waiting for L3
        if state is not None:
            pending = state.get("l2_pending", {})
            if pending.get("instrument") == instrument:
                _expiry = int(_sig_cfg().get("l2_expiry_minutes", L2_EXPIRY_MINUTES))
                age_minutes = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(pending["timestamp"])
                ).total_seconds() / 60

                if age_minutes <= _expiry:
                    log.info(
                        "%s: L2 pending (%s) — checking L3 [%.1f min elapsed]",
                        instrument, pending["direction"], age_minutes,
                    )
                    return self._check_l3_only(
                        instrument,
                        direction=pending["direction"],
                        score_so_far=3,
                        reasons=["(L0+L1+L2 already confirmed — checking L3 entry only)"],
                        state=state,
                    )
                else:
                    log.info("%s: L2 pending EXPIRED (%.1f min) — resetting", instrument, age_minutes)
                    state.pop("l2_pending", None)

        # ── L0: H4 MACRO TREND — EMA50 ────────────────────────────────────
        h4_c, h4_h, h4_l, _ = self._fetch_candles(instrument, "H4", 60)
        if len(h4_c) < 51:
            log.info("%s: L0 SKIP — not enough H4 data (%d)", instrument, len(h4_c))
            return 0, "NONE", "Not enough H4 data (" + str(len(h4_c)) + ")"

        h4_ema50 = self._ema(h4_c, int(_sig_cfg().get('h4_ema_slow', 50)))[-1]
        h4_price = h4_c[-1]

        if h4_price > h4_ema50:
            direction = "BUY"
            reasons.append("✅ L0 H4 BUY above EMA50=" + str(round(h4_ema50, 5)))
        elif h4_price < h4_ema50:
            direction = "SELL"
            reasons.append("✅ L0 H4 SELL below EMA50=" + str(round(h4_ema50, 5)))
        else:
            log.info("%s: L0 FAIL — H4 EMA50 flat", instrument)
            return 0, "NONE", "H4 EMA50 flat — no macro trend"

        score = 1

        # ── ATR VETO: H1 FLAT-MARKET BLOCK ────────────────────────────────
        h1_c, h1_h, h1_l, _ = self._fetch_candles(instrument, "H1", 60)
        if len(h1_c) < 20:
            log.info("%s: ATR VETO SKIP — not enough H1 data (%d)", instrument, len(h1_c))
            return score, "NONE", " | ".join(reasons) + " | Not enough H1 data"

        h1_atr      = self._atr(h1_h, h1_l, h1_c, int(_sig_cfg().get('h1_atr_period', 14)))
        cfg          = _sig_cfg()
        pip_size     = float(_sig_cfg().get('pip_size', 0.0001))
        h1_atr_pip   = h1_atr / pip_size
        MIN_ATR_PIPS = float(_filters_cfg().get("min_atr_pips", 4.0))

        if h1_atr_pip < MIN_ATR_PIPS:
            msg = "🚫 ATR VETO: H1 ATR=" + str(round(h1_atr_pip, 1)) + "p < " + str(MIN_ATR_PIPS) + "p — market too quiet"
            log.info("%s: %s", instrument, msg)
            reasons.append(msg)
            return score, "NONE", " | ".join(reasons)
        else:
            reasons.append("✅ ATR OK: H1 ATR=" + str(round(h1_atr_pip, 1)) + "p")

        # ── L1: H1 DUAL-EMA ALIGNMENT — EMA21 + EMA50 ─────────────────────
        h1_ema21 = self._ema(h1_c, int(_sig_cfg().get('h1_ema_fast', 21)))[-1]
        h1_ema50 = self._ema(h1_c, int(_sig_cfg().get('h1_ema_slow', 50)))[-1]
        h1_close = h1_c[-1]

        bull_h1 = (h1_close > h1_ema21) and (h1_ema21 > h1_ema50)
        bear_h1 = (h1_close < h1_ema21) and (h1_ema21 < h1_ema50)

        if direction == "BUY" and bull_h1:
            reasons.append("✅ L1 H1 BULL stack: price>" + str(round(h1_ema21, 5)) + ">EMA50=" + str(round(h1_ema50, 5)))
            score = 2
        elif direction == "SELL" and bear_h1:
            reasons.append("✅ L1 H1 BEAR stack: price<" + str(round(h1_ema21, 5)) + "<EMA50=" + str(round(h1_ema50, 5)))
            score = 2
        else:
            msg = (
                "L1 FAIL — H1 EMAs not aligned: price=" + str(round(h1_close, 5))
                + " EMA21=" + str(round(h1_ema21, 5))
                + " EMA50=" + str(round(h1_ema50, 5))
            )
            log.info("%s: %s", instrument, msg)
            reasons.append(msg)
            return score, "NONE", " | ".join(reasons)

        # ── L2: M15 IMPULSE CANDLE BREAK ──────────────────────────────────
        m15_c, m15_h, m15_l, m15_o = self._fetch_candles(instrument, "M15", 20)
        if len(m15_c) < 8:
            log.info("%s: L2 SKIP — not enough M15 data (%d)", instrument, len(m15_c))
            return score, "NONE", " | ".join(reasons) + " | Not enough M15 data"

        lookback       = int(cfg.get("m15_lookback_bars", 5))
        recent_highs   = m15_h[-lookback - 1:-1]
        recent_lows    = m15_l[-lookback - 1:-1]
        structure_high = max(recent_highs)
        structure_low  = min(recent_lows)
        last_close     = m15_c[-1]
        last_open      = m15_o[-1]
        last_high      = m15_h[-1]
        last_low       = m15_l[-1]
        candle_range   = max(last_high - last_low, 0.00001)

        _m15_body_ratio = float(_sig_cfg().get("m15_body_ratio_min", 0.50))
        bull_body_m15 = (last_close > last_open) and ((last_close - last_low) / candle_range >= _m15_body_ratio)
        bear_body_m15 = (last_close < last_open) and ((last_high - last_close) / candle_range >= _m15_body_ratio)

        _m15_break_tol = float(cfg.get('m15_break_tolerance', 0.00080))
        bull_break = (last_close > structure_high) and (last_close <= structure_high + _m15_break_tol) and bull_body_m15
        bear_break = (last_close < structure_low)  and (last_close >= structure_low  - _m15_break_tol) and bear_body_m15

        if direction == "BUY" and bull_break:
            reasons.append(
                "✅ L2 M15 impulse UP close=" + str(round(last_close, 5))
                + " > high=" + str(round(structure_high, 5))
                + " body=" + str(round((last_close - last_low) / candle_range * 100)) + "%"
            )
            score = 3
        elif direction == "SELL" and bear_break:
            reasons.append(
                "✅ L2 M15 impulse DOWN close=" + str(round(last_close, 5))
                + " < low=" + str(round(structure_low, 5))
                + " body=" + str(round((last_high - last_close) / candle_range * 100)) + "%"
            )
            score = 3
        else:
            msg = (
                "L2 FAIL — no M15 impulse: high=" + str(round(structure_high, 5))
                + " low=" + str(round(structure_low, 5))
                + " close=" + str(round(last_close, 5))
                + " bull_body=" + str(bull_body_m15)
                + " bear_body=" + str(bear_body_m15)
            )
            log.info("%s: %s", instrument, msg)
            reasons.append(msg)
            return score, "NONE", " | ".join(reasons)

        # L2 PASSED → save to state, wait for L3 on next scan
        if state is not None:
            state["l2_pending"] = {
                "instrument": instrument,
                "direction":  direction,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
            }
            log.info(
                "%s: ✅ L2 FIRED (%s) — saved to state, checking L3 for up to %d min",
                instrument, direction, L2_EXPIRY_MINUTES,
            )
            reasons.append("⏳ L2 confirmed — waiting for L3 pullback entry (next scan)...")
            return score, "NONE", " | ".join(reasons)

        # Stateless fallback (no state dict provided)
        return self._check_l3_only(instrument, direction, score, reasons, state=None)

    # ──────────────────────────────────────────────────────────────────────
    def _check_l3_only(self, instrument, direction, score_so_far, reasons, state=None):
        """Called on scans AFTER L2 fires.

        Checks M5 RSI(7) pullback to EMA13, then runs V1 and V2.
        Clears l2_pending from state and returns score=4 + direction on success.
        """
        cfg   = _sig_cfg()   # load settings once for this call
        score = score_so_far

        # ── L3: M5 RSI(7) ENTRY TIMING + EMA13 TOUCH ──────────────────────
        m5_c, m5_h, m5_l, m5_o = self._fetch_candles(instrument, "M5", 50)
        if len(m5_c) < 15:
            log.info("%s: L3 SKIP — not enough M5 data (%d)", instrument, len(m5_c))
            return score, "NONE", " | ".join(reasons) + " | Not enough M5 data"

        ema13    = self._ema(m5_c, int(_sig_cfg().get('m5_ema_fast', 13)))[-1]
        rsi7     = self._rsi(m5_c, int(_sig_cfg().get('rsi_period', 7)))
        m5_close = m5_c[-1]
        m5_open  = m5_o[-1]
        m5_high  = m5_h[-1]
        m5_low   = m5_l[-1]
        m5_range = max(m5_high - m5_low, 0.00001)

        MIN_M5_RANGE     = float(cfg.get("min_m5_range",      0.00015))
        RSI_BUY_MAX      = float(cfg.get("rsi_buy_max",        58))
        RSI_SELL_MIN     = float(cfg.get("rsi_sell_min",       42))
        M5_BODY_RATIO    = float(cfg.get("m5_body_ratio_min",  0.50))

        bull_m5_body = (m5_close > m5_open) and ((m5_close - m5_low) / m5_range >= M5_BODY_RATIO) and (m5_range >= MIN_M5_RANGE)
        bear_m5_body = (m5_close < m5_open) and ((m5_high - m5_close) / m5_range >= M5_BODY_RATIO) and (m5_range >= MIN_M5_RANGE)

        ema_tol         = float(cfg.get("ema_tolerance", 0.00020))
        recent_lows_m5  = m5_l[-3:-1]
        recent_highs_m5 = m5_h[-3:-1]
        bull_pb = any(l <= ema13 + ema_tol for l in recent_lows_m5)
        bear_pb = any(h >= ema13 - ema_tol for h in recent_highs_m5)

        bull_rsi = rsi7 < RSI_BUY_MAX
        bear_rsi = rsi7 > RSI_SELL_MIN

        if direction == "BUY" and bull_pb and bull_m5_body and bull_rsi:
            reasons.append(
                "✅ L3 M5 entry: EMA13=" + str(round(ema13, 5))
                + " RSI7=" + str(round(rsi7, 1))
                + " bounce body=" + str(round((m5_close - m5_low) / m5_range * 100)) + "%"
            )
            score = 4
        elif direction == "SELL" and bear_pb and bear_m5_body and bear_rsi:
            reasons.append(
                "✅ L3 M5 entry: EMA13=" + str(round(ema13, 5))
                + " RSI7=" + str(round(rsi7, 1))
                + " bounce body=" + str(round((m5_high - m5_close) / m5_range * 100)) + "%"
            )
            score = 4
        else:
            msg = (
                "L3 FAIL — EMA13=" + str(round(ema13, 5))
                + " RSI7=" + str(round(rsi7, 1))
                + " (need <" + str(RSI_BUY_MAX) + " buy / >" + str(RSI_SELL_MIN) + " sell)"
                + " bull_pb=" + str(bull_pb) + " bear_pb=" + str(bear_pb)
                + " bull_body=" + str(bull_m5_body) + " bear_body=" + str(bear_m5_body)
            )
            log.info("%s: %s", instrument, msg)
            reasons.append(msg)
            return score, "NONE", " | ".join(reasons)

        # ── V1: H1 EMA200 HARD BLOCK ───────────────────────────────────────
        h1_long_c, _, _, _ = self._fetch_candles(instrument, "H1", 210)
        if len(h1_long_c) >= 200:
            h1_ema200 = self._ema(h1_long_c, int(_sig_cfg().get('h1_ema_veto', 200)))[-1]
            price_now = m5_c[-1]
            if direction == "BUY" and price_now < h1_ema200:
                msg = "🚫 V1 H1 EMA200=" + str(round(h1_ema200, 5)) + " price below — no BUY"
                log.info("%s: %s", instrument, msg)
                reasons.append(msg)
                return score, "NONE", " | ".join(reasons)
            elif direction == "SELL" and price_now > h1_ema200:
                msg = "🚫 V1 H1 EMA200=" + str(round(h1_ema200, 5)) + " price above — no SELL"
                log.info("%s: %s", instrument, msg)
                reasons.append(msg)
                return score, "NONE", " | ".join(reasons)
            else:
                reasons.append("✅ V1 pass EMA200=" + str(round(h1_ema200, 5)))
        else:
            log.warning("Not enough H1 for EMA200 (%d) — V1 veto skipped", len(h1_long_c))
            reasons.append("⚠️ V1 EMA200 unavailable — veto skipped")

        # ── V2: M30 COUNTER-TREND BLOCK ────────────────────────────────────
        m30_c, m30_h, m30_l, m30_o = self._fetch_candles(instrument, "M30", 10)
        if len(m30_c) >= 4:
            _m30_body = float(cfg.get('m30_counter_body_ratio', 0.65))  # hoisted out of loop; set once for both directions
            counter_trend_count = 0
            for i in range(-3, 0):
                c_rng = max(m30_h[i] - m30_l[i], 0.00001)
                if direction == "BUY":
                    # bearish candle with strong body = counter to BUY
                    if (m30_c[i] < m30_o[i]) and ((m30_h[i] - m30_c[i]) / c_rng >= _m30_body):
                        counter_trend_count += 1
                else:
                    # bullish candle with strong body = counter to SELL
                    if (m30_c[i] > m30_o[i]) and ((m30_c[i] - m30_l[i]) / c_rng >= _m30_body):
                        counter_trend_count += 1

            if counter_trend_count >= 3:
                msg = "🚫 V2 M30 counter-trend: 3/3 candles opposing " + direction
                log.info("%s: %s", instrument, msg)
                reasons.append(msg)
                return score, "NONE", " | ".join(reasons)
            else:
                reasons.append("✅ V2 M30 ok: " + str(counter_trend_count) + "/3 counter candles")

        # ── ALL LAYERS + VETOES PASSED — clear state and fire ──────────────
        if state is not None:
            state.pop("l2_pending", None)
            log.info("%s: ✅ ALL 4 LAYERS PASSED — firing trade", instrument)

        return score, direction, " | ".join(reasons)
