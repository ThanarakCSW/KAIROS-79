#!/usr/bin/env python3
"""
Loss Pattern Learning & Post-Mortem Analysis Engine (engine/loss_learning.py).
Studies, records, and remembers failure patterns from losing trades,
enforces 20% Drawdown Circuit Breaker, triggers Deep Re-Analysis,
and prevents repeating identical mistakes in subsequent trade evaluations.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kairos.loss_learning")

DEFAULT_MEMORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "loss_patterns.json"
)


def _bkk_time_str(ts: Optional[float] = None) -> str:
    t = ts if ts is not None else time.time()
    t_bkk = t + (7 * 3600)
    gm = time.gmtime(t_bkk)
    return time.strftime("%d/%m %H:%M:%S น.", gm)


class LossPatternMemoryManager:
    """Manages persistent loss pattern memory, failure clustering, and deep re-analysis."""

    def __init__(self, persistence_path: Optional[str] = None):
        self.persistence_path = persistence_path or DEFAULT_MEMORY_FILE
        self.patterns: Dict[str, Dict[str, Any]] = {}
        self.loss_history: List[Dict[str, Any]] = []
        self.latest_reanalysis: Optional[Dict[str, Any]] = None
        self.consecutive_loss_usd: float = 0.0
        self.consecutive_loss_count: int = 0
        self.circuit_breaker_tripped: bool = False
        self.last_reset_ts: int = 0
        self.load_from_disk()

    def load_from_disk(self) -> None:
        """Load loss patterns from disk if file exists."""
        if not os.path.exists(self.persistence_path):
            self.patterns = {}
            self.loss_history = []
            return
        try:
            with open(self.persistence_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.patterns = data.get("patterns", {})
            self.loss_history = data.get("loss_history", [])
            self.latest_reanalysis = data.get("latest_reanalysis")
            self.consecutive_loss_usd = float(data.get("consecutive_loss_usd", 0.0))
            self.consecutive_loss_count = int(data.get("consecutive_loss_count", 0))
            self.circuit_breaker_tripped = bool(data.get("circuit_breaker_tripped", False))
            self.last_reset_ts = int(data.get("last_reset_ts", 0))
        except Exception as e:
            logger.warning(f"Failed to load loss pattern memory from {self.persistence_path}: {e}")
            self.patterns = {}
            self.loss_history = []

    def save_to_disk(self) -> None:
        """Persist memory to disk atomically."""
        data = {
            "saved_at_bkk": _bkk_time_str(),
            "consecutive_loss_usd": round(self.consecutive_loss_usd, 2),
            "consecutive_loss_count": self.consecutive_loss_count,
            "circuit_breaker_tripped": self.circuit_breaker_tripped,
            "last_reset_ts": self.last_reset_ts,
            "total_patterns_tracked": len(self.patterns),
            "patterns": self.patterns,
            "loss_history": self.loss_history[-100:],  # Keep last 100 historical losses
            "latest_reanalysis": self.latest_reanalysis,
        }
        try:
            tmp_path = self.persistence_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_path, self.persistence_path)
        except Exception as e:
            try:
                # Direct fallback write if atomic replace encounters Windows lock
                with open(self.persistence_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            except Exception as e2:
                logger.error(f"Failed to save loss pattern memory: {e2}")

    def compute_adaptive_tuning(self, streak_count: int) -> Dict[str, Any]:
        """Background adaptive self-tuning when consecutive losses exceed 3 trades.
        Never freezes trading via a hard DD Circuit Breaker; instead adapts entry quality,
        order allocation (lot scaling), and SL/TP buffers dynamically."""
        if streak_count >= 4:
            return {
                "active": True,
                "level": "DEEP_ADAPTIVE",
                "label_th": f"🧬 ปรับกลยุทธ์เข้มข้น (แพ้ติดกัน {streak_count} ไม้ • ลด Lot 50% + บังคับยืนยันแท่งเทียน/MTF)",
                "lot_scale": 0.50,
                "min_confluence_boost": 14,
                "sl_buffer_mult": 1.25,
                "tp_mult": 0.80,
                "require_candle_confirm": True,
                "require_mtf_sr_align": True,
            }
        if streak_count >= 3:
            return {
                "active": True,
                "level": "ADAPTIVE_TUNE",
                "label_th": f"🧬 วิเคราะห์และปรับจูนเบื้องหลัง (แพ้ติดกัน {streak_count} ไม้ • ลด Lot 30% + ยกระดับจุดเข้า)",
                "lot_scale": 0.70,
                "min_confluence_boost": 8,
                "sl_buffer_mult": 1.15,
                "tp_mult": 0.85,
                "require_candle_confirm": True,
                "require_mtf_sr_align": True,
            }
        return {
            "active": False,
            "level": "NORMAL",
            "label_th": "✅ สภาวะปกติ: จัดสรรออเดอร์เต็มประสิทธิภาพ (100% Allocation)",
            "lot_scale": 1.0,
            "min_confluence_boost": 0,
            "sl_buffer_mult": 1.0,
            "tp_mult": 1.0,
            "require_candle_confirm": False,
            "require_mtf_sr_align": False,
        }

    def update_consecutive_losses(
        self,
        order_history: List[Dict[str, Any]],
        balance: float = 5000.0,
        threshold_pct: float = 20.0,
    ) -> Dict[str, Any]:
        """Compute consecutive loss streak and produce background adaptive tuning (>= 3 losses).
        DD Circuit Breaker lock is removed — replaced by automatic background analysis & adaptation."""
        streak_count = 0
        streak_usd = 0.0

        # Scan order history in reverse (most recent closed trades first)
        clean_history = [
            o for o in order_history
            if not str(o.get("order_id", "")).startswith("test_")
            and o.get("close_reason") not in ("TEST_CLEANUP", "SYSTEM_INIT")
        ]

        now_wall = int(time.time())
        for o in reversed(clean_history):
            raw_cts = int(o.get("closed_ts") or o.get("created_ts") or 0)
            # Sanitize legacy future MT5 UTC+2 timestamps (> now + 60s)
            c_ts = (raw_cts - 7200) if raw_cts > (now_wall + 60) else raw_cts
            if self.last_reset_ts > 0 and c_ts <= self.last_reset_ts:
                break
            pnl_usd = float(o.get("realized_pnl_usd") or 0.0)
            if pnl_usd < -0.01:
                streak_count += 1
                streak_usd += abs(pnl_usd)
            elif pnl_usd > 0.01:
                # Winning trade resets the streak and restores 100% allocation
                break

        self.consecutive_loss_count = streak_count
        self.consecutive_loss_usd = round(streak_usd, 2)
        self.circuit_breaker_tripped = False  # DD Circuit Breaker removed in favor of background adaptive analysis

        bal = max(1.0, balance)
        loss_pct = round((self.consecutive_loss_usd / bal) * 100.0, 2)
        adaptive = self.compute_adaptive_tuning(streak_count)
        needs_reanalysis = streak_count >= 3

        reason = ""
        if needs_reanalysis:
            reason = (
                f"🧬 ขาดทุนติดกัน {streak_count} ไม้ (-${self.consecutive_loss_usd:.2f}) — "
                f"รันวิเคราะห์เบื้องหลังและปรับปรุงจุดเข้า/จัดสรร Lot เป็น {int(adaptive['lot_scale'] * 100)}% อัตโนมัติ"
            )

        self.save_to_disk()
        return {
            "consecutive_loss_count": streak_count,
            "consecutive_loss_usd": self.consecutive_loss_usd,
            "loss_pct": loss_pct,
            "consecutive_loss_pct": loss_pct,
            "circuit_breaker_tripped": False,
            "needs_adaptive_reanalysis": needs_reanalysis,
            "adaptive_tuning": adaptive,
            "reason": reason,
        }

    def reset_circuit_breaker(self) -> None:
        """Reset consecutive loss streak, clear stale patterns, and restore normal 100% adaptive allocation."""
        self.full_reset()

    def full_reset(self) -> Dict[str, Any]:
        """Completely clear all loss-pattern vetoes, streak locks, and adaptive blocks for a fresh start."""
        cleared_patterns = len(self.patterns)
        prev_streak = int(self.consecutive_loss_count or 0)
        self.circuit_breaker_tripped = False
        self.consecutive_loss_count = 0
        self.consecutive_loss_usd = 0.0
        self.patterns = {}
        self.latest_reanalysis = None
        self.last_reset_ts = int(time.time())
        self.save_to_disk()
        return {
            "cleared_patterns": cleared_patterns,
            "previous_streak": prev_streak,
            "reset_ts": self.last_reset_ts,
        }

    def record_loss_order(
        self,
        order: Dict[str, Any],
        market_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Extract failure signature from a losing trade and register it into pattern memory."""
        realized_usd = float(order.get("realized_pnl_usd") or order.get("floating_pnl_usd") or 0.0)
        realized_r = float(order.get("realized_pnl_r") or order.get("floating_pnl_r") or 0.0)

        # Only process losing orders
        if realized_usd >= -0.01 and realized_r >= -0.05:
            return None

        sym = str(order.get("symbol") or "XAUUSD").upper()
        side = str(order.get("side") or "BUY").upper()
        strat = str(order.get("strategy_mode") or "SMC_SWING").upper()
        arch = str(order.get("setup_archetype") or "PULLBACK").upper()
        tf = str(order.get("timeframe") or "M1").upper()
        close_reason = str(order.get("close_reason") or "STOP_LOSS").upper()

        # Extract market context features at time of trade/close
        ctx = market_context or {}
        st = ctx.get("structure") or {}
        trend = st.get("trend") or {}
        if isinstance(trend, dict):
            regime = "SIDEWAY" if order.get("is_sideway") else str(trend.get("regime") or "TRENDING").upper()
            if "RANGING" in regime or "CHOP" in regime:
                regime = "SIDEWAY"
            bias = str(trend.get("bias") or st.get("current_bias") or "NEUTRAL").upper()
            ema_align = str(trend.get("ema_alignment") or "MIXED").upper()
        else:
            regime = "SIDEWAY" if order.get("is_sideway") else "TRENDING"
            bias = str(trend or "NEUTRAL").upper()
            ema_align = "ALIGNED" if ("BULL" in bias or "BEAR" in bias) else "MIXED"
        vf = st.get("volume_flow") or {}
        flow_state = str(vf.get("flow_state") or "NORMAL").upper()

        # Build composite signature key: {sym}_{side}_{regime}_{flow_state}
        sig_key = f"{sym}_{side}_{regime}_{flow_state}".replace(" ", "_")
        pattern_label = f"{side} ใน {regime} ({flow_state})"

        # Generate Thai lesson learned
        lesson = self._generate_lesson_text(side, regime, flow_state, arch, close_reason)

        now_ts = int(time.time())
        if sig_key not in self.patterns:
            self.patterns[sig_key] = {
                "pattern_key": sig_key,
                "symbol": sym,
                "side": side,
                "market_regime": regime,
                "flow_state": flow_state,
                "ema_alignment": ema_align,
                "trend_bias": bias,
                "setup_archetype": arch,
                "pattern_label_th": pattern_label,
                "total_losses": 0,
                "total_loss_usd": 0.0,
                "total_loss_r": 0.0,
                "first_loss_ts": now_ts,
                "last_loss_ts": now_ts,
                "last_loss_bkk": _bkk_time_str(now_ts),
                "lesson_learned_th": lesson,
                "recent_tickets": [],
            }

        pat = self.patterns[sig_key]
        pat["total_losses"] += 1
        pat["failure_count"] = pat["total_losses"]
        pat["total_loss_usd"] = round(pat["total_loss_usd"] + abs(realized_usd), 2)
        pat["total_loss_r"] = round(pat["total_loss_r"] + abs(realized_r), 2)
        pat["last_loss_ts"] = now_ts
        pat["last_loss_bkk"] = _bkk_time_str(now_ts)
        pat["lesson_learned_th"] = lesson
        pat["lesson_learned"] = lesson
        pat["signature"] = sig_key

        ticket = order.get("broker_ticket") or order.get("order_id")
        if ticket and ticket not in pat["recent_tickets"]:
            pat["recent_tickets"].append(ticket)
            pat["recent_tickets"] = pat["recent_tickets"][-5:]

        # Add to global loss log
        loss_entry = {
            "order_id": order.get("order_id"),
            "broker_ticket": order.get("broker_ticket"),
            "symbol": sym,
            "side": side,
            "strategy_mode": strat,
            "regime": regime,
            "flow_state": flow_state,
            "realized_pnl_usd": realized_usd,
            "realized_pnl_r": realized_r,
            "close_reason": close_reason,
            "timestamp": now_ts,
            "time_bkk": _bkk_time_str(now_ts),
            "signature_key": sig_key,
        }
        self.loss_history.append(loss_entry)
        self.save_to_disk()
        return pat

    def record_consecutive_loss_streak(
        self,
        losing_orders: List[Dict[str, Any]],
        market_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record and remember a critical 5-consecutive-loss failure streak.
        Analyzes all 5 losing trades, aggregates their total loss, identifies common failure factors,
        and saves a high-priority veto pattern to prevent repeating the streak."""
        if not losing_orders:
            return {}

        now_ts = int(time.time())
        sym = str(losing_orders[0].get("symbol") or "XAUUSD").upper()
        sides = [str(o.get("side") or "").upper() for o in losing_orders]
        dominant_side = max(set(sides), key=sides.count) if sides else "BUY"
        total_streak_loss_usd = round(sum(abs(float(o.get("realized_pnl_usd") or 0.0)) for o in losing_orders), 2)
        total_streak_loss_r = round(sum(abs(float(o.get("realized_pnl_r") or 0.0)) for o in losing_orders), 2)

        # Record each of the individual orders first
        for idx, o in enumerate(losing_orders, 1):
            o["loss_pattern_recorded"] = True
            o["streak_index"] = idx
            o["is_5_consecutive_streak"] = True
            self.record_loss_order(o, market_context=market_context)

        # Contextual regime
        ctx = market_context or {}
        st = ctx.get("structure") or {}
        trend = st.get("trend") or {}
        regime = "SIDEWAY" if any(o.get("is_sideway") for o in losing_orders) else str(trend.get("regime") or "TRENDING").upper()
        flow_state = str((st.get("volume_flow") or {}).get("flow_state") or "CHOPPY").upper()

        sig_key = f"CONSECUTIVE_5_STREAK_{sym}_{dominant_side}_{regime}"
        lesson = (
            f"🚨 บทเรียนความล้มเหลวต่อเนื่อง 5 ไม้ติด: เกิดการแพ้สะสมติดกัน 5 ไม้ ({dominant_side} ในสภาวะ {regime} / {flow_state}) "
            f"ความเสียหายรวม -${total_streak_loss_usd:.2f} (-{total_streak_loss_r:.2f}R) "
            f"— สาเหตุเกิดจากการเทรดซ้ำในสภาวะตลาดที่ไม่เอื้ออำนวย ระบบทำการ Veto ห้ามเข้าออเดอร์ในรูปแบบนี้จนกว่าจะผ่านการวิเคราะห์เชิงลึก"
        )

        streak_tickets = [o.get("broker_ticket") or o.get("order_id") for o in losing_orders if o.get("broker_ticket") or o.get("order_id")]

        self.patterns[sig_key] = {
            "pattern_key": sig_key,
            "signature": sig_key,
            "symbol": sym,
            "side": dominant_side,
            "market_regime": regime,
            "flow_state": flow_state,
            "is_consecutive_streak": True,
            "streak_size": len(losing_orders),
            "failure_count": len(losing_orders),
            "total_losses": len(losing_orders),
            "total_loss_usd": total_streak_loss_usd,
            "total_loss_r": total_streak_loss_r,
            "pattern_label_th": f"🚨 แพ้ต่อเนื่อง 5 ไม้ติด ({sym} {dominant_side} {regime})",
            "lesson_learned_th": lesson,
            "lesson_learned": lesson,
            "is_critical_veto": True,
            "first_loss_ts": now_ts,
            "last_loss_ts": now_ts,
            "last_loss_bkk": _bkk_time_str(now_ts),
            "recent_tickets": streak_tickets,
            "orders": [
                {
                    "order_id": o.get("order_id"),
                    "broker_ticket": o.get("broker_ticket"),
                    "side": o.get("side"),
                    "loss_usd": round(abs(float(o.get("realized_pnl_usd") or 0.0)), 2),
                    "close_reason": o.get("close_reason"),
                }
                for o in losing_orders
            ],
        }

        self.save_to_disk()
        return self.patterns[sig_key]

    def _generate_lesson_text(
        self,
        side: str,
        regime: str,
        flow_state: str,
        archetype: str,
        close_reason: str,
    ) -> str:
        """Create actionable diagnostic insight in Thai."""
        if regime == "SIDEWAY":
            if "SLOWDOWN" in flow_state or "CONSOLIDATION" in flow_state:
                return (
                    f"⚠️ บทเรียน: เข้า {side} ในช่วงตลาดไซด์เวย์กรอบพักตัวและวอลุ่มชะลอตัว ({flow_state}) "
                    f"ทำให้ราคาขาดโมเมนตัมส่งและโดนกวาดหาง SL ({close_reason}) — ควรลดการเข้าออเดอร์ หรือรอให้เบรคกรอบชัดเจนก่อน"
                )
            return (
                f"⚠️ บทเรียน: การเปิด {side} ในสภาวะไซด์เวย์มีความเสี่ยงติดกรอบสะบัด — "
                f"แนะนำให้ใช้ระยะ TP สั้นกระชับ (สไนเปอร์เก็บไว) และห้ามถือแช่ข้ามรอบ"
            )
        else:
            return (
                f"⚠️ บทเรียน: เปิด {side} ทวนโมเมนตัมหรือเข้าจังหวะปลายคลื่น ({archetype}) "
                f"ทำให้ตลาดพักตัวแรงจนชน SL ({close_reason}) — รอบหน้าต้องรอสัญญาณแท่งเทียนกลับตัวยืนยันก่อน"
            )

    def evaluate_setup_risk(
        self,
        trade_setup: Dict[str, Any],
        structure_res: Optional[Dict[str, Any]] = None,
        symbol: str = "XAUUSD",
    ) -> Dict[str, Any]:
        """Cross-reference a proposed new trade setup against memorized loss patterns.
        Returns risk verdict and advisory information."""
        if not self.patterns:
            return {"is_high_risk": False, "match_found": False, "risk_score": 0}

        now_ts = int(time.time())
        side = str(trade_setup.get("side") or "BUY").upper()
        sym = str(symbol or trade_setup.get("symbol") or "XAUUSD").upper()
        setup_conf = int(trade_setup.get("confluence_score") or 0)

        st = structure_res or {}
        trend = st.get("trend") or {}
        if isinstance(trend, dict):
            regime = "SIDEWAY" if trade_setup.get("is_sideway") else str(trend.get("regime") or "TRENDING").upper()
            if "RANGING" in regime or "CHOP" in regime:
                regime = "SIDEWAY"
        else:
            regime = "SIDEWAY" if trade_setup.get("is_sideway") else "TRENDING"
        vf = st.get("volume_flow") or {}
        flow_state = str(vf.get("flow_state") or "NORMAL").upper()

        # Short anti-revenge-trade cooldown (90s) only — NEVER freeze the whole system for 30-45 minutes!
        # Background Adaptive Auto-Tuning (lot_scale + confluence + SL/TP) handles post-loss adaptation continuously.
        STREAK_COOLDOWN_SEC = 90
        PATTERN_TTL_SEC = 900  # 15 minutes soft memory window

        # 1. Recent Losing-Streak Guard (brief 90s cooldown for weak setups; never hard-freezes trading!)
        for k, p in self.patterns.items():
            if p.get("is_consecutive_streak") or p.get("is_critical_veto"):
                last_ts = min(int(p.get("last_loss_ts") or 0), now_ts)
                if self.last_reset_ts > 0 and last_ts <= self.last_reset_ts:
                    continue
                age_sec = max(0, now_ts - last_ts)
                if age_sec > PATTERN_TTL_SEC:
                    continue
                if p.get("symbol") == sym and p.get("side") == side and p.get("market_regime") == regime:
                    is_block = bool(age_sec < STREAK_COOLDOWN_SEC and setup_conf < 80)
                    return {
                        "match_found": True,
                        "is_high_risk": is_block,
                        "risk_score": 78 if is_block else 55,
                        "pattern_key": k,
                        "pattern_label_th": p.get("pattern_label_th"),
                        "loss_count": p.get("total_losses", 3),
                        "total_loss_usd": p.get("total_loss_usd", 0.0),
                        "lesson_learned_th": p.get("lesson_learned_th"),
                        "warning_message": (
                            f"🧠 เพิ่งแพ้ต่อเนื่อง ({p.get('pattern_label_th')}) — พักจังหวะสั้นๆ {max(1, STREAK_COOLDOWN_SEC - age_sec)} วินาทีแล้วปรับแผนต่อ"
                            if is_block
                            else f"🧬 ปรับขนาด Lot และกรองจังหวะอัตโนมัติหลังประวัติแพ้ ({p.get('pattern_label_th')})"
                        ),
                        "warning": f"🧠 สภาวะนี้เคยมีประวัติแพ้ ({p.get('pattern_label_th')})",
                    }

        sig_key = f"{sym}_{side}_{regime}_{flow_state}".replace(" ", "_")
        matched_pat = self.patterns.get(sig_key)
        if matched_pat:
            last_ts = min(int(matched_pat.get("last_loss_ts") or 0), now_ts)
            if self.last_reset_ts > 0 and last_ts <= self.last_reset_ts:
                matched_pat = None
            else:
                age_sec = max(0, now_ts - last_ts)
                if age_sec > PATTERN_TTL_SEC:
                    matched_pat = None

        if matched_pat and matched_pat.get("total_losses", 0) >= 3:
            loss_count = int(matched_pat["total_losses"])
            loss_usd = float(matched_pat["total_loss_usd"])
            label = matched_pat.get("pattern_label_th", sig_key)
            lesson = matched_pat.get("lesson_learned_th", "")

            # Soft caution only — never permanently lock out trading!
            risk_score = min(82, 48 + (loss_count * 6))
            is_high_risk = False

            return {
                "match_found": True,
                "is_high_risk": is_high_risk,
                "risk_score": risk_score,
                "pattern_key": sig_key,
                "pattern_label_th": label,
                "loss_count": loss_count,
                "total_loss_usd": loss_usd,
                "lesson_learned_th": lesson,
                "warning_message": (
                    f"🧠 AI เตือนรูปแบบนี้เคยแพ้ {loss_count} ครั้งล่าสุด ({label}, -${loss_usd:.2f}) — "
                    + ("งดเข้าไม้เนื่องจากคะแนนสัญญาณยังไม่สูงพอ" if is_high_risk else "ผ่านเกณฑ์ด้วยความระมัดระวัง (Confluence สูง)")
                ),
                "warning": (
                    f"🧠 AI เตือนรูปแบบนี้เคยแพ้ {loss_count} ครั้งล่าสุด ({label}, -${loss_usd:.2f})"
                ),
            }

        return {
            "match_found": False,
            "is_high_risk": False,
            "risk_score": 0,
        }

    def run_deep_reanalysis(
        self,
        symbol: str = "XAUUSD",
        timeframe: str = "M5",
        structure: Optional[Dict[str, Any]] = None,
        mtf: Optional[Dict[str, Any]] = None,
        macro: Optional[Dict[str, Any]] = None,
        account_balance: float = 5000.0,
    ) -> Dict[str, Any]:
        """Perform comprehensive post-mortem deep re-analysis when circuit breaker trips
        or when multiple consecutive losses occur."""
        now_ts = int(time.time())
        st = structure or {}
        tr = st.get("trend") or {}
        vf = st.get("volume_flow") or {}

        # 1. Failure analysis from history
        sym_losses = [
            l for l in self.loss_history
            if l.get("symbol", "XAUUSD").upper() == symbol.upper()
        ]
        total_recent_losses = len(sym_losses)
        recent_loss_usd = round(sum(abs(float(l.get("realized_pnl_usd", 0.0))) for l in sym_losses[-5:]), 2)

        # 2. Market Regime diagnosis
        if isinstance(tr, dict):
            regime = str(tr.get("regime") or "UNKNOWN").upper()
            bias = str(tr.get("bias") or st.get("current_bias") or "NEUTRAL").upper()
        else:
            regime = "TRENDING"
            bias = str(tr or "NEUTRAL").upper()
        flow = str(vf.get("flow_state") or "NORMAL").upper()
        atr = float(st.get("atr14") or 2.5)

        # 3. Formulate deep diagnostic conclusions
        findings = []
        recommendations = []

        if "RANGING" in regime or "CHOP" in regime:
            findings.append("ตลาดกำลังอยู่ในสภาวะสะสมตัวไร้ทิศทาง (Choppy Range / Consolidation) ไม่เหมาะกับการถือออเดอร์ตามเทรนด์")
            recommendations.append("ปรับโหมดเป็นสไนเปอร์เก็บกำไรสั้น ($0.40 - $0.60) หรือหยุดเทรดรอการ Breakout หลุดกรอบ")
        else:
            findings.append(f"โครงสร้างหลักยังคงเป็นแนวโน้ม {bias} แต่วอลุ่มโมเมนตัมเริ่มแสดงอาการ {flow}")

        if total_recent_losses > 0:
            top_bad_patterns = sorted(self.patterns.values(), key=lambda p: p.get("total_losses", 0), reverse=True)[:2]
            for p in top_bad_patterns:
                findings.append(f"รูปแบบที่พบการเสียเปรียบสูงสุด: {p.get('pattern_label_th')} (แพ้สะสม {p.get('total_losses')} ครั้ง)")

        recommendations.append("ยกระดับเกณฑ์ Confluence Score ขั้นต่ำสำหรับการเปิดไม้ใหม่เป็นอย่างน้อย 80 คะแนน")
        recommendations.append("กำหนดให้ต้องมีสัญญาณกลับตัวระดับโครงสร้าง (CHoCH หรือ Rejection Wick ชัดเจน) ก่อนเข้าไม้")
        recommendations.append("บังคับตั้ง SL แคบและดึงจุดเข้าให้ใกล้แนวรับต้าน Order Block มากที่สุด")

        report = {
            "symbol": symbol,
            "analyzed_at_ts": now_ts,
            "analyzed_at_bkk": _bkk_time_str(now_ts),
            "timeframe": timeframe,
            "consecutive_losses": self.consecutive_loss_count,
            "consecutive_loss_usd": self.consecutive_loss_usd,
            "drawdown_pct": round((self.consecutive_loss_usd / max(1.0, account_balance)) * 100.0, 2),
            "circuit_breaker_active": self.circuit_breaker_tripped,
            "current_market_regime": regime,
            "current_trend_bias": bias,
            "volume_flow_state": flow,
            "atr14": atr,
            "diagnostic_findings_th": findings,
            "strategic_directives_th": recommendations,
            "strategic_directives": recommendations,
            "summary_th": (
                f"🛡️ รายงานการวิเคราะห์เชิงลึก (Deep Re-Analysis): พอร์ตมีสถิติขาดทุนสะสม {self.consecutive_loss_count} ไม้ "
                f"(-${self.consecutive_loss_usd:.2f}) ระบบได้บันทึกรูปแบบความเสี่ยงลงฐานความจำ "
                f"และเปิดใช้งานตัวกรองป้องกันการแพ้ซ้ำเรียบร้อยแล้ว"
            ),
            "diagnostic_summary_th": (
                f"🛡️ รายงานการวิเคราะห์เชิงลึก (Deep Re-Analysis): พอร์ตมีสถิติขาดทุนสะสม {self.consecutive_loss_count} ไม้ "
                f"(-${self.consecutive_loss_usd:.2f}) ระบบได้บันทึกรูปแบบความเสี่ยงลงฐานความจำ "
                f"และเปิดใช้งานตัวกรองป้องกันการแพ้ซ้ำเรียบร้อยแล้ว"
            ),
            "market_context": {
                "symbol": symbol,
                "timeframe": timeframe,
                "trend": bias,
                "structure_state": regime,
                "flow_state": flow,
            },
            "timestamp": now_ts,
        }

        self.latest_reanalysis = report
        self.save_to_disk()
        return report

    def get_summary_dict(self) -> Dict[str, Any]:
        """Return clean summary dictionary for frontend and state reporting."""
        top_failing = sorted(
            self.patterns.values(),
            key=lambda p: (p.get("total_losses", 0), p.get("total_loss_usd", 0.0)),
            reverse=True,
        )[:10]

        adaptive = self.compute_adaptive_tuning(self.consecutive_loss_count)
        return {
            "total_patterns_remembered": len(self.patterns),
            "consecutive_loss_count": self.consecutive_loss_count,
            "consecutive_losses_count": self.consecutive_loss_count,
            "consecutive_loss_usd": self.consecutive_loss_usd,
            "consecutive_losses_usd": self.consecutive_loss_usd,
            "circuit_breaker_tripped": False,
            "adaptive_tuning": adaptive,
            "patterns": top_failing,
            "top_failing_patterns": top_failing,
            "latest_reanalysis": self.latest_reanalysis,
        }

    get_status = get_summary_dict
