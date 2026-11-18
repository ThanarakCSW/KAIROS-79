import os
import shutil
import tempfile
import unittest

from engine.loss_learning import LossPatternMemoryManager
from engine.risk_gate import evaluate_risk_and_size


class TestLossLearningAndDailyTarget(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.loss_file = os.path.join(self.test_dir, "loss_patterns_test.json")
        self.mgr = LossPatternMemoryManager(persistence_path=self.loss_file)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_loss_and_cluster(self):
        order_1 = {
            "order_id": "ord-001",
            "symbol": "XAUUSD",
            "side": "BUY",
            "entry_mt5": 2650.0,
            "stop_mt5": 2640.0,
            "target_mt5": 2670.0,
            "realized_pnl_usd": -100.0,
            "close_reason": "SL_HIT",
            "trading_style": "micro_scalp",
        }
        mkt_ctx = {
            "structure": {
                "trend": "BEARISH",
                "current_price": 2639.0,
            }
        }
        self.mgr.record_loss_order(order_1, market_context=mkt_ctx)
        self.assertEqual(len(self.mgr.patterns), 1)
        pat = list(self.mgr.patterns.values())[0]
        self.assertEqual(pat["side"], "BUY")
        self.assertEqual(pat["failure_count"], 1)
        self.assertEqual(pat["total_loss_usd"], 100.0)

        # Record another loss with the same signature to form a cluster
        order_2 = {
            "order_id": "ord-002",
            "symbol": "XAUUSD",
            "side": "BUY",
            "entry_mt5": 2648.0,
            "stop_mt5": 2638.0,
            "target_mt5": 2668.0,
            "realized_pnl_usd": -120.0,
            "close_reason": "SL_HIT",
            "trading_style": "micro_scalp",
        }
        self.mgr.record_loss_order(order_2, market_context=mkt_ctx)
        self.assertEqual(len(self.mgr.patterns), 1)
        pat = list(self.mgr.patterns.values())[0]
        self.assertEqual(pat["failure_count"], 2)
        self.assertEqual(pat["total_loss_usd"], 220.0)

        # Now evaluate candidate setup matching this failing cluster
        candidate = {
            "side": "BUY",
            "confluence_score": 60,
        }
        eval_res = self.mgr.evaluate_setup_risk(
            trade_setup=candidate,
            structure_res={"trend": "BEARISH"},
            symbol="XAUUSD",
        )
        self.assertTrue(eval_res["is_high_risk"])
        self.assertGreater(eval_res["risk_score"], 0)
        self.assertIn("เคยล้มเหลว", eval_res["warning"])

    def test_consecutive_loss_trips_20pct_circuit_breaker(self):
        balance = 1000.0
        # Create history with consecutive losses reaching $210 (21% of $1000)
        order_history = [
            {"order_id": "1", "realized_pnl_usd": 50.0, "status": "CLOSED_TP"},
            {"order_id": "2", "realized_pnl_usd": -80.0, "status": "CLOSED_SL"},
            {"order_id": "3", "realized_pnl_usd": -70.0, "status": "CLOSED_SL"},
            {"order_id": "4", "realized_pnl_usd": -60.0, "status": "CLOSED_SL"},
        ]
        info = self.mgr.update_consecutive_losses(order_history, balance=balance, threshold_pct=20.0)
        self.assertEqual(info["consecutive_loss_count"], 3)
        self.assertEqual(info["consecutive_loss_usd"], 210.0)
        self.assertEqual(info["consecutive_loss_pct"], 21.0)
        self.assertTrue(info["circuit_breaker_tripped"])
        self.assertIn("ขาดทุนสะสมต่อเนื่อง", info["reason"])

        # Reset circuit breaker
        self.mgr.reset_circuit_breaker()
        status = self.mgr.get_status()
        self.assertFalse(status["circuit_breaker_tripped"])
        self.assertEqual(status["consecutive_losses_count"], 0)
        self.assertEqual(status["consecutive_losses_usd"], 0.0)

    def test_deep_reanalysis_report_generation(self):
        report = self.mgr.run_deep_reanalysis(
            symbol="XAUUSD",
            timeframe="M5",
            structure={"trend": "BEARISH", "current_price": 2650.0},
            mtf={"bias": "BEARISH", "h4_trend": "BEARISH"},
            macro={"dxy_trend": "BULLISH", "us10y_trend": "BULLISH"},
            account_balance=5000.0,
        )
        self.assertIn("diagnostic_summary_th", report)
        self.assertIn("strategic_directives", report)
        self.assertGreater(len(report["strategic_directives"]), 0)
        self.assertEqual(report["market_context"]["trend"], "BEARISH")

    def test_risk_gate_daily_profit_target_reached(self):
        setup = {
            "side": "BUY",
            "entry": 2650.0,
            "stop": 2640.0,
            "target": 2670.0,
            "take_profit_1": 2660.0,
            "take_profit_2": 2670.0,
            "confluence_score": 75,
        }
        sim = {
            "expected_value_r": 0.65,
            "probabilities": {
                "win_pct": 55.0,
            },
        }
        news = {"is_imminent_news": False}

        # Case 1: Daily profit target reached -> REJECT
        cfg_target_reached = {
            "account_balance": 5000.0,
            "risk_per_trade_pct": 1.0,
            "daily_profit_target_usd": 10.0,
            "daily_profit_target_enabled": True,
            "current_daily_profit_usd": 15.0,
        }
        res = evaluate_risk_and_size(trade_setup=setup, simulation=sim, news_status=news, config=cfg_target_reached)
        self.assertEqual(res["verdict"], "REJECT")
        self.assertTrue(any("บรรลุเป้าหมายกำไรรายวัน" in r for r in res["reject_reasons"]))

        # Case 2: Daily profit target NOT reached -> PASS
        cfg_not_reached = {
            "account_balance": 5000.0,
            "risk_per_trade_pct": 1.0,
            "daily_profit_target_usd": 20.0,
            "daily_profit_target_enabled": True,
            "current_daily_profit_usd": 5.0,
        }
        res2 = evaluate_risk_and_size(trade_setup=setup, simulation=sim, news_status=news, config=cfg_not_reached)
        self.assertEqual(res2["verdict"], "PASS")

    def test_risk_gate_20pct_circuit_breaker_rejection(self):
        setup = {
            "side": "BUY",
            "entry": 2650.0,
            "stop": 2640.0,
            "target": 2670.0,
            "take_profit_1": 2660.0,
            "take_profit_2": 2670.0,
            "confluence_score": 75,
        }
        sim = {
            "expected_value_r": 0.65,
            "probabilities": {
                "win_pct": 55.0,
            },
        }
        news = {"is_imminent_news": False}
        cfg_circuit_tripped = {
            "account_balance": 5000.0,
            "risk_per_trade_pct": 1.0,
            "circuit_breaker_20pct_tripped": True,
            "consecutive_loss_count": 4,
            "consecutive_loss_usd": 1100.0,
        }
        res = evaluate_risk_and_size(trade_setup=setup, simulation=sim, news_status=news, config=cfg_circuit_tripped)
        self.assertEqual(res["verdict"], "REJECT")
        self.assertTrue(any("ระบบตัดวงจรหยุดเทรดฉุกเฉิน" in r for r in res["reject_reasons"]))

    def test_risk_gate_loss_pattern_memory_rejection(self):
        setup = {
            "side": "BUY",
            "entry": 2650.0,
            "stop": 2640.0,
            "target": 2670.0,
            "take_profit_1": 2660.0,
            "take_profit_2": 2670.0,
            "confluence_score": 75,
        }
        sim = {
            "expected_value_r": 0.65,
            "probabilities": {
                "win_pct": 55.0,
            },
        }
        news = {"is_imminent_news": False}
        cfg_loss_pattern_veto = {
            "account_balance": 5000.0,
            "risk_per_trade_pct": 1.0,
            "loss_pattern_eval": {
                "is_high_risk": True,
                "warning_message": "AI สกัดกั้นตามฐานความจำการขาดทุน: รูปแบบนี้เคยแพ้ซ้ำ 3 ครั้ง",
            },
        }
        res = evaluate_risk_and_size(trade_setup=setup, simulation=sim, news_status=news, config=cfg_loss_pattern_veto)
        self.assertEqual(res["verdict"], "REJECT")
        self.assertTrue(any("AI สกัดกั้นตามฐานความจำการขาดทุน" in r for r in res["reject_reasons"]))


if __name__ == "__main__":
    unittest.main()
