#!/usr/bin/env python3
"""Chart Simulation & Risk Probability Engine (simulate.py).

Evolved from day-trader-skill's timemachine.py for XAU/USD:
  - Hybrid Historical Bootstrap + ATR Volatility + EMA Slope Drift Monte Carlo (N=1,000 paths)
  - Calculates Win Probability (P_TP), Stop Probability (P_SL), Open-at-Horizon (P_neither)
  - Calculates Sweep Risk (P_sweep): paths that pierce SL (within deeper liquidity / invalidation band)
    and subsequently reverse to hit TP within the simulation horizon
  - Calculates Expected Value (EV in R units): EV = (P_TP * R_reward) - (P_SL * 1.0)
  - Produces Fan Chart percentile bands (P5, P25, P50, P75, P95) and 3-Scenario Tree data
"""

import argparse
import json
import random
import statistics
import sys
from typing import Any, Dict, List, Optional

from engine.smc import analyze_smc
from engine.structure import analyze_structure, r2

TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
}


def _pct(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(q * len(sorted_vals))))
    return sorted_vals[idx]


def build_bar_pool(bars: List[Dict[str, Any]], atr_val: float) -> List[Dict[str, float]]:
    """Build pool of normalized bar deltas (scaled to current ATR) from recent history."""
    if len(bars) < 5:
        return [{"dc": 0.0, "hi_ext": atr_val * 0.4, "lo_ext": atr_val * 0.4}]

    # Measure historical average range to rescale into current ATR regime
    ranges = [max(0.05, float(b["h"]) - float(b["l"])) for b in bars[1:]]
    avg_hist_range = sum(ranges) / len(ranges) if ranges else max(0.5, atr_val)
    vol_scale = max(0.5, min(2.0, atr_val / max(0.1, avg_hist_range)))

    pool: List[Dict[str, float]] = []
    for i in range(1, len(bars)):
        prev_c = float(bars[i - 1]["c"])
        h = float(bars[i]["h"])
        l = float(bars[i]["l"])
        c = float(bars[i]["c"])

        dc = (c - prev_c) * vol_scale
        hi_ext = max(0.0, h - max(prev_c, c)) * vol_scale
        lo_ext = max(0.0, min(prev_c, c) - l) * vol_scale
        pool.append({"dc": dc, "hi_ext": hi_ext, "lo_ext": lo_ext})

    return pool


def run_simulation(
    bars: List[Dict[str, Any]],
    trade_setup: Dict[str, Any],
    structure_res: Dict[str, Any],
    replays: int = 35000,
    bars_ahead: int = 36,
    seed: int = 19,
) -> Dict[str, Any]:
    """Pure function: run N Monte Carlo paths (default 35,000) for a proposed XAU/USD trade setup."""
    if not bars:
        raise ValueError("Cannot run simulation on empty bars")

    replays = max(100, min(100000, int(replays)))
    bars_ahead = max(6, min(144, int(bars_ahead)))
    band_sample_limit = min(replays, 2500)

    side = str(trade_setup["side"]).upper()
    is_buy = side == "BUY"
    entry = float(trade_setup["entry"])
    stop = float(trade_setup["stop"])
    target = float(trade_setup["target"])
    invalidation = float(
        trade_setup.get("scenario_c_invalidation")
        or (stop - 1.5 if is_buy else stop + 1.5)
    )
    sweep_reentry = float(
        trade_setup.get("scenario_b_sweep_reentry")
        or (stop - 0.8 if is_buy else stop + 0.8)
    )

    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        raise ValueError(f"Invalid stop distance: entry={entry}, stop={stop}")
    r_reward = round(reward / risk, 3)

    atr_val = float(structure_res.get("atr14") or 2.0)
    trend = structure_res.get("trend", {})
    slope20 = float(trend.get("slope_ema20_atr") or 0.0)
    slope50 = float(trend.get("slope_ema50_atr") or 0.0)

    pool = build_bar_pool(bars[-180:], atr_val)
    mean_pool_dc = sum(x["dc"] for x in pool) / len(pool)
    recent_pool = pool[-30:] if len(pool) >= 30 else pool
    recent_mean_dc = sum(x["dc"] for x in recent_pool) / len(recent_pool)

    # Purely empirical market drift from observed EMA slopes & recent 30-bar returns (NEVER biased by trade side or confluence score)
    ema_slope_drift = (slope20 * 0.55 + slope50 * 0.45) * 0.06 * atr_val
    raw_drift = (ema_slope_drift * 0.65) + (recent_mean_dc * 0.35)
    drift_per_bar = max(-atr_val * 0.18, min(atr_val * 0.18, raw_drift))

    rng = random.Random(seed)
    choice = rng.choice

    tp_hits = 0
    sl_hits = 0
    neither_hits = 0
    sweep_then_tp_hits = 0
    deep_invalidation_hits = 0
    r_outcomes: List[float] = []
    terminal_closes: List[float] = []
    all_paths: List[List[float]] = []

    for path_idx in range(replays):
        p = entry
        keep_path = path_idx < band_sample_limit
        path = [round(p, 2)] if keep_path else []
        first_outcome: Optional[str] = None
        swept_stop_without_invalidation = False
        recovered_to_tp_after_sweep = False
        hit_invalidation = False

        for _step in range(bars_ahead):
            sample = choice(pool)
            # Zero-center historical step and apply current EMA trend drift
            step_dc = (sample["dc"] - mean_pool_dc) + drift_per_bar
            next_c = p + step_dc
            hi = max(p, next_c) + sample["hi_ext"]
            lo = min(p, next_c) - sample["lo_ext"]

            if is_buy:
                if lo <= invalidation:
                    hit_invalidation = True
                # Conservative check: Stop checked before Target on the same bar
                if first_outcome is None:
                    if lo <= stop:
                        first_outcome = "STOP"
                        if lo > invalidation:
                            swept_stop_without_invalidation = True
                    elif hi >= target:
                        first_outcome = "TARGET"
                elif first_outcome == "STOP" and swept_stop_without_invalidation and not hit_invalidation:
                    if hi >= target:
                        recovered_to_tp_after_sweep = True
            else:
                if hi >= invalidation:
                    hit_invalidation = True
                if first_outcome is None:
                    if hi >= stop:
                        first_outcome = "STOP"
                        if hi < invalidation:
                            swept_stop_without_invalidation = True
                    elif lo <= target:
                        first_outcome = "TARGET"
                elif first_outcome == "STOP" and swept_stop_without_invalidation and not hit_invalidation:
                    if lo <= target:
                        recovered_to_tp_after_sweep = True

            p = next_c
            if keep_path:
                path.append(round(p, 2))

        if keep_path:
            all_paths.append(path)
        terminal_closes.append(p)

        if hit_invalidation:
            deep_invalidation_hits += 1

        if first_outcome == "TARGET":
            tp_hits += 1
            r_outcomes.append(r_reward)
        elif first_outcome == "STOP":
            sl_hits += 1
            r_outcomes.append(-1.0)
            if recovered_to_tp_after_sweep:
                sweep_then_tp_hits += 1
        else:
            neither_hits += 1
            open_r = ((p - entry) / risk) if is_buy else ((entry - p) / risk)
            r_outcomes.append(round(open_r, 4))

    p_tp = round(tp_hits / replays, 4)
    p_sl = round(sl_hits / replays, 4)
    p_neither = round(neither_hits / replays, 4)
    p_sweep = round(sweep_then_tp_hits / replays, 4)
    p_invalidation = round(deep_invalidation_hits / replays, 4)

    # Expected Value (EV) on resolved trades & full paths
    ev_resolved = round((p_tp * r_reward) - (p_sl * 1.0), 3)
    avg_r_total = round(statistics.mean(r_outcomes), 3)
    break_even_win_rate = round(1.0 / (1.0 + r_reward), 4)
    resolved_total = tp_hits + sl_hits
    resolved_win_share = round(tp_hits / resolved_total, 4) if resolved_total > 0 else 0.0

    # Compute Fan Chart percentile bands at each future bar step
    tf_sec = TIMEFRAME_SECONDS.get(str(structure_res.get("timeframe", "M5")).upper(), 300)
    last_t = int(structure_res["last_bar_time"])
    fan_bands: List[Dict[str, Any]] = []
    for step_idx in range(bars_ahead + 1):
        col = sorted(path[step_idx] for path in all_paths)
        fan_bands.append(
            {
                "step": step_idx,
                "time": last_t + step_idx * tf_sec,
                "p5": r2(_pct(col, 0.05)),
                "p25": r2(_pct(col, 0.25)),
                "p50": r2(_pct(col, 0.50)),
                "p75": r2(_pct(col, 0.75)),
                "p95": r2(_pct(col, 0.95)),
            }
        )

    sorted_closes = sorted(terminal_closes)
    verdict = _build_verdict(resolved_win_share, break_even_win_rate, ev_resolved, p_sweep)
    scenarios = _build_three_scenarios(
        trade_setup=trade_setup,
        p_tp=p_tp,
        p_sl=p_sl,
        p_sweep=p_sweep,
        p_invalidation=p_invalidation,
        ev_r=ev_resolved,
        sweep_reentry=sweep_reentry,
        invalidation=invalidation,
    )

    return {
        "created_by": "xau-simulator",
        "replays": replays,
        "bars_ahead": bars_ahead,
        "seed": seed,
        "drift_per_bar_usd": r2(drift_per_bar),
        "probabilities": {
            "p_win": p_tp,
            "p_stop": p_sl,
            "p_neither": p_neither,
            "p_sweep": p_sweep,
            "p_invalidation": p_invalidation,
            "win_pct": round(p_tp * 100, 1),
            "stop_pct": round(p_sl * 100, 1),
            "neither_pct": round(p_neither * 100, 1),
            "sweep_risk_pct": round(p_sweep * 100, 1),
            "resolved_win_pct": round(resolved_win_share * 100, 1),
            "break_even_win_pct": round(break_even_win_rate * 100, 1),
        },
        "expected_value_r": ev_resolved,
        "avg_r_all_paths": avg_r_total,
        "terminal_price": {
            "median": r2(_pct(sorted_closes, 0.50)),
            "p5": r2(_pct(sorted_closes, 0.05)),
            "p95": r2(_pct(sorted_closes, 0.95)),
        },
        "verdict": verdict,
        "scenarios": scenarios,
        "fan_chart": fan_bands,
        "sample_paths": all_paths[:25],
    }


def _build_verdict(
    resolved_win_share: float,
    break_even_win_rate: float,
    ev_r: float,
    p_sweep: float,
) -> str:
    edge = resolved_win_share - break_even_win_rate
    if ev_r >= 0.25 and edge >= 0.08:
        base = f"POSITIVE EDGE (EV {ev_r:+.2f}R): โอกาสชนะเหนือกว่าระดับคุ้มทุนชัดเจน"
    elif ev_r > 0.0:
        base = f"SLIGHT EDGE (EV {ev_r:+.2f}R): ได้เปรียบทางสถิติเล็กน้อย ต้องคุมจุดเข้าให้แม่นยำ"
    else:
        base = f"NEGATIVE / COIN-FLIP (EV {ev_r:+.2f}R): ความได้เปรียบต่ำกว่าเกณฑ์คุ้มทุน"

    if p_sweep >= 0.20:
        base += f" | ระวัง Sweep Risk สูง ({p_sweep * 100:.1f}%)"
    return base


def _build_three_scenarios(
    trade_setup: Dict[str, Any],
    p_tp: float,
    p_sl: float,
    p_sweep: float,
    p_invalidation: float,
    ev_r: float,
    sweep_reentry: float,
    invalidation: float,
) -> Dict[str, Dict[str, Any]]:
    side = trade_setup["side"]
    entry = trade_setup["entry"]
    stop = trade_setup["stop"]
    target = trade_setup["target"]
    z_low = trade_setup["zone_low"]
    z_high = trade_setup["zone_high"]

    return {
        "scenario_a_primary": {
            "title": f"Scenario A (แผนหลัก - {side} ตามโซน Confluence)",
            "probability_pct": round(p_tp * 100, 1),
            "trigger_zone": f"{z_low:.2f} - {z_high:.2f}",
            "entry": entry,
            "stop": stop,
            "target": target,
            "action": (
                f"รอราคาทดสอบโซน {z_low:.2f}-{z_high:.2f} แล้วเกิดแท่งเทียนปฏิเสธราคา (Rejection / CHoCH ย่อย) "
                f"ค่อยกด {side} ใน MT5 ตั้ง SL ที่ {stop:.2f} และ TP ที่ {target:.2f} (EV {ev_r:+.2f}R)"
            ),
        },
        "scenario_b_sweep_trap": {
            "title": "Scenario B (แผนรับมือ Liquidity Sweep / กวาดหางก่อนวิ่งจริง)",
            "probability_pct": round(p_sweep * 100, 1),
            "sweep_level": stop,
            "secondary_entry_zone": sweep_reentry,
            "action": (
                f"โอกาสโดนกวาด SL แล้ววิ่งกลับไป TP อยู่ที่ {p_sweep * 100:.1f}% — หากรอบแรกเกิดไส้เทียนทะลุ {stop:.2f} "
                f"ไปทดสอบโซนถัดไปแถว {sweep_reentry:.2f} แล้วดึงกลับเร็ว ให้รอจังหวะเข้าไม้สองเมื่อปิดแท่งกลับเข้าโซนเท่านั้น"
            ),
        },
        "scenario_c_invalidation": {
            "title": "Scenario C (จุดยกเลิกแผนทันที / Structural Invalidation)",
            "probability_pct": round(p_invalidation * 100, 1),
            "invalidation_level": invalidation,
            "action": (
                f"หากแท่งเทียนปิดเต็มแท่ง (Body Close) ทะลุระดับ {invalidation:.2f} แปลว่าโครงสร้าง {trade_setup['bias']} พังสมบูรณ์ "
                f"ให้ยกเลิกแผน {side} ทันที ห้ามเปิดไม้ถัวเฉลี่ยเด็ดขาด"
            ),
        },
    }


def optimize_setup_for_win_prob(
    bars: List[Dict[str, Any]],
    trade_setup: Dict[str, Any],
    structure_res: Dict[str, Any],
    replays: int = 15000,
    bars_ahead: int = 36,
    seed: int = 19,
    min_win_prob_pct: float = 50.0,
) -> tuple:
    """Evaluate the setup using 100% unmanipulated empirical Monte Carlo statistics (no artificial target shrinking).
    Only if a deeper limit entry inside the fresh zone (keeping the same structural SL and TP) genuinely achieves >50% win probability
    is it selected; otherwise returns WATCHING_FOR_50PCT_EDGE so the user is never rushed into a low-probability trade."""
    is_scalp = str(trade_setup.get("trading_style") or "intraday").lower() == "scalping"
    eff_bars_ahead = 18 if is_scalp else bars_ahead
    base_sim = run_simulation(
        bars=bars,
        trade_setup=trade_setup,
        structure_res=structure_res,
        replays=replays,
        bars_ahead=eff_bars_ahead,
        seed=seed,
    )

    side = str(trade_setup["side"]).upper()
    is_buy = side == "BUY"
    entry = float(trade_setup["entry"])
    stop = float(trade_setup["stop"])
    target = float(trade_setup["target"])
    atr_val = float(structure_res.get("atr14") or 2.0)
    current_price = float(structure_res.get("current_price") or entry)

    best_setup = trade_setup
    best_sim = base_sim

    # Check if waiting for a deeper Pending Limit entry at the 50% equilibrium of the fresh zone
    # (keeping the exact same structural SL & TP) improves true statistical win rate
    if best_sim["probabilities"]["win_pct"] <= min_win_prob_pct:
        z_low = float(trade_setup.get("zone_low") or (entry - atr_val * 0.4))
        z_high = float(trade_setup.get("zone_high") or (entry + atr_val * 0.4))
        opt_entry = r2((z_low + z_high) / 2.0)
        if (is_buy and stop < opt_entry < entry and opt_entry <= current_price) or (
            not is_buy and entry < opt_entry < stop and opt_entry >= current_price
        ):
            cand = dict(trade_setup)
            opt_risk = max(0.4, abs(opt_entry - stop))
            opt_reward = abs(target - opt_entry)
            cand["entry"] = opt_entry
            cand["distance_to_entry_usd"] = r2(abs(current_price - opt_entry))
            cand["risk_usd"] = r2(opt_risk)
            cand["reward_usd"] = r2(opt_reward)
            cand["tp1"] = r2(opt_entry + opt_risk * (0.85 if is_scalp else 1.0)) if is_buy else r2(opt_entry - opt_risk * (0.85 if is_scalp else 1.0))
            cand["reward_risk"] = round(opt_reward / opt_risk, 2)
            cand["sl_atr_ratio"] = round(opt_risk / max(0.1, atr_val), 2)
            full_sim = run_simulation(
                bars=bars,
                trade_setup=cand,
                structure_res=structure_res,
                replays=replays,
                bars_ahead=eff_bars_ahead,
                seed=seed,
            )
            if full_sim["probabilities"]["win_pct"] > best_sim["probabilities"]["win_pct"]:
                best_setup = cand
                best_sim = full_sim

    # Compute Next Advance Watch Zone when current win probability is still <= 50%
    watch_entry = float(
        best_setup.get("entry")
        if (is_buy and float(best_setup.get("entry", entry)) < current_price)
        or (not is_buy and float(best_setup.get("entry", entry)) > current_price)
        else (best_setup.get("scenario_b_sweep_reentry") or (stop - atr_val * 0.5 if is_buy else stop + atr_val * 0.5))
    )
    watch_stop = r2(watch_entry - atr_val * 0.9) if is_buy else r2(watch_entry + atr_val * 0.9)
    watch_target = r2(watch_entry + atr_val * 1.45) if is_buy else r2(watch_entry - atr_val * 1.45)

    if best_sim["probabilities"]["win_pct"] > min_win_prob_pct and int(best_setup.get("confluence_score", 0)) >= 70:
        best_sim["scanner_status"] = "SIGNAL_READY"
        best_sim["next_watch_zone"] = None
    else:
        best_sim["scanner_status"] = "WATCHING_FOR_50PCT_EDGE"
        best_sim["next_watch_zone"] = {
            "side": side,
            "order_type": f"{side} LIMIT",
            "watch_entry": r2(watch_entry),
            "watch_stop": watch_stop,
            "watch_target": watch_target,
            "current_win_pct": best_sim["probabilities"]["win_pct"],
            "required_win_pct": min_win_prob_pct,
            "reason_th": (
                f"สถิติจริงจาก 15,000 เส้นทางพบโอกาสชน TP เพียง {best_sim['probabilities']['win_pct']:.1f}% (ยังไม่เกิน {min_win_prob_pct:.0f}%) "
                f"— ห้ามรีบเปิดออเดอร์ ให้รอดัก {side} LIMIT ล่วงหน้าที่โซน {watch_entry:.2f} และรอความได้เปรียบทางสถิติเกิน 50% ก่อน"
            ),
        }

    return best_setup, best_sim


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run XAUUSD Monte Carlo Chart Simulation.")
    ap.add_argument("--offline", required=True, help="Path to JSON file containing OHLC bars")
    ap.add_argument("--replays", type=int, default=1000)
    ap.add_argument("--bars-ahead", type=int, default=36)
    ap.add_argument("--seed", type=int, default=19)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    with open(args.offline, encoding="utf-8") as fh:
        doc = json.load(fh)
    bars = doc["bars"] if isinstance(doc, dict) and "bars" in doc else doc
    st = analyze_structure(bars)
    smc = analyze_smc(bars, structure_res=st)
    sim = run_simulation(
        bars=bars,
        trade_setup=smc["trade_setup"],
        structure_res=st,
        replays=args.replays,
        bars_ahead=args.bars_ahead,
        seed=args.seed,
    )
    if args.json:
        out = dict(sim)
        out.pop("sample_paths", None)
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        p = sim["probabilities"]
        print(
            f"Monte Carlo ({sim['replays']} paths, {sim['bars_ahead']} bars ahead, seed={sim['seed']}):"
        )
        print(
            f"  Win(TP): {p['win_pct']}% | Stop(SL): {p['stop_pct']}% | Sweep Risk: {p['sweep_risk_pct']}% | EV: {sim['expected_value_r']:+.2f}R"
        )
        print(f"  Verdict: {sim['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
