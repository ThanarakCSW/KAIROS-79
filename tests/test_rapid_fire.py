import json
import os
import unittest
from engine.structure import analyze_structure, analyze_multi_timeframe
from engine.smc import analyze_smc, build_trade_setup
from engine.simulate import optimize_setup_for_win_prob
from engine.risk_gate import evaluate_risk_and_size
from engine.order_lifecycle import OrderLifecycleManager
from app import TradingAssistantCore

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURE_PATH = os.path.join(TEST_DIR, "fixtures", "xauusd_m5_sample.json")


class TestRapidFireDemoMode(unittest.TestCase):
    def setUp(self):
        self.persist_path = os.path.join(TEST_DIR, "temp_test_rapid.json")
        if os.path.exists(self.persist_path):
            os.remove(self.persist_path)
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.bars = doc["bars"] if isinstance(doc, dict) and "bars" in doc else doc
        self.structure = analyze_structure(self.bars, timeframe="M1")
        self.config = {
            "account_balance": 5000.0,
            "leverage": 100.0,
            "risk_per_trade_pct": 1.0,
            "min_reward_risk": 1.5,
            "broker_spread_points": 25.0,
            "point_size": 0.01,
            "min_win_prob_pct": 50.0,
            "rapid_fire_test": {
                "enabled": True,
                "lot_size": 0.01,
                "primary_timeframe": "M1",
                "target_tp_usd": 0.80,
                "hard_sl_usd": 1.50,
                "target_tp_usd_btc": 30.0,
                "hard_sl_usd_btc": 60.0,
                "max_concurrent_orders": 10,
                "min_spacing_usd": 0.30,
                "min_spacing_usd_btc": 15.0,
                "cooldown_seconds": 3,
                "scan_interval_seconds": 2,
            },
        }
        self.news_clear = {
            "in_news_blackout": False,
            "is_imminent_news": False,
            "blackout_events": [],
            "imminent_news_events": [],
            "session": {"is_rollover_danger": False},
        }

    def tearDown(self):
        if os.path.exists(self.persist_path):
            os.remove(self.persist_path)

    def test_rapid_fire_setup_generation(self):
        """Verify that Rapid-Fire mode creates an instant MARKET order with tight $1.50 SL and $0.80 TP."""
        setup = build_trade_setup(
            structure_res=self.structure,
            obs=[],
            fvgs=[],
            breaks=[],
            sweeps=[],
            liquidity_pools=[],
            spread_usd=0.25,
            trading_style="rapid_fire_test",
            symbol="XAUUSD",
        )
        self.assertEqual(setup["trading_style"], "rapid_fire_test")
        self.assertEqual(setup["execution_type"], "MARKET")
        self.assertEqual(setup["setup_archetype"], "DEMO_RAPID_FIRE")
        self.assertEqual(setup["confluence_score"], 95)
        self.assertIn("โหมดทดสอบยิงถี่ Demo", setup["style_label_th"])

        # Check tight SL ($1.50 + spread) and TP ($0.80)
        entry = setup["entry"]
        if setup["side"] == "BUY":
            self.assertAlmostEqual(setup["stop"], entry - 1.50, places=2)
            self.assertAlmostEqual(setup["target"], entry + 0.80, places=2)
        else:
            self.assertAlmostEqual(setup["stop"], entry + 1.50 + 0.25, places=2)
            self.assertAlmostEqual(setup["target"], entry - 0.80, places=2)

    def test_rapid_fire_risk_gate_passes_and_locks_001_lot(self):
        """Verify that Risk Gate passes Demo Rapid-Fire setups and locks lot size to strictly 0.01 lot."""
        setup = build_trade_setup(
            structure_res=self.structure,
            obs=[],
            fvgs=[],
            breaks=[],
            sweeps=[],
            liquidity_pools=[],
            spread_usd=0.25,
            trading_style="rapid_fire_test",
            symbol="XAUUSD",
        )
        sim = {
            "scanner_status": "SIGNAL_READY",
            "probabilities": {"win_pct": 65.0, "break_even_win_pct": 50.0},
            "expected_value_r": 0.35,
        }
        ticket = evaluate_risk_and_size(
            trade_setup=setup,
            simulation=sim,
            news_status=self.news_clear,
            config=self.config,
        )
        self.assertEqual(ticket["verdict"], "PASS")
        self.assertEqual(ticket["lot_size"], 0.01)
        self.assertTrue(ticket["is_rapid_fire"])
        self.assertIn("โหมดทดสอบยิงถี่ Demo", ticket["badge_th"])

    def test_rapid_fire_order_lifecycle_sub_orders(self):
        """Verify that Order Lifecycle Manager adds 0.01 sub-orders with tight spacing in rapid-fire mode."""
        mgr = OrderLifecycleManager(persistence_path=self.persist_path)
        cur_p = 2650.00
        setup = {
            "symbol": "XAUUSD",
            "side": "BUY",
            "execution_type": "MARKET",
            "order_type": "BUY NOW (Market)",
            "trading_style": "rapid_fire_test",
            "is_rapid_fire": True,
            "entry_mt5": cur_p,
            "stop_mt5": cur_p - 1.50,
            "target_mt5": cur_p + 0.80,
            "tp1_mt5": cur_p + 0.40,
            "lot_size": 0.01,
            "actual_risk_usd": 15.0,
            "potential_profit_usd": 8.0,
            "reward_risk": 0.53,
            "confluence_score": 95,
        }
        mgr.register_trade_setup(
            risk_ticket=dict(setup, verdict="PASS"),
            smc_setup=setup,
            current_price=cur_p,
            timeframe="M1",
            trading_style="rapid_fire_test",
            symbol="XAUUSD",
        )
        self.assertTrue(mgr.has_active_order())
        primary = mgr.active_orders[0]
        self.assertEqual(primary["lot_size"], 0.01)
        self.assertTrue(primary["is_rapid_fire"])

        # Add sub order requesting 0.10 lot -> must be overridden to 0.01
        sub = mgr.add_sub_order(
            purpose="SCALE_IN",
            execution_type="MARKET",
            current_price=cur_p + 0.35,
            lot_size=0.10,
        )
        self.assertIsNotNone(sub)
        self.assertEqual(sub["lot_size"], 0.01)
        self.assertTrue(sub["is_rapid_fire"])
        self.assertEqual(len(mgr.active_orders), 2)

    def test_rapid_fire_core_toggle(self):
        """Verify toggle_rapid_fire_mode switches timeframe to M1 and sets config."""
        core = TradingAssistantCore("XAUUSD", persistence_path=self.persist_path)
        was_on = core.toggle_rapid_fire_mode(True)
        self.assertTrue(was_on)
        self.assertEqual(core.timeframe, "M1")
        self.assertTrue(core.config["rapid_fire_test"]["enabled"])

        was_off = core.toggle_rapid_fire_mode(False)
        self.assertFalse(was_off)
        self.assertFalse(core.config["rapid_fire_test"]["enabled"])


if __name__ == "__main__":
    unittest.main()
