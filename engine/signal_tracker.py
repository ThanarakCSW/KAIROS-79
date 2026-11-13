#!/usr/bin/env python3
"""Signal Performance Tracker & Walk-Forward Verification Engine (signal_tracker.py).

Tracks live AI/SMC trade signals and performs walk-forward historical verification
on actual OHLCV bars to grade outcomes:
  - WIN_TP2: Full target reached (+R:R)
  - WIN_TP1_BE: Reached TP1 (1.0R), closed 50% + moved SL to Break-Even (+0.50R)
  - LOSS_SWEEP: Hit Stop Loss via wick sweep then reversed back across Entry (-1.00R)
  - LOSS_SL: Hit Stop Loss on structural continuation (-1.00R)
  - OPEN: Currently active or waiting in zone
"""

import datetime
import math
from typing import Any, Dict, List, Optional

from engine.smc import analyze_smc
from engine.structure import analyze_structure

BKK_TZ = datetime.timezone(datetime.timedelta(hours=7))


def r2(val: Optional[float]) -> float:
    if val is None or math.isnan(val) or math.isinf(val):
        return 0.0
    return round(float(val), 2) + 0.0


def _format_bkk_ts(ts: int) -> str:
    dt = datetime.datetime.fromtimestamp(int(ts), tz=BKK_TZ)
    return dt.strftime("%d/%m %H:%M")


def grade_signal_on_forward_bars(
    side: str,
    entry: float,
    stop: float,
    tp1: float,
    target: float,
    reward_risk: float,
    forward_bars: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Trace forward OHLC bars to grade a signal with TP1 Partial Close + Break-Even rules."""
    if not forward_bars:
        return {
            "outcome": "OPEN",
            "outcome_label_th": "⏳ กำลังวิ่งอยู่ในตลาด (OPEN)",
            "pnl_r": 0.0,
        }

    triggered = False
    reached_tp1 = False
    sl_hit_idx: Optional[int] = None

    for idx, b in enumerate(forward_bars):
        hi = float(b["h"])
        lo = float(b["l"])

        if not triggered:
            if lo <= entry <= hi:
                triggered = True
            else:
                continue

        if side == "BUY":
            # Check SL vs TP on this bar
            if not reached_tp1 and lo <= stop:
                sl_hit_idx = idx
                break
            if reached_tp1 and lo <= entry:
                # Stopped at Break-Even after banking 50% at TP1 (1.0R)
                return {
                    "outcome": "WIN_TP1_BE",
                    "outcome_label_th": "🟢 ชนะ TP1 + บังทุน (+0.50R)",
                    "pnl_r": 0.50,
                }
            if hi >= target:
                # 50% at TP1 (1.0R) + 50% at TP2 (reward_risk R)
                net_r = round(0.5 * 1.0 + 0.5 * reward_risk, 2)
                return {
                    "outcome": "WIN_TP2",
                    "outcome_label_th": f"🏆 ชนะแตะเป้า TP2 (+{net_r:.2f}R)",
                    "pnl_r": net_r,
                }
            if hi >= tp1:
                reached_tp1 = True
        else:
            if not reached_tp1 and hi >= stop:
                sl_hit_idx = idx
                break
            if reached_tp1 and hi >= entry:
                return {
                    "outcome": "WIN_TP1_BE",
                    "outcome_label_th": "🟢 ชนะ TP1 + บังทุน (+0.50R)",
                    "pnl_r": 0.50,
                }
            if lo <= target:
                net_r = round(0.5 * 1.0 + 0.5 * reward_risk, 2)
                return {
                    "outcome": "WIN_TP2",
                    "outcome_label_th": f"🏆 ชนะแตะเป้า TP2 (+{net_r:.2f}R)",
                    "pnl_r": net_r,
                }
            if lo <= tp1:
                reached_tp1 = True

    if sl_hit_idx is not None:
        # Check if price reversed back past entry within the next 8 bars (Liquidity Sweep)
        after_sl = forward_bars[sl_hit_idx + 1 : sl_hit_idx + 9]
        swept_and_reversed = False
        for b in after_sl:
            if side == "BUY" and float(b["c"]) > entry:
                swept_and_reversed = True
                break
            if side == "SELL" and float(b["c"]) < entry:
                swept_and_reversed = True
                break
        if swept_and_reversed:
            return {
                "outcome": "LOSS_SWEEP",
                "outcome_label_th": "🟡 โดนไส้กวาด SL ก่อนกลับตัว (-1.00R)",
                "pnl_r": -1.0,
            }
        return {
            "outcome": "LOSS_SL",
            "outcome_label_th": "🔴 แพ้ชน SL (-1.00R)",
            "pnl_r": -1.0,
        }

    if reached_tp1:
        return {
            "outcome": "WIN_TP1_BE",
            "outcome_label_th": "🟢 แตะ TP1 แล้ว (ล็อกกำไร +0.50R)",
            "pnl_r": 0.50,
        }

    if not triggered:
        return {
            "outcome": "UNFILLED",
            "outcome_label_th": "⚪ ตั้งดักล่วงหน้าแต่ราคาไม่ย่อมาเกี่ยว (0.00R)",
            "pnl_r": 0.0,
        }

    return {
        "outcome": "OPEN",
        "outcome_label_th": "⏳ กำลังถือครอง (OPEN)",
        "pnl_r": 0.0,
    }


def evaluate_signal_performance(
    bars: List[Dict[str, Any]],
    timeframe: str,
    current_ticket: Dict[str, Any],
    current_smc: Dict[str, Any],
    spread_usd: float = 0.25,
) -> Dict[str, Any]:
    """Run walk-forward verification across historical checkpoints + current signal to report real win-rate & R stats."""
    n = len(bars)
    signals: List[Dict[str, Any]] = []

    # Generate walk-forward historical checkpoints (every ~32 bars from bar 75 up to n - 18)
    if n >= 95:
        step = max(22, (n - 85) // 5)
        checkpoints = list(range(70, n - 16, step))[-5:]
    else:
        checkpoints = [max(25, n // 2)] if n >= 40 else []

    for cp in checkpoints:
        sub_bars = bars[:cp]
        forward_bars = bars[cp : min(n, cp + 36)]
        try:
            st_cp = analyze_structure(sub_bars, timeframe=timeframe)
            smc_cp = analyze_smc(sub_bars, timeframe=timeframe, spread_usd=spread_usd, structure_res=st_cp)
            setup = smc_cp["trade_setup"]
            side = setup["side"]
            entry = float(setup["entry"])
            stop = float(setup["stop"])
            tp1 = float(setup.get("tp1") or (entry + abs(entry - stop) if side == "BUY" else entry - abs(entry - stop)))
            target = float(setup["target"])
            rr = float(setup["reward_risk"])

            grade = grade_signal_on_forward_bars(
                side=side,
                entry=entry,
                stop=stop,
                tp1=tp1,
                target=target,
                reward_risk=rr,
                forward_bars=forward_bars,
            )
            signals.append({
                "bar_time": int(sub_bars[-1]["t"]),
                "time_bkk": _format_bkk_ts(int(sub_bars[-1]["t"])),
                "timeframe": timeframe,
                "side": side,
                "entry": r2(entry),
                "stop": r2(stop),
                "tp1": r2(tp1),
                "target": r2(target),
                "reward_risk": rr,
                "confluence_score": int(setup["confluence_score"]),
                "outcome": grade["outcome"],
                "outcome_label_th": grade["outcome_label_th"],
                "pnl_r": grade["pnl_r"],
            })
        except Exception:
            continue

    # Append current live signal at the top
    curr_setup = current_smc.get("trade_setup") or {}
    if current_ticket and bars:
        win_passed = current_ticket.get("win_prob_passed", True)
        is_market = (
            current_ticket.get("execution_type") == "MARKET"
            or "NOW" in str(current_ticket.get("order_type") or "")
        )
        side = current_ticket.get("side", "BUY")
        verdict = current_ticket.get("verdict", "PASS")
        if not win_passed:
            status_lbl = "⏳ รอจังหวะ (โอกาสชนะตามสถิติจริงยังไม่ถึง 50%)"
        elif is_market:
            status_lbl = f"⚡ เข้าทันที: {side} NOW ({verdict})"
        else:
            status_lbl = f"⏳ ดักเข้าล่วงหน้า: {side} LIMIT ({verdict})"
        signals.append({
            "bar_time": int(bars[-1]["t"]),
            "time_bkk": _format_bkk_ts(int(bars[-1]["t"])) + " (แผนปัจจุบัน)",
            "timeframe": timeframe,
            "side": current_ticket.get("side", "BUY"),
            "entry": r2(current_ticket.get("entry_mt5")),
            "stop": r2(current_ticket.get("stop_mt5")),
            "tp1": r2(current_ticket.get("tp1_mt5")),
            "target": r2(current_ticket.get("target_mt5")),
            "reward_risk": float(current_ticket.get("reward_risk") or 2.0),
            "confluence_score": int(curr_setup.get("confluence_score") or 70),
            "outcome": "OPEN",
            "outcome_label_th": status_lbl,
            "pnl_r": 0.0,
        })

    closed = [s for s in signals if s["outcome"] not in ("OPEN", "UNFILLED")]
    wins = [s for s in closed if s["outcome"] in ("WIN_TP2", "WIN_TP1_BE")]
    losses = [s for s in closed if s["outcome"] in ("LOSS_SL", "LOSS_SWEEP")]
    sweeps = [s for s in closed if s["outcome"] == "LOSS_SWEEP"]

    closed_cnt = len(closed)
    win_cnt = len(wins)
    loss_cnt = len(losses)
    win_rate_pct = round((win_cnt / closed_cnt) * 100.0, 1) if closed_cnt > 0 else 0.0
    cum_r = round(sum(float(s["pnl_r"]) for s in closed), 2)
    gross_win = sum(float(s["pnl_r"]) for s in wins)
    gross_loss = abs(sum(float(s["pnl_r"]) for s in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 1.0)

    return {
        "timeframe": timeframe,
        "total_signals": len(signals),
        "closed_count": closed_cnt,
        "open_count": len(signals) - closed_cnt,
        "win_count": win_cnt,
        "loss_count": loss_cnt,
        "sweep_count": len(sweeps),
        "win_rate_pct": win_rate_pct,
        "cumulative_r": cum_r,
        "profit_factor": profit_factor,
        "recent_signals": list(reversed(signals)),
    }
