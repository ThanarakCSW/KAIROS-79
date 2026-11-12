#!/usr/bin/env python3
"""Market Structure, EMA & Support/Resistance Engine (structure.py).

Deterministic calculations for XAU/USD:
  - EMA 20, EMA 50, EMA 200 alignment, distance, and normalized slope
  - Wilder's ATR(14) for zone width and volatility scaling
  - Fractal Swing High / Swing Low detection with HH, HL, LH, LL classification
  - Support & Resistance zone clustering (zones instead of single lines)
"""

import argparse
import json
import math
import sys
from typing import Any, Dict, List, Optional


def r2(val: Optional[float]) -> Optional[float]:
    if val is None or math.isnan(val) or math.isinf(val):
        return None
    return round(float(val), 2) + 0.0


def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    """Compute standard Exponential Moving Average series."""
    n = len(values)
    if n == 0 or period <= 0:
        return []
    out: List[Optional[float]] = [None] * n
    if n < period:
        # Fallback cumulative average for short series so charts still render
        running = 0.0
        for i, v in enumerate(values):
            running += float(v)
            out[i] = round(running / (i + 1), 2)
        return out

    sma = sum(float(x) for x in values[:period]) / float(period)
    out[period - 1] = round(sma, 2)
    k = 2.0 / (period + 1.0)
    prev = sma
    for i in range(period, n):
        curr = (float(values[i]) - prev) * k + prev
        out[i] = round(curr, 2)
        prev = curr
    return out


def atr_series(bars: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    """Wilder's Average True Range (ATR) series on OHLC bars."""
    n = len(bars)
    if n == 0:
        return []
    trs: List[float] = []
    for i, b in enumerate(bars):
        h, l = float(b["h"]), float(b["l"])
        if i == 0:
            trs.append(max(0.0, h - l))
        else:
            pc = float(bars[i - 1]["c"])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    out: List[Optional[float]] = [None] * n
    if n < period:
        avg = sum(trs) / float(n)
        return [round(avg, 2) for _ in range(n)]

    curr = sum(trs[:period]) / float(period)
    out[period - 1] = round(curr, 2)
    for i in range(period, n):
        curr = (curr * (period - 1) + trs[i]) / float(period)
        out[i] = round(curr, 2)
    return out


def detect_swings(
    bars: List[Dict[str, Any]],
    left: int = 3,
    right: int = 3,
    eq_tolerance: float = 0.35,
) -> Dict[str, List[Dict[str, Any]]]:
    """Identify Fractal Swing Highs & Swing Lows and label HH/LH/EH and HL/LL/EL."""
    n = len(bars)
    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []
    if n < left + right + 1:
        return {"highs": highs, "lows": lows, "all": []}

    for i in range(left, n - right):
        hi = float(bars[i]["h"])
        lo = float(bars[i]["l"])

        is_swing_high = all(hi > float(bars[j]["h"]) for j in range(i - left, i)) and all(
            hi >= float(bars[j]["h"]) for j in range(i + 1, i + right + 1)
        )
        is_swing_low = all(lo < float(bars[j]["l"]) for j in range(i - left, i)) and all(
            lo <= float(bars[j]["l"]) for j in range(i + 1, i + right + 1)
        )

        if is_swing_high:
            prev_hi = highs[-1]["price"] if highs else None
            if prev_hi is None:
                label = "SH"
            elif abs(hi - prev_hi) <= eq_tolerance:
                label = "EQH"
            elif hi > prev_hi:
                label = "HH"
            else:
                label = "LH"
            highs.append(
                {
                    "index": i,
                    "t": int(bars[i]["t"]),
                    "price": r2(hi),
                    "kind": "high",
                    "label": label,
                }
            )

        if is_swing_low:
            prev_lo = lows[-1]["price"] if lows else None
            if prev_lo is None:
                label = "SL"
            elif abs(lo - prev_lo) <= eq_tolerance:
                label = "EQL"
            elif lo > prev_lo:
                label = "HL"
            else:
                label = "LL"
            lows.append(
                {
                    "index": i,
                    "t": int(bars[i]["t"]),
                    "price": r2(lo),
                    "kind": "low",
                    "label": label,
                }
            )

    combined = sorted(highs + lows, key=lambda x: (x["index"], 0 if x["kind"] == "low" else 1))
    return {"highs": highs, "lows": lows, "all": combined}


def cluster_sr_zones(
    swings: Dict[str, List[Dict[str, Any]]],
    current_price: float,
    atr_val: float,
    cluster_atr_mult: float = 0.55,
    max_zones: int = 8,
) -> List[Dict[str, Any]]:
    """Cluster swing points into Support & Resistance price zones bounded by ATR width."""
    pts = swings.get("all", [])
    if not pts:
        return []

    band = max(0.8, atr_val * cluster_atr_mult)
    half_zone = max(0.4, atr_val * 0.25)
    sorted_pts = sorted(pts, key=lambda x: x["price"])

    clusters: List[List[Dict[str, Any]]] = []
    for p in sorted_pts:
        if not clusters:
            clusters.append([p])
            continue
        last_cluster = clusters[-1]
        cluster_mid = sum(item["price"] for item in last_cluster) / len(last_cluster)
        if abs(p["price"] - cluster_mid) <= band:
            last_cluster.append(p)
        else:
            clusters.append([p])

    zones: List[Dict[str, Any]] = []
    for cl in clusters:
        prices = [x["price"] for x in cl]
        mid = sum(prices) / len(prices)
        low = min(min(prices), mid - half_zone)
        high = max(max(prices), mid + half_zone)
        # Keep zone compact around mid if outliers stretched it too wide
        if high - low > atr_val * 1.1:
            low = mid - atr_val * 0.45
            high = mid + atr_val * 0.45

        touches = len(cl)
        high_cnt = sum(1 for x in cl if x["kind"] == "high")
        low_cnt = sum(1 for x in cl if x["kind"] == "low")
        is_flip = high_cnt > 0 and low_cnt > 0
        role = "support" if mid <= current_price else "resistance"
        strength = min(100, touches * 25 + (15 if is_flip else 0))

        zones.append(
            {
                "role": role,
                "is_flip": is_flip,
                "low": r2(low),
                "high": r2(high),
                "mid": r2(mid),
                "touches": touches,
                "strength": strength,
                "first_t": min(x["t"] for x in cl),
                "last_t": max(x["t"] for x in cl),
                "distance_usd": r2(abs(current_price - mid)),
            }
        )

    # Sort by relevance: multi-touch or close to current price first
    zones.sort(key=lambda z: (-z["touches"], z["distance_usd"]))
    return zones[:max_zones]


def evaluate_trend_regime(
    current_price: float,
    ema20: Optional[float],
    ema50: Optional[float],
    ema200: Optional[float],
    ema20_prev: Optional[float],
    ema50_prev: Optional[float],
    atr_val: float,
    swings: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Determine market regime ('Strong Trend', 'Pullback', or 'Ranging') and directional bias."""
    e20 = ema20 if ema20 is not None else current_price
    e50 = ema50 if ema50 is not None else current_price
    e200 = ema200 if ema200 is not None else e50

    slope20 = ((e20 - ema20_prev) / max(0.5, atr_val)) if ema20_prev is not None else 0.0
    slope50 = ((e50 - ema50_prev) / max(0.5, atr_val)) if ema50_prev is not None else 0.0

    recent_highs = [h["label"] for h in swings.get("highs", [])[-2:]]
    recent_lows = [l["label"] for l in swings.get("lows", [])[-2:]]

    bull_ema = e20 > e50 >= e200
    bear_ema = e20 < e50 <= e200
    bull_swings = ("HH" in recent_highs) and ("HL" in recent_lows)
    bear_swings = ("LH" in recent_highs) and ("LL" in recent_lows)

    if (bull_ema or bull_swings) and current_price >= e20 and slope20 > 0.08:
        regime = "Strong Trend"
        bias = "BULLISH"
    elif (bear_ema or bear_swings) and current_price <= e20 and slope20 < -0.08:
        regime = "Strong Trend"
        bias = "BEARISH"
    elif (e50 >= e200 or bull_swings) and current_price < e20 and current_price >= e200 - atr_val:
        regime = "Pullback"
        bias = "BULLISH"
    elif (e50 <= e200 or bear_swings) and current_price > e20 and current_price <= e200 + atr_val:
        regime = "Pullback"
        bias = "BEARISH"
    else:
        if e20 > e50 and slope20 > 0.03:
            regime = "Pullback"
            bias = "BULLISH"
        elif e20 < e50 and slope20 < -0.03:
            regime = "Pullback"
            bias = "BEARISH"
        else:
            regime = "Ranging"
            bias = "NEUTRAL"

    return {
        "regime": regime,
        "bias": bias,
        "ema_alignment": (
            "BULLISH_STACK" if bull_ema else "BEARISH_STACK" if bear_ema else "MIXED"
        ),
        "ema20": r2(e20),
        "ema50": r2(e50),
        "ema200": r2(e200),
        "dist_ema20_usd": r2(current_price - e20),
        "dist_ema50_usd": r2(current_price - e50),
        "dist_ema200_usd": r2(current_price - e200),
        "slope_ema20_atr": round(slope20, 3),
        "slope_ema50_atr": round(slope50, 3),
        "recent_high_labels": recent_highs,
        "recent_low_labels": recent_lows,
    }


def analyze_candlestick_behavior(
    bars: List[Dict[str, Any]],
    atr_val: float,
) -> Dict[str, Any]:
    """Analyze historical candlestick patterns, wick absorption/rejection, and dealing range."""
    if not bars:
        return {}

    lookback_bars = bars[-60:] if len(bars) >= 60 else bars
    recent_30 = bars[-30:] if len(bars) >= 30 else bars
    safe_atr = max(0.3, atr_val)

    bull_body_sum = 0.0
    bear_body_sum = 0.0
    lower_wick_sum = 0.0
    upper_wick_sum = 0.0

    for b in recent_30:
        o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
        body = abs(c - o)
        if c >= o:
            bull_body_sum += body
        else:
            bear_body_sum += body
        lower_wick_sum += max(0.0, min(o, c) - l)
        upper_wick_sum += max(0.0, h - max(o, c))

    total_body = max(0.01, bull_body_sum + bear_body_sum)
    bull_pressure_pct = round((bull_body_sum / total_body) * 100.0, 1)

    avg_lower_wick = lower_wick_sum / max(1, len(recent_30))
    avg_upper_wick = upper_wick_sum / max(1, len(recent_30))

    if avg_lower_wick > avg_upper_wick * 1.22:
        wick_dominance = "BUYER_ABSORPTION"
        wick_summary_th = "มีแรงซื้อดักรับด้านล่าง (ทิ้งไส้ล่างยาวสะสม)"
    elif avg_upper_wick > avg_lower_wick * 1.22:
        wick_dominance = "SELLER_REJECTION"
        wick_summary_th = "มีแรงขายกดทับด้านบน (ทิ้งไส้บนยาวปฏิเสธราคา)"
    else:
        wick_dominance = "BALANCED"
        wick_summary_th = "แรงซื้อ-ขายจากไส้เทียนสมดุลกัน"

    # Dealing Range (60 bars) -> Premium / Equilibrium / Discount
    r_high = max(float(b["h"]) for b in lookback_bars)
    r_low = min(float(b["l"]) for b in lookback_bars)
    r_span = max(0.5, r_high - r_low)
    curr_c = float(bars[-1]["c"])
    pos_pct = round(((curr_c - r_low) / r_span) * 100.0, 1)
    if pos_pct <= 38.0:
        zone_name = "DISCOUNT"
        zone_th = f"โซนของถูก (Discount {pos_pct:.0f}% ของกรอบ 60 แท่ง) ได้เปรียบฝั่ง BUY"
    elif pos_pct >= 62.0:
        zone_name = "PREMIUM"
        zone_th = f"โซนของแพง (Premium {pos_pct:.0f}% ของกรอบ 60 แท่ง) ได้เปรียบฝั่ง SELL"
    else:
        zone_name = "EQUILIBRIUM"
        zone_th = f"กึ่งกลางกรอบราคา (Equilibrium {pos_pct:.0f}%)"

    # Scan last 35 bars for notable candlestick patterns
    patterns: List[Dict[str, Any]] = []
    start_idx = max(1, len(bars) - 35)
    for i in range(start_idx, len(bars)):
        prev = bars[i - 1]
        curr = bars[i]
        po, ph, pl, pc = float(prev["o"]), float(prev["h"]), float(prev["l"]), float(prev["c"])
        co, ch, cl, cc = float(curr["o"]), float(curr["h"]), float(curr["l"]), float(curr["c"])
        p_body = abs(pc - po)
        c_body = abs(cc - co)
        c_range = max(0.01, ch - cl)
        l_wick = max(0.0, min(co, cc) - cl)
        u_wick = max(0.0, ch - max(co, cc))

        # 1. Bullish Engulfing
        if pc < po and cc > co and cc >= po and co <= pc and c_body >= max(p_body * 1.05, safe_atr * 0.35):
            patterns.append({
                "index": i,
                "t": int(curr["t"]),
                "pattern": "BULLISH_ENGULFING",
                "bias": "BULLISH",
                "price": r2(cc),
                "label_th": "Bullish Engulfing (แท่งเขียวกลืนกินขาขึ้น)",
            })
        # 2. Bearish Engulfing
        elif pc > po and cc < co and cc <= po and co >= pc and c_body >= max(p_body * 1.05, safe_atr * 0.35):
            patterns.append({
                "index": i,
                "t": int(curr["t"]),
                "pattern": "BEARISH_ENGULFING",
                "bias": "BEARISH",
                "price": r2(cc),
                "label_th": "Bearish Engulfing (แท่งแดงกลืนกินขาลง)",
            })
        # 3. Bullish Pin Bar / Hammer Rejection
        elif l_wick >= max(c_body * 1.8, safe_atr * 0.45) and (l_wick / c_range) >= 0.55 and u_wick <= l_wick * 0.45:
            patterns.append({
                "index": i,
                "t": int(curr["t"]),
                "pattern": "BULLISH_PINBAR",
                "bias": "BULLISH",
                "price": r2(cl),
                "label_th": "Bullish Pin Bar (ไส้ล่างยาวปฏิเสธราคาลง)",
            })
        # 4. Bearish Pin Bar / Shooting Star Rejection
        elif u_wick >= max(c_body * 1.8, safe_atr * 0.45) and (u_wick / c_range) >= 0.55 and l_wick <= u_wick * 0.45:
            patterns.append({
                "index": i,
                "t": int(curr["t"]),
                "pattern": "BEARISH_PINBAR",
                "bias": "BEARISH",
                "price": r2(ch),
                "label_th": "Shooting Star / Pin Bar (ไส้บนยาวปฏิเสธราคาขึ้น)",
            })
        # 5. Strong Momentum Impulse (Marubozu)
        elif c_body >= safe_atr * 1.35 and (c_body / c_range) >= 0.75:
            is_bull = cc > co
            patterns.append({
                "index": i,
                "t": int(curr["t"]),
                "pattern": "BULLISH_IMPULSE" if is_bull else "BEARISH_IMPULSE",
                "bias": "BULLISH" if is_bull else "BEARISH",
                "price": r2(cc),
                "label_th": "แท่งเขียวเต็มแท่งพุ่งแรง (Impulse)" if is_bull else "แท่งแดงเต็มแท่งทุบแรง (Impulse)",
            })

    return {
        "lookback_bars": len(lookback_bars),
        "bullish_pressure_pct": bull_pressure_pct,
        "bearish_pressure_pct": round(100.0 - bull_pressure_pct, 1),
        "avg_lower_wick_usd": r2(avg_lower_wick),
        "avg_upper_wick_usd": r2(avg_upper_wick),
        "wick_dominance": wick_dominance,
        "wick_summary_th": wick_summary_th,
        "dealing_range": {
            "low": r2(r_low),
            "high": r2(r_high),
            "equilibrium": r2((r_low + r_high) / 2.0),
            "position_pct": pos_pct,
            "zone": zone_name,
            "summary_th": zone_th,
        },
        "recent_patterns": patterns[-8:],
    }


def rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """Compute Wilder's Relative Strength Index (RSI) series."""
    n = len(closes)
    if n == 0:
        return []
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return [50.0 for _ in range(n)]

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        diff = float(closes[i]) - float(closes[i - 1])
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))

    avg_gain = sum(gains) / float(period)
    avg_loss = sum(losses) / float(period)
    out[period] = 100.0 if avg_loss == 0 else round(100.0 - (100.0 / (1.0 + avg_gain / avg_loss)), 2)

    for i in range(period + 1, n):
        diff = float(closes[i]) - float(closes[i - 1])
        g = max(0.0, diff)
        l = max(0.0, -diff)
        avg_gain = (avg_gain * (period - 1) + g) / float(period)
        avg_loss = (avg_loss * (period - 1) + l) / float(period)
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = round(100.0 - (100.0 / (1.0 + rs)), 2)
    return out


def analyze_rsi_divergence(
    bars: List[Dict[str, Any]],
    swings: Dict[str, List[Dict[str, Any]]],
    rsi_list: List[Optional[float]],
) -> Dict[str, Any]:
    """Detect Regular & Hidden RSI(14) Divergences at swing highs and lows."""
    curr_rsi = float(rsi_list[-1] if rsi_list and rsi_list[-1] is not None else 50.0)
    if curr_rsi >= 70.0:
        state = "OVERBOUGHT"
        state_th = f"Overbought ({curr_rsi:.1f}) — ระวังแรงเทขายทำกำไร"
    elif curr_rsi <= 30.0:
        state = "OVERSOLD"
        state_th = f"Oversold ({curr_rsi:.1f}) — ระวังการดีดกลับทางเทคนิค"
    elif curr_rsi >= 55.0:
        state = "BULLISH_MOMENTUM"
        state_th = f"โมเมนตัมฝั่งซื้อได้เปรียบ (RSI {curr_rsi:.1f})"
    elif curr_rsi <= 45.0:
        state = "BEARISH_MOMENTUM"
        state_th = f"โมเมนตัมฝั่งขายกดดัน (RSI {curr_rsi:.1f})"
    else:
        state = "NEUTRAL"
        state_th = f"โมเมนตัมเป็นกลาง (RSI {curr_rsi:.1f})"

    div_type = "NONE"
    div_bias = "NEUTRAL"
    div_label_th = "ไม่พบสัญญาณขัดแย้ง (Divergence) ที่จุดสวิงล่าสุด"

    lows = [s for s in swings.get("lows", []) if s["index"] < len(rsi_list) and rsi_list[s["index"]] is not None]
    highs = [s for s in swings.get("highs", []) if s["index"] < len(rsi_list) and rsi_list[s["index"]] is not None]

    if len(lows) >= 2:
        l1, l2 = lows[-2], lows[-1]
        r1, r2_val = float(rsi_list[l1["index"]] or 50.0), float(rsi_list[l2["index"]] or 50.0)
        if l2["price"] < l1["price"] and r2_val > r1 + 1.5:
            div_type = "REGULAR_BULLISH_DIV"
            div_bias = "BULLISH"
            div_label_th = f"Bullish Divergence (ราคาทำ Low ต่ำลงที่ {l2['price']:.2f} แต่ RSI ยกตัว {r1:.0f} → {r2_val:.0f} ยืนยันแรงขายหมด)"
        elif l2["price"] > l1["price"] and r2_val < r1 - 1.5:
            div_type = "HIDDEN_BULLISH_DIV"
            div_bias = "BULLISH"
            div_label_th = f"Hidden Bullish Divergence (ราคายก HL ที่ {l2['price']:.2f} แต่ RSI พักตัวลง {r1:.0f} → {r2_val:.0f} หนุนไปต่อตามเทรนด์ขึ้น)"

    if div_type == "NONE" and len(highs) >= 2:
        h1, h2 = highs[-2], highs[-1]
        r1, r2_val = float(rsi_list[h1["index"]] or 50.0), float(rsi_list[h2["index"]] or 50.0)
        if h2["price"] > h1["price"] and r2_val < r1 - 1.5:
            div_type = "REGULAR_BEARISH_DIV"
            div_bias = "BEARISH"
            div_label_th = f"Bearish Divergence (ราคาทำ High สูงขึ้นที่ {h2['price']:.2f} แต่ RSI ลดลง {r1:.0f} → {r2_val:.0f} เตือนแรงซื้ออ่อนแรง)"
        elif h2["price"] < h1["price"] and r2_val > r1 + 1.5:
            div_type = "HIDDEN_BEARISH_DIV"
            div_bias = "BEARISH"
            div_label_th = f"Hidden Bearish Divergence (ราคากด LH ที่ {h2['price']:.2f} แต่ RSI ดีดตึง {r1:.0f} → {r2_val:.0f} หนุนลงต่อตามเทรนด์ลง)"

    return {
        "rsi14": r2(curr_rsi),
        "rsi_state": state,
        "rsi_state_th": state_th,
        "divergence": div_type,
        "divergence_bias": div_bias,
        "summary_th": div_label_th,
    }


def compute_institutional_levels(
    bars: List[Dict[str, Any]],
    current_price: float,
    atr_val: float,
) -> Dict[str, Any]:
    """Compute PDH/PDL (Previous Day High/Low), Asian Session Range, Session VWAP, and Volume Profile (POC/VAH/VAL)."""
    if not bars:
        return {}

    n = len(bars)
    # 1. Previous Day High / Low (PDH / PDL) & Current Day High / Low
    # Group bars by UTC day (t // 86400) if span > 1 day, else split historical window into prior half & recent half
    day_buckets: Dict[int, List[Dict[str, Any]]] = {}
    for b in bars:
        day_id = int(b["t"]) // 86400
        day_buckets.setdefault(day_id, []).append(b)

    sorted_days = sorted(day_buckets.keys())
    if len(sorted_days) >= 2 and len(day_buckets[sorted_days[-2]]) >= 6 and len(day_buckets[sorted_days[-1]]) >= 1:
        prev_day_bars = day_buckets[sorted_days[-2]]
        curr_day_bars = day_buckets[sorted_days[-1]]
    else:
        split_idx = max(1, min(n - 1, int(n * 0.55)))
        prev_day_bars = bars[:split_idx]
        curr_day_bars = bars[split_idx:] or bars[-1:]

    pdh = max(float(b["h"]) for b in prev_day_bars)
    pdl = min(float(b["l"]) for b in prev_day_bars)
    pdc = float(prev_day_bars[-1]["c"])

    curr_high = max(float(b["h"]) for b in curr_day_bars)
    curr_low = min(float(b["l"]) for b in curr_day_bars)
    pdh_swept = curr_high > pdh and current_price < pdh
    pdl_swept = curr_low < pdl and current_price > pdl

    # 2. Asian Session Range (00:00 - 06:00 UTC -> hours 0..5)
    asian_bars = [
        b for b in curr_day_bars if 0 <= ((int(b["t"]) % 86400) // 3600) < 6
    ]
    if len(asian_bars) < 4:
        # Fallback to first third of current session window
        a_len = max(6, len(curr_day_bars) // 3)
        asian_bars = curr_day_bars[:a_len]

    asian_high = max(float(b["h"]) for b in asian_bars)
    asian_low = min(float(b["l"]) for b in asian_bars)
    post_asian_bars = curr_day_bars[len(asian_bars) :] or curr_day_bars[-10:]
    post_high = max(float(b["h"]) for b in post_asian_bars)
    post_low = min(float(b["l"]) for b in post_asian_bars)

    if post_high > asian_high and current_price < asian_high:
        asian_status = "ASIAN_HIGH_SWEPT_BEARISH"
        asian_th = f"กวาดหัวกรอบเอเชีย ({asian_high:.2f}) แล้วดึงกลับ (ระวังแรงเทขายย้อนกลับ)"
    elif post_low < asian_low and current_price > asian_low:
        asian_status = "ASIAN_LOW_SWEPT_BULLISH"
        asian_th = f"กวาดหางกรอบเอเชีย ({asian_low:.2f}) แล้วดึงกลับ (หนุนแรงซื้อดีดขึ้น)"
    elif current_price > asian_high:
        asian_status = "BREAKOUT_ABOVE_ASIAN"
        asian_th = f"ยืนเหนือกรอบเอเชีย ({asian_low:.2f}–{asian_high:.2f})"
    elif current_price < asian_low:
        asian_status = "BREAKOUT_BELOW_ASIAN"
        asian_th = f"หลุดใต้กรอบเอเชีย ({asian_low:.2f}–{asian_high:.2f})"
    else:
        asian_status = "INSIDE_ASIAN_RANGE"
        asian_th = f"แกว่งในกรอบเอเชีย ({asian_low:.2f}–{asian_high:.2f}) รอเลือกทาง"

    # 3. Session VWAP & 20-Bin Volume Profile (POC / VAH / VAL)
    vp_bars = bars[-120:] if n >= 120 else bars
    vwap_num = 0.0
    vwap_den = 0.0
    for b in curr_day_bars:
        typ = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        vol = float(b.get("v") or 0)
        weight = vol if vol > 0 else max(0.5, float(b["h"]) - float(b["l"])) * 100.0
        vwap_num += typ * weight
        vwap_den += weight
    vwap = vwap_num / max(1.0, vwap_den)

    vp_low = min(float(b["l"]) for b in vp_bars)
    vp_high = max(float(b["h"]) for b in vp_bars)
    vp_span = max(1.0, vp_high - vp_low)
    num_bins = 20
    bin_size = vp_span / float(num_bins)
    bins = [0.0] * num_bins

    for b in vp_bars:
        typ = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        vol = float(b.get("v") or 0)
        weight = vol if vol > 0 else max(0.5, float(b["h"]) - float(b["l"])) * 100.0
        idx = min(num_bins - 1, max(0, int((typ - vp_low) / bin_size)))
        bins[idx] += weight

    poc_idx = max(range(num_bins), key=lambda k: bins[k])
    poc_price = vp_low + (poc_idx + 0.5) * bin_size

    # Expand from POC until 70% of volume is captured for Value Area (VAL - VAH)
    total_vp_vol = max(1.0, sum(bins))
    captured = bins[poc_idx]
    lo_idx, hi_idx = poc_idx, poc_idx
    while captured < total_vp_vol * 0.70 and (lo_idx > 0 or hi_idx < num_bins - 1):
        next_lo = bins[lo_idx - 1] if lo_idx > 0 else -1.0
        next_hi = bins[hi_idx + 1] if hi_idx < num_bins - 1 else -1.0
        if next_hi >= next_lo and hi_idx < num_bins - 1:
            hi_idx += 1
            captured += bins[hi_idx]
        elif lo_idx > 0:
            lo_idx -= 1
            captured += bins[lo_idx]
        else:
            break

    val_price = vp_low + lo_idx * bin_size
    vah_price = vp_low + (hi_idx + 1) * bin_size

    return {
        "pdh": r2(pdh),
        "pdl": r2(pdl),
        "pdc": r2(pdc),
        "pdh_swept": pdh_swept,
        "pdl_swept": pdl_swept,
        "asian_high": r2(asian_high),
        "asian_low": r2(asian_low),
        "asian_status": asian_status,
        "asian_summary_th": asian_th,
        "vwap": r2(vwap),
        "dist_vwap_usd": r2(current_price - vwap),
        "price_vs_vwap": "ABOVE_VWAP" if current_price >= vwap else "BELOW_VWAP",
        "poc": r2(poc_price),
        "vah": r2(vah_price),
        "val": r2(val_price),
    }


def analyze_scalping_signals(
    bars: List[Dict[str, Any]],
    closes: List[float],
    current_price: float,
    atr_val: float,
    inst_levels: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute 5 Gold Scalping Techniques:
    1) EMA 9 / 21 Fast Momentum Ribbon & Pullback Bounce
    2) VWAP Deviation Bands (±1.5x / ±2.2x ATR) Mean-Reversion & Squeeze Breakout
    3) Micro-Liquidity Sweep (5-15 bar High/Low wick sweep & close back inside)
    4) Fast RSI(7) + Stochastic RSI (14,3,3) Hook
    5) Micro Entry Zone & Target Calibration
    """
    n = len(bars)
    safe_atr = max(0.3, atr_val)
    e9_list = ema_series(closes, 9)
    e21_list = ema_series(closes, 21)
    rsi7_list = rsi_series(closes, 7)
    rsi14_list = rsi_series(closes, 14)

    e9 = float(e9_list[-1] or current_price)
    e21 = float(e21_list[-1] or current_price)
    rsi7 = float(rsi7_list[-1] or 50.0)
    rsi7_prev = float(rsi7_list[-2] or rsi7) if n >= 2 else rsi7

    # Stochastic RSI (14-period window on RSI14, 3-bar smoothing)
    valid_r14 = [float(x if x is not None else 50.0) for x in rsi14_list]
    raw_stoch: List[float] = []
    for i in range(n):
        w = valid_r14[max(0, i - 13) : i + 1]
        lo_r, hi_r = min(w), max(w)
        raw_stoch.append(50.0 if hi_r - lo_r < 0.1 else ((valid_r14[i] - lo_r) / (hi_r - lo_r)) * 100.0)
    stoch_k = round(sum(raw_stoch[-3:]) / min(3, len(raw_stoch)), 1)
    stoch_k_prev = round(sum(raw_stoch[-4:-1]) / max(1, len(raw_stoch[-4:-1])), 1) if n >= 4 else stoch_k

    bull_points = 0
    bear_points = 0
    triggers: List[str] = []
    triggers_th: List[str] = []

    # 1. EMA 9 / 21 Fast Momentum Ribbon & Micro-Pullback
    last_bar = bars[-1]
    lo_last, hi_last, op_last, cl_last = (
        float(last_bar["l"]),
        float(last_bar["h"]),
        float(last_bar["o"]),
        float(last_bar["c"]),
    )
    if e9 >= e21:
        bull_points += 20
        ribbon_state = "BULL_RIBBON"
        if lo_last <= max(e9, e21) + safe_atr * 0.25 and cl_last >= e9:
            bull_points += 18
            triggers.append("EMA9/21 Pullback Bounce (BUY)")
            triggers_th.append(f"ราคาย่อแตะเส้นความเร็วสูง EMA9/21 ({e9:.2f}) แล้วดีดกลับ")
    else:
        bear_points += 20
        ribbon_state = "BEAR_RIBBON"
        if hi_last >= min(e9, e21) - safe_atr * 0.25 and cl_last <= e9:
            bear_points += 18
            triggers.append("EMA9/21 Pullback Reject (SELL)")
            triggers_th.append(f"ราคาเด้งชนเส้นความเร็วสูง EMA9/21 ({e9:.2f}) แล้วถูกตบลง")

    # 2. VWAP Deviation Bands (±1.5 ATR / ±2.2 ATR) & Volatility Squeeze Breakout
    vwap = float(inst_levels.get("vwap") or current_price)
    vwap_up_15 = round(vwap + safe_atr * 1.5, 2)
    vwap_dn_15 = round(vwap - safe_atr * 1.5, 2)
    vwap_up_22 = round(vwap + safe_atr * 2.2, 2)
    vwap_dn_22 = round(vwap - safe_atr * 2.2, 2)

    if lo_last <= vwap_dn_15 and cl_last > op_last:
        bull_points += 22
        vwap_band_state = "OVERSOLD_BELOW_LOWER_BAND"
        triggers.append(f"VWAP Lower Band Bounce ({vwap_dn_15:.2f})")
        triggers_th.append(f"ราคาฉีกต่ำกว่ากรอบ VWAP Band ล่าง ({vwap_dn_15:.2f}) แล้วดีดกลับหาค่าเฉลี่ย")
    elif hi_last >= vwap_up_15 and cl_last < op_last:
        bear_points += 22
        vwap_band_state = "OVERBOUGHT_ABOVE_UPPER_BAND"
        triggers.append(f"VWAP Upper Band Reject ({vwap_up_15:.2f})")
        triggers_th.append(f"ราคาฉีกสูงกว่ากรอบ VWAP Band บน ({vwap_up_15:.2f}) แล้วถูกตบกลับหาค่าเฉลี่ย")
    else:
        vwap_band_state = "NORMAL_VWAP_CHANNEL"

    # Volatility Squeeze & Breakout (last 5 bars)
    squeeze_state = "NORMAL"
    if n >= 6:
        prior4 = bars[-5:-1]
        avg_r4 = sum(float(b["h"]) - float(b["l"]) for b in prior4) / 4.0
        hi4 = max(float(b["h"]) for b in prior4)
        lo4 = min(float(b["l"]) for b in prior4)
        curr_rng = hi_last - lo_last
        if avg_r4 <= safe_atr * 0.85 and curr_rng >= safe_atr * 0.95:
            if cl_last > hi4:
                bull_points += 20
                squeeze_state = "BULL_SQUEEZE_BREAKOUT"
                triggers.append(f"Volatility Squeeze Breakout UP ({hi4:.2f})")
                triggers_th.append(f"บีบกรอบแคบแล้วพุ่งทะลุ High ย่อย ({hi4:.2f}) พร้อมโมเมนตัม")
            elif cl_last < lo4:
                bear_points += 20
                squeeze_state = "BEAR_SQUEEZE_BREAKOUT"
                triggers.append(f"Volatility Squeeze Breakout DOWN ({lo4:.2f})")
                triggers_th.append(f"บีบกรอบแคบแล้วทุบหลุด Low ย่อย ({lo4:.2f}) พร้อมโมเมนตัม")

    # 3. Micro-Liquidity Sweep (5-12 bars lookback)
    micro_sweep = "NONE"
    if n >= 12:
        ref_window = bars[-12:-2]
        micro_hi = max(float(b["h"]) for b in ref_window)
        micro_lo = min(float(b["l"]) for b in ref_window)
        recent2_lo = min(float(b["l"]) for b in bars[-2:])
        recent2_hi = max(float(b["h"]) for b in bars[-2:])
        if recent2_lo < micro_lo and cl_last > micro_lo:
            bull_points += 24
            micro_sweep = "BULLISH_MICRO_SWEEP"
            triggers.append(f"Micro-Liquidity Sweep Low ({micro_lo:.2f})")
            triggers_th.append(f"กวาด Stop Loss ขอบล่างระยะสั้น ({micro_lo:.2f}) แล้วดึงกลับทันที")
        elif recent2_hi > micro_hi and cl_last < micro_hi:
            bear_points += 24
            micro_sweep = "BEARISH_MICRO_SWEEP"
            triggers.append(f"Micro-Liquidity Sweep High ({micro_hi:.2f})")
            triggers_th.append(f"กวาด Stop Loss ขอบบนระยะสั้น ({micro_hi:.2f}) แล้วตบกลับทันที")

    # 4. Fast RSI(7) + Stochastic RSI Hook
    if (stoch_k <= 35.0 and stoch_k >= stoch_k_prev) or (rsi7 <= 38.0 and rsi7 >= rsi7_prev):
        bull_points += 16
        triggers.append(f"StochRSI/RSI7 Hook UP (K={stoch_k:.0f}, RSI7={rsi7:.0f})")
        triggers_th.append(f"StochRSI ({stoch_k:.0f}) & RSI7 ({rsi7:.0f}) หักหัวขึ้นจากโซนขายมากเกินไป")
    elif (stoch_k >= 65.0 and stoch_k <= stoch_k_prev) or (rsi7 >= 62.0 and rsi7 <= rsi7_prev):
        bear_points += 16
        triggers.append(f"StochRSI/RSI7 Hook DOWN (K={stoch_k:.0f}, RSI7={rsi7:.0f})")
        triggers_th.append(f"StochRSI ({stoch_k:.0f}) & RSI7 ({rsi7:.0f}) หักหัวลงจากโซนซื้อมากเกินไป")

    scalp_bias = "BULLISH" if bull_points >= bear_points else "BEARISH"
    scalp_score = min(100, max(52, 48 + max(bull_points, bear_points)))

    return {
        "scalp_bias": scalp_bias,
        "scalp_score": scalp_score,
        "ema9": r2(e9),
        "ema21": r2(e21),
        "ribbon_state": ribbon_state,
        "rsi7": r2(rsi7),
        "stoch_rsi_k": stoch_k,
        "vwap_upper_1_5": vwap_up_15,
        "vwap_lower_1_5": vwap_dn_15,
        "vwap_upper_2_2": vwap_up_22,
        "vwap_lower_2_2": vwap_dn_22,
        "vwap_band_state": vwap_band_state,
        "squeeze_state": squeeze_state,
        "micro_sweep": micro_sweep,
        "triggers": triggers or [f"Fast {ribbon_state} (EMA9={e9:.2f}, EMA21={e21:.2f})"],
        "triggers_th": triggers_th or [f"เคลื่อนไหวตามเส้นความเร็วสูง EMA9 ({e9:.2f}) / EMA21 ({e21:.2f})"],
    }


def analyze_volume_and_flow(
    bars: List[Dict[str, Any]],
    trend: Dict[str, Any],
    atr_val: float,
    inst_levels: Dict[str, Any],
) -> Dict[str, Any]:
    """Analyze real bar volume, Relative Volume (RVOL), Buy vs Sell volume delta, and baseline market flow."""
    window = bars[-20:] if len(bars) >= 20 else bars
    vols = [max(1, int(b.get("v") or 1)) for b in window]
    curr_vol = vols[-1]
    avg_vol_20 = round(sum(vols) / len(vols), 1)
    recent_3_vol = round(sum(vols[-3:]) / min(3, len(vols)), 1)

    rvol_curr = round(curr_vol / max(1.0, avg_vol_20), 2)
    rvol_3bar = round(recent_3_vol / max(1.0, avg_vol_20), 2)
    eff_rvol = round(max(rvol_curr, rvol_3bar), 2)

    # Compute Buying Volume vs Selling Volume over the 20-bar window using intrabar close position
    buy_vol_sum = 0.0
    sell_vol_sum = 0.0
    ranges_20: List[float] = []
    for b in window:
        hi, lo, cl, op = float(b["h"]), float(b["l"]), float(b["c"]), float(b["o"])
        v = max(1.0, float(b.get("v") or 1.0))
        rng = max(0.05, hi - lo)
        ranges_20.append(rng)
        buy_ratio = (cl - lo) / rng
        # Boost slightly if solid bullish/bearish body
        if cl > op:
            buy_ratio = min(1.0, buy_ratio * 1.1)
        elif cl < op:
            buy_ratio = max(0.0, buy_ratio * 0.9)
        buy_vol_sum += v * buy_ratio
        sell_vol_sum += v * (1.0 - buy_ratio)

    total_vol_sum = max(1.0, buy_vol_sum + sell_vol_sum)
    buy_vol_pct = round((buy_vol_sum / total_vol_sum) * 100.0, 1)
    sell_vol_pct = round(100.0 - buy_vol_pct, 1)
    delta_pct = round(buy_vol_pct - sell_vol_pct, 1)

    avg_range_20 = sum(ranges_20) / len(ranges_20)
    avg_range_5 = sum(ranges_20[-5:]) / min(5, len(ranges_20))
    range_expansion_ratio = round(avg_range_5 / max(0.05, avg_range_20), 2)

    if eff_rvol >= 1.50:
        vol_state = "CLIMAX_SURGE"
        vol_label_th = f"🔥 วอลุ่มพุ่งทะลัก ({eff_rvol:.2f}x ของค่าเฉลี่ย)"
    elif eff_rvol >= 1.12:
        vol_state = "HIGH_ACTIVE"
        vol_label_th = f"🟢 วอลุ่มเข้าหนาแน่น ({eff_rvol:.2f}x ของค่าเฉลี่ย)"
    elif eff_rvol >= 0.82:
        vol_state = "NORMAL"
        vol_label_th = f"⚖️ วอลุ่มระดับปกติ ({eff_rvol:.2f}x ของค่าเฉลี่ย)"
    else:
        vol_state = "LOW_SLOWDOWN"
        vol_label_th = f"⏸️ วอลุ่มเบาบาง/ชะลอตัว ({eff_rvol:.2f}x ของค่าเฉลี่ย)"

    regime = trend.get("regime", "RANGING")
    bias = trend.get("bias", "NEUTRAL")
    is_slowdown = (
        vol_state == "LOW_SLOWDOWN"
        or range_expansion_ratio < 0.78
        or regime in ("RANGING", "BULLISH_PULLBACK", "BEARISH_PULLBACK")
    )

    if is_slowdown and regime == "RANGING":
        flow_state = "SLOWDOWN_CONSOLIDATION"
        flow_headline_th = (
            f"⏸️ ตลาดชะลอตัวและแกว่งสะสมพลังในกรอบ เนื่องจาก {vol_label_th} "
            f"และกรอบการแกว่งตัวหดแคบลง ({range_expansion_ratio:.2f}x) รอเลือกทางใกล้ VWAP ({inst_levels.get('vwap', 0):.2f})"
        )
    elif is_slowdown:
        flow_state = "PULLBACK_SLOWDOWN"
        dir_th = "ขาขึ้น" if bias == "BULLISH" else "ขาลง"
        flow_headline_th = (
            f"⏸️ ตลาดชะลอตัวพักฐานในเทรนด์{dir_th} เนื่องจากแรงไล่ราคาพักตัวชั่วคราว ({vol_label_th} | "
            f"แรงซื้อ {buy_vol_pct:.0f}% vs แรงขาย {sell_vol_pct:.0f}%)"
        )
    elif bias == "BULLISH":
        flow_state = "BULLISH_TREND_FLOW"
        flow_headline_th = (
            f"🌊 เกิดกระแสเทรนด์ขาขึ้น (Bullish Flow) เนื่องจากมีแรงซื้อสะสมนำตลาด ({buy_vol_pct:.0f}% vs แรงขาย {sell_vol_pct:.0f}%) "
            f"พร้อม {vol_label_th} ดันราคาไต่เหนือ EMA20/50"
        )
    else:
        flow_state = "BEARISH_TREND_FLOW"
        flow_headline_th = (
            f"🩸 เกิดกระแสเทรนด์ขาลง (Bearish Flow) เนื่องจากมีแรงเทขายกดดันต่อเนื่อง ({sell_vol_pct:.0f}% vs แรงซื้อ {buy_vol_pct:.0f}%) "
            f"พร้อม {vol_label_th} กดราคาต่ำกว่า EMA20/50"
        )

    return {
        "current_volume": curr_vol,
        "avg_volume_20": avg_vol_20,
        "recent_3bar_volume": recent_3_vol,
        "rvol": eff_rvol,
        "rvol_current_bar": rvol_curr,
        "buy_volume_pct": buy_vol_pct,
        "sell_volume_pct": sell_vol_pct,
        "volume_delta_pct": delta_pct,
        "range_expansion_ratio": range_expansion_ratio,
        "volume_state": vol_state,
        "volume_label_th": vol_label_th,
        "flow_state": flow_state,
        "headline_th": flow_headline_th,
        "causes_th": [flow_headline_th],
    }


def build_market_flow_narrative(
    structure: Dict[str, Any],
    mtf: Optional[Dict[str, Any]] = None,
    macro: Optional[Dict[str, Any]] = None,
    smc: Optional[Dict[str, Any]] = None,
    news: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Synthesize a complete causal Market Flow & Volume explanation ('ชะลอตัวเนื่องจาก...' / 'เกิดเทรนด์เนื่องจาก...')
    combining Volume (RVOL & Delta), Session/News, DXY/US10Y Macro, MTF Alignment, and SMC Structure."""
    base_vf = dict(structure.get("volume_flow") or {})
    trend = structure.get("trend") or {}
    inst = structure.get("institutional_levels") or {}
    regime = trend.get("regime", "RANGING")
    bias = trend.get("bias", "NEUTRAL")

    rvol = float(base_vf.get("rvol") or 1.0)
    buy_pct = float(base_vf.get("buy_volume_pct") or 50.0)
    sell_pct = float(base_vf.get("sell_volume_pct") or 50.0)
    rng_ratio = float(base_vf.get("range_expansion_ratio") or 1.0)
    vol_state = str(base_vf.get("volume_state") or "NORMAL")

    session_info = (news or {}).get("session") or {}
    session_name = str(session_info.get("session") or "MARKET")
    upcoming_news = (news or {}).get("upcoming_usd_red_news") or []
    in_blackout = bool((news or {}).get("in_news_blackout"))

    mtf_status = (mtf or {}).get("alignment_status", "")
    dxy_chg = float((macro or {}).get("dxy_change_pct") or 0.0)
    dxy_price = float((macro or {}).get("dxy_price") or 0.0)
    us10y = float((macro or {}).get("us10y_yield") or 0.0)
    smt = str((macro or {}).get("smt_divergence") or "NONE")

    breaks = (smc or {}).get("structure_breaks") or []
    sweeps = (smc or {}).get("liquidity_sweeps") or []
    last_break = breaks[-1] if breaks else None
    last_sweep = sweeps[-1] if sweeps else None

    slowdown_reasons: List[str] = []
    trend_reasons: List[str] = []

    # 1. Check Slowdown / Consolidation causes
    if vol_state == "LOW_SLOWDOWN" or rvol < 0.88:
        slowdown_reasons.append(
            f"ปริมาณการซื้อขาย (Volume) เบาบางลงเหลือเพียง {rvol:.2f}x ของค่าเฉลี่ย 20 แท่ง (แรงซื้อ {buy_pct:.0f}% vs แรงขาย {sell_pct:.0f}%)"
        )
    if rng_ratio < 0.85:
        slowdown_reasons.append(
            f"กรอบการแกว่งตัวของแท่งเทียนหดแคบลง ({rng_ratio:.2f}x ของระยะปกติ) สะท้อนการพักตัวสะสมออเดอร์"
        )
    if "ASIAN" in session_name.upper() or session_info.get("is_rollover_danger"):
        slowdown_reasons.append(
            f"อยู่ในช่วง {session_info.get('label_th', session_name)} ซึ่งสถาบันการเงินฝั่งยุโรปและสหรัฐฯ ยังไม่เปิดเต็มตัว"
        )
    if upcoming_news:
        ev = upcoming_news[0]
        mins = int(ev.get("minutes_away") or 0)
        if 0 < mins <= 180:
            slowdown_reasons.append(
                f"นักลงทุนชะลอการเปิดสถานะก้อนใหญ่เพื่อรอดูตัวเลขข่าวกล่องแดง USD ({ev.get('title')}) ในอีก {mins} นาที"
            )
    if mtf_status == "MIXED_CONFLICT":
        slowdown_reasons.append(
            "ทิศทางไทม์เฟรมใหญ่ (H4/H1) และไทม์เฟรมย่อย (M15/M5) ขัดแย้งกัน ทำให้ราคายังเลือกทางไม่เด็ดขาด"
        )
    if abs(float(inst.get("dist_vwap_usd") or 0.0)) <= float(structure.get("atr14") or 2.0) * 0.45:
        slowdown_reasons.append(
            f"ราคาเคลื่อนไหวเกาะติดค่าเฉลี่ยสถาบัน VWAP ({inst.get('vwap', 0):.2f}) และจุดที่มีวอลุ่มสะสมสูงสุด POC ({inst.get('poc', 0):.2f})"
        )

    # 2. Check Active Trend / Momentum causes
    if bias == "BULLISH":
        if buy_pct >= 53.0 or rvol >= 1.05:
            trend_reasons.append(
                f"มีวอลุ่มแรงซื้อไหลเข้า {buy_pct:.0f}% (ชนะแรงขาย {sell_pct:.0f}%) ด้วยความหนาแน่น Volume {rvol:.2f}x ของค่าเฉลี่ย"
            )
        if last_break and last_break.get("direction") == "BULLISH":
            trend_reasons.append(
                f"แท่งเทียนพุ่งทะลุโครงสร้าง {last_break['type']} (Break of Structure) เหนือระดับ {last_break['level']:.2f}"
            )
        if last_sweep and str(last_sweep.get("implication", "")).startswith("BULLISH"):
            trend_reasons.append(
                f"เกิดการกวาดสภาพคล่องฝั่งขาย (Liquidity Sweep) ที่ {last_sweep['level']:.2f} แล้วมีแรงซื้อดึงกลับทันที"
            )
        if dxy_chg < -0.03:
            trend_reasons.append(
                f"ได้แรงหนุนจากดัชนีดอลลาร์ DXY ที่อ่อนค่าลง ({dxy_chg:+.2f}% ที่ระดับ {dxy_price:.2f}) หนุนราคาทองคำ"
            )
        elif smt == "BULLISH_SMT_DIVERGENCE":
            trend_reasons.append("เกิดสัญญาณ Bullish SMT Divergence ระหว่างทองคำกับดัชนีดอลลาร์ DXY")
        if "STRONG_BULLISH" in mtf_status or "MODERATE_BULLISH" in mtf_status:
            trend_reasons.append("โครงสร้างหลายไทม์เฟรม (M15/H1/H4) เรียงตัวสอดคล้องกันในฝั่งขาขึ้น")
    elif bias == "BEARISH":
        if sell_pct >= 53.0 or rvol >= 1.05:
            trend_reasons.append(
                f"มีวอลุ่มแรงขายกดดัน {sell_pct:.0f}% (มากกว่าแรงซื้อ {buy_pct:.0f}%) ด้วยความหนาแน่น Volume {rvol:.2f}x ของค่าเฉลี่ย"
            )
        if last_break and last_break.get("direction") == "BEARISH":
            trend_reasons.append(
                f"แท่งเทียนทุบหลุดโครงสร้าง {last_break['type']} (Break of Structure) ต่ำกว่าระดับ {last_break['level']:.2f}"
            )
        if last_sweep and str(last_sweep.get("implication", "")).startswith("BEARISH"):
            trend_reasons.append(
                f"เกิดการกวาดสภาพคล่องฝั่งซื้อ (Liquidity Sweep) ที่ {last_sweep['level']:.2f} แล้วถูกแรงขายตบกลับลงมา"
            )
        if dxy_chg > 0.03:
            trend_reasons.append(
                f"ถูกกดดันจากดัชนีดอลลาร์ DXY ที่แข็งค่าขึ้น ({dxy_chg:+.2f}% ที่ระดับ {dxy_price:.2f}) และ US10Y ที่ {us10y:.2f}%"
            )
        elif smt == "BEARISH_SMT_DIVERGENCE":
            trend_reasons.append("เกิดสัญญาณ Bearish SMT Divergence ระหว่างทองคำกับดัชนีดอลลาร์ DXY")
        if "STRONG_BEARISH" in mtf_status or "MODERATE_BEARISH" in mtf_status:
            trend_reasons.append("โครงสร้างหลายไทม์เฟรม (M15/H1/H4) เรียงตัวสอดคล้องกันในฝั่งขาลง")

    if "LONDON" in session_name.upper() or "NEW YORK" in session_name.upper() or "OVERLAP" in session_name.upper():
        trend_reasons.append(f"อยู่ในช่วงตลาด {session_name} ซึ่งมีเม็ดเงินสถาบันหมุนเวียนสูง")

    # Decide primary state: Slowdown vs Active Trend (or Trend with temporary Pullback Slowdown)
    is_primarily_slowdown = (
        regime == "RANGING"
        or vol_state == "LOW_SLOWDOWN"
        or rng_ratio < 0.78
        or in_blackout
    )

    if is_primarily_slowdown:
        primary_causes = slowdown_reasons[:3] if slowdown_reasons else [
            f"ปริมาณการซื้อขายทรงตัว ({rvol:.2f}x) และราคาแกว่งตัวในกรอบสมดุลใกล้ VWAP ({inst.get('vwap', 0):.2f})"
        ]
        if regime in ("BULLISH_PULLBACK", "BEARISH_PULLBACK") or bias in ("BULLISH", "BEARISH"):
            dir_th = "ขาขึ้น" if bias == "BULLISH" else "ขาลง"
            flow_type = "PULLBACK_SLOWDOWN"
            badge_th = f"⏸️ ชะลอตัวพักฐาน (ในโครงสร้าง{dir_th})"
            summary_th = (
                f"ตลาดกำลังชะลอตัวพักฐานในโครงสร้าง{dir_th} เนื่องจาก: "
                + " • ".join(primary_causes)
            )
        else:
            flow_type = "MARKET_SLOWDOWN"
            badge_th = "⏸️ ตลาดชะลอตัว / สะสมพลัง"
            summary_th = "ตลาดมีการชะลอตัวเนื่องจาก: " + " • ".join(primary_causes)
        causes_list = primary_causes
    else:
        dir_th = "ขาขึ้น (Uptrend)" if bias == "BULLISH" else "ขาลง (Downtrend)"
        flow_type = "TREND_EXPANSION"
        badge_th = f"{'🌊 เกิดเทรนด์ขาขึ้น' if bias == 'BULLISH' else '🩸 เกิดเทรนด์ขาลง'} (Vol {rvol:.2f}x)"
        primary_causes = trend_reasons[:3] if trend_reasons else [
            f"เส้นค่าเฉลี่ย EMA20/50/200 เรียงตัวชัดเจน และมีแรง{'ซื้อ' if bias == 'BULLISH' else 'ขาย'}คุมตลาด ({max(buy_pct, sell_pct):.0f}%)"
        ]
        summary_th = f"กำลังเกิดเทรนด์{dir_th}เนื่องจาก: " + " • ".join(primary_causes)
        if slowdown_reasons:
            summary_th += f" (หมายเหตุการชะลอตัวระยะสั้น: {slowdown_reasons[0]})"
        causes_list = primary_causes

    base_vf.update({
        "flow_type": flow_type,
        "badge_th": badge_th,
        "headline_th": summary_th,
        "causes_th": causes_list,
        "slowdown_causes_th": slowdown_reasons,
        "trend_causes_th": trend_reasons,
    })
    return base_vf


def analyze_structure(
    bars: List[Dict[str, Any]],
    timeframe: str = "M5",
    swing_left: int = 3,
    swing_right: int = 3,
) -> Dict[str, Any]:
    """Pure function: analyze full market structure, EMAs, ATR, and S&R zones from OHLCV bars."""
    if not bars:
        raise ValueError("Cannot analyze structure on empty bar list")

    closes = [float(b["c"]) for b in bars]
    e20_list = ema_series(closes, 20)
    e50_list = ema_series(closes, 50)
    e200_list = ema_series(closes, 200)
    atr_list = atr_series(bars, 14)

    current_price = closes[-1]
    atr_val = atr_list[-1] or max(1.0, (float(bars[-1]["h"]) - float(bars[-1]["l"])))
    lookback = min(5, len(bars) - 1)
    e20_prev = e20_list[-1 - lookback] if lookback > 0 else e20_list[-1]
    e50_prev = e50_list[-1 - lookback] if lookback > 0 else e50_list[-1]

    eff_left = min(swing_left, max(1, (len(bars) - 1) // 3))
    eff_right = min(swing_right, max(1, (len(bars) - 1) // 3))
    swings = detect_swings(
        bars,
        left=eff_left,
        right=eff_right,
        eq_tolerance=max(0.25, atr_val * 0.15),
    )
    sr_zones = cluster_sr_zones(swings, current_price, atr_val)
    trend = evaluate_trend_regime(
        current_price=current_price,
        ema20=e20_list[-1],
        ema50=e50_list[-1],
        ema200=e200_list[-1],
        ema20_prev=e20_prev,
        ema50_prev=e50_prev,
        atr_val=atr_val,
        swings=swings,
    )
    candle_behavior = analyze_candlestick_behavior(bars, atr_val)
    rsi_list = rsi_series(closes, 14)
    rsi_div = analyze_rsi_divergence(bars, swings, rsi_list)
    inst_levels = compute_institutional_levels(bars, current_price, atr_val)
    scalping = analyze_scalping_signals(bars, closes, current_price, atr_val, inst_levels)
    volume_flow = analyze_volume_and_flow(bars, trend, atr_val, inst_levels)

    # Build time-aligned EMA series for chart rendering
    ema_chart = {
        "ema20": [
            {"time": int(bars[i]["t"]), "value": e20_list[i]}
            for i in range(len(bars))
            if e20_list[i] is not None
        ],
        "ema50": [
            {"time": int(bars[i]["t"]), "value": e50_list[i]}
            for i in range(len(bars))
            if e50_list[i] is not None
        ],
        "ema200": [
            {"time": int(bars[i]["t"]), "value": e200_list[i]}
            for i in range(len(bars))
            if e200_list[i] is not None
        ],
    }

    nearest_support = next((z for z in sr_zones if z["role"] == "support"), None)
    nearest_resistance = next((z for z in sr_zones if z["role"] == "resistance"), None)

    return {
        "created_by": "xau-structure",
        "timeframe": timeframe,
        "bars_count": len(bars),
        "last_bar_time": int(bars[-1]["t"]),
        "current_price": r2(current_price),
        "atr14": r2(atr_val),
        "trend": trend,
        "candle_behavior": candle_behavior,
        "rsi_divergence": rsi_div,
        "institutional_levels": inst_levels,
        "scalping": scalping,
        "volume_flow": volume_flow,
        "swings": {
            "highs": swings["highs"][-10:],
            "lows": swings["lows"][-10:],
            "all": swings["all"][-16:],
        },
        "sr_zones": sr_zones,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "ema_series": ema_chart,
    }


def analyze_multi_timeframe(
    mtf_bars_by_tf: Dict[str, List[Dict[str, Any]]],
    primary_tf: str = "M5",
    current_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Analyze M5, M15, H1, and H4 simultaneously to extract Multi-Timeframe trend alignment and cross-TF S/R zones."""
    tf_order = ["M5", "M15", "H1", "H4"]
    tf_weights = {"M5": 1, "M15": 2, "H1": 3, "H4": 4}
    primary_tf = primary_tf.upper()

    timeframes_summary: Dict[str, Dict[str, Any]] = {}
    all_htf_zones: List[Dict[str, Any]] = []
    bull_weight = 0
    bear_weight = 0
    total_weight = 0

    for tf in tf_order:
        tf_bars = mtf_bars_by_tf.get(tf) or []
        if not tf_bars:
            continue
        st = analyze_structure(tf_bars, timeframe=tf)
        ref_price = current_price if current_price is not None else st["current_price"]
        bias = st["trend"]["bias"]
        w = tf_weights.get(tf, 1)
        total_weight += w
        if bias == "BULLISH":
            bull_weight += w
        elif bias == "BEARISH":
            bear_weight += w

        cb = st.get("candle_behavior") or {}
        dr = cb.get("dealing_range") or {}

        timeframes_summary[tf] = {
            "timeframe": tf,
            "bars_count": len(tf_bars),
            "bias": bias,
            "regime": st["trend"]["regime"],
            "ema20": st["trend"]["ema20"],
            "ema50": st["trend"]["ema50"],
            "ema200": st["trend"]["ema200"],
            "atr14": st["atr14"],
            "wick_dominance": cb.get("wick_dominance", "BALANCED"),
            "bullish_pressure_pct": cb.get("bullish_pressure_pct", 50.0),
            "dealing_zone": dr.get("zone", "EQUILIBRIUM"),
            "nearest_support": st["nearest_support"],
            "nearest_resistance": st["nearest_resistance"],
        }

        # Collect S/R zones from every timeframe, tagging with timeframe & weight
        for z in st["sr_zones"][:4]:
            mid = float(z["mid"])
            role = "support" if mid <= ref_price else "resistance"
            all_htf_zones.append({
                "timeframe": tf,
                "tf_weight": w,
                "role": role,
                "is_flip": z["is_flip"],
                "low": z["low"],
                "high": z["high"],
                "mid": z["mid"],
                "touches": z["touches"],
                "strength": min(100, int(z["strength"] + (w - 1) * 10)),
                "distance_usd": r2(abs(ref_price - mid)),
            })

    # Detect cross-timeframe S/R confluence (zones from different TFs overlapping or within $2.2)
    for i, z1 in enumerate(all_htf_zones):
        confluent_tfs = {z1["timeframe"]}
        for j, z2 in enumerate(all_htf_zones):
            if i == j or z1["timeframe"] == z2["timeframe"]:
                continue
            overlap = min(z1["high"], z2["high"]) >= max(z1["low"], z2["low"]) - 1.5
            if overlap or abs(z1["mid"] - z2["mid"]) <= 2.2:
                confluent_tfs.add(z2["timeframe"])
        z1["confluent_timeframes"] = sorted(
            list(confluent_tfs), key=lambda x: tf_weights.get(x, 1), reverse=True
        )
        z1["is_mtf_confluence"] = len(confluent_tfs) >= 2
        if z1["is_mtf_confluence"]:
            z1["strength"] = min(100, z1["strength"] + 15)

    # Sort HTF zones: prioritize higher timeframes (H4/H1/M15) and close proximity
    htf_zones = [
        z for z in all_htf_zones if tf_weights.get(z["timeframe"], 1) >= tf_weights.get(primary_tf, 1)
    ]
    htf_zones.sort(key=lambda z: (-int(z["is_mtf_confluence"]), -z["tf_weight"], z["distance_usd"]))

    # Deduplicate nearly identical mid prices
    deduped_zones: List[Dict[str, Any]] = []
    for z in htf_zones:
        if any(abs(z["mid"] - existing["mid"]) < 1.2 and z["role"] == existing["role"] for existing in deduped_zones):
            continue
        deduped_zones.append(z)

    htf_supports = sorted(
        [z for z in deduped_zones if z["role"] == "support"],
        key=lambda z: z["distance_usd"],
    )
    htf_resistances = sorted(
        [z for z in deduped_zones if z["role"] == "resistance"],
        key=lambda z: z["distance_usd"],
    )

    nearest_htf_support = htf_supports[0] if htf_supports else None
    nearest_htf_resistance = htf_resistances[0] if htf_resistances else None

    # Overall MTF Alignment score & verdict
    if bull_weight >= 7:
        overall_bias = "BULLISH"
        alignment_status = "STRONG_BULLISH_ALIGNMENT"
        alignment_score = int(round((bull_weight / max(1, total_weight)) * 100))
        summary_th = "เทรนด์ใหญ่ (H4/H1/M15) สอดคล้องกันเป็นขาขึ้นชัดเจน (เน้นย่อ BUY ตามเทรนด์ใหญ่)"
    elif bear_weight >= 7:
        overall_bias = "BEARISH"
        alignment_status = "STRONG_BEARISH_ALIGNMENT"
        alignment_score = int(round((bear_weight / max(1, total_weight)) * 100))
        summary_th = "เทรนด์ใหญ่ (H4/H1/M15) สอดคล้องกันเป็นขาลงชัดเจน (เน้นเด้ง SELL ตามเทรนด์ใหญ่)"
    elif bull_weight > bear_weight:
        overall_bias = "BULLISH"
        alignment_status = "MODERATE_BULLISH"
        alignment_score = int(round((bull_weight / max(1, total_weight)) * 100))
        summary_th = "ภาพรวมเอนเอียงฝั่งขาขึ้น แต่มีบาง Timeframe พักตัว/ย่อตัว"
    elif bear_weight > bull_weight:
        overall_bias = "BEARISH"
        alignment_status = "MODERATE_BEARISH"
        alignment_score = int(round((bear_weight / max(1, total_weight)) * 100))
        summary_th = "ภาพรวมเอนเอียงฝั่งขาลง แต่มีบาง Timeframe เด้งพักตัว"
    else:
        overall_bias = "NEUTRAL"
        alignment_status = "MIXED_CONFLICT"
        alignment_score = 50
        summary_th = "Timeframe ใหญ่และเล็กขัดแย้งกัน (ตลาดเลือกทาง/ไซด์เวย์ ควรรอเข้าเฉพาะโซน MTF Confluence)"

    return {
        "primary_timeframe": primary_tf,
        "overall_bias": overall_bias,
        "alignment_status": alignment_status,
        "alignment_score": alignment_score,
        "summary_th": summary_th,
        "timeframes": timeframes_summary,
        "htf_sr_zones": deduped_zones[:10],
        "nearest_htf_support": nearest_htf_support,
        "nearest_htf_resistance": nearest_htf_resistance,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Analyze XAUUSD Trend, EMA, and S&R Structure.")
    ap.add_argument("--offline", required=True, help="Path to JSON file containing OHLC bars")
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--json", action="store_true", help="Print JSON output")
    args = ap.parse_args(argv)

    with open(args.offline, encoding="utf-8") as fh:
        doc = json.load(fh)
    bars = doc["bars"] if isinstance(doc, dict) and "bars" in doc else doc
    res = analyze_structure(bars, timeframe=args.timeframe)
    if args.json:
        out = dict(res)
        out.pop("ema_series", None)
        print(json.dumps(out, indent=2))
    else:
        t = res["trend"]
        print(
            f"XAUUSD ({res['timeframe']}) | Price: {res['current_price']:.2f} | ATR(14): {res['atr14']:.2f}"
        )
        print(
            f"Regime: {t['regime']} ({t['bias']}) | EMA20: {t['ema20']} | EMA50: {t['ema50']} | EMA200: {t['ema200']}"
        )
        print(f"Detected {len(res['sr_zones'])} S&R Zones, {len(res['swings']['all'])} Recent Swings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
