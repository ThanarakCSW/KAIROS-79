#!/usr/bin/env python3
"""Smart Money Concepts (SMC) Engine (smc.py).

Deterministic detection for XAU/USD:
  - BOS (Break of Structure) & CHoCH (Change of Character) via Body Close
  - Liquidity Sweep (Wick Break above/below Swing High/Low that closes back inside)
  - Unmitigated Order Blocks (Bullish & Bearish OB) with Confluence Scoring (0-100)
  - Unmitigated Fair Value Gaps (Bullish & Bearish FVG)
  - Equal Highs / Equal Lows (Liquidity Pools & Inducement)
  - Automated Trade Plan Proposal (Entry, SL, TP, Scenario B Sweep Zone, Scenario C Invalidation)
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from engine.structure import analyze_structure, r2


def detect_bos_choch_sweeps(
    bars: List[Dict[str, Any]],
    swings: Dict[str, List[Dict[str, Any]]],
    atr_val: float,
) -> Dict[str, List[Dict[str, Any]]]:
    """Detect BOS, CHoCH (body close breaks) and Liquidity Sweeps (wick breaks)."""
    events: List[Dict[str, Any]] = []
    sweeps: List[Dict[str, Any]] = []

    highs = sorted(swings.get("highs", []), key=lambda x: x["index"])
    lows = sorted(swings.get("lows", []), key=lambda x: x["index"])
    if not highs or not lows:
        return {"structure_breaks": events, "liquidity_sweeps": sweeps}

    struct_dir = 0  # 1 = bullish, -1 = bearish, 0 = initial
    h_ptr = 0
    l_ptr = 0
    active_hi: Optional[Dict[str, Any]] = None
    active_lo: Optional[Dict[str, Any]] = None
    broken_hi_idx = set()
    broken_lo_idx = set()
    swept_hi_idx = set()
    swept_lo_idx = set()

    min_wick = max(0.10, atr_val * 0.05)

    for i, b in enumerate(bars):
        # Activate swings whose fractal right-side confirmation has completed
        while h_ptr < len(highs) and highs[h_ptr]["index"] + 2 < i:
            active_hi = highs[h_ptr]
            h_ptr += 1
        while l_ptr < len(lows) and lows[l_ptr]["index"] + 2 < i:
            active_lo = lows[l_ptr]
            l_ptr += 1

        h, l, c, o = float(b["h"]), float(b["l"]), float(b["c"]), float(b["o"])
        t = int(b["t"])

        if active_hi and active_hi["index"] not in broken_hi_idx:
            lvl = float(active_hi["price"])
            if c > lvl + 0.02:
                kind = "CHoCH" if struct_dir == -1 else "BOS"
                events.append(
                    {
                        "type": kind,
                        "direction": "BULLISH",
                        "level": r2(lvl),
                        "swing_index": active_hi["index"],
                        "swing_t": active_hi["t"],
                        "break_index": i,
                        "break_t": t,
                        "close_price": r2(c),
                        "break_mode": "BODY_CLOSE",
                    }
                )
                broken_hi_idx.add(active_hi["index"])
                struct_dir = 1
            elif h > lvl + min_wick and max(o, c) <= lvl and active_hi["index"] not in swept_hi_idx:
                sweeps.append(
                    {
                        "type": "LIQUIDITY_SWEEP",
                        "side_swept": "BUY_SIDE_HIGH",
                        "implication": "BEARISH_REVERSAL",
                        "level": r2(lvl),
                        "wick_extreme": r2(h),
                        "close_price": r2(c),
                        "swing_t": active_hi["t"],
                        "sweep_index": i,
                        "sweep_t": t,
                    }
                )
                swept_hi_idx.add(active_hi["index"])

        if active_lo and active_lo["index"] not in broken_lo_idx:
            lvl = float(active_lo["price"])
            if c < lvl - 0.02:
                kind = "CHoCH" if struct_dir == 1 else "BOS"
                events.append(
                    {
                        "type": kind,
                        "direction": "BEARISH",
                        "level": r2(lvl),
                        "swing_index": active_lo["index"],
                        "swing_t": active_lo["t"],
                        "break_index": i,
                        "break_t": t,
                        "close_price": r2(c),
                        "break_mode": "BODY_CLOSE",
                    }
                )
                broken_lo_idx.add(active_lo["index"])
                struct_dir = -1
            elif l < lvl - min_wick and min(o, c) >= lvl and active_lo["index"] not in swept_lo_idx:
                sweeps.append(
                    {
                        "type": "LIQUIDITY_SWEEP",
                        "side_swept": "SELL_SIDE_LOW",
                        "implication": "BULLISH_REVERSAL",
                        "level": r2(lvl),
                        "wick_extreme": r2(l),
                        "close_price": r2(c),
                        "swing_t": active_lo["t"],
                        "sweep_index": i,
                        "sweep_t": t,
                    }
                )
                swept_lo_idx.add(active_lo["index"])

    return {
        "structure_breaks": events[-12:],
        "liquidity_sweeps": sweeps[-8:],
    }


def detect_fvgs(bars: List[Dict[str, Any]], atr_val: float) -> List[Dict[str, Any]]:
    """Detect unmitigated Bullish & Bearish Fair Value Gaps (3-candle imbalances)."""
    n = len(bars)
    if n < 4:
        return []

    min_gap = max(0.15, atr_val * 0.12)
    fvgs: List[Dict[str, Any]] = []

    for i in range(2, n):
        b0 = bars[i - 2]
        b1 = bars[i - 1]
        b2 = bars[i]

        h0, l0 = float(b0["h"]), float(b0["l"])
        l2, h2 = float(b2["l"]), float(b2["h"])

        # Bullish FVG: candle i-2 high < candle i low
        if l2 - h0 >= min_gap and float(b1["c"]) > float(b1["o"]):
            gap_low = h0
            gap_high = l2
            mitigated = False
            touched = False
            for j in range(i + 1, n):
                lj = float(bars[j]["l"])
                cj = float(bars[j]["c"])
                if lj <= gap_high:
                    touched = True
                if cj < gap_low:
                    mitigated = True
                    break
            if not mitigated:
                fvgs.append(
                    {
                        "kind": "BULLISH_FVG",
                        "bias": "BULLISH",
                        "index": i - 1,
                        "t": int(b1["t"]),
                        "low": r2(gap_low),
                        "high": r2(gap_high),
                        "mid": r2((gap_low + gap_high) / 2.0),
                        "width_usd": r2(gap_high - gap_low),
                        "tested": touched,
                    }
                )

        # Bearish FVG: candle i-2 low > candle i high
        if l0 - h2 >= min_gap and float(b1["c"]) < float(b1["o"]):
            gap_low = h2
            gap_high = l0
            mitigated = False
            touched = False
            for j in range(i + 1, n):
                hj = float(bars[j]["h"])
                cj = float(bars[j]["c"])
                if hj >= gap_low:
                    touched = True
                if cj > gap_high:
                    mitigated = True
                    break
            if not mitigated:
                fvgs.append(
                    {
                        "kind": "BEARISH_FVG",
                        "bias": "BEARISH",
                        "index": i - 1,
                        "t": int(b1["t"]),
                        "low": r2(gap_low),
                        "high": r2(gap_high),
                        "mid": r2((gap_low + gap_high) / 2.0),
                        "width_usd": r2(gap_high - gap_low),
                        "tested": touched,
                    }
                )

    return fvgs[-10:]


def _zones_overlap(low1: float, high1: float, low2: float, high2: float, pad: float = 0.0) -> bool:
    return max(low1 - pad, low2 - pad) <= min(high1 + pad, high2 + pad)


def detect_order_blocks(
    bars: List[Dict[str, Any]],
    structure_res: Dict[str, Any],
    fvgs: List[Dict[str, Any]],
    breaks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Detect unmitigated Bullish & Bearish Order Blocks and score Confluence (0-100)."""
    n = len(bars)
    if n < 6:
        return []

    atr_val = float(structure_res["atr14"] or 2.0)
    current_price = float(structure_res["current_price"])
    sr_zones = structure_res.get("sr_zones", [])
    trend = structure_res.get("trend", {})
    e20 = float(trend.get("ema20") or current_price)
    e50 = float(trend.get("ema50") or current_price)
    e200 = float(trend.get("ema200") or current_price)

    obs: List[Dict[str, Any]] = []
    min_disp = max(0.6, atr_val * 0.65)
    break_indices = {b["break_index"]: b for b in breaks}

    for i in range(1, n - 2):
        b_base = bars[i]
        o0, h0, l0, c0 = float(b_base["o"]), float(b_base["h"]), float(b_base["l"]), float(b_base["c"])

        # Look at 2-bar forward displacement from candle i
        b_next1 = bars[i + 1]
        b_next2 = bars[i + 2]
        disp_up = max(float(b_next1["c"]), float(b_next2["c"])) - h0
        disp_dn = l0 - min(float(b_next1["c"]), float(b_next2["c"]))

        # Bullish Order Block candidate: bearish/base candle followed by strong upward displacement
        if c0 <= o0 + (atr_val * 0.15) and disp_up >= min_disp:
            ob_low = l0
            ob_high = max(o0, h0) if (h0 - l0) <= atr_val * 1.2 else (l0 + atr_val * 0.85)
            invalidated = False
            retested = False
            already_used = False
            first_touch_idx: Optional[int] = None
            for j in range(i + 3, n):
                if float(bars[j]["l"]) <= ob_high:
                    retested = True
                    if first_touch_idx is None:
                        first_touch_idx = j
                if first_touch_idx is not None and j > first_touch_idx and float(bars[j]["c"]) > ob_high + (atr_val * 0.30):
                    # Price already touched this OB in the past and bounced away -> entry already passed!
                    already_used = True
                if float(bars[j]["c"]) < ob_low - (atr_val * 0.1):
                    invalidated = True
                    break
            if not invalidated and not already_used and current_price >= ob_low:
                obs.append(
                    _score_ob(
                        kind="BULLISH_OB",
                        bias="BULLISH",
                        index=i,
                        t=int(b_base["t"]),
                        ob_low=ob_low,
                        ob_high=ob_high,
                        disp_atr=disp_up / max(0.5, atr_val),
                        retested=retested,
                        current_price=current_price,
                        atr_val=atr_val,
                        fvgs=fvgs,
                        sr_zones=sr_zones,
                        trend_bias=trend.get("bias", "NEUTRAL"),
                        emas=(e20, e50, e200),
                        has_bos=(i + 1 in break_indices or i + 2 in break_indices),
                    )
                )

        # Bearish Order Block candidate: bullish/base candle followed by strong downward displacement
        if c0 >= o0 - (atr_val * 0.15) and disp_dn >= min_disp:
            ob_high = h0
            ob_low = min(o0, l0) if (h0 - l0) <= atr_val * 1.2 else (h0 - atr_val * 0.85)
            invalidated = False
            retested = False
            already_used = False
            first_touch_idx = None
            for j in range(i + 3, n):
                if float(bars[j]["h"]) >= ob_low:
                    retested = True
                    if first_touch_idx is None:
                        first_touch_idx = j
                if first_touch_idx is not None and j > first_touch_idx and float(bars[j]["c"]) < ob_low - (atr_val * 0.30):
                    # Price already touched this OB in the past and dropped away -> entry already passed!
                    already_used = True
                if float(bars[j]["c"]) > ob_high + (atr_val * 0.1):
                    invalidated = True
                    break
            if not invalidated and not already_used and current_price <= ob_high:
                obs.append(
                    _score_ob(
                        kind="BEARISH_OB",
                        bias="BEARISH",
                        index=i,
                        t=int(b_base["t"]),
                        ob_low=ob_low,
                        ob_high=ob_high,
                        disp_atr=disp_dn / max(0.5, atr_val),
                        retested=retested,
                        current_price=current_price,
                        atr_val=atr_val,
                        fvgs=fvgs,
                        sr_zones=sr_zones,
                        trend_bias=trend.get("bias", "NEUTRAL"),
                        emas=(e20, e50, e200),
                        has_bos=(i + 1 in break_indices or i + 2 in break_indices),
                    )
                )

    # Deduplicate overlapping OBs of same bias (keep higher score / newer)
    deduped: List[Dict[str, Any]] = []
    for ob in sorted(obs, key=lambda x: (-x["confluence_score"], -x["index"])):
        if any(
            existing["bias"] == ob["bias"]
            and _zones_overlap(existing["low"], existing["high"], ob["low"], ob["high"], pad=atr_val * 0.2)
            for existing in deduped
        ):
            continue
        deduped.append(ob)

    deduped.sort(key=lambda x: (x["distance_usd"], -x["confluence_score"]))
    return deduped[:8]


def _score_ob(
    kind: str,
    bias: str,
    index: int,
    t: int,
    ob_low: float,
    ob_high: float,
    disp_atr: float,
    retested: bool,
    current_price: float,
    atr_val: float,
    fvgs: List[Dict[str, Any]],
    sr_zones: List[Dict[str, Any]],
    trend_bias: str,
    emas: tuple,
    has_bos: bool,
) -> Dict[str, Any]:
    score = 40
    factors = ["Unmitigated Order Block"]

    if disp_atr >= 1.2:
        score += 15
        factors.append(f"Strong Displacement ({disp_atr:.1f}x ATR)")
    else:
        score += 8

    if has_bos:
        score += 10
        factors.append("Created Structure Break (BOS/CHoCH)")

    has_fvg = any(
        f["bias"] == bias and _zones_overlap(ob_low, ob_high, f["low"], f["high"], pad=atr_val * 0.4)
        for f in fvgs
    )
    if has_fvg:
        score += 15
        factors.append("FVG Imbalance Attached")

    has_sr = any(
        _zones_overlap(ob_low, ob_high, z["low"], z["high"], pad=atr_val * 0.35) for z in sr_zones
    )
    if has_sr:
        score += 15
        factors.append("Overlaps S&R Cluster Zone")

    if trend_bias == bias:
        score += 10
        factors.append(f"Aligned with {bias} Trend")

    if any(ob_low - atr_val * 0.4 <= ema_v <= ob_high + atr_val * 0.4 for ema_v in emas if ema_v):
        score += 5
        factors.append("Dynamic EMA Confluence")

    mid = (ob_low + ob_high) / 2.0
    dist = 0.0 if ob_low <= current_price <= ob_high else min(abs(current_price - ob_low), abs(current_price - ob_high))

    return {
        "kind": kind,
        "bias": bias,
        "index": index,
        "t": t,
        "low": r2(ob_low),
        "high": r2(ob_high),
        "mid": r2(mid),
        "retested": retested,
        "distance_usd": r2(dist),
        "confluence_score": min(100, score),
        "confluence_factors": factors,
    }


def detect_liquidity_pools(
    swings: Dict[str, List[Dict[str, Any]]],
    current_price: float,
    atr_val: float,
) -> List[Dict[str, Any]]:
    """Identify Equal Highs (EQH), Equal Lows (EQL), and major unswept swing targets."""
    pools: List[Dict[str, Any]] = []
    for h in swings.get("highs", [])[-6:]:
        if h["price"] > current_price:
            pools.append(
                {
                    "type": "BUY_SIDE_LIQUIDITY" if h["label"] != "EQH" else "EQUAL_HIGHS_LIQUIDITY",
                    "price": r2(h["price"]),
                    "t": h["t"],
                    "label": h["label"],
                    "distance_usd": r2(h["price"] - current_price),
                }
            )
    for l in swings.get("lows", [])[-6:]:
        if l["price"] < current_price:
            pools.append(
                {
                    "type": "SELL_SIDE_LIQUIDITY" if l["label"] != "EQL" else "EQUAL_LOWS_LIQUIDITY",
                    "price": r2(l["price"]),
                    "t": l["t"],
                    "label": l["label"],
                    "distance_usd": r2(current_price - l["price"]),
                }
            )
    pools.sort(key=lambda x: x["distance_usd"])
    return pools[:8]


def build_trade_setup(
    structure_res: Dict[str, Any],
    obs: List[Dict[str, Any]],
    fvgs: List[Dict[str, Any]],
    breaks: List[Dict[str, Any]],
    sweeps: List[Dict[str, Any]],
    liquidity_pools: List[Dict[str, Any]],
    spread_usd: float = 0.25,
    mtf_res: Optional[Dict[str, Any]] = None,
    macro_res: Optional[Dict[str, Any]] = None,
    trading_style: str = "intraday",
) -> Dict[str, Any]:
    """Build a deterministic Trade Plan (BUY or SELL) for either 'intraday' or 'scalping' mode."""
    current_price = float(structure_res["current_price"])
    atr_val = max(0.8, float(structure_res["atr14"] or 2.0))
    trend = structure_res["trend"]
    sr_zones = list(structure_res.get("sr_zones", []))
    htf_zones = list((mtf_res or {}).get("htf_sr_zones") or [])
    scalp_info = structure_res.get("scalping") or {}
    is_scalp = str(trading_style).lower() == "scalping"

    # Prioritize 100% fresh unmitigated Order Blocks ahead of price (never recommend a zone that already passed)
    max_ob_dist = atr_val * (1.2 if is_scalp else 2.2)
    candidate_obs = sorted(
        obs,
        key=lambda o: (
            0 if not o.get("retested", False) else 1,
            0 if o["distance_usd"] <= max_ob_dist else 1,
            -o["confluence_score"],
            o["distance_usd"],
        ),
    )

    chosen_ob = candidate_obs[0] if candidate_obs else None
    if is_scalp and scalp_info:
        # In Scalping mode, always plan a forward pullback limit entry (ahead of price, never chasing a passed candle)
        bias = scalp_info.get("scalp_bias", "BULLISH")
        side = "BUY" if bias == "BULLISH" else "SELL"
        scalp_ob = next(
            (o for o in candidate_obs if o["bias"] == bias and o["distance_usd"] <= atr_val * 1.5),
            None,
        )
        if scalp_ob:
            zone_low, zone_high = scalp_ob["low"], scalp_ob["high"]
            if side == "BUY":
                # Strictly below current_price so it is always a forward-looking BUY LIMIT in advance
                target_lvl = round((zone_low + zone_high) / 2.0, 2) if current_price <= zone_high else zone_high
                entry = min(round(current_price - max(0.20, atr_val * 0.15), 2), target_lvl)
            else:
                # Strictly above current_price so it is always a forward-looking SELL LIMIT in advance
                target_lvl = round((zone_low + zone_high) / 2.0, 2) if current_price >= zone_low else zone_low
                entry = max(round(current_price + max(0.20, atr_val * 0.15), 2), target_lvl)
        else:
            e9 = float(scalp_info.get("ema9") or current_price)
            if side == "BUY":
                pullback_lvl = min(e9, current_price - max(0.30, atr_val * 0.25))
                entry = round(pullback_lvl, 2)
                zone_low = entry - atr_val * 0.35
                zone_high = entry + atr_val * 0.10
            else:
                pullback_lvl = max(e9, current_price + max(0.30, atr_val * 0.25))
                entry = round(pullback_lvl, 2)
                zone_low = entry - atr_val * 0.10
                zone_high = entry + atr_val * 0.35
        confluence_score = int(scalp_info.get("scalp_score") or 58)
        confluence_factors = list(scalp_info.get("triggers") or [])
    elif chosen_ob:
        bias = chosen_ob["bias"]
        side = "BUY" if bias == "BULLISH" else "SELL"
        zone_low, zone_high = chosen_ob["low"], chosen_ob["high"]
        if side == "BUY":
            # Always give entry strictly in advance below current_price (BUY LIMIT at fresh zone)
            target_lvl = round((zone_low + zone_high) / 2.0, 2) if current_price <= zone_high else zone_high
            entry = min(round(current_price - max(0.25, atr_val * 0.15), 2), target_lvl)
        else:
            # Always give entry strictly in advance above current_price (SELL LIMIT at fresh zone)
            target_lvl = round((zone_low + zone_high) / 2.0, 2) if current_price >= zone_low else zone_low
            entry = max(round(current_price + max(0.25, atr_val * 0.15), 2), target_lvl)
        confluence_score = chosen_ob["confluence_score"]
        confluence_factors = list(chosen_ob["confluence_factors"])
    else:
        # Fallback when no clean OB is nearby: plan a Pending Limit at nearest S&R / FVG ahead of price
        mtf_bias = (mtf_res or {}).get("overall_bias")
        bias = (
            trend["bias"]
            if trend["bias"] in ("BULLISH", "BEARISH")
            else (mtf_bias if mtf_bias in ("BULLISH", "BEARISH") else "BULLISH")
        )
        side = "BUY" if bias == "BULLISH" else "SELL"
        if side == "BUY":
            entry = round(current_price - atr_val * 0.50, 2)
            zone_low = entry - atr_val * 0.40
            zone_high = entry + atr_val * 0.15
        else:
            entry = round(current_price + atr_val * 0.50, 2)
            zone_low = entry - atr_val * 0.15
            zone_high = entry + atr_val * 0.40
        confluence_score = 48
        confluence_factors = [f"{trend['regime']} ({bias})", f"EMA Alignment: {trend['ema_alignment']}"]

    # Determine execution type: MARKET (เข้าทันทีที่ราคาปัจจุบัน) vs LIMIT (ดักเข้าล่วงหน้า)
    # If price is inside the fresh zone or scalping momentum/sweep confirmation is active, enter immediately at market!
    is_in_zone = (zone_low - atr_val * 0.15) <= current_price <= (zone_high + atr_val * 0.15)
    has_recent_sweep = (
        bool(sweeps)
        and sweeps[-1]["implication"].startswith(bias)
        and (len(structure_res.get("swings", {}).get("all", [])) > 0)
    )
    has_scalp_trigger = is_scalp and int(scalp_info.get("scalp_score") or 0) >= 55

    should_market_enter = is_in_zone or has_recent_sweep or has_scalp_trigger
    if should_market_enter:
        execution_type = "MARKET"
        order_type = f"{side} NOW (Market)"
        entry = r2(current_price)
        dist_to_entry = 0.0
        status = "ACTIVE_IN_ZONE"
        confluence_factors.append("สัญญาณยืนยันเข้าทันทีที่ราคาปัจจุบัน (Market Execution)")
    else:
        execution_type = "LIMIT"
        order_type = f"{side} LIMIT"
        dist_to_entry = abs(current_price - entry)
        status = "WAITING_PULLBACK"

    # Bonus confluence if a recent Liquidity Sweep or CHoCH/BOS aligns with the setup
    recent_break = breaks[-1] if breaks else None
    if recent_break and recent_break["direction"] == bias:
        confluence_score = min(100, confluence_score + 10)
        confluence_factors.append(f"Recent {recent_break['type']} ({bias}) at {recent_break['level']:.2f}")

    recent_sweep = sweeps[-1] if sweeps else None
    if recent_sweep and recent_sweep["implication"].startswith(bias):
        confluence_score = min(100, confluence_score + 12)
        confluence_factors.append(f"Liquidity Sweep at {recent_sweep['level']:.2f}")

    # Historical Candlestick Behavior & Dealing Range confluence
    cb = structure_res.get("candle_behavior") or {}
    dr = cb.get("dealing_range") or {}
    if (side == "BUY" and dr.get("zone") == "DISCOUNT") or (side == "SELL" and dr.get("zone") == "PREMIUM"):
        confluence_score = min(100, confluence_score + 6)
        confluence_factors.append(f"Dealing Range {dr.get('zone')} ({dr.get('position_pct', 50):.0f}%)")

    recent_patterns = cb.get("recent_patterns") or []
    matching_patterns = [p for p in recent_patterns[-4:] if p.get("bias") == bias]
    if matching_patterns:
        last_pat = matching_patterns[-1]
        confluence_score = min(100, confluence_score + 8)
        confluence_factors.append(f"Candle Pattern: {last_pat['pattern']} @ {last_pat['price']:.2f}")

    # Institutional Levels: PDH/PDL, Asian Session Range, Session VWAP, Volume Profile POC
    inst = structure_res.get("institutional_levels") or {}
    if inst:
        poc = inst.get("poc")
        vwap = inst.get("vwap")
        if poc and (zone_low - atr_val * 0.6 <= poc <= zone_high + atr_val * 0.6):
            confluence_score = min(100, confluence_score + 8)
            confluence_factors.append(f"Volume Profile POC Confluence ({poc:.2f})")
        elif vwap and (zone_low - atr_val * 0.6 <= vwap <= zone_high + atr_val * 0.6):
            confluence_score = min(100, confluence_score + 6)
            confluence_factors.append(f"Session VWAP Confluence ({vwap:.2f})")

        if side == "BUY" and (inst.get("pdl_swept") or inst.get("asian_status") == "ASIAN_LOW_SWEPT_BULLISH"):
            confluence_score = min(100, confluence_score + 10)
            confluence_factors.append("Asian Low / PDL Liquidity Sweep Reversal")
        elif side == "SELL" and (inst.get("pdh_swept") or inst.get("asian_status") == "ASIAN_HIGH_SWEPT_BEARISH"):
            confluence_score = min(100, confluence_score + 10)
            confluence_factors.append("Asian High / PDH Liquidity Sweep Reversal")

    # RSI(14) & Momentum Divergence Confluence
    rsi_div = structure_res.get("rsi_divergence") or {}
    if rsi_div.get("divergence_bias") == bias:
        confluence_score = min(100, confluence_score + 9)
        confluence_factors.append(f"RSI Momentum: {rsi_div.get('divergence')}")

    # Macro Correlation (DXY & US10Y & SMT Divergence)
    if macro_res:
        if macro_res.get("smt_divergence", "NONE") != "NONE" and macro_res.get("macro_gold_bias") == bias:
            confluence_score = min(100, confluence_score + 10)
            confluence_factors.append(f"Macro {macro_res['smt_divergence']} vs DXY")
        elif macro_res.get("macro_gold_bias") == bias:
            confluence_score = min(100, confluence_score + 5)
            confluence_factors.append(f"DXY Macro Tailwind ({macro_res.get('dxy_change_pct', 0):+.2f}%)")

    # Multi-Timeframe (M15/H1/H4) Trend & S/R Confluence
    if mtf_res:
        if mtf_res.get("overall_bias") == bias:
            confluence_score = min(100, confluence_score + 10)
            confluence_factors.append(f"MTF Aligned ({mtf_res.get('alignment_status')})")
        expected_role = "support" if side == "BUY" else "resistance"
        overlapping_htf = [
            hz
            for hz in htf_zones
            if hz["role"] == expected_role
            and _zones_overlap(zone_low, zone_high, hz["low"], hz["high"], pad=atr_val * 0.6)
        ]
        if overlapping_htf:
            best_hz = overlapping_htf[0]
            confluence_score = min(100, confluence_score + 12)
            tfs_str = "+".join(best_hz.get("confluent_timeframes") or [best_hz["timeframe"]])
            confluence_factors.append(f"HTF {tfs_str} {expected_role.upper()} Zone ({best_hz['mid']:.2f})")

    # Historical Multi-Touch Support / Resistance Confluence
    hist_sr = list(structure_res.get("historical_sr") or [])
    expected_eff_role = "support" if side == "BUY" else "resistance"
    near_hist = [
        hz
        for hz in hist_sr
        if hz.get("effective_role") == expected_eff_role
        and abs(entry - float(hz["mid"])) <= atr_val * 0.75
    ]
    if near_hist:
        best_hist = near_hist[0]
        confluence_score = min(100, confluence_score + 8)
        confluence_factors.append(f"แนวรับต้านเดิมย้อนหลัง ({best_hist['touches_count']}x @ {best_hist['mid']:.2f})")

    # Stop Loss placement: beyond distal edge of zone + ATR buffer + Broker Spread
    sl_buffer = max(atr_val * (0.32 if is_scalp else 0.45), spread_usd * 2.0)
    min_sl_atr = 0.72 if is_scalp else 0.90
    min_target_rr = 1.25 if is_scalp else 1.55
    default_target_rr = 1.45 if is_scalp else 2.0
    combined_sr = sr_zones + htf_zones + hist_sr
    if side == "BUY":
        raw_stop = min(zone_low - sl_buffer, entry - max(atr_val * min_sl_atr, spread_usd * 3.0))
        stop = r2(raw_stop)
        risk = max(0.4, entry - stop)
        tp1 = r2(entry + risk * (0.85 if is_scalp else 1.0))

        up_targets = [
            p["price"]
            for p in liquidity_pools
            if p["price"] >= entry + risk * min_target_rr
        ] + [
            z["mid"]
            for z in combined_sr
            if z["role"] == "resistance" and z["mid"] >= entry + risk * min_target_rr
        ]
        if inst and inst.get("pdh") and inst["pdh"] >= entry + risk * min_target_rr:
            up_targets.append(float(inst["pdh"]))
        if is_scalp and inst and inst.get("vwap") and float(inst["vwap"]) >= entry + risk * min_target_rr:
            up_targets.append(float(inst["vwap"]))
        target = r2(min(up_targets)) if up_targets else r2(entry + risk * default_target_rr)
        if is_scalp and (target - entry) > risk * 2.1:
            target = r2(entry + risk * 1.55)

        deeper_zones = [
            o for o in obs if o["bias"] == "BULLISH" and o["high"] < zone_low
        ] + [
            z for z in combined_sr if z["role"] == "support" and z["mid"] < stop
        ]
        sweep_zone_price = r2(deeper_zones[0]["mid"]) if deeper_zones else r2(stop - atr_val * 0.7)
        invalidation_price = r2(stop - atr_val * 0.45)
    else:
        # For SELL, Stop Loss is hit by Ask (High + Spread), so include spread_usd explicitly
        raw_stop = max(zone_high + sl_buffer + spread_usd, entry + max(atr_val * min_sl_atr, spread_usd * 3.0))
        stop = r2(raw_stop)
        risk = max(0.4, stop - entry)
        tp1 = r2(entry - risk * (0.85 if is_scalp else 1.0))

        dn_targets = [
            p["price"]
            for p in liquidity_pools
            if p["price"] <= entry - risk * min_target_rr
        ] + [
            z["mid"]
            for z in combined_sr
            if z["role"] == "support" and z["mid"] <= entry - risk * min_target_rr
        ]
        if inst and inst.get("pdl") and inst["pdl"] <= entry - risk * min_target_rr:
            dn_targets.append(float(inst["pdl"]))
        if is_scalp and inst and inst.get("vwap") and float(inst["vwap"]) <= entry - risk * min_target_rr:
            dn_targets.append(float(inst["vwap"]))
        target = r2(max(dn_targets)) if dn_targets else r2(entry - risk * default_target_rr)
        if is_scalp and (entry - target) > risk * 2.1:
            target = r2(entry - risk * 1.55)

        higher_zones = [
            o for o in obs if o["bias"] == "BEARISH" and o["low"] > zone_high
        ] + [
            z for z in combined_sr if z["role"] == "resistance" and z["mid"] > stop
        ]
        sweep_zone_price = r2(higher_zones[0]["mid"]) if higher_zones else r2(stop + atr_val * 0.7)
        invalidation_price = r2(stop + atr_val * 0.45)

    reward = abs(target - entry)
    rr = round(reward / max(0.01, risk), 2)
    dist_to_entry = abs(current_price - entry)
    is_advance_entry = True if execution_type == "MARKET" else ((entry <= current_price) if side == "BUY" else (entry >= current_price))

    return {
        "symbol": "XAUUSD",
        "trading_style": "scalping" if is_scalp else "intraday",
        "style_label_th": "⚡ โหมดเทรดสั้น (Gold Scalping)" if is_scalp else "🎯 โหมดเทรดรอบปกติ (Intraday SMC)",
        "side": side,
        "execution_type": execution_type,
        "order_type": order_type,
        "is_advance_entry": is_advance_entry,
        "distance_to_entry_usd": r2(dist_to_entry),
        "current_price": r2(current_price),
        "bias": bias,
        "status": status,
        "entry": r2(entry),
        "stop": r2(stop),
        "tp1": r2(tp1),
        "target": r2(target),
        "risk_usd": r2(risk),
        "reward_usd": r2(reward),
        "reward_risk": rr,
        "sl_atr_ratio": round(risk / max(0.01, atr_val), 2),
        "spread_included_usd": r2(spread_usd),
        "zone_low": r2(zone_low),
        "zone_high": r2(zone_high),
        "confluence_score": confluence_score,
        "confluence_factors": confluence_factors,
        "scenario_b_sweep_reentry": sweep_zone_price,
        "scenario_c_invalidation": invalidation_price,
    }


def analyze_smc(
    bars: List[Dict[str, Any]],
    timeframe: str = "M5",
    spread_usd: float = 0.25,
    structure_res: Optional[Dict[str, Any]] = None,
    mtf_res: Optional[Dict[str, Any]] = None,
    macro_res: Optional[Dict[str, Any]] = None,
    trading_style: str = "intraday",
) -> Dict[str, Any]:
    """Pure function: run full Structure + SMC + MTF + Macro analysis and produce a Trade Setup."""
    st = structure_res or analyze_structure(bars, timeframe=timeframe)
    atr_val = float(st["atr14"] or 2.0)
    current_price = float(st["current_price"])

    bos_sweeps = detect_bos_choch_sweeps(bars, st["swings"], atr_val)
    fvgs = detect_fvgs(bars, atr_val)
    obs = detect_order_blocks(bars, st, fvgs, bos_sweeps["structure_breaks"])
    pools = detect_liquidity_pools(st["swings"], current_price, atr_val)
    setup = build_trade_setup(
        structure_res=st,
        obs=obs,
        fvgs=fvgs,
        breaks=bos_sweeps["structure_breaks"],
        sweeps=bos_sweeps["liquidity_sweeps"],
        liquidity_pools=pools,
        spread_usd=spread_usd,
        mtf_res=mtf_res,
        macro_res=macro_res,
        trading_style=trading_style,
    )

    return {
        "created_by": "xau-smc",
        "timeframe": timeframe,
        "current_price": st["current_price"],
        "atr14": st["atr14"],
        "structure_breaks": bos_sweeps["structure_breaks"],
        "liquidity_sweeps": bos_sweeps["liquidity_sweeps"],
        "order_blocks": obs,
        "fvgs": fvgs,
        "liquidity_pools": pools,
        "trade_setup": setup,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Analyze XAUUSD Smart Money Concepts (SMC).")
    ap.add_argument("--offline", required=True, help="Path to JSON file containing OHLC bars")
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--spread", type=float, default=0.25, help="Broker spread in USD (default 0.25)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    with open(args.offline, encoding="utf-8") as fh:
        doc = json.load(fh)
    bars = doc["bars"] if isinstance(doc, dict) and "bars" in doc else doc
    res = analyze_smc(bars, timeframe=args.timeframe, spread_usd=args.spread)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        s = res["trade_setup"]
        print(
            f"SMC Setup [{s['status']}]: {s['side']} XAUUSD @ {s['entry']:.2f} | SL {s['stop']:.2f} | TP {s['target']:.2f} (R:R {s['reward_risk']})"
        )
        print(
            f"Confluence Score: {s['confluence_score']}/100 | Factors: {', '.join(s['confluence_factors'])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
