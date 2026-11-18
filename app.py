#!/usr/bin/env python3
"""KAIROS-79 (K79): Precision Gold Timing & Risk System — Web & WebSocket Server (app.py).

Zero-extra-dependency Async HTTP + RFC 6455 WebSocket Server:
  - Runs directly with `python3 app.py` (uses Python 3 standard library asyncio)
  - Serves the interactive Web UI (`web/index.html`, `web/style.css`, `web/app.js`)
  - Connects to Real-Time Free Gold Feeds in Live Mode:
      1) Swissquote Institutional Spot XAU/USD Bid/Ask/Spread feed (every 2s)
      2) Binance Live Trade WebSocket (`wss://stream.binance.com:9443/ws/paxgusdt@trade`)
      3) Multi-timeframe OHLCV candles (M5, M15, H1, H4) aligned to Spot XAU/USD
  - Automatically re-evaluates SMC + Monte Carlo + AI Risk Advisor when a candle closes
"""

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import signal
import struct
import subprocess
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("KAIROS-79")

BKK_TZ = timezone(timedelta(hours=7))


def _fmt_bkk(ts: Optional[int] = None) -> str:
    t = int(ts if ts is not None else time.time())
    return datetime.fromtimestamp(t, BKK_TZ).strftime("%d/%m %H:%M น.")

from advisor.ai_evaluator import (
    evaluate_chart_with_gemini_or_fallback,
    finalize_advisory,
)
from engine.data_feed import (
    MarketDataHub,
    fetch_live_spot_xauusd,
    fetch_mt5_live_tick,
    resample_bars,
    stream_binance_gold_ws,
    stream_tradingview_gold_ws,
)
from engine.news_feed import evaluate_news_window
from engine.ea_bridge import EABridgeManager
from engine.order_lifecycle import OrderLifecycleManager
from engine.loss_learning import LossPatternMemoryManager
from engine.ai_memory_trainer import OnlineTradeMemoryTrainer
from engine.risk_gate import evaluate_risk_and_size
from engine.signal_tracker import evaluate_signal_performance
from engine.simulate import optimize_setup_for_win_prob, run_simulation
from engine.smc import analyze_smc
from engine.structure import (
    analyze_multi_timeframe,
    analyze_structure,
    build_market_flow_narrative,
)

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
WEB_DIR = os.path.join(ROOT_DIR, "web")
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_config(cfg: Dict[str, Any]) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)


class TradingAssistantCore:
    """Coordinates MarketDataHub, Deterministic Engines, and Automated AI Advisor."""

    def __init__(self, symbol: str = "XAUUSD", persistence_path: Optional[str] = None) -> None:
        self.symbol = (symbol or "XAUUSD").upper()
        self.config = load_config()
        self.hub = MarketDataHub()
        self.hub.preferred_symbol = self.symbol
        is_btc = "BTC" in self.symbol
        if persistence_path:
            p_path = persistence_path
        elif os.environ.get("K79_TEST_MODE") == "1":
            import tempfile
            p_path = os.path.join(tempfile.gettempdir(), f"k79_test_{self.symbol}_{os.getpid()}_{int(time.time()*1000)}.json")
        else:
            persistence_file = "active_order_btc.json" if is_btc else "active_order.json"
            p_path = os.path.join(ROOT_DIR, persistence_file)
        self.order_manager = OrderLifecycleManager(persistence_path=p_path)
        loss_mem_file = "loss_patterns_btc.json" if is_btc else "loss_patterns.json"
        self.loss_memory = LossPatternMemoryManager(persistence_path=os.path.join(ROOT_DIR, loss_mem_file))
        ai_mem_file = "ai_trade_memory_btc.json" if is_btc else "ai_trade_memory.json"
        self.ai_memory = OnlineTradeMemoryTrainer(persistence_path=os.path.join(ROOT_DIR, ai_mem_file))
        try:
            self.ai_memory.ingest_order_history(getattr(self.order_manager, "order_history", []))
        except Exception:
            pass
        self.ea_bridge = EABridgeManager(CONFIG_PATH)
        self.timeframe = str(self.config.get("default_timeframe") or "M5").upper()
        self.config["robot_scalper"] = {"enabled": False}
        if os.environ.get("K79_TEST_MODE") == "1":
            self.config = dict(self.config)
            self.config["rapid_fire_test"] = dict(self.config.get("rapid_fire_test") or {}, enabled=False)
            self.config["simulation_replays"] = 2500
            self.trading_style = "intraday"
        else:
            self.trading_style = str(self.config.get("trading_style") or "intraday").lower()
            if self.trading_style not in ("intraday", "scalping", "micro_scalp"):
                self.trading_style = "intraday"
        self.last_state: Optional[Dict[str, Any]] = None
        self.last_alert_fingerprint: Optional[str] = None
        self.sideway_guard: Dict[str, Any] = {
            "status": "NORMAL",  # NORMAL | PENDING_USER | CONTINUE_ALLOWED | PAUSED_WAIT_BREAKOUT
            "is_sideway_now": False,
            "prompt_id": 0,
            "prompt_ts": 0,
            "deadline_ts": 0,
            "countdown_sec": 30,
            "sideway_confirm_count": 0,
            "breakout_confirm_count": 0,
            "allow_until_ts": 0,
            "reset_grace_until_ts": 0,
            "just_resumed_ts": 0,
            "just_resumed_note_th": "",
            "reason_th": "",
            "regime": "",
            "ema_alignment": "",
            "range_ratio": 1.0,
        }

    def update_sideway_guard(self, structure: Dict[str, Any], now_ts: int) -> Dict[str, Any]:
        """Track Sideway regime, pause new orders & prompt user (30s auto-pause), and auto-resume on Trend Breakout."""
        sg = self.sideway_guard
        tr_info = structure.get("trend") or {}
        tr_reg = str(tr_info.get("regime") or "").upper()
        tr_bias = str(tr_info.get("bias") or "").upper()
        ema_al = str(tr_info.get("ema_alignment") or "").upper()
        vf = structure.get("volume_flow") or {}
        rng_ratio = float(vf.get("range_expansion_ratio") if vf.get("range_expansion_ratio") is not None else 1.0)
        flow_st = str(vf.get("flow_state") or "").upper()

        is_sideway_now = bool(
            "RANGING" in tr_reg
            or "SIDEWAY" in tr_reg
            or tr_bias in ("NEUTRAL", "CHOP", "SIDEWAY")
            or ema_al == "MIXED"
            or "CONSOLIDATION" in flow_st
            or (vf.get("range_expansion_ratio") is not None and rng_ratio < 0.72)
        )

        reasons: List[str] = []
        if "RANGING" in tr_reg or "SIDEWAY" in tr_reg:
            reasons.append(f"Regime: {tr_reg}")
        if ema_al == "MIXED":
            reasons.append("EMA พันกัน (MIXED)")
        if tr_bias in ("NEUTRAL", "CHOP", "SIDEWAY"):
            reasons.append(f"ทิศทางไม่ชัดเจน ({tr_bias})")
        if "CONSOLIDATION" in flow_st:
            reasons.append("ตลาดบีบกรอบพักตัว (CONSOLIDATION)")
        if vf.get("range_expansion_ratio") is not None and rng_ratio < 0.72:
            reasons.append(f"กรอบแท่งแคบกว่าค่าเฉลี่ย ({rng_ratio:.2f}x)")

        sg["is_sideway_now"] = is_sideway_now
        sg["regime"] = tr_reg or "UNKNOWN"
        sg["ema_alignment"] = ema_al or "UNKNOWN"
        sg["range_ratio"] = round(rng_ratio, 2)
        sg["reason_th"] = " • ".join(reasons) if reasons else "กราฟเคลื่อนไหวเป็นเทรนด์ปกติ"

        if is_sideway_now:
            sg["sideway_confirm_count"] = min(10, int(sg.get("sideway_confirm_count", 0)) + 1)
            sg["breakout_confirm_count"] = 0
        else:
            sg["breakout_confirm_count"] = min(10, int(sg.get("breakout_confirm_count", 0)) + 1)
            sg["sideway_confirm_count"] = 0

        status = str(sg.get("status") or "NORMAL")
        in_grace = now_ts < int(sg.get("reset_grace_until_ts") or 0)

        if status == "NORMAL":
            if is_sideway_now and sg["sideway_confirm_count"] >= 2 and not in_grace and self.ea_bridge.auto_trading_enabled:
                sg["status"] = "PENDING_USER"
                sg["prompt_id"] = int(sg.get("prompt_id") or 0) + 1
                sg["prompt_ts"] = now_ts
                sg["deadline_ts"] = now_ts + 30
        elif status == "PENDING_USER":
            # If market already broke cleanly into a trend while waiting, return to NORMAL automatically
            if not is_sideway_now and sg["breakout_confirm_count"] >= 2:
                sg["status"] = "NORMAL"
                sg["prompt_ts"] = 0
                sg["deadline_ts"] = 0
            # Server-side 30s Safe Default: if user did not click within 30s -> auto-pause & wait for breakout!
            elif int(sg.get("deadline_ts") or 0) > 0 and now_ts >= int(sg["deadline_ts"]):
                sg["status"] = "PAUSED_WAIT_BREAKOUT"
                sg["prompt_ts"] = 0
                sg["deadline_ts"] = 0
        elif status == "CONTINUE_ALLOWED":
            # Remain allowed until market leaves Sideway into trend (>= 2 scans) OR 25-min allow window expires
            if not is_sideway_now and sg["breakout_confirm_count"] >= 2:
                sg["status"] = "NORMAL"
                sg["allow_until_ts"] = 0
            elif is_sideway_now and int(sg.get("allow_until_ts") or 0) > 0 and now_ts >= int(sg["allow_until_ts"]):
                sg["status"] = "PENDING_USER"
                sg["prompt_id"] = int(sg.get("prompt_id") or 0) + 1
                sg["prompt_ts"] = now_ts
                sg["deadline_ts"] = now_ts + 30
        elif status == "PAUSED_WAIT_BREAKOUT":
            # Option 1: Auto-Resume trading as soon as chart breaks out of Sideway into a clear Trend!
            if not is_sideway_now and sg["breakout_confirm_count"] >= 2:
                sg["status"] = "NORMAL"
                sg["prompt_ts"] = 0
                sg["deadline_ts"] = 0
                sg["just_resumed_ts"] = now_ts
                sg["just_resumed_note_th"] = (
                    f"🚀 กราฟหลุดกรอบ Sideway เข้าสู่เทรนด์ชัดเจน ({tr_reg or tr_bias}) — ระบบกลับมาออกไม้อัตโนมัติแล้ว!"
                )

        rem_sec = max(0, int(sg.get("deadline_ts") or 0) - now_ts) if sg["status"] == "PENDING_USER" else 0
        out = dict(sg)
        out["remaining_sec"] = rem_sec
        out["block_new_orders"] = sg["status"] in ("PENDING_USER", "PAUSED_WAIT_BREAKOUT")
        out["server_ts"] = now_ts
        return out

    def apply_sideway_decision(self, decision: str) -> Dict[str, Any]:
        """Handle user decision from the 30s Sideway Popup or Card 3 quick toggle."""
        dec = str(decision or "pause").strip().lower()
        now_ts = int(time.time())
        sg = self.sideway_guard
        if dec in ("continue", "resume"):
            if sg.get("is_sideway_now"):
                sg["status"] = "CONTINUE_ALLOWED"
                sg["allow_until_ts"] = now_ts + 1500  # 25 min uninterrupted during this Sideway
            else:
                sg["status"] = "NORMAL"
                sg["allow_until_ts"] = 0
            sg["prompt_ts"] = 0
            sg["deadline_ts"] = 0
            msg = "▶️ รับทราบครับ! อนุญาตให้ระบบออกไม้ทำงานต่อในช่วง Sideway นี้"
        else:
            sg["status"] = "PAUSED_WAIT_BREAKOUT"
            sg["prompt_ts"] = 0
            sg["deadline_ts"] = 0
            sg["allow_until_ts"] = 0
            msg = "⏸️ หยุดการออกไม้ใหม่ชั่วคราวแล้ว — เฝ้ารอกราฟหลุดกรอบ Sideway เพื่อกลับมาออกไม้อัตโนมัติ"
        out = dict(sg)
        out["remaining_sec"] = 0
        out["block_new_orders"] = sg["status"] in ("PENDING_USER", "PAUSED_WAIT_BREAKOUT")
        out["message"] = msg
        return out

    def _apply_ai_memory_to_setup(
        self,
        setup: Optional[Dict[str, Any]],
        structure: Dict[str, Any],
        mtf: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Score a setup with OnlineTradeMemoryTrainer and apply bounded confluence & lot adjustments."""
        if not setup:
            return {}
        pred = self.ai_memory.predict_setup_quality(
            trade_setup=setup,
            structure_res=structure,
            mtf_res=mtf,
        )
        conf_delta = int(pred.get("confluence_delta") or 0)
        lot_mult = float(pred.get("lot_multiplier") or 1.0)
        base_conf = int(setup.get("confluence_score") or 65)
        setup["confluence_score"] = max(20, min(98, base_conf + conf_delta))
        base_alloc = float(setup.get("allocation_factor") or 1.0)
        setup["allocation_factor"] = round(max(0.40, min(1.40, base_alloc * lot_mult)), 2)
        setup["ai_memory_eval"] = pred
        setup["ai_features"] = pred.get("features") or {}
        if pred.get("insight_th"):
            factors = list(setup.get("confluence_factors") or [])
            if pred["insight_th"] not in factors:
                factors.append(pred["insight_th"])
            setup["confluence_factors"] = factors
        return pred

    @property
    def broker_symbol(self) -> str:
        """Return live broker-mapped symbol from connected MT5 EA (e.g. GOLD instead of XAUUSD)."""
        hb_sym = (self.ea_bridge.account_info.get("symbol") or "").strip()
        if hb_sym:
            if self.symbol == "XAUUSD" and any(k in hb_sym.upper() for k in ("GOLD", "XAU")):
                return hb_sym
            if "BTC" in self.symbol and any(k in hb_sym.upper() for k in ("BTC", "BITCOIN")):
                return hb_sym
        pref_sym = getattr(self.hub, "preferred_symbol", None)
        if pref_sym:
            if self.symbol == "XAUUSD" and any(k in pref_sym.upper() for k in ("GOLD", "XAU")):
                return pref_sym
            if "BTC" in self.symbol and any(k in pref_sym.upper() for k in ("BTC", "BITCOIN")):
                return pref_sym
        return self.symbol

    def toggle_robot_mode(self, enabled: Optional[bool] = None) -> bool:
        """Deprecated: Robot scalper mode has been removed permanently due to high drawdown risk."""
        self.config["robot_scalper"] = {"enabled": False}
        save_config(self.config)
        return False

    def toggle_rapid_fire_mode(self, enabled: Optional[bool] = None) -> bool:
        rf_cfg = self.config.setdefault("rapid_fire_test", {})
        if enabled is None:
            new_val = not bool(rf_cfg.get("enabled", False))
        else:
            new_val = bool(enabled)
        rf_cfg["enabled"] = new_val
        if new_val:
            self.timeframe = "M1"
            self.config["default_timeframe"] = "M1"
            self.ea_bridge.set_auto_trading(True)
            # Cancel any stale pending orders from other modes so test can trade immediately
            if self.order_manager.has_active_order():
                act = self.order_manager.active_order
                if act and act.get("status") == "PENDING":
                    self.order_manager.cancel_active_order(reason="RAPID_FIRE_ACTIVATED")
        else:
            if self.order_manager.has_active_order():
                act = self.order_manager.active_order
                if act and act.get("status") == "PENDING" and act.get("trading_style") == "rapid_fire_test":
                    self.order_manager.cancel_active_order(reason="RAPID_FIRE_DEACTIVATED")
        save_config(self.config)
        return new_val

    def compute_full_state(
        self,
        timeframe: Optional[str] = None,
        force_refresh: bool = False,
        force_ai: bool = False,
        trading_style: Optional[str] = None,
    ) -> Dict[str, Any]:
        tf = (timeframe or self.timeframe).upper()
        self.timeframe = tf
        if trading_style in ("intraday", "scalping", "micro_scalp", "rapid_fire_test"):
            self.trading_style = trading_style
            self.config["trading_style"] = trading_style

        is_rapid = (
            bool((self.config.get("rapid_fire_test") or {}).get("enabled", False))
            and (trading_style is None or trading_style == "rapid_fire_test")
        ) or (trading_style == "rapid_fire_test")

        effective_style = "rapid_fire_test" if is_rapid else self.trading_style

        # In rapid fire mode, prioritize M1 as the primary timeframe for fast execution unless explicitly requested
        if is_rapid and (not timeframe or timeframe == "M5"):
            tf = "M1"
            self.timeframe = "M1"

        is_btc = "BTC" in self.symbol
        feed = self.hub.get_bars(tf, force_refresh=force_refresh, symbol=self.symbol)
        raw_bars = (feed.get("bars") or [])[-600:]
        is_mt5_feed = str(feed.get("source", "")).startswith("mt5_broker")
        offset = 0.0 if is_mt5_feed else float(self.config.get("broker_price_offset_btc" if is_btc else "broker_price_offset") or 0.0)
        def_spread_pts = 1500.0 if is_btc else 25.0
        spread_pts = float(self.config.get("broker_spread_points_btc" if is_btc else "broker_spread_points") or def_spread_pts)
        point_size = float(self.config.get("point_size_btc" if is_btc else "point_size") or 0.01)
        spread_usd = round(spread_pts * point_size, 2)

        # Apply broker offset to displayed chart bars so the chart & zones match MT5 directly
        chart_bars = [
            {
                "t": int(b["t"]),
                "o": round(float(b["o"]) + offset, 2),
                "h": round(float(b["h"]) + offset, 2),
                "l": round(float(b["l"]) + offset, 2),
                "c": round(float(b["c"]) + offset, 2),
                "v": int(b.get("v") or 0),
            }
            for b in raw_bars
        ]

        mtf_bars = self.hub.get_mtf_bars(primary_tf=tf, primary_bars=chart_bars[-300:], offset_usd=offset)
        mtf_bars = {k: v[-300:] for k, v in mtf_bars.items()}
        structure = analyze_structure(chart_bars, timeframe=tf, mtf_bars_by_tf=mtf_bars)
        mtf = analyze_multi_timeframe(mtf_bars, primary_tf=tf, current_price=structure["current_price"])
        macro = self.hub.get_macro_correlation(chart_bars, timeframe=tf)
        smc = analyze_smc(
            chart_bars,
            timeframe=tf,
            spread_usd=spread_usd,
            structure_res=structure,
            mtf_res=mtf,
            macro_res=macro,
            trading_style=effective_style,
            symbol=self.symbol,
        )
        if effective_style == "rapid_fire_test":
            replays_cnt = min(1500, int(self.config.get("simulation_replays_rapid") or 1000))
        elif effective_style == "micro_scalp":
            replays_cnt = min(2500, int(self.config.get("simulation_replays_scalp") or 2500))
        else:
            replays_cnt = min(2500, int(self.config.get("simulation_replays") or 2500))
        bars_ahead_cnt = 8 if effective_style == "rapid_fire_test" else (14 if effective_style == "micro_scalp" else (18 if effective_style == "scalping" else int(self.config.get("simulation_bars_ahead") or 36)))
        sim_seed = int(self.config.get("simulation_seed") or 19)
        min_win_prob = float(self.config.get("min_win_prob_pct") or 50.0)
        self.config["min_win_prob_pct"] = min_win_prob

        opt_setup, sim = optimize_setup_for_win_prob(
            bars=chart_bars,
            trade_setup=smc["trade_setup"],
            structure_res=structure,
            replays=replays_cnt,
            bars_ahead=bars_ahead_cnt,
            seed=sim_seed,
            min_win_prob_pct=min_win_prob,
        )
        smc["trade_setup"] = opt_setup
        pns_cfg = self.config.get("pre_news_shield") or {}
        pre_mins = int(pns_cfg.get("minutes_before") or 15)
        news = evaluate_news_window(
            blackout_minutes=int(self.config.get("news_blackout_minutes") or 30),
            pre_news_minutes=pre_mins,
        )
        market_flow = build_market_flow_narrative(
            structure=structure,
            mtf=mtf,
            macro=macro,
            smc=smc,
            news=news,
        )
        structure["volume_flow"] = market_flow
        if "ema_series" in structure and isinstance(structure["ema_series"], dict):
            for k in ("ema20", "ema50", "ema200"):
                if k in structure["ema_series"] and isinstance(structure["ema_series"][k], list):
                    structure["ema_series"][k] = structure["ema_series"][k][-200:]

        # Only invoke Gemini AI when scanning for a new trade entry (or when explicitly forced),
        # saving AI resources when an order is already active and managed by deterministic lifecycle rules.
        if effective_style != "rapid_fire_test" and (force_ai or not self.order_manager.has_active_order()):
            chosen_setup, gemini_raw = evaluate_chart_with_gemini_or_fallback(
                bars=chart_bars,
                structure=structure,
                smc=smc,
                baseline_sim=sim,
                news=news,
                config=self.config,
                spread_usd=spread_usd,
                force_call=force_ai,
                mtf=mtf,
                macro=macro,
            )
            if chosen_setup is not smc["trade_setup"]:
                chosen_setup["trading_style"] = self.trading_style
                opt_gemini_setup, sim = optimize_setup_for_win_prob(
                    bars=chart_bars,
                    trade_setup=chosen_setup,
                    structure_res=structure,
                    replays=replays_cnt,
                    bars_ahead=bars_ahead_cnt,
                    seed=sim_seed,
                    min_win_prob_pct=min_win_prob,
                )
                smc["trade_setup"] = opt_gemini_setup
        else:
            gemini_raw = None

        now_ts_eval = int(time.time())
        now_day = now_ts_eval // 86400
        ea_acc = self.ea_bridge.account_info or {}
        is_mt5_live = bool(self.ea_bridge.is_connected and float(ea_acc.get("balance") or 0.0) > 0.0)
        live_bal = float(ea_acc.get("balance") or 0.0) if is_mt5_live else float(self.config.get("account_balance") or 5000.0)
        live_eq = float(ea_acc.get("equity") or live_bal) if is_mt5_live else live_bal

        # Anchored Balance at the exact moment user configured Profit/Loss targets
        anchor_bal = float(self.config.get("pnl_anchor_balance") or 0.0)
        anchor_ts = int(self.config.get("pnl_anchor_ts") or 0)
        anchor_bkk = str(self.config.get("pnl_anchor_bkk") or "")
        if anchor_bal <= 0.0 and live_bal > 0.0:
            anchor_bal = round(live_bal, 2)
            anchor_ts = now_ts_eval
            anchor_bkk = _fmt_bkk(anchor_ts)
            self.config["pnl_anchor_balance"] = anchor_bal
            self.config["pnl_anchor_ts"] = anchor_ts
            self.config["pnl_anchor_bkk"] = anchor_bkk
            save_config(self.config)

        # Calculate session PnL cleanly from Anchored Balance (not polluted by old deals before setup)
        if is_mt5_live and anchor_bal > 0.0:
            daily_realized_pnl_usd = round(live_bal - anchor_bal, 2)
            session_equity_pnl_usd = round(live_eq - anchor_bal, 2)
        else:
            since_ts = anchor_ts if anchor_ts > 0 else (now_day * 86400)
            today_closed = [
                o for o in getattr(self.order_manager, "order_history", [])
                if int(o.get("closed_ts") or o.get("created_ts") or 0) >= since_ts
                and not str(o.get("order_id") or "").startswith("test_")
                and o.get("close_reason") not in ("TEST_CLEANUP", "SYSTEM_INIT")
                and bool(o.get("broker_ticket") or o.get("broker_deal_id"))
            ]
            daily_realized_pnl_usd = round(sum(float(o.get("realized_pnl_usd") or 0.0) for o in today_closed), 2)
            session_equity_pnl_usd = daily_realized_pnl_usd

        raw_target_cfg = self.config.get("daily_profit_target_usd")
        daily_profit_target_usd = float(raw_target_cfg) if raw_target_cfg is not None else 10.0
        daily_target_enabled = bool(self.config.get("daily_profit_target_enabled", True))
        effective_profit_usd = max(daily_realized_pnl_usd, session_equity_pnl_usd)
        target_balance_goal = round(anchor_bal + daily_profit_target_usd, 2)
        daily_profit_reached = bool(
            daily_target_enabled
            and daily_profit_target_usd > 0.0
            and effective_profit_usd >= daily_profit_target_usd
        )

        # Record ONLY genuine recent newly closed losing trades (within 30 min & after anchor_ts)
        # Never re-ingest stale historical MT5 deals on server restart or after user System Reset!
        rec_loss_cutoff = max(anchor_ts, int(getattr(self.loss_memory, "last_reset_ts", 0) or 0), now_ts_eval - 1800)
        recorded_any_loss = False
        for o in getattr(self.order_manager, "order_history", [])[-20:]:
            o_closed_ts = int(o.get("closed_ts") or o.get("created_ts") or 0)
            if (
                not o.get("loss_pattern_recorded")
                and o_closed_ts >= rec_loss_cutoff
                and float(o.get("realized_pnl_usd") or 0.0) < -0.01
            ):
                o["loss_pattern_recorded"] = True
                self.loss_memory.record_loss_order(o, market_context={"structure": structure})
                recorded_any_loss = True
        if recorded_any_loss:
            self.order_manager.save_to_disk()

        # Anchored Portfolio Loss Limit (always calculated from Balance at the time user set it!)
        raw_loss_pct = self.config.get("max_consecutive_loss_pct")
        threshold_pct = float(raw_loss_pct) if raw_loss_pct is not None else 20.0
        raw_max_loss_usd = self.config.get("max_loss_usd")
        max_loss_usd = (
            round(float(raw_max_loss_usd), 2)
            if raw_max_loss_usd is not None and float(raw_max_loss_usd) > 0.0
            else round(anchor_bal * (threshold_pct / 100.0), 2)
        )
        stop_floor_balance = round(max(0.0, anchor_bal - max_loss_usd), 2)
        worst_val = min(live_bal, live_eq)
        anchor_drawdown_usd = round(max(0.0, anchor_bal - worst_val), 2)
        anchor_drawdown_pct = round((anchor_drawdown_usd / max(1.0, anchor_bal)) * 100.0, 2)
        anchor_loss_tripped = bool(
            max_loss_usd > 0.0
            and (anchor_drawdown_usd >= max_loss_usd or anchor_drawdown_pct >= threshold_pct)
        )

        acc_bal = anchor_bal if anchor_bal > 0.0 else live_bal
        consec_info = self.loss_memory.update_consecutive_losses(
            order_history=getattr(self.order_manager, "order_history", []),
            balance=acc_bal,
            threshold_pct=threshold_pct,
        )
        adaptive_tuning = consec_info.get("adaptive_tuning") or self.loss_memory.compute_adaptive_tuning(
            int(consec_info.get("consecutive_loss_count") or 0)
        )

        # Background Adaptive Auto-Analysis & Strategy Improvement when losing streak >= 3 trades
        # (Replaces DD Circuit Breaker lock — analyzes in background and adapts entry/allocation automatically!)
        streak_cnt = int(consec_info.get("consecutive_loss_count") or 0)
        if streak_cnt >= 3:
            streak_orders = []
            for o in reversed(getattr(self.order_manager, "order_history", [])):
                if str(o.get("order_id", "")).startswith("test_") or o.get("close_reason") in ("TEST_CLEANUP", "SYSTEM_INIT"):
                    continue
                pnl = float(o.get("realized_pnl_usd") or 0.0)
                if pnl < -0.01:
                    streak_orders.append(o)
                    if len(streak_orders) == min(streak_cnt, 5):
                        break
                elif pnl > 0.01:
                    break
            if len(streak_orders) >= 3:
                self.loss_memory.record_consecutive_loss_streak(
                    streak_orders, market_context={"structure": structure}
                )
            prev_analyzed_cnt = int((self.loss_memory.latest_reanalysis or {}).get("consecutive_losses") or 0)
            prev_analyzed_ts = int((self.loss_memory.latest_reanalysis or {}).get("timestamp") or 0)
            if not self.loss_memory.latest_reanalysis or prev_analyzed_cnt != streak_cnt or (now_ts_eval - prev_analyzed_ts) >= 300:
                self.loss_memory.run_deep_reanalysis(
                    symbol=self.symbol,
                    timeframe=tf,
                    structure=structure,
                    mtf=mtf,
                    macro=macro,
                    account_balance=acc_bal,
                )

        # Single Source of Truth for Portfolio Loss Limit = Dashboard Anchored Stop Floor (`anchor_loss_tripped`).
        # If an attached MT5 EA still has its old duplicate Kill Switch tripped while Dashboard Stop Floor is safe,
        # automatically send RESET_KILL_SWITCH to keep MT5 unblocked!
        if ea_acc.get("kill_switch_tripped") and not anchor_loss_tripped:
            self.ea_bridge.enqueue_action(
                action_type="RESET_KILL_SWITCH",
                payload={"symbol": self.broker_symbol},
                signal_id=f"auto_unlock_ea_ks_{now_ts_eval // 10}",
            )
            ea_acc["kill_switch_tripped"] = False

        # Anchored Portfolio Loss Floor Enforcement (only when user's configured Stop Floor from Anchor Balance is hit)
        self.config["circuit_breaker_20pct_tripped"] = False
        if anchor_loss_tripped:
            consec_info["reason"] = (
                f"🛑 แตะลิมิตขาดทุนพอร์ต -${anchor_drawdown_usd:.2f} (-{anchor_drawdown_pct:.1f}%) "
                f"จาก Balance ตอนตั้ง ${anchor_bal:.2f} (Floor ${stop_floor_balance:.2f})"
            )
            if self.ea_bridge.auto_trading_enabled:
                self.ea_bridge.set_auto_trading(False)
            for act in list(self.order_manager.active_orders):
                if act.get("status") == "PENDING":
                    self.order_manager.cancel_active_order(reason="ANCHOR_LOSS_LIMIT_REACHED", order_id=act.get("order_id"))
                    bt = act.get("broker_ticket")
                    if bt:
                        self.ea_bridge.enqueue_action("CLOSE_ORDER", {"ticket": int(bt), "symbol": self.broker_symbol})
            if int(ea_acc.get("open_positions_count") or 0) > 0 or int(ea_acc.get("pending_orders_count") or 0) > 0:
                self.ea_bridge.enqueue_action(
                    "CLOSE_ALL",
                    {"symbol": self.broker_symbol, "magic": 797979},
                    signal_id=f"loss_limit_close_{now_ts_eval // 5}",
                )

        # Daily Profit Target Enforcement: Lock profit, close open positions if equity reached target, pause Auto-Trading!
        if daily_profit_reached:
            if self.ea_bridge.auto_trading_enabled:
                self.ea_bridge.set_auto_trading(False)
            for act in list(self.order_manager.active_orders):
                if act.get("status") == "PENDING":
                    self.order_manager.cancel_active_order(reason="DAILY_PROFIT_TARGET_REACHED", order_id=act.get("order_id"))
                    bt = act.get("broker_ticket")
                    if bt:
                        self.ea_bridge.enqueue_action("CLOSE_ORDER", {"ticket": int(bt), "symbol": self.broker_symbol})
            if session_equity_pnl_usd >= daily_profit_target_usd and (int(ea_acc.get("open_positions_count") or 0) > 0 or int(ea_acc.get("pending_orders_count") or 0) > 0):
                self.ea_bridge.enqueue_action(
                    "CLOSE_ALL",
                    {"symbol": self.broker_symbol, "magic": 797979},
                    signal_id=f"profit_lock_close_{now_ts_eval // 5}",
                )

        # Sideway Guard State Machine: Auto-pause new orders & prompt user (30s safe default -> wait for breakout)
        sw_guard = self.update_sideway_guard(structure, now_ts_eval)
        sideway_paused = bool(sw_guard.get("block_new_orders", False))

        # Online AI Trade Memory: continuously train on any newly closed real trades (both WIN and LOSS!)
        try:
            self.ai_memory.ingest_order_history(getattr(self.order_manager, "order_history", []))
        except Exception:
            pass
        ai_mem_eval = self._apply_ai_memory_to_setup(smc.get("trade_setup"), structure, mtf)

        # Evaluate candidate setup against loss memory
        loss_eval = self.loss_memory.evaluate_setup_risk(
            trade_setup=smc.get("trade_setup") or {},
            structure_res=structure,
            symbol=self.symbol,
        )

        trades_today_count = sum(
            1
            for o in getattr(self.order_manager, "order_history", [])
            if (int(o.get("created_ts") or 0) // 86400) == now_day
            and not str(o.get("order_id") or "").startswith("test_")
            and o.get("status") not in ("CANCELLED", "CLOSED_TEST_CLEANUP", "REJECTED")
        )
        open_portfolio_risk_usd = (
            sum(float(o.get("actual_risk_usd") or 0.0) for o in self.order_manager.active_orders)
            if not self.order_manager.has_active_order()
            else 0.0
        )
        def_contract_size = 1.0 if is_btc else 100.0
        contract_size = float(self.config.get("contract_size_btc" if is_btc else "contract_size") or def_contract_size)
        cfg_for_risk = dict(
            self.config,
            symbol=self.symbol,
            contract_size=contract_size,
            broker_price_offset=0.0,
            trading_style=effective_style,
            min_win_prob_pct=min_win_prob,
            current_daily_loss_pct=max(float(ea_acc.get("daily_dd_pct") or 0.0), anchor_drawdown_pct),
            kill_switch_tripped=bool(ea_acc.get("kill_switch_tripped", False)),
            trades_today_count=trades_today_count,
            open_portfolio_risk_usd=open_portfolio_risk_usd,
            current_daily_profit_usd=effective_profit_usd,
            consecutive_loss_usd=consec_info["consecutive_loss_usd"],
            consecutive_loss_count=consec_info["consecutive_loss_count"],
            circuit_breaker_20pct_tripped=False,
            anchor_loss_limit_reached=anchor_loss_tripped,
            max_loss_usd=max_loss_usd,
            stop_floor_balance=stop_floor_balance,
            adaptive_tuning=adaptive_tuning,
            loss_pattern_eval=loss_eval,
            ai_memory_eval=ai_mem_eval,
            sideway_paused=sideway_paused,
        )
        risk_ticket = evaluate_risk_and_size(
            trade_setup=smc["trade_setup"],
            simulation=sim,
            news_status=news,
            config=cfg_for_risk,
        )
        risk_ticket["broker_offset_usd"] = round(offset, 2)
        risk_ticket["setup_archetype"] = smc.get("trade_setup", {}).get("setup_archetype", "ORDER_BLOCK_PULLBACK")
        risk_ticket["archetype_label_th"] = smc.get("trade_setup", {}).get("archetype_label_th", "🏛️ SMC Order Block Pullback")
        risk_ticket["available_setups"] = smc.get("available_setups", [])

        cur_p = float(structure["current_price"])
        last_b = chart_bars[-1] if chart_bars else {}
        cur_hi = float(last_b.get("h", cur_p))
        cur_lo = float(last_b.get("l", cur_p))
        cur_ts = int(last_b.get("t", time.time()))
        atr_val = float(structure.get("atr14") or 3.50)

        order_event = self.order_manager.update_price_tick(
            current_price=cur_p,
            high=cur_hi,
            low=cur_lo,
            ts=cur_ts,
            contract_size=contract_size,
            config=self.config,
            symbol=self.symbol,
            market_context={"structure": structure, "smc": smc, "bars": chart_bars},
        )

        # Pre-News Position Shield: Cancel PENDING limit orders & move winning FILLED to Breakeven
        if pns_cfg.get("enabled", True) and news.get("is_imminent_news") and self.order_manager.has_active_order():
            imminent_events = news.get("imminent_news_events") or []
            ev_title = imminent_events[0]["title"] if imminent_events else "High-Impact USD News"
            min_away = float(imminent_events[0].get("minutes_away", 0.0)) if imminent_events else 0.0

            for ord_item in list(self.order_manager.active_orders):
                # 1. Cancel Pending Limit Orders
                if ord_item.get("status") == "PENDING" and pns_cfg.get("cancel_pending_limit", True):
                    oid = ord_item.get("order_id")
                    if oid and not ord_item.get("pre_news_shield_handled"):
                        ord_item["pre_news_shield_handled"] = True
                        cancel_ev = self.order_manager.cancel_active_order(
                            reason="PRE_NEWS_SHIELD",
                            order_id=oid,
                        )
                        if cancel_ev:
                            cancel_ev["message"] = (
                                f"🛡️ Pre-News Shield: ยกเลิกใบจอง {ord_item['side']} LIMIT @ {ord_item['entry_mt5']:.2f} "
                                f"ก่อนข่าวแดง USD ({ev_title}) ออกใน {min_away:.0f} นาที"
                            )
                            if not order_event:
                                order_event = cancel_ev
                        bt = ord_item.get("broker_ticket")
                        if bt:
                            self.ea_bridge.enqueue_action("CLOSE_ORDER", {"ticket": int(bt), "symbol": self.symbol})

                # 2. Protect Winning Positions with Break-Even SL
                elif ord_item.get("status") == "FILLED" and not ord_item.get("sl_moved_breakeven"):
                    be_threshold = float(pns_cfg.get("breakeven_filled_min_r") or 0.25)
                    flt_r = float(ord_item.get("floating_pnl_r") or 0.0)
                    if flt_r >= be_threshold:
                        be_sl = float(ord_item.get("breakeven_sl_mt5") or ord_item.get("entry_mt5") or 0.0)
                        ord_item["sl_moved_breakeven"] = True
                        ord_item["stop_mt5"] = be_sl
                        ord_item["pre_news_shield_handled"] = True
                        ord_item["management_note_th"] = (
                            f"🛡️ Pre-News Shield: ขยับ SL บังทุนที่ {be_sl:.2f} "
                            f"ก่อนข่าวแดง USD ({ev_title}) ออกใน {min_away:.0f} นาที"
                        )
                        self.order_manager.save_to_disk()
                        bt = ord_item.get("broker_ticket")
                        if bt:
                            self.ea_bridge.enqueue_action(
                                action_type="MODIFY_ORDER",
                                payload={
                                    "ticket": int(bt),
                                    "symbol": ord_item.get("symbol", self.symbol),
                                    "new_sl": be_sl,
                                    "new_tp": float(ord_item.get("target_mt5") or 0.0),
                                },
                                signal_id=f"{ord_item['order_id']}-NEWS-BE",
                            )

        min_conf = int((self.config.get("ai") or {}).get("auto_trigger_min_confluence") or 55)

        if self.order_manager.has_active_order():
            for act in list(self.order_manager.active_orders):
                if act.get("status") == "PENDING" and act.get("trading_style") != effective_style:
                    self.order_manager.cancel_active_order(reason="STYLE_CHANGED", order_id=act.get("order_id"))

        active_ord = self.order_manager.get_active_order_dict(cur_p) if self.order_manager.has_active_order() else None
        is_same_style_order = bool(active_ord and active_ord.get("trading_style") == effective_style)

        if is_same_style_order:
            # Autonomously scan and execute recovery (แก้ไม้) or scale-in (ทำกำไรเพิ่ม)
            auto_event = self.order_manager.check_and_apply_auto_multi_order(
                current_price=cur_p,
                high=cur_hi,
                low=cur_lo,
                atr=atr_val,
                structure=structure,
                smc=smc,
                risk_ticket=risk_ticket,
                config=self.config,
                ts=cur_ts,
            )
            if auto_event:
                if not order_event:
                    order_event = auto_event
                active_ord = self.order_manager.get_active_order_dict(cur_p)

            if active_ord:
                cur_lev = max(1.0, float(self.config.get("leverage") or 100.0))
                act_lot = float(active_ord.get("total_lot_size", active_ord["lot_size"]))
                act_entry = float(active_ord.get("avg_entry_price", active_ord["entry_mt5"]))
                cur_bal = float(self.config.get("account_balance", 5000.0))
                contract_size = float(self.config.get("contract_size_btc" if is_btc else "contract_size", (1.0 if is_btc else 100.0)))
                margin_req = round((act_lot * contract_size * act_entry) / cur_lev, 2)
                risk_ticket["leverage"] = cur_lev
                risk_ticket["margin_required_usd"] = margin_req
                risk_ticket["free_margin_usd"] = round(cur_bal - margin_req, 2)
                risk_ticket["margin_level_pct"] = round((cur_bal / margin_req) * 100.0, 1) if margin_req > 0 else 9999.0
                risk_ticket["side"] = active_ord["side"]
                risk_ticket["execution_type"] = active_ord.get("execution_type", "LIMIT")
                risk_ticket["order_type"] = active_ord.get("order_type", f"{active_ord['side']} LIMIT")
                risk_ticket["entry_mt5"] = active_ord["entry_mt5"]
                risk_ticket["stop_mt5"] = active_ord["stop_mt5"]
                risk_ticket["tp1_mt5"] = active_ord["tp1_mt5"]
                risk_ticket["target_mt5"] = active_ord["target_mt5"]
                risk_ticket["breakeven_sl_mt5"] = active_ord.get("breakeven_sl_mt5", active_ord["entry_mt5"])
                risk_ticket["lot_size"] = active_ord["lot_size"]
                risk_ticket["actual_risk_usd"] = active_ord["actual_risk_usd"]
                risk_ticket["potential_profit_usd"] = active_ord["potential_profit_usd"]
                risk_ticket["reward_risk"] = active_ord["reward_risk"]
                risk_ticket["status"] = active_ord["status"]
                risk_ticket["win_prob_passed"] = True
                risk_ticket["floating_pnl_usd"] = active_ord.get("floating_pnl_usd", 0.0)
                risk_ticket["floating_pnl_r"] = active_ord.get("floating_pnl_r", 0.0)
                risk_ticket["management_note_th"] = active_ord.get("management_note_th", "")
                risk_ticket["active_order"] = active_ord
                risk_ticket["verdict"] = "ACTIVE_ORDER" if active_ord["status"] == "FILLED" else "PENDING_ORDER"
                orders_cnt = active_ord.get("total_orders_count", 1)
                tot_lot = active_ord.get("total_lot_size", active_ord["lot_size"])
                avg_e = active_ord.get("avg_entry_price", active_ord["entry_mt5"])
                risk_ticket["badge_th"] = (
                    f"🚀 ถือครอง {orders_cnt} ไม้ (รวม {tot_lot:.2f} Lot • ทุนเฉลี่ย {avg_e:.2f})"
                    if orders_cnt > 1
                    else (
                        f"🚀 ถือครองออเดอร์ ({active_ord['side']} @ {active_ord['entry_mt5']:.2f})"
                        if active_ord["status"] == "FILLED"
                        else f"⏳ รอเกี่ยวคำสั่ง {active_ord['order_type']} @ {active_ord['entry_mt5']:.2f}"
                    )
                )

        # ---------------- Unified Multi-Strategy Engine ----------------
        # Simultaneously evaluates enabled channels (SMC_SWING, RAPID_FIRE)
        primary_strat = "RAPID_FIRE" if effective_style == "rapid_fire_test" else "SMC_SWING"

        # Channel 1: Primary Setup Registration (allows both ACTIVE_IN_ZONE and WAIT_RETRACEMENT pending limit OB setups!)
        eff_min_conf = min_conf + int(adaptive_tuning.get("min_confluence_boost") or 0)
        if (
            not daily_profit_reached
            and not anchor_loss_tripped
            and not sideway_paused
            and not loss_eval.get("is_high_risk")
            and risk_ticket.get("win_prob_passed", False)
            and risk_ticket.get("verdict") in ("PASS", "CAUTION")
            and smc["trade_setup"].get("confluence_score", 0) >= eff_min_conf
        ):
            existing_sides = {
                str(o.get("side")).upper()
                for o in self.order_manager.active_orders
                if str(o.get("symbol", self.symbol)).upper() == self.symbol.upper()
            }
            cand_side = str(risk_ticket.get("side") or "").upper()
            # Direction Harmony Guard: never allow opposing simultaneous positions on same symbol
            if (not existing_sides or cand_side in existing_sides) and not self.order_manager.has_active_order(symbol=self.symbol, strategy_mode=primary_strat):
                self.order_manager.register_trade_setup(
                    risk_ticket=risk_ticket,
                    smc_setup=smc["trade_setup"],
                    current_price=cur_p,
                    timeframe=tf,
                    trading_style=effective_style,
                    symbol=self.symbol,
                    strategy_mode=primary_strat,
                )
                existing_sides.add(cand_side)
                if not order_event:
                    order_event = self.order_manager.last_event

        # Channel 2: Rapid Fire / Harvester (Parallel Channel — MTF S/R + Candlestick + Chart Patterns + Adaptive)
        is_rapid_cfg = bool((self.config.get("rapid_fire_test") or {}).get("enabled", False))
        if (
            not daily_profit_reached
            and not anchor_loss_tripped
            and not sideway_paused
            and is_rapid_cfg
            and primary_strat != "RAPID_FIRE"
            and not self.order_manager.has_active_order(symbol=self.symbol, strategy_mode="RAPID_FIRE")
        ):
            try:
                rapid_smc = analyze_smc(
                    chart_bars,
                    timeframe=tf,
                    spread_usd=spread_usd,
                    structure_res=structure,
                    mtf_res=mtf,
                    macro_res=macro,
                    trading_style="rapid_fire_test",
                    symbol=self.symbol,
                    adaptive_tuning=adaptive_tuning,
                )
                rapid_setup = rapid_smc.get("trade_setup")
                rapid_ai_eval = self._apply_ai_memory_to_setup(rapid_setup, structure, mtf)
                rapid_side = str((rapid_setup or {}).get("side") or "").upper()
                rapid_ready = str((rapid_setup or {}).get("status") or "").upper() == "ACTIVE_IN_ZONE"
                existing_sides = {
                    str(o.get("side")).upper()
                    for o in self.order_manager.active_orders
                    if str(o.get("symbol", self.symbol)).upper() == self.symbol.upper()
                }
                # Direction Harmony Guard + MTF S/R Wall & Candlestick Gate + Independent Loss Eval & Simulation
                if rapid_setup and rapid_ready and rapid_side and (not existing_sides or rapid_side in existing_sides):
                    rapid_loss_eval = self.loss_memory.evaluate_setup_risk(
                        trade_setup=rapid_setup,
                        structure_res=structure,
                        symbol=self.symbol,
                    )
                    if not rapid_loss_eval.get("is_high_risk"):
                        rapid_sim = run_simulation(
                            bars=chart_bars,
                            trade_setup=rapid_setup,
                            structure_res=structure,
                            replays=1000,
                            bars_ahead=12,
                            seed=sim_seed,
                        )
                        cfg_rapid = dict(
                            cfg_for_risk,
                            trading_style="rapid_fire_test",
                            loss_pattern_eval=rapid_loss_eval,
                            ai_memory_eval=rapid_ai_eval,
                        )
                        rapid_ticket = evaluate_risk_and_size(trade_setup=rapid_setup, simulation=rapid_sim, news_status=news, config=cfg_rapid)
                        if rapid_ticket.get("win_prob_passed") and rapid_ticket.get("verdict") in ("PASS", "CAUTION"):
                            self.order_manager.register_trade_setup(
                                risk_ticket=rapid_ticket,
                                smc_setup=rapid_setup,
                                current_price=cur_p,
                                timeframe=tf,
                                trading_style="rapid_fire_test",
                                symbol=self.symbol,
                                strategy_mode="RAPID_FIRE",
                            )
                            existing_sides.add(rapid_side)
                            if not order_event:
                                order_event = self.order_manager.last_event
            except Exception:
                pass

        # Channel 3: SMC Swing / Intraday (Flagship Model: always evaluates M15 higher timeframe!)
        if (
            not daily_profit_reached
            and not anchor_loss_tripped
            and not sideway_paused
            and primary_strat != "SMC_SWING"
            and not self.order_manager.has_active_order(symbol=self.symbol, strategy_mode="SMC_SWING")
        ):
            try:
                # Fetch M15 higher timeframe bars for real swing Order Blocks and targets
                swing_feed = self.hub.get_bars("M15", force_refresh=False, symbol=self.symbol)
                swing_bars = (swing_feed.get("bars") or [])[-600:]
                if not swing_bars:
                    swing_bars = chart_bars
                swing_struct = analyze_structure(swing_bars, timeframe="M15", mtf_bars_by_tf=mtf_bars)
                swing_smc = analyze_smc(
                    swing_bars,
                    timeframe="M15",
                    spread_usd=spread_usd,
                    structure_res=swing_struct,
                    mtf_res=mtf,
                    macro_res=macro,
                    trading_style="intraday",
                    symbol=self.symbol,
                    adaptive_tuning=adaptive_tuning,
                )
                swing_setup = swing_smc.get("trade_setup")
                swing_ai_eval = self._apply_ai_memory_to_setup(swing_setup, swing_struct, mtf)
                swing_side = str((swing_setup or {}).get("side") or "").upper()
                existing_sides = {
                    str(o.get("side")).upper()
                    for o in self.order_manager.active_orders
                    if str(o.get("symbol", self.symbol)).upper() == self.symbol.upper()
                }
                # Direction Harmony Guard + Independent M15 Loss Eval & M15 Simulation
                if swing_setup and swing_side and (not existing_sides or swing_side in existing_sides):
                    swing_loss_eval = self.loss_memory.evaluate_setup_risk(
                        trade_setup=swing_setup,
                        structure_res=swing_struct,
                        symbol=self.symbol,
                    )
                    if not swing_loss_eval.get("is_high_risk"):
                        swing_sim = run_simulation(
                            bars=swing_bars,
                            trade_setup=swing_setup,
                            structure_res=swing_struct,
                            replays=1500,
                            bars_ahead=24,
                            seed=sim_seed,
                        )
                        cfg_swing = dict(
                            cfg_for_risk,
                            trading_style="intraday",
                            loss_pattern_eval=swing_loss_eval,
                            ai_memory_eval=swing_ai_eval,
                        )
                        swing_ticket = evaluate_risk_and_size(trade_setup=swing_setup, simulation=swing_sim, news_status=news, config=cfg_swing)
                        if swing_ticket.get("win_prob_passed") and swing_ticket.get("verdict") in ("PASS", "CAUTION"):
                            self.order_manager.register_trade_setup(
                                risk_ticket=swing_ticket,
                                smc_setup=swing_setup,
                                current_price=cur_p,
                                timeframe="M15",
                                trading_style="intraday",
                                symbol=self.symbol,
                                strategy_mode="SMC_SWING",
                            )
                            existing_sides.add(swing_side)
                            if not order_event:
                                order_event = self.order_manager.last_event
            except Exception:
                pass

        risk_ticket["active_order"] = self.order_manager.get_active_order_dict(cur_p)

        # Auto-Trading Dispatch to MT5 EA Bridge when Auto-Trading is ON
        if self.ea_bridge.auto_trading_enabled:
            # Dispatch all active orders (SMC_SWING, RAPID_FIRE, PROMOTED_RUNNER) needing execution
            for ord_item in list(self.order_manager.active_orders):
                if ord_item.get("status") in ("PENDING", "FILLED") and ord_item.get("order_id"):
                    sig_id = ord_item["order_id"]
                    has_ticket = bool(ord_item.get("broker_ticket"))
                    is_in_queue = any(a.get("signal_id") == sig_id for a in self.ea_bridge.action_queue)
                    already_dispatched = (
                        sig_id in self.ea_bridge.executed_signal_ids
                        or bool(ord_item.get("ea_dispatch_status"))
                    )
                    if not has_ticket and not is_in_queue and not already_dispatched:
                        if daily_profit_reached or anchor_loss_tripped or sideway_paused:
                            continue
                        strat_m = str(ord_item.get("strategy_mode") or "SMC_SWING").upper()
                        comment = ord_item.get("comment") or f"K79-{strat_m[:3]}-{ord_item['side']}"
                        exec_t = ord_item.get("execution_type", "LIMIT")
                        entry_val = float(ord_item.get("entry_mt5") or cur_p)

                        # Anti-duplicate pending limit guard: check if MT5 already has this pending order
                        mt5_pendings = (self.ea_bridge.account_info or {}).get("pending_orders") or []
                        if exec_t == "LIMIT" and mt5_pendings:
                            matched_po = next(
                                (
                                    p for p in mt5_pendings
                                    if ord_item["side"] in str(p.get("type", "")).upper()
                                    and abs(float(p.get("open_price") or 0.0) - entry_val) < 1.0
                                ),
                                None,
                            )
                            if matched_po:
                                ord_item["broker_ticket"] = int(matched_po["ticket"])
                                ord_item["ea_dispatch_status"] = "DISPATCHED"
                                continue

                        ord_item["ea_dispatch_status"] = "DISPATCHED"
                        self.ea_bridge.enqueue_action(
                            action_type="OPEN_ORDER",
                            payload={
                                "side": ord_item["side"],
                                "order_type": exec_t,
                                "symbol": self.broker_symbol,
                                "lot": float(ord_item.get("lot_size") or (0.01 if is_btc else 0.10)),
                                "price": entry_val,
                                "sl": float(ord_item.get("stop_mt5") or 0.0),
                                "tp": float(ord_item.get("target_mt5") or 0.0),
                                "tp1": float(ord_item.get("tp1_mt5") or 0.0),
                                "magic": 797979,
                                "comment": comment,
                            },
                            signal_id=sig_id,
                        )

            # Case 2.4: Fast Breakeven Modify on MT5 (Sideway & Scalp Quick Lock)
            if order_event and order_event.get("type") == "BREAKEVEN_LOCKED":
                be_oid = order_event.get("order_id")
                target_ord = next(
                    (o for o in self.order_manager.active_orders if o.get("order_id") == be_oid),
                    self.order_manager.active_order,
                )
                if target_ord and target_ord.get("broker_ticket") and order_event.get("be_sl"):
                    new_sl_val = float(order_event["be_sl"])
                    sig_be = f"{target_ord['order_id']}-BE-LOCK"
                    if sig_be not in self.ea_bridge.executed_signal_ids:
                        self.ea_bridge.enqueue_action(
                            action_type="MODIFY_ORDER",
                            payload={
                                "ticket": int(target_ord["broker_ticket"]),
                                "symbol": target_ord.get("symbol", self.symbol),
                                "new_sl": new_sl_val,
                                "new_tp": float(target_ord.get("target_mt5") or 0.0),
                            },
                            signal_id=sig_be,
                        )

            # Case 2.5: TP1 Partial Close + Move SL to Breakeven
            if order_event and order_event.get("type") == "TP1_REACHED":
                tp1_oid = order_event.get("order_id")
                target_ord = next(
                    (o for o in self.order_manager.active_orders if o.get("order_id") == tp1_oid),
                    self.order_manager.active_order,
                )
                if (
                    target_ord
                    and target_ord.get("broker_ticket")
                    and not target_ord.get("tp1_partial_dispatched")
                ):
                    target_ord["tp1_partial_dispatched"] = True
                    close_lot = round(max(0.01, float(target_ord.get("lot_size") or 0.10) * 0.5), 2)
                    be_sl = float(target_ord.get("breakeven_sl_mt5") or target_ord.get("entry_mt5") or 0.0)
                    self.ea_bridge.enqueue_action(
                        action_type="CLOSE_PARTIAL",
                        payload={
                            "ticket": int(target_ord["broker_ticket"]),
                            "symbol": target_ord.get("symbol", "XAUUSD"),
                            "close_lot": close_lot,
                            "new_sl": be_sl,
                        },
                        signal_id=f"{target_ord['order_id']}-TP1",
                    )

            # Case 2.6: Step Trailing Stop Modify on MT5
            if order_event and order_event.get("type") == "TRAILING_STOP_UPDATED":
                trail_oid = order_event.get("order_id")
                target_ord = next(
                    (o for o in self.order_manager.active_orders if o.get("order_id") == trail_oid),
                    self.order_manager.active_order,
                )
                if target_ord and target_ord.get("broker_ticket") and order_event.get("new_sl"):
                    new_sl_val = float(order_event["new_sl"])
                    step_idx = order_event.get("step_index", 1)
                    sig_trail = f"{target_ord['order_id']}-TRAIL-{step_idx}"
                    if sig_trail not in self.ea_bridge.executed_signal_ids:
                        self.ea_bridge.enqueue_action(
                            action_type="MODIFY_ORDER",
                            payload={
                                "ticket": int(target_ord["broker_ticket"]),
                                "symbol": target_ord.get("symbol", "XAUUSD"),
                                "new_sl": new_sl_val,
                                "new_tp": float(target_ord.get("target_mt5") or 0.0),
                            },
                            signal_id=sig_trail,
                        )

            # Case 2.7: Dynamic Order Morphing / Promotion on MT5 (Rapid / Scalp -> Swing Runner)
            if order_event and order_event.get("type") == "ORDER_PROMOTED_TO_RUNNER":
                prom_oid = order_event.get("order_id")
                target_ord = next(
                    (o for o in self.order_manager.active_orders if o.get("order_id") == prom_oid),
                    None,
                )
                if target_ord and target_ord.get("broker_ticket"):
                    sig_prom = f"{target_ord['order_id']}-PROMOTED"
                    if sig_prom not in self.ea_bridge.executed_signal_ids:
                        self.ea_bridge.enqueue_action(
                            action_type="MODIFY_ORDER",
                            payload={
                                "ticket": int(target_ord["broker_ticket"]),
                                "symbol": target_ord.get("symbol", self.symbol),
                                "new_sl": float(order_event.get("new_sl") or target_ord.get("stop_mt5")),
                                "new_tp": float(order_event.get("new_tp") or target_ord.get("target_mt5")),
                            },
                            signal_id=sig_prom,
                        )

            # Case 3: Autonomous Close / Invalidation (NEVER force-close for virtual SL/TP/BE — MT5 broker handles hard SL/TP natively!)
            if order_event and order_event.get("type") in (
                "ORDER_CLOSED_DISADVANTAGE",
                "BUNDLE_CIRCUIT_BREAKER",
                "ROBOT_BASKET_CUT",
                "TRADE_CLOSED_REVERSAL_PROFIT_LOCK",
                "ORDER_CLOSED_REVERSAL_PROFIT_LOCK",
                "TRADE_CLOSED_SIDEWAY_STAGNATION_GUARD",
                "ORDER_CLOSED_SIDEWAY_STAGNATION_GUARD",
                "TRADE_CLOSED_TIME_DECAY",
                "ORDER_CANCELLED",
            ):
                ord_obj = order_event.get("order") or {}
                tickets_to_close = []
                if order_event.get("broker_ticket"):
                    tickets_to_close.append(order_event.get("broker_ticket"))
                for t in order_event.get("broker_tickets") or []:
                    if t and t not in tickets_to_close:
                        tickets_to_close.append(t)
                if ord_obj.get("broker_ticket") and ord_obj.get("broker_ticket") not in tickets_to_close:
                    tickets_to_close.append(ord_obj.get("broker_ticket"))
                for sub_o in ord_obj.get("orders") or []:
                    bt = sub_o.get("broker_ticket")
                    if bt and bt not in tickets_to_close:
                        tickets_to_close.append(bt)
                for t in tickets_to_close:
                    self.ea_bridge.enqueue_action(
                        action_type="CLOSE_ORDER",
                        payload={"ticket": int(t), "symbol": self.symbol},
                    )
                if order_event.get("type") == "ROBOT_BASKET_CUT" and not tickets_to_close:
                    self.ea_bridge.enqueue_action(
                        action_type="CLOSE_ALL",
                        payload={"symbol": self.symbol, "magic": 797979},
                    )

        signal_tracker = evaluate_signal_performance(
            bars=chart_bars,
            timeframe=tf,
            current_ticket=risk_ticket,
            current_smc=smc,
            spread_usd=spread_usd,
        )

        ai_advisory = finalize_advisory(
            structure=structure,
            smc=smc,
            simulation=sim,
            risk_ticket=risk_ticket,
            news=news,
            config=self.config,
            gemini_raw=gemini_raw,
            mtf=mtf,
            macro=macro,
        )

        # --- Cross-Asset Breadth Multi-Symbol Scan (EURUSD, GBPUSD) ---
        cross_asset_cfg = self.config.get("cross_asset") or {}
        cross_asset_state: Dict[str, Any] = {}
        if cross_asset_cfg.get("enabled", False):
            symbols_to_scan = [s for s in cross_asset_cfg.get("symbols", ["XAUUSD", "EURUSD", "GBPUSD"]) if s != "XAUUSD"]
            active_open_symbols = [
                str(o.get("symbol", "XAUUSD")).upper()
                for o in self.order_manager.active_orders
                if o.get("status") in ("PENDING", "FILLED")
            ]
            active_symbol_directions = {
                str(o.get("symbol", "XAUUSD")).upper(): str(o.get("side")).upper()
                for o in self.order_manager.active_orders
                if o.get("status") in ("PENDING", "FILLED")
            }
            if risk_ticket.get("verdict") in ("PASS", "CAUTION", "ACTIVE_ORDER", "PENDING_ORDER"):
                active_symbol_directions["XAUUSD"] = str(risk_ticket["side"]).upper()

            for sym in symbols_to_scan:
                sym_feed = self.hub.get_bars("M5", symbol=sym)
                sym_bars = (sym_feed.get("bars") or [])[-250:]
                if len(sym_bars) < 30:
                    continue

                is_forex = ("EUR" in sym or "GBP" in sym or ("USD" in sym and "XAU" not in sym))
                sym_digits = 5 if is_forex else 2
                sym_point = 0.00001 if is_forex else 0.01
                sym_cfg_entry = (cross_asset_cfg.get("symbols_config") or {}).get(sym, {})
                sym_contract = float(sym_cfg_entry.get("contract_size") or (100000.0 if is_forex else 100.0))
                sym_spread_pts = float(sym_cfg_entry.get("max_spread_pts") or (18.0 if is_forex else 25.0))
                sym_spread_usd = round(sym_spread_pts * sym_point, sym_digits)

                sym_struct = analyze_structure(sym_bars, timeframe="M5")
                sym_mtf_bars = {
                    "H1": resample_bars(sym_bars, "H1"),
                    "M15": resample_bars(sym_bars, "M15"),
                }
                sym_mtf = analyze_multi_timeframe(sym_mtf_bars, primary_tf="M5", current_price=sym_struct["current_price"])
                sym_macro = self.hub.get_macro_correlation(sym_bars, timeframe="M5")
                sym_smc = analyze_smc(
                    sym_bars,
                    timeframe="M5",
                    spread_usd=sym_spread_usd,
                    structure_res=sym_struct,
                    mtf_res=sym_mtf,
                    macro_res=sym_macro,
                    trading_style=effective_style,
                )
                sym_setup = sym_smc.get("trade_setup") or {}

                sym_opt_setup, sym_sim = optimize_setup_for_win_prob(
                    bars=sym_bars,
                    trade_setup=sym_setup,
                    structure_res=sym_struct,
                    replays=1000,
                    bars_ahead=18,
                    seed=sim_seed,
                    min_win_prob_pct=min_win_prob,
                )
                sym_smc["trade_setup"] = sym_opt_setup

                sym_risk_cfg = dict(
                    self.config,
                    symbol=sym,
                    contract_size=sym_contract,
                    point_size=sym_point,
                    broker_price_offset=0.0,
                    broker_spread_points=sym_spread_pts,
                    active_open_symbols=active_open_symbols,
                    active_symbol_directions=active_symbol_directions,
                )
                sym_ticket = evaluate_risk_and_size(
                    trade_setup=sym_opt_setup,
                    simulation=sym_sim,
                    news_status=news,
                    config=sym_risk_cfg,
                )

                sym_cur_p = float(sym_struct["current_price"])
                sym_last_b = sym_bars[-1]
                sym_hi = float(sym_last_b.get("h", sym_cur_p))
                sym_lo = float(sym_last_b.get("l", sym_cur_p))
                sym_ts = int(sym_last_b.get("t", time.time()))

                sym_order_event = self.order_manager.update_price_tick(
                    current_price=sym_cur_p,
                    high=sym_hi,
                    low=sym_lo,
                    ts=sym_ts,
                    contract_size=sym_contract,
                    config=self.config,
                    symbol=sym,
                )

                sym_active_ord = self.order_manager.get_active_order_dict(sym_cur_p, symbol=sym)
                if not sym_active_ord:
                    if (
                        sym_ticket.get("win_prob_passed", False)
                        and sym_ticket.get("verdict") in ("PASS", "CAUTION")
                        and sym_smc["trade_setup"].get("confluence_score", 0) >= min_conf
                    ):
                        self.order_manager.register_trade_setup(
                            risk_ticket=sym_ticket,
                            smc_setup=sym_smc["trade_setup"],
                            current_price=sym_cur_p,
                            timeframe="M5",
                            trading_style=effective_style,
                            symbol=sym,
                        )
                        sym_active_ord = self.order_manager.get_active_order_dict(sym_cur_p, symbol=sym)

                if self.ea_bridge.auto_trading_enabled and sym_active_ord:
                    sym_ord_id = sym_active_ord.get("order_id")
                    if (
                        sym_ord_id
                        and sym_ord_id not in self.ea_bridge.executed_signal_ids
                        and not sym_active_ord.get("broker_ticket")
                        and not sym_active_ord.get("ea_dispatch_status")
                        and not any(a.get("signal_id") == sym_ord_id for a in self.ea_bridge.action_queue)
                    ):
                        sym_active_ord["ea_dispatch_status"] = "DISPATCHED"
                        self.ea_bridge.enqueue_action(
                            action_type="OPEN_ORDER",
                            payload={
                                "side": sym_active_ord["side"],
                                "order_type": sym_active_ord.get("execution_type", "LIMIT"),
                                "symbol": sym,
                                "lot": float(sym_active_ord.get("lot_size") or 0.10),
                                "price": float(sym_active_ord.get("entry_mt5") or sym_cur_p),
                                "sl": float(sym_active_ord.get("stop_mt5") or 0.0),
                                "tp": float(sym_active_ord.get("target_mt5") or 0.0),
                                "tp1": float(sym_active_ord.get("tp1_mt5") or 0.0),
                                "magic": 797979,
                                "comment": f"K79-{sym}-{sym_active_ord['side']}",
                            },
                            signal_id=sym_ord_id,
                        )

                cross_asset_state[sym] = {
                    "symbol": sym,
                    "current_price": sym_cur_p,
                    "structure": {
                        "trend": sym_struct.get("trend"),
                        "atr14": sym_struct.get("atr14"),
                        "swing_points": sym_struct.get("swing_points", [])[-4:],
                    },
                    "smc": {
                        "trade_setup": sym_opt_setup,
                        "order_blocks": sym_smc.get("order_blocks", [])[:3],
                    },
                    "risk_ticket": sym_ticket,
                    "active_order": sym_active_ord,
                    "order_event": sym_order_event,
                }

        fp = risk_ticket["fingerprint"]
        is_new_signal_alert = False
        min_conf = int((self.config.get("ai") or {}).get("auto_trigger_min_confluence") or 55)
        if order_event and order_event.get("type") in (
            "NEW_ORDER_REGISTERED",
            "ORDER_FILLED",
            "TP1_REACHED",
            "TRADE_CLOSED_TP2",
            "TRADE_CLOSED_BREAKEVEN",
            "TRADE_CLOSED_SL",
        ):
            is_new_signal_alert = True
        elif (
            force_ai
            or (
                fp != self.last_alert_fingerprint
                and smc["trade_setup"]["confluence_score"] >= min_conf
                and risk_ticket.get("win_prob_passed", False)
                and risk_ticket["verdict"] in ("PASS", "CAUTION", "PENDING_ORDER", "ACTIVE_ORDER")
            )
        ):
            self.last_alert_fingerprint = fp
            is_new_signal_alert = True

        clean_history = [
            o
            for o in self.order_manager.order_history
            if not str(o.get("order_id") or "").startswith("test_")
            and o.get("close_reason") not in ("TEST_CLEANUP", "SYSTEM_INIT")
        ][-50:]

        live_quote = feed.get("live_quote")
        state = {
            "type": "full_state",
            "symbol": self.symbol,
            "is_crypto": is_btc,
            "timeframe": tf,
            "trading_style": effective_style,
            "data_source": feed["source"],
            "live_quote": live_quote,
            "replay_mode": self.hub.replay_mode,
            "replay_cursor": self.hub.replay_cursor,
            "replay_total": len(self.hub.fixture_bars),
            "is_new_signal_alert": is_new_signal_alert,
            "active_order": self.order_manager.get_active_order_dict(cur_p),
            "order_event": order_event,
            "order_history": clean_history,
            "bars": chart_bars[-300:],
            "structure": structure,
            "chart_patterns": structure.get("chart_patterns"),
            "market_flow": market_flow,
            "mtf": mtf,
            "macro": macro,
            "signal_tracker": signal_tracker,
            "smc": smc,
            "simulation": {
                k: v for k, v in sim.items() if k != "sample_paths"
            },
            "risk_ticket": risk_ticket,
            "ea_bridge": self.ea_bridge.get_status_dict(),
            "robot_scalper": self.config.get("robot_scalper", {}),
            "rapid_fire_test": self.config.get("rapid_fire_test", {}),
            "cross_asset": {
                "enabled": bool(cross_asset_cfg.get("enabled", False)),
                "symbols": cross_asset_state,
            },
            "news": news,
            "ai_advisory": ai_advisory,
            "ai_memory_trainer": self.ai_memory.get_dashboard_summary(),
            "ai_memory_eval": ai_mem_eval,
            "sideway_guard": sw_guard,
            "daily_profit_tracking": {
                "anchor_balance": round(anchor_bal, 2),
                "anchor_ts": anchor_ts,
                "anchor_bkk": anchor_bkk,
                "daily_profit_target_usd": round(daily_profit_target_usd, 2),
                "daily_profit_target_enabled": daily_target_enabled,
                "daily_realized_pnl_usd": round(daily_realized_pnl_usd, 2),
                "session_equity_pnl_usd": round(session_equity_pnl_usd, 2),
                "target_balance_goal": target_balance_goal,
                "daily_profit_target_reached": daily_profit_reached,
                "progress_pct": round(min(100.0, (effective_profit_usd / daily_profit_target_usd * 100.0)), 1) if (daily_profit_target_usd > 0 and effective_profit_usd > 0) else 0.0,
            },
            "circuit_breaker_20pct": {
                "tripped": bool(anchor_loss_tripped),
                "anchor_loss_limit_reached": bool(anchor_loss_tripped),
                "anchor_balance": round(anchor_bal, 2),
                "anchor_bkk": anchor_bkk,
                "max_loss_usd": round(max_loss_usd, 2),
                "stop_floor_balance": stop_floor_balance,
                "anchor_drawdown_usd": anchor_drawdown_usd,
                "anchor_drawdown_pct": anchor_drawdown_pct,
                "consecutive_loss_count": int(consec_info.get("consecutive_loss_count", 0)),
                "consecutive_loss_usd": round(float(consec_info.get("consecutive_loss_usd", 0.0)), 2),
                "consecutive_loss_pct": round(float(consec_info.get("consecutive_loss_pct", 0.0)), 2),
                "threshold_pct": round(threshold_pct, 2),
                "adaptive_tuning": adaptive_tuning,
                "reason": consec_info.get("reason", ""),
            },
            "loss_memory": self.loss_memory.get_status(),
            "config": {
                "account_balance": self.config.get("account_balance", 5000.0),
                "leverage": float(self.config.get("leverage") or 100.0),
                "risk_per_trade_pct": self.config.get("risk_per_trade_pct", 1.0),
                "min_reward_risk": self.config.get("min_reward_risk", 1.5),
                "broker_price_offset": offset,
                "broker_spread_points": int(spread_pts),
                "daily_profit_target_usd": round(daily_profit_target_usd, 2),
                "daily_profit_target_enabled": daily_target_enabled,
                "max_consecutive_loss_pct": round(threshold_pct, 2),
                "max_loss_usd": round(max_loss_usd, 2),
                "pnl_anchor_balance": round(anchor_bal, 2),
                "pnl_anchor_bkk": anchor_bkk,
                "robot_scalper": self.config.get("robot_scalper", {}),
                "rapid_fire_test": self.config.get("rapid_fire_test", {}),
                "cross_asset_enabled": bool(cross_asset_cfg.get("enabled", False)),
                "has_gemini_key": bool(
                    (self.config.get("ai") or {}).get("api_key")
                    or os.environ.get("GEMINI_API_KEY")
                ),
            },
        }
        self.last_state = state
        return state

    def reanchor_pnl_targets(self) -> float:
        """Anchor Profit/Loss tracking to current live MT5 Balance at the exact moment of setup."""
        ea_acc = self.ea_bridge.account_info or {}
        is_mt5_live = bool(self.ea_bridge.is_connected and float(ea_acc.get("balance") or 0.0) > 0.0)
        cur_bal = float(ea_acc.get("balance") or 0.0) if is_mt5_live else float(self.config.get("account_balance") or 5000.0)
        cur_bal = round(max(1.0, cur_bal), 2)
        now_ts = int(time.time())
        self.config["pnl_anchor_balance"] = cur_bal
        self.config["pnl_anchor_ts"] = now_ts
        self.config["pnl_anchor_bkk"] = _fmt_bkk(now_ts)
        loss_pct = float(self.config.get("max_consecutive_loss_pct") if self.config.get("max_consecutive_loss_pct") is not None else 20.0)
        self.config["max_loss_usd"] = round(cur_bal * (loss_pct / 100.0), 2)
        return cur_bal

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        prev_target = self.config.get("daily_profit_target_usd")
        prev_loss_pct = self.config.get("max_consecutive_loss_pct")
        prev_loss_usd = self.config.get("max_loss_usd")

        for key in (
            "account_balance",
            "leverage",
            "risk_per_trade_pct",
            "min_reward_risk",
            "broker_price_offset",
            "broker_spread_points",
            "daily_profit_target_usd",
            "max_consecutive_loss_pct",
            "max_loss_usd",
        ):
            if key in payload and payload[key] is not None and str(payload[key]).strip() != "":
                val = float(payload[key])
                if key in ("daily_profit_target_usd", "max_loss_usd"):
                    val = max(0.0, val)
                elif key == "max_consecutive_loss_pct":
                    val = max(1.0, min(95.0, val))
                self.config[key] = round(val, 2)

        if "daily_profit_target_enabled" in payload:
            self.config["daily_profit_target_enabled"] = bool(payload["daily_profit_target_enabled"])
        if "auto_trading_enabled" in payload:
            self.ea_bridge.set_auto_trading(bool(payload["auto_trading_enabled"]))
        elif "auto_trading" in payload and isinstance(payload["auto_trading"], dict):
            if "enabled" in payload["auto_trading"]:
                self.ea_bridge.set_auto_trading(bool(payload["auto_trading"]["enabled"]))

        # Anchor Profit/Loss calculations to live MT5 Balance at the time user sets them!
        pnl_changed = (
            bool(payload.get("reanchor_pnl"))
            or ("daily_profit_target_usd" in payload and self.config.get("daily_profit_target_usd") != prev_target)
            or ("max_consecutive_loss_pct" in payload and self.config.get("max_consecutive_loss_pct") != prev_loss_pct)
            or ("max_loss_usd" in payload and self.config.get("max_loss_usd") != prev_loss_usd)
            or float(self.config.get("pnl_anchor_balance") or 0.0) <= 0.0
        )
        if pnl_changed:
            ea_acc = self.ea_bridge.account_info or {}
            is_mt5_live = bool(self.ea_bridge.is_connected and float(ea_acc.get("balance") or 0.0) > 0.0)
            anchor_bal = float(ea_acc.get("balance") or 0.0) if is_mt5_live else float(self.config.get("account_balance") or 5000.0)
            anchor_bal = round(max(1.0, anchor_bal), 2)
            now_ts = int(time.time())
            self.config["pnl_anchor_balance"] = anchor_bal
            self.config["pnl_anchor_ts"] = now_ts
            self.config["pnl_anchor_bkk"] = _fmt_bkk(now_ts)

            loss_mode = str(payload.get("loss_input_mode") or "pct").lower()
            if loss_mode == "usd" and "max_loss_usd" in payload and float(self.config.get("max_loss_usd") or 0.0) > 0.0:
                usd_lim = round(min(anchor_bal * 0.95, float(self.config["max_loss_usd"])), 2)
                self.config["max_loss_usd"] = usd_lim
                self.config["max_consecutive_loss_pct"] = round((usd_lim / anchor_bal) * 100.0, 2)
            else:
                pct_lim = float(self.config.get("max_consecutive_loss_pct") if self.config.get("max_consecutive_loss_pct") is not None else 20.0)
                self.config["max_consecutive_loss_pct"] = round(pct_lim, 2)
                self.config["max_loss_usd"] = round(anchor_bal * (pct_lim / 100.0), 2)

            if payload.get("reanchor_pnl"):
                self.loss_memory.reset_circuit_breaker()
                self.config["circuit_breaker_20pct_tripped"] = False

        self.config["simulation_replays"] = max(
            35000, int(self.config.get("simulation_replays") or 35000)
        )
        if "multi_order" in payload and isinstance(payload["multi_order"], dict):
            self.config.setdefault("multi_order", {}).update(payload["multi_order"])
        if "cross_asset" in payload and isinstance(payload["cross_asset"], dict):
            self.config.setdefault("cross_asset", {}).update(payload["cross_asset"])
        if "trading_style" in payload and payload["trading_style"] is not None:
            style = str(payload["trading_style"]).lower()
            if style in ("intraday", "scalping", "micro_scalp"):
                self.trading_style = style
                self.config["trading_style"] = style
                if style == "micro_scalp":
                    self.timeframe = "M1"
                    self.config["default_timeframe"] = "M1"
                elif style == "scalping":
                    self.timeframe = "M5"
                    self.config["default_timeframe"] = "M5"
        gemini_updated = False
        if "gemini_api_key" in payload:
            raw_key = str(payload["gemini_api_key"]).strip()
            if raw_key and " " not in raw_key and not raw_key.startswith("[MT5"):
                self.config.setdefault("ai", {})["api_key"] = raw_key
                gemini_updated = True
        save_config(self.config)
        return self.compute_full_state(force_ai=gemini_updated)

    def save_config(self) -> None:
        save_config(self.config)


# ---------------------------------------------------------------------------
# Pure-Python Async HTTP + RFC 6455 WebSocket Server
# ---------------------------------------------------------------------------

class WebSocketClient:
    def __init__(self, writer: asyncio.StreamWriter, symbol: str = "XAUUSD") -> None:
        self.writer = writer
        self.closed = False
        self.symbol = (symbol or "XAUUSD").upper()

    async def send_json(self, data: Dict[str, Any]) -> None:
        if self.closed:
            return
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        length = len(raw)
        header = bytearray([0x81])  # FIN + text frame
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        try:
            self.writer.write(bytes(header) + raw)
            await self.writer.drain()
        except Exception:
            self.closed = True


async def read_ws_frame(reader: asyncio.StreamReader) -> Optional[str]:
    try:
        head = await reader.readexactly(2)
    except Exception:
        return None
    opcode = head[0] & 0x0F
    if opcode == 0x8:  # Close frame
        return None
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    try:
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
        mask_key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length)
    except Exception:
        return None
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    if opcode == 0x9:  # Ping
        return "__PING__"
    return payload.decode("utf-8", "replace")


def safe_json_loads(data: bytes) -> Dict[str, Any]:
    """Robustly parse JSON bytes from HTTP clients, stripping null bytes and trailing noise."""
    text = data.decode("utf-8", "replace").strip("\x00 \r\n\t")
    if not text:
        return {}
    try:
        res = json.loads(text)
        return res if isinstance(res, dict) else {}
    except Exception:
        # Strip potential null byte truncation from MT5 WebRequest
        clean = text.split("\x00")[0].strip()
        if clean:
            try:
                res = json.loads(clean)
                return res if isinstance(res, dict) else {}
            except Exception:
                pass
        return {}


class AssistantServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        self.host = host
        self.port = port
        self.cores: Dict[str, TradingAssistantCore] = {
            "XAUUSD": TradingAssistantCore("XAUUSD"),
        }
        self.core = self.cores["XAUUSD"]
        self.clients: Set[WebSocketClient] = set()
        self.auto_replay = False
        self._is_computing: Dict[str, bool] = {}
        self._last_ws_broadcast_ts = 0.0
        self._last_tv_tick_ts = 0.0

    def get_core(self, symbol: Optional[str] = None) -> TradingAssistantCore:
        return self.cores["XAUUSD"]

    async def broadcast(self, message: Dict[str, Any], symbol: Optional[str] = None) -> None:
        target_sym = (symbol or message.get("symbol") or (message.get("risk_ticket") or {}).get("symbol") or "XAUUSD").upper()
        norm_sym = "BTCUSD" if "BTC" in target_sym else "XAUUSD"
        dead: List[WebSocketClient] = []
        for c in list(self.clients):
            c_sym = getattr(c, "symbol", "XAUUSD")
            if symbol == "*" or c_sym == norm_sym:
                await c.send_json(message)
                if c.closed:
                    dead.append(c)
        for c in dead:
            self.clients.discard(c)

    async def tv_ws_loop(self) -> None:
        """Primary Live Stream: real-time XAUUSD ticks directly from TradingView WebSocket (`OANDA:XAUUSD`)."""
        async def on_tv_tick(
            price: float,
            bid: Optional[float],
            ask: Optional[float],
            ts: int,
        ) -> None:
            gold_core = self.cores["XAUUSD"]
            has_subscribers = bool(self.clients)
            has_ea = gold_core.ea_bridge.auto_trading_enabled or gold_core.ea_bridge.is_connected
            if gold_core.hub.replay_mode or (not has_subscribers and not has_ea):
                return
            now = time.time()
            self._last_tv_tick_ts = now
            if has_subscribers and (now - self._last_ws_broadcast_ts < 0.25):
                return
            self._last_ws_broadcast_ts = now

            offset = float(gold_core.config.get("broker_price_offset") or 0.0)
            ingested = gold_core.hub.ingest_live_price(
                timeframe=gold_core.timeframe,
                price=price,
                bid=bid,
                ask=ask,
                ts=ts,
                offset_usd=offset,
                symbol="XAUUSD",
            )
            if not ingested:
                return
            b = ingested["bar"]
            ev = gold_core.order_manager.update_price_tick(
                current_price=float(b["c"]),
                high=float(b["h"]),
                low=float(b["l"]),
                ts=int(b["t"]),
                contract_size=100.0,
                config=gold_core.config,
                symbol="XAUUSD",
            )
            if ingested["new_bar_closed"] or ev:
                state = await asyncio.to_thread(gold_core.compute_full_state, None, True, False)
                if has_subscribers:
                    await self.broadcast(state, symbol="XAUUSD")
            elif has_subscribers:
                await self.broadcast(
                    {
                        "type": "tick",
                        "symbol": "XAUUSD",
                        "timeframe": gold_core.timeframe,
                        "bar": ingested["bar"],
                        "bid": ingested["bid"],
                        "ask": ingested["ask"],
                        "spread_points": ingested["spread_points"],
                        "active_order": gold_core.order_manager.get_active_order_dict(float(b["c"])),
                        "source": "tradingview_ws (OANDA:XAUUSD)",
                    },
                    symbol="XAUUSD",
                )

        await stream_tradingview_gold_ws(on_tv_tick)

    async def binance_ws_loop(self) -> None:
        """Fallback Stream: real-time gold trades from wss://stream.binance.com:9443/ws/paxgusdt@trade when TradingView WS is idle."""
        gold_core = self.cores["XAUUSD"]
        async def on_trade(trade_price: float, trade_ts: int) -> None:
            has_subscribers = bool(self.clients)
            has_ea = gold_core.ea_bridge.auto_trading_enabled or gold_core.ea_bridge.is_connected
            if gold_core.hub.replay_mode or (not has_subscribers and not has_ea):
                return
            # Priority guard: if TradingView WS is streaming within last 5s, suppress Binance PAXG fallback ticks
            if time.time() - self._last_tv_tick_ts < 5.0:
                return
            now = time.time()
            if has_subscribers and (now - self._last_ws_broadcast_ts < 0.25):
                return
            self._last_ws_broadcast_ts = now

            spot_equivalent = trade_price + gold_core.hub.basis_shift
            offset = float(gold_core.config.get("broker_price_offset") or 0.0)
            ingested = gold_core.hub.ingest_live_price(
                timeframe=gold_core.timeframe,
                price=spot_equivalent,
                ts=trade_ts,
                offset_usd=offset,
                symbol="XAUUSD",
            )
            if not ingested:
                return
            b = ingested["bar"]
            ev = gold_core.order_manager.update_price_tick(
                current_price=float(b["c"]),
                high=float(b["h"]),
                low=float(b["l"]),
                ts=int(b["t"]),
                contract_size=100.0,
                config=gold_core.config,
                symbol="XAUUSD",
            )
            if ingested["new_bar_closed"] or ev:
                state = await asyncio.to_thread(gold_core.compute_full_state, None, True, False)
                if has_subscribers:
                    await self.broadcast(state, symbol="XAUUSD")
            elif has_subscribers:
                await self.broadcast(
                    {
                        "type": "tick",
                        "symbol": "XAUUSD",
                        "timeframe": gold_core.timeframe,
                        "bar": ingested["bar"],
                        "active_order": gold_core.order_manager.get_active_order_dict(float(b["c"])),
                        "source": ingested["source"],
                    },
                    symbol="XAUUSD",
                )

        await stream_binance_gold_ws(on_trade)

    async def binance_btc_ws_loop(self) -> None:
        """Stream real-time BTC trades from wss://stream.binance.com:9443/ws/btcusdt@trade."""
        btc_core = self.cores["BTCUSD"]
        last_btc_broadcast = 0.0

        async def on_btc_trade(trade_price: float, trade_ts: int) -> None:
            nonlocal last_btc_broadcast
            has_subscribers = bool(self.clients)
            has_ea = btc_core.ea_bridge.auto_trading_enabled or btc_core.ea_bridge.is_connected
            if btc_core.hub.replay_mode or (not has_subscribers and not has_ea):
                return
            now = time.time()
            if has_subscribers and (now - last_btc_broadcast < 0.25):
                return
            last_btc_broadcast = now

            offset = float(btc_core.config.get("broker_price_offset_btc") or 0.0)
            ingested = btc_core.hub.ingest_live_price(
                timeframe=btc_core.timeframe,
                price=trade_price,
                ts=trade_ts,
                offset_usd=offset,
                symbol="BTCUSD",
            )
            if not ingested:
                return
            b = ingested["bar"]
            ev = btc_core.order_manager.update_price_tick(
                current_price=float(b["c"]),
                high=float(b["h"]),
                low=float(b["l"]),
                ts=int(b["t"]),
                contract_size=1.0,
                config=btc_core.config,
                symbol="BTCUSD",
            )
            if ingested["new_bar_closed"] or ev:
                state = await asyncio.to_thread(btc_core.compute_full_state, None, True, False)
                if has_subscribers:
                    await self.broadcast(state, symbol="BTCUSD")
            elif has_subscribers:
                await self.broadcast(
                    {
                        "type": "tick",
                        "symbol": "BTCUSD",
                        "timeframe": btc_core.timeframe,
                        "bar": ingested["bar"],
                        "active_order": btc_core.order_manager.get_active_order_dict(float(b["c"])),
                        "source": "binance_ws (btcusdt@trade)",
                    },
                    symbol="BTCUSD",
                )

        await stream_binance_btc_ws(on_btc_trade)

    async def tick_loop(self) -> None:
        """Poll real spot Bid/Ask and trigger updates for subscribed symbol clients."""
        loop_counter = 0
        while True:
            await asyncio.sleep(0.5)
            has_ea_any = any(c.ea_bridge.auto_trading_enabled or c.ea_bridge.is_connected or c.order_manager.has_active_order() for c in self.cores.values())
            if not self.clients and not has_ea_any:
                continue
            loop_counter += 1

            for sym_name, core_inst in list(self.cores.items()):
                has_sub = any(getattr(c, "symbol", "XAUUSD") == sym_name for c in self.clients)
                has_ea_target = core_inst.ea_bridge.auto_trading_enabled or core_inst.ea_bridge.is_connected or core_inst.order_manager.has_active_order()
                if not has_sub and not has_ea_target:
                    continue

                is_btc = "BTC" in sym_name
                # 1. Replay Mode handling
                if core_inst.hub.replay_mode:
                    if self.auto_replay and loop_counter % 2 == 0:
                        core_inst.hub.step_replay(1)
                        state = await asyncio.to_thread(core_inst.compute_full_state)
                        if has_sub:
                            await self.broadcast(state, symbol=sym_name)
                    elif core_inst.last_state and core_inst.last_state.get("bars"):
                        last_bar = core_inst.last_state["bars"][-1]
                        tick = core_inst.hub.simulate_live_tick(last_bar, offset_usd=0.0)
                        if has_sub:
                            await self.broadcast(
                                {
                                    "type": "tick",
                                    "symbol": sym_name,
                                    "timeframe": core_inst.timeframe,
                                    "bar": tick,
                                    "source": "offline_replay",
                                },
                                symbol=sym_name,
                            )
                    continue

                # 2. Live Mode
                spot = None
                if HAS_MT5_LIB and _mt5_initialized:
                    spot = await asyncio.to_thread(fetch_mt5_live_tick, core_inst.hub.preferred_symbol)
                if not spot and not is_btc:
                    if core_inst.ea_bridge.account_info.get("price") and not "BTC" in str(core_inst.ea_bridge.account_info.get("symbol", "")).upper():
                        acc = core_inst.ea_bridge.account_info
                        spot = {
                            "price": float(acc["price"]),
                            "bid": float(acc.get("bid") or acc["price"]),
                            "ask": float(acc.get("ask") or acc["price"]),
                            "spread_points": float(acc.get("spread_points") or 0.0),
                            "source": f"mt5_broker ({acc.get('symbol', 'XAUUSD')})",
                            "ts": int(time.time()),
                        }
                    elif not spot and (time.time() - self._last_tv_tick_ts > 5.0):
                        spot = await asyncio.to_thread(fetch_live_spot_xauusd)

                if spot:
                    core_inst.hub.latest_spot_quote = spot
                    is_mt5_feed = str(spot.get("source", "")).startswith("mt5_broker")
                    offset = 0.0 if is_mt5_feed else float(core_inst.config.get("broker_price_offset_btc" if is_btc else "broker_price_offset") or 0.0)
                    ingested = core_inst.hub.ingest_live_price(
                        timeframe=core_inst.timeframe,
                        price=spot["price"],
                        bid=spot["bid"],
                        ask=spot["ask"],
                        ts=spot["ts"],
                        offset_usd=offset,
                        symbol=sym_name,
                    )
                    if ingested:
                        b = ingested["bar"]
                        ev = core_inst.order_manager.update_price_tick(
                            current_price=float(b["c"]),
                            high=float(b["h"]),
                            low=float(b["l"]),
                            ts=int(b["t"]),
                            contract_size=1.0 if is_btc else 100.0,
                            config=core_inst.config,
                            symbol=sym_name,
                            market_context=(
                                {"structure": core_inst.last_state.get("structure"), "smc": core_inst.last_state.get("smc")}
                                if core_inst.last_state else None
                            ),
                        )
                        if ingested["new_bar_closed"] or ev or loop_counter % 10 == 0:
                            state = await asyncio.to_thread(
                                core_inst.compute_full_state, None, ingested["new_bar_closed"], False
                            )
                            if has_sub:
                                await self.broadcast(state, symbol=sym_name)
                        elif has_sub:
                            await self.broadcast(
                                {
                                    "type": "tick",
                                    "symbol": sym_name,
                                    "timeframe": core_inst.timeframe,
                                    "bar": ingested["bar"],
                                    "bid": ingested["bid"],
                                    "ask": ingested["ask"],
                                    "spread_points": ingested["spread_points"],
                                    "active_order": core_inst.order_manager.get_active_order_dict(float(b["c"])),
                                    "source": spot["source"],
                                },
                                symbol=sym_name,
                            )
                elif core_inst.last_state and core_inst.last_state.get("bars"):
                    last_b = core_inst.last_state["bars"][-1]
                    tick = core_inst.hub.simulate_live_tick(last_b, offset_usd=0.0)
                    ev = core_inst.order_manager.update_price_tick(
                        current_price=float(tick["c"]),
                        high=float(tick["h"]),
                        low=float(tick["l"]),
                        ts=int(tick["t"]),
                        contract_size=1.0 if is_btc else 100.0,
                        config=core_inst.config,
                        symbol=sym_name,
                    )
                    if ev or loop_counter % 10 == 0:
                        state = await asyncio.to_thread(core_inst.compute_full_state, None, False, False)
                        if has_sub:
                            await self.broadcast(state, symbol=sym_name)
                    elif has_sub:
                        await self.broadcast(
                            {
                                "type": "tick",
                                "symbol": sym_name,
                                "timeframe": core_inst.timeframe,
                                "bar": tick,
                                "active_order": core_inst.order_manager.get_active_order_dict(float(tick["c"])),
                                "source": core_inst.last_state.get("data_source", "live"),
                            },
                            symbol=sym_name,
                        )

                # Periodic autonomous setup scan: evaluate setups every 2-8 seconds when auto trading is ON
                is_rapid_on = bool((core_inst.config.get("rapid_fire_test") or {}).get("enabled", False))
                scan_mod = 4 if is_rapid_on else (6 if core_inst.trading_style == "micro_scalp" else 10)
                should_scan = (
                    core_inst.ea_bridge.auto_trading_enabled
                    and (is_rapid_on or not core_inst.order_manager.has_active_order())
                    and (loop_counter % scan_mod == 0)
                    and not self._is_computing.get(sym_name, False)
                )
                if should_scan:
                    self._is_computing[sym_name] = True
                    try:
                        state = await asyncio.to_thread(core_inst.compute_full_state, None, False, False)
                        if has_sub:
                            await self.broadcast(state, symbol=sym_name)
                    except Exception as e:
                        logger.warning(f"Autonomous periodic evaluation failed for {sym_name}: {e}")
                    finally:
                        self._is_computing[sym_name] = False

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            req_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not req_line:
                writer.close()
                return
            parts = req_line.decode("utf-8", "replace").strip().split(" ")
            if len(parts) < 2:
                writer.close()
                return
            method, full_path = parts[0].upper(), parts[1]
            norm_full_path = "/" + full_path.lstrip("/")
            parsed_url = urllib.parse.urlparse(norm_full_path)
            raw_path = parsed_url.path
            path = "/" + "/".join([seg for seg in raw_path.split("/") if seg])
            query = urllib.parse.parse_qs(parsed_url.query)

            headers: Dict[str, str] = {}
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
                decoded = line.decode("utf-8", "replace").strip()
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            # Check for WebSocket Upgrade on /ws
            if path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
                ws_key = headers.get("sec-websocket-key", "")
                accept = base64.b64encode(
                    hashlib.sha1((ws_key + WS_MAGIC).encode("utf-8")).digest()
                ).decode("ascii")
                handshake = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                )
                writer.write(handshake.encode("utf-8"))
                await writer.drain()

                ws_sym = (query.get("symbol") or ["XAUUSD"])[0].upper()
                sym_norm = "BTCUSD" if "BTC" in ws_sym else "XAUUSD"
                client = WebSocketClient(writer, symbol=sym_norm)
                self.clients.add(client)
                target_core = self.get_core(sym_norm)
                state = target_core.last_state or target_core.compute_full_state()
                await client.send_json(state)

                while not client.closed:
                    msg_text = await read_ws_frame(reader)
                    if msg_text is None:
                        break
                    if msg_text == "__PING__":
                        continue
                    try:
                        cmd = json.loads(msg_text)
                        await self.handle_ws_command(client, cmd)
                    except Exception:
                        pass
                self.clients.discard(client)
                writer.close()
                return

            content_len = int(headers.get("content-length") or 0)
            body_bytes = await reader.readexactly(content_len) if content_len > 0 else b""

            await self.handle_http_request(writer, method, path, query, body_bytes)
        except Exception:
            try:
                writer.close()
            except Exception:
                pass

    def dispatch_order_to_mt5(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Manually or explicitly dispatch the current setup / active order into MT5 EA queue."""
        core = self.get_core(symbol)
        act = core.order_manager.active_order
        last_st = core.last_state or {}
        rt = last_st.get("risk_ticket") or {}
        is_btc = "BTC" in core.symbol
        cur_p = float(
            (core.hub.latest_spot or {}).get("price")
            or (act.get("entry_mt5") if act else 0.0)
            or (rt.get("entry_mt5") or 0.0)
            or (core.hub.fixture_bars[-1]["c"] if core.hub.fixture_bars else (90500.0 if is_btc else 2650.0))
        )
        side = (act.get("side") if act else None) or rt.get("side") or "BUY"

        # If no active order exists, register one so Web UI tracks it and displays PnL
        if not act:
            req_exec_type = str(rt.get("execution_type") or "LIMIT").upper()
            if req_exec_type not in ("MARKET", "LIMIT"):
                req_exec_type = "LIMIT"
            entry_p = float(rt.get("entry_mt5") or cur_p)
            sl_dist = 150.0 if is_btc else 5.0
            tp_dist = 300.0 if is_btc else 10.0
            stop_p = float(rt.get("stop_mt5") or (entry_p - sl_dist if side == "BUY" else entry_p + sl_dist))
            target_p = float(rt.get("target_mt5") or (entry_p + tp_dist if side == "BUY" else entry_p - tp_dist))
            tp1_p = float(rt.get("tp1_mt5") or (entry_p + sl_dist if side == "BUY" else entry_p - sl_dist))
            lot = float(rt.get("lot_size") or (0.01 if is_btc else 0.10))
            order_desc = f"{side} NOW (Market)" if req_exec_type == "MARKET" else f"{side} LIMIT @ {entry_p:.2f}"
            core.order_manager.register_trade_setup(
                risk_ticket={
                    "symbol": core.symbol,
                    "side": side,
                    "entry_mt5": entry_p,
                    "stop_mt5": stop_p,
                    "tp1_mt5": tp1_p,
                    "target_mt5": target_p,
                    "lot_size": lot,
                    "contract_size": 1.0 if is_btc else 100.0,
                    "actual_risk_usd": float(rt.get("actual_risk_usd") or 50.0),
                    "potential_profit_usd": float(rt.get("potential_profit_usd") or 100.0),
                    "reward_risk": float(rt.get("reward_risk") or 2.0),
                    "order_type": order_desc,
                    "execution_type": req_exec_type,
                },
                trade_setup=last_st.get("smc", {}).get("trade_setup") or {},
                current_price=cur_p,
                timeframe=core.timeframe,
                trading_style=core.trading_style,
                force_replace=True,
                symbol=core.symbol,
            )
            act = core.order_manager.active_order

        sig_id = (act.get("order_id") if act else None) or rt.get("fingerprint") or f"manual_{int(time.time())}"
        # Discard so manual dispatch can re-queue even if already queued
        core.ea_bridge.executed_signal_ids.discard(sig_id)

        exec_type = (act.get("execution_type") if act else None) or rt.get("execution_type") or "LIMIT"
        sym = core.broker_symbol
        action = core.ea_bridge.enqueue_action(
            action_type="OPEN_ORDER",
            payload={
                "side": side,
                "order_type": exec_type,
                "symbol": sym,
                "lot": float((act.get("lot_size") if act else None) or rt.get("lot_size") or (0.01 if is_btc else 0.10)),
                "price": float((act.get("entry_mt5") if act else None) or cur_p),
                "sl": float((act.get("stop_mt5") if act else None) or rt.get("stop_mt5") or 0.0),
                "tp": float((act.get("target_mt5") if act else None) or rt.get("target_mt5") or 0.0),
                "tp1": float((act.get("tp1_mt5") if act else None) or rt.get("tp1_mt5") or 0.0),
                "magic": 797979,
                "comment": f"K79-{core.symbol[:3]}-{side}-MANUAL",
                "manual": True,
            },
            signal_id=sig_id,
        )
        return {"status": "ok", "action": action, "active_order": act}

    def dispatch_test_order(
        self,
        side: str = "BUY",
        order_type: str = "MARKET",
        lot: Optional[float] = None,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Dispatch an instant test BUY/SELL (Market or Limit) order directly into MT5 EA queue."""
        core = self.get_core(symbol)
        is_btc = "BTC" in core.symbol
        side = str(side or "BUY").upper()
        if side not in ("BUY", "SELL"):
            side = "BUY"
        order_type = str(order_type or "MARKET").upper()
        if order_type not in ("MARKET", "LIMIT"):
            order_type = "MARKET"

        cur_p = float(
            (core.hub.latest_spot or {}).get("price")
            or (core.hub.fixture_bars[-1]["c"] if core.hub.fixture_bars else (90500.0 if is_btc else 2650.0))
        )

        test_lot = float(lot) if lot and float(lot) > 0 else 0.01
        sl_dist = 150.0 if is_btc else 4.0
        tp_dist = 300.0 if is_btc else 8.0
        limit_offset = 100.0 if is_btc else 2.50
        contract_size = 1.0 if is_btc else 100.0

        # Calculate entry, SL, TP based on type
        if order_type == "MARKET":
            entry_p = round(cur_p, 2)
            if side == "BUY":
                sl_p = round(cur_p - sl_dist, 2)
                tp_p = round(cur_p + tp_dist, 2)
                tp1_p = round(cur_p + sl_dist, 2)
            else:
                sl_p = round(cur_p + sl_dist, 2)
                tp_p = round(cur_p - tp_dist, 2)
                tp1_p = round(cur_p - sl_dist, 2)
        else:  # LIMIT
            if side == "BUY":
                entry_p = round(cur_p - limit_offset, 2)
                sl_p = round(entry_p - sl_dist, 2)
                tp_p = round(entry_p + tp_dist, 2)
                tp1_p = round(entry_p + sl_dist, 2)
            else:
                entry_p = round(cur_p + limit_offset, 2)
                sl_p = round(entry_p + sl_dist, 2)
                tp_p = round(entry_p - tp_dist, 2)
                tp1_p = round(entry_p - sl_dist, 2)

        exec_desc = f"{side} NOW (Market Test)" if order_type == "MARKET" else f"{side} LIMIT (Pending Test)"

        # Register trade setup so Web UI tracks it and displays PnL
        core.order_manager.register_trade_setup(
            risk_ticket={
                "symbol": core.symbol,
                "side": side,
                "entry_mt5": entry_p,
                "stop_mt5": sl_p,
                "tp1_mt5": tp1_p,
                "target_mt5": tp_p,
                "lot_size": test_lot,
                "contract_size": contract_size,
                "actual_risk_usd": round(test_lot * contract_size * sl_dist, 2),
                "potential_profit_usd": round(test_lot * contract_size * tp_dist, 2),
                "reward_risk": 2.0,
                "order_type": exec_desc,
                "execution_type": order_type,
            },
            trade_setup={
                "symbol": core.symbol,
                "side": side,
                "setup_type": f"TEST_{order_type}",
                "entry": entry_p,
                "stop": sl_p,
                "tp1": tp1_p,
                "target": tp_p,
                "lot_size": test_lot,
                "confidence": "TEST_SIGNAL",
            },
            current_price=cur_p,
            timeframe=core.timeframe,
            trading_style=core.trading_style,
            force_replace=True,
            symbol=core.symbol,
        )
        act = core.order_manager.active_order

        # Generate unique signal_id for MT5 tracking
        sig_id = f"test_{core.symbol.lower()}_{side.lower()}_{order_type.lower()}_{int(time.time() * 1000)}"
        if act:
            act["order_id"] = sig_id

        # Discard previous executed record so manual dispatch always queues fresh
        core.ea_bridge.executed_signal_ids.discard(sig_id)

        sym = core.broker_symbol
        action = core.ea_bridge.enqueue_action(
            action_type="OPEN_ORDER",
            payload={
                "side": side,
                "order_type": order_type,
                "symbol": sym,
                "lot": test_lot,
                "price": entry_p,
                "sl": sl_p,
                "tp": tp_p,
                "tp1": tp1_p,
                "magic": 797979,
                "comment": f"K79-TEST-{core.symbol[:3]}-{side[:1]}{order_type[:1]}",
                "manual": True,
            },
            signal_id=sig_id,
        )
        return {
            "status": "ok",
            "message": f"ส่งคำสั่งทดสอบ {core.symbol} {side} ({order_type}) ขนาด {test_lot} Lot ไปยัง MT5 แล้ว",
            "action": action,
            "active_order": act,
        }

    async def handle_ws_command(self, client: WebSocketClient, cmd: Dict[str, Any]) -> None:
        cmd_sym = cmd.get("symbol") or getattr(client, "symbol", "XAUUSD")
        core = self.get_core(cmd_sym)
        action = str(cmd.get("action") or "")
        if action == "change_tf":
            tf = str(cmd.get("timeframe") or "M5")
            state = await asyncio.to_thread(core.compute_full_state, tf, True, False)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "change_style":
            style = str(cmd.get("trading_style") or "intraday").lower()
            tf = cmd.get("timeframe")
            if core.order_manager.has_active_order():
                act = core.order_manager.active_order
                if act and act.get("status") == "PENDING" and act.get("trading_style") != style:
                    core.order_manager.cancel_active_order(reason="STYLE_CHANGED")
            state = await asyncio.to_thread(core.compute_full_state, tf, True, True, style)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "step_replay":
            self.auto_replay = False
            steps = int(cmd.get("steps") or 1)
            core.hub.step_replay(steps)
            state = await asyncio.to_thread(core.compute_full_state)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "reset_replay":
            self.auto_replay = False
            core.hub.reset_replay(int(cmd.get("cursor") or 234))
            state = await asyncio.to_thread(core.compute_full_state, None, False, True)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "toggle_auto_replay":
            core.hub.replay_mode = True
            self.auto_replay = not self.auto_replay
            await self.broadcast({"type": "auto_replay_status", "auto_replay": self.auto_replay}, symbol=core.symbol)
        elif action == "live_mode":
            self.auto_replay = False
            core.hub.replay_mode = False
            state = await asyncio.to_thread(core.compute_full_state, None, True, True)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "analyze_now":
            state = await asyncio.to_thread(core.compute_full_state, None, True, True)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "cancel_active_order":
            reason = str(cmd.get("reason") or "MANUAL_CANCEL")
            act = core.order_manager.active_order
            if act and act.get("broker_ticket"):
                core.ea_bridge.enqueue_action(
                    action_type="CLOSE_ORDER",
                    payload={"ticket": int(act["broker_ticket"]), "symbol": core.symbol},
                )
            core.order_manager.cancel_active_order(reason=reason)
            state = await asyncio.to_thread(core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "close_active_order":
            cp = float(cmd.get("current_price") or core.hub.latest_spot.get("price") or 0.0)
            act = core.order_manager.active_order
            if act and act.get("broker_ticket"):
                core.ea_bridge.enqueue_action(
                    action_type="CLOSE_ORDER",
                    payload={"ticket": int(act["broker_ticket"]), "symbol": core.symbol},
                )
            core.order_manager.close_active_order(reason="MANUAL_CLOSE", current_price=cp)
            state = await asyncio.to_thread(core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "fill_active_order_now":
            cp = float(cmd.get("current_price") or core.hub.latest_spot.get("price") or 0.0)
            core.order_manager.fill_now(current_price=cp)
            state = await asyncio.to_thread(core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "set_breakeven":
            core.order_manager.set_breakeven()
            act = core.order_manager.active_order
            if act and act.get("broker_ticket"):
                core.ea_bridge.enqueue_action(
                    action_type="MODIFY_ORDER",
                    payload={
                        "ticket": int(act["broker_ticket"]),
                        "symbol": core.symbol,
                        "new_sl": float(act.get("entry_mt5") or 0.0),
                        "new_tp": float(act.get("target_mt5") or 0.0),
                    },
                )
            state = await asyncio.to_thread(core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "send_order_to_mt5":
            self.dispatch_order_to_mt5(symbol=core.symbol)
            state = await asyncio.to_thread(core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "dispatch_test_order":
            side = str(cmd.get("side") or "BUY")
            order_type = str(cmd.get("order_type") or "MARKET")
            lot = float(cmd.get("lot")) if cmd.get("lot") else None
            self.dispatch_test_order(side=side, order_type=order_type, lot=lot, symbol=core.symbol)
            state = await asyncio.to_thread(core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "scan_new_setup":
            core.order_manager.cancel_active_order(reason="SCAN_NEW_SETUP")
            state = await asyncio.to_thread(core.compute_full_state, None, True, True)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "toggle_cross_asset":
            ca_cfg = core.config.setdefault("cross_asset", {})
            req_enable = cmd.get("enabled")
            new_val = not bool(ca_cfg.get("enabled", False)) if req_enable is None else bool(req_enable)
            ca_cfg["enabled"] = new_val
            core.save_config()
            state = await asyncio.to_thread(core.compute_full_state)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "toggle_robot":
            req_enable = cmd.get("enabled")
            new_val = core.toggle_robot_mode(req_enable)
            state = await asyncio.to_thread(core.compute_full_state)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "toggle_rapid_fire":
            req_enable = cmd.get("enabled")
            new_val = core.toggle_rapid_fire_mode(req_enable)
            state = await asyncio.to_thread(core.compute_full_state)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "add_sub_order":
            cp = float(cmd.get("current_price") or core.hub.latest_spot.get("price") or 0.0)
            purpose = str(cmd.get("purpose") or "AUTO")
            exec_type = str(cmd.get("execution_type") or "MARKET")
            entry_p = float(cmd.get("entry_price") or cp)
            lot = float(cmd.get("lot_size")) if cmd.get("lot_size") else None
            core.order_manager.add_sub_order(
                purpose=purpose,
                execution_type=exec_type,
                current_price=cp,
                entry_price=entry_p,
                lot_size=lot,
            )
            state = await asyncio.to_thread(core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "close_order_by_id":
            cp = float(cmd.get("current_price") or core.hub.latest_spot.get("price") or 0.0)
            oid = str(cmd.get("order_id") or "")
            core.order_manager.close_order_by_id(order_id=oid, current_price=cp)
            state = await asyncio.to_thread(core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=core.symbol)
        elif action == "close_all_active_orders":
            cp = float(cmd.get("current_price") or core.hub.latest_spot.get("price") or 0.0)
            core.ea_bridge.enqueue_action(
                action_type="CLOSE_ALL",
                payload={"symbol": core.symbol, "magic": 797979},
            )
            core.order_manager.close_all_orders(current_price=cp)
            state = await asyncio.to_thread(core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=core.symbol)
        elif action in ("reload", "sync_mt5"):
            cp = float(cmd.get("current_price") or core.hub.latest_spot.get("price") or 0.0)
            ea_acc = core.ea_bridge.account_info or {}
            core.order_manager.force_sync_with_mt5(
                open_positions=ea_acc.get("open_positions") or [],
                pending_orders=ea_acc.get("pending_orders") or [],
                current_price=cp,
            )
            state = await asyncio.to_thread(core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=core.symbol)

    async def handle_http_request(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        body_bytes: bytes,
    ) -> None:
        sym_req = (query.get("symbol") or query.get("sym") or ["XAUUSD"])[0]
        core = self.get_core(sym_req)

        if path == "/api/state" and method == "GET":
            tf = (query.get("tf") or [core.timeframe])[0]
            force = (query.get("force") or ["false"])[0].lower() in ("1", "true")
            if core.last_state and not force and core.last_state.get("timeframe") == tf:
                await self.send_http_json(writer, 200, core.last_state)
                return
            state = await asyncio.to_thread(core.compute_full_state, tf)
            await self.send_http_json(writer, 200, state)
            return

        if path in ("/api/config", "/api/settings") and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            try:
                state = await asyncio.to_thread(target_core.update_settings, payload)
                await self.broadcast(state, symbol=target_core.symbol)
                await self.send_http_json(writer, 200, state)
            except Exception as e:
                await self.send_http_json(writer, 500, {"status": "error", "message": str(e)})
            return

        if path == "/api/replay" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            mode = payload.get("mode", "step")
            if mode == "reset":
                self.auto_replay = False
                target_core.hub.reset_replay(int(payload.get("cursor") or 234))
            elif mode == "live":
                self.auto_replay = False
                target_core.hub.replay_mode = False
            else:
                self.auto_replay = False
                target_core.hub.step_replay(int(payload.get("steps") or 1))
            state = target_core.compute_full_state(force_refresh=(mode == "live"), force_ai=True)
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(writer, 200, state)
            return

        if path == "/api/analyze" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            state = target_core.compute_full_state(force_ai=True)
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(writer, 200, state)
            return

        if path == "/api/order/action" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            action = str(payload.get("action") or "")
            cp = float(payload.get("current_price") or target_core.hub.latest_spot.get("price") or 0.0)
            if action == "cancel":
                target_core.order_manager.cancel_active_order(reason="MANUAL_CANCEL")
            elif action == "close":
                target_core.order_manager.close_active_order(reason="MANUAL_CLOSE", current_price=cp)
            elif action == "fill_now":
                target_core.order_manager.fill_now(current_price=cp)
            elif action == "set_breakeven":
                target_core.order_manager.set_breakeven()
            elif action == "scan_new_setup":
                target_core.order_manager.cancel_active_order(reason="SCAN_NEW_SETUP")
            elif action == "add_sub_order":
                purpose = str(payload.get("purpose") or "AUTO")
                exec_type = str(payload.get("execution_type") or "MARKET")
                entry_p = float(payload.get("entry_price") or cp)
                lot = float(payload.get("lot_size")) if payload.get("lot_size") else None
                target_core.order_manager.add_sub_order(
                    purpose=purpose,
                    execution_type=exec_type,
                    current_price=cp,
                    entry_price=entry_p,
                    lot_size=lot,
                )
            elif action == "close_by_id":
                oid = str(payload.get("order_id") or "")
                for o in target_core.order_manager.active_orders:
                    if o.get("order_id") == oid and o.get("broker_ticket"):
                        target_core.ea_bridge.enqueue_action(
                            "CLOSE_ORDER", {"ticket": o["broker_ticket"], "symbol": target_core.symbol}
                        )
                target_core.order_manager.close_order_by_id(order_id=oid, current_price=cp)
            elif action == "close_all":
                target_core.ea_bridge.enqueue_action(
                    "CLOSE_ALL", {"symbol": target_core.symbol, "magic": 797979}
                )
                target_core.order_manager.close_all_orders(current_price=cp)
            elif action == "close_ticket":
                t = int(payload.get("ticket") or 0)
                if t:
                    target_core.ea_bridge.enqueue_action(
                        "CLOSE_ORDER", {"ticket": t, "symbol": target_core.broker_symbol}
                    )
            elif action == "clean_duplicate_pending":
                seen = set()
                cancelled = 0
                mt5_pendings = (target_core.ea_bridge.account_info or {}).get("pending_orders") or []
                for po in list(mt5_pendings):
                    key = (str(po.get("type")), round(float(po.get("open_price") or 0.0), 1))
                    if key in seen:
                        target_core.ea_bridge.enqueue_action(
                            "CLOSE_ORDER", {"ticket": int(po["ticket"]), "symbol": target_core.broker_symbol}
                        )
                        cancelled += 1
                    else:
                        seen.add(key)
                state = await asyncio.to_thread(target_core.compute_full_state, None, True, False)
                await self.broadcast(state, symbol=target_core.symbol)
                await self.send_http_json(
                    writer,
                    200,
                    {
                        "status": "ok",
                        "message": f"ล้างคำสั่ง Pending Limit ที่ซ้ำซ้อนเรียบร้อยแล้ว ({cancelled} รายการ)",
                        "cancelled_count": cancelled,
                        "state": state,
                    },
                )
                return

            elif action in ("reload", "sync_mt5"):
                ea_acc = target_core.ea_bridge.account_info or {}
                sync_res = target_core.order_manager.force_sync_with_mt5(
                    open_positions=ea_acc.get("open_positions") or [],
                    pending_orders=ea_acc.get("pending_orders") or [],
                    current_price=cp,
                )
                state = await asyncio.to_thread(target_core.compute_full_state, None, True, False)
                await self.broadcast(state, symbol=target_core.symbol)
                await self.send_http_json(
                    writer,
                    200,
                    {
                        "status": "ok",
                        "message": f"ซิงค์สถานะกับ MT5 สำเร็จ: ล้างไม้ค้าง {sync_res['purged_count']} ไม้ (MT5 มี {sync_res['mt5_open_count']} ไม้)",
                        "sync_result": sync_res,
                        "state": state,
                    },
                )
                return

            if action in ("close", "cancel"):
                target_core.ea_bridge.enqueue_action(
                    "CLOSE_ALL", {"symbol": target_core.symbol, "magic": 797979}
                )
            elif action == "set_breakeven":
                act_ord = target_core.order_manager.active_order
                if act_ord and act_ord.get("broker_ticket"):
                    target_core.ea_bridge.enqueue_action(
                        "MODIFY_ORDER",
                        {
                            "ticket": act_ord["broker_ticket"],
                            "symbol": target_core.symbol,
                            "new_sl": act_ord.get("breakeven_sl_mt5", act_ord["entry_mt5"]),
                            "new_tp": act_ord.get("target_mt5", 0.0),
                        },
                    )

            state = await asyncio.to_thread(
                target_core.compute_full_state, None, True, action == "scan_new_setup"
            )
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(writer, 200, state)
            return

        if path in ("/api/order/reload", "/api/ea/sync") and method in ("GET", "POST"):
            sym = sym_req
            if method == "POST" and body_bytes:
                payload = safe_json_loads(body_bytes)
                sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            cp = float(target_core.hub.latest_spot.get("price") or 0.0)
            ea_acc = target_core.ea_bridge.account_info or {}
            sync_res = target_core.order_manager.force_sync_with_mt5(
                open_positions=ea_acc.get("open_positions") or [],
                pending_orders=ea_acc.get("pending_orders") or [],
                current_price=cp,
            )
            # Enqueue RESET_KILL_SWITCH to MT5 EA and unlatch local kill switch
            target_core.ea_bridge.enqueue_action(
                action_type="RESET_KILL_SWITCH",
                payload={"symbol": target_core.broker_symbol},
                signal_id=f"reset_ks_{int(time.time())}",
            )
            if target_core.ea_bridge.account_info:
                target_core.ea_bridge.account_info["kill_switch_tripped"] = False

            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "message": f"ซิงค์สถานะกับ MT5 สำเร็จ: ล้างไม้ค้าง {sync_res['purged_count']} ไม้ (MT5 มี {sync_res['mt5_open_count']} ไม้, รีเซ็ต Kill Switch สำเร็จ)",
                    "sync_result": sync_res,
                },
            )

            async def _bg_reload_update(tc=target_core):
                try:
                    st = await asyncio.to_thread(tc.compute_full_state, None, True, False)
                    await self.broadcast(st, symbol=tc.symbol)
                except Exception as e:
                    logger.warning(f"Background reload sync update failed: {e}")

            asyncio.create_task(_bg_reload_update())
            return

        if path in ("/api/order/purge_ghosts", "/api/order/clear_ghosts") and method in ("GET", "POST"):
            sym = sym_req
            if method == "POST" and body_bytes:
                payload = safe_json_loads(body_bytes)
                sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            cp = float(target_core.hub.latest_spot.get("price") or 0.0)
            ea_acc = target_core.ea_bridge.account_info or {}

            # 1. Force purge any active orders on server that do not exist in MT5
            sync_res = target_core.order_manager.force_sync_with_mt5(
                open_positions=ea_acc.get("open_positions") or [],
                pending_orders=ea_acc.get("pending_orders") or [],
                current_price=cp,
            )
            # 2. Clear orphaned action queue in ea_bridge
            target_core.ea_bridge.action_queue.clear()

            # 3. Enqueue RESET_KILL_SWITCH to MT5 EA and clear local kill switch
            target_core.ea_bridge.enqueue_action(
                action_type="RESET_KILL_SWITCH",
                payload={"symbol": target_core.broker_symbol},
                signal_id=f"reset_ks_{int(time.time())}",
            )
            if target_core.ea_bridge.account_info:
                target_core.ea_bridge.account_info["kill_switch_tripped"] = False

            state = await asyncio.to_thread(target_core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "message": f"ล้างไม้ผีเรียบร้อย (เคลียร์ไม้ตกค้าง {sync_res['purged_count']} ไม้ • MT5 เหลือ {sync_res['mt5_open_count']} ไม้)",
                    "purged_count": sync_res["purged_count"],
                    "purged_orders": sync_res["purged_orders"],
                    "mt5_open_count": sync_res["mt5_open_count"],
                    "state": state,
                },
            )
            return

        if path == "/api/daily_target/set" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            if "target_usd" in payload:
                target_core.config["daily_profit_target_usd"] = round(max(0.0, float(payload["target_usd"])), 2)
            if "enabled" in payload:
                target_core.config["daily_profit_target_enabled"] = bool(payload["enabled"])
            target_core.reanchor_pnl_targets()
            target_core.save_config()
            state = await asyncio.to_thread(target_core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "daily_profit_target_usd": target_core.config.get("daily_profit_target_usd"),
                    "daily_profit_target_enabled": target_core.config.get("daily_profit_target_enabled"),
                    "state": state,
                },
            )
            return

        if path == "/api/ai_memory/reset" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            rst = target_core.ai_memory.reset_memory()
            state = await asyncio.to_thread(target_core.compute_full_state, None, False, False)
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "cleared_trained_trades": rst.get("cleared_trained_trades", 0),
                    "message": f"🧹 รีเซ็ต AI Trade Memory เรียบร้อย (ล้างข้อมูลเทรนเก่า {rst.get('cleared_trained_trades', 0)} ไม้ เริ่มเรียนรู้ใหม่ตั้งแต่ต้น)",
                    "state": state,
                },
            )
            return

        if path == "/api/sideway/decision" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            decision = str(payload.get("decision") or "pause").strip().lower()
            target_core = self.get_core(sym)
            sg_res = target_core.apply_sideway_decision(decision)
            state = await asyncio.to_thread(target_core.compute_full_state, None, False, False)
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "decision": decision,
                    "sideway_guard": sg_res,
                    "message": sg_res.get("message", "บันทึกการตัดสินใจแล้ว"),
                    "state": state,
                },
            )
            return

        if path == "/api/system/reset" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            now_rst = int(time.time())

            # 1. Clear all loss-pattern vetoes, consecutive streaks & adaptive blocks
            loss_rst = target_core.loss_memory.full_reset()

            # 2. Re-anchor portfolio profit/loss targets to live MT5 balance & clear guards
            target_core.config["circuit_breaker_20pct_tripped"] = False
            if hasattr(target_core, "reanchor_pnl_targets"):
                target_core.reanchor_pnl_targets()
            else:
                ea_bal = float((target_core.ea_bridge.account_info or {}).get("balance") or target_core.config.get("account_balance") or 0.0)
                if ea_bal > 0:
                    target_core.config["account_balance"] = round(ea_bal, 2)
                    target_core.config["pnl_anchor_balance"] = round(ea_bal, 2)
                    target_core.config["pnl_anchor_ts"] = now_rst
                    target_core.config["pnl_anchor_bkk"] = _fmt_bkk(now_rst)
                    loss_pct = float(target_core.config.get("max_consecutive_loss_pct") or 20.0)
                    target_core.config["max_loss_usd"] = round(ea_bal * (loss_pct / 100.0), 2)

            # 3. Clear stuck EA queues, stale signal locks & kill switch (never closes real MT5 positions!)
            cleared_queue = len(target_core.ea_bridge.action_queue)
            target_core.ea_bridge.action_queue.clear()
            target_core.ea_bridge.executed_signal_ids.clear()
            if target_core.ea_bridge.account_info:
                target_core.ea_bridge.account_info["kill_switch_tripped"] = False
            target_core.ea_bridge.enqueue_action(
                action_type="RESET_KILL_SWITCH",
                payload={"symbol": target_core.broker_symbol},
                signal_id=f"sys_reset_{now_rst}",
            )

            # 4. Purge ghost server-side orders not present in MT5 (keep any real open MT5 positions untouched!)
            live_pos_tickets = {
                int(p.get("ticket"))
                for p in ((target_core.ea_bridge.account_info or {}).get("positions") or [])
                if p.get("ticket")
            }
            live_pend_tickets = {
                int(p.get("ticket"))
                for p in ((target_core.ea_bridge.account_info or {}).get("pending_orders") or [])
                if p.get("ticket")
            }
            live_all_tickets = live_pos_tickets | live_pend_tickets
            kept_orders = []
            purged_ghosts = 0
            for o in list(target_core.order_manager.active_orders):
                bt = o.get("broker_ticket")
                if bt and int(bt) in live_all_tickets:
                    kept_orders.append(o)
                else:
                    purged_ghosts += 1
            target_core.order_manager.active_orders = kept_orders
            target_core.order_manager.active_order = kept_orders[0] if kept_orders else None
            # Clamp any legacy future MT5 server-time closed_ts in history so they never re-trigger streaks
            for ho in getattr(target_core.order_manager, "order_history", []):
                h_ts = int(ho.get("closed_ts") or 0)
                if h_ts > now_rst:
                    ho["closed_ts"] = now_rst - 1
                ho["loss_pattern_recorded"] = True
            target_core.order_manager.save_to_disk()

            # 5. Re-enable Auto-Trading, clear AI cooldown & sideway pause, and save config
            target_core.ea_bridge.set_auto_trading(True)
            target_core.config.setdefault("auto_trading", {})["enabled"] = True
            target_core.sideway_guard["status"] = "NORMAL"
            target_core.sideway_guard["prompt_ts"] = 0
            target_core.sideway_guard["deadline_ts"] = 0
            target_core.sideway_guard["sideway_confirm_count"] = 0
            target_core.sideway_guard["reset_grace_until_ts"] = now_rst + 120
            if hasattr(target_core, "last_ai_eval_ts"):
                target_core.last_ai_eval_ts = 0.0
            if hasattr(target_core, "cached_ai"):
                target_core.cached_ai = None
            target_core.save_config()

            # 6. Immediate fresh evaluation & live broadcast
            state = await asyncio.to_thread(target_core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=target_core.symbol)
            live_bal_now = float(target_core.config.get("account_balance") or 0.0)
            msg_th = (
                f"🔄 รีเซ็ตระบบสำเร็จ! ปลดล็อก Veto {loss_rst['cleared_patterns']} รูปแบบ, "
                f"ล้างคิวค้าง {cleared_queue} / ไม้ผี {purged_ghosts}, "
                f"ตั้งฐาน Balance ใหม่ที่ ${live_bal_now:.2f} และพร้อมออกออเดอร์ทันที"
            )
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "message": msg_th,
                    "reset_summary": {
                        "cleared_patterns": loss_rst["cleared_patterns"],
                        "cleared_queue": cleared_queue,
                        "purged_ghosts": purged_ghosts,
                        "kept_mt5_orders": len(kept_orders),
                        "new_anchor_balance": live_bal_now,
                    },
                    "state": state,
                },
            )
            return

        if path == "/api/circuit_breaker/reset" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            target_core.loss_memory.reset_circuit_breaker()
            target_core.config["circuit_breaker_20pct_tripped"] = False
            target_core.reanchor_pnl_targets()
            target_core.ea_bridge.set_auto_trading(True)
            target_core.save_config()
            target_core.ea_bridge.enqueue_action(
                action_type="RESET_KILL_SWITCH",
                payload={"symbol": target_core.broker_symbol},
                signal_id=f"reset_cb_{int(time.time())}",
            )
            if target_core.ea_bridge.account_info:
                target_core.ea_bridge.account_info["kill_switch_tripped"] = False
            state = await asyncio.to_thread(target_core.compute_full_state, None, True, False)
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "message": "รีเซ็ต Circuit Breaker และเปิดให้ระบบทำการวิเคราะห์/เทรดรอบใหม่เรียบร้อยแล้ว",
                    "state": state,
                },
            )
            return

        if path == "/api/reanalysis/run" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            state = await asyncio.to_thread(target_core.compute_full_state, None, True, False)
            structure = state.get("structure") or {}
            mtf = state.get("mtf") or {}
            macro = state.get("macro") or {}
            acc_bal = float(target_core.config.get("account_balance") or 5000.0)
            report = target_core.loss_memory.run_deep_reanalysis(
                symbol=sym,
                timeframe=target_core.timeframe,
                structure=structure,
                mtf=mtf,
                macro=macro,
                account_balance=acc_bal,
            )
            state["loss_memory"] = target_core.loss_memory.get_status()
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "report": report,
                    "state": state,
                },
            )
            return

        if path == "/api/loss_memory/status" and method == "GET":
            target_core = self.get_core(sym_req)
            await self.send_http_json(writer, 200, target_core.loss_memory.get_status())
            return

        # --- MT5 EA Bridge HTTP Endpoints ---
        if path == "/api/ea/poll" and method in ("GET", "POST"):
            actions = []
            auto_trading_any = False
            for c in self.cores.values():
                c_actions = c.ea_bridge.poll_actions()
                if c_actions:
                    actions.extend(c_actions)
                c.ea_bridge.record_poll()
                if c.ea_bridge.auto_trading_enabled:
                    auto_trading_any = True

            resp_doc = {
                "status": "ok",
                "auto_trading_enabled": auto_trading_any,
                "has_actions": len(actions) > 0,
                "actions": actions,
                "next_poll_ms": 100 if len(actions) > 0 else 150,
                "server_time": int(time.time()),
            }
            await self.send_http_json(writer, 200, resp_doc)
            return

        if path == "/api/ea/heartbeat" and method == "POST":
            payload = safe_json_loads(body_bytes)
            ea_sym = str(payload.get("symbol") or "XAUUSD").upper()
            target_core = self.get_core(ea_sym)
            cached_bars = (target_core.hub.cached_bars_by_tf.get(target_core.timeframe) or [])
            ref_px = float(cached_bars[-1].get("c")) if cached_bars and cached_bars[-1].get("c") else None
            info = target_core.ea_bridge.record_heartbeat(payload, reference_feed_price=ref_px)
            # Sync balance / equity, live spread & auto-calibrated broker offset
            if info.get("equity") and float(info["equity"]) > 0:
                target_core.config["account_balance"] = float(info["equity"])
            if info.get("spread_points") and float(info["spread_points"]) > 0:
                target_core.config["broker_spread_points"] = float(info["spread_points"])
            if (
                target_core.config.get("auto_sync_broker_offset", True)
                and len(target_core.ea_bridge.basis_samples) >= 2
            ):
                target_core.config["broker_price_offset"] = float(info.get("dynamic_offset_usd") or 0.0)
            if info.get("symbol"):
                target_core.hub.preferred_symbol = str(info["symbol"]).strip()
            # Ingest live broker price / tick directly from MT5 EA heartbeat
            if info.get("price") and float(info["price"]) > 0:
                cur_p = float(info["price"])
                bid_p = float(info.get("bid") or cur_p)
                ask_p = float(info.get("ask") or cur_p)
                target_core.hub.latest_spot = {
                    "price": cur_p,
                    "bid": bid_p,
                    "ask": ask_p,
                    "spread_points": float(info.get("spread_points") or 0.0),
                    "source": f"mt5_broker ({info.get('symbol', target_core.symbol)})",
                    "time": int(time.time()),
                    "ts": int(time.time()),
                }
            # Reconcile tracked orders against MT5 open positions & pending orders
            target_core.order_manager.reconcile_mt5_positions(
                open_positions=info.get("open_positions") or [],
                pending_orders=info.get("pending_orders") or [],
                current_price=float(info.get("price") or 0.0),
            )
            # Broadcast lightweight telemetry so web UI sees live MT5 status immediately without resetting chart
            ea_status_msg = {
                "type": "ea_heartbeat",
                "symbol": target_core.symbol,
                "ea_bridge": target_core.ea_bridge.get_status_dict(),
                "active_order": target_core.order_manager.get_active_order_dict(float(info.get("price") or 0.0)),
                "order_history": [
                    o
                    for o in target_core.order_manager.order_history
                    if not str(o.get("order_id") or "").startswith("test_")
                    and o.get("close_reason") not in ("TEST_CLEANUP", "SYSTEM_INIT")
                ][-50:],
            }
            await self.broadcast(ea_status_msg, symbol=target_core.symbol)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "auto_trading_enabled": target_core.ea_bridge.auto_trading_enabled,
                    "server_time": int(time.time()),
                },
            )
            return

        if path == "/api/ea/execution" and method == "POST":
            payload = safe_json_loads(body_bytes)
            ea_sym = str(payload.get("symbol") or "XAUUSD").upper()
            target_core = self.get_core(ea_sym)
            rec = target_core.ea_bridge.record_execution(payload)
            target_core.order_manager.bind_execution_report(rec)
            await self.send_http_json(writer, 200, {"status": "ok", "execution": rec})

            async def _bg_exec_update(tc=target_core):
                try:
                    st = await asyncio.to_thread(tc.compute_full_state)
                    await self.broadcast(st, symbol=tc.symbol)
                except Exception as e:
                    print(f"[EA-EXEC] Background execution state update failed: {e}")

            asyncio.create_task(_bg_exec_update())
            return

        if path == "/api/ea/toggle_auto" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            req_enable = payload.get("enabled")
            if req_enable is None:
                new_val = target_core.ea_bridge.toggle_auto_trading()
            else:
                new_val = target_core.ea_bridge.set_auto_trading(bool(req_enable))
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "auto_trading_enabled": new_val,
                    "ea_bridge": target_core.ea_bridge.get_status_dict(),
                },
            )

            async def _bg_toggle_update(tc=target_core):
                try:
                    st = await asyncio.to_thread(tc.compute_full_state)
                    await self.broadcast(st, symbol=tc.symbol)
                except Exception as e:
                    logger.warning(f"Background toggle state update failed: {e}")

            asyncio.create_task(_bg_toggle_update())
            return

        if path == "/api/ea/send_order" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            res = self.dispatch_order_to_mt5(symbol=sym)
            target_core = self.get_core(sym)
            await self.send_http_json(writer, 200, res)

            async def _bg_send_update(tc=target_core):
                try:
                    st = await asyncio.to_thread(tc.compute_full_state, None, True, False)
                    await self.broadcast(st, symbol=tc.symbol)
                except Exception as e:
                    logger.warning(f"Background send_order state update failed: {e}")

            asyncio.create_task(_bg_send_update())
            return

        if path == "/api/ea/test_order" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            side = str(payload.get("side") or "BUY")
            order_type = str(payload.get("order_type") or "MARKET")
            lot = float(payload.get("lot")) if payload.get("lot") else None
            res = self.dispatch_test_order(side=side, order_type=order_type, lot=lot, symbol=sym)
            target_core = self.get_core(sym)
            await self.send_http_json(writer, 200, res)

            async def _bg_test_update(tc=target_core):
                try:
                    st = await asyncio.to_thread(tc.compute_full_state, None, True, False)
                    await self.broadcast(st, symbol=tc.symbol)
                except Exception as e:
                    logger.warning(f"Background test_order state update failed: {e}")

            asyncio.create_task(_bg_test_update())
            return

        if path == "/api/cross_asset/toggle" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            ca_cfg = target_core.config.setdefault("cross_asset", {})
            req_enable = payload.get("enabled")
            if req_enable is None:
                new_val = not bool(ca_cfg.get("enabled", False))
            else:
                new_val = bool(req_enable)
            ca_cfg["enabled"] = new_val
            target_core.save_config()
            state = await asyncio.to_thread(target_core.compute_full_state)
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "cross_asset_enabled": new_val,
                    "cross_asset": ca_cfg,
                },
            )
            return

        if path == "/api/robot/toggle" and method == "POST":
            target_core = self.get_core(sym_req)
            target_core.toggle_robot_mode(False)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "deprecated",
                    "robot_scalper_enabled": False,
                    "message": "โหมด Robot Scalp ถูกถอดออกจากระบบอย่างถาวรเนื่องจากความเสี่ยงสูง",
                    "robot_scalper": {"enabled": False},
                },
            )
            return

        if path == "/api/robot/status" and method == "GET":
            await self.send_http_json(writer, 200, {"enabled": False, "status": "deprecated"})
            return

        if path == "/api/rapid_fire/toggle" and method == "POST":
            payload = safe_json_loads(body_bytes)
            sym = payload.get("symbol") or sym_req
            target_core = self.get_core(sym)
            req_enable = payload.get("enabled")
            new_val = target_core.toggle_rapid_fire_mode(req_enable)
            state = await asyncio.to_thread(target_core.compute_full_state)
            await self.broadcast(state, symbol=target_core.symbol)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "rapid_fire_enabled": new_val,
                    "rapid_fire_test": target_core.config.get("rapid_fire_test", {}),
                },
            )
            return

        if path == "/api/rapid_fire/status" and method == "GET":
            sym = (query.get("symbol") or ["XAUUSD"])[0]
            target_core = self.get_core(sym)
            await self.send_http_json(writer, 200, target_core.config.get("rapid_fire_test", {}))
            return

        if path == "/api/ea/status" and method == "GET":
            sym = (query.get("symbol") or ["XAUUSD"])[0]
            target_core = self.get_core(sym)
            await self.send_http_json(writer, 200, target_core.ea_bridge.get_status_dict())
            return

        rel_path = "index.html" if path in ("/", "", "/btc", "/btc.html") else path.lstrip("/")
        file_path = os.path.abspath(os.path.join(WEB_DIR, rel_path))
        if not file_path.startswith(WEB_DIR) or not os.path.isfile(file_path):
            await self.send_http_json(writer, 404, {"error": f"Not found: {path}"})
            return

        mime, _ = mimetypes.guess_type(file_path)
        content_type = mime or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        with open(file_path, "rb") as fh:
            data = fh.read()
        resp = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Cache-Control: no-cache\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + data
        writer.write(resp)
        await writer.drain()
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def send_http_json(
        self, writer: asyncio.StreamWriter, status: int, doc: Dict[str, Any]
    ) -> None:
        raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        status_text = "OK" if status == 200 else "Not Found" if status == 404 else "Error"
        resp = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(raw)}\r\n"
            f"Cache-Control: no-cache\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + raw
        writer.write(resp)
        await writer.drain()
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def serve_forever(self) -> None:
        listen_host = self.host if self.host else "0.0.0.0"
        try:
            server = await asyncio.start_server(
                self.handle_client, listen_host, self.port, reuse_address=True
            )
        except OSError:
            # Automatically terminate any old server instance still holding the port
            if os.name != "nt":
                try:
                    out = subprocess.check_output(
                        ["lsof", "-ti", f"tcp:{self.port}"], text=True
                    ).strip()
                    my_pid = os.getpid()
                    for line in out.splitlines():
                        pid = int(line.strip())
                        if pid != my_pid:
                            os.kill(pid, signal.SIGTERM)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
            else:
                try:
                    out = subprocess.check_output(
                        f"netstat -ano | findstr :{self.port}", shell=True, text=True
                    )
                    my_pid = os.getpid()
                    for line in out.splitlines():
                        parts = line.strip().split()
                        if len(parts) >= 5 and "LISTENING" in line.upper():
                            pid = int(parts[-1])
                            if pid != my_pid and pid > 0:
                                subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
            try:
                server = await asyncio.start_server(
                    self.handle_client, listen_host, self.port, reuse_address=True
                )
            except OSError as exc:
                print(f"❌ Failed to bind to port {self.port}: {exc}")
                print(f"👉 Please close the process using port {self.port} or run with: python app.py --port 8001")
                raise

        for c in self.cores.values():
            c.compute_full_state(force_refresh=True)
        tick_task = asyncio.create_task(self.tick_loop())
        tv_task = asyncio.create_task(self.tv_ws_loop())
        ws_task = asyncio.create_task(self.binance_ws_loop())

        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
        except Exception:
            lan_ip = "127.0.0.1"

        print("=" * 64)
        print("🚀 KAIROS-79 (Precision Gold Intelligence & MT5 Execution System) Online")
        print(f"🟡 Gold (XAU/USD) Dashboard:  http://localhost:{self.port}/")
        print(f"🌐 Web Dashboard (LAN/VPS):  http://{lan_ip}:{self.port}")
        print(f"🤖 MT5 EA Bridge URL:        http://{lan_ip}:{self.port} (or http://127.0.0.1:{self.port})")
        print(f"Active Data Feed:            {self.core.last_state.get('data_source') if self.core.last_state else 'initializing'}")
        print("=" * 64)
        loop = asyncio.get_running_loop()
        def _win_exc_handler(l, context):
            exc = context.get("exception")
            if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (64, 10054, 121):
                return
            l.default_exception_handler(context)
        loop.set_exception_handler(_win_exc_handler)

        try:
            async with server:
                await server.serve_forever()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        except Exception as e:
            logger.error(f"Server serve error: {e}", exc_info=True)
        finally:
            tick_task.cancel()
            tv_task.cancel()
            ws_task.cancel()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run KAIROS-79 (Precision Gold Timing & Risk System) Web Server.")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    srv = AssistantServer(host=args.host, port=args.port)
    try:
        asyncio.run(srv.serve_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
