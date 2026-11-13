#!/usr/bin/env python3
"""Unit and Integration Tests for Order Lifecycle & Market Execution (test_order_lifecycle.py)."""

import json
import os
import tempfile
import time
import unittest
from engine.order_lifecycle import OrderLifecycleManager
from engine.risk_gate import evaluate_risk_and_size
from engine.smc import build_trade_setup


class TestOrderLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.persist_path = os.path.join(self.tmp_dir.name, "active_order.json")
        self.manager = OrderLifecycleManager(persistence_path=self.persist_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_pending_limit_order_stays_pinned_until_filled(self):
        """Verify entry level remains permanently locked and does not vanish on subsequent price updates."""
        risk_ticket = {
            "side": "BUY",
            "entry_mt5": 2650.00,
            "stop_mt5": 2645.00,
            "tp1_mt5": 2655.00,
            "target_mt5": 2660.00,
            "lot_size": 0.10,
            "actual_risk_usd": 50.0,
            "potential_profit_usd": 100.0,
            "reward_risk": 2.0,
            "order_type": "BUY LIMIT",
            "execution_type": "LIMIT",
        }
        smc_setup = {
            "side": "BUY",
            "execution_type": "LIMIT",
            "confluence_score": 75,
            "confluence_factors": ["Bullish OB", "EMA200 Support"],
        }

        # 1. Register setup
        order = self.manager.register_trade_setup(
            risk_ticket=risk_ticket,
            smc_setup=smc_setup,
            current_price=2655.00,
        )
        self.assertIsNotNone(order)
        self.assertEqual(order["status"], "PENDING")
        self.assertEqual(order["entry_mt5"], 2650.00)
        self.assertTrue(self.manager.has_active_order())

        # 2. Subsequent price movement above entry - order MUST NOT vanish or change entry
        ev = self.manager.update_price_tick(current_price=2654.00, high=2656.00, low=2652.00)
        self.assertIsNone(ev)  # No transition yet
        self.assertEqual(self.manager.active_order["entry_mt5"], 2650.00)
        self.assertEqual(self.manager.active_order["status"], "PENDING")

        # 3. Price drops and touches entry (low=2649.50 <= 2650.00) -> FILLED
        ev = self.manager.update_price_tick(current_price=2650.00, high=2652.00, low=2649.50)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["type"], "ORDER_FILLED")
        self.assertEqual(self.manager.active_order["status"], "FILLED")
        self.assertEqual(self.manager.active_order["filled_price"], 2650.00)

        # 4. Floating PnL calculation: price rallies to 2653.00 (+3.00 * 0.10 * 100 = +$30.00)
        ev = self.manager.update_price_tick(current_price=2653.00, high=2653.50, low=2650.00)
        d = self.manager.get_active_order_dict(current_price=2653.00)
        self.assertEqual(d["floating_pnl_usd"], 30.0)
        self.assertEqual(d["floating_pnl_r"], 0.60)

        # 5. Price reaches TP1 (2655.00) -> partial profit bank & breakeven SL triggered
        ev = self.manager.update_price_tick(current_price=2655.50, high=2656.00, low=2652.00)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["type"], "TP1_REACHED")
        self.assertTrue(self.manager.active_order["tp1_hit"])
        self.assertTrue(self.manager.active_order["sl_moved_breakeven"])

        # 6. Price reaches TP2 full target (2660.00) -> Closed with +2.0R win
        ev = self.manager.update_price_tick(current_price=2660.50, high=2661.00, low=2655.00)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["type"], "TRADE_CLOSED_TP2")
        self.assertFalse(self.manager.has_active_order())
        self.assertEqual(len(self.manager.order_history), 1)
        self.assertEqual(self.manager.order_history[0]["realized_pnl_r"], 2.0)

    def test_instant_market_execution_order_registers_as_filled_immediately(self):
        """Verify market order at current price enters FILLED status immediately without waiting."""
        risk_ticket = {
            "side": "SELL",
            "entry_mt5": 2670.00,
            "stop_mt5": 2674.00,
            "tp1_mt5": 2666.00,
            "target_mt5": 2660.00,
            "lot_size": 0.20,
            "actual_risk_usd": 80.0,
            "potential_profit_usd": 200.0,
            "reward_risk": 2.5,
            "order_type": "SELL NOW (Market)",
            "execution_type": "MARKET",
        }
        smc_setup = {
            "side": "SELL",
            "execution_type": "MARKET",
            "order_type": "SELL NOW (Market)",
            "confluence_score": 80,
            "confluence_factors": ["Liquidity Sweep Rejection", "Strong Bearish RVOL"],
        }

        order = self.manager.register_trade_setup(
            risk_ticket=risk_ticket,
            smc_setup=smc_setup,
            current_price=2670.00,
        )
        self.assertIsNotNone(order)
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["execution_type"], "MARKET")
        self.assertIn("NOW", order["order_type"])
        self.assertEqual(order["filled_price"], 2670.00)

        # Price moves favorably to 2668.00 (profit for SELL: +2.00 * 0.20 * 100 = +$40.00)
        self.manager.update_price_tick(current_price=2668.00, high=2670.00, low=2667.50)
        d = self.manager.get_active_order_dict(current_price=2668.00)
        self.assertEqual(d["floating_pnl_usd"], 40.0)
        self.assertEqual(d["floating_pnl_r"], 0.50)

    def test_persistence_across_server_restarts(self):
        """Verify active order state survives restart and reloads from disk."""
        risk_ticket = {
            "side": "BUY",
            "entry_mt5": 2650.00,
            "stop_mt5": 2645.00,
            "tp1_mt5": 2655.00,
            "target_mt5": 2660.00,
            "lot_size": 0.10,
            "actual_risk_usd": 50.0,
            "potential_profit_usd": 100.0,
            "reward_risk": 2.0,
            "order_type": "BUY LIMIT",
            "execution_type": "LIMIT",
        }
        smc_setup = {"side": "BUY", "execution_type": "LIMIT", "confluence_score": 70}

        self.manager.register_trade_setup(risk_ticket, smc_setup, 2655.00)
        self.assertTrue(os.path.exists(self.persist_path))

        # Re-instantiate manager (simulating process restart)
        new_manager = OrderLifecycleManager(persistence_path=self.persist_path)
        self.assertTrue(new_manager.has_active_order())
        self.assertEqual(new_manager.active_order["entry_mt5"], 2650.00)
        self.assertEqual(new_manager.active_order["status"], "PENDING")

    def test_manual_actions(self):
        """Verify user manual triggers (fill now, breakeven, close, cancel)."""
        risk_ticket = {
            "side": "BUY",
            "entry_mt5": 2650.00,
            "stop_mt5": 2645.00,
            "tp1_mt5": 2655.00,
            "target_mt5": 2660.00,
            "lot_size": 0.10,
            "actual_risk_usd": 50.0,
            "reward_risk": 2.0,
        }
        smc_setup = {"side": "BUY", "execution_type": "LIMIT", "confluence_score": 70}

        # 1. Register pending
        self.manager.register_trade_setup(risk_ticket, smc_setup, 2655.00)

        # 2. Fill now manually
        self.manager.fill_now(current_price=2651.00)
        self.assertEqual(self.manager.active_order["status"], "FILLED")
        self.assertEqual(self.manager.active_order["filled_price"], 2651.00)

        # 3. Move breakeven manually
        self.manager.set_breakeven()
        self.assertTrue(self.manager.active_order["sl_moved_breakeven"])

        # 4. Close manually
        self.manager.close_active_order(reason="MANUAL_CLOSE", current_price=2658.00)
        self.assertFalse(self.manager.has_active_order())
        self.assertEqual(len(self.manager.order_history), 1)
        self.assertEqual(self.manager.order_history[0]["status"], "CLOSED_MANUAL")

    def test_multi_order_recovery_and_scale_in(self):
        """Verify opening multiple orders for DCA/Recovery (แก้ไม้) and Scale-in (ทำกำไรเพิ่ม)."""
        risk_ticket = {
            "side": "BUY",
            "entry_mt5": 2650.00,
            "stop_mt5": 2640.00,
            "tp1_mt5": 2660.00,
            "target_mt5": 2670.00,
            "lot_size": 0.10,
            "actual_risk_usd": 100.0,
            "reward_risk": 2.0,
            "order_type": "BUY NOW (Market)",
            "execution_type": "MARKET",
        }
        smc_setup = {"side": "BUY", "execution_type": "MARKET", "confluence_score": 75}

        # 1. Register primary order at 2650.00 (FILLED)
        o1 = self.manager.register_trade_setup(risk_ticket, smc_setup, 2650.00)
        self.assertEqual(o1["status"], "FILLED")
        self.assertEqual(len(self.manager.active_orders), 1)

        # 2. Price drops to 2645.00 (drawdown) -> Open Recovery / DCA Order (แก้ไม้)
        o2 = self.manager.add_sub_order(
            purpose="AUTO",
            execution_type="MARKET",
            current_price=2645.00,
            lot_size=0.10,
        )
        self.assertIsNotNone(o2)
        self.assertEqual(o2["purpose"], "RECOVERY")
        self.assertEqual(o2["purpose_th"], "ไม้แก้ (DCA)")
        self.assertEqual(o2["entry_mt5"], 2645.00)
        self.assertEqual(len(self.manager.active_orders), 2)

        # 3. Check combined position stats at price 2648.00:
        # Order 1: 2650 -> 2648 = -2.00 * 0.10 * 100 = -$20.00
        # Order 2: 2645 -> 2648 = +3.00 * 0.10 * 100 = +$30.00
        # Total PnL = +$10.00
        # Avg Entry = (2650 * 0.1 + 2645 * 0.1) / 0.2 = 2647.50
        summary = self.manager.get_active_order_dict(current_price=2648.00)
        self.assertEqual(summary["total_orders_count"], 2)
        self.assertEqual(summary["total_lot_size"], 0.20)
        self.assertEqual(summary["avg_entry_price"], 2647.50)
        self.assertEqual(summary["total_floating_pnl_usd"], 10.00)
        self.assertEqual(summary["floating_pnl_usd"], 10.00)

        # 4. Price surges to 2655.00 -> Add Scale-in / Pyramiding Order (ทำกำไรเพิ่ม)
        o3 = self.manager.add_sub_order(
            purpose="AUTO",
            execution_type="MARKET",
            current_price=2655.00,
            lot_size=0.10,
        )
        self.assertIsNotNone(o3)
        self.assertEqual(o3["purpose"], "SCALE_IN")
        self.assertEqual(o3["purpose_th"], "ไม้เพิ่มกำไร (Scale)")
        self.assertEqual(len(self.manager.active_orders), 3)

        # 5. Close individual order (e.g. close the recovery order #2 to lock in profit)
        ev_close_o2 = self.manager.close_order_by_id(o2["order_id"], current_price=2655.00)
        self.assertIsNotNone(ev_close_o2)
        self.assertEqual(len(self.manager.active_orders), 2)
        self.assertEqual(len(self.manager.order_history), 1)

        # 6. Close remaining orders all at once
        all_closed = self.manager.close_all_orders(current_price=2656.00)
        self.assertEqual(len(all_closed), 2)
        self.assertFalse(self.manager.has_active_order())
        self.assertEqual(len(self.manager.order_history), 3)

    def test_autonomous_multi_order_scanner_recovery_and_scale_in(self):
        """Verify autonomous multi-order scanner detects DCA recovery and scale-in without manual clicking."""
        risk_ticket = {
            "side": "BUY",
            "entry_mt5": 2650.00,
            "stop_mt5": 2640.00,
            "tp1_mt5": 2658.00,
            "target_mt5": 2665.00,
            "lot_size": 0.10,
            "actual_risk_usd": 100.0,
            "reward_risk": 1.5,
            "order_type": "BUY NOW (Market)",
            "execution_type": "MARKET",
            "scenario_b_reentry_mt5": 2645.00,
        }
        smc_setup = {"side": "BUY", "execution_type": "MARKET", "confluence_score": 80}

        # 1. Register primary market order at 2650.00
        o1 = self.manager.register_trade_setup(risk_ticket, smc_setup, 2650.00)
        self.assertEqual(o1["status"], "FILLED")
        self.assertEqual(len(self.manager.active_orders), 1)

        cfg = {
            "multi_order": {
                "auto_mode_enabled": True,
                "max_concurrent_orders": 3,
                "min_spacing_usd": 3.0,
                "cooldown_seconds": 0,
                "recovery_enabled": True,
                "scale_in_enabled": True,
            }
        }

        # 2. Price drops to 2645.00 (Scenario B sweep touch + RSI oversold = 28.0)
        ev_rec = self.manager.check_and_apply_auto_multi_order(
            current_price=2645.00,
            high=2646.00,
            low=2644.50,
            atr=3.50,
            structure={"rsi": {"rsi14": 28.0}},
            config=cfg,
            ts=1700000000,
        )
        self.assertIsNotNone(ev_rec)
        self.assertEqual(ev_rec["type"], "AUTO_ORDER_ADDED")
        self.assertEqual(ev_rec["purpose"], "RECOVERY")
        self.assertEqual(ev_rec["side"], "BUY")
        self.assertEqual(ev_rec["entry_price"], 2645.00)
        # Crucial check: Original order is still active, now there are 2 active orders!
        self.assertEqual(len(self.manager.active_orders), 2)
        self.assertEqual(self.manager.active_orders[0]["order_id"], o1["order_id"])

        # 3. Spacing protection: within 3.00 USD spacing (2646.00 is too close to 2645.00)
        ev_blocked = self.manager.check_and_apply_auto_multi_order(
            current_price=2646.00,
            atr=3.50,
            config=cfg,
            ts=1700000010,
        )
        self.assertIsNone(ev_blocked)
        self.assertEqual(len(self.manager.active_orders), 2)

        # 4. Price rallies strongly to 2657.00 (+7.00 above primary entry)
        # Update tick so floating PnL is refreshed
        self.manager.update_price_tick(current_price=2657.00, high=2657.50, low=2650.00)
        self.assertGreaterEqual(self.manager.active_orders[0]["floating_pnl_r"], 0.60)

        # Scanner finds scale-in opportunity with Bullish trend and above EMA20 (2652.00)
        st_bull = {
            "trend": {"bias": "BULLISH"},
            "ema": {"ema20": 2652.00},
        }
        ev_scale = self.manager.check_and_apply_auto_multi_order(
            current_price=2657.00,
            high=2657.50,
            low=2656.00,
            atr=3.50,
            structure=st_bull,
            config=cfg,
            ts=1700000050,
        )
        self.assertIsNotNone(ev_scale)
        self.assertEqual(ev_scale["type"], "AUTO_ORDER_ADDED")
        self.assertEqual(ev_scale["purpose"], "SCALE_IN")
        self.assertEqual(ev_scale["entry_price"], 2657.00)
        self.assertEqual(len(self.manager.active_orders), 3)

        # 5. Max concurrent orders limit reached (3/3) -> No more orders added
        ev_max = self.manager.check_and_apply_auto_multi_order(
            current_price=2663.00,
            atr=3.50,
            structure=st_bull,
            config=cfg,
            ts=1700000100,
        )
        self.assertIsNone(ev_max)
        self.assertEqual(len(self.manager.active_orders), 3)

        # 6. Verify combined stats
        stats = self.manager.get_active_order_dict(current_price=2658.00)
        self.assertEqual(stats["total_orders_count"], 3)
        self.assertAlmostEqual(stats["total_lot_size"], 0.30, places=2)
        # Avg entry = (2650 + 2645 + 2657) / 3 = 2650.666...
        self.assertAlmostEqual(stats["avg_entry_price"], 2650.67, places=2)

    def test_early_cut_disadvantage_invalidation(self):
        """Verify early cut on structural disadvantage/invalidation before hitting full Stop Loss."""
        risk_ticket = {
            "side": "BUY",
            "entry_mt5": 2650.00,
            "stop_mt5": 2640.00,
            "tp1_mt5": 2660.00,
            "target_mt5": 2670.00,
            "lot_size": 0.10,
            "actual_risk_usd": 100.0,
            "potential_profit_usd": 200.0,
            "reward_risk": 2.0,
            "order_type": "BUY NOW (Market)",
            "execution_type": "MARKET",
        }
        smc_setup = {
            "side": "BUY",
            "execution_type": "MARKET",
            "confluence_score": 80,
            "confluence_factors": ["Bullish OB"],
        }
        cfg = {
            "multi_order": {
                "auto_mode_enabled": True,
            }
        }
        # 1. Register and fill
        order = self.manager.register_trade_setup(risk_ticket, smc_setup, current_price=2650.00)
        self.assertEqual(order["status"], "FILLED")

        # 2. Price moves against position to 2645 (-0.5R) with bearish CHoCH structure break
        self.manager.update_price_tick(current_price=2645.00, high=2650.00, low=2644.50)
        st_bear = {
            "breaks": [{"type": "CHOCH", "direction": "BEARISH", "level": 2646.0}],
            "trend": {"bias": "BEARISH"},
        }
        ev_cut = self.manager.check_and_apply_auto_multi_order(
            current_price=2645.00,
            high=2646.00,
            low=2644.50,
            atr=3.50,
            structure=st_bear,
            config=cfg,
            ts=1700000200,
        )
        self.assertIsNotNone(ev_cut)
        self.assertEqual(ev_cut["type"], "ORDER_CLOSED_DISADVANTAGE")
        self.assertIn("ตัดขาดทุนก่อนชน SL เต็ม", ev_cut["reason_th"])
        self.assertFalse(self.manager.has_active_order())

    def test_step_trailing_stop_after_tp1(self):
        """Verify that after TP1 is hit, price advancement towards TP2 triggers Step Trailing Stop."""
        risk_ticket = {
            "side": "BUY",
            "entry_mt5": 2650.00,
            "stop_mt5": 2640.00,  # 10 USD risk
            "tp1_mt5": 2660.00,   # +1.0R
            "target_mt5": 2670.00,# +2.0R
            "breakeven_sl_mt5": 2650.25,
            "lot_size": 0.10,
            "actual_risk_usd": 100.00,
            "execution_type": "MARKET",
            "order_type": "BUY NOW",
        }
        smc_setup = {"side": "BUY", "confluence_score": 85}
        cfg = {
            "step_trailing": {
                "enabled": True,
                "steps": [
                    {"trigger_r": 1.4, "lock_r": 0.60},
                    {"trigger_r": 1.8, "lock_r": 1.00},
                ],
            }
        }
        # 1. Register BUY order
        self.manager.register_trade_setup(risk_ticket, smc_setup, current_price=2650.00)
        self.assertEqual(self.manager.active_order["status"], "FILLED")

        # 2. Price rallies to 2660.50 (touches TP1 at 2660) -> TP1_REACHED event
        ev_tp1 = self.manager.update_price_tick(current_price=2660.50, high=2661.00, low=2650.00, config=cfg)
        self.assertIsNotNone(ev_tp1)
        self.assertEqual(ev_tp1["type"], "TP1_REACHED")
        self.assertTrue(self.manager.active_order["tp1_hit"])
        self.assertEqual(self.manager.active_order["stop_mt5"], 2650.25)

        # 3. Price pushes to 2664.50 (+1.45R >= 1.4R) -> Step Trailing Step 1 locks +0.60R
        # Entry 2650 + 0.60 * 10 = 2656.00
        ev_step1 = self.manager.update_price_tick(current_price=2664.50, high=2665.00, low=2660.00, config=cfg)
        self.assertIsNotNone(ev_step1)
        self.assertEqual(ev_step1["type"], "TRAILING_STOP_UPDATED")
        self.assertEqual(ev_step1["locked_r"], 0.60)
        self.assertEqual(ev_step1["new_sl"], 2656.00)
        self.assertEqual(self.manager.active_order["stop_mt5"], 2656.00)
        self.assertEqual(self.manager.active_order["trailing_step_index"], 1)

        # 4. Price pushes further to 2668.50 (+1.85R >= 1.8R) -> Step Trailing Step 2 locks +1.00R
        # Entry 2650 + 1.00 * 10 = 2660.00
        ev_step2 = self.manager.update_price_tick(current_price=2668.50, high=2669.00, low=2664.00, config=cfg)
        self.assertIsNotNone(ev_step2)
        self.assertEqual(ev_step2["type"], "TRAILING_STOP_UPDATED")
        self.assertEqual(ev_step2["locked_r"], 1.00)
        self.assertEqual(ev_step2["new_sl"], 2660.00)
        self.assertEqual(self.manager.active_order["stop_mt5"], 2660.00)
        self.assertEqual(self.manager.active_order["trailing_step_index"], 2)

    def test_real_deal_history_ingestion_and_updates(self):
        """Verify that DEAL_CLOSED execution reports capture exact broker financials (PnL, Swap, Comm)."""
        risk_ticket = {
            "side": "BUY",
            "entry_mt5": 2650.00,
            "stop_mt5": 2640.00,
            "tp1_mt5": 2660.00,
            "target_mt5": 2670.00,
            "lot_size": 0.10,
            "actual_risk_usd": 100.00,
            "execution_type": "MARKET",
            "order_type": "BUY NOW",
        }
        smc_setup = {"side": "BUY", "confluence_score": 85}
        order = self.manager.register_trade_setup(risk_ticket, smc_setup, current_price=2650.00)
        # Bind broker ticket
        self.manager.bind_execution_report({
            "status": "FILLED",
            "signal_id": order["order_id"],
            "ticket": 777111,
            "fill_price": 2650.20,
        })
        self.assertEqual(self.manager.active_order["broker_ticket"], 777111)

        # MT5 reports exit deal via OnTradeTransaction with real financial numbers
        deal_report = {
            "status": "DEAL_CLOSED",
            "ticket": 777111,
            "deal": 999888,
            "profit": 150.00,
            "swap": -3.50,
            "commission": -6.00,
            "fill_price": 2665.00,
            "close_reason": "DEAL_TP",
        }
        bound = self.manager.bind_execution_report(deal_report)
        self.assertTrue(bound)
        self.assertFalse(self.manager.has_active_order())

        # Check recorded history
        closed_item = self.manager.order_history[-1]
        self.assertEqual(closed_item["broker_deal_id"], 999888)
        self.assertEqual(closed_item["close_price"], 2665.00)
        self.assertEqual(closed_item["close_reason"], "DEAL_TP")
        # 150.00 - 3.50 - 6.00 = 140.50 net USD
        self.assertAlmostEqual(closed_item["realized_pnl_usd"], 140.50, places=2)
        self.assertAlmostEqual(closed_item["swap_usd"], -3.50, places=2)
        self.assertAlmostEqual(closed_item["commission_usd"], -6.00, places=2)
        self.assertAlmostEqual(closed_item["realized_pnl_r"], 1.41, places=2)

    def test_pending_order_recycling_timeout_and_runaway(self):
        """Verify pending limit orders are automatically recycled (cancelled) when expired or runaway."""
        risk_ticket = {
            "side": "BUY",
            "entry_mt5": 2650.00,
            "stop_mt5": 2640.00,
            "tp1_mt5": 2675.00,
            "target_mt5": 2690.00,
            "lot_size": 0.10,
            "actual_risk_usd": 100.00,
            "execution_type": "LIMIT",
            "order_type": "BUY LIMIT",
        }
        smc_setup = {"side": "BUY", "confluence_score": 75}
        order = self.manager.register_trade_setup(risk_ticket, smc_setup, current_price=2655.00)
        self.assertEqual(order["status"], "PENDING")

        # Test Case 1: Runaway price (price rockets away to 2678 without filling)
        config = {
            "multi_strategy": {
                "pending_expiry_minutes": 180,
                "max_adverse_distance_atr": 2.5,
            }
        }
        ev = self.manager.update_price_tick(
            current_price=2678.00,
            high=2678.50,
            low=2677.00,
            ts=int(time.time()),
            config=config,
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev["type"], "ORDER_CANCELLED")
        self.assertEqual(ev["reason"], "EXPIRED_STRUCTURE_RUNAWAY")
        self.assertFalse(self.manager.has_active_order())

        # Test Case 2: Time-based expiry (stale limit order after 200 minutes)
        order2 = self.manager.register_trade_setup(risk_ticket, smc_setup, current_price=2653.00, force_replace=True)
        self.assertEqual(order2["status"], "PENDING")
        past_ts = int(time.time()) - (200 * 60)
        order2["created_ts"] = past_ts
        self.manager.save_to_disk()

        ev2 = self.manager.update_price_tick(
            current_price=2654.00,
            high=2655.00,
            low=2652.00,
            ts=int(time.time()),
            config=config,
        )
        self.assertIsNotNone(ev2)
        self.assertEqual(ev2["type"], "ORDER_CANCELLED")
        self.assertEqual(ev2["reason"], "EXPIRED_TIMEOUT")
        self.assertFalse(self.manager.has_active_order())


if __name__ == "__main__":
    unittest.main()
