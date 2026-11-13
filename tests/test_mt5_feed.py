#!/usr/bin/env python3
"""Unit tests for Tier 0 MT5 Broker Feed Ingestion and Fallback Engine."""

import unittest
from unittest.mock import MagicMock, patch

from engine.data_feed import (
    MarketDataHub,
    fetch_mt5_broker_bars,
    fetch_mt5_live_tick,
    resolve_mt5_gold_symbol,
)


class TestMT5BrokerFeed(unittest.TestCase):
    def test_mt5_feed_graceful_fallback_when_unavailable(self):
        """When MetaTrader5 library is not present or MT5 terminal is closed, functions return None gracefully."""
        res = fetch_mt5_broker_bars("M5", limit=100)
        # On non-Windows or when MT5 is not running, must be None without throwing exceptions
        self.assertIsNone(res)

        tick = fetch_mt5_live_tick()
        self.assertIsNone(tick)

    def test_mt5_broker_bars_mock_parsing(self):
        """Verify MT5 numpy recarray rates and tick are parsed accurately into OHLCV bars and quote."""
        mock_mt5 = MagicMock()
        mock_mt5.initialize.return_value = True

        mock_sym_info = MagicMock()
        mock_sym_info.visible = True
        mock_mt5.symbol_info.return_value = mock_sym_info

        mock_tick = MagicMock()
        mock_tick.bid = 2655.25
        mock_tick.ask = 2655.45
        mock_tick.time = 1794580000
        mock_mt5.symbol_info_tick.return_value = mock_tick

        mock_term = MagicMock()
        mock_term.name = "ICMarkets-MT5"
        mock_mt5.terminal_info.return_value = mock_term

        # Sample rates array
        mock_rates = [
            {"time": 1794579600, "open": 2654.0, "high": 2656.0, "low": 2653.5, "close": 2655.0, "tick_volume": 120},
            {"time": 1794579900, "open": 2655.0, "high": 2656.5, "low": 2654.8, "close": 2655.25, "tick_volume": 145},
        ]
        mock_mt5.copy_rates_from_pos.return_value = mock_rates

        with patch("engine.data_feed.HAS_MT5_LIB", True), \
             patch("engine.data_feed._mt5", mock_mt5), \
             patch("engine.data_feed._mt5_initialized", True):
            res = fetch_mt5_broker_bars(timeframe="M5", preferred_symbol="XAUUSDm", limit=100)
            self.assertIsNotNone(res)
            self.assertEqual(len(res["bars"]), 2)
            self.assertEqual(res["bars"][-1]["c"], 2655.25)
            self.assertEqual(res["source"], "mt5_broker (ICMarkets-MT5 : XAUUSDm)")
            self.assertEqual(res["quote"]["bid"], 2655.25)
            self.assertEqual(res["quote"]["ask"], 2655.45)
            self.assertEqual(res["quote"]["spread_points"], 20.0)

    def test_hub_integrates_mt5_tier0_feed(self):
        """Verify MarketDataHub seamlessly adopts MT5 broker feed when available."""
        hub = MarketDataHub()
        mock_bars_data = {
            "bars": [
                {"t": 1794579900, "o": 2655.0, "h": 2656.0, "l": 2654.0, "c": 2655.5, "v": 100}
            ],
            "source": "mt5_broker (Exness : XAUUSDm)",
            "timeframe": "M5",
            "quote": {"price": 2655.5, "bid": 2655.5, "ask": 2655.7, "ts": 1794579900, "source": "mt5_broker (XAUUSDm)"},
            "symbol": "XAUUSDm",
        }

        with patch("engine.data_feed.fetch_mt5_broker_bars", return_value=mock_bars_data):
            res = hub.get_bars("M5", force_refresh=True)
            self.assertEqual(res["source"], "mt5_broker (Exness : XAUUSDm)")
            self.assertEqual(res["bars"][0]["c"], 2655.5)
            self.assertEqual(hub.basis_shift, 0.0)


if __name__ == "__main__":
    unittest.main()
