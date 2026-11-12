import asyncio
import base64
import json
import struct
import unittest

from advisor.ai_evaluator import _validate_gemini_trade_decision
from app import AssistantServer, TradingAssistantCore
from engine.data_feed import _tv_pack_msg, _tv_unpack_msgs


class _MemoryWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes):
        self.buffer.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class TestAppIntegration(unittest.TestCase):
    def test_live_price_ingestion_and_candle_rollover(self):
        core = TradingAssistantCore()
        core.hub.cached_bars_by_tf["M5"] = [
            {"t": 1762862400, "o": 2650.0, "h": 2651.0, "l": 2649.5, "c": 2650.5, "v": 100}
        ]
        # Same M5 bar slot (within 300s) -> updates high/low/close, new_bar_closed=False
        res1 = core.hub.ingest_live_price(
            timeframe="M5",
            price=2652.30,
            bid=2652.20,
            ask=2652.40,
            ts=1762862520,
        )
        self.assertFalse(res1["new_bar_closed"])
        self.assertEqual(res1["bar"]["c"], 2652.30)
        self.assertEqual(res1["bar"]["h"], 2652.30)
        self.assertEqual(res1["spread_points"], 20)

        # Next M5 bar slot (+300s) -> closes old bar and opens new bar, new_bar_closed=True
        res2 = core.hub.ingest_live_price(
            timeframe="M5",
            price=2653.10,
            bid=2653.00,
            ask=2653.25,
            ts=1762862705,
        )
        self.assertTrue(res2["new_bar_closed"])
        self.assertEqual(res2["bar"]["t"], 1762862700)
        self.assertEqual(res2["bar"]["o"], 2653.10)

        # Verify TradingView Socket.IO ~m~ packet encoding/decoding roundtrip
        p1 = _tv_pack_msg("chart_create_session", ["cs_test123", ""])
        p2 = _tv_pack_msg("quote_add_symbols", ["qs_test123", "OANDA:XAUUSD"])
        unpacked = _tv_unpack_msgs(p1 + "~m~4~m~~h~1" + p2)
        self.assertEqual(len(unpacked), 3)
        self.assertEqual(unpacked[1], "~h~1")
        self.assertEqual(json.loads(unpacked[2])["p"][1], "OANDA:XAUUSD")

    def test_full_pipeline_state_and_ai_advisory(self):
        core = TradingAssistantCore()
        state = core.compute_full_state(timeframe="M5", force_ai=True)

        self.assertEqual(state["type"], "full_state")
        self.assertEqual(state["symbol"], "XAUUSD")
        self.assertEqual(state["simulation"]["replays"], 15000)
        self.assertGreater(len(state["bars"]), 50)
        self.assertIn("structure", state)
        self.assertIn("market_flow", state)
        self.assertIn("mtf", state)
        self.assertIn("macro", state)
        self.assertIn("signal_tracker", state)
        self.assertIn("smc", state)
        self.assertIn("simulation", state)
        self.assertIn("risk_ticket", state)
        self.assertIn("ai_advisory", state)

        # Verify Volume & Market Flow causal narrative
        mf = state["market_flow"]
        self.assertGreater(mf["rvol"], 0.0)
        self.assertAlmostEqual(mf["buy_volume_pct"] + mf["sell_volume_pct"], 100.0, places=0)
        self.assertIn(mf["flow_type"], ("MARKET_SLOWDOWN", "PULLBACK_SLOWDOWN", "TREND_EXPANSION"))
        self.assertIn("เนื่องจาก", mf["headline_th"])
        self.assertGreaterEqual(len(mf["causes_th"]), 1)

        # Verify 4 new Institutional Upgrade components
        inst = state["structure"]["institutional_levels"]
        self.assertGreater(inst["pdh"], inst["pdl"])
        self.assertGreater(inst["vwap"], 0)
        self.assertGreater(inst["poc"], 0)

        rsi_div = state["structure"]["rsi_divergence"]
        self.assertGreater(rsi_div["rsi14"], 0)

        macro = state["macro"]
        self.assertGreater(macro["dxy_price"], 50)
        self.assertGreater(macro["us10y_yield"], 0)

        tracker = state["signal_tracker"]
        self.assertGreaterEqual(tracker["total_signals"], 1)
        self.assertIsNotNone(state["risk_ticket"]["tp1_mt5"])

        ai = state["ai_advisory"]
        self.assertTrue(ai["headline"])
        self.assertIn("scenario_a", ai["scenarios"])
        self.assertIn("scenario_b", ai["scenarios"])
        self.assertIn("scenario_c", ai["scenarios"])

        # Verify Gemini Trade Decision validation
        gemini_setup = _validate_gemini_trade_decision(
            decision={
                "side": "BUY",
                "status": "ACTIVE_IN_ZONE",
                "entry": 2657.00,
                "stop": 2653.50,
                "target": 2665.50,
                "confluence_score": 88,
                "confluence_factors": ["Gemini OB + FVG", "Sweep Confirmation"],
            },
            fallback_setup=state["smc"]["trade_setup"],
            atr_val=2.5,
            spread_usd=0.25,
        )
        self.assertIsNotNone(gemini_setup)
        self.assertEqual(gemini_setup["decided_by"], "gemini_ai")
        self.assertEqual(gemini_setup["confluence_score"], 88)
        self.assertEqual(gemini_setup["entry"], 2657.00)

        # Verify Scalping Mode + M1 Timeframe + >50% Win Probability Continuous Scanner
        scalp_state = core.compute_full_state(
            timeframe="M1", force_ai=False, trading_style="scalping"
        )
        self.assertEqual(scalp_state["timeframe"], "M1")
        self.assertEqual(scalp_state["trading_style"], "scalping")
        self.assertIn("scalping", scalp_state["structure"])
        scalp_info = scalp_state["structure"]["scalping"]
        self.assertIn("ema9", scalp_info)
        self.assertIn("stoch_rsi_k", scalp_info)
        self.assertIn("vwap_upper_1_5", scalp_info)
        self.assertEqual(scalp_state["risk_ticket"]["min_win_prob_pct"], 50.0)
        win_pct = scalp_state["simulation"]["probabilities"]["win_pct"]
        if win_pct > 50.0:
            self.assertTrue(scalp_state["risk_ticket"]["win_prob_passed"])
            self.assertIn(scalp_state["risk_ticket"]["recommended_action"], ("BUY", "SELL"))
        else:
            self.assertFalse(scalp_state["risk_ticket"]["win_prob_passed"])
            self.assertEqual(scalp_state["risk_ticket"]["recommended_action"], "WAIT")

    def test_http_and_websocket_handler_roundtrip(self):
        async def _run():
            srv = AssistantServer(host="127.0.0.1", port=8080)
            srv.core.compute_full_state()

            # 1. Test GET /api/state?tf=M15
            reader1 = asyncio.StreamReader()
            reader1.feed_data(b"GET /api/state?tf=M15 HTTP/1.1\r\nHost: localhost\r\n\r\n")
            reader1.feed_eof()
            writer1 = _MemoryWriter()
            await srv.handle_client(reader1, writer1)

            body = bytes(writer1.buffer).split(b"\r\n\r\n", 1)[1]
            doc = json.loads(body.decode("utf-8"))
            self.assertEqual(doc["timeframe"], "M15")

            # 2. Test WebSocket /ws upgrade & initial full_state push
            reader2 = asyncio.StreamReader()
            key = base64.b64encode(b"1234567890123456").decode("ascii")
            ws_req = (
                f"GET /ws HTTP/1.1\r\n"
                f"Host: localhost\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n\r\n"
            ).encode("utf-8")
            # Send handshake followed by WebSocket Close frame (0x88, 0x00)
            reader2.feed_data(ws_req + b"\x88\x00")
            reader2.feed_eof()
            writer2 = _MemoryWriter()
            await srv.handle_client(reader2, writer2)

            raw_ws = bytes(writer2.buffer)
            self.assertIn(b"101 Switching Protocols", raw_ws)
            frame_bytes = raw_ws.split(b"\r\n\r\n", 1)[1]
            self.assertEqual(frame_bytes[0], 0x81)
            payload_len = frame_bytes[1]
            if payload_len == 126:
                real_len = struct.unpack("!H", frame_bytes[2:4])[0]
                ws_json = json.loads(frame_bytes[4 : 4 + real_len].decode("utf-8"))
            else:
                ws_json = json.loads(frame_bytes[2 : 2 + payload_len].decode("utf-8"))
            self.assertEqual(ws_json["type"], "full_state")
            self.assertEqual(ws_json["symbol"], "XAUUSD")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
