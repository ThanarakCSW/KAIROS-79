#!/usr/bin/env python3
"""Lightweight Online Self-Learning Trade Memory & Pattern Trainer (ai_memory_trainer.py).

Zero-cost, 100% local Online Machine Learning engine for KAIROS-79:
  - Learns continuously from EVERY real closed trade (both WIN and LOSS)
  - Extracts 7 low-cardinality entry-characteristic features + composite fingerprint
    (side × regime, trend vs counter-trend, candlestick category/pattern, volume regime,
    dealing range zone, market session, setup archetype)
  - Dual Online Learning Core:
      1) Time-Decayed Bayesian Smoothed Win/Loss statistics (prevents small-sample bias)
      2) Online Logistic Regression SGD with L2 regularization & bounded weights [-1.25, +1.25]
  - Outputs bounded Confluence Delta [-15, +12] and Lot Multiplier [0.60, 1.25]
    so the system remembers losing entry characteristics, rewards winning ones,
    and NEVER hard-freezes trading permanently.
"""

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BKK_TZ = timezone(timedelta(hours=7))


def _bkk_str(ts: Optional[float] = None) -> str:
    t = ts if ts is not None else time.time()
    return datetime.fromtimestamp(t, BKK_TZ).strftime("%d/%m %H:%M:%S น.")


def _sigmoid(x: float) -> float:
    x_clamped = max(-12.0, min(12.0, x))
    return 1.0 / (1.0 + math.exp(-x_clamped))


def _session_from_ts(ts: Optional[int] = None) -> str:
    t = int(ts or time.time())
    utc_hour = datetime.fromtimestamp(t, timezone.utc).hour
    if 0 <= utc_hour < 7:
        return "ASIAN"
    if 7 <= utc_hour < 12:
        return "LONDON"
    if 12 <= utc_hour < 20:
        return "NEW_YORK"
    return "OFF_HOURS"


FEATURE_LABELS_TH: Dict[str, str] = {
    # Direction vs MTF
    "dir:TREND_ALIGNED": "เข้าตามเทรนด์ใหญ่ MTF",
    "dir:COUNTER_TREND": "เข้าสวนเทรนด์ใหญ่ MTF",
    # Candlestick category
    "candle:WICK_REJECTION": "แท่งเทียนทิ้งไส้ปฏิเสธราคา (Pin Bar / Sweep)",
    "candle:REVERSAL": "แท่งเทียนกลับตัว (Engulfing / Star / Tweezer)",
    "candle:CONTINUATION": "แท่งเทียนส่งแรงต่อเนื่อง (Impulse / Breakout)",
    "candle:NONE": "ไม่มีรูปแบบแท่งเทียนเด่น",
    # Volume regime
    "vol:LOW_VOLUME": "ตลาด Volume เบาบาง (RVOL < 0.8x)",
    "vol:NORMAL_VOLUME": "ตลาด Volume ปกติ",
    "vol:HIGH_VOLUME": "ตลาด Volume สูงมีแรงส่ง",
    "vol:SURGE_VOLUME": "ตลาด Volume พุ่งรุนแรง",
    # Dealing zone
    "zone:DISCOUNT": "โซนของถูก (Discount)",
    "zone:EQUILIBRIUM": "โซนกึ่งกลาง (Equilibrium)",
    "zone:PREMIUM": "โซนของแพง (Premium)",
    # Market session
    "sess:ASIAN": "รอบตลาดเอเชีย (Asian)",
    "sess:LONDON": "รอบตลาดลอนดอน (London)",
    "sess:NEW_YORK": "รอบตลาดนิวยอร์ก (New York)",
    "sess:OFF_HOURS": "ช่วงนอกรอบตลาดหลัก",
    # Regime × Side
    "sr:BUY_TRENDING": "BUY ในตลาดเทรนด์",
    "sr:SELL_TRENDING": "SELL ในตลาดเทรนด์",
    "sr:BUY_SIDEWAY": "BUY ในตลาดไซด์เวย์",
    "sr:SELL_SIDEWAY": "SELL ในตลาดไซด์เวย์",
    "sr:BUY_REVERSAL": "BUY จังหวะกลับตัว",
    "sr:SELL_REVERSAL": "SELL จังหวะกลับตัว",
    # Archetype
    "arch:RAPID_CANDLE_REVERSAL": "Rapid กลับตัวแท่งเทียน",
    "arch:RAPID_SCALP_HARVESTER": "Rapid สเกลป์ตามโครงสร้าง",
    "arch:CANDLE_REVERSAL_SMC": "SMC กลับตัวแท่งเทียน",
    "arch:ORDER_BLOCK_PULLBACK": "SMC Order Block Pullback",
    "arch:SESSION_SWEEP": "SMC Session Liquidity Sweep",
    "arch:MICRO_PULLBACK": "SMC Micro-Pullback",
}


def _human_feature_label(feat_key: str) -> str:
    if feat_key in FEATURE_LABELS_TH:
        return FEATURE_LABELS_TH[feat_key]
    return feat_key.replace(":", " • ")


def _human_combo_label(combo_key: str) -> str:
    parts = combo_key.split("|")
    if len(parts) != 4:
        return combo_key
    side, dir_t, regime, c_cat = parts
    dir_th = "ตามเทรนด์" if dir_t == "TREND_ALIGNED" else "สวนเทรนด์"
    reg_th = {"SIDEWAY": "ไซด์เวย์", "REVERSAL": "กลับตัว", "TRENDING": "เทรนด์"}.get(regime, regime)
    c_th = {
        "WICK_REJECTION": "แท่งทิ้งไส้",
        "REVERSAL": "แท่งกลับตัว",
        "CONTINUATION": "แท่งส่งแรง",
        "NONE": "ไม่มีแท่งยืนยัน",
    }.get(c_cat, c_cat)
    return f"{side} {dir_th} • {reg_th} • {c_th}"


def extract_trade_features(
    trade_setup: Optional[Dict[str, Any]] = None,
    structure_res: Optional[Dict[str, Any]] = None,
    mtf_res: Optional[Dict[str, Any]] = None,
    order_obj: Optional[Dict[str, Any]] = None,
    ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Extract normalized low-cardinality feature tokens + composite entry fingerprint."""
    src = trade_setup or order_obj or {}
    # If order already stored exact entry-time features, reuse them directly!
    if order_obj and isinstance(order_obj.get("ai_features"), dict) and order_obj["ai_features"].get("features"):
        return dict(order_obj["ai_features"])

    side = str(src.get("side") or "BUY").upper()
    if side not in ("BUY", "SELL"):
        side = "BUY"

    # Regime
    raw_reg = str(src.get("market_regime") or "").upper()
    is_sw = bool(src.get("is_sideway", False))
    if not raw_reg and structure_res:
        tr = structure_res.get("trend") or {}
        raw_reg = str(tr.get("regime") or "TRENDING").upper()
    if is_sw or "SIDEWAY" in raw_reg or "RANGING" in raw_reg or "CHOP" in raw_reg:
        regime = "SIDEWAY"
    elif "REVERSAL" in raw_reg:
        regime = "REVERSAL"
    else:
        regime = "TRENDING"

    # Counter-trend vs Trend-aligned
    is_counter = src.get("is_counter_trend")
    if is_counter is None and mtf_res:
        mtf_b = str(mtf_res.get("overall_bias") or "").upper()
        want_b = "BULLISH" if side == "BUY" else "BEARISH"
        is_counter = mtf_b in ("BULLISH", "BEARISH") and mtf_b != want_b
    elif is_counter is None:
        is_counter = (regime == "REVERSAL")
    dir_type = "COUNTER_TREND" if bool(is_counter) else "TREND_ALIGNED"

    # Candlestick behavior category
    csig = src.get("candle_signal") or {}
    c_cat = str(csig.get("category") or "").upper()
    if c_cat not in ("WICK_REJECTION", "REVERSAL", "CONTINUATION"):
        c_pat = str(csig.get("pattern") or "").upper()
        if any(k in c_pat for k in ("PINBAR", "WICK", "DOJI")):
            c_cat = "WICK_REJECTION"
        elif any(k in c_pat for k in ("ENGULF", "STAR", "TWEEZER", "PIERCING", "DARK_CLOUD")):
            c_cat = "REVERSAL"
        elif any(k in c_pat for k in ("IMPULSE", "BREAKOUT", "SOLDIERS", "CROWS")):
            c_cat = "CONTINUATION"
        else:
            c_cat = "NONE"

    # Volume regime
    vscale = src.get("volume_tpsl_scaling") or {}
    v_reg = str(vscale.get("regime") or "").upper()
    if v_reg not in ("LOW_VOLUME", "NORMAL_VOLUME", "HIGH_VOLUME", "SURGE_VOLUME"):
        vf = (structure_res or {}).get("volume_flow") or {}
        rvol = float(vf.get("rvol") or 1.0)
        if rvol < 0.80:
            v_reg = "LOW_VOLUME"
        elif rvol <= 1.30:
            v_reg = "NORMAL_VOLUME"
        elif rvol <= 1.80:
            v_reg = "HIGH_VOLUME"
        else:
            v_reg = "SURGE_VOLUME"

    # Dealing range zone
    d_zone = str(csig.get("dealing_zone") or "").upper()
    if d_zone not in ("DISCOUNT", "EQUILIBRIUM", "PREMIUM") and structure_res:
        dr = (structure_res.get("candle_behavior") or {}).get("dealing_range") or {}
        d_zone = str(dr.get("zone") or "EQUILIBRIUM").upper()
    if d_zone not in ("DISCOUNT", "EQUILIBRIUM", "PREMIUM"):
        d_zone = "EQUILIBRIUM"

    # Strategy archetype
    arch = str(src.get("setup_archetype") or "").upper()
    if not arch:
        sm = str(src.get("strategy_mode") or "").upper()
        arch = "RAPID_SCALP_HARVESTER" if "RAPID" in sm else "ORDER_BLOCK_PULLBACK"

    # Session
    use_ts = ts or int(src.get("filled_ts") or src.get("created_ts") or time.time())
    wall_now = int(time.time())
    if use_ts > wall_now + 60:
        use_ts = wall_now
    sess = _session_from_ts(use_ts)

    features = [
        f"sr:{side}_{regime}",
        f"dir:{dir_type}",
        f"candle:{c_cat}",
        f"vol:{v_reg}",
        f"zone:{d_zone}",
        f"sess:{sess}",
        f"arch:{arch}",
    ]
    combo_key = f"{side}|{dir_type}|{regime}|{c_cat}"
    return {
        "features": features,
        "combo_key": combo_key,
        "combo_label_th": _human_combo_label(combo_key),
        "side": side,
        "regime": regime,
        "dir_type": dir_type,
        "candle_category": c_cat,
        "volume_regime": v_reg,
        "dealing_zone": d_zone,
        "session": sess,
        "archetype": arch,
    }


class OnlineTradeMemoryTrainer:
    """Self-learning trade outcome model combining Bayesian feature stats + Online Logistic SGD."""

    PRIOR_ALPHA = 2.0
    PRIOR_BETA = 2.0
    DECAY_PER_TRADE = 0.985
    LEARNING_RATE = 0.12
    L2_REG = 0.015
    WEIGHT_CAP = 1.25

    def __init__(self, persistence_path: Optional[str] = None) -> None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.persistence_path = persistence_path or os.path.join(base_dir, "ai_trade_memory.json")
        self.bias: float = 0.0
        self.weights: Dict[str, float] = {}
        self.feature_stats: Dict[str, Dict[str, Any]] = {}
        self.combo_stats: Dict[str, Dict[str, Any]] = {}
        self.recorded_trade_ids: List[str] = []
        self.total_trained_trades: int = 0
        self.total_wins: int = 0
        self.total_losses: int = 0
        self.total_net_pnl_usd: float = 0.0
        self.last_trained_ts: int = 0
        self.last_trained_bkk: str = "—"
        self.recent_training_log: List[Dict[str, Any]] = []
        self.load_from_disk()

    def load_from_disk(self) -> None:
        if not os.path.exists(self.persistence_path):
            return
        try:
            with open(self.persistence_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.bias = float(data.get("bias") or 0.0)
            self.weights = {str(k): float(v) for k, v in (data.get("weights") or {}).items()}
            self.feature_stats = dict(data.get("feature_stats") or {})
            self.combo_stats = dict(data.get("combo_stats") or {})
            self.recorded_trade_ids = list(data.get("recorded_trade_ids") or [])[-500:]
            self.total_trained_trades = int(data.get("total_trained_trades") or 0)
            self.total_wins = int(data.get("total_wins") or 0)
            self.total_losses = int(data.get("total_losses") or 0)
            self.total_net_pnl_usd = float(data.get("total_net_pnl_usd") or 0.0)
            self.last_trained_ts = int(data.get("last_trained_ts") or 0)
            self.last_trained_bkk = str(data.get("last_trained_bkk") or "—")
            self.recent_training_log = list(data.get("recent_training_log") or [])[-25:]
        except Exception:
            pass

    def save_to_disk(self) -> None:
        payload = {
            "saved_at_bkk": _bkk_str(),
            "bias": round(self.bias, 5),
            "weights": {k: round(v, 5) for k, v in self.weights.items()},
            "feature_stats": self.feature_stats,
            "combo_stats": self.combo_stats,
            "recorded_trade_ids": self.recorded_trade_ids[-500:],
            "total_trained_trades": self.total_trained_trades,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "total_net_pnl_usd": round(self.total_net_pnl_usd, 2),
            "last_trained_ts": self.last_trained_ts,
            "last_trained_bkk": self.last_trained_bkk,
            "recent_training_log": self.recent_training_log[-25:],
        }
        tmp_path = self.persistence_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.persistence_path)
        except Exception:
            try:
                with open(self.persistence_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    def _apply_time_decay(self) -> None:
        d = self.DECAY_PER_TRADE
        for st in self.feature_stats.values():
            st["eff_wins"] = round(float(st.get("eff_wins", st.get("wins", 0))) * d, 4)
            st["eff_losses"] = round(float(st.get("eff_losses", st.get("losses", 0))) * d, 4)
        for st in self.combo_stats.values():
            st["eff_wins"] = round(float(st.get("eff_wins", st.get("wins", 0))) * d, 4)
            st["eff_losses"] = round(float(st.get("eff_losses", st.get("losses", 0))) * d, 4)

    def _is_trainable_order(self, order: Dict[str, Any]) -> Tuple[bool, str]:
        oid = str(order.get("order_id") or "")
        if not oid or oid.startswith("test_"):
            return False, ""
        reason = str(order.get("close_reason") or "").upper()
        status = str(order.get("status") or "").upper()
        if any(
            skip in reason or skip in status
            for skip in ("TEST_CLEANUP", "SYSTEM_INIT", "CANCELLED", "GHOST", "REJECTED", "MANUAL")
        ):
            return False, ""
        pnl_raw = order.get("realized_pnl_usd")
        if pnl_raw is None:
            return False, ""
        pnl_usd = float(pnl_raw)
        if abs(pnl_usd) < 0.05:
            return False, ""  # Skip flat breakeven scratches
        # Build stable dedupe key (prefer broker_ticket when available)
        bt = order.get("broker_ticket")
        dedupe_key = f"T:{int(bt)}" if bt else f"O:{oid}"
        if dedupe_key in self.recorded_trade_ids:
            return False, ""
        return True, dedupe_key

    def record_trade_outcome(
        self,
        order: Dict[str, Any],
        structure_res: Optional[Dict[str, Any]] = None,
        mtf_res: Optional[Dict[str, Any]] = None,
        save: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Train online model on one real closed trade (WIN or LOSS)."""
        ok, dedupe_key = self._is_trainable_order(order)
        if not ok:
            return None

        pnl_usd = float(order.get("realized_pnl_usd") or 0.0)
        pnl_r = float(order.get("realized_pnl_r") or (1.0 if pnl_usd > 0 else -1.0))
        is_win = pnl_usd > 0.0
        y = 1.0 if is_win else 0.0

        feat_info = extract_trade_features(
            order_obj=order,
            structure_res=structure_res,
            mtf_res=mtf_res,
            ts=int(order.get("filled_ts") or order.get("created_ts") or time.time()),
        )
        feats: List[str] = feat_info["features"]
        combo_key: str = feat_info["combo_key"]
        combo_label_th: str = feat_info["combo_label_th"]

        # 1. Time-decay existing effective counts so newer Gold regimes carry higher weight
        self._apply_time_decay()

        # 2. Online Logistic Regression SGD update (with L2 regularization & R-magnitude weighting)
        logit = self.bias + sum(self.weights.get(f, 0.0) for f in feats)
        p_hat = _sigmoid(logit)
        err = y - p_hat
        r_mag = max(0.65, min(1.75, abs(pnl_r) if abs(pnl_r) > 0.05 else 1.0))
        step = self.LEARNING_RATE * err * r_mag

        self.bias = max(-0.85, min(0.85, self.bias * (1.0 - self.L2_REG) + step * 0.35))
        for f in feats:
            old_w = self.weights.get(f, 0.0)
            new_w = old_w * (1.0 - self.L2_REG) + step
            self.weights[f] = max(-self.WEIGHT_CAP, min(self.WEIGHT_CAP, new_w))

            fst = self.feature_stats.setdefault(
                f,
                {
                    "key": f,
                    "label_th": _human_feature_label(f),
                    "wins": 0,
                    "losses": 0,
                    "eff_wins": 0.0,
                    "eff_losses": 0.0,
                    "net_pnl_usd": 0.0,
                },
            )
            if is_win:
                fst["wins"] = int(fst.get("wins", 0)) + 1
                fst["eff_wins"] = round(float(fst.get("eff_wins", 0.0)) + 1.0, 4)
            else:
                fst["losses"] = int(fst.get("losses", 0)) + 1
                fst["eff_losses"] = round(float(fst.get("eff_losses", 0.0)) + 1.0, 4)
            fst["net_pnl_usd"] = round(float(fst.get("net_pnl_usd", 0.0)) + pnl_usd, 2)
            fst["weight"] = round(self.weights[f], 4)

        # 3. Update composite entry-characteristic fingerprint stats
        cst = self.combo_stats.setdefault(
            combo_key,
            {
                "combo_key": combo_key,
                "label_th": combo_label_th,
                "wins": 0,
                "losses": 0,
                "eff_wins": 0.0,
                "eff_losses": 0.0,
                "net_pnl_usd": 0.0,
                "last_ts": 0,
                "last_bkk": "",
            },
        )
        now_ts = int(time.time())
        if is_win:
            cst["wins"] = int(cst.get("wins", 0)) + 1
            cst["eff_wins"] = round(float(cst.get("eff_wins", 0.0)) + 1.0, 4)
        else:
            cst["losses"] = int(cst.get("losses", 0)) + 1
            cst["eff_losses"] = round(float(cst.get("eff_losses", 0.0)) + 1.0, 4)
        cst["net_pnl_usd"] = round(float(cst.get("net_pnl_usd", 0.0)) + pnl_usd, 2)
        cst["last_ts"] = now_ts
        cst["last_bkk"] = _bkk_str(now_ts)

        # 4. Global counters & audit log
        self.recorded_trade_ids.append(dedupe_key)
        if len(self.recorded_trade_ids) > 500:
            self.recorded_trade_ids = self.recorded_trade_ids[-500:]
        self.total_trained_trades += 1
        if is_win:
            self.total_wins += 1
        else:
            self.total_losses += 1
        self.total_net_pnl_usd = round(self.total_net_pnl_usd + pnl_usd, 2)
        self.last_trained_ts = now_ts
        self.last_trained_bkk = _bkk_str(now_ts)

        log_entry = {
            "ts": now_ts,
            "bkk": self.last_trained_bkk,
            "trade_key": dedupe_key,
            "outcome": "WIN" if is_win else "LOSS",
            "pnl_usd": round(pnl_usd, 2),
            "combo_label_th": combo_label_th,
            "pred_before_pct": round(p_hat * 100.0, 1),
        }
        self.recent_training_log.append(log_entry)
        self.recent_training_log = self.recent_training_log[-25:]

        if save:
            self.save_to_disk()
        return log_entry

    def ingest_order_history(self, order_history: List[Dict[str, Any]]) -> int:
        """Batch-train on any unrecorded real closed trades in order_history (safe idempotent sync)."""
        trained_count = 0
        for o in order_history or []:
            res = self.record_trade_outcome(o, save=False)
            if res is not None:
                trained_count += 1
        if trained_count > 0:
            self.save_to_disk()
        return trained_count

    def predict_setup_quality(
        self,
        trade_setup: Dict[str, Any],
        structure_res: Optional[Dict[str, Any]] = None,
        mtf_res: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Score a candidate trade setup using learned online weights + Bayesian entry-combo memory."""
        feat_info = extract_trade_features(
            trade_setup=trade_setup,
            structure_res=structure_res,
            mtf_res=mtf_res,
            ts=int(time.time()),
        )
        feats: List[str] = feat_info["features"]
        combo_key: str = feat_info["combo_key"]
        combo_label_th: str = feat_info["combo_label_th"]

        # Cold-start maturity ramps gently from 0.0 -> 1.0 across first 12 trained trades
        maturity = min(1.0, self.total_trained_trades / 12.0)

        # 1. Logistic model win probability
        logit = self.bias + sum(self.weights.get(f, 0.0) for f in feats)
        raw_model_prob = _sigmoid(logit)
        # Shrink toward neutral 0.50 during cold-start
        model_prob = 0.50 + (raw_model_prob - 0.50) * maturity

        # 2. Exact entry-characteristic combo Bayesian Win Rate
        cst = self.combo_stats.get(combo_key)
        c_wins = int(cst.get("wins", 0)) if cst else 0
        c_losses = int(cst.get("losses", 0)) if cst else 0
        c_total = c_wins + c_losses
        c_eff_w = float(cst.get("eff_wins", c_wins)) if cst else 0.0
        c_eff_l = float(cst.get("eff_losses", c_losses)) if cst else 0.0
        c_net_usd = float(cst.get("net_pnl_usd", 0.0)) if cst else 0.0
        combo_bayes_prob = (c_eff_w + self.PRIOR_ALPHA) / (c_eff_w + c_eff_l + self.PRIOR_ALPHA + self.PRIOR_BETA)

        # Blend logistic generalization (55%) + exact combo memory (45% when combo seen >= 2 times)
        if c_total >= 2:
            combo_w = min(0.50, 0.25 + 0.05 * c_total)
            blended_prob = (model_prob * (1.0 - combo_w)) + (combo_bayes_prob * combo_w)
        else:
            blended_prob = model_prob

        # Bounded adjustments: Confluence [-15, +12], Lot Multiplier [0.60, 1.25]
        edge = blended_prob - 0.50  # roughly [-0.35, +0.35]
        conf_delta = int(round(max(-15.0, min(12.0, edge * 42.0))))
        lot_mult = round(max(0.60, min(1.25, 1.0 + (edge * 1.15))), 2)

        # Additional safety nudge if this EXACT entry combo lost repeatedly (>= 3 losses & < 38% win rate)
        if c_losses >= 3 and combo_bayes_prob < 0.38:
            conf_delta = min(conf_delta, -10)
            lot_mult = min(lot_mult, 0.72)

        if self.total_trained_trades < 3:
            conf_delta = 0
            lot_mult = 1.0
            risk_level = "LEARNING"
            insight_th = (
                f"🧠 AI Memory กำลังสะสมข้อมูลเรียนรู้ ({self.total_trained_trades}/3 ไม้แรก) — ใช้ค่ามาตรฐาน 1.00x"
            )
        elif conf_delta <= -6 or (c_losses >= 3 and combo_bayes_prob < 0.40):
            risk_level = "HIGH_CAUTION"
            insight_th = (
                f"🧠 AI Memory เตือนลักษณะเคยแพ้บ่อย [{combo_label_th}]: "
                f"สถิติชนะ {c_wins}/แพ้ {c_losses} (Net ${c_net_usd:+.2f}, AI Win {blended_prob * 100:.0f}%) "
                f"→ ลดคะแนน {conf_delta:+d} และคุม Lot {lot_mult:.2f}x"
            )
        elif conf_delta >= 5:
            risk_level = "FAVORABLE"
            insight_th = (
                f"🧠 AI Memory ยืนยันท่าทำกำไรดี [{combo_label_th}]: "
                f"สถิติชนะ {c_wins}/แพ้ {c_losses} (Net ${c_net_usd:+.2f}, AI Win {blended_prob * 100:.0f}%) "
                f"→ เสริมคะแนน {conf_delta:+d} และหนุน Lot {lot_mult:.2f}x"
            )
        else:
            risk_level = "NEUTRAL"
            insight_th = (
                f"🧠 AI Memory ประเมินสมดุล [{combo_label_th}]: AI Win {blended_prob * 100:.0f}% "
                f"(เคยชนะ {c_wins}/แพ้ {c_losses}) → ปรับคะแนน {conf_delta:+d} / Lot {lot_mult:.2f}x"
            )

        return {
            "ai_win_prob_pct": round(blended_prob * 100.0, 1),
            "model_win_prob_pct": round(model_prob * 100.0, 1),
            "combo_bayes_win_pct": round(combo_bayes_prob * 100.0, 1),
            "confluence_delta": conf_delta,
            "lot_multiplier": lot_mult,
            "risk_level": risk_level,
            "maturity_pct": round(maturity * 100.0, 0),
            "total_trained_trades": self.total_trained_trades,
            "combo_key": combo_key,
            "combo_label_th": combo_label_th,
            "combo_wins": c_wins,
            "combo_losses": c_losses,
            "combo_net_pnl_usd": round(c_net_usd, 2),
            "features": feat_info,
            "insight_th": insight_th,
        }

    def reset_memory(self) -> Dict[str, Any]:
        """Wipe all learned weights and stats for a clean re-training start."""
        prev_trades = self.total_trained_trades
        self.bias = 0.0
        self.weights = {}
        self.feature_stats = {}
        self.combo_stats = {}
        self.recorded_trade_ids = []
        self.total_trained_trades = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_net_pnl_usd = 0.0
        self.last_trained_ts = int(time.time())
        self.last_trained_bkk = _bkk_str()
        self.recent_training_log = []
        self.save_to_disk()
        return {"cleared_trained_trades": prev_trades}

    def get_dashboard_summary(self) -> Dict[str, Any]:
        """Structured summary for the Dashboard AI Self-Learning Panel."""
        total = max(0, self.total_trained_trades)
        win_rate = round((self.total_wins / total) * 100.0, 1) if total > 0 else 0.0
        maturity_pct = round(min(1.0, total / 12.0) * 100.0, 0)

        combos_enriched: List[Dict[str, Any]] = []
        for c in self.combo_stats.values():
            w = int(c.get("wins", 0))
            l = int(c.get("losses", 0))
            tot_c = w + l
            if tot_c <= 0:
                continue
            ew = float(c.get("eff_wins", w))
            el = float(c.get("eff_losses", l))
            b_wr = round(((ew + self.PRIOR_ALPHA) / (ew + el + self.PRIOR_ALPHA + self.PRIOR_BETA)) * 100.0, 1)
            combos_enriched.append(
                {
                    "combo_key": c.get("combo_key"),
                    "label_th": c.get("label_th") or _human_combo_label(str(c.get("combo_key") or "")),
                    "wins": w,
                    "losses": l,
                    "total": tot_c,
                    "raw_win_pct": round((w / tot_c) * 100.0, 1),
                    "bayes_win_pct": b_wr,
                    "net_pnl_usd": round(float(c.get("net_pnl_usd", 0.0)), 2),
                    "last_bkk": c.get("last_bkk", "—"),
                }
            )

        top_losing = sorted(
            [c for c in combos_enriched if c["losses"] >= 1 and (c["net_pnl_usd"] < 0 or c["bayes_win_pct"] < 50.0)],
            key=lambda x: (x["bayes_win_pct"], x["net_pnl_usd"], -x["losses"]),
        )[:5]

        top_winning = sorted(
            [c for c in combos_enriched if c["wins"] >= 1 and (c["net_pnl_usd"] > 0 or c["bayes_win_pct"] >= 50.0)],
            key=lambda x: (-x["bayes_win_pct"], -x["net_pnl_usd"], -x["wins"]),
        )[:5]

        feat_list = [
            {
                "key": k,
                "label_th": _human_feature_label(k),
                "weight": round(float(w), 3),
                "wins": int((self.feature_stats.get(k) or {}).get("wins", 0)),
                "losses": int((self.feature_stats.get(k) or {}).get("losses", 0)),
                "net_pnl_usd": round(float((self.feature_stats.get(k) or {}).get("net_pnl_usd", 0.0)), 2),
            }
            for k, w in self.weights.items()
        ]
        top_neg_features = sorted([f for f in feat_list if f["weight"] < -0.02], key=lambda x: x["weight"])[:4]
        top_pos_features = sorted([f for f in feat_list if f["weight"] > 0.02], key=lambda x: -x["weight"])[:4]

        return {
            "total_trained_trades": total,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "overall_win_rate_pct": win_rate,
            "total_net_pnl_usd": round(self.total_net_pnl_usd, 2),
            "maturity_pct": maturity_pct,
            "last_trained_bkk": self.last_trained_bkk,
            "top_losing_patterns": top_losing,
            "top_winning_patterns": top_winning,
            "top_negative_features": top_neg_features,
            "top_positive_features": top_pos_features,
            "recent_training_log": list(reversed(self.recent_training_log[-8:])),
        }
