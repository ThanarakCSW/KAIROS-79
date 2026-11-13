#!/usr/bin/env python3
"""Unit and Integration Tests for Order Lifecycle & Market Execution (test_order_lifecycle.py)."""

import json
import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
