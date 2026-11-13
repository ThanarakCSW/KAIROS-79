import datetime
import unittest

from engine.news_feed import evaluate_news_window
from engine.risk_gate import evaluate_risk_and_size


class TestRiskGate(unittest.TestCase):
    def setUp(self):
        self.config = {
            "account_balance": 5000.0,
            "risk_per_trade_pct": 1.0,
            "contract_size": 100,
            "min_lot": 0.01,
            "max_lot": 10.0,
            "lot_step": 0.01,
            "min_reward_risk": 1.5,
            "min_sl_atr_mult": 0.8,
            "max_sweep_risk_pct": 20.0,
            "broker_price_offset": 0.50,
            "broker_spread_points": 20,
            "point_size": 0.01,
        }
        self.setup_buy = {
            "symbol": "XAUUSD",
            "side": "BUY",
            "status": "ACTIVE_IN_ZONE",
            "entry": 2650.00,
            "stop": 2645.00,
            "target": 2660.00,
            "sl_atr_ratio": 1.25,
            "scenario_b_sweep_reentry": 2642.50,
            "scenario_c_invalidation": 2640.00,
        }
        self.sim_good = {
            "expected_value_r": 0.65,
            "probabilities": {
                "win_pct": 55.0,
                "stop_pct": 35.0,
                "sweep_risk_pct": 10.0,
            },
        }
        self.news_clear = {
            "in_news_blackout": False,
            "blackout_minutes": 30,
            "blackout_events": [],
            "session": {"session": "LONDON", "is_rollover_danger": False},
        }

    def test_pass_verdict_and_lot_calculation_with_broker_offset(self):
        res = evaluate_risk_and_size(
            self.setup_buy, self.sim_good, self.news_clear, self.config
        )
        self.assertEqual(res["verdict"], "PASS")
        # 1% of $5,000 = $50 risk. SL distance = $5.00 ($500/lot). Lot = 50 / 500 = 0.10 lot
        self.assertEqual(res["lot_size"], 0.10)
        self.assertEqual(res["actual_risk_usd"], 50.0)
        # Broker offset +0.50 applied to MT5 levels
        self.assertEqual(res["entry_mt5"], 2650.50)
        self.assertEqual(res["stop_mt5"], 2645.50)
        self.assertEqual(res["target_mt5"], 2660.50)
        self.assertEqual(len(res["fingerprint"]), 16)

    def test_reject_on_low_rr_or_red_news(self):
        bad_setup = dict(self.setup_buy, target=2654.00)  # R:R = 4 / 5 = 0.8
        now = datetime.datetime(2026, 11, 11, 13, 20, tzinfo=datetime.timezone.utc)
        news_red = evaluate_news_window(
            now_utc=now,
            blackout_minutes=30,
            events_override=[
                {
                    "title": "Core CPI m/m",
                    "country": "USD",
                    "impact": "High",
                    "date": "2026-11-11T13:30:00Z",
                }
            ],
        )
        self.assertTrue(news_red["in_news_blackout"])
        res = evaluate_risk_and_size(bad_setup, self.sim_good, news_red, self.config)
        self.assertEqual(res["verdict"], "REJECT")
        self.assertGreaterEqual(len(res["reject_reasons"]), 2)

    def test_caution_on_tight_sl_and_high_sweep_risk(self):
        tight_setup = dict(self.setup_buy, sl_atr_ratio=0.55)
        high_sweep_sim = {
            "expected_value_r": 0.35,
            "probabilities": {
                "win_pct": 48.0,
                "stop_pct": 42.0,
                "sweep_risk_pct": 26.5,
            },
        }
        res = evaluate_risk_and_size(
            tight_setup, high_sweep_sim, self.news_clear, self.config
        )
        self.assertEqual(res["verdict"], "CAUTION")
        self.assertGreaterEqual(len(res["caution_warnings"]), 2)

    def test_strict_50pct_win_prob_rule_waits_when_not_exceeding_50pct(self):
        cfg_strict = dict(self.config, min_win_prob_pct=50.0, trading_style="scalping")
        sim_50_exact = {
            "expected_value_r": 0.40,
            "next_watch_zone": 2648.20,
            "probabilities": {
                "win_pct": 50.0,  # Must be strictly > 50.0%
                "stop_pct": 40.0,
                "sweep_risk_pct": 10.0,
            },
        }
        res_wait = evaluate_risk_and_size(
            self.setup_buy, sim_50_exact, self.news_clear, cfg_strict
        )
        self.assertFalse(res_wait["win_prob_passed"])
        self.assertEqual(res_wait["recommended_action"], "WAIT")
        self.assertEqual(res_wait["status"], "WATCHING_FOR_50PCT_EDGE")
        self.assertEqual(res_wait["next_watch_zone"], 2648.20)

        sim_above_50 = {
            "expected_value_r": 0.65,
            "probabilities": {
                "win_pct": 53.5,  # > 50.0% -> Signal Ready!
                "stop_pct": 36.5,
                "sweep_risk_pct": 10.0,
            },
        }
        res_ready = evaluate_risk_and_size(
            self.setup_buy, sim_above_50, self.news_clear, cfg_strict
        )
        self.assertTrue(res_ready["win_prob_passed"])
        self.assertEqual(res_ready["recommended_action"], "BUY")
        self.assertEqual(res_ready["verdict"], "PASS")

    def test_leverage_and_margin_sizing_checks(self):
        """Verify margin required calculation and gate enforcement across leverage ratios."""
        # 1. Normal account with 1:100 leverage
        cfg_100 = dict(self.config, account_balance=5000.0, leverage=100.0)
        res_100 = evaluate_risk_and_size(self.setup_buy, self.sim_good, self.news_clear, cfg_100)
        # Entry ~2650, 0.10 lot -> 0.10 * 100 * 2650 / 100 = $265 margin
        self.assertAlmostEqual(res_100["margin_required_usd"], (res_100["lot_size"] * 100 * res_100["entry_mt5"]) / 100.0, delta=1.0)
        self.assertEqual(res_100["leverage"], 100.0)
        self.assertGreater(res_100["free_margin_usd"], 0)

        # 2. 1:1000 leverage -> 10x smaller margin requirement
        cfg_1000 = dict(self.config, account_balance=5000.0, leverage=1000.0)
        res_1000 = evaluate_risk_and_size(self.setup_buy, self.sim_good, self.news_clear, cfg_1000)
        self.assertAlmostEqual(res_1000["margin_required_usd"], res_100["margin_required_usd"] / 10.0, delta=1.0)

        # 3. Small account ($100) with 1:20 leverage -> lot 0.02 requires $265.05 margin > $100 balance -> REJECT
        cfg_micro = dict(self.config, account_balance=100.0, leverage=20.0, risk_per_trade_pct=10.0)
        res_micro = evaluate_risk_and_size(self.setup_buy, self.sim_good, self.news_clear, cfg_micro)
        self.assertEqual(res_micro["verdict"], "REJECT")
        self.assertTrue(any("Margin ไม่พอ" in r for r in res_micro["reject_reasons"]))

    def test_daily_loss_limit_and_spread_guard_rejection(self):
        """Verify daily drawdown kill switch, spread USD cap, and max trades per day reject entries."""
        # 1. Daily drawdown >= max_daily_loss_pct -> REJECT
        cfg_dd = dict(self.config, max_daily_loss_pct=4.0, current_daily_loss_pct=4.2)
        res_dd = evaluate_risk_and_size(self.setup_buy, self.sim_good, self.news_clear, cfg_dd)
        self.assertEqual(res_dd["verdict"], "REJECT")
        self.assertTrue(any("เพดานขาดทุนรายวัน" in r for r in res_dd["reject_reasons"]))

        # 2. Spread USD > max_spread_usd (120 pts * 0.01 = $1.20 > $0.80) -> REJECT
        cfg_spread = dict(self.config, broker_spread_points=120.0, max_spread_usd=0.80)
        res_spread = evaluate_risk_and_size(self.setup_buy, self.sim_good, self.news_clear, cfg_spread)
        self.assertEqual(res_spread["verdict"], "REJECT")
        self.assertTrue(any("Spread สูงเกินเพดาน" in r for r in res_spread["reject_reasons"]))

        # 3. Max trades per day exhausted -> REJECT
        cfg_trades = dict(self.config, max_trades_per_day=6, trades_today_count=6)
        res_trades = evaluate_risk_and_size(self.setup_buy, self.sim_good, self.news_clear, cfg_trades)
        self.assertEqual(res_trades["verdict"], "REJECT")
        self.assertTrue(any("โควต้าจำนวนเทรด" in r for r in res_trades["reject_reasons"]))


if __name__ == "__main__":
    unittest.main()
