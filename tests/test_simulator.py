import json
import os
import unittest

from engine.simulate import run_simulation
from engine.smc import analyze_smc
from engine.structure import analyze_structure

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "xauusd_m5_sample.json"
)


class TestSimulator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        cls.bars = doc["bars"]
        cls.st = analyze_structure(cls.bars, timeframe="M5")
        cls.smc = analyze_smc(cls.bars, timeframe="M5", structure_res=cls.st)

    def test_monte_carlo_reproducibility_and_probabilities(self):
        sim1 = run_simulation(
            bars=self.bars,
            trade_setup=self.smc["trade_setup"],
            structure_res=self.st,
            replays=500,
            bars_ahead=30,
            seed=19,
        )
        sim2 = run_simulation(
            bars=self.bars,
            trade_setup=self.smc["trade_setup"],
            structure_res=self.st,
            replays=500,
            bars_ahead=30,
            seed=19,
        )

        # Deterministic with same seed
        self.assertEqual(sim1["probabilities"], sim2["probabilities"])
        self.assertEqual(sim1["expected_value_r"], sim2["expected_value_r"])

        probs = sim1["probabilities"]
        total_p = round(probs["p_win"] + probs["p_stop"] + probs["p_neither"], 4)
        self.assertAlmostEqual(total_p, 1.0, places=3)
        self.assertGreaterEqual(probs["p_sweep"], 0.0)
        self.assertLessEqual(probs["p_sweep"], probs["p_stop"])

        # Fan chart has bars_ahead + 1 steps and ordered percentiles
        self.assertEqual(len(sim1["fan_chart"]), 31)
        last_band = sim1["fan_chart"][-1]
        self.assertLessEqual(last_band["p5"], last_band["p25"])
        self.assertLessEqual(last_band["p25"], last_band["p50"])
        self.assertLessEqual(last_band["p50"], last_band["p75"])
        self.assertLessEqual(last_band["p75"], last_band["p95"])

        # 3 Scenarios present
        self.assertIn("scenario_a_primary", sim1["scenarios"])
        self.assertIn("scenario_b_sweep_trap", sim1["scenarios"])
        self.assertIn("scenario_c_invalidation", sim1["scenarios"])

    def test_35k_simulation_paths_and_performance(self):
        """Verify 35,000 paths Monte Carlo simulation computes accurately and finishes quickly."""
        import time
        t0 = time.perf_counter()
        sim = run_simulation(
            bars=self.bars,
            trade_setup=self.smc["trade_setup"],
            structure_res=self.st,
            replays=35000,
            bars_ahead=36,
            seed=42,
        )
        elapsed = time.perf_counter() - t0
        self.assertEqual(sim["replays"], 35000)
        self.assertIn("win_pct", sim["probabilities"])
        self.assertIn("expected_value_r", sim)
        self.assertLess(elapsed, 2.5, f"35,000 simulations took {elapsed:.2f}s, expected < 2.5s")


if __name__ == "__main__":
    unittest.main()
