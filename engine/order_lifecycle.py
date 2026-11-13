#!/usr/bin/env python3
"""Active Order Lifecycle & Position Tracking Engine (order_lifecycle.py).

Provides persistent, deterministic state management for XAU/USD trade plans:
  1. Prevents entry levels from jumping or vanishing when subsequent candles form.
  2. Tracks the full life cycle of trade orders:
     - PENDING: Waiting for market price to touch and fill the Pending Limit entry.
     - FILLED: Order executed (Market or Limit filled); tracks live floating PnL in USD and R.
     - TP1_REACHED: Partial profit banked (50%), SL moved to Break-Even.
     - CLOSED_TP2: Full target reached (+R:R win).
     - CLOSED_BREAKEVEN: Protected stop hit (+0.50R net win).
     - CLOSED_SL: Stop Loss hit (-1.00R loss).
     - CANCELLED: Setup invalidated or target reached before entry ever got filled.
  3. Supports both Instant Market Execution (เข้าทันทีที่ราคาปัจจุบัน) and Pending Limit (ดักเข้าล่วงหน้า).
  4. Automatically persists active trade state to `active_order.json` across server restarts.
"""

import datetime
import json
import math
import os
import time
from typing import Any, Dict, List, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER_FILE_PATH = os.path.join(ROOT_DIR, "active_order.json")
BKK_TZ = datetime.timezone(datetime.timedelta(hours=7))


def r2(val: Optional[float]) -> float:
    if val is None or math.isnan(val) or math.isinf(val):
        return 0.0
    return round(float(val), 2) + 0.0


def _now_bkk_str(ts: Optional[int] = None) -> str:
    now_ts = int(ts or time.time())
    dt = datetime.datetime.fromtimestamp(now_ts, tz=BKK_TZ)
    return dt.strftime("%d/%m %H:%M:%S น.")


class OrderLifecycleManager:
    """Manages active trade order life cycle, price updates, floating PnL, and persistence.
    Supports single or multiple concurrent orders for recovery/DCA (แก้ไม้) or scale-in (ทำกำไรเพิ่ม)."""

    def __init__(self, persistence_path: Optional[str] = None):
        self.file_path = persistence_path or ORDER_FILE_PATH
        self.active_orders: List[Dict[str, Any]] = []
        self.active_order: Optional[Dict[str, Any]] = None
        self.order_history: List[Dict[str, Any]] = []
        self.last_event: Optional[Dict[str, Any]] = None
        self.last_order_add_ts: int = 0
        self.load_from_disk()

    def _sync_primary(self) -> None:
        """Keep active_orders clean and synchronize active_order to the primary slot."""
        self.active_orders = [o for o in self.active_orders if o.get("status") in ("PENDING", "FILLED")]
        self.active_order = self.active_orders[0] if self.active_orders else None

    def load_from_disk(self) -> None:
        """Restore active orders from disk so open positions are never lost on restart."""
        if not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict):
                act_list = doc.get("active_orders")
                if isinstance(act_list, list) and act_list:
                    self.active_orders = [
                        o for o in act_list if isinstance(o, dict) and o.get("status") in ("PENDING", "FILLED")
                    ]
                elif doc.get("active_order"):
                    act = doc.get("active_order")
                    if isinstance(act, dict) and act.get("status") in ("PENDING", "FILLED"):
                        self.active_orders = [act]
                self._sync_primary()
                hist = doc.get("order_history")
                if isinstance(hist, list):
                    self.order_history = hist[-50:]
        except Exception:
            pass

    def save_to_disk(self) -> None:
        """Atomically persist active order state and recent trade history."""
        try:
            self._sync_primary()
            payload = {
                "saved_at_bkk": _now_bkk_str(),
                "active_order": self.active_order,
                "active_orders": self.active_orders,
                "order_history": self.order_history[-50:],
            }
            tmp = self.file_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.file_path)
        except Exception:
            pass

    def has_active_order(self) -> bool:
        """Check if any order is currently active (waiting for fill or currently live in MT5)."""
        return bool(any(o.get("status") in ("PENDING", "FILLED") for o in self.active_orders))

    def register_trade_setup(
        self,
        risk_ticket: Dict[str, Any],
        smc_setup: Dict[str, Any],
        current_price: float,
        timeframe: str = "M5",
        trading_style: str = "intraday",
        force_replace: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Register a new trade setup into active tracking.
        If an order is already active (PENDING or FILLED), keeps the existing order unless force_replace=True."""
        if self.has_active_order() and not force_replace:
            return self.active_order

        # Only accept setups that passed risk gate and have a valid direction
        side = str(risk_ticket.get("side") or smc_setup.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            return None

        # Check if setup is market execution or pending limit
        exec_type = str(smc_setup.get("execution_type") or "LIMIT").upper()
        if "NOW" in str(smc_setup.get("order_type") or "") or "NOW" in str(risk_ticket.get("order_type") or ""):
            exec_type = "MARKET"

        entry_mt5 = r2(risk_ticket.get("entry_mt5") or current_price)
        stop_mt5 = r2(risk_ticket.get("stop_mt5"))
        tp1_mt5 = r2(risk_ticket.get("tp1_mt5"))
        target_mt5 = r2(risk_ticket.get("target_mt5"))
        lot_size = float(risk_ticket.get("lot_size") or 0.10)
        actual_risk_usd = float(risk_ticket.get("actual_risk_usd") or 50.0)
        reward_risk = float(risk_ticket.get("reward_risk") or 1.5)

        now_ts = int(time.time())
        order_id = f"K79-{side}-{now_ts}-O1"

        is_filled = exec_type == "MARKET"
        initial_status = "FILLED" if is_filled else "PENDING"
        order_type_str = f"{side} NOW (Market)" if is_filled else f"{side} LIMIT"

        order = {
            "order_id": order_id,
            "order_index": 1,
            "symbol": "XAUUSD",
            "side": side,
            "purpose": "INITIAL",
            "purpose_th": "ไม้หลัก (Initial)",
            "execution_type": exec_type,
            "order_type": order_type_str,
            "status": initial_status,
            "timeframe": timeframe,
            "trading_style": trading_style,
            "entry_mt5": entry_mt5,
            "stop_mt5": stop_mt5,
            "tp1_mt5": tp1_mt5,
            "target_mt5": target_mt5,
            "breakeven_sl_mt5": r2(risk_ticket.get("breakeven_sl_mt5") or entry_mt5),
            "scenario_b_reentry_mt5": r2(risk_ticket.get("scenario_b_reentry_mt5") or stop_mt5),
            "scenario_c_invalidation_mt5": r2(risk_ticket.get("scenario_c_invalidation_mt5") or stop_mt5),
            "lot_size": lot_size,
            "actual_risk_usd": actual_risk_usd,
            "potential_profit_usd": float(risk_ticket.get("potential_profit_usd") or 100.0),
            "reward_risk": reward_risk,
            "confluence_score": int(smc_setup.get("confluence_score") or 70),
            "confluence_factors": list(smc_setup.get("confluence_factors") or []),
            "created_ts": now_ts,
            "created_bkk": _now_bkk_str(now_ts),
            "filled_ts": now_ts if is_filled else None,
            "filled_price": entry_mt5 if is_filled else None,
            "tp1_hit": False,
            "tp1_hit_ts": None,
            "sl_moved_breakeven": False,
            "floating_pnl_usd": 0.0,
            "floating_pnl_r": 0.0,
            "highest_pnl_r": 0.0,
            "lowest_pnl_r": 0.0,
            "closed_ts": None,
            "close_price": None,
            "close_reason": None,
            "management_note_th": (
                f"🚀 ออเดอร์เปิดแล้ว ({order_type_str}) — ตั้ง Stop Loss ที่ {stop_mt5:.2f} และ Take Profit ที่ {target_mt5:.2f}"
                if is_filled
                else f"⏳ ตั้งคำสั่ง {order_type_str} ที่ราคา {entry_mt5:.2f} ใน MT5 — รอราคาย่อมาเกี่ยวคำสั่ง (จุดเข้าคงเดิม ไม่เปลี่ยน)"
            ),
        }

        self.active_orders = [order]
        self._sync_primary()
        self.last_event = {
            "type": "NEW_ORDER_REGISTERED",
            "order_id": order_id,
            "side": side,
            "status": initial_status,
            "message": (
                f"🔔 สัญญาณเข้าทันที: {side} NOW @ {entry_mt5:.2f} (SL {stop_mt5:.2f} | TP {target_mt5:.2f})"
                if is_filled
                else f"🔔 สัญญาณตั้งดักล่วงหน้า: {side} LIMIT @ {entry_mt5:.2f} (SL {stop_mt5:.2f} | TP {target_mt5:.2f})"
            ),
        }
        self.save_to_disk()
        return order

    def add_sub_order(
        self,
        purpose: str = "AUTO",
        execution_type: str = "MARKET",
        current_price: float = 0.0,
        entry_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        lot_size: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Add a secondary order for DCA/Recovery (แก้ไม้) or Scale-in (ทำกำไรเพิ่ม).
        Supports up to 5 concurrent active orders."""
        if not self.has_active_order():
            return None
        if len(self.active_orders) >= 5:
            return None

        primary = self.active_orders[0]
        side = primary["side"]
        now_ts = int(time.time())
        idx = len(self.active_orders) + 1

        is_market = execution_type.upper() == "MARKET"
        actual_entry = r2(current_price if is_market else (entry_price or current_price))

        # Auto-determine purpose if AUTO:
        # If price is in drawdown vs primary entry -> RECOVERY (แก้ไม้)
        # If price is in profit vs primary entry -> SCALE_IN (ทำกำไรเพิ่ม)
        purp = purpose.upper()
        if purp == "AUTO":
            primary_entry = float(primary.get("filled_price") or primary.get("entry_mt5") or actual_entry)
            if side == "BUY":
                purp = "RECOVERY" if actual_entry < primary_entry else "SCALE_IN"
            else:
                purp = "RECOVERY" if actual_entry > primary_entry else "SCALE_IN"

        purpose_th = "ไม้แก้ (DCA)" if purp == "RECOVERY" else "ไม้เพิ่มกำไร (Scale)"
        order_id = f"K79-{side}-{now_ts}-O{idx}"
        order_type_str = f"{side} NOW ({purpose_th})" if is_market else f"{side} LIMIT ({purpose_th})"
        initial_status = "FILLED" if is_market else "PENDING"

        sl = r2(stop_loss or primary.get("stop_mt5"))
        tp = r2(take_profit or primary.get("target_mt5"))
        lot = float(lot_size or primary.get("lot_size") or 0.10)
        actual_risk = max(1.0, float(primary.get("actual_risk_usd") or 50.0))

        sub_order = {
            "order_id": order_id,
            "order_index": idx,
            "symbol": "XAUUSD",
            "side": side,
            "purpose": purp,
            "purpose_th": purpose_th,
            "execution_type": "MARKET" if is_market else "LIMIT",
            "order_type": order_type_str,
            "status": initial_status,
            "timeframe": primary.get("timeframe", "M5"),
            "trading_style": primary.get("trading_style", "intraday"),
            "entry_mt5": actual_entry,
            "stop_mt5": sl,
            "tp1_mt5": primary.get("tp1_mt5", tp),
            "target_mt5": tp,
            "breakeven_sl_mt5": r2(actual_entry),
            "lot_size": lot,
            "actual_risk_usd": actual_risk,
            "potential_profit_usd": float(primary.get("potential_profit_usd") or 100.0),
            "reward_risk": float(primary.get("reward_risk") or 1.5),
            "created_ts": now_ts,
            "created_bkk": _now_bkk_str(now_ts),
            "filled_ts": now_ts if is_market else None,
            "filled_price": actual_entry if is_market else None,
            "tp1_hit": False,
            "tp1_hit_ts": None,
            "sl_moved_breakeven": False,
            "floating_pnl_usd": 0.0,
            "floating_pnl_r": 0.0,
            "highest_pnl_r": 0.0,
            "lowest_pnl_r": 0.0,
            "closed_ts": None,
            "close_price": None,
            "close_reason": None,
            "management_note_th": (
                f"⚡ เปิด{purpose_th} สำเร็จ @ {actual_entry:.2f} (Lot {lot:.2f}) — คุม SL ที่ {sl:.2f} และ TP ที่ {tp:.2f}"
                if is_market
                else f"⏳ ดัก{purpose_th} @ {actual_entry:.2f} — รอเกี่ยวคำสั่ง"
            ),
        }

        self.active_orders.append(sub_order)
        self._sync_primary()
        self.last_event = {
            "type": "SUB_ORDER_ADDED",
            "order_id": order_id,
            "purpose": purp,
            "purpose_th": purpose_th,
            "side": side,
            "entry_price": actual_entry,
            "message": f"🔔 เปิด{purpose_th} สำเร็จ: {side} @ {actual_entry:.2f} (Lot {lot:.2f})",
        }
        self.save_to_disk()
        return sub_order

    def check_and_apply_auto_multi_order(
        self,
        current_price: float,
        high: Optional[float] = None,
        low: Optional[float] = None,
        atr: float = 2.5,
        structure: Optional[Dict[str, Any]] = None,
        smc: Optional[Dict[str, Any]] = None,
        risk_ticket: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
        ts: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Autonomously scan for recovery/DCA (แก้ไม้) or scale-in (ทำกำไรเพิ่ม) opportunities
        and automatically register additional orders without closing existing active orders."""
        if not self.has_active_order():
            return None

        cfg = (config or {}).get("multi_order") or {}
        if not cfg.get("auto_mode_enabled", True):
            return None

        max_orders = int(cfg.get("max_concurrent_orders") or 3)
        if len(self.active_orders) >= max_orders:
            return None

        now_ts = int(ts or time.time())
        cooldown = int(cfg.get("cooldown_seconds") or 30)
        if (now_ts - getattr(self, "last_order_add_ts", 0)) < cooldown:
            return None

        primary = self.active_orders[0]
        side = primary["side"]
        primary_entry = float(primary.get("filled_price") or primary.get("entry_mt5") or current_price)
        stop_p = float(primary.get("stop_mt5") or 0.0)
        target_p = float(primary.get("target_mt5") or 0.0)
        min_spacing = float(cfg.get("min_spacing_usd") or max(2.50, 0.8 * atr))

        # Check distance from all existing active orders to prevent clustering
        for o in self.active_orders:
            o_entry = float(o.get("filled_price") or o.get("entry_mt5") or current_price)
            if abs(current_price - o_entry) < min_spacing:
                return None

        st = structure or {}
        st_smc = smc or {}
        bar_hi = high if high is not None else current_price
        bar_lo = low if low is not None else current_price

        # ---------------- 1. Recovery Opportunity (แก้ไม้ DCA) ----------------
        if cfg.get("recovery_enabled", True):
            in_drawdown = False
            safe_above_sl = False

            if side == "BUY":
                in_drawdown = current_price <= (primary_entry - min_spacing)
                safe_above_sl = current_price >= (stop_p + 0.35 * atr)
            else:
                in_drawdown = current_price >= (primary_entry + min_spacing)
                safe_above_sl = current_price <= (stop_p - 0.35 * atr)

            if in_drawdown and safe_above_sl:
                trigger_reason = None

                # Trigger A: Scenario B Liquidity Sweep zone
                scen_b = float(primary.get("scenario_b_reentry_mt5") or 0.0)
                if scen_b > 0 and abs(current_price - scen_b) <= (0.6 * atr):
                    trigger_reason = f"แตะโซนดัก Liquidity Sweep ({scen_b:.2f})"

                # Trigger B: Deep Historical S&R level
                if not trigger_reason:
                    hist_sr = (st.get("historical_sr") or {}).get("levels") or []
                    for sr in hist_sr:
                        sr_price = float(sr.get("price") or 0.0)
                        if abs(current_price - sr_price) <= (0.45 * atr) and sr.get("touches", 0) >= 2:
                            trigger_reason = f"แตะแนวรับประวัติศาสตร์ ({sr_price:.2f}, ทดสอบ {sr['touches']} ครั้ง)"
                            break

                # Trigger C: Unmitigated Order Block / FVG
                if not trigger_reason:
                    obs = st_smc.get("order_blocks") or []
                    for ob in obs:
                        ob_hi = float(ob.get("high") or 0.0)
                        ob_lo = float(ob.get("low") or 0.0)
                        if ob_lo <= current_price <= ob_hi:
                            trigger_reason = f"แตะโซน Order Block ({ob.get('type', 'OB')})"
                            break

                # Trigger D: RSI Oversold / Overbought Rebound
                if not trigger_reason:
                    rsi_val = float((st.get("rsi") or {}).get("rsi14") or 50.0)
                    scalp_info = st.get("scalping") or {}
                    stoch_k = float(scalp_info.get("stoch_rsi_k") or 50.0)
                    if side == "BUY" and (rsi_val <= 34.0 or stoch_k <= 22.0):
                        trigger_reason = f"RSI อยู่ในโซนขายมากเกินไป (Oversold {rsi_val:.1f})"
                    elif side == "SELL" and (rsi_val >= 66.0 or stoch_k >= 78.0):
                        trigger_reason = f"RSI อยู่ในโซนซื้อมากเกินไป (Overbought {rsi_val:.1f})"

                # Trigger E: Lower / Upper shadow rejection
                if not trigger_reason and abs(bar_hi - bar_lo) > 0.05:
                    candle_rng = abs(bar_hi - bar_lo)
                    if side == "BUY" and (min(current_price, bar_hi) - bar_lo) / candle_rng >= 0.40:
                        trigger_reason = "เกิดไส้เทียนล่างปฏิเสธราคา (Wick Rejection)"
                    elif side == "SELL" and (bar_hi - max(current_price, bar_lo)) / candle_rng >= 0.40:
                        trigger_reason = "เกิดไส้เทียนบนปฏิเสธราคา (Wick Rejection)"

                if trigger_reason:
                    lot = float(primary.get("lot_size") or 0.10)
                    sub = self.add_sub_order(
                        purpose="RECOVERY",
                        execution_type="MARKET",
                        current_price=current_price,
                        lot_size=lot,
                    )
                    if sub:
                        sub["trigger_reason"] = trigger_reason
                        sub["management_note_th"] = f"🤖 AI เปิดไม้แก้ (DCA) อัตโนมัติ @ {current_price:.2f} — {trigger_reason}"
                        self.last_order_add_ts = now_ts
                        self.last_event = {
                            "type": "AUTO_ORDER_ADDED",
                            "order_id": sub["order_id"],
                            "purpose": "RECOVERY",
                            "purpose_th": "ไม้แก้ (DCA)",
                            "side": side,
                            "entry_price": current_price,
                            "trigger_reason": trigger_reason,
                            "message": f"🤖 AI เปิดไม้แก้ (DCA) อัตโนมัติ: {side} @ {current_price:.2f} ({trigger_reason})",
                        }
                        self.save_to_disk()
                        return self.last_event

        # ---------------- 2. Scale-in Opportunity (ทำกำไรเพิ่ม) ----------------
        if cfg.get("scale_in_enabled", True):
            primary_flt_r = float(primary.get("floating_pnl_r") or 0.0)
            in_profit = primary_flt_r >= 0.60 or primary.get("tp1_hit") or primary.get("sl_moved_breakeven")

            aligned_direction = False
            if side == "BUY" and current_price >= (primary_entry + min_spacing):
                aligned_direction = True
            elif side == "SELL" and current_price <= (primary_entry - min_spacing):
                aligned_direction = True

            if in_profit and aligned_direction:
                trend_bias = (st.get("trend") or {}).get("bias")
                is_trend_aligned = (side == "BUY" and trend_bias == "BULLISH") or (side == "SELL" and trend_bias == "BEARISH")

                trigger_reason = None
                ema_info = st.get("ema") or {}
                ema20 = float(ema_info.get("ema20") or 0.0)

                if is_trend_aligned:
                    if side == "BUY" and ema20 > 0 and current_price >= ema20:
                        trigger_reason = "เทรนด์แข็งแกร่ง ยืนเหนือ EMA20 พร้อมขยายกำไร"
                    elif side == "SELL" and ema20 > 0 and current_price <= ema20:
                        trigger_reason = "เทรนด์แข็งแกร่ง อยู่ใต้ EMA20 พร้อมขยายกำไร"
                    else:
                        trigger_reason = "กำไรทะลุ +0.6R และโมเมนตัมขยายตัวตามแนวโน้ม"

                if trigger_reason:
                    lot = float(primary.get("lot_size") or 0.10)
                    sub = self.add_sub_order(
                        purpose="SCALE_IN",
                        execution_type="MARKET",
                        current_price=current_price,
                        lot_size=lot,
                    )
                    if sub:
                        sub["trigger_reason"] = trigger_reason
                        sub["management_note_th"] = f"🤖 AI เปิดไม้ทำกำไร (Scale) อัตโนมัติ @ {current_price:.2f} — {trigger_reason}"
                        self.last_order_add_ts = now_ts
                        self.last_event = {
                            "type": "AUTO_ORDER_ADDED",
                            "order_id": sub["order_id"],
                            "purpose": "SCALE_IN",
                            "purpose_th": "ไม้เพิ่มกำไร (Scale)",
                            "side": side,
                            "entry_price": current_price,
                            "trigger_reason": trigger_reason,
                            "message": f"🤖 AI เปิดไม้เพิ่มกำไรอัตโนมัติ: {side} @ {current_price:.2f} ({trigger_reason})",
                        }
                        self.save_to_disk()
                        return self.last_event

        return None

    def update_price_tick(
        self,
        current_price: float,
        high: Optional[float] = None,
        low: Optional[float] = None,
        ts: Optional[int] = None,
        contract_size: float = 100.0,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate active orders against the latest price tick / candle bar.
        Returns the last significant state transition event if occurred."""
        if not self.has_active_order():
            return None

        bar_hi = high if high is not None else current_price
        bar_lo = low if low is not None else current_price
        now_ts = int(ts or time.time())
        last_event = None

        remaining_orders = []
        for order in self.active_orders:
            side = order["side"]
            entry = float(order["entry_mt5"])
            stop = float(order["stop_mt5"])
            tp1 = float(order["tp1_mt5"])
            target = float(order["target_mt5"])
            lot = float(order.get("lot_size") or 0.10)
            risk_usd = max(1.0, float(order.get("actual_risk_usd") or 50.0))
            rr = float(order.get("reward_risk") or 1.5)
            be_sl = float(order.get("breakeven_sl_mt5") or entry)
            invalidation = float(order.get("scenario_c_invalidation_mt5") or stop)

            # ---------------- Case 1: PENDING LIMIT ORDER ----------------
            if order["status"] == "PENDING":
                dist = abs(current_price - entry)
                order["distance_to_entry_usd"] = r2(dist)

                filled = False
                if side == "BUY" and bar_lo <= entry:
                    filled = True
                elif side == "SELL" and bar_hi >= entry:
                    filled = True

                if filled:
                    order["status"] = "FILLED"
                    order["filled_ts"] = now_ts
                    order["filled_price"] = entry
                    order["filled_bkk"] = _now_bkk_str(now_ts)
                    order["management_note_th"] = (
                        f"🚀 ออเดอร์เกี่ยวกับตลาดแล้ว ({order.get('purpose_th', side)})! "
                        f"ถือครองตามแผน — TP1 {tp1:.2f} / TP2 {target:.2f}"
                    )
                    last_event = {
                        "type": "ORDER_FILLED",
                        "order_id": order["order_id"],
                        "side": side,
                        "filled_price": entry,
                        "message": f"🚀 ออเดอร์เกี่ยวกับตลาดแล้ว: {side} @ {entry:.2f}!",
                    }
                    remaining_orders.append(order)
                else:
                    invalidated = False
                    reason = ""
                    if (side == "BUY" and bar_lo <= invalidation) or (side == "SELL" and bar_hi >= invalidation):
                        invalidated = True
                        reason = "INVALIDATED_BEFORE_FILL"
                    elif (side == "BUY" and bar_hi >= target) or (side == "SELL" and bar_lo <= target):
                        invalidated = True
                        reason = "MISSED_TARGET"

                    if invalidated:
                        order["status"] = "CANCELLED"
                        order["closed_ts"] = now_ts
                        order["close_price"] = current_price
                        order["close_reason"] = reason
                        order["management_note_th"] = "คำสั่งถูกยกเลิกเนื่องจากราคาเลยจุดเข้าหรือชนเป้าก่อน"
                        self.order_history.append(dict(order))
                        last_event = {
                            "type": "ORDER_CANCELLED",
                            "order_id": order["order_id"],
                            "side": side,
                            "reason": reason,
                            "message": f"คำสั่ง {order['order_id']} ถูกยกเลิก ({reason})",
                        }
                    else:
                        remaining_orders.append(order)

            # ---------------- Case 2: FILLED ACTIVE TRADE ----------------
            elif order["status"] == "FILLED":
                filled_p = float(order.get("filled_price") or entry)
                delta = (current_price - filled_p) if side == "BUY" else (filled_p - current_price)
                flt_usd = round(delta * lot * contract_size, 2)
                flt_r = round(flt_usd / risk_usd, 2)

                order["floating_pnl_usd"] = flt_usd
                order["floating_pnl_r"] = flt_r
                order["highest_pnl_r"] = max(float(order.get("highest_pnl_r") or flt_r), flt_r)
                order["lowest_pnl_r"] = min(float(order.get("lowest_pnl_r") or flt_r), flt_r)

                active_sl = be_sl if order.get("sl_moved_breakeven") else stop

                # 1. Full Target TP2
                hit_tp2 = False
                if side == "BUY" and bar_hi >= target:
                    hit_tp2 = True
                elif side == "SELL" and bar_lo <= target:
                    hit_tp2 = True

                if hit_tp2:
                    order["status"] = "CLOSED_TP2"
                    order["closed_ts"] = now_ts
                    order["close_price"] = target
                    order["close_reason"] = "TP2_FULL_TARGET"
                    order["realized_pnl_r"] = rr
                    order["realized_pnl_usd"] = round(risk_usd * rr, 2)
                    order["management_note_th"] = f"🏆 ชนะแตะเป้า TP2 ({target:.2f}) สมบูรณ์! (+{rr:.2f}R)"
                    self.order_history.append(dict(order))
                    last_event = {
                        "type": "TRADE_CLOSED_TP2",
                        "order_id": order["order_id"],
                        "side": side,
                        "pnl_r": rr,
                        "message": f"🏆 ชนะแตะเป้า TP2 สมบูรณ์! {side} XAUUSD (+{rr:.2f}R)",
                    }
                    continue

                # 2. Stop Loss / Breakeven SL
                hit_sl = False
                if side == "BUY" and bar_lo <= active_sl:
                    hit_sl = True
                elif side == "SELL" and bar_hi >= active_sl:
                    hit_sl = True

                if hit_sl:
                    if order.get("sl_moved_breakeven"):
                        order["status"] = "CLOSED_BREAKEVEN"
                        order["realized_pnl_r"] = 0.50
                        order["realized_pnl_usd"] = round(risk_usd * 0.50, 2)
                        order["management_note_th"] = f"🟢 ชน SL บังทุน ({active_sl:.2f}) ล็อกกำไรไม้แรก (+0.50R)"
                        last_event = {
                            "type": "TRADE_CLOSED_BREAKEVEN",
                            "order_id": order["order_id"],
                            "side": side,
                            "pnl_r": 0.50,
                            "message": f"🟢 ชน SL บังทุน ล็อกกำไรไม้แรก (+0.50R)",
                        }
                    else:
                        order["status"] = "CLOSED_SL"
                        order["realized_pnl_r"] = -1.00
                        order["realized_pnl_usd"] = -risk_usd
                        order["management_note_th"] = f"🔴 ชน Stop Loss ({stop:.2f}) จบแผนตามวินัย (-1.00R)"
                        last_event = {
                            "type": "TRADE_CLOSED_SL",
                            "order_id": order["order_id"],
                            "side": side,
                            "pnl_r": -1.00,
                            "message": f"🔴 ออเดอร์ชน Stop Loss ที่ {stop:.2f} (-1.00R)",
                        }
                    order["closed_ts"] = now_ts
                    order["close_price"] = active_sl
                    order["close_reason"] = "BREAKEVEN_SL" if order.get("sl_moved_breakeven") else "STOP_LOSS"
                    self.order_history.append(dict(order))
                    continue

                # 3. TP1 Partial Profit & Breakeven SL
                if not order.get("tp1_hit"):
                    hit_tp1 = False
                    if side == "BUY" and bar_hi >= tp1:
                        hit_tp1 = True
                    elif side == "SELL" and bar_lo <= tp1:
                        hit_tp1 = True

                    if hit_tp1:
                        order["tp1_hit"] = True
                        order["tp1_hit_ts"] = now_ts
                        order["sl_moved_breakeven"] = True
                        order["management_note_th"] = (
                            f"🟢 ราคาแตะ TP1 ({tp1:.2f}) แล้ว! แนะนำแบ่งปิด 50% "
                            f"และเลื่อน Stop Loss มาบังทุนที่ {be_sl:.2f} ทันที"
                        )
                        last_event = {
                            "type": "TP1_REACHED",
                            "order_id": order["order_id"],
                            "side": side,
                            "message": f"🟢 แตะเป้า TP1 ({tp1:.2f}) แล้ว! แบ่งปิด 50% และเลื่อน SL บังทุน",
                        }

                remaining_orders.append(order)

        self.active_orders = remaining_orders
        self._sync_primary()
        if last_event:
            self.last_event = last_event
        self.save_to_disk()
        return last_event

    def close_order_by_id(
        self,
        order_id: str,
        current_price: Optional[float] = None,
        reason: str = "MANUAL_CLOSE",
    ) -> Optional[Dict[str, Any]]:
        """Close a specific active order by its ID."""
        target_ord = None
        for o in self.active_orders:
            if o.get("order_id") == order_id:
                target_ord = o
                break
        if not target_ord:
            return None

        now_ts = int(time.time())
        cp = current_price or target_ord.get("entry_mt5") or 0.0
        side = target_ord["side"]
        lot = float(target_ord.get("lot_size") or 0.10)
        entry_p = float(target_ord.get("filled_price") or target_ord.get("entry_mt5") or cp)
        delta = (cp - entry_p) if side == "BUY" else (entry_p - cp)
        flt_usd = round(delta * lot * 100.0, 2)
        risk_usd = max(1.0, float(target_ord.get("actual_risk_usd") or 50.0))
        flt_r = round(flt_usd / risk_usd, 2)

        target_ord["status"] = "CLOSED_MANUAL"
        target_ord["closed_ts"] = now_ts
        target_ord["close_price"] = cp
        target_ord["close_reason"] = reason
        target_ord["realized_pnl_usd"] = flt_usd
        target_ord["realized_pnl_r"] = flt_r
        target_ord["management_note_th"] = f"จบการเทรดไม้นี้เรียบร้อย (PnL {flt_r:+.2f}R, {flt_usd:+.2f} USD)"

        self.order_history.append(dict(target_ord))
        self.active_orders = [o for o in self.active_orders if o.get("order_id") != order_id]
        self._sync_primary()
        event = {
            "type": "ORDER_CLOSED_MANUAL",
            "order_id": order_id,
            "pnl_usd": flt_usd,
            "pnl_r": flt_r,
            "message": f"จบการเทรดไม้ {target_ord.get('purpose_th', '')} เรียบร้อย ({flt_usd:+.2f} USD)",
        }
        self.last_event = event
        self.save_to_disk()
        return event

    def close_all_orders(
        self,
        current_price: Optional[float] = None,
        reason: str = "MANUAL_CLOSE_ALL",
    ) -> List[Dict[str, Any]]:
        """Close all currently active orders simultaneously."""
        events = []
        order_ids = [o["order_id"] for o in list(self.active_orders)]
        for oid in order_ids:
            ev = self.close_order_by_id(oid, current_price=current_price, reason=reason)
            if ev:
                events.append(ev)
        self.active_orders = []
        self._sync_primary()
        self.save_to_disk()
        return events

    def close_active_order(self, reason: str = "MANUAL_CLOSE", current_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Backwards-compatible: closes the active order (or all if multiple)."""
        if not self.has_active_order():
            return None
        if len(self.active_orders) > 1:
            events = self.close_all_orders(current_price=current_price, reason=reason)
            return events[-1] if events else None
        else:
            return self.close_order_by_id(self.active_orders[0]["order_id"], current_price=current_price, reason=reason)

    def cancel_active_order(self, reason: str = "MANUAL_CANCEL", order_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Cancel a pending limit order (or all pending orders if order_id is None)."""
        if not self.has_active_order():
            return None
        if order_id:
            for o in self.active_orders:
                if o.get("order_id") == order_id:
                    o["status"] = "CANCELLED"
                    o["closed_ts"] = int(time.time())
                    o["close_reason"] = reason
                    self.order_history.append(dict(o))
                    self.active_orders = [x for x in self.active_orders if x.get("order_id") != order_id]
                    self._sync_primary()
                    self.save_to_disk()
                    return {"type": "ORDER_CANCELLED", "order_id": order_id, "message": "ยกเลิกคำสั่งเรียบร้อย"}
            return None
        else:
            for o in list(self.active_orders):
                if o.get("status") == "PENDING":
                    o["status"] = "CANCELLED"
                    o["closed_ts"] = int(time.time())
                    o["close_reason"] = reason
                    self.order_history.append(dict(o))
            self.active_orders = [o for o in self.active_orders if o.get("status") == "FILLED"]
            self._sync_primary()
            self.save_to_disk()
            return {"type": "ORDER_CANCELLED", "message": "ยกเลิกคำสั่งที่รอทั้งหมดเรียบร้อย"}

    def fill_now(self, current_price: float, order_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """User pressed fill now for pending order(s)."""
        if not self.has_active_order():
            return None
        target = None
        for o in self.active_orders:
            if (not order_id or o.get("order_id") == order_id) and o.get("status") == "PENDING":
                target = o
                break
        if not target:
            return None

        now_ts = int(time.time())
        target["status"] = "FILLED"
        target["filled_ts"] = now_ts
        target["filled_price"] = current_price
        target["filled_bkk"] = _now_bkk_str(now_ts)
        target["management_note_th"] = (
            f"🚀 เปิดออเดอร์แล้ว @ {current_price:.2f} — ติดตามเป้าหมาย TP1 ({target['tp1_mt5']:.2f}) และ TP2 ({target['target_mt5']:.2f})"
        )
        self._sync_primary()
        event = {
            "type": "ORDER_FILLED",
            "order_id": target["order_id"],
            "filled_price": current_price,
            "message": f"เปลี่ยนสถานะเป็นเปิดออเดอร์แล้ว @ {current_price:.2f}",
        }
        self.last_event = event
        self.save_to_disk()
        return event

    def set_breakeven(self, order_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """User marked SL as moved to Break-Even."""
        if not self.has_active_order():
            return None
        affected = 0
        for o in self.active_orders:
            if (not order_id or o.get("order_id") == order_id) and o.get("status") == "FILLED":
                o["sl_moved_breakeven"] = True
                o["management_note_th"] = f"🛡️ เลื่อน SL บังทุนที่ {o.get('breakeven_sl_mt5', o['entry_mt5']):.2f} เรียบร้อย"
                affected += 1
        if affected == 0:
            return None
        self._sync_primary()
        event = {
            "type": "SL_MOVED_BREAKEVEN",
            "message": f"บันทึกการขยับ SL บังทุน ({affected} ไม้) เรียบร้อย",
        }
        self.last_event = event
        self.save_to_disk()
        return event

    def get_active_order_dict(self, current_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Return serialized active order data with individual positions and portfolio aggregates."""
        self._sync_primary()
        if not self.active_orders:
            return None

        primary = self.active_orders[0]
        out = dict(primary)
        cp = current_price if current_price is not None else float(primary.get("entry_mt5") or 0.0)

        orders_data = []
        total_lot = 0.0
        total_pnl_usd = 0.0
        total_pnl_r = 0.0
        weighted_entry_sum = 0.0

        for idx, ord_item in enumerate(self.active_orders, 1):
            o_dict = dict(ord_item)
            o_dict["order_index"] = idx
            lot = float(o_dict.get("lot_size") or 0.10)
            risk_usd = max(1.0, float(o_dict.get("actual_risk_usd") or 50.0))
            entry_p = float(o_dict.get("filled_price") or o_dict.get("entry_mt5") or cp)
            side = o_dict.get("side", "BUY")

            if o_dict.get("status") == "PENDING":
                o_dict["distance_to_entry_usd"] = r2(abs(cp - entry_p))
            elif o_dict.get("status") == "FILLED":
                delta = (cp - entry_p) if side == "BUY" else (entry_p - cp)
                flt_usd = round(delta * lot * 100.0, 2)
                flt_r = round(flt_usd / risk_usd, 2)
                o_dict["floating_pnl_usd"] = flt_usd
                o_dict["floating_pnl_r"] = flt_r

                total_lot += lot
                total_pnl_usd += flt_usd
                total_pnl_r += flt_r
                weighted_entry_sum += entry_p * lot

            orders_data.append(o_dict)

        avg_entry = round(weighted_entry_sum / total_lot, 2) if total_lot > 0 else float(primary.get("entry_mt5") or cp)

        out["orders"] = orders_data
        out["total_orders_count"] = len(orders_data)
        out["filled_orders_count"] = sum(1 for o in orders_data if o.get("status") == "FILLED")
        out["pending_orders_count"] = sum(1 for o in orders_data if o.get("status") == "PENDING")
        out["total_lot_size"] = round(total_lot, 2) if total_lot > 0 else float(primary.get("lot_size") or 0.10)
        out["total_floating_pnl_usd"] = round(total_pnl_usd, 2)
        out["total_floating_pnl_r"] = round(total_pnl_r, 2)
        out["avg_entry_price"] = avg_entry

        if len(orders_data) > 1:
            out["floating_pnl_usd"] = round(total_pnl_usd, 2)
            out["floating_pnl_r"] = round(total_pnl_r, 2)
        elif len(orders_data) == 1 and orders_data[0].get("status") == "FILLED":
            out["floating_pnl_usd"] = orders_data[0]["floating_pnl_usd"]
            out["floating_pnl_r"] = orders_data[0]["floating_pnl_r"]

        return out
