import os
import shutil
import tempfile
import unittest

from engine.data_feed import load_fixture_bars
from engine.risk_gate import evaluate_risk_and_size
from engine.order_lifecycle import OrderLifecycleManager


class TestCrossAssetBreadth(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.order_file = os.path.join(self.tmp_dir, "active_order.json")
        self.mgr = OrderLifecycleManager(persistence_path=self.order_file)

        self.base_cfg = {
            "account_balance": 5000.0,
            "risk_per_trade_pct": 2.0,  # $100 budget
            "min_lot": 0.01,
            "max_lot": 10.0,
            "lot_step": 0.01,
            "min_reward_risk": 1.5,
            "min_sl_atr_mult": 0.8,
            "max_sweep_risk_pct": 25.0,
            "cross_asset": {
                "enabled": True,
                "symbols": ["XAUUSD", "EURUSD", "GBPUSD"],
                "max_open_symbols": 2,
                "usd_correlation_guard": True,
            },
        }

        self.news_clear = {
            "in_news_blackout": False,
            "blackout_minutes": 30,
            "session": {"is_rollover_danger": False},
        }

        self.sim_pass = {
            "expected_value_r": 0.70,
            "probabilities": {
                "win_pct": 58.0,
                "stop_pct": 32.0,
                "sweep_risk_pct": 10.0,
            },
        }

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_forex_risk_and_lot_sizing(self):
        """Verify Forex lot sizing: contract size 100,000, 5 digits, 0.00001 point size."""
        eur_setup = {
            "symbol": "EURUSD",
            "side": "BUY",
            "status": "ACTIVE_IN_ZONE",
            "entry": 1.05500,
            "stop": 1.05300,  # 20 pips SL
            "target": 1.05900,  # 40 pips TP (2.0 R:R)
            "sl_atr_ratio": 1.5,
        }

        ticket = evaluate_risk_and_size(
            trade_setup=eur_setup,
            simulation=self.sim_pass,
            news_status=self.news_clear,
            config=self.base_cfg,
        )

        self.assertEqual(ticket["verdict"], "PASS")
        self.assertEqual(ticket["symbol"], "EURUSD")
        self.assertEqual(ticket["pip_digits"], 5)
        self.assertEqual(ticket["contract_size"], 100000.0)
        self.assertEqual(ticket["point_size"], 0.00001)
        self.assertEqual(ticket["entry_mt5"], 1.05500)
        self.assertEqual(ticket["stop_mt5"], 1.05300)
        self.assertEqual(ticket["target_mt5"], 1.05900)

        # Risk budget $5,000 * 2% = $100.
        # Loss per 1 lot = 0.00200 * 100,000 = $200.
        # Lot size = $100 / $200 = 0.50 lot.
        self.assertEqual(ticket["lot_size"], 0.50)
        self.assertEqual(ticket["reward_risk"], 2.0)

    def test_usd_correlation_guard_and_portfolio_capacity(self):
        """Verify USD Correlation Guard prevents opposing USD trades and caps max open symbols."""
        eur_setup_buy = {
            "symbol": "EURUSD",
            "side": "BUY",
            "entry": 1.05500,
            "stop": 1.05300,
            "target": 1.05900,
            "sl_atr_ratio": 1.5,
        }
        eur_setup_sell = {
            "symbol": "EURUSD",
            "side": "SELL",
            "entry": 1.05500,
            "stop": 1.05700,
            "target": 1.05100,
            "sl_atr_ratio": 1.5,
        }

        # Case A: Gold has active BUY, EURUSD wants BUY -> Aligned on USD (Short USD) -> PASS
        cfg_aligned = dict(
            self.base_cfg,
            active_open_symbols=["XAUUSD"],
            active_symbol_directions={"XAUUSD": "BUY"},
        )
        t_aligned = evaluate_risk_and_size(
            trade_setup=eur_setup_buy,
            simulation=self.sim_pass,
            news_status=self.news_clear,
            config=cfg_aligned,
        )
        self.assertEqual(t_aligned["verdict"], "PASS")

        # Case B: Gold has active BUY, EURUSD wants SELL -> Opposing USD -> REJECT
        t_opposing = evaluate_risk_and_size(
            trade_setup=eur_setup_sell,
            simulation=self.sim_pass,
            news_status=self.news_clear,
            config=cfg_aligned,
        )
        self.assertEqual(t_opposing["verdict"], "REJECT")
        self.assertTrue(any("USD Correlation Guard" in r for r in t_opposing["reject_reasons"]))

        # Case C: Max open symbols is 2, currently XAUUSD and EURUSD open -> GBPUSD wants BUY -> REJECT (capacity)
        gbp_setup = {
            "symbol": "GBPUSD",
            "side": "BUY",
            "entry": 1.26500,
            "stop": 1.26250,
            "target": 1.27000,
            "sl_atr_ratio": 1.5,
        }
        cfg_full = dict(
            self.base_cfg,
            active_open_symbols=["XAUUSD", "EURUSD"],
            active_symbol_directions={"XAUUSD": "BUY", "EURUSD": "BUY"},
        )
        t_cap = evaluate_risk_and_size(
            trade_setup=gbp_setup,
            simulation=self.sim_pass,
            news_status=self.news_clear,
            config=cfg_full,
        )
        self.assertEqual(t_cap["verdict"], "REJECT")
        self.assertTrue(any("เกินโควต้าจำนวนคู่เงิน" in r for r in t_cap["reject_reasons"]))

    def test_multi_symbol_order_lifecycle_isolation(self):
        """Verify order lifecycle isolates ticks by symbol and calculates floating PnL accurately."""
        # 1. Register XAUUSD BUY limit @ 2650.00
        gold_ticket = {
            "symbol": "XAUUSD",
            "side": "BUY",
            "execution_type": "LIMIT",
            "entry_mt5": 2650.00,
            "stop_mt5": 2645.00,
            "tp1_mt5": 2655.00,
            "target_mt5": 2660.00,
            "lot_size": 0.10,
            "contract_size": 100.0,
            "point_size": 0.01,
            "pip_digits": 2,
            "actual_risk_usd": 50.0,
        }
        gold_ord = self.mgr.register_trade_setup(risk_ticket=gold_ticket, current_price=2652.00, symbol="XAUUSD")
        self.assertIsNotNone(gold_ord)
        self.assertEqual(gold_ord["status"], "PENDING")

        # 2. Register EURUSD BUY limit @ 1.05500
        eur_ticket = {
            "symbol": "EURUSD",
            "side": "BUY",
            "execution_type": "LIMIT",
            "entry_mt5": 1.05500,
            "stop_mt5": 1.05300,
            "tp1_mt5": 1.05700,
            "target_mt5": 1.05900,
            "lot_size": 0.50,
            "contract_size": 100000.0,
            "point_size": 0.00001,
            "pip_digits": 5,
            "actual_risk_usd": 100.0,
        }
        eur_ord = self.mgr.register_trade_setup(risk_ticket=eur_ticket, current_price=1.05600, symbol="EURUSD")
        self.assertIsNotNone(eur_ord)
        self.assertEqual(eur_ord["status"], "PENDING")

        # Both symbols exist concurrently in active_orders
        self.assertEqual(len(self.mgr.active_orders), 2)
        self.assertTrue(self.mgr.has_active_order(symbol="XAUUSD"))
        self.assertTrue(self.mgr.has_active_order(symbol="EURUSD"))

        # 3. Price tick on Gold (2649.50) -> Fills Gold, does NOT affect EURUSD
        ev_gold = self.mgr.update_price_tick(
            current_price=2649.50,
            low=2649.50,
            high=2651.00,
            symbol="XAUUSD",
            contract_size=100.0,
        )
        self.assertIsNotNone(ev_gold)
        self.assertEqual(ev_gold["type"], "ORDER_FILLED")

        # EURUSD remains PENDING
        eur_live = next(o for o in self.mgr.active_orders if o["symbol"] == "EURUSD")
        self.assertEqual(eur_live["status"], "PENDING")

        # 4. Price tick on EURUSD (1.05480) -> Fills EURUSD
        ev_eur = self.mgr.update_price_tick(
            current_price=1.05480,
            low=1.05480,
            high=1.05550,
            symbol="EURUSD",
            contract_size=100000.0,
        )
        self.assertIsNotNone(ev_eur)
        self.assertEqual(ev_eur["type"], "ORDER_FILLED")

        # 5. Price tick on EURUSD advances to 1.05600 (+10 pips = +$50 on 0.50 lot)
        self.mgr.update_price_tick(
            current_price=1.05600,
            low=1.05550,
            high=1.05610,
            symbol="EURUSD",
            contract_size=100000.0,
        )
        eur_dict = self.mgr.get_active_order_dict(1.05600, symbol="EURUSD")
        self.assertIsNotNone(eur_dict)
        # Delta = 1.05600 - 1.05500 = +0.00100 * 0.50 * 100,000 = +$50.00 (+0.50R)
        self.assertEqual(eur_dict["total_floating_pnl_usd"], 50.0)
        self.assertEqual(eur_dict["total_floating_pnl_r"], 0.50)

    def test_data_feed_fixture_scaling(self):
        """Verify load_fixture_bars scales bars accurately for EURUSD and GBPUSD."""
        eur_bars = load_fixture_bars(symbol="EURUSD", target_count=50)
        self.assertGreater(len(eur_bars), 0)
        # Price should be around 1.0550
        self.assertGreater(eur_bars[-1]["c"], 0.90)
        self.assertLess(eur_bars[-1]["c"], 1.30)

        gbp_bars = load_fixture_bars(symbol="GBPUSD", target_count=50)
        self.assertGreater(len(gbp_bars), 0)
        # Price should be around 1.2650
        self.assertGreater(gbp_bars[-1]["c"], 1.10)
        self.assertLess(gbp_bars[-1]["c"], 1.50)

    def test_cross_asset_full_pipeline_and_http_toggle(self):
        """Verify full pipeline includes cross_asset state and /api/cross_asset/toggle works."""
        import asyncio
        from app import AssistantServer, TradingAssistantCore

        core = TradingAssistantCore()
        core.config["cross_asset"] = {
            "enabled": True,
            "symbols": ["XAUUSD", "EURUSD", "GBPUSD"],
            "max_open_symbols": 2,
            "usd_correlation_guard": True,
        }
        state = core.compute_full_state(timeframe="M5")
        self.assertIn("cross_asset", state)
        self.assertTrue(state["cross_asset"]["enabled"])
        self.assertIn("EURUSD", state["cross_asset"]["symbols"])
        self.assertIn("GBPUSD", state["cross_asset"]["symbols"])

        eur_info = state["cross_asset"]["symbols"]["EURUSD"]
        self.assertEqual(eur_info["symbol"], "EURUSD")
        self.assertGreater(eur_info["current_price"], 0.90)
        self.assertIn("structure", eur_info)
        self.assertIn("risk_ticket", eur_info)

        # Test toggle via server handle_http_request
        server = AssistantServer()
        class DummyWriter:
            def __init__(self):
                self.data = b""
                self.closed = False
            def write(self, d):
                self.data += d
            async def drain(self):
                pass
            def close(self):
                self.closed = True

        # Toggle to False
        w = DummyWriter()
        asyncio.run(
            server.handle_http_request(
                writer=w,
                method="POST",
                path="/api/cross_asset/toggle",
                query={},
                body_bytes=b'{"enabled": false}',
            )
        )
        self.assertIn(b"200 OK", w.data)
        self.assertIn(b'"cross_asset_enabled":false', w.data)

        # Toggle to True
        w2 = DummyWriter()
        asyncio.run(
            server.handle_http_request(
                writer=w2,
                method="POST",
                path="/api/cross_asset/toggle",
                query={},
                body_bytes=b'{"enabled": true}',
            )
        )
        self.assertIn(b"200 OK", w2.data)
        self.assertIn(b'"cross_asset_enabled":true', w2.data)
