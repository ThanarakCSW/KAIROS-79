#!/usr/bin/env python3
"""Unit and Integration Tests for KAIROS-79 MT5 EA Bridge."""

import asyncio
import json
import os
import unittest

from engine.ea_bridge import EABridgeManager


class TestEABridge(unittest.TestCase):
    def setUp(self):
        self.tmp_config = os.path.join(os.path.dirname(__file__), "test_ea_config.json")
        with open(self.tmp_config, "w", encoding="utf-8") as fh:
            json.dump({"auto_trading": {"enabled": False}}, fh)
        self.bridge = EABridgeManager(config_path=self.tmp_config, heartbeat_timeout_sec=2.0)

    def tearDown(self):
        if os.path.exists(self.tmp_config):
            try:
                os.remove(self.tmp_config)
            except Exception:
                pass

    def test_auto_trading_toggle_and_persistence(self):
        """Verify Master Auto-Trading Switch toggles and persists."""
        self.assertFalse(self.bridge.auto_trading_enabled)

        # Toggle to True
        val = self.bridge.toggle_auto_trading()
        self.assertTrue(val)
        self.assertTrue(self.bridge.auto_trading_enabled)

        # Reload from disk
        bridge2 = EABridgeManager(config_path=self.tmp_config)
        self.assertTrue(bridge2.auto_trading_enabled)

        # Set explicitly to False
        self.bridge.set_auto_trading(False)
        self.assertFalse(self.bridge.auto_trading_enabled)

    def test_heartbeat_telemetry_and_connectivity(self):
        """Verify heartbeat records account info and tracks connection state."""
        self.assertFalse(self.bridge.is_connected)

        hb_payload = {
            "account_id": 881920,
            "broker": "ICMarkets-Live",
            "server": "ICMarkets-Live02",
            "currency": "USD",
            "balance": 10000.0,
            "equity": 10250.50,
            "margin": 150.0,
            "free_margin": 10100.50,
            "margin_level_pct": 6833.67,
            "spread_points": 12.0,
            "symbol": "XAUUSD",
            "open_positions": [
                {
                    "ticket": 991204,
                    "symbol": "XAUUSD",
                    "type": "BUY",
                    "lots": 0.20,
                    "open_price": 2650.0,
                    "sl": 2645.0,
                    "tp": 2660.0,
                    "profit": 250.50,
                    "comment": "KAIROS-79",
                }
            ],
        }

        info = self.bridge.record_heartbeat(hb_payload)
        self.assertTrue(self.bridge.is_connected)
        self.assertEqual(info["account_id"], 881920)
        self.assertEqual(info["equity"], 10250.50)
        self.assertEqual(info["open_positions_count"], 1)
        self.assertEqual(info["total_floating_pnl"], 250.50)

        # Status summary contract
        st = self.bridge.get_status_dict()
        self.assertTrue(st["is_connected"])
        self.assertEqual(st["status_label"], "CONNECTED")
        self.assertEqual(st["broker"], "ICMarkets-Live")

    def test_action_queue_poll_and_execution_tracking(self):
        """Verify action enqueueing, polling, and execution confirmation."""
        # Enqueue OPEN_ORDER
        act1 = self.bridge.enqueue_action(
            action_type="OPEN_ORDER",
            payload={
                "side": "BUY",
                "order_type": "MARKET",
                "symbol": "XAUUSD",
                "lot": 0.15,
                "price": 2655.00,
                "sl": 2650.00,
                "tp": 2665.00,
                "magic": 797979,
            },
            signal_id="sig_test_1",
        )
        self.assertIsNotNone(act1)
        self.assertEqual(len(self.bridge.action_queue), 1)

        # Duplicate signal_id should be ignored
        act_dup = self.bridge.enqueue_action(
            action_type="OPEN_ORDER",
            payload={"side": "BUY", "lot": 0.15},
            signal_id="sig_test_1",
        )
        self.assertIsNone(act_dup, "Duplicate signal_id must not be enqueued")

        # Poll actions
        polled = self.bridge.poll_actions()
        self.assertEqual(len(polled), 1)
        self.assertEqual(polled[0]["action_id"], act1["action_id"])
        self.assertEqual(len(self.bridge.action_queue), 0, "Queue must be empty after poll")

        # Record execution confirmation from MT5 EA
        exec_conf = {
            "action_id": act1["action_id"],
            "signal_id": "sig_test_1",
            "status": "FILLED",
            "ticket": 1283921,
            "fill_price": 2655.05,
            "slippage_points": 5.0,
            "lot": 0.15,
            "sl": 2650.00,
            "tp": 2665.00,
        }
        rec = self.bridge.record_execution(exec_conf)
        self.assertEqual(rec["status"], "FILLED")
        self.assertEqual(rec["ticket"], 1283921)
        self.assertEqual(len(self.bridge.execution_history), 1)

    def test_ea_http_endpoints_roundtrip(self):
        """Verify HTTP endpoints for MT5 EA: heartbeat, poll, execution, and auto-toggle."""
        from app import AssistantServer

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

        async def _run():
            srv = AssistantServer(host="127.0.0.1", port=8080)
            srv.core.compute_full_state()

            # 1. Test POST /api/ea/heartbeat
            hb_data = json.dumps({
                "account_id": 1234567,
                "broker": "ICMarkets-Live",
                "server": "Live01",
                "balance": 5000.0,
                "equity": 5240.0,
                "margin": 120.0,
                "free_margin": 5120.0,
                "spread_points": 11.5,
                "open_positions": [],
            }).encode("utf-8")
            req_hb = (
                f"POST /api/ea/heartbeat HTTP/1.1\r\n"
                f"Host: localhost\r\n"
                f"Content-Length: {len(hb_data)}\r\n"
                f"Content-Type: application/json\r\n\r\n"
            ).encode("utf-8") + hb_data
            r1 = asyncio.StreamReader()
            r1.feed_data(req_hb)
            r1.feed_eof()
            w1 = _MemoryWriter()
            await srv.handle_client(r1, w1)

            resp_hb = json.loads(bytes(w1.buffer).split(b"\r\n\r\n", 1)[1].decode("utf-8"))
            self.assertEqual(resp_hb["status"], "ok")
            self.assertTrue(srv.core.ea_bridge.is_connected)
            self.assertEqual(srv.core.ea_bridge.account_info["account_id"], 1234567)

            # 2. Test POST /api/ea/toggle_auto
            toggle_data = json.dumps({"enabled": True}).encode("utf-8")
            req_toggle = (
                f"POST /api/ea/toggle_auto HTTP/1.1\r\n"
                f"Host: localhost\r\n"
                f"Content-Length: {len(toggle_data)}\r\n"
                f"Content-Type: application/json\r\n\r\n"
            ).encode("utf-8") + toggle_data
            r2 = asyncio.StreamReader()
            r2.feed_data(req_toggle)
            r2.feed_eof()
            w2 = _MemoryWriter()
            await srv.handle_client(r2, w2)

            resp_toggle = json.loads(bytes(w2.buffer).split(b"\r\n\r\n", 1)[1].decode("utf-8"))
            self.assertTrue(resp_toggle["auto_trading_enabled"])
            self.assertTrue(srv.core.ea_bridge.auto_trading_enabled)

            # 3. Test GET /api/ea/poll
            # First manually enqueue an action
            srv.core.ea_bridge.enqueue_action("OPEN_ORDER", {"side": "BUY", "lot": 0.1, "price": 2655.0})
            req_poll = b"GET /api/ea/poll HTTP/1.1\r\nHost: localhost\r\n\r\n"
            r3 = asyncio.StreamReader()
            r3.feed_data(req_poll)
            r3.feed_eof()
            w3 = _MemoryWriter()
            await srv.handle_client(r3, w3)

            resp_poll = json.loads(bytes(w3.buffer).split(b"\r\n\r\n", 1)[1].decode("utf-8"))
            self.assertTrue(resp_poll["has_actions"])
            self.assertEqual(len(resp_poll["actions"]), 1)
            self.assertEqual(resp_poll["actions"][0]["type"], "OPEN_ORDER")

            # 4. Test POST /api/ea/execution
            exec_data = json.dumps({
                "action_id": resp_poll["actions"][0]["action_id"],
                "status": "FILLED",
                "ticket": 778899,
                "fill_price": 2655.10,
                "lot": 0.10,
            }).encode("utf-8")
            req_exec = (
                f"POST /api/ea/execution HTTP/1.1\r\n"
                f"Host: localhost\r\n"
                f"Content-Length: {len(exec_data)}\r\n"
                f"Content-Type: application/json\r\n\r\n"
            ).encode("utf-8") + exec_data
            r4 = asyncio.StreamReader()
            r4.feed_data(req_exec)
            r4.feed_eof()
            w4 = _MemoryWriter()
            await srv.handle_client(r4, w4)

            resp_exec = json.loads(bytes(w4.buffer).split(b"\r\n\r\n", 1)[1].decode("utf-8"))
            self.assertEqual(resp_exec["status"], "ok")
            self.assertEqual(resp_exec["execution"]["ticket"], 778899)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
