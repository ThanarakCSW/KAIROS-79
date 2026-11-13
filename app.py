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
import mimetypes
import os
import signal
import struct
import subprocess
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Set

from advisor.ai_evaluator import (
    evaluate_chart_with_gemini_or_fallback,
    finalize_advisory,
)
from engine.data_feed import (
    MarketDataHub,
    fetch_live_spot_xauusd,
    fetch_mt5_live_tick,
    stream_binance_gold_ws,
    stream_tradingview_gold_ws,
)
from engine.news_feed import evaluate_news_window
from engine.ea_bridge import EABridgeManager
from engine.order_lifecycle import OrderLifecycleManager
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

    def __init__(self) -> None:
        self.config = load_config()
        self.hub = MarketDataHub()
        self.order_manager = OrderLifecycleManager()
        self.ea_bridge = EABridgeManager(CONFIG_PATH)
        self.timeframe = str(self.config.get("default_timeframe") or "M5").upper()
        self.trading_style = str(self.config.get("trading_style") or "intraday").lower()
        if self.trading_style not in ("intraday", "scalping"):
            self.trading_style = "intraday"
        self.last_state: Optional[Dict[str, Any]] = None
        self.last_alert_fingerprint: Optional[str] = None

    def compute_full_state(
        self,
        timeframe: Optional[str] = None,
        force_refresh: bool = False,
        force_ai: bool = False,
        trading_style: Optional[str] = None,
    ) -> Dict[str, Any]:
        tf = (timeframe or self.timeframe).upper()
        self.timeframe = tf
        if trading_style in ("intraday", "scalping"):
            self.trading_style = trading_style
            self.config["trading_style"] = trading_style

        feed = self.hub.get_bars(tf, force_refresh=force_refresh)
        raw_bars = feed["bars"]
        offset = float(self.config.get("broker_price_offset") or 0.0)
        spread_pts = float(self.config.get("broker_spread_points") or 25.0)
        point_size = float(self.config.get("point_size") or 0.01)
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

        mtf_bars = self.hub.get_mtf_bars(primary_tf=tf, primary_bars=chart_bars, offset_usd=offset)
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
            trading_style=self.trading_style,
        )
        replays_cnt = max(35000, int(self.config.get("simulation_replays") or 35000))
        self.config["simulation_replays"] = replays_cnt
        bars_ahead_cnt = 18 if self.trading_style == "scalping" else int(self.config.get("simulation_bars_ahead") or 36)
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

        # Let Gemini analyze the full chart (raw bars + MTF + Macro + SMC + 15,000-path baseline) and decide the setup
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

        now_day = int(time.time()) // 86400
        trades_today_count = sum(
            1
            for o in getattr(self.order_manager, "order_history", [])
            if (int(o.get("created_ts") or 0) // 86400) == now_day
        )
        open_portfolio_risk_usd = (
            sum(float(o.get("actual_risk_usd") or 0.0) for o in self.order_manager.active_orders)
            if not self.order_manager.has_active_order()
            else 0.0
        )
        ea_acc = self.ea_bridge.account_info or {}
        cfg_for_risk = dict(
            self.config,
            broker_price_offset=0.0,
            trading_style=self.trading_style,
            min_win_prob_pct=min_win_prob,
            current_daily_loss_pct=float(ea_acc.get("daily_dd_pct") or 0.0),
            kill_switch_tripped=bool(ea_acc.get("kill_switch_tripped", False)),
            trades_today_count=trades_today_count,
            open_portfolio_risk_usd=open_portfolio_risk_usd,
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
            config=self.config,
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
                            self.ea_bridge.enqueue_action("CLOSE_ORDER", {"ticket": int(bt), "symbol": "XAUUSD"})

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
                                    "symbol": ord_item.get("symbol", "XAUUSD"),
                                    "new_sl": be_sl,
                                    "new_tp": float(ord_item.get("target_mt5") or 0.0),
                                },
                                signal_id=f"{ord_item['order_id']}-NEWS-BE",
                            )

        min_conf = int((self.config.get("ai") or {}).get("auto_trigger_min_confluence") or 55)
        if self.order_manager.has_active_order():
            act = self.order_manager.active_order
            if act and act.get("status") == "PENDING" and act.get("trading_style") != self.trading_style:
                self.order_manager.cancel_active_order(reason="STYLE_CHANGED")

        active_ord = self.order_manager.get_active_order_dict(cur_p) if self.order_manager.has_active_order() else None
        is_same_style_order = bool(active_ord and active_ord.get("trading_style") == self.trading_style)

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
            if auto_event and not order_event:
                order_event = auto_event

            if active_ord:
                cur_lev = max(1.0, float(self.config.get("leverage") or 100.0))
                act_lot = float(active_ord.get("total_lot_size", active_ord["lot_size"]))
                act_entry = float(active_ord.get("avg_entry_price", active_ord["entry_mt5"]))
                cur_bal = float(self.config.get("account_balance", 5000.0))
                contract_size = float(self.config.get("contract_size", 100.0))
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
        else:
            if (
                risk_ticket.get("win_prob_passed", False)
                and risk_ticket.get("verdict") in ("PASS", "CAUTION")
                and smc["trade_setup"].get("confluence_score", 0) >= min_conf
            ):
                if not self.order_manager.has_active_order():
                    self.order_manager.register_trade_setup(
                        risk_ticket=risk_ticket,
                        smc_setup=smc["trade_setup"],
                        current_price=cur_p,
                        timeframe=tf,
                        trading_style=self.trading_style,
                    )
                risk_ticket["active_order"] = self.order_manager.get_active_order_dict(cur_p)
                order_event = self.order_manager.last_event
            else:
                risk_ticket["active_order"] = active_ord

        # Auto-Trading Dispatch to MT5 EA Bridge when Auto-Trading is ON
        if self.ea_bridge.auto_trading_enabled:
            # Case 1: Primary active order needing dispatch (never re-enqueue once dispatched/rejected)
            act_ord = self.order_manager.active_order
            if act_ord and act_ord.get("status") in ("PENDING", "FILLED") and act_ord.get("order_id"):
                sig_id = act_ord["order_id"]
                has_ticket = bool(act_ord.get("broker_ticket"))
                is_in_queue = any(a.get("signal_id") == sig_id for a in self.ea_bridge.action_queue)
                already_dispatched = (
                    sig_id in self.ea_bridge.executed_signal_ids
                    or bool(act_ord.get("ea_dispatch_status"))
                )
                if not has_ticket and not is_in_queue and not already_dispatched:
                    self.ea_bridge.enqueue_action(
                        action_type="OPEN_ORDER",
                        payload={
                            "side": act_ord["side"],
                            "order_type": act_ord.get("execution_type", "LIMIT"),
                            "symbol": act_ord.get("symbol", "XAUUSD"),
                            "lot": float(act_ord.get("lot_size") or risk_ticket.get("lot_size") or 0.10),
                            "price": float(act_ord.get("entry_mt5") or cur_p),
                            "sl": float(act_ord.get("stop_mt5") or 0.0),
                            "tp": float(act_ord.get("target_mt5") or 0.0),
                            "tp1": float(act_ord.get("tp1_mt5") or 0.0),
                            "magic": 797979,
                            "comment": f"K79-{act_ord['side']}",
                        },
                        signal_id=sig_id,
                    )
            elif (
                order_event
                and order_event.get("type") == "NEW_ORDER_REGISTERED"
                and risk_ticket.get("win_prob_passed", False)
                and risk_ticket.get("verdict") in ("PASS", "CAUTION")
            ):
                sig_id = risk_ticket.get("fingerprint") or f"sig_{int(time.time())}"
                self.ea_bridge.enqueue_action(
                    action_type="OPEN_ORDER",
                    payload={
                        "side": risk_ticket["side"],
                        "order_type": risk_ticket.get("execution_type", "LIMIT"),
                        "symbol": "XAUUSD",
                        "lot": float(risk_ticket.get("lot_size") or 0.10),
                        "price": float(risk_ticket.get("entry_mt5") or cur_p),
                        "sl": float(risk_ticket.get("stop_mt5") or 0.0),
                        "tp": float(risk_ticket.get("target_mt5") or 0.0),
                        "tp1": float(risk_ticket.get("tp1_mt5") or 0.0),
                        "magic": 797979,
                        "comment": f"K79-{risk_ticket['side']}",
                    },
                    signal_id=sig_id,
                )

            # Case 2: Multi-orders (scale-in / recovery) needing dispatch
            if order_event and order_event.get("type") in ("MULTI_ORDER_ADDED", "AUTO_ORDER_ADDED", "SUB_ORDER_ADDED"):
                sub_o = order_event.get("order") or {}
                if not sub_o and order_event.get("order_id"):
                    sub_o = next(
                        (o for o in self.order_manager.active_orders if o.get("order_id") == order_event.get("order_id")),
                        {},
                    )
                sub_id = sub_o.get("order_id")
                if (
                    sub_id
                    and sub_id not in self.ea_bridge.executed_signal_ids
                    and not sub_o.get("broker_ticket")
                    and not sub_o.get("ea_dispatch_status")
                ):
                    self.ea_bridge.enqueue_action(
                        action_type="OPEN_ORDER",
                        payload={
                            "side": sub_o.get("side", risk_ticket["side"]),
                            "order_type": sub_o.get("execution_type", "MARKET"),
                            "symbol": sub_o.get("symbol", "XAUUSD"),
                            "lot": float(sub_o.get("lot_size") or 0.10),
                            "price": float(sub_o.get("entry_mt5") or cur_p),
                            "sl": float(sub_o.get("stop_mt5") or 0.0),
                            "tp": float(sub_o.get("target_mt5") or 0.0),
                            "magic": 797979,
                            "comment": f"K79-{sub_o.get('purpose', 'MULTI')}",
                        },
                        signal_id=sub_id,
                    )
            elif len(self.order_manager.active_orders) > 1:
                for sub_o in self.order_manager.active_orders[1:]:
                    sub_id = sub_o.get("order_id")
                    if (
                        sub_id
                        and sub_id not in self.ea_bridge.executed_signal_ids
                        and not sub_o.get("broker_ticket")
                        and not sub_o.get("ea_dispatch_status")
                        and sub_o.get("status") in ("PENDING", "FILLED")
                    ):
                        self.ea_bridge.enqueue_action(
                            action_type="OPEN_ORDER",
                            payload={
                                "side": sub_o.get("side", act_ord["side"] if act_ord else "BUY"),
                                "order_type": sub_o.get("execution_type", "MARKET"),
                                "symbol": sub_o.get("symbol", "XAUUSD"),
                                "lot": float(sub_o.get("lot_size") or 0.10),
                                "price": float(sub_o.get("entry_mt5") or cur_p),
                                "sl": float(sub_o.get("stop_mt5") or 0.0),
                                "tp": float(sub_o.get("target_mt5") or 0.0),
                                "magic": 797979,
                                "comment": f"K79-{sub_o.get('purpose', 'MULTI')}",
                            },
                            signal_id=sub_id,
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

            # Case 3: Autonomous Close / Invalidation when disadvantaged, circuit-broken, or exited
            if order_event and order_event.get("type") in (
                "ORDER_CLOSED_DISADVANTAGE",
                "BUNDLE_CIRCUIT_BREAKER",
                "TRADE_CLOSED_SL",
                "TRADE_CLOSED_TP2",
                "TRADE_CLOSED_BREAKEVEN",
                "ORDER_CANCELLED",
            ):
                ord_obj = order_event.get("order") or {}
                tickets_to_close = []
                if order_event.get("broker_ticket"):
                    tickets_to_close.append(order_event.get("broker_ticket"))
                if ord_obj.get("broker_ticket") and ord_obj.get("broker_ticket") not in tickets_to_close:
                    tickets_to_close.append(ord_obj.get("broker_ticket"))
                for sub_o in ord_obj.get("orders") or []:
                    bt = sub_o.get("broker_ticket")
                    if bt and bt not in tickets_to_close:
                        tickets_to_close.append(bt)
                for t in tickets_to_close:
                    self.ea_bridge.enqueue_action(
                        action_type="CLOSE_ORDER",
                        payload={"ticket": int(t), "symbol": "XAUUSD"},
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

        live_quote = feed.get("live_quote")
        state = {
            "type": "full_state",
            "symbol": "XAUUSD",
            "timeframe": tf,
            "trading_style": self.trading_style,
            "data_source": feed["source"],
            "live_quote": live_quote,
            "replay_mode": self.hub.replay_mode,
            "replay_cursor": self.hub.replay_cursor,
            "replay_total": len(self.hub.fixture_bars),
            "is_new_signal_alert": is_new_signal_alert,
            "active_order": self.order_manager.get_active_order_dict(cur_p),
            "order_event": order_event,
            "order_history": self.order_manager.order_history[-10:],
            "bars": chart_bars,
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
            "news": news,
            "ai_advisory": ai_advisory,
            "config": {
                "account_balance": self.config.get("account_balance", 5000.0),
                "leverage": float(self.config.get("leverage") or 100.0),
                "risk_per_trade_pct": self.config.get("risk_per_trade_pct", 1.0),
                "min_reward_risk": self.config.get("min_reward_risk", 1.5),
                "broker_price_offset": offset,
                "broker_spread_points": int(spread_pts),
                "has_gemini_key": bool(
                    (self.config.get("ai") or {}).get("api_key")
                    or os.environ.get("GEMINI_API_KEY")
                ),
            },
        }
        self.last_state = state
        return state

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        for key in (
            "account_balance",
            "leverage",
            "risk_per_trade_pct",
            "min_reward_risk",
            "broker_price_offset",
            "broker_spread_points",
        ):
            if key in payload and payload[key] is not None:
                self.config[key] = float(payload[key])
        self.config["simulation_replays"] = max(
            35000, int(self.config.get("simulation_replays") or 35000)
        )
        if "multi_order" in payload and isinstance(payload["multi_order"], dict):
            self.config.setdefault("multi_order", {}).update(payload["multi_order"])
        if "gemini_api_key" in payload:
            raw_key = str(payload["gemini_api_key"]).strip()
            if " " not in raw_key and not raw_key.startswith("[MT5"):
                self.config.setdefault("ai", {})["api_key"] = raw_key
        save_config(self.config)
        return self.compute_full_state(force_ai=True)


# ---------------------------------------------------------------------------
# Pure-Python Async HTTP + RFC 6455 WebSocket Server
# ---------------------------------------------------------------------------

class WebSocketClient:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.closed = False

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
        self.core = TradingAssistantCore()
        self.clients: Set[WebSocketClient] = set()
        self.auto_replay = False
        self._last_ws_broadcast_ts = 0.0

    async def broadcast(self, message: Dict[str, Any]) -> None:
        dead: List[WebSocketClient] = []
        for c in list(self.clients):
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
            if self.core.hub.replay_mode or not self.clients:
                return
            now = time.time()
            if now - self._last_ws_broadcast_ts < 0.25:
                return
            self._last_ws_broadcast_ts = now

            offset = float(self.core.config.get("broker_price_offset") or 0.0)
            ingested = self.core.hub.ingest_live_price(
                timeframe=self.core.timeframe,
                price=price,
                bid=bid,
                ask=ask,
                ts=ts,
                offset_usd=offset,
            )
            if not ingested:
                return
            b = ingested["bar"]
            ev = self.core.order_manager.update_price_tick(
                current_price=float(b["c"]),
                high=float(b["h"]),
                low=float(b["l"]),
                ts=int(b["t"]),
            )
            if ingested["new_bar_closed"] or ev:
                state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
                await self.broadcast(state)
            else:
                await self.broadcast(
                    {
                        "type": "tick",
                        "timeframe": self.core.timeframe,
                        "bar": ingested["bar"],
                        "bid": ingested["bid"],
                        "ask": ingested["ask"],
                        "spread_points": ingested["spread_points"],
                        "active_order": self.core.order_manager.get_active_order_dict(float(b["c"])),
                        "source": "tradingview_ws (OANDA:XAUUSD)",
                    }
                )

        await stream_tradingview_gold_ws(on_tv_tick)

    async def binance_ws_loop(self) -> None:
        """Fallback Stream: real-time gold trades from wss://stream.binance.com:9443/ws/paxgusdt@trade when TradingView WS is idle."""
        async def on_trade(trade_price: float, trade_ts: int) -> None:
            if self.core.hub.replay_mode or not self.clients:
                return
            # Only use Binance fallback stream if TradingView primary stream hasn't pushed recently
            if str(self.core.hub.source).startswith("tradingview_ws"):
                return
            now = time.time()
            # Throttle WebSocket push to at most 4 frames/sec for smooth browser rendering
            if now - self._last_ws_broadcast_ts < 0.25:
                return
            self._last_ws_broadcast_ts = now

            spot_equivalent = trade_price + self.core.hub.basis_shift
            offset = float(self.core.config.get("broker_price_offset") or 0.0)
            ingested = self.core.hub.ingest_live_price(
                timeframe=self.core.timeframe,
                price=spot_equivalent,
                ts=trade_ts,
                offset_usd=offset,
            )
            if not ingested:
                return
            b = ingested["bar"]
            ev = self.core.order_manager.update_price_tick(
                current_price=float(b["c"]),
                high=float(b["h"]),
                low=float(b["l"]),
                ts=int(b["t"]),
            )
            if ingested["new_bar_closed"] or ev:
                state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
                await self.broadcast(state)
            else:
                await self.broadcast(
                    {
                        "type": "tick",
                        "timeframe": self.core.timeframe,
                        "bar": ingested["bar"],
                        "active_order": self.core.order_manager.get_active_order_dict(float(b["c"])),
                        "source": ingested["source"],
                    }
                )

        await stream_binance_gold_ws(on_trade)

    async def tick_loop(self) -> None:
        """Poll real Spot XAU/USD Bid/Ask every 2.0s in Live Mode (or step Replay in Replay Mode)."""
        loop_counter = 0
        while True:
            await asyncio.sleep(2.0)
            if not self.clients:
                continue
            loop_counter += 1

            # 1. Replay Mode handling
            if self.core.hub.replay_mode:
                if self.auto_replay and loop_counter % 2 == 0:
                    self.core.hub.step_replay(1)
                    state = await asyncio.to_thread(self.core.compute_full_state)
                    await self.broadcast(state)
                elif self.core.last_state and self.core.last_state.get("bars"):
                    last_bar = self.core.last_state["bars"][-1]
                    tick = self.core.hub.simulate_live_tick(last_bar, offset_usd=0.0)
                    await self.broadcast(
                        {
                            "type": "tick",
                            "timeframe": self.core.timeframe,
                            "bar": tick,
                            "source": "offline_replay",
                        }
                    )
                continue

            # 2. Live Mode: if TradingView WS is active, periodically refresh full state every 20s;
            #    otherwise use Swissquote / Binance fallback quote polling
            if str(self.core.hub.source).startswith("tradingview_ws"):
                if loop_counter % 10 == 0:
                    state = await asyncio.to_thread(
                        self.core.compute_full_state, None, True, False
                    )
                    await self.broadcast(state)
                continue

            spot = None
            if HAS_MT5_LIB and _mt5_initialized:
                spot = await asyncio.to_thread(fetch_mt5_live_tick, self.core.hub.preferred_symbol)
            if not spot and self.core.ea_bridge.account_info.get("price"):
                acc = self.core.ea_bridge.account_info
                spot = {
                    "price": float(acc["price"]),
                    "bid": float(acc.get("bid") or acc["price"]),
                    "ask": float(acc.get("ask") or acc["price"]),
                    "spread_points": float(acc.get("spread_points") or 0.0),
                    "source": f"mt5_broker ({acc.get('symbol', 'XAUUSD')})",
                    "ts": int(time.time()),
                }
            elif not spot and str(self.core.hub.source).startswith("mt5_broker"):
                spot = await asyncio.to_thread(fetch_mt5_live_tick, self.core.hub.preferred_symbol)
            if not spot:
                spot = await asyncio.to_thread(fetch_live_spot_xauusd)

            if spot:
                self.core.hub.latest_spot_quote = spot
                is_mt5_feed = str(spot.get("source", "")).startswith("mt5_broker")
                offset = 0.0 if is_mt5_feed else float(self.core.config.get("broker_price_offset") or 0.0)
                ingested = self.core.hub.ingest_live_price(
                    timeframe=self.core.timeframe,
                    price=spot["price"],
                    bid=spot["bid"],
                    ask=spot["ask"],
                    ts=spot["ts"],
                    offset_usd=offset,
                )
                if ingested:
                    b = ingested["bar"]
                    ev = self.core.order_manager.update_price_tick(
                        current_price=float(b["c"]),
                        high=float(b["h"]),
                        low=float(b["l"]),
                        ts=int(b["t"]),
                    )
                    if ingested["new_bar_closed"] or ev or loop_counter % 10 == 0:
                        # Recompute full SMC + Monte Carlo state when candle closes, order event occurs, or every 20s
                        state = await asyncio.to_thread(
                            self.core.compute_full_state, None, ingested["new_bar_closed"], False
                        )
                        await self.broadcast(state)
                    else:
                        await self.broadcast(
                            {
                                "type": "tick",
                                "timeframe": self.core.timeframe,
                                "bar": ingested["bar"],
                                "bid": ingested["bid"],
                                "ask": ingested["ask"],
                                "spread_points": ingested["spread_points"],
                                "active_order": self.core.order_manager.get_active_order_dict(float(b["c"])),
                                "source": spot["source"],
                            }
                        )

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

                client = WebSocketClient(writer)
                self.clients.add(client)
                state = self.core.last_state or self.core.compute_full_state()
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

    def dispatch_order_to_mt5(self) -> Dict[str, Any]:
        """Manually or explicitly dispatch the current setup / active order into MT5 EA queue."""
        act = self.core.order_manager.active_order
        last_st = self.core.last_state or {}
        rt = last_st.get("risk_ticket") or {}
        cur_p = float(
            (self.core.hub.latest_spot or {}).get("price")
            or (act.get("entry_mt5") if act else 0.0)
            or (rt.get("entry_mt5") or 0.0)
            or (self.core.hub.fixture_bars[-1]["c"] if self.core.hub.fixture_bars else 2650.0)
        )
        side = (act.get("side") if act else None) or rt.get("side") or "BUY"

        # If no active order exists, register one so Web UI tracks it and displays PnL
        if not act:
            stop_p = float(rt.get("stop_mt5") or (cur_p - 5.0 if side == "BUY" else cur_p + 5.0))
            target_p = float(rt.get("target_mt5") or (cur_p + 10.0 if side == "BUY" else cur_p - 10.0))
            tp1_p = float(rt.get("tp1_mt5") or (cur_p + 5.0 if side == "BUY" else cur_p - 5.0))
            lot = float(rt.get("lot_size") or 0.10)
            self.core.order_manager.register_trade_setup(
                risk_ticket={
                    "side": side,
                    "entry_mt5": cur_p,
                    "stop_mt5": stop_p,
                    "tp1_mt5": tp1_p,
                    "target_mt5": target_p,
                    "lot_size": lot,
                    "actual_risk_usd": float(rt.get("actual_risk_usd") or 50.0),
                    "potential_profit_usd": float(rt.get("potential_profit_usd") or 100.0),
                    "reward_risk": 2.0,
                    "order_type": f"{side} NOW (Market)",
                    "execution_type": "MARKET",
                },
                trade_setup=last_st.get("smc", {}).get("trade_setup") or {},
                current_price=cur_p,
                timeframe=self.core.timeframe,
                trading_style=self.core.trading_style,
                force_replace=True,
            )
            act = self.core.order_manager.active_order

        sig_id = (act.get("order_id") if act else None) or rt.get("fingerprint") or f"manual_{int(time.time())}"
        # Discard so manual dispatch can re-queue even if already queued
        self.core.ea_bridge.executed_signal_ids.discard(sig_id)

        exec_type = (act.get("execution_type") if act else None) or "MARKET"
        sym = self.core.hub.preferred_symbol or "XAUUSD"
        action = self.core.ea_bridge.enqueue_action(
            action_type="OPEN_ORDER",
            payload={
                "side": side,
                "order_type": exec_type,
                "symbol": sym,
                "lot": float((act.get("lot_size") if act else None) or rt.get("lot_size") or 0.10),
                "price": float((act.get("entry_mt5") if act else None) or cur_p),
                "sl": float((act.get("stop_mt5") if act else None) or rt.get("stop_mt5") or 0.0),
                "tp": float((act.get("target_mt5") if act else None) or rt.get("target_mt5") or 0.0),
                "tp1": float((act.get("tp1_mt5") if act else None) or rt.get("tp1_mt5") or 0.0),
                "magic": 797979,
                "comment": f"K79-{side}-MANUAL",
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
    ) -> Dict[str, Any]:
        """Dispatch an instant test BUY/SELL (Market or Limit) order directly into MT5 EA queue."""
        side = str(side or "BUY").upper()
        if side not in ("BUY", "SELL"):
            side = "BUY"
        order_type = str(order_type or "MARKET").upper()
        if order_type not in ("MARKET", "LIMIT"):
            order_type = "MARKET"

        cur_p = float(
            (self.core.hub.latest_spot or {}).get("price")
            or (self.core.hub.fixture_bars[-1]["c"] if self.core.hub.fixture_bars else 2650.0)
        )

        test_lot = float(lot) if lot and float(lot) > 0 else 0.01

        # Calculate entry, SL, TP based on type
        if order_type == "MARKET":
            entry_p = round(cur_p, 2)
            if side == "BUY":
                sl_p = round(cur_p - 4.00, 2)
                tp_p = round(cur_p + 8.00, 2)
                tp1_p = round(cur_p + 4.00, 2)
            else:
                sl_p = round(cur_p + 4.00, 2)
                tp_p = round(cur_p - 8.00, 2)
                tp1_p = round(cur_p - 4.00, 2)
        else:  # LIMIT
            # For BUY LIMIT: place entry $2.50 below market so it sits as pending order waiting to be filled
            # For SELL LIMIT: place entry $2.50 above market
            if side == "BUY":
                entry_p = round(cur_p - 2.50, 2)
                sl_p = round(entry_p - 4.00, 2)
                tp_p = round(entry_p + 8.00, 2)
                tp1_p = round(entry_p + 4.00, 2)
            else:
                entry_p = round(cur_p + 2.50, 2)
                sl_p = round(entry_p + 4.00, 2)
                tp_p = round(entry_p - 8.00, 2)
                tp1_p = round(entry_p - 4.00, 2)

        exec_desc = f"{side} NOW (Market Test)" if order_type == "MARKET" else f"{side} LIMIT (Pending Test)"

        # Register trade setup so Web UI tracks it and displays PnL
        self.core.order_manager.register_trade_setup(
            risk_ticket={
                "side": side,
                "entry_mt5": entry_p,
                "stop_mt5": sl_p,
                "tp1_mt5": tp1_p,
                "target_mt5": tp_p,
                "lot_size": test_lot,
                "actual_risk_usd": round(test_lot * 100 * 4.0, 2),
                "potential_profit_usd": round(test_lot * 100 * 8.0, 2),
                "reward_risk": 2.0,
                "order_type": exec_desc,
                "execution_type": order_type,
            },
            trade_setup={
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
            timeframe=self.core.timeframe,
            trading_style=self.core.trading_style,
            force_replace=True,
        )
        act = self.core.order_manager.active_order

        # Generate unique signal_id for MT5 tracking
        sig_id = f"test_{side.lower()}_{order_type.lower()}_{int(time.time() * 1000)}"
        if act:
            act["order_id"] = sig_id

        # Discard previous executed record so manual dispatch always queues fresh
        self.core.ea_bridge.executed_signal_ids.discard(sig_id)

        sym = self.core.hub.preferred_symbol or "XAUUSD"
        action = self.core.ea_bridge.enqueue_action(
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
                "comment": f"K79-TEST-{side[:1]}{order_type[:1]}",
                "manual": True,
            },
            signal_id=sig_id,
        )
        return {
            "status": "ok",
            "message": f"ส่งคำสั่งทดสอบ {side} ({order_type}) ขนาด {test_lot} Lot ไปยัง MT5 แล้ว",
            "action": action,
            "active_order": act,
        }

    async def handle_ws_command(self, client: WebSocketClient, cmd: Dict[str, Any]) -> None:
        action = str(cmd.get("action") or "")
        if action == "change_tf":
            tf = str(cmd.get("timeframe") or "M5")
            state = await asyncio.to_thread(self.core.compute_full_state, tf, True, False)
            await self.broadcast(state)
        elif action == "change_style":
            style = str(cmd.get("trading_style") or "intraday").lower()
            tf = cmd.get("timeframe")
            if self.core.order_manager.has_active_order():
                act = self.core.order_manager.active_order
                if act and act.get("status") == "PENDING" and act.get("trading_style") != style:
                    self.core.order_manager.cancel_active_order(reason="STYLE_CHANGED")
            state = await asyncio.to_thread(self.core.compute_full_state, tf, True, True, style)
            await self.broadcast(state)
        elif action == "step_replay":
            self.auto_replay = False
            steps = int(cmd.get("steps") or 1)
            self.core.hub.step_replay(steps)
            state = await asyncio.to_thread(self.core.compute_full_state)
            await self.broadcast(state)
        elif action == "reset_replay":
            self.auto_replay = False
            self.core.hub.reset_replay(int(cmd.get("cursor") or 234))
            state = await asyncio.to_thread(self.core.compute_full_state, None, False, True)
            await self.broadcast(state)
        elif action == "toggle_auto_replay":
            self.core.hub.replay_mode = True
            self.auto_replay = not self.auto_replay
            await self.broadcast({"type": "auto_replay_status", "auto_replay": self.auto_replay})
        elif action == "live_mode":
            self.auto_replay = False
            self.core.hub.replay_mode = False
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, True)
            await self.broadcast(state)
        elif action == "analyze_now":
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, True)
            await self.broadcast(state)
        elif action == "cancel_active_order":
            reason = str(cmd.get("reason") or "MANUAL_CANCEL")
            act = self.core.order_manager.active_order
            if act and act.get("broker_ticket"):
                self.core.ea_bridge.enqueue_action(
                    action_type="CLOSE_ORDER",
                    payload={"ticket": int(act["broker_ticket"])},
                )
            self.core.order_manager.cancel_active_order(reason=reason)
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "close_active_order":
            cp = float(cmd.get("current_price") or self.core.hub.latest_spot.get("price") or 0.0)
            act = self.core.order_manager.active_order
            if act and act.get("broker_ticket"):
                self.core.ea_bridge.enqueue_action(
                    action_type="CLOSE_ORDER",
                    payload={"ticket": int(act["broker_ticket"])},
                )
            self.core.order_manager.close_active_order(reason="MANUAL_CLOSE", current_price=cp)
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "fill_active_order_now":
            cp = float(cmd.get("current_price") or self.core.hub.latest_spot.get("price") or 0.0)
            self.core.order_manager.fill_now(current_price=cp)
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "set_breakeven":
            self.core.order_manager.set_breakeven()
            act = self.core.order_manager.active_order
            if act and act.get("broker_ticket"):
                self.core.ea_bridge.enqueue_action(
                    action_type="MODIFY_ORDER",
                    payload={
                        "ticket": int(act["broker_ticket"]),
                        "new_sl": float(act.get("entry_mt5") or 0.0),
                        "new_tp": float(act.get("target_mt5") or 0.0),
                    },
                )
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "send_order_to_mt5":
            self.dispatch_order_to_mt5()
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "dispatch_test_order":
            side = str(cmd.get("side") or "BUY")
            order_type = str(cmd.get("order_type") or "MARKET")
            lot = float(cmd.get("lot")) if cmd.get("lot") else None
            self.dispatch_test_order(side=side, order_type=order_type, lot=lot)
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "scan_new_setup":
            self.core.order_manager.cancel_active_order(reason="SCAN_NEW_SETUP")
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, True)
            await self.broadcast(state)
        elif action == "add_sub_order":
            cp = float(cmd.get("current_price") or self.core.hub.latest_spot.get("price") or 0.0)
            purpose = str(cmd.get("purpose") or "AUTO")
            exec_type = str(cmd.get("execution_type") or "MARKET")
            entry_p = float(cmd.get("entry_price") or cp)
            lot = float(cmd.get("lot_size")) if cmd.get("lot_size") else None
            self.core.order_manager.add_sub_order(
                purpose=purpose,
                execution_type=exec_type,
                current_price=cp,
                entry_price=entry_p,
                lot_size=lot,
            )
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "close_order_by_id":
            cp = float(cmd.get("current_price") or self.core.hub.latest_spot.get("price") or 0.0)
            oid = str(cmd.get("order_id") or "")
            self.core.order_manager.close_order_by_id(order_id=oid, current_price=cp)
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "close_all_active_orders":
            cp = float(cmd.get("current_price") or self.core.hub.latest_spot.get("price") or 0.0)
            self.core.ea_bridge.enqueue_action(
                action_type="CLOSE_ALL",
                payload={"symbol": "XAUUSD", "magic": 797979},
            )
            self.core.order_manager.close_all_orders(current_price=cp)
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)

    async def handle_http_request(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        body_bytes: bytes,
    ) -> None:
        if path == "/api/state" and method == "GET":
            tf = (query.get("tf") or [self.core.timeframe])[0]
            state = self.core.compute_full_state(timeframe=tf)
            await self.send_http_json(writer, 200, state)
            return

        if path == "/api/config" and method == "POST":
            payload = safe_json_loads(body_bytes)
            state = self.core.update_settings(payload)
            await self.broadcast(state)
            await self.send_http_json(writer, 200, state)
            return

        if path == "/api/replay" and method == "POST":
            payload = safe_json_loads(body_bytes)
            mode = payload.get("mode", "step")
            if mode == "reset":
                self.auto_replay = False
                self.core.hub.reset_replay(int(payload.get("cursor") or 234))
            elif mode == "live":
                self.auto_replay = False
                self.core.hub.replay_mode = False
            else:
                self.auto_replay = False
                self.core.hub.step_replay(int(payload.get("steps") or 1))
            state = self.core.compute_full_state(force_refresh=(mode == "live"), force_ai=True)
            await self.broadcast(state)
            await self.send_http_json(writer, 200, state)
            return

        if path == "/api/analyze" and method == "POST":
            state = self.core.compute_full_state(force_ai=True)
            await self.broadcast(state)
            await self.send_http_json(writer, 200, state)
            return

        if path == "/api/order/action" and method == "POST":
            payload = safe_json_loads(body_bytes)
            action = str(payload.get("action") or "")
            cp = float(payload.get("current_price") or self.core.hub.latest_spot.get("price") or 0.0)
            if action == "cancel":
                self.core.order_manager.cancel_active_order(reason="MANUAL_CANCEL")
            elif action == "close":
                self.core.order_manager.close_active_order(reason="MANUAL_CLOSE", current_price=cp)
            elif action == "fill_now":
                self.core.order_manager.fill_now(current_price=cp)
            elif action == "set_breakeven":
                self.core.order_manager.set_breakeven()
            elif action == "scan_new_setup":
                self.core.order_manager.cancel_active_order(reason="SCAN_NEW_SETUP")
            elif action == "add_sub_order":
                purpose = str(payload.get("purpose") or "AUTO")
                exec_type = str(payload.get("execution_type") or "MARKET")
                entry_p = float(payload.get("entry_price") or cp)
                lot = float(payload.get("lot_size")) if payload.get("lot_size") else None
                self.core.order_manager.add_sub_order(
                    purpose=purpose,
                    execution_type=exec_type,
                    current_price=cp,
                    entry_price=entry_p,
                    lot_size=lot,
                )
            elif action == "close_by_id":
                oid = str(payload.get("order_id") or "")
                # Find matching sub order broker_ticket if any
                for o in self.core.order_manager.active_orders:
                    if o.get("order_id") == oid and o.get("broker_ticket"):
                        self.core.ea_bridge.enqueue_action(
                            "CLOSE_ORDER", {"ticket": o["broker_ticket"]}
                        )
                self.core.order_manager.close_order_by_id(order_id=oid, current_price=cp)
            elif action == "close_all":
                self.core.ea_bridge.enqueue_action(
                    "CLOSE_ALL", {"symbol": "XAUUSD", "magic": 797979}
                )
                self.core.order_manager.close_all_orders(current_price=cp)

            if action in ("close", "cancel"):
                self.core.ea_bridge.enqueue_action(
                    "CLOSE_ALL", {"symbol": "XAUUSD", "magic": 797979}
                )
            elif action == "set_breakeven":
                act_ord = self.core.order_manager.active_order
                if act_ord and act_ord.get("broker_ticket"):
                    self.core.ea_bridge.enqueue_action(
                        "MODIFY_ORDER",
                        {
                            "ticket": act_ord["broker_ticket"],
                            "new_sl": act_ord.get("breakeven_sl_mt5", act_ord["entry_mt5"]),
                            "new_tp": act_ord.get("target_mt5", 0.0),
                        },
                    )

            state = await asyncio.to_thread(
                self.core.compute_full_state, None, True, action == "scan_new_setup"
            )
            await self.broadcast(state)
            await self.send_http_json(writer, 200, state)
            return

        # --- MT5 EA Bridge HTTP Endpoints ---
        if path == "/api/ea/poll" and method in ("GET", "POST"):
            actions = self.core.ea_bridge.poll_actions()
            self.core.ea_bridge.record_poll()
            resp_doc = {
                "status": "ok",
                "auto_trading_enabled": self.core.ea_bridge.auto_trading_enabled,
                "has_actions": len(actions) > 0,
                "actions": actions,
                "next_poll_ms": 200 if len(actions) > 0 else 500,
                "server_time": int(time.time()),
            }
            await self.send_http_json(writer, 200, resp_doc)
            return

        if path == "/api/ea/heartbeat" and method == "POST":
            payload = safe_json_loads(body_bytes)
            cached_bars = (self.core.hub.cached_bars_by_tf.get(self.core.timeframe) or [])
            ref_px = float(cached_bars[-1].get("c")) if cached_bars and cached_bars[-1].get("c") else None
            info = self.core.ea_bridge.record_heartbeat(payload, reference_feed_price=ref_px)
            # Sync balance / equity, live spread & auto-calibrated broker offset
            if info.get("equity") and float(info["equity"]) > 0:
                self.core.config["account_balance"] = float(info["equity"])
            if info.get("spread_points") and float(info["spread_points"]) > 0:
                self.core.config["broker_spread_points"] = float(info["spread_points"])
            if (
                self.core.config.get("auto_sync_broker_offset")
                and len(self.core.ea_bridge.basis_samples) >= 3
            ):
                self.core.config["broker_price_offset"] = float(info.get("dynamic_offset_usd") or 0.0)
            if info.get("symbol"):
                self.core.hub.preferred_symbol = str(info["symbol"]).strip()
            # Ingest live broker price / tick directly from MT5 EA heartbeat
            if info.get("price") and float(info["price"]) > 0:
                cur_p = float(info["price"])
                bid_p = float(info.get("bid") or cur_p)
                ask_p = float(info.get("ask") or cur_p)
                self.core.hub.latest_spot = {
                    "price": cur_p,
                    "bid": bid_p,
                    "ask": ask_p,
                    "spread_points": float(info.get("spread_points") or 0.0),
                    "source": f"mt5_broker ({info.get('symbol', 'XAUUSD')})",
                    "time": int(time.time()),
                    "ts": int(time.time()),
                }
            # Reconcile tracked orders against MT5 open positions & pending orders
            self.core.order_manager.reconcile_mt5_positions(
                open_positions=info.get("open_positions") or [],
                pending_orders=info.get("pending_orders") or [],
                current_price=float(info.get("price") or 0.0),
            )
            # Broadcast state so web UI sees live MT5 status immediately
            state = self.core.last_state or {}
            if state:
                state["ea_bridge"] = self.core.ea_bridge.get_status_dict()
                await self.broadcast(state)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "auto_trading_enabled": self.core.ea_bridge.auto_trading_enabled,
                    "server_time": int(time.time()),
                },
            )
            return

        if path == "/api/ea/execution" and method == "POST":
            payload = safe_json_loads(body_bytes)
            rec = self.core.ea_bridge.record_execution(payload)
            self.core.order_manager.bind_execution_report(rec)
            state = await asyncio.to_thread(self.core.compute_full_state)
            await self.broadcast(state)
            await self.send_http_json(writer, 200, {"status": "ok", "execution": rec})
            return

        if path == "/api/ea/toggle_auto" and method == "POST":
            payload = safe_json_loads(body_bytes)
            req_enable = payload.get("enabled")
            if req_enable is None:
                new_val = self.core.ea_bridge.toggle_auto_trading()
            else:
                new_val = self.core.ea_bridge.set_auto_trading(bool(req_enable))
            state = await asyncio.to_thread(self.core.compute_full_state)
            await self.broadcast(state)
            await self.send_http_json(
                writer,
                200,
                {
                    "status": "ok",
                    "auto_trading_enabled": new_val,
                    "ea_bridge": self.core.ea_bridge.get_status_dict(),
                },
            )
            return

        if path == "/api/ea/send_order" and method == "POST":
            res = self.dispatch_order_to_mt5()
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
            await self.send_http_json(writer, 200, res)
            return

        if path == "/api/ea/test_order" and method == "POST":
            payload = safe_json_loads(body_bytes)
            side = str(payload.get("side") or "BUY")
            order_type = str(payload.get("order_type") or "MARKET")
            lot = float(payload.get("lot")) if payload.get("lot") else None
            res = self.dispatch_test_order(side=side, order_type=order_type, lot=lot)
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
            await self.send_http_json(writer, 200, res)
            return

        if path == "/api/ea/status" and method == "GET":
            await self.send_http_json(writer, 200, self.core.ea_bridge.get_status_dict())
            return

        rel_path = "index.html" if path in ("/", "") else path.lstrip("/")
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
        writer.close()

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
        writer.close()

    async def serve_forever(self) -> None:
        self.core.compute_full_state(force_refresh=True)
        listen_host = None if self.host in ("0.0.0.0", "", None) else self.host
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
        print("🚀 KAIROS-79 (Precision Gold Timing & Risk System) Online")
        print(f"🌐 Web Dashboard (Local):    http://localhost:{self.port}")
        print(f"🌐 Web Dashboard (LAN/VPS):  http://{lan_ip}:{self.port}")
        print(f"🤖 MT5 EA Bridge URL:        http://{lan_ip}:{self.port} (or http://127.0.0.1:{self.port})")
        print(f"Active Data Feed:            {self.core.last_state.get('data_source') if self.core.last_state else 'initializing'}")
        print("=" * 64)
        try:
            async with server:
                await server.serve_forever()
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
