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


def _collect_live_mtf_sr_levels(
    bars: Optional[List[Dict[str, Any]]],
    current_price: float,
    structure_res: Dict[str, Any],
    mtf_res: Optional[Dict[str, Any]] = None,
    max_dist_usd: float = 45.0,
) -> Dict[str, Any]:
    """Collect live Multi-Timeframe Support & Resistance levels around current_price
    from HTF MTF zones (H4/H1/M15/M5), resampled live M5 & M15 swing pivots,
    structure SR zones, and institutional levels (VWAP, POC, PDH/PDL, Asian Hi/Lo)."""
    supports: List[Dict[str, Any]] = []
    resistances: List[Dict[str, Any]] = []

    def _add_lvl(mid: float, low: float, high: float, tf_lbl: str, weight: int = 2, role_hint: str = "") -> None:
        if mid <= 0.0 or abs(mid - current_price) > max_dist_usd:
            return
        item = {
            "mid": round(mid, 2),
            "low": round(min(low, mid), 2),
            "high": round(max(high, mid), 2),
            "timeframe": tf_lbl,
            "weight": weight,
            "distance_usd": round(abs(current_price - mid), 2),
        }
        if mid < current_price - 0.05:
            supports.append(item)
        elif mid > current_price + 0.05:
            resistances.append(item)

    # 1. HTF S/R Zones from MTF analyzer (filter out stale/distant fixture prices)
    for z in (mtf_res or {}).get("htf_sr_zones") or []:
        mid = float(z.get("mid") or 0.0)
        lo = float(z.get("low") or mid)
        hi = float(z.get("high") or mid)
        tf_lbl = str(z.get("timeframe") or "HTF")
        w = int(z.get("tf_weight") or 3)
        _add_lvl(mid, lo, hi, tf_lbl, weight=w)

    # 2. Live Resampled M5 & M15 Swing Highs / Lows directly from live bars
    if bars and len(bars) >= 30:
        for bucket_n, tf_lbl, w in ((5, "M5", 2), (15, "M15", 3)):
            agg: List[Dict[str, float]] = []
            for i in range(0, len(bars), bucket_n):
                chunk = bars[i : i + bucket_n]
                if len(chunk) < max(2, bucket_n // 2):
                    continue
                agg.append(
                    {
                        "h": max(float(b["h"]) for b in chunk),
                        "l": min(float(b["l"]) for b in chunk),
                    }
                )
            for j in range(2, len(agg) - 2):
                hh = agg[j]["h"]
                ll = agg[j]["l"]
                if hh >= max(agg[j - 2]["h"], agg[j - 1]["h"], agg[j + 1]["h"], agg[j + 2]["h"]):
                    _add_lvl(hh, hh - 0.35, hh + 0.35, tf_lbl, weight=w)
                if ll <= min(agg[j - 2]["l"], agg[j - 1]["l"], agg[j + 1]["l"], agg[j + 2]["l"]):
                    _add_lvl(ll, ll - 0.35, ll + 0.35, tf_lbl, weight=w)

    # 3. Structure SR zones & Institutional levels (VWAP, POC, PDH, PDL, Asian High/Low)
    for z in structure_res.get("sr_zones") or []:
        mid = float(z.get("mid") or 0.0)
        _add_lvl(mid, float(z.get("low") or mid), float(z.get("high") or mid), "SR", weight=2)

    inst = structure_res.get("institutional_levels") or {}
    for k, lbl, w in (
        ("pdh", "PDH", 4),
        ("pdl", "PDL", 4),
        ("vwap", "VWAP", 3),
        ("poc", "POC", 3),
        ("asian_high", "ASIA-H", 4),
        ("asian_low", "ASIA-L", 4),
    ):
        val = inst.get(k)
        if val is not None and float(val) > 0.0:
            v = float(val)
            _add_lvl(v, v - 0.25, v + 0.25, lbl, weight=w)

    # Sort closest first, breaking ties by higher timeframe weight
    supports.sort(key=lambda x: (x["distance_usd"], -x["weight"]))
    resistances.sort(key=lambda x: (x["distance_usd"], -x["weight"]))

    # Deduplicate levels within $0.45 of each other (keeping higher weight)
    def _dedup(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for it in items:
            if not any(abs(it["mid"] - o["mid"]) < 0.45 for o in out):
                out.append(it)
            if len(out) >= 6:
                break
        return out

    sup_clean = _dedup(supports)
    res_clean = _dedup(resistances)
    return {
        "supports": sup_clean,
        "resistances": res_clean,
        "nearest_support": sup_clean[0] if sup_clean else None,
        "nearest_resistance": res_clean[0] if res_clean else None,
    }


def _compute_volume_tpsl_multipliers(
    vol_flow: Dict[str, Any],
    side: str,
) -> Dict[str, Any]:
    """Compute bounded TP & SL scaling multipliers from live Market Volume (RVOL & Volume Delta).
    - Low volume (RVOL < 0.80): tighten TP (0.85x) & SL (0.92x) for quiet sessions
    - Normal volume (0.80 - 1.30): baseline 1.00x
    - High volume (1.30 - 1.80): expand SL (1.15x) against wicks; expand TP (1.25x if Delta aligns, else 1.12x)
    - Surge volume (> 1.80): cap SL expansion at 1.25x, expand TP (1.35x if aligned, else 1.15x)
    """
    rvol = float(vol_flow.get("rvol") or vol_flow.get("rvol_current_bar") or 1.0)
    delta_pct = float(vol_flow.get("volume_delta_pct") or 0.0)
    delta_aligned = (side == "BUY" and delta_pct >= 6.0) or (side == "SELL" and delta_pct <= -6.0)

    if rvol < 0.80:
        tp_mult = 0.85
        sl_mult = 0.92
        regime = "LOW_VOLUME"
        note_th = f"📊 Volume เบาบาง (RVOL {rvol:.2f}x): กระชับ TP 0.85x / SL 0.92x เพื่อเก็บกำไรได้จริงในตลาดเงียบ"
    elif rvol <= 1.30:
        tp_mult = 1.00
        sl_mult = 1.00
        regime = "NORMAL_VOLUME"
        note_th = f"📊 Volume ปกติ (RVOL {rvol:.2f}x): ใช้ระยะ TP/SL มาตรฐาน 1.00x"
    elif rvol <= 1.80:
        sl_mult = 1.15
        tp_mult = 1.25 if delta_aligned else 1.12
        regime = "HIGH_VOLUME"
        note_th = (
            f"📊 Volume สูงมีแรงส่ง (RVOL {rvol:.2f}x, Delta {delta_pct:+.0f}%): "
            f"ขยาย TP {tp_mult:.2f}x / SL {sl_mult:.2f}x กันไส้สะบัด"
        )
    else:
        sl_mult = 1.25
        tp_mult = 1.35 if delta_aligned else 1.15
        regime = "SURGE_VOLUME"
        note_th = (
            f"🔥 Volume พุ่งแรง (RVOL {rvol:.2f}x, Delta {delta_pct:+.0f}%): "
            f"ขยาย TP {tp_mult:.2f}x / SL {sl_mult:.2f}x (คุมเพดาน SL ปลอดภัย)"
        )

    return {
        "rvol": round(rvol, 2),
        "volume_delta_pct": round(delta_pct, 1),
        "delta_aligned": delta_aligned,
        "tp_mult": tp_mult,
        "sl_mult": sl_mult,
        "regime": regime,
        "note_th": note_th,
    }


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
    symbol: str = "XAUUSD",
    bars: Optional[List[Dict[str, Any]]] = None,
    adaptive_tuning: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a deterministic Trade Plan (BUY or SELL) for either 'intraday' or 'scalping' mode."""
    current_price = float(structure_res["current_price"])
    atr_val = max(0.8, float(structure_res["atr14"] or 2.0))
    adapt = adaptive_tuning or {}
    adapt_lot_scale = float(adapt.get("lot_scale") or 1.0)
    adapt_conf_boost = int(adapt.get("min_confluence_boost") or 0)
    adapt_sl_mult = float(adapt.get("sl_buffer_mult") or 1.0)
    adapt_tp_mult = float(adapt.get("tp_mult") or 1.0)
    adapt_req_candle = bool(adapt.get("require_candle_confirm", False))

    is_rapid = str(trading_style).lower() in ("rapid_fire", "rapid_fire_test")
    if is_rapid:
        is_btc_sym = "BTC" in str(symbol).upper()
        tr_dir = str(structure_res.get("trend", {}).get("direction") or "BULLISH").upper()
        tr_bias = str(structure_res.get("trend", {}).get("bias") or "BULLISH").upper()
        scalp_info = structure_res.get("scalping") or {}
        scalp_bias = scalp_info.get("scalp_bias") or tr_bias
        mtf_bias = str((mtf_res or {}).get("overall_bias") or tr_bias).upper()

        # 1. Multi-Timeframe Support & Resistance Analysis (M5 / M15 / H1 / H4 / Institutional)
        mtf_sr = _collect_live_mtf_sr_levels(
            bars=bars,
            current_price=current_price,
            structure_res=structure_res,
            mtf_res=mtf_res,
            max_dist_usd=500.0 if is_btc_sym else 45.0,
        )
        near_sup = mtf_sr.get("nearest_support")
        near_res = mtf_sr.get("nearest_resistance")

        # 2. Candlestick Behavior, Wick Rejection & Multi-Bar Reversal Pattern Analysis
        candle_beh = structure_res.get("candle_behavior") or {}
        bull_press = float(candle_beh.get("bullish_pressure_pct") or 50.0)
        bear_press = float(candle_beh.get("bearish_pressure_pct") or 50.0)
        wick_dom = str(candle_beh.get("wick_dominance") or "BALANCED").upper()
        dealing_zone = str((candle_beh.get("dealing_range") or {}).get("zone") or "EQUILIBRIUM").upper()
        cb_live = candle_beh.get("live_signal") or {}
        rsi_div_rf = structure_res.get("rsi_divergence") or {}
        rsi_div_bias = str(rsi_div_rf.get("divergence_bias") or "NEUTRAL").upper()
        recent_candles = candle_beh.get("recent_patterns") or []
        bars_len = int(structure_res.get("bars_count") or (len(bars) if bars else 600))
        fresh_candles = [
            cp for cp in recent_candles
            if (bars_len - 1 - int(cp.get("index") or 0)) <= 4
        ]
        latest_candle_pat = fresh_candles[-1] if fresh_candles else None

        live_candle_bias = "NEUTRAL"
        live_candle_note = ""
        live_candle_cat = "NONE"
        live_candle_str = 0
        live_candle_pat_code = ""
        live_rej_price: Optional[float] = None
        # Only trust validated live_signal (never trade an invalidated/broken historical pattern!)
        if cb_live.get("has_signal"):
            live_candle_bias = str(cb_live.get("signal_bias") or "NEUTRAL").upper()
            live_candle_note = str(cb_live.get("label_th") or cb_live.get("pattern") or "")
            live_candle_cat = str(cb_live.get("category") or "CONTINUATION").upper()
            live_candle_str = int(cb_live.get("strength") or 72)
            live_candle_pat_code = str(cb_live.get("pattern") or "")
            live_rej_price = cb_live.get("rejection_price")

        # 3. Chart Pattern Analysis (Flags, Double Top/Bottom, H&S, Triangles)
        active_pat = structure_res.get("active_chart_pattern")
        if not active_pat:
            rec_pats = structure_res.get("recent_chart_patterns") or []
            valid_pats = [
                p for p in rec_pats
                if int(p.get("bars_ago") or 999) <= 25
                and str(p.get("status") or "").lower() in ("forming", "breakout", "confirmed")
            ]
            active_pat = valid_pats[0] if valid_pats else None
        pat_bias = str((active_pat or {}).get("bias") or "NEUTRAL").upper()
        pat_name_th = str((active_pat or {}).get("name_th") or (active_pat or {}).get("name") or "")
        pat_score = int((active_pat or {}).get("confluence_score") or 0)
        pat_target = float((active_pat or {}).get("target") or 0.0)
        pat_base = float((active_pat or {}).get("base_level") or 0.0)

        # Direction Selection: Balance Trend + Candlestick Reversal/Wick Rejection + MTF S/R + Pattern
        bull_votes = 0.0
        bear_votes = 0.0
        if "BULL" in scalp_bias:
            bull_votes += 2.0
        elif "BEAR" in scalp_bias:
            bear_votes += 2.0
        if "BULL" in mtf_bias:
            bull_votes += 1.5
        elif "BEAR" in mtf_bias:
            bear_votes += 1.5

        # Evaluate Candlestick Reversal / Wick Rejection Override Power
        is_candle_reversal_entry = False
        rev_ctx_hits = 0
        if live_candle_cat in ("REVERSAL", "WICK_REJECTION") and live_candle_bias in ("BULLISH", "BEARISH") and live_candle_str >= 74:
            if live_candle_bias == "BULLISH":
                if near_sup and near_sup["distance_usd"] <= (25.0 if is_btc_sym else 2.80):
                    rev_ctx_hits += 1
                if dealing_zone == "DISCOUNT":
                    rev_ctx_hits += 1
                if wick_dom == "BUYER_ABSORPTION":
                    rev_ctx_hits += 1
                if rsi_div_bias == "BULLISH":
                    rev_ctx_hits += 1
                if live_candle_str >= 83:
                    rev_ctx_hits += 1
                if rev_ctx_hits >= 1 or live_candle_str >= 84:
                    bull_votes += 4.5 if rev_ctx_hits >= 2 else 3.85
                    is_candle_reversal_entry = True
                else:
                    bull_votes += 2.0
            else:
                if near_res and near_res["distance_usd"] <= (25.0 if is_btc_sym else 2.80):
                    rev_ctx_hits += 1
                if dealing_zone == "PREMIUM":
                    rev_ctx_hits += 1
                if wick_dom == "SELLER_REJECTION":
                    rev_ctx_hits += 1
                if rsi_div_bias == "BEARISH":
                    rev_ctx_hits += 1
                if live_candle_str >= 83:
                    rev_ctx_hits += 1
                if rev_ctx_hits >= 1 or live_candle_str >= 84:
                    bear_votes += 4.5 if rev_ctx_hits >= 2 else 3.85
                    is_candle_reversal_entry = True
                else:
                    bear_votes += 2.0
        elif live_candle_bias == "BULLISH":
            bull_votes += 2.0
        elif live_candle_bias == "BEARISH":
            bear_votes += 2.0

        if pat_bias == "BULLISH" and pat_score >= 65:
            bull_votes += 1.35
        elif pat_bias == "BEARISH" and pat_score >= 65:
            bear_votes += 1.35
        # MTF S/R bounce bias when touching a key MTF zone
        if near_sup and near_sup["distance_usd"] <= (22.0 if is_btc_sym else 1.80):
            bull_votes += 1.25
        if near_res and near_res["distance_usd"] <= (22.0 if is_btc_sym else 1.80):
            bear_votes += 1.25

        side = "BUY" if bull_votes >= bear_votes else "SELL"
        want_bias_pre = "BULLISH" if side == "BUY" else "BEARISH"
        is_candle_reversal_entry = bool(is_candle_reversal_entry and live_candle_bias == want_bias_pre)
        is_counter_trend_rev = bool(
            is_candle_reversal_entry
            and mtf_bias in ("BULLISH", "BEARISH")
            and mtf_bias != want_bias_pre
        )
        entry_style = "CANDLE_REVERSAL" if is_candle_reversal_entry else "TREND_CONTINUATION"
        entry = current_price

        # Market Regime Detection for Rapid Fire: Sideway / Consolidation vs Trending
        trend_info = structure_res.get("trend") or {}
        tr_regime = str(trend_info.get("regime") or "").upper()
        ema_align = str(trend_info.get("ema_alignment") or "").upper()
        vol_flow = structure_res.get("volume_flow") or {}
        rng_ratio = float(vol_flow.get("range_expansion_ratio") or 1.0)
        flow_state = str(vol_flow.get("flow_state") or "").upper()

        is_sideway = bool(
            "RANGING" in tr_regime
            or "SIDEWAY" in tr_regime
            or tr_bias in ("NEUTRAL", "CHOP", "SIDEWAY")
            or ema_align == "MIXED"
            or "SLOWDOWN" in flow_state
            or "CONSOLIDATION" in flow_state
            or (vol_flow.get("range_expansion_ratio") is not None and rng_ratio < 0.78)
        )

        # Base Dynamic Calibration + Adaptive Multipliers
        if is_candle_reversal_entry and is_counter_trend_rev:
            min_target_gold = max(1.40, spread_usd + 0.85)
            target_dist = max(25.0, min(55.0, round(0.85 * atr_val, 2))) if is_btc_sym else max(min_target_gold, min(3.40, round(1.15 * atr_val, 2)))
            sl_dist = max(35.0, min(70.0, round(1.05 * atr_val, 2))) if is_btc_sym else max(2.10, min(3.60, round(1.40 * atr_val, 2)))
            regime_label = f"🕯️ สวนเทรนด์กลับตัวด้วยแท่งเทียน ({live_candle_note})"
            regime_tag = "REVERSAL"
        elif is_sideway:
            min_target_gold = max(1.20, spread_usd + 0.65)
            target_dist = max(20.0, min(45.0, round(0.55 * atr_val, 2))) if is_btc_sym else max(min_target_gold, min(2.60, round(0.85 * atr_val, 2)))
            sl_dist = max(35.0, min(65.0, round(0.85 * atr_val, 2))) if is_btc_sym else max(2.00, min(3.40, round(1.35 * atr_val, 2)))
            regime_label = "🦀 ตลาดพักตัว/ไซด์เวย์ (สไนเปอร์อิงแนวรับ-ต้าน MTF + แท่งเทียน)"
            regime_tag = "SIDEWAY"
        else:
            min_target_gold = max(1.80, spread_usd + 1.20)
            target_dist = max(35.0, 1.4 * atr_val) if is_btc_sym else max(min_target_gold, min(5.00, round(1.60 * atr_val, 2)))
            sl_dist = max(70.0, 2.0 * atr_val) if is_btc_sym else max(2.50, min(4.80, round(1.90 * atr_val, 2)))
            regime_label = (
                f"🕯️ กลับตัว/ยืนยันแท่งเทียนตามโครงสร้าง ({live_candle_note})"
                if is_candle_reversal_entry
                else "🚀 ตลาดมีเทรนด์ชัดเจน (รันตาม MTF S/R + Pattern + แท่งเทียน)"
            )
            regime_tag = "TRENDING"

        # Dynamic Volume-Scaled TP/SL Multipliers (RVOL + Volume Delta)
        vol_scale = _compute_volume_tpsl_multipliers(vol_flow, side)
        min_safe_sl = 35.0 if is_btc_sym else max(1.80, spread_usd + 1.50)
        max_safe_sl = 120.0 if is_btc_sym else 5.00  # Hard Cap to protect micro accounts during surges

        sl_dist = round(
            max(min_safe_sl, min(max_safe_sl, sl_dist * adapt_sl_mult * vol_scale["sl_mult"])),
            2,
        )
        target_dist = round(
            max(min_target_gold if not is_btc_sym else 20.0, target_dist * adapt_tp_mult * vol_scale["tp_mult"]),
            2,
        )

        # 4. Smart MTF S/R + Rejection Wick + Chart Pattern TP / SL Placement
        # Only hard-block when slamming straight into a MAJOR HTF wall (H1/H4/PDH/PDL/Asian High-Low, weight >= 4)
        # within a tight $0.80 clearance — minor M5/local SR lines still cap TP/anchor SL without freezing order flow!
        min_wall_clearance = 12.0 if is_btc_sym else max(0.80, spread_usd + 0.25)
        mtf_wall_blocked = False
        sr_notes: List[str] = [vol_scale["note_th"]]

        if side == "BUY":
            stop = r2(entry - sl_dist)
            target = r2(entry + target_dist)
            # Anchor SL behind fresh Rejection Wick tip if reversal candle triggered
            if is_candle_reversal_entry and live_rej_price is not None and float(live_rej_price) < entry:
                wick_sl = r2(float(live_rej_price) - (8.0 if is_btc_sym else 0.35))
                if (entry - wick_sl) >= min_safe_sl and (entry - wick_sl) <= max_safe_sl:
                    stop = wick_sl
                    sr_notes.append(f"🕯️ วาง SL ใต้ปลายไส้เทียนกลับตัว ({float(live_rej_price):.2f} → SL {stop:.2f})")
            # Anchor SL safely behind nearest MTF Support if within reasonable scalp range
            elif near_sup and near_sup["distance_usd"] <= (max_safe_sl - 0.45):
                sup_sl = r2(near_sup["low"] - (10.0 if is_btc_sym else 0.45))
                if (entry - sup_sl) >= min_safe_sl and (entry - sup_sl) <= max_safe_sl:
                    stop = sup_sl
                    sr_notes.append(f"🛡️ วาง SL ใต้แนวรับ {near_sup['timeframe']} ({near_sup['mid']:.2f} → SL {stop:.2f})")
            # Check MTF Resistance Wall ahead & cap TP safely BEFORE hitting the wall
            if near_res:
                is_major_res = int(near_res.get("weight") or 2) >= 4
                if is_major_res and near_res["distance_usd"] < min_wall_clearance and live_candle_str < 83:
                    mtf_wall_blocked = True
                    sr_notes.append(f"⛔ ติดกำแพงแนวต้านใหญ่ {near_res['timeframe']} ({near_res['mid']:.2f} ห่าง ${near_res['distance_usd']:.2f}) — รอเบรกหรือย่อตัว")
                elif near_res["distance_usd"] <= target_dist * 1.45:
                    capped_tp = r2(near_res["low"] - (5.0 if is_btc_sym else 0.25))
                    if (capped_tp - entry) >= (20.0 if is_btc_sym else min_target_gold):
                        target = capped_tp
                        sr_notes.append(f"🎯 ตั้ง TP หน้าแนวต้าน {near_res['timeframe']} ({near_res['mid']:.2f} → TP {target:.2f})")
            # Refine TP with aligned Bullish Chart Pattern target if realistic
            if pat_bias == "BULLISH" and pat_target > entry:
                pat_dist = pat_target - entry
                if (min_target_gold if not is_btc_sym else 20.0) <= pat_dist <= (target_dist * 1.50):
                    if not near_res or pat_target <= near_res["low"]:
                        target = r2(pat_target)
                        sr_notes.append(f"📐 อิงเป้า Pattern {pat_name_th} → TP {target:.2f}")
            risk = max(0.01, entry - stop)
            reward = max(0.01, target - entry)
            tp1 = r2(entry + reward * 0.50)
        else:
            stop = r2(entry + min(max_safe_sl, sl_dist + spread_usd))
            target = r2(entry - target_dist)
            # Anchor SL behind fresh Rejection Wick tip (+ spread for SELL Ask) if reversal candle triggered
            if is_candle_reversal_entry and live_rej_price is not None and float(live_rej_price) > entry:
                wick_sl = r2(float(live_rej_price) + spread_usd + (8.0 if is_btc_sym else 0.35))
                if (wick_sl - entry) >= (min_safe_sl + spread_usd) and (wick_sl - entry) <= max_safe_sl:
                    stop = wick_sl
                    sr_notes.append(f"🕯️ วาง SL เหนือปลายไส้เทียนกลับตัว ({float(live_rej_price):.2f} + Spread → SL {stop:.2f})")
            # Anchor SL safely behind nearest MTF Resistance (+ spread cushion for SELL Ask, within hard cap)
            elif near_res and near_res["distance_usd"] <= (max_safe_sl - spread_usd - 0.45):
                res_sl = r2(near_res["high"] + spread_usd + (10.0 if is_btc_sym else 0.45))
                if (res_sl - entry) >= (min_safe_sl + spread_usd) and (res_sl - entry) <= max_safe_sl:
                    stop = res_sl
                    sr_notes.append(f"🛡️ วาง SL เหนือแนวต้าน {near_res['timeframe']} ({near_res['mid']:.2f} + Spread → SL {stop:.2f})")
            # Check MTF Support Floor ahead & cap TP BEFORE hitting the floor (+ Spread Cushion so Ask fills cleanly!)
            if near_sup:
                is_major_sup = int(near_sup.get("weight") or 2) >= 4
                if is_major_sup and near_sup["distance_usd"] < min_wall_clearance and live_candle_str < 83:
                    mtf_wall_blocked = True
                    sr_notes.append(f"⛔ ติดแนวรับใหญ่ {near_sup['timeframe']} ({near_sup['mid']:.2f} ห่าง ${near_sup['distance_usd']:.2f}) — ห้าม SELL ชนแนวรับ")
                elif near_sup["distance_usd"] <= target_dist * 1.45:
                    capped_tp = r2(near_sup["high"] + spread_usd + (5.0 if is_btc_sym else 0.15))
                    if (entry - capped_tp) >= (20.0 if is_btc_sym else min_target_gold):
                        target = capped_tp
                        sr_notes.append(f"🎯 ตั้ง TP เหนือแนวรับ {near_sup['timeframe']} + เผื่อ Spread ({near_sup['mid']:.2f} → TP {target:.2f})")
            # Refine TP with aligned Bearish Chart Pattern target if realistic (+ spread cushion for Ask fill)
            if pat_bias == "BEARISH" and 0.0 < pat_target < entry:
                adj_pat_tp = r2(pat_target + (0.0 if is_btc_sym else spread_usd))
                pat_dist = entry - adj_pat_tp
                if (min_target_gold if not is_btc_sym else 20.0) <= pat_dist <= (target_dist * 1.50):
                    if not near_sup or adj_pat_tp >= (near_sup["high"] + spread_usd):
                        target = adj_pat_tp
                        sr_notes.append(f"📐 อิงเป้า Pattern {pat_name_th} + Spread → TP {target:.2f}")
            risk = max(0.01, stop - entry)
            reward = max(0.01, entry - target)
            tp1 = r2(entry - reward * 0.50)

        rr = round(reward / max(0.01, risk), 2)
        sym_u = (symbol or "XAUUSD").upper()

        # 5. Confluence Scoring & Order Allocation Weight (MTF S/R + Candlestick + Pattern + Volume + Adaptive)
        confluence_factors = [
            (
                f"🕯️ เข้าไม้จากพฤติกรรมแท่งเทียนกลับตัว/ทิ้งไส้: {live_candle_note}"
                if is_candle_reversal_entry
                else f"⚡ สอดคล้องโครงสร้าง M1 Micro-Trend ({scalp_bias}) & MTF ({mtf_bias})"
            ),
            f"🌐 สภาพตลาด: {regime_label}",
        ] + sr_notes

        score = 64 if is_candle_reversal_entry else 62
        alloc_factor = 1.0
        want_bias = "BULLISH" if side == "BUY" else "BEARISH"

        if tr_bias == want_bias or mtf_bias == want_bias:
            score += 10
            alloc_factor *= 1.08
            confluence_factors.append(f"📈 ทิศทางตรงกับเทรนด์ใหญ่ MTF ({mtf_bias})")
        elif is_counter_trend_rev:
            # Counter-trend reversal: keep Lot sizing disciplined unless backed by major MTF S/R!
            alloc_factor *= 0.92
            confluence_factors.append(f"🔄 ไม้สวนเทรนด์ระยะสั้นจากสัญญาณแท่งเทียนกลับตัว (MTF: {mtf_bias})")

        # MTF S/R bounce bonus
        if side == "BUY" and near_sup and near_sup["distance_usd"] <= (25.0 if is_btc_sym else 2.60):
            score += 11
            alloc_factor *= 1.15
            confluence_factors.append(f"🧱 เด้งจากแนวรับ {near_sup['timeframe']} ({near_sup['mid']:.2f})")
        elif side == "SELL" and near_res and near_res["distance_usd"] <= (25.0 if is_btc_sym else 2.60):
            score += 11
            alloc_factor *= 1.15
            confluence_factors.append(f"🧱 โดนตบจากแนวต้าน {near_res['timeframe']} ({near_res['mid']:.2f})")

        if mtf_wall_blocked:
            score -= 22
            alloc_factor *= 0.50

        # Candlestick confirmation / opposition
        candle_aligned = (live_candle_bias == want_bias)
        candle_opposing = (live_candle_bias in ("BULLISH", "BEARISH") and live_candle_bias != want_bias)
        if candle_aligned:
            c_bonus = 14 if (is_candle_reversal_entry and live_candle_str >= 80) else 10
            score += c_bonus
            alloc_factor *= 1.14 if live_candle_str >= 80 else 1.08
            confluence_factors.append(f"🕯️ พฤติกรรมแท่งเทียนยืนยัน ({live_candle_pat_code or live_candle_cat}): {live_candle_note}")
        elif candle_opposing:
            score -= 16 if live_candle_str >= 80 else 12
            alloc_factor *= 0.68
            confluence_factors.append(f"⚠️ แท่งเทียนล่าสุดสวนทาง: {live_candle_note}")

        if (side == "BUY" and dealing_zone == "DISCOUNT") or (side == "SELL" and dealing_zone == "PREMIUM"):
            score += 5
            alloc_factor *= 1.05
            confluence_factors.append(f"💎 โซนราคาได้เปรียบ ({dealing_zone})")

        if vol_scale.get("delta_aligned"):
            score += 5
            alloc_factor *= 1.06

        # Chart Pattern confirmation / opposition
        pattern_aligned = bool(active_pat and pat_bias == want_bias)
        pattern_opposing = bool(active_pat and pat_bias in ("BULLISH", "BEARISH") and pat_bias != want_bias and pat_score >= 72)
        if pattern_aligned:
            score += 10
            alloc_factor *= 1.10
            confluence_factors.append(f"📐 Chart Pattern ยืนยัน: {pat_name_th} (Score {pat_score})")
        elif pattern_opposing:
            score -= 14
            alloc_factor *= 0.70
            confluence_factors.append(f"⚠️ ขัดกับ Chart Pattern: {pat_name_th} ({pat_bias})")

        if scalp_info.get("micro_fvg"):
            score += 6
            confluence_factors.append("💧 ตรวจพบ Micro-FVG หนุนแรงส่ง")
        if spread_usd <= 0.60:
            score += 4

        # Apply Background Adaptive Auto-Tuning (when consecutive losses >= 3)
        alloc_factor = round(max(0.40, min(1.40, alloc_factor * adapt_lot_scale)), 2)
        if adapt.get("active"):
            confluence_factors.append(f"🧬 Adaptive Engine: จัดสรร Lot {int(alloc_factor * 100)}% (หลังวิเคราะห์แพ้ติดกัน)")

        score = max(25, min(96, score))
        min_req_score = 60 + adapt_conf_boost
        should_wait = (
            mtf_wall_blocked
            or score < min_req_score
            or (adapt_req_candle and not candle_aligned)
        )
        setup_status = "WAIT_RETRACEMENT" if should_wait else "ACTIVE_IN_ZONE"

        return {
            "symbol": sym_u,
            "trading_style": "rapid_fire_test",
            "style_label_th": (
                "🕯️ โหมด Rapid Scalp กลับตัวด้วยแท่งเทียน (Wick/Reversal Sniper)"
                if is_candle_reversal_entry
                else "⚡ โหมด Rapid Scalp (MTF S/R + แท่งเทียน + Chart Pattern)"
            ),
            "setup_archetype": "RAPID_CANDLE_REVERSAL" if is_candle_reversal_entry else "RAPID_SCALP_HARVESTER",
            "archetype_label_th": (
                f"🕯️ Rapid กลับตัวแท่งเทียน ({live_candle_pat_code or 'REVERSAL'})"
                if is_candle_reversal_entry
                else "⚡ Rapid Scalp MTF+Candle+Pattern"
            ),
            "entry_style": entry_style,
            "is_counter_trend": is_counter_trend_rev,
            "available_setups": [],
            "side": side,
            "execution_type": "MARKET",
            "order_type": f"{side} NOW (Market)" if not should_wait else f"WAIT ({side} MTF/Candle)",
            "is_advance_entry": not should_wait,
            "distance_to_entry_usd": 0.0,
            "current_price": current_price,
            "bias": want_bias,
            "market_regime": regime_tag,
            "is_sideway": is_sideway,
            "regime_label_th": regime_label,
            "status": setup_status,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "target": target,
            "risk_usd": r2(risk),
            "reward_usd": r2(reward),
            "reward_risk": rr,
            "sl_atr_ratio": round(risk / max(0.1, atr_val), 2),
            "spread_included_usd": spread_usd,
            "zone_low": min(entry, stop),
            "zone_high": max(entry, stop),
            "confluence_score": score,
            "confluence_factors": confluence_factors,
            "allocation_factor": alloc_factor,
            "volume_tpsl_scaling": vol_scale,
            "mtf_sr_context": mtf_sr,
            "candle_signal": {
                "bias": live_candle_bias,
                "category": live_candle_cat,
                "pattern": live_candle_pat_code,
                "strength": live_candle_str,
                "rejection_price": live_rej_price,
                "aligned": candle_aligned,
                "opposing": candle_opposing,
                "note_th": live_candle_note,
                "bullish_pressure_pct": bull_press,
                "bearish_pressure_pct": bear_press,
                "dealing_zone": dealing_zone,
            },
            "pattern_signal": {
                "name_th": pat_name_th,
                "bias": pat_bias,
                "aligned": pattern_aligned,
                "opposing": pattern_opposing,
                "target": pat_target,
                "base_level": pat_base,
                "score": pat_score,
            },
            "scenario_b_sweep_reentry": r2(stop - 0.50 if side == "BUY" else stop + 0.50),
            "scenario_c_invalidation": r2(stop - 0.30 if side == "BUY" else stop + 0.30),
        }
    trend = structure_res["trend"]
    sr_zones = list(structure_res.get("sr_zones", []))
    htf_zones = list((mtf_res or {}).get("htf_sr_zones") or [])
    scalp_info = structure_res.get("scalping") or {}
    is_robot = str(trading_style).lower() == "robot_scalper"
    is_micro = str(trading_style).lower() == "micro_scalp"
    is_scalp = str(trading_style).lower() in ("scalping", "micro_scalp", "robot_scalper")

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

    # 1. Detect Session Sweep (Judas Swing) Setup:
    inst = structure_res.get("institutional_levels") or {}
    asian_status = str(inst.get("asian_status") or "")
    ash = inst.get("asian_high")
    asl = inst.get("asian_low")
    post_h = inst.get("post_high", ash)
    post_l = inst.get("post_low", asl)
    asian_mid = inst.get("asian_mid", round((ash + asl) / 2.0, 2) if (ash is not None and asl is not None) else None)

    session_sweep_setup: Optional[Dict[str, Any]] = None
    if ash is not None and asl is not None and (ash - asl) >= max(1.5, atr_val * 0.8):
        if asian_status == "ASIAN_HIGH_SWEPT_BEARISH" and current_price <= (ash + atr_val * 0.35) and current_price >= (asl - atr_val * 0.2):
            sw_hi = float(post_h or ash)
            sw_sl = r2(sw_hi + max(0.60, atr_val * 0.20, spread_usd * 2.0))
            is_close_to_ash = abs(current_price - ash) <= atr_val * 0.45
            sw_exec = "MARKET" if is_close_to_ash else "LIMIT"
            sw_entry = r2(current_price if sw_exec == "MARKET" else ash)
            sw_risk = max(0.35, abs(sw_sl - sw_entry))
            sw_tp1 = r2(asian_mid) if asian_mid else r2(sw_entry - sw_risk)
            cand_tp2 = r2(asl + max(0.40, (ash - asl) * 0.15))
            sw_tp2 = cand_tp2 if (sw_entry - cand_tp2) >= sw_risk * 1.55 else r2(sw_entry - sw_risk * 1.85)
            sw_score = 84
            sw_factors = [
                f"🎯 กวาดสภาพคล่องหัวตลาดเอเชีย (Asian High {ash:.2f} Swept by +{max(0.0, sw_hi - ash):.2f} USD)",
                "⚡ สถาบัน London/NY ทุบราคากลับเข้ากรอบ (Judas Swing Reversal)",
                f"เป้าหมายทดสอบดุลยภาพกรอบเอเชีย ({sw_tp1:.2f}) และขอบล่าง ({sw_tp2:.2f})",
            ]
            session_sweep_setup = {
                "setup_archetype": "SESSION_SWEEP",
                "archetype_label_th": "🎯 Asian High Liquidity Sweep (Judas Swing)",
                "bias": "BEARISH",
                "side": "SELL",
                "entry": sw_entry,
                "stop": sw_sl,
                "tp1": sw_tp1,
                "target": sw_tp2,
                "zone_low": r2(ash - atr_val * 0.15),
                "zone_high": r2(sw_hi),
                "confluence_score": sw_score,
                "confluence_factors": sw_factors,
                "execution_type": sw_exec,
            }
        elif asian_status == "ASIAN_LOW_SWEPT_BULLISH" and current_price >= (asl - atr_val * 0.35) and current_price <= (ash + atr_val * 0.2):
            sw_lo = float(post_l or asl)
            sw_sl = r2(sw_lo - max(0.60, atr_val * 0.20, spread_usd * 2.0))
            is_close_to_asl = abs(current_price - asl) <= atr_val * 0.45
            sw_exec = "MARKET" if is_close_to_asl else "LIMIT"
            sw_entry = r2(current_price if sw_exec == "MARKET" else asl)
            sw_risk = max(0.35, abs(sw_entry - sw_sl))
            sw_tp1 = r2(asian_mid) if asian_mid else r2(sw_entry + sw_risk)
            cand_tp2 = r2(ash - max(0.40, (ash - asl) * 0.15))
            sw_tp2 = cand_tp2 if (cand_tp2 - sw_entry) >= sw_risk * 1.55 else r2(sw_entry + sw_risk * 1.85)
            sw_score = 84
            sw_factors = [
                f"🎯 กวาดสภาพคล่องหางตลาดเอเชีย (Asian Low {asl:.2f} Swept by -{max(0.0, asl - sw_lo):.2f} USD)",
                "⚡ สถาบัน London/NY ดันราคากลับเข้ากรอบ (Judas Swing Reversal)",
                f"เป้าหมายทดสอบดุลยภาพกรอบเอเชีย ({sw_tp1:.2f}) และขอบบน ({sw_tp2:.2f})",
            ]
            session_sweep_setup = {
                "setup_archetype": "SESSION_SWEEP",
                "archetype_label_th": "🎯 Asian Low Liquidity Sweep (Judas Swing)",
                "bias": "BULLISH",
                "side": "BUY",
                "entry": sw_entry,
                "stop": sw_sl,
                "tp1": sw_tp1,
                "target": sw_tp2,
                "zone_low": r2(sw_lo),
                "zone_high": r2(asl + atr_val * 0.15),
                "confluence_score": sw_score,
                "confluence_factors": sw_factors,
                "execution_type": sw_exec,
            }

    # 2. Detect HTF-Aligned M5 Micro-Pullback Setup:
    htf_bias = (mtf_res or {}).get("overall_bias") or trend.get("bias")
    micro_pullback_setup: Optional[Dict[str, Any]] = None
    if htf_bias in ("BULLISH", "BEARISH") and scalp_info:
        r_state = scalp_info.get("ribbon_state")
        s_triggers = list(scalp_info.get("triggers") or [])
        e9 = float(scalp_info.get("ema9") or current_price)
        e21 = float(scalp_info.get("ema21") or (current_price - (atr_val * 0.5 if htf_bias == "BULLISH" else -atr_val * 0.5)))
        has_bounce = any("Pullback" in t or "Hook" in t or "Squeeze" in t for t in s_triggers)

        if htf_bias == "BULLISH" and (r_state == "BULL_RIBBON" or has_bounce):
            mp_entry = r2(current_price)
            mp_sl = r2(min(e21 - max(0.40, atr_val * 0.25), mp_entry - max(atr_val * 0.85, spread_usd * 2.5)))
            mp_risk = max(0.35, mp_entry - mp_sl)
            micro_pullback_setup = {
                "setup_archetype": "MICRO_PULLBACK",
                "archetype_label_th": "⚡ M5 Micro-Pullback ตามเทรนด์ใหญ่ H1/H4 (Bullish)",
                "bias": "BULLISH",
                "side": "BUY",
                "entry": mp_entry,
                "stop": mp_sl,
                "tp1": r2(mp_entry + mp_risk * 1.05),
                "target": r2(mp_entry + mp_risk * 2.10),
                "zone_low": r2(min(e9, e21) - atr_val * 0.15),
                "zone_high": r2(max(e9, e21) + atr_val * 0.15),
                "confluence_score": 78,
                "confluence_factors": [
                    f"📈 สอดคล้องเทรนด์ใหญ่ H1/H4 ({htf_bias})",
                    f"⚡ EMA9/21 Ribbon ({r_state}) @ {e9:.2f}",
                    *(s_triggers[:2] if s_triggers else []),
                ],
                "execution_type": "MARKET" if abs(current_price - e9) <= atr_val * 0.35 else "LIMIT",
            }
        elif htf_bias == "BEARISH" and (r_state == "BEAR_RIBBON" or has_bounce):
            mp_entry = r2(current_price)
            mp_sl = r2(max(e21 + max(0.40, atr_val * 0.25), mp_entry + max(atr_val * 0.85, spread_usd * 2.5)))
            mp_risk = max(0.35, mp_sl - mp_entry)
            micro_pullback_setup = {
                "setup_archetype": "MICRO_PULLBACK",
                "archetype_label_th": "⚡ M5 Micro-Pullback ตามเทรนด์ใหญ่ H1/H4 (Bearish)",
                "bias": "BEARISH",
                "side": "SELL",
                "entry": mp_entry,
                "stop": mp_sl,
                "tp1": r2(mp_entry - mp_risk * 1.05),
                "target": r2(mp_entry - mp_risk * 2.10),
                "zone_low": r2(min(e9, e21) - atr_val * 0.15),
                "zone_high": r2(max(e9, e21) + atr_val * 0.15),
                "confluence_score": 78,
                "confluence_factors": [
                    f"📈 สอดคล้องเทรนด์ใหญ่ H1/H4 ({htf_bias})",
                    f"⚡ EMA9/21 Ribbon ({r_state}) @ {e9:.2f}",
                    *(s_triggers[:2] if s_triggers else []),
                ],
                "execution_type": "MARKET" if abs(current_price - e9) <= atr_val * 0.35 else "LIMIT",
            }

    # 2.5 Detect High-Conviction Candlestick Reversal / Wick Rejection Setup (Counter-Trend & Zone Reversal!)
    smc_cb = structure_res.get("candle_behavior") or {}
    smc_cb_live = smc_cb.get("live_signal") or {}
    smc_dr = smc_cb.get("dealing_range") or {}
    smc_dr_zone = str(smc_dr.get("zone") or "EQUILIBRIUM").upper()
    smc_wick_dom = str(smc_cb.get("wick_dominance") or "BALANCED").upper()
    smc_rsi_div = structure_res.get("rsi_divergence") or {}
    smc_hist_sr = list(structure_res.get("historical_sr") or [])
    all_smc_sr = sr_zones + htf_zones + smc_hist_sr

    candle_reversal_setup: Optional[Dict[str, Any]] = None
    if (
        smc_cb_live.get("has_reversal_signal")
        and int(smc_cb_live.get("bars_ago", 99)) <= 2
        and int(smc_cb_live.get("strength", 0)) >= 74
        and smc_cb_live.get("signal_bias") in ("BULLISH", "BEARISH")
    ):
        cr_bias = str(smc_cb_live["signal_bias"]).upper()
        cr_side = "BUY" if cr_bias == "BULLISH" else "SELL"
        cr_str = int(smc_cb_live.get("strength") or 75)
        cr_pat = str(smc_cb_live.get("pattern") or "REVERSAL")
        cr_label = str(smc_cb_live.get("label_th") or cr_pat)
        cr_rej = smc_cb_live.get("rejection_price")
        exp_sr_role = "support" if cr_side == "BUY" else "resistance"

        # Check SMC / MTF Location Confluence for Reversal
        rev_reasons: List[str] = [f"🕯️ รูปแบบแท่งเทียนกลับตัว/ทิ้งไส้: {cr_label} (Strength {cr_str})"]
        loc_hits = 0

        near_sr_hit = next(
            (
                z for z in all_smc_sr
                if (z.get("role") == exp_sr_role or z.get("effective_role") == exp_sr_role)
                and abs(current_price - float(z.get("mid") or z.get("price") or 0.0)) <= atr_val * 1.35
            ),
            None,
        )
        if near_sr_hit:
            loc_hits += 1
            tf_lbl = near_sr_hit.get("timeframe") or near_sr_hit.get("role") or "MTF"
            mid_v = float(near_sr_hit.get("mid") or near_sr_hit.get("price") or current_price)
            rev_reasons.append(f"🧱 กลับตัวตรงแนว{'รับ' if cr_side == 'BUY' else 'ต้าน'} {tf_lbl} ({mid_v:.2f})")

        near_ob_hit = next(
            (o for o in obs if o.get("bias") == cr_bias and float(o.get("distance_usd") or 999) <= atr_val * 1.35),
            None,
        )
        if near_ob_hit:
            loc_hits += 1
            rev_reasons.append(f"🏛️ แท่งเทียนยืนยันในโซน Order Block ({near_ob_hit['low']:.2f}-{near_ob_hit['high']:.2f})")

        if (cr_side == "BUY" and smc_dr_zone == "DISCOUNT") or (cr_side == "SELL" and smc_dr_zone == "PREMIUM"):
            loc_hits += 1
            rev_reasons.append(f"💎 ราคาอยู่ในโซนได้เปรียบ {smc_dr_zone} ({smc_dr.get('position_pct', 50):.0f}%)")

        if smc_rsi_div.get("divergence_bias") == cr_bias:
            loc_hits += 1
            rev_reasons.append(f"📈 RSI Divergence สนับสนุนการกลับตัว ({smc_rsi_div.get('divergence')})")

        if (cr_side == "BUY" and smc_wick_dom == "BUYER_ABSORPTION") or (cr_side == "SELL" and smc_wick_dom == "SELLER_REJECTION"):
            loc_hits += 1
            rev_reasons.append(f"🕯️ พฤติกรรมไส้เทียนสะสมสนับสนุน ({smc_cb.get('wick_summary_th', '')})")

        if sweeps and sweeps[-1].get("implication", "").startswith(cr_bias):
            loc_hits += 1
            rev_reasons.append(f"💧 เกิด Liquidity Sweep @ {sweeps[-1]['level']:.2f}")

        # Trigger SMC Candlestick Reversal if backed by >=1 key zone/context OR elite pattern (strength >= 84)
        if loc_hits >= 1 or cr_str >= 84:
            cr_entry = r2(current_price)
            cr_buf = max(0.45, atr_val * 0.25, spread_usd * 1.5)
            if cr_side == "BUY":
                raw_cr_sl = (float(cr_rej) - cr_buf) if (cr_rej is not None and float(cr_rej) < cr_entry) else (cr_entry - max(atr_val * 1.0, spread_usd * 2.8))
                cr_sl = r2(min(raw_cr_sl, cr_entry - max(1.80, spread_usd * 2.5)))
                cr_risk = max(0.35, cr_entry - cr_sl)
                cr_tp1 = r2(cr_entry + cr_risk * 1.05)
                cr_tp2 = r2(cr_entry + cr_risk * 2.05)
                cr_zl = r2(float(cr_rej) if cr_rej is not None else cr_sl)
                cr_zh = r2(cr_entry + atr_val * 0.20)
            else:
                raw_cr_sl = (float(cr_rej) + cr_buf + spread_usd) if (cr_rej is not None and float(cr_rej) > cr_entry) else (cr_entry + max(atr_val * 1.0 + spread_usd, spread_usd * 3.0))
                cr_sl = r2(max(raw_cr_sl, cr_entry + max(1.80 + spread_usd, spread_usd * 3.0)))
                cr_risk = max(0.35, cr_sl - cr_entry)
                cr_tp1 = r2(cr_entry - cr_risk * 1.05)
                cr_tp2 = r2(cr_entry - cr_risk * 2.05)
                cr_zl = r2(cr_entry - atr_val * 0.20)
                cr_zh = r2(float(cr_rej) if cr_rej is not None else cr_sl)

            cr_score = min(92, 72 + (loc_hits * 5) + (6 if cr_str >= 82 else 0))
            candle_reversal_setup = {
                "setup_archetype": "CANDLE_REVERSAL_SMC",
                "archetype_label_th": f"🕯️ SMC กลับตัวด้วยแท่งเทียน ({cr_label})",
                "bias": cr_bias,
                "side": cr_side,
                "entry": cr_entry,
                "stop": cr_sl,
                "tp1": cr_tp1,
                "target": cr_tp2,
                "zone_low": cr_zl,
                "zone_high": cr_zh,
                "confluence_score": cr_score,
                "confluence_factors": rev_reasons,
                "execution_type": "MARKET",
            }

    # Archetype Selection Hierarchy:
    # 1. Session Liquidity Sweep (Judas Swing) - real-time high conviction
    # 2. Candlestick Reversal / Wick Rejection at Key SMC/MTF Zone (catches reversals & counter-trend bounces!)
    # 3. Scalping mode (if is_scalp)
    # 4. Order Block within reachable distance
    # 5. Micro-Pullback in direction of HTF trend
    # 6. Order Block / S&R fallback
    setup_archetype = "ORDER_BLOCK_PULLBACK"
    archetype_label_th = "🏛️ SMC Order Block Pullback"
    preset_stop: Optional[float] = None
    preset_tp1: Optional[float] = None
    preset_target: Optional[float] = None

    if session_sweep_setup is not None:
        setup_archetype = session_sweep_setup["setup_archetype"]
        archetype_label_th = session_sweep_setup["archetype_label_th"]
        bias = session_sweep_setup["bias"]
        side = session_sweep_setup["side"]
        entry = session_sweep_setup["entry"]
        preset_stop = session_sweep_setup["stop"]
        preset_tp1 = session_sweep_setup["tp1"]
        preset_target = session_sweep_setup["target"]
        zone_low = session_sweep_setup["zone_low"]
        zone_high = session_sweep_setup["zone_high"]
        confluence_score = session_sweep_setup["confluence_score"]
        confluence_factors = list(session_sweep_setup["confluence_factors"])
    elif candle_reversal_setup is not None:
        setup_archetype = candle_reversal_setup["setup_archetype"]
        archetype_label_th = candle_reversal_setup["archetype_label_th"]
        bias = candle_reversal_setup["bias"]
        side = candle_reversal_setup["side"]
        entry = candle_reversal_setup["entry"]
        preset_stop = candle_reversal_setup["stop"]
        preset_tp1 = candle_reversal_setup["tp1"]
        preset_target = candle_reversal_setup["target"]
        zone_low = candle_reversal_setup["zone_low"]
        zone_high = candle_reversal_setup["zone_high"]
        confluence_score = candle_reversal_setup["confluence_score"]
        confluence_factors = list(candle_reversal_setup["confluence_factors"])
    elif is_scalp and scalp_info:
        # In Scalping / Micro-Scalp mode, prioritize fast momentum, Micro-FVG fill, and tight EMA 9/21 pullbacks
        micro_fvg = scalp_info.get("micro_fvg")
        bias = scalp_info.get("scalp_bias", "BULLISH")
        side = "BUY" if bias == "BULLISH" else "SELL"
        e9 = float(scalp_info.get("ema9") or current_price)
        confluence_score = int(scalp_info.get("scalp_score") or 62)
        confluence_factors = [
            f"⚡ EMA9/21 Scalp Ribbon ({scalp_info.get('ribbon_state', '-')})",
            *list(scalp_info.get("triggers") or []),
        ]

        if micro_fvg:
            setup_archetype = "MICRO_FVG_SCALP"
            archetype_label_th = "⚡ M1 Micro-FVG Scalp (เติมสภาพคล่องความเร็วสูง)" if is_micro else "⚡ Micro-FVG Scalp"
            f_top = float(micro_fvg["top"])
            f_bot = float(micro_fvg["bottom"])
            zone_low = r2(f_bot)
            zone_high = r2(f_top)
            entry = r2(current_price)
            confluence_score = max(confluence_score, 82)
            confluence_factors.append(f"Micro-FVG Imbalance ({f_bot:.2f} - {f_top:.2f})")
        else:
            setup_archetype = "MICRO_SCALP_MOMENTUM" if is_micro else "SCALPING_MOMENTUM"
            archetype_label_th = "⚡ สัญญาณสเกลป์ความเร็วสูง (High-Frequency Micro-Scalp)" if is_micro else "⚡ สัญญาณสเกลป์ความเร็วสูง (Fast Scalp)"
            scalp_ob = next(
                (o for o in candidate_obs if o["bias"] == bias and o["distance_usd"] <= atr_val * 0.85),
                None,
            )
            if scalp_ob:
                zone_low, zone_high = scalp_ob["low"], scalp_ob["high"]
                if side == "BUY":
                    target_lvl = round((zone_low + zone_high) / 2.0, 2) if current_price <= zone_high else zone_high
                    entry = min(round(current_price - max(0.15, atr_val * 0.10), 2), target_lvl)
                else:
                    target_lvl = round((zone_low + zone_high) / 2.0, 2) if current_price >= zone_low else zone_low
                    entry = max(round(current_price + max(0.15, atr_val * 0.10), 2), target_lvl)
            else:
                if side == "BUY":
                    pullback_lvl = min(e9, current_price - max(0.15, atr_val * 0.10))
                    entry = round(pullback_lvl, 2)
                    zone_low = entry - atr_val * 0.20
                    zone_high = entry + atr_val * 0.08
                else:
                    pullback_lvl = max(e9, current_price + max(0.15, atr_val * 0.10))
                    entry = round(pullback_lvl, 2)
                    zone_low = entry - atr_val * 0.08
                    zone_high = entry + atr_val * 0.20
    elif chosen_ob and chosen_ob["distance_usd"] <= atr_val * 1.6:
        setup_archetype = "ORDER_BLOCK_PULLBACK"
        archetype_label_th = "🏛️ SMC Order Block Pullback"
        bias = chosen_ob["bias"]
        side = "BUY" if bias == "BULLISH" else "SELL"
        zone_low, zone_high = chosen_ob["low"], chosen_ob["high"]
        if side == "BUY":
            target_lvl = round((zone_low + zone_high) / 2.0, 2) if current_price <= zone_high else zone_high
            entry = min(round(current_price - max(0.25, atr_val * 0.15), 2), target_lvl)
        else:
            target_lvl = round((zone_low + zone_high) / 2.0, 2) if current_price >= zone_low else zone_low
            entry = max(round(current_price + max(0.25, atr_val * 0.15), 2), target_lvl)
        confluence_score = chosen_ob["confluence_score"]
        confluence_factors = list(chosen_ob["confluence_factors"])
    elif micro_pullback_setup is not None and any("Pullback" in t or "Hook" in t for t in list(scalp_info.get("triggers") or [])):
        setup_archetype = micro_pullback_setup["setup_archetype"]
        archetype_label_th = micro_pullback_setup["archetype_label_th"]
        bias = micro_pullback_setup["bias"]
        side = micro_pullback_setup["side"]
        entry = micro_pullback_setup["entry"]
        preset_stop = micro_pullback_setup["stop"]
        preset_tp1 = micro_pullback_setup["tp1"]
        preset_target = micro_pullback_setup["target"]
        zone_low = micro_pullback_setup["zone_low"]
        zone_high = micro_pullback_setup["zone_high"]
        confluence_score = micro_pullback_setup["confluence_score"]
        confluence_factors = list(micro_pullback_setup["confluence_factors"])
    elif chosen_ob:
        setup_archetype = "ORDER_BLOCK_PULLBACK"
        archetype_label_th = "🏛️ SMC Order Block Pullback"
        bias = chosen_ob["bias"]
        side = "BUY" if bias == "BULLISH" else "SELL"
        zone_low, zone_high = chosen_ob["low"], chosen_ob["high"]
        if side == "BUY":
            target_lvl = round((zone_low + zone_high) / 2.0, 2) if current_price <= zone_high else zone_high
            entry = min(round(current_price - max(0.25, atr_val * 0.15), 2), target_lvl)
        else:
            target_lvl = round((zone_low + zone_high) / 2.0, 2) if current_price >= zone_low else zone_low
            entry = max(round(current_price + max(0.25, atr_val * 0.15), 2), target_lvl)
        confluence_score = chosen_ob["confluence_score"]
        confluence_factors = list(chosen_ob["confluence_factors"])
    else:
        # Fallback when no clean OB is nearby: plan a Pending Limit at nearest S&R / FVG ahead of price
        setup_archetype = "SR_LEVEL"
        archetype_label_th = "📊 แนวรับต้านสำคัญ (Key S&R)"
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
    # If price is inside the fresh zone or candlestick reversal/wick confirmation is active, enter immediately at market!
    is_in_zone = (zone_low - atr_val * 0.15) <= current_price <= (zone_high + atr_val * 0.15)
    is_near_zone_candle = (
        smc_cb_live.get("has_signal")
        and str(smc_cb_live.get("signal_bias") or "").upper() == bias
        and int(smc_cb_live.get("strength") or 0) >= 75
        and (zone_low - atr_val * 0.65) <= current_price <= (zone_high + atr_val * 0.65)
    )
    has_recent_sweep = (
        bool(sweeps)
        and sweeps[-1]["implication"].startswith(bias)
        and (len(structure_res.get("swings", {}).get("all", [])) > 0)
    )
    has_scalp_trigger = is_scalp and (int(scalp_info.get("scalp_score") or 0) >= 55 or is_micro)

    if setup_archetype == "CANDLE_REVERSAL_SMC":
        should_market_enter = True
    elif is_scalp:
        should_market_enter = is_in_zone or has_recent_sweep or has_scalp_trigger or is_micro or is_robot or is_near_zone_candle
    else:
        # SMC Swing: enter Market when inside Order Block zone OR confirmed by a live candlestick reversal/rejection near the zone
        should_market_enter = is_in_zone or is_near_zone_candle
    if should_market_enter:
        execution_type = "MARKET"
        order_type = f"{side} NOW (Market)"
        entry = r2(current_price)
        dist_to_entry = 0.0
        status = "ACTIVE_IN_ZONE"
        confluence_factors.append(
            "🕯️ แท่งเทียนยืนยันกลับตัว/ปฏิเสธราคาในโซน — เข้าทันที (Market Execution)"
            if (setup_archetype == "CANDLE_REVERSAL_SMC" or is_near_zone_candle)
            else "สัญญาณยืนยันเข้าทันทีที่ราคาปัจจุบัน (Market Execution)"
        )
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

    # Historical & Live Candlestick Behavior, Wick Rejection & Dealing Range confluence
    cb = structure_res.get("candle_behavior") or {}
    dr = cb.get("dealing_range") or {}
    if (side == "BUY" and dr.get("zone") == "DISCOUNT") or (side == "SELL" and dr.get("zone") == "PREMIUM"):
        confluence_score = min(100, confluence_score + 6)
        confluence_factors.append(f"Dealing Range {dr.get('zone')} ({dr.get('position_pct', 50):.0f}%)")

    smc_candle_aligned = False
    smc_candle_opposing = False
    cb_sig_bias = str(smc_cb_live.get("signal_bias") or "NEUTRAL").upper()
    cb_sig_str = int(smc_cb_live.get("strength") or 0)
    if smc_cb_live.get("has_signal") and cb_sig_bias == bias:
        smc_candle_aligned = True
        if setup_archetype != "CANDLE_REVERSAL_SMC":
            confluence_score = min(100, confluence_score + (12 if cb_sig_str >= 80 else 9))
            confluence_factors.append(f"🕯️ พฤติกรรมแท่งเทียนยืนยัน: {smc_cb_live.get('label_th') or smc_cb_live.get('pattern')}")
    elif smc_cb_live.get("has_reversal_signal") and cb_sig_bias in ("BULLISH", "BEARISH") and cb_sig_bias != bias and cb_sig_str >= 78:
        smc_candle_opposing = True
        confluence_score = max(30, confluence_score - 12)
        confluence_factors.append(f"⚠️ ระวังแท่งเทียนล่าสุดสวนทาง: {smc_cb_live.get('label_th') or smc_cb_live.get('pattern')}")
    else:
        recent_patterns = cb.get("recent_patterns") or []
        matching_patterns = [p for p in recent_patterns[-4:] if p.get("bias") == bias]
        if matching_patterns:
            smc_candle_aligned = True
            last_pat = matching_patterns[-1]
            confluence_score = min(100, confluence_score + 8)
            confluence_factors.append(f"🕯️ Candle Pattern: {last_pat.get('label_th') or last_pat['pattern']} @ {last_pat['price']:.2f}")

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

    # Deep Historical Multi-Touch Support / Resistance Confluence across 15,000 bars
    hist_sr = list(structure_res.get("historical_sr") or [])
    expected_eff_role = "support" if side == "BUY" else "resistance"
    near_hist = [
        hz
        for hz in hist_sr
        if hz.get("effective_role") == expected_eff_role
        and abs(entry - float(hz["mid"])) <= atr_val * 0.90
    ]
    if near_hist:
        best_hist = near_hist[0]
        # Align Limit entry directly with high-confluence historical shelf if very close
        if abs(entry - float(best_hist["mid"])) <= atr_val * 0.35:
            entry = r2(best_hist["mid"])
        confluence_score = min(100, confluence_score + 10)
        touches = best_hist.get("touches_count") or len(best_hist.get("tfs") or [])
        confluence_factors.append(
            f"แนวรับต้านประวัติศาสตร์ 15,000 แท่ง ({touches}x @ {best_hist['mid']:.2f}, {best_hist.get('role', 'SR')})"
        )

    # Chart Pattern Confluence (Double Top/Bottom, H&S, Triangles, Flags)
    chart_patterns = structure_res.get("chart_patterns") or {}
    active_pat = chart_patterns.get("active_pattern")
    if active_pat and active_pat.get("bias") == bias:
        confluence_score = min(100, confluence_score + 9)
        pat_name = active_pat.get("name_th") or active_pat.get("name")
        pat_status = active_pat.get("status_th") or active_pat.get("status")
        confluence_factors.append(f"Chart Pattern: {pat_name} ({pat_status})")

    # Dynamic Volume-Scaled TP/SL Multipliers (RVOL + Volume Delta) + Safe Hard SL Cap
    is_btc_sym = "BTC" in str(symbol).upper()
    smc_vol_scale = _compute_volume_tpsl_multipliers(structure_res.get("volume_flow") or {}, side)
    adapt_sl_mult = max(0.75, min(1.35, float((adaptive_tuning or {}).get("sl_buffer_mult", 1.0) or 1.0)))
    adapt_tp_mult = max(0.75, min(1.35, float((adaptive_tuning or {}).get("tp_mult", 1.0) or 1.0)))
    confluence_factors.append(smc_vol_scale["note_th"])

    max_smc_sl = 250.0 if is_btc_sym else (4.00 if is_micro else (5.50 if is_scalp else 8.00))
    min_smc_sl = 40.0 if is_btc_sym else max(1.80, spread_usd * 2.5)

    # Stop Loss placement: beyond distal edge of zone + Volume-Scaled ATR buffer + Broker Spread
    sl_buffer = (
        max(
            atr_val * (0.18 if is_micro else (0.25 if is_scalp else 0.50)),
            spread_usd * (1.2 if is_micro else (1.5 if is_scalp else 2.5)),
        )
        * smc_vol_scale["sl_mult"]
        * adapt_sl_mult
    )
    min_sl_atr = (0.50 if is_micro else (0.65 if is_scalp else 1.05)) * smc_vol_scale["sl_mult"] * adapt_sl_mult
    base_min_rr = 1.15 if is_micro else (1.25 if is_scalp else 1.55)
    min_target_rr = round(max(1.10, base_min_rr * min(1.0, smc_vol_scale["tp_mult"])), 2)
    default_target_rr = round(
        (1.35 if is_micro else (1.45 if is_scalp else 2.20)) * smc_vol_scale["tp_mult"] * adapt_tp_mult,
        2,
    )
    combined_sr = sr_zones + htf_zones + hist_sr
    if preset_stop is not None and preset_target is not None:
        raw_risk = abs(entry - preset_stop) * smc_vol_scale["sl_mult"] * adapt_sl_mult
        raw_reward = abs(preset_target - entry) * smc_vol_scale["tp_mult"] * adapt_tp_mult
        if side == "BUY":
            risk = round(max(min_smc_sl, min(max_smc_sl, raw_risk)), 2)
            stop = r2(entry - risk)
            target = r2(entry + max(risk * min_target_rr, raw_reward))
            up_walls = [
                p["price"] for p in liquidity_pools if p["price"] >= entry + risk * min_target_rr
            ] + [
                z["mid"] for z in combined_sr if z["role"] == "resistance" and z["mid"] >= entry + risk * min_target_rr
            ]
            if up_walls and min(up_walls) < target:
                target = r2(min(up_walls))
            tp1 = r2(entry + min(risk, (target - entry) * 0.55))
            sweep_zone_price = r2(stop - atr_val * 0.7)
            invalidation_price = r2(stop - atr_val * 0.45)
        else:
            risk = round(max(min_smc_sl + spread_usd, min(max_smc_sl, raw_risk + spread_usd)), 2)
            stop = r2(entry + risk)
            target = r2(entry - max(risk * min_target_rr, raw_reward))
            dn_walls = [
                p["price"] for p in liquidity_pools if p["price"] <= entry - risk * min_target_rr
            ] + [
                z["mid"] for z in combined_sr if z["role"] == "support" and z["mid"] <= entry - risk * min_target_rr
            ]
            if dn_walls and max(dn_walls) > target:
                cushioned_tp = r2(max(dn_walls) + (0.0 if is_btc_sym else spread_usd))
                if (entry - cushioned_tp) >= max(risk * 0.90, spread_usd + 0.90):
                    target = cushioned_tp
                    confluence_factors.append(
                        f"🎯 ตั้ง TP SELL เหนือแนวรับ + เผื่อ Spread ${spread_usd:.2f} (TP {target:.2f})"
                    )
                else:
                    target = r2(max(dn_walls))
            tp1 = r2(entry - min(risk, (entry - target) * 0.55))
            sweep_zone_price = r2(stop + atr_val * 0.7)
            invalidation_price = r2(stop + atr_val * 0.45)
    elif side == "BUY":
        raw_stop = min(zone_low - sl_buffer, entry - max(atr_val * min_sl_atr, min_smc_sl))
        if is_scalp and scalp_info.get("ema21"):
            e21 = float(scalp_info["ema21"])
            if e21 < entry:
                raw_stop = max(raw_stop, e21 - sl_buffer)
        stop = r2(raw_stop)
        risk = max(0.35, entry - stop)
        if is_micro and risk > atr_val * 0.75 * smc_vol_scale["sl_mult"]:
            stop = r2(entry - atr_val * 0.65 * smc_vol_scale["sl_mult"])
            risk = max(0.35, entry - stop)
        elif is_scalp and risk > atr_val * 1.0 * smc_vol_scale["sl_mult"]:
            stop = r2(entry - atr_val * 0.85 * smc_vol_scale["sl_mult"])
            risk = max(0.35, entry - stop)
        if risk > max_smc_sl:
            stop = r2(entry - max_smc_sl)
            risk = max(0.35, entry - stop)

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
        if smc_vol_scale["tp_mult"] < 1.0 and (target - entry) > risk * default_target_rr * 1.15:
            target = r2(entry + risk * default_target_rr)
        if is_scalp and (target - entry) > risk * 1.60 * smc_vol_scale["tp_mult"]:
            target = r2(entry + risk * 1.45 * smc_vol_scale["tp_mult"])
        tp1 = r2(entry + min(risk * (0.75 if is_micro else (0.80 if is_scalp else 1.0)), (target - entry) * 0.55))

        deeper_zones = [
            o for o in obs if o["bias"] == "BULLISH" and o["high"] < zone_low
        ] + [
            z for z in combined_sr if z["role"] == "support" and z["mid"] < stop
        ]
        sweep_zone_price = r2(deeper_zones[0]["mid"]) if deeper_zones else r2(stop - atr_val * 0.7)
        invalidation_price = r2(stop - atr_val * 0.45)
    else:
        # For SELL, Stop Loss is hit by Ask (High + Spread), so include spread_usd explicitly
        raw_stop = max(
            zone_high + sl_buffer + spread_usd,
            entry + max(atr_val * min_sl_atr + spread_usd, min_smc_sl + spread_usd),
        )
        if is_scalp and scalp_info.get("ema21"):
            e21 = float(scalp_info["ema21"])
            if e21 > entry:
                raw_stop = min(raw_stop, e21 + sl_buffer + spread_usd)
        stop = r2(raw_stop)
        risk = max(0.35, stop - entry)
        if is_micro and risk > atr_val * 0.75 * smc_vol_scale["sl_mult"] + spread_usd:
            stop = r2(entry + atr_val * 0.65 * smc_vol_scale["sl_mult"] + spread_usd)
            risk = max(0.35, stop - entry)
        elif is_scalp and risk > atr_val * 1.0 * smc_vol_scale["sl_mult"] + spread_usd:
            stop = r2(entry + atr_val * 0.85 * smc_vol_scale["sl_mult"] + spread_usd)
            risk = max(0.35, stop - entry)
        if risk > max_smc_sl:
            stop = r2(entry + max_smc_sl)
            risk = max(0.35, stop - entry)

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
        if dn_targets:
            nearest_floor = max(dn_targets)
            cushioned_tp = r2(nearest_floor + (0.0 if is_btc_sym else spread_usd))
            if (entry - cushioned_tp) >= max(risk * 0.90, spread_usd + 0.90):
                target = cushioned_tp
                confluence_factors.append(
                    f"🎯 ตั้ง TP SELL เหนือแนวรับ/สภาพคล่อง + เผื่อ Spread ${spread_usd:.2f} (TP {target:.2f})"
                )
            else:
                target = r2(nearest_floor)
        else:
            target = r2(entry - risk * default_target_rr)
        if smc_vol_scale["tp_mult"] < 1.0 and (entry - target) > risk * default_target_rr * 1.15:
            target = r2(entry - risk * default_target_rr)
        if is_scalp and (entry - target) > risk * 1.60 * smc_vol_scale["tp_mult"]:
            target = r2(entry - risk * 1.45 * smc_vol_scale["tp_mult"])
        tp1 = r2(entry - min(risk * (0.75 if is_micro else (0.80 if is_scalp else 1.0)), (entry - target) * 0.55))

        higher_zones = [
            o for o in obs if o["bias"] == "BEARISH" and o["low"] > zone_high
        ] + [
            z for z in combined_sr if z["role"] == "resistance" and z["mid"] > stop
        ]
        sweep_zone_price = r2(higher_zones[0]["mid"]) if higher_zones else r2(stop + atr_val * 0.7)
        invalidation_price = r2(stop + atr_val * 0.45)

    reward = abs(target - entry)

    if is_robot:
        is_btc_sym = "BTC" in str(symbol).upper()
        robot_tp_dist = 100.0 if is_btc_sym else 1.80
        robot_sl_dist = 180.0 if is_btc_sym else 2.80
        robot_tp1_dist = 60.0 if is_btc_sym else 1.20
        if side == "BUY":
            stop = r2(entry - robot_sl_dist)
            target = r2(entry + robot_tp_dist)
            tp1 = r2(entry + robot_tp1_dist)
            risk = max(0.35, entry - stop)
            sweep_zone_price = r2(stop - atr_val * 0.7)
            invalidation_price = r2(stop - atr_val * 0.45)
        else:
            stop = r2(entry + robot_sl_dist + spread_usd)
            target = r2(entry - robot_tp_dist)
            tp1 = r2(entry - robot_tp1_dist)
            risk = max(0.35, stop - entry)
            sweep_zone_price = r2(stop + atr_val * 0.7)
            invalidation_price = r2(stop + atr_val * 0.45)
        reward = abs(target - entry)
        confluence_score = max(confluence_score, 78)
        confluence_factors.append("🤖 สัญญาณหุ่นยนต์เก็บกำไรสั้นความถี่สูง (Robot Range Harvester)")
        confluence_factors.append(f"🎯 เป้าหมายสั้น ${robot_tp_dist:.2f} | Hard Stop ${robot_sl_dist:.2f}")
        confluence_factors.append("⚡ วิเคราะห์ Timeframe 1M เป็นหลักเพื่อความเร็วในการเข้าออกออเดอร์")
        confluence_factors.append("🔒 ออกไม้คงที่ 0.01 Lot ต่อออเดอร์ (เน้นซอยไม้กระจายความเสี่ยง)")

        # Check MTF higher timeframe S/R alignment (M5, M15, H1, H4)
        htf_supports = [z for z in htf_zones if z.get("role") == "support" and float(z.get("mid", 0)) <= entry]
        htf_resistances = [z for z in htf_zones if z.get("role") == "resistance" and float(z.get("mid", 0)) >= entry]
        if side == "BUY" and htf_supports:
            nearest_s = min(htf_supports, key=lambda z: abs(entry - float(z.get("mid", 0))))
            if abs(entry - float(nearest_s.get("mid", 0))) <= atr_val * 1.5:
                confluence_score = min(100, confluence_score + 8)
                confluence_factors.append(
                    f"🏛️ วิเคราะห์ร่วม MTF: ได้แรงหนุนจากแนวรับ {nearest_s['timeframe']} ({nearest_s['mid']:.2f})"
                )
        elif side == "SELL" and htf_resistances:
            nearest_r = min(htf_resistances, key=lambda z: abs(float(z.get("mid", 0)) - entry))
            if abs(float(nearest_r.get("mid", 0)) - entry) <= atr_val * 1.5:
                confluence_score = min(100, confluence_score + 8)
                confluence_factors.append(
                    f"🏛️ วิเคราะห์ร่วม MTF: ได้แรงกดดันจากแนวต้าน {nearest_r['timeframe']} ({nearest_r['mid']:.2f})"
                )

    rr = round(reward / max(0.01, risk), 2)
    dist_to_entry = abs(current_price - entry)
    is_advance_entry = True if execution_type == "MARKET" else ((entry <= current_price) if side == "BUY" else (entry >= current_price))

    # Smart Conviction Allocation Factor for SMC Swing (drives Dynamic Lot Allocation in risk_gate)
    smc_alloc = 1.0
    if confluence_score >= 86:
        smc_alloc *= 1.22
    elif confluence_score >= 78:
        smc_alloc *= 1.12
    elif confluence_score < 64:
        smc_alloc *= 0.80
    if smc_candle_aligned:
        smc_alloc *= 1.12
    elif smc_candle_opposing:
        smc_alloc *= 0.72
    if smc_vol_scale.get("delta_aligned"):
        smc_alloc *= 1.06
    if rr >= 2.0:
        smc_alloc *= 1.05
    smc_alloc = round(max(0.45, min(1.40, smc_alloc * adapt_lot_scale)), 2)

    smc_entry_style = (
        "CANDLE_REVERSAL"
        if setup_archetype in ("CANDLE_REVERSAL_SMC", "SESSION_SWEEP")
        else "TREND_CONTINUATION"
    )
    smc_is_counter_trend = bool(
        mtf_res
        and str(mtf_res.get("overall_bias") or "").upper() in ("BULLISH", "BEARISH")
        and str(mtf_res.get("overall_bias") or "").upper() != bias
    )

    available_setups = [s for s in [session_sweep_setup, candle_reversal_setup, micro_pullback_setup] if s is not None]

    sym_u = (symbol or "XAUUSD").upper()
    is_btc = "BTC" in sym_u
    if is_robot:
        style_label = (
            "🤖 โหมดหุ่นยนต์เก็บกำไรถี่ (BTC Robot Harvester)"
            if is_btc
            else "🤖 โหมดหุ่นยนต์เก็บกำไรถี่ (Gold Robot Harvester)"
        )
        setup_archetype = "ROBOT_HARVESTER"
        archetype_label_th = "🤖 Robot Range Harvester (เก็บกำไรถี่)"
    elif is_micro:
        style_label = (
            "⚡ โหมดเทรดสั้นความถี่สูง (BTC Micro-Scalp)"
            if is_btc
            else "⚡ โหมดเทรดสั้นความถี่สูง (Gold Micro-Scalp)"
        )
    elif is_scalp:
        style_label = (
            "⚡ โหมดเทรดสั้น (BTC Scalping)"
            if is_btc
            else "⚡ โหมดเทรดสั้น (Gold Scalping)"
        )
    elif setup_archetype == "CANDLE_REVERSAL_SMC":
        style_label = (
            "🕯️ โหมด SMC กลับตัวด้วยแท่งเทียน (BTC Reversal)"
            if is_btc
            else "🕯️ โหมด SMC กลับตัวด้วยแท่งเทียน (Gold Reversal)"
        )
    else:
        style_label = (
            "🎯 โหมดเทรดรอบปกติ (BTC SMC)"
            if is_btc
            else "🎯 โหมดเทรดรอบปกติ (Intraday SMC)"
        )

    return {
        "symbol": sym_u,
        "trading_style": "robot_scalper" if is_robot else ("micro_scalp" if is_micro else ("scalping" if is_scalp else "intraday")),
        "style_label_th": style_label,
        "setup_archetype": setup_archetype,
        "archetype_label_th": archetype_label_th,
        "entry_style": smc_entry_style,
        "is_counter_trend": smc_is_counter_trend,
        "available_setups": available_setups,
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
        "volume_tpsl_scaling": smc_vol_scale,
        "allocation_factor": smc_alloc,
        "candle_signal": {
            "bias": cb_sig_bias,
            "category": str(smc_cb_live.get("category") or "NONE").upper(),
            "pattern": str(smc_cb_live.get("pattern") or ""),
            "strength": cb_sig_str,
            "rejection_price": smc_cb_live.get("rejection_price"),
            "aligned": smc_candle_aligned,
            "opposing": smc_candle_opposing,
            "note_th": str(smc_cb_live.get("label_th") or ""),
            "dealing_zone": smc_dr_zone,
        },
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
    symbol: str = "XAUUSD",
    adaptive_tuning: Optional[Dict[str, Any]] = None,
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
        symbol=symbol,
        bars=bars,
        adaptive_tuning=adaptive_tuning,
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
        "available_setups": setup.get("available_setups", []),
        "chart_patterns": st.get("chart_patterns"),
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
