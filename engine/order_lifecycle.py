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
    """Manages active trade order life cycle, price updates, floating PnL, and persistence."""

    def __init__(self, persistence_path: Optional[str] = None):
        self.file_path = persistence_path or ORDER_FILE_PATH
        self.active_order: Optional[Dict[str, Any]] = None
        self.order_history: List[Dict[str, Any]] = []
        self.last_event: Optional[Dict[str, Any]] = None
        self.load_from_disk()

    def load_from_disk(self) -> None:
        """Restore active order from disk so MT5 open positions are never lost on restart."""
        if not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict):
                act = doc.get("active_order")
                if act and isinstance(act, dict) and act.get("status") in ("PENDING", "FILLED"):
                    self.active_order = act
                hist = doc.get("order_history")
                if isinstance(hist, list):
                    self.order_history = hist[-25:]
        except Exception:
            pass

    def save_to_disk(self) -> None:
        """Atomically persist active order state and recent trade history."""
        try:
            payload = {
                "saved_at_bkk": _now_bkk_str(),
                "active_order": self.active_order,
                "order_history": self.order_history[-25:],
            }
            tmp = self.file_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.file_path)
        except Exception:
            pass

    def has_active_order(self) -> bool:
        """Check if an order is currently active (waiting for fill or currently live in MT5)."""
        return bool(self.active_order and self.active_order.get("status") in ("PENDING", "FILLED"))

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
        order_id = f"K79-{side}-{now_ts}"

        is_filled = exec_type == "MARKET"
        initial_status = "FILLED" if is_filled else "PENDING"
        order_type_str = f"{side} NOW (Market)" if is_filled else f"{side} LIMIT"

        order = {
            "order_id": order_id,
            "symbol": "XAUUSD",
            "side": side,
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

        self.active_order = order
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

    def update_price_tick(
        self,
        current_price: float,
        high: Optional[float] = None,
        low: Optional[float] = None,
        ts: Optional[int] = None,
        contract_size: float = 100.0,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate active order against the latest price tick / candle bar.
        Returns a dict describing the event if an order state transition occurred (e.g. FILLED, TP1, TP2, SL)."""
        if not self.has_active_order():
            return None

        order = self.active_order
        side = order["side"]
        entry = float(order["entry_mt5"])
        stop = float(order["stop_mt5"])
        tp1 = float(order["tp1_mt5"])
        target = float(order["target_mt5"])
        lot = float(order.get("lot_size") or 0.10)
        risk_usd = max(1.0, float(order.get("actual_risk_usd") or 50.0))
        rr = float(order.get("reward_risk") or 1.5)
        invalidation = float(order.get("scenario_c_invalidation_mt5") or stop)
        be_sl = float(order.get("breakeven_sl_mt5") or entry)

        bar_hi = high if high is not None else current_price
        bar_lo = low if low is not None else current_price
        now_ts = int(ts or time.time())

        event = None

        # ---------------- Case 1: PENDING LIMIT ORDER ----------------
        if order["status"] == "PENDING":
            dist = abs(current_price - entry)
            order["distance_to_entry_usd"] = r2(dist)

            # Check if price filled the limit entry
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
                    f"🚀 ออเดอร์เกี่ยวกับตลาดแล้ว (FILLED)! ถือครองตำแหน่งตามแผน — "
                    f"เมื่อแตะ TP1 ({tp1:.2f}) ให้แบ่งปิด 50% และเลื่อน SL มาบังทุนที่ {be_sl:.2f}"
                )
                event = {
                    "type": "ORDER_FILLED",
                    "order_id": order["order_id"],
                    "side": side,
                    "filled_price": entry,
                    "message": f"🚀 ออเดอร์เกี่ยวกับตลาดแล้ว (FILLED): {side} XAUUSD @ {entry:.2f}!",
                }
            else:
                # Check if setup was invalidated before price ever filled
                invalidated = False
                if side == "BUY" and bar_lo <= invalidation:
                    invalidated = True
                    reason = "INVALIDATED_BEFORE_FILL"
                    note = f"⚠️ ราคาทะลุแนว Invalidation ({invalidation:.2f}) ก่อนเกี่ยวออเดอร์ — แนะนำยกเลิกคำสั่ง {side} LIMIT ใน MT5"
                elif side == "SELL" and bar_hi >= invalidation:
                    invalidated = True
                    reason = "INVALIDATED_BEFORE_FILL"
                    note = f"⚠️ ราคาทะลุแนว Invalidation ({invalidation:.2f}) ก่อนเกี่ยวออเดอร์ — แนะนำยกเลิกคำสั่ง {side} LIMIT ใน MT5"
                elif (side == "BUY" and bar_hi >= target) or (side == "SELL" and bar_lo <= target):
                    invalidated = True
                    reason = "MISSED_TARGET"
                    note = f"⚪ ราคาพุ่งแตะเป้า TP2 ({target:.2f}) โดยไม่ย่อมาเกี่ยวคำสั่ง (ยกเลิกแผน)"

                if invalidated:
                    order["status"] = "CANCELLED"
                    order["closed_ts"] = now_ts
                    order["close_price"] = current_price
                    order["close_reason"] = reason
                    order["management_note_th"] = note
                    event = {
                        "type": "ORDER_CANCELLED",
                        "order_id": order["order_id"],
                        "side": side,
                        "reason": reason,
                        "message": note,
                    }
                    self._archive_order()

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

            # 1. Check Full Target (TP2)
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
                order["management_note_th"] = f"🏆 ชนะแตะเป้า TP2 ({target:.2f}) สมบูรณ์! ปิดกำไรทั้งชุด (+{rr:.2f}R)"
                event = {
                    "type": "TRADE_CLOSED_TP2",
                    "order_id": order["order_id"],
                    "side": side,
                    "pnl_r": rr,
                    "message": f"🏆 ชนะแตะเป้า TP2 สมบูรณ์! {side} XAUUSD (+{rr:.2f}R)",
                }
                self._archive_order()
                self.save_to_disk()
                return event

            # 2. Check Stop Loss / Breakeven SL
            hit_sl = False
            if side == "BUY" and bar_lo <= active_sl:
                hit_sl = True
            elif side == "SELL" and bar_hi >= active_sl:
                hit_sl = True

            if hit_sl:
                if order.get("sl_moved_breakeven"):
                    order["status"] = "CLOSED_BREAKEVEN"
                    order["closed_ts"] = now_ts
                    order["close_price"] = active_sl
                    order["close_reason"] = "BREAKEVEN_SL"
                    order["realized_pnl_r"] = 0.50  # 50% locked at TP1, 50% stopped at BE
                    order["realized_pnl_usd"] = round(risk_usd * 0.50, 2)
                    order["management_note_th"] = f"🟢 ชน SL บังทุน ({active_sl:.2f}) ล็อกกำไรไม้แรก 50% สำเร็จ (+0.50R)"
                    event = {
                        "type": "TRADE_CLOSED_BREAKEVEN",
                        "order_id": order["order_id"],
                        "side": side,
                        "pnl_r": 0.50,
                        "message": f"🟢 ชน SL บังทุน ล็อกกำไรไม้แรก 50% เรียบร้อย (+0.50R)",
                    }
                else:
                    order["status"] = "CLOSED_SL"
                    order["closed_ts"] = now_ts
                    order["close_price"] = stop
                    order["close_reason"] = "STOP_LOSS"
                    order["realized_pnl_r"] = -1.00
                    order["realized_pnl_usd"] = -risk_usd
                    order["management_note_th"] = f"🔴 ชน Stop Loss ({stop:.2f}) จบแผนตามวินัย (-1.00R)"
                    event = {
                        "type": "TRADE_CLOSED_SL",
                        "order_id": order["order_id"],
                        "side": side,
                        "pnl_r": -1.00,
                        "message": f"🔴 ออเดอร์ชน Stop Loss ที่ {stop:.2f} (-1.00R)",
                    }
                self._archive_order()
                self.save_to_disk()
                return event

            # 3. Check TP1 Partial Profit & Move SL to Breakeven
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
                        f"🟢 ราคาแตะ TP1 ({tp1:.2f}) แล้ว! แนะนำแบ่งปิด 50% ใน MT5 "
                        f"และเลื่อน Stop Loss มาบังทุนที่ {be_sl:.2f} ทันที"
                    )
                    event = {
                        "type": "TP1_REACHED",
                        "order_id": order["order_id"],
                        "side": side,
                        "message": f"🟢 แตะเป้า TP1 ({tp1:.2f}) แล้ว! แบ่งปิด 50% และเลื่อน SL บังทุน",
                    }

        if event:
            self.last_event = event
        self.save_to_disk()
        return event

    def _archive_order(self) -> None:
        """Move completed/cancelled order into history and clear active slot."""
        if self.active_order:
            self.order_history.append(dict(self.active_order))
            self.active_order = None

    def cancel_active_order(self, reason: str = "MANUAL_CANCEL") -> Optional[Dict[str, Any]]:
        """User explicitly cancels the pending order in MT5."""
        if not self.active_order:
            return None
        order = self.active_order
        order["status"] = "CANCELLED"
        order["closed_ts"] = int(time.time())
        order["close_reason"] = reason
        order["management_note_th"] = "ยกเลิกคำสั่งเรียบร้อยแล้ว — กลับสู่โหมดสแกนหาจังหวะใหม่"
        event = {
            "type": "ORDER_CANCELLED",
            "order_id": order["order_id"],
            "reason": reason,
            "message": "ยกเลิกคำสั่งเรียบร้อยแล้ว",
        }
        self.last_event = event
        self._archive_order()
        self.save_to_disk()
        return event

    def close_active_order(self, reason: str = "MANUAL_CLOSE", current_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """User explicitly closed the active order in MT5."""
        if not self.active_order:
            return None
        order = self.active_order
        now_ts = int(time.time())
        order["status"] = "CLOSED_MANUAL"
        order["closed_ts"] = now_ts
        order["close_price"] = current_price or order.get("entry_mt5")
        order["close_reason"] = reason
        order["management_note_th"] = f"ปิดออเดอร์ใน MT5 เรียบร้อย (PnL {order.get('floating_pnl_r', 0.0):+.2f}R)"
        event = {
            "type": "TRADE_CLOSED_MANUAL",
            "order_id": order["order_id"],
            "message": f"บันทึกการปิดออเดอร์เรียบร้อย ({order.get('floating_pnl_usd', 0.0):+.2f} USD)",
        }
        self.last_event = event
        self._archive_order()
        self.save_to_disk()
        return event

    def fill_now(self, current_price: float) -> Optional[Dict[str, Any]]:
        """User pressed fill now (e.g. manually entered at market in MT5 before limit was hit)."""
        if not self.active_order or self.active_order.get("status") != "PENDING":
            return None
        now_ts = int(time.time())
        order = self.active_order
        order["status"] = "FILLED"
        order["filled_ts"] = now_ts
        order["filled_price"] = current_price
        order["filled_bkk"] = _now_bkk_str(now_ts)
        order["management_note_th"] = (
            f"🚀 เปิดออเดอร์ใน MT5 แล้ว @ {current_price:.2f} — "
            f"ติดตามเป้าหมาย TP1 ({order['tp1_mt5']:.2f}) และ TP2 ({order['target_mt5']:.2f})"
        )
        event = {
            "type": "ORDER_FILLED",
            "order_id": order["order_id"],
            "filled_price": current_price,
            "message": f"เปลี่ยนสถานะเป็นเปิดออเดอร์แล้ว @ {current_price:.2f}",
        }
        self.last_event = event
        self.save_to_disk()
        return event

    def set_breakeven(self) -> Optional[Dict[str, Any]]:
        """User marked SL as moved to Break-Even."""
        if not self.active_order or self.active_order.get("status") != "FILLED":
            return None
        order = self.active_order
        order["sl_moved_breakeven"] = True
        order["management_note_th"] = (
            f"🛡️ เลื่อน SL บังทุนที่ {order.get('breakeven_sl_mt5', order['entry_mt5']):.2f} เรียบร้อย — ถือครองลุ้น TP2 อย่างปลอดภัย"
        )
        event = {
            "type": "SL_MOVED_BREAKEVEN",
            "order_id": order["order_id"],
            "message": "บันทึกการขยับ SL บังทุนเรียบร้อย",
        }
        self.last_event = event
        self.save_to_disk()
        return event

    def get_active_order_dict(self, current_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Return serialized active order data with live updated metrics."""
        if not self.active_order:
            return None
        out = dict(self.active_order)
        if current_price is not None:
            if out.get("status") == "PENDING":
                out["distance_to_entry_usd"] = r2(abs(current_price - float(out["entry_mt5"])))
            elif out.get("status") == "FILLED":
                filled_p = float(out.get("filled_price") or out["entry_mt5"])
                side = out["side"]
                lot = float(out.get("lot_size") or 0.10)
                risk_usd = max(1.0, float(out.get("actual_risk_usd") or 50.0))
                delta = (current_price - filled_p) if side == "BUY" else (filled_p - current_price)
                flt_usd = round(delta * lot * 100.0, 2)
                flt_r = round(flt_usd / risk_usd, 2)
                out["floating_pnl_usd"] = flt_usd
                out["floating_pnl_r"] = flt_r
        return out
