import os
import shutil
import tempfile
import unittest

from engine.data_feed import MarketDataHub, load_fixture_bars
from engine.smc import analyze_smc
from engine.risk_gate import evaluate_risk_and_size
from engine.order_lifecycle import OrderLifecycleManager
from app import TradingAssistantCore, AssistantServer


class TestBitcoinEngine(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.btc_order_file = os.path.join(self.tmp_dir, "active_order_btc.json")
        self.xau_order_file = os.path.join(self.tmp_dir, "active_order.json")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_fixture_bars_scaling_for_btc(self):
        """Verify fixture bars for BTCUSD are scaled to ~$90,500 with proper candle structure."""
        xau_bars = load_fixture_bars(symbol="XAUUSD")
        btc_bars = load_fixture_bars(symbol="BTCUSD")

        self.assertGreater(len(btc_bars), 50)
        self.assertEqual(len(xau_bars), len(btc_bars))

        # Check price levels: XAU is ~$2650, BTC is ~$90,500
        latest_btc = btc_bars[-1]
        self.assertGreater(latest_btc["c"], 80000.0)
        self.assertLess(latest_btc["c"], 105000.0)
        self.assertGreaterEqual(latest_btc["h"], latest_btc["l"])
        self.assertGreaterEqual(latest_btc["h"], latest_btc["o"])
        self.assertGreaterEqual(latest_btc["h"], latest_btc["c"])

    def test_market_data_hub_fallback_for_btc(self):
        """Verify MarketDataHub returns valid BTC bars when external feeds are mocked/offline."""
        hub = MarketDataHub()
        res = hub.get_bars(symbol="BTCUSD", timeframe="M5")
        bars = res.get("bars", [])
        self.assertGreaterEqual(len(bars), 50)
        self.assertGreater(bars[-1]["c"], 80000.0)
        self.assertEqual(res.get("symbol"), "BTCUSD")

    def test_smc_analysis_for_btc(self):
        """Verify SMC engine produces valid market structure and zones for BTCUSD."""
        bars = load_fixture_bars(symbol="BTCUSD")
        structure = analyze_smc(bars, symbol="BTCUSD")

        self.assertIn("current_price", structure)
        self.assertGreater(structure["current_price"], 80000.0)
        self.assertIn("atr14", structure)
        self.assertIn("order_blocks", structure)
        self.assertIn("fvgs", structure)
        self.assertIn("structure_breaks", structure)

        setup = structure.get("trade_setup")
        self.assertIsNotNone(setup)
        self.assertEqual(setup.get("symbol"), "BTCUSD")
        self.assertGreater(setup.get("entry", 0), 80000.0)

    def test_risk_gate_btc_contract_size_and_lot_sizing(self):
        """Verify risk gate uses contract_size = 1.0 (1 lot = 1 BTC) and correctly calculates risk & margin."""
        btc_setup = {
            "symbol": "BTCUSD",
            "side": "BUY",
            "status": "ACTIVE_IN_ZONE",
            "entry": 90500.0,
            "stop": 90200.0,  # $300 SL distance
            "target": 91200.0,  # $700 TP distance (R:R 2.33)
            "sl_atr_ratio": 1.5,
        }

        sim_pass = {
            "expected_value_r": 0.85,
            "probabilities": {
                "win_pct": 58.0,
                "stop_pct": 32.0,
                "sweep_risk_pct": 10.0,
            },
        }

        news_clear = {
            "in_news_blackout": False,
            "blackout_minutes": 30,
            "session": {"is_rollover_danger": False},
        }

        cfg = {
            "account_balance": 10000.0,
            "risk_per_trade_pct": 1.0,  # $100 risk budget
            "leverage": 100.0,
            "broker_spread_points": 1500.0,  # $15 spread
            "min_reward_risk": 1.5,
            "min_sl_atr_mult": 0.8,
            "max_sweep_risk_pct": 25.0,
        }

        ticket = evaluate_risk_and_size(
            trade_setup=btc_setup,
            simulation=sim_pass,
            news_status=news_clear,
            config=cfg,
        )

        self.assertEqual(ticket["verdict"], "PASS")
        self.assertEqual(ticket["symbol"], "BTCUSD")
        self.assertEqual(ticket["contract_size"], 1.0)
        self.assertEqual(ticket["point_size"], 0.01)

        # SL dist with spread: 90500 - 90200 + 15 = $315 per BTC
        # Budget = $100 -> lot size = 100 / 315 = ~0.31 lot
        self.assertAlmostEqual(ticket["lot_size"], 0.31, places=1)
        self.assertGreater(ticket["margin_required_usd"], 0.0)

    def test_order_lifecycle_btc_state_and_pnl(self):
        """Verify OrderLifecycleManager handles BTC floating PnL with contract_size = 1.0."""
        mgr = OrderLifecycleManager(persistence_path=self.btc_order_file)

        setup = {
            "symbol": "BTCUSD",
            "side": "BUY",
            "entry_mt5": 90000.0,
            "stop_mt5": 89500.0,
            "target_mt5": 91500.0,
            "lot_size": 0.50,  # 0.5 BTC
            "order_type": "MARKET_NOW",
            "execution_type": "MARKET",
            "actual_risk_usd": 250.0,
            "reward_risk": 3.0,
        }

        mgr.register_trade_setup(setup)
        mgr.fill_now(current_price=90000.0)

        # Price rises by +$1,000 to $91,000
        # PnL = (91000 - 90000) * 0.5 lot * 1.0 contract_size = +$500.0
        mgr.update_price_tick(current_price=91000.0)
        state = mgr.get_active_order_dict(current_price=91000.0)

        self.assertEqual(state["symbol"], "BTCUSD")
        self.assertEqual(state["status"], "FILLED")
        self.assertAlmostEqual(state["floating_pnl_usd"], 500.0, places=2)
        self.assertAlmostEqual(state["floating_pnl_r"], 2.0, places=2)

    def test_state_isolation_between_xau_and_btc(self):
        """Verify Gold and Bitcoin order lifecycles and state persistence do not overwrite each other."""
        xau_mgr = OrderLifecycleManager(persistence_path=self.xau_order_file)
        btc_mgr = OrderLifecycleManager(persistence_path=self.btc_order_file)

        xau_setup = {
            "symbol": "XAUUSD",
            "side": "BUY",
            "entry_mt5": 2650.0,
            "stop_mt5": 2645.0,
            "target_mt5": 2665.0,
            "lot_size": 0.10,
            "execution_type": "MARKET",
        }
        btc_setup = {
            "symbol": "BTCUSD",
            "side": "SELL",
            "entry_mt5": 90500.0,
            "stop_mt5": 91000.0,
            "target_mt5": 89000.0,
            "lot_size": 0.05,
            "execution_type": "LIMIT",
        }

        xau_mgr.register_trade_setup(xau_setup)
        btc_mgr.register_trade_setup(btc_setup)

        xau_dict = xau_mgr.get_active_order_dict()
        btc_dict = btc_mgr.get_active_order_dict()

        self.assertEqual(xau_dict["symbol"], "XAUUSD")
        self.assertEqual(xau_dict["side"], "BUY")
        self.assertEqual(btc_dict["symbol"], "BTCUSD")
        self.assertEqual(btc_dict["side"], "SELL")

        # Verify files are isolated
        self.assertTrue(os.path.exists(self.xau_order_file))
        self.assertTrue(os.path.exists(self.btc_order_file))

    def test_trading_assistant_core_btc(self):
        """Verify TradingAssistantCore instantiates and calculates state for BTCUSD."""
        core = TradingAssistantCore(symbol="BTCUSD")
        state = core.compute_full_state()

        self.assertEqual(state["symbol"], "BTCUSD")
        self.assertIn("structure", state)
        self.assertIn("risk_ticket", state)
        self.assertEqual(state["risk_ticket"]["symbol"], "BTCUSD")
        self.assertGreater(state["structure"]["current_price"], 80000.0)

    def test_server_routes_btc_page_and_api(self):
        """Verify AssistantServer serves /btc, /btc.html, and /api/state?symbol=BTCUSD."""
        server = AssistantServer(host="127.0.0.1", port=8000)

        # Verify server has dual cores
        self.assertIn("XAUUSD", server.cores)
        self.assertIn("BTCUSD", server.cores)

        btc_core = server.get_core("BTCUSD")
        self.assertEqual(btc_core.symbol, "BTCUSD")

        xau_core = server.get_core("XAUUSD")
        self.assertEqual(xau_core.symbol, "XAUUSD")


if __name__ == "__main__":
    unittest.main()
