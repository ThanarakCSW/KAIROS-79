#!/usr/bin/env python3
"""Deterministic Position Sizing & Risk Gate Engine (risk_gate.py).

Evolved from day-trader-skill's size.py and check.py for XAU/USD on MT5:
  - Applies Broker Price Offset ($) and Broker Spread (Points) calibration
  - Calculates exact Gold Lot Size (0.01 step) for both BUY and SELL setups:
      Lot Size = (Account Equity * Risk %) / (SL Distance ($) * Contract Size)
  - Enforces hard Risk Gate rules (PASS / CAUTION / REJECT):
      * REJECT if R:R < 1.5
      * REJECT if Expected Value (EV) <= 0
      * REJECT if inside +-30 min USD Red-Folder News Blackout
      * REJECT if Lot Size < 0.01 (SL too wide for account risk budget)
      * CAUTION if SL < 0.8 * ATR(14) (too tight for Gold)
      * CAUTION if Sweep Risk (P_sweep) > max_sweep_risk_pct
      * CAUTION if Spread > 25% of SL distance or inside Daily Rollover gap
"""

import hashlib
import json
import math
from typing import Any, Dict, List, Optional

from engine.structure import r2


def floor_lot(raw_lot: float, lot_step: float = 0.01, max_lot: float = 10.0) -> float:
    if raw_lot <= 0 or lot_step <= 0:
        return 0.0
    steps = math.floor((raw_lot + 1e-9) / lot_step)
    sized = round(steps * lot_step, 2)
    return min(max_lot, max(0.0, sized))


def order_fingerprint(ticket: Dict[str, Any]) -> str:
    core = {
        "symbol": ticket.get("symbol"),
        "side": ticket.get("side"),
        "entry_mt5": ticket.get("entry_mt5"),
        "stop_mt5": ticket.get("stop_mt5"),
        "target_mt5": ticket.get("target_mt5"),
        "lot_size": ticket.get("lot_size"),
    }
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def evaluate_risk_and_size(
    trade_setup: Dict[str, Any],
    simulation: Dict[str, Any],
    news_status: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Pure function: size XAU/USD position and run deterministic Risk Gate checks."""
    balance = float(config.get("account_balance") or 5000.0)
    risk_pct = float(config.get("risk_per_trade_pct") or 1.0)
    contract_size = float(config.get("contract_size") or 100.0)
    min_lot = float(config.get("min_lot") or 0.01)
    max_lot = float(config.get("max_lot") or 10.0)
    lot_step = float(config.get("lot_step") or 0.01)

    trading_style = str(trade_setup.get("trading_style") or config.get("trading_style") or "intraday").lower()
    is_scalp = trading_style == "scalping"
    min_rr = float(config.get("min_reward_risk") or 1.5)
    if is_scalp:
        min_rr = min(min_rr, 1.15)
    min_sl_atr = float(config.get("min_sl_atr_mult") or 0.8)
    if is_scalp:
        min_sl_atr = min(min_sl_atr, 0.65)
    max_sweep_pct = float(config.get("max_sweep_risk_pct") or 20.0)
    min_win_prob = float(config.get("min_win_prob_pct") or 0.0)

    broker_offset = float(config.get("broker_price_offset") or 0.0)
    spread_points = float(config.get("broker_spread_points") or 25.0)
    point_size = float(config.get("point_size") or 0.01)
    spread_usd = round(spread_points * point_size, 2)

    side = str(trade_setup["side"]).upper()
    entry_raw = float(trade_setup["entry"])
    stop_raw = float(trade_setup["stop"])
    target_raw = float(trade_setup["target"])

    # Apply Broker Offset so levels match the user's MT5 chart directly
    entry_mt5 = r2(entry_raw + broker_offset)
    stop_mt5 = r2(stop_raw + broker_offset)
    target_mt5 = r2(target_raw + broker_offset)
    scenario_b_mt5 = r2(float(trade_setup.get("scenario_b_sweep_reentry") or stop_raw) + broker_offset)
    scenario_c_mt5 = r2(float(trade_setup.get("scenario_c_invalidation") or stop_raw) + broker_offset)

    sl_distance_usd = max(0.01, abs(entry_mt5 - stop_mt5))
    tp_distance_usd = max(0.01, abs(target_mt5 - entry_mt5))
    rr = round(tp_distance_usd / sl_distance_usd, 2)
    sl_atr_ratio = float(trade_setup.get("sl_atr_ratio") or 1.0)

    tp1_raw = trade_setup.get("tp1")
    if tp1_raw is not None:
        tp1_mt5 = r2(float(tp1_raw) + broker_offset)
    else:
        tp1_mt5 = r2(entry_mt5 + sl_distance_usd if side == "BUY" else entry_mt5 - sl_distance_usd)
    breakeven_sl_mt5 = r2(entry_mt5 + 0.15 if side == "BUY" else entry_mt5 - 0.15)

    risk_budget_usd = round(balance * (risk_pct / 100.0), 2)
    loss_per_1lot = sl_distance_usd * contract_size
    raw_lot = risk_budget_usd / loss_per_1lot if loss_per_1lot > 0 else 0.0
    lot_size = floor_lot(raw_lot, lot_step=lot_step, max_lot=max_lot)

    actual_risk_usd = round(lot_size * sl_distance_usd * contract_size, 2)
    potential_profit_usd = round(lot_size * tp_distance_usd * contract_size, 2)
    tp1_partial_profit_usd = round((lot_size * 0.5) * abs(tp1_mt5 - entry_mt5) * contract_size, 2)
    spread_cost_usd = round(lot_size * spread_usd * contract_size, 2)
    spread_to_sl_pct = round((spread_usd / sl_distance_usd) * 100.0, 1)

    probs = simulation.get("probabilities", {})
    ev_r = float(simulation.get("expected_value_r") or 0.0)
    win_pct = float(probs.get("win_pct") or 0.0)
    sweep_pct = float(probs.get("sweep_risk_pct") or 0.0)

    reject_reasons: List[str] = []
    caution_warnings: List[str] = []
    passed_checks: List[str] = []

    # 0. Strict >50% TP Win Probability & Advance Entry check (when configured)
    win_prob_passed = True
    if min_win_prob > 0.0:
        if win_pct <= min_win_prob:
            win_prob_passed = False
            reject_reasons.append(
                f"สถิติจริงจาก 15,000 เส้นทางพบโอกาสถึง TP เพียง {win_pct:.1f}% (ยังไม่เกิน {min_win_prob:.0f}%) — ห้ามรีบเปิดออเดอร์ ให้รอดักโซนล่วงหน้า"
            )
        else:
            passed_checks.append(
                f"โอกาสถึง TP จากสถิติจริง {win_pct:.1f}% (ผ่านเกณฑ์ > {min_win_prob:.0f}%)"
            )

    is_market_entry = (
        trade_setup.get("execution_type") == "MARKET"
        or "NOW" in str(trade_setup.get("order_type") or "")
    )
    if is_market_entry:
        passed_checks.append(
            f"สัญญาณเข้าทันทีที่ราคาปัจจุบัน ({side} NOW @ {entry_mt5:.2f}) ตามเงื่อนไขยืนยันในโซน"
        )
    elif trade_setup.get("is_advance_entry") is False:
        win_prob_passed = False
        reject_reasons.append(
            "ราคาวิ่งผ่านจุดเข้าไปแล้ว — ระบบจะแจ้งเฉพาะจุดดักเข้าล่วงหน้า (Pending Limit) เท่านั้น"
        )
    else:
        dist_entry = float(trade_setup.get("distance_to_entry_usd") or 0.0)
        passed_checks.append(
            f"จุดดักเข้าล่วงหน้า ({side} LIMIT @ {entry_mt5:.2f}) ห่างจากราคาปัจจุบัน ${dist_entry:.2f} (ยังไม่ผ่านจุดเข้า)"
        )

    # 1. R:R check
    if rr < min_rr:
        reject_reasons.append(
            f"R:R ต่ำกว่าเกณฑ์ขั้นต่ำ ({rr:.2f} < {min_rr:.2f})"
        )
    else:
        passed_checks.append(f"อัตราส่วนคุ้มค่า R:R = 1:{rr:.2f} (ผ่านเกณฑ์ >= {min_rr})")

    # 2. Expected Value check
    if ev_r <= 0.0:
        reject_reasons.append(
            f"ค่าคาดหวังทางสถิติติดลบหรือเท่ากับศูนย์ (EV = {ev_r:+.2f}R)"
        )
    else:
        passed_checks.append(f"ค่าคาดหวังเป็นบวก EV = {ev_r:+.2f}R (โอกาสชนะ {win_pct:.1f}%)")

    # 3. Lot size viability check
    if lot_size < min_lot:
        reject_reasons.append(
            f"งบความเสี่ยง ${risk_budget_usd:.2f} ไม่พอสำหรับระยะ SL ${sl_distance_usd:.2f} (ขั้นต่ำ 0.01 Lot ใช้ความเสี่ยง ${0.01 * loss_per_1lot:.2f})"
        )
    else:
        passed_checks.append(
            f"ขนาดไม้ {lot_size:.2f} Lot คุมความเสี่ยงที่ ${actual_risk_usd:.2f} ({actual_risk_usd / balance * 100:.2f}% ของพอร์ต)"
        )

    # 4. News blackout check (+- 30 mins of USD Red News)
    if news_status.get("in_news_blackout"):
        titles = ", ".join(e["title"] for e in news_status.get("blackout_events", []))
        reject_reasons.append(
            f"ติดช่วงประกาศข่าวกล่องแดง USD ภายใน ±{news_status.get('blackout_minutes', 30)} นาที ({titles})"
        )
    else:
        passed_checks.append("ปลอดข่าวกล่องแดง USD ในระยะ ±30 นาที")

    # 5. SL vs ATR(14) check
    if sl_atr_ratio < min_sl_atr:
        caution_warnings.append(
            f"ระยะ SL แคบเกินไปสำหรับทองคำ ({sl_atr_ratio:.2f}x ATR < {min_sl_atr:.2f}x ATR) เสี่ยงโดนสะบัดหาง"
        )
    else:
        passed_checks.append(f"ระยะ SL กว้างเหมาะสม ({sl_atr_ratio:.2f}x ATR14)")

    # 6. Sweep Risk check
    if sweep_pct > max_sweep_pct:
        caution_warnings.append(
            f"โอกาสโดนกวาด SL แล้ววิ่งกลับ (Sweep Risk) สูงถึง {sweep_pct:.1f}% แนะนำพิจารณาขยับ SL หรือรอรับที่โซน {scenario_b_mt5:.2f}"
        )

    # 7. Spread vs SL check & Rollover danger
    if spread_to_sl_pct > 25.0:
        caution_warnings.append(
            f"ค่า Spread (${spread_usd:.2f}) คิดเป็น {spread_to_sl_pct:.1f}% ของระยะ SL ซึ่งสูงเกินไป"
        )
    session_info = news_status.get("session", {})
    if session_info.get("is_rollover_danger"):
        caution_warnings.append(
            "อยู่ในช่วงตลาดเปลี่ยนวัน (Rollover Gap) โบรกเกอร์มักถ่าง Spread สูง ไม่ควรเข้าออเดอร์ใหม่"
        )

    if reject_reasons:
        verdict = "REJECT"
        if not win_prob_passed:
            badge_th = "⏳ รอจังหวะ (โอกาสถึง TP ยังไม่เกิน 50% — ระบบกำลังเฝ้าหาจุดเข้าต่อ)"
        else:
            badge_th = "ห้ามเข้าเทรด (REJECT)"
    elif caution_warnings:
        verdict = "CAUTION"
        badge_th = "เข้าได้แต่ต้องระวัง (CAUTION)"
    else:
        verdict = "PASS"
        badge_th = "ผ่านเกณฑ์ความเสี่ยงครบทุกข้อ (PASS)"

    recommended_action = side if (win_prob_passed and not reject_reasons) else "WAIT"

    ticket = {
        "symbol": "XAUUSD",
        "trading_style": trading_style,
        "side": side,
        "execution_type": "MARKET" if is_market_entry else "LIMIT",
        "order_type": trade_setup.get("order_type", f"{side} NOW (Market)" if is_market_entry else f"{side} LIMIT"),
        "is_advance_entry": bool(trade_setup.get("is_advance_entry", True)),
        "distance_to_entry_usd": r2(float(trade_setup.get("distance_to_entry_usd") or 0.0)),
        "recommended_action": recommended_action,
        "win_prob_passed": win_prob_passed,
        "min_win_prob_pct": r2(min_win_prob),
        "next_watch_zone": simulation.get("next_watch_zone"),
        "status": "WATCHING_FOR_50PCT_EDGE" if not win_prob_passed else trade_setup.get("status", "ACTIVE_IN_ZONE"),
        "verdict": verdict,
        "badge_th": badge_th,
        "lot_size": lot_size,
        "entry_mt5": entry_mt5,
        "stop_mt5": stop_mt5,
        "tp1_mt5": tp1_mt5,
        "breakeven_sl_mt5": breakeven_sl_mt5,
        "tp1_partial_profit_usd": tp1_partial_profit_usd,
        "target_mt5": target_mt5,
        "scenario_b_reentry_mt5": scenario_b_mt5,
        "scenario_c_invalidation_mt5": scenario_c_mt5,
        "sl_distance_usd": r2(sl_distance_usd),
        "tp_distance_usd": r2(tp_distance_usd),
        "sl_points": int(round(sl_distance_usd / point_size)),
        "tp_points": int(round(tp_distance_usd / point_size)),
        "reward_risk": rr,
        "sl_atr_ratio": sl_atr_ratio,
        "account_balance": r2(balance),
        "risk_pct_configured": risk_pct,
        "risk_budget_usd": risk_budget_usd,
        "actual_risk_usd": actual_risk_usd,
        "potential_profit_usd": potential_profit_usd,
        "broker_offset_usd": r2(broker_offset),
        "broker_spread_points": int(spread_points),
        "broker_spread_usd": spread_usd,
        "spread_cost_usd": spread_cost_usd,
        "spread_to_sl_pct": spread_to_sl_pct,
        "reject_reasons": reject_reasons,
        "caution_warnings": caution_warnings,
        "passed_checks": passed_checks,
    }
    ticket["fingerprint"] = order_fingerprint(ticket)
    return ticket
