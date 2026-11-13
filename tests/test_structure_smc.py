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


if __name__ == "__main__":
    unittest.main()
