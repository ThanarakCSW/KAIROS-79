import json
import os
import unittest

from engine.data_feed import resample_bars
from engine.smc import analyze_smc
from engine.structure import analyze_multi_timeframe, analyze_structure, atr_series, ema_series

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "xauusd_m5_sample.json"
)


class TestStructureAndSMC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        cls.bars = doc["bars"]

    def test_ema_and_atr_calculation(self):
        closes = [b["c"] for b in self.bars]
        e20 = ema_series(closes, 20)
        e50 = ema_series(closes, 50)
        e200 = ema_series(closes, 200)
        atrs = atr_series(self.bars, 14)

        self.assertEqual(len(e20), len(self.bars))
        self.assertIsNone(e20[0])
        self.assertIsNotNone(e20[19])
        self.assertIsNotNone(e50[49])
        self.assertIsNotNone(e200[199])
        self.assertGreater(atrs[-1], 0.5)

    def test_structure_analysis_output_contract(self):
        res = analyze_structure(self.bars, timeframe="M5")
        self.assertEqual(res["created_by"], "xau-structure")
        self.assertEqual(res["timeframe"], "M5")
        self.assertEqual(res["current_price"], 2657.75)
        self.assertGreater(res["atr14"], 0.8)

        trend = res["trend"]
        self.assertIn(trend["regime"], ("Strong Trend", "Pullback", "Ranging"))
        self.assertIn(trend["bias"], ("BULLISH", "BEARISH", "NEUTRAL"))
        self.assertIsNotNone(trend["ema20"])
        self.assertIsNotNone(trend["ema50"])
        self.assertIsNotNone(trend["ema200"])

        self.assertGreater(len(res["swings"]["highs"]), 2)
        self.assertGreater(len(res["swings"]["lows"]), 2)
        self.assertGreater(len(res["sr_zones"]), 0)
        for z in res["sr_zones"]:
            self.assertLess(z["low"], z["high"])
            self.assertIn(z["role"], ("support", "resistance"))

    def test_smc_detection_and_trade_setup(self):
        res = analyze_smc(self.bars, timeframe="M5", spread_usd=0.25)
        self.assertEqual(res["created_by"], "xau-smc")
        self.assertGreater(len(res["structure_breaks"]), 0)
        self.assertGreater(len(res["order_blocks"]), 0)
        self.assertGreater(len(res["fvgs"]), 0)

        setup = res["trade_setup"]
        self.assertIn(setup["side"], ("BUY", "SELL"))
        self.assertGreater(setup["confluence_score"], 50)
        self.assertGreater(setup["reward_risk"], 1.5)
        if setup["side"] == "BUY":
            self.assertLess(setup["stop"], setup["entry"])
            self.assertGreater(setup["target"], setup["entry"])
        else:
            self.assertGreater(setup["stop"], setup["entry"])
            self.assertLess(setup["target"], setup["entry"])

    def test_candlestick_behavior_and_multi_timeframe_analysis(self):
        st = analyze_structure(self.bars, timeframe="M5")
        cb = st["candle_behavior"]
        self.assertGreaterEqual(cb["lookback_bars"], 30)
        self.assertIn(cb["wick_dominance"], ("BUYER_ABSORPTION", "SELLER_REJECTION", "BALANCED"))
        self.assertIn(cb["dealing_range"]["zone"], ("DISCOUNT", "EQUILIBRIUM", "PREMIUM"))
        self.assertIsInstance(cb["recent_patterns"], list)

        mtf_bars = {
            "M5": self.bars,
            "M15": resample_bars(self.bars, "M15"),
            "H1": resample_bars(self.bars, "H1"),
            "H4": resample_bars(self.bars, "H4"),
        }
        mtf = analyze_multi_timeframe(mtf_bars, primary_tf="M5", current_price=st["current_price"])
        self.assertEqual(mtf["primary_timeframe"], "M5")
        self.assertIn(mtf["overall_bias"], ("BULLISH", "BEARISH", "NEUTRAL"))
        for tf in ("M5", "M15", "H1", "H4"):
            self.assertIn(tf, mtf["timeframes"])
        self.assertGreater(len(mtf["htf_sr_zones"]), 0)

    def test_historical_sr_detection_and_multi_touch_clustering(self):
        """Verify deep historical support and resistance clustering, touches, and flip detection."""
        mtf_bars = {
            "M5": self.bars,
            "M15": resample_bars(self.bars, "M15"),
            "H1": resample_bars(self.bars, "H1"),
            "H4": resample_bars(self.bars, "H4"),
        }
        st = analyze_structure(self.bars, timeframe="M5", mtf_bars_by_tf=mtf_bars)
        self.assertIn("historical_sr", st)
        hist = st["historical_sr"]
        self.assertGreater(len(hist), 0)

        for z in hist:
            self.assertIn(z["effective_role"], ("support", "resistance"))
            self.assertGreaterEqual(z["touches_count"], 1)
            self.assertIn("label_th", z)
            self.assertIn("description_th", z)
            self.assertGreater(z["strength"], 0)
            self.assertGreater(z["mid"], 0)

        # Check that nearest historical support is below current price and nearest resistance is above
        cur_p = float(st["current_price"])
        if st["nearest_historical_support"]:
            self.assertLessEqual(st["nearest_historical_support"]["mid"], cur_p + 0.5)
        if st["nearest_historical_resistance"]:
            self.assertGreaterEqual(st["nearest_historical_resistance"]["mid"], cur_p - 0.5)

    def test_signal_tracker_market_now_vs_pending_limit_label_separation(self):
        """Verify market NOW vs pending LIMIT are clearly separated without mixing text."""
        from engine.signal_tracker import evaluate_signal_performance

        # 1. Market NOW setup
        mkt_ticket = {
            "side": "BUY",
            "entry_mt5": 2652.00,
            "stop_mt5": 2647.00,
            "target_mt5": 2662.00,
            "execution_type": "MARKET",
            "order_type": "BUY NOW (Market)",
            "verdict": "PASS",
            "win_prob_passed": True,
        }
        res_mkt = evaluate_signal_performance(self.bars, "M5", mkt_ticket, {})
        sig_mkt = res_mkt["recent_signals"][0]
        self.assertIn("⚡ เข้าทันที", sig_mkt["outcome_label_th"])
        self.assertIn("BUY NOW", sig_mkt["outcome_label_th"])
        self.assertNotIn("ดัก", sig_mkt["outcome_label_th"])

        # 2. Pending LIMIT setup
        lmt_ticket = {
            "side": "BUY",
            "entry_mt5": 2645.00,
            "stop_mt5": 2640.00,
            "target_mt5": 2655.00,
            "execution_type": "LIMIT",
            "order_type": "BUY LIMIT",
            "verdict": "PASS",
            "win_prob_passed": True,
        }
        res_lmt = evaluate_signal_performance(self.bars, "M5", lmt_ticket, {})
        sig_lmt = res_lmt["recent_signals"][0]
        self.assertIn("⏳ ดักเข้าล่วงหน้า", sig_lmt["outcome_label_th"])
        self.assertIn("BUY LIMIT", sig_lmt["outcome_label_th"])
        self.assertNotIn("NOW", sig_lmt["outcome_label_th"])
        self.assertNotIn("ทันที", sig_lmt["outcome_label_th"])

    def test_chart_pattern_detection_and_2000_bars_history(self):
        """Verify classical chart patterns detection across 1,500-2,000 bars."""
        from engine.data_feed import load_fixture_bars
        from engine.structure import detect_chart_patterns, detect_swings, atr_series

        deep_bars = load_fixture_bars()
        self.assertGreaterEqual(len(deep_bars), 1500, "Should load at least 1500 continuous historical bars")

        atr_val = atr_series(deep_bars, 14)[-1]
        sw = detect_swings(deep_bars, left=3, right=3, eq_tolerance=atr_val * 0.25)
        cur_p = float(deep_bars[-1]["c"])

        cp = detect_chart_patterns(
            bars=deep_bars,
            swings=sw,
            atr_val=atr_val,
            current_price=cur_p,
            max_lookback=2000,
        )

        self.assertIn("total_detected", cp)
        self.assertGreater(cp["total_detected"], 5, "Should detect multiple classical chart patterns in 1500 bars")
        self.assertIn("completed_count", cp)
        self.assertIn("success_rate_pct", cp)
        self.assertGreaterEqual(cp["success_rate_pct"], 0.0)

        # Check recent patterns structure
        recent = cp.get("recent_patterns", [])
        self.assertGreater(len(recent), 0)
        p0 = recent[0]
        self.assertIn("name", p0)
        self.assertIn("name_th", p0)
        self.assertIn("neckline", p0)
        self.assertIn("target", p0)
        self.assertIn("status", p0)
        self.assertIn(p0["status"], ("forming", "breakout", "completed", "invalidated"))
        self.assertIn("bars_ago", p0)

        # Check full structure and SMC integration
        st = analyze_structure(deep_bars, timeframe="M5")
        self.assertEqual(st["bars_count"], len(deep_bars))
        self.assertIn("chart_patterns", st)
        self.assertIsNotNone(st["chart_patterns"])

        smc_res = analyze_smc(deep_bars, timeframe="M5", structure_res=st)
        self.assertIn("chart_patterns", smc_res)
        self.assertIsNotNone(smc_res["chart_patterns"])


if __name__ == "__main__":
    unittest.main()
