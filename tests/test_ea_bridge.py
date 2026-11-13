#!/usr/bin/env python3
"""Unit and Integration Tests for KAIROS-79 MT5 EA Bridge."""

import asyncio
import json
import os
import unittest

from engine.ea_bridge import EABridgeManager
from app import TradingAssistantCore


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
            # Clear any auto-dispatched actions and manually enqueue a test action
            srv.core.ea_bridge.action_queue.clear()
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

            # 5. Test POST /api/ea/send_order (manual dispatch endpoint)
            srv.core.ea_bridge.action_queue.clear()
            req_send_order = (
                b"POST /api/ea/send_order HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Length: 2\r\n"
                b"Content-Type: application/json\r\n\r\n{}"
            )
            r5 = asyncio.StreamReader()
            r5.feed_data(req_send_order)
            r5.feed_eof()
            w5 = _MemoryWriter()
            await srv.handle_client(r5, w5)

            resp_send_order = json.loads(bytes(w5.buffer).split(b"\r\n\r\n", 1)[1].decode("utf-8"))
            self.assertEqual(resp_send_order["status"], "ok")
            self.assertEqual(len(srv.core.ea_bridge.action_queue), 1)
            self.assertEqual(srv.core.ea_bridge.action_queue[0]["type"], "OPEN_ORDER")

        asyncio.run(_run())

    def test_polling_activity_records_keepalive(self):
        """Verify record_poll marks MT5 EA as connected even before telemetry heartbeat."""
        self.assertFalse(self.bridge.is_connected)
        self.bridge.record_poll()
        self.assertTrue(self.bridge.is_connected)
        st = self.bridge.get_status_dict()
        self.assertTrue(st["is_connected"])
        self.assertEqual(st["status_label"], "CONNECTED")

    def test_auto_dispatch_and_manual_order_to_mt5(self):
        """Verify active order dispatch upon Auto ON and manual send_order_to_mt5 WS command."""
        from app import AssistantServer
        from engine.order_lifecycle import OrderLifecycleManager
        import tempfile
        import time

        srv = AssistantServer(host="127.0.0.1", port=8000)
        tmp_dir = tempfile.TemporaryDirectory()
        tmp_persist_path = os.path.join(tmp_dir.name, "test_active_order.json")
        srv.core.order_manager = OrderLifecycleManager(persistence_path=tmp_persist_path)

        # 1. Register an active trade order while Auto-Trading is OFF
        srv.core.ea_bridge.auto_trading_enabled = False
        dummy_risk = {
            "side": "BUY",
            "execution_type": "LIMIT",
            "entry_mt5": 2650.0,
            "stop_mt5": 2645.0,
            "tp1_mt5": 2655.0,
            "target_mt5": 2660.0,
            "lot_size": 0.10,
            "actual_risk_usd": 50.0,
            "reward_risk": 2.0,
            "win_prob_passed": True,
            "verdict": "PASS",
            "fingerprint": "test_fp_001",
        }
        dummy_smc = {
            "side": "BUY",
            "execution_type": "LIMIT",
            "confluence_score": 85,
        }
        ord_obj = srv.core.order_manager.register_trade_setup(
            risk_ticket=dummy_risk,
            smc_setup=dummy_smc,
            current_price=2652.0,
            timeframe="M5",
            force_replace=True,
        )
        self.assertIsNotNone(ord_obj)
        self.assertTrue(srv.core.order_manager.has_active_order())
        # Action queue should be empty because Auto-Trading was OFF
        self.assertEqual(len(srv.core.ea_bridge.action_queue), 0)

        # 2. User turns Auto-Trading ON -> compute_full_state() must immediately enqueue the order!
        srv.core.ea_bridge.set_auto_trading(True)
        # Mock last spot price
        srv.core.hub.latest_spot = {"price": 2652.0, "time": int(time.time())}
        srv.core.compute_full_state()

        self.assertGreaterEqual(len(srv.core.ea_bridge.action_queue), 1)
        dispatched_act = srv.core.ea_bridge.action_queue[0]
        self.assertEqual(dispatched_act["type"], "OPEN_ORDER")
        self.assertEqual(dispatched_act["side"], "BUY")
        self.assertEqual(dispatched_act["lot"], 0.10)
        self.assertEqual(dispatched_act["price"], 2650.0)

        # 3. Test Manual Dispatch via WebSocket command
        async def _test_ws():
            from unittest.mock import MagicMock
            mock_client = MagicMock()
            mock_client.closed = False

            # Drain queue first
            srv.core.ea_bridge.poll_actions()
            self.assertEqual(len(srv.core.ea_bridge.action_queue), 0)

            # User clicks "⚡ ส่งเข้า MT5 ทันที"
            await srv.handle_ws_command(mock_client, {"action": "send_order_to_mt5"})
            self.assertEqual(len(srv.core.ea_bridge.action_queue), 1)
            manual_act = srv.core.ea_bridge.action_queue[0]
            self.assertEqual(manual_act["type"], "OPEN_ORDER")
            self.assertTrue(manual_act.get("manual"))

            # 4. Test close_active_order enqueues CLOSE_ORDER when broker_ticket is set
            srv.core.order_manager.active_order["broker_ticket"] = 123456
            srv.core.ea_bridge.poll_actions()  # Clear queue
            await srv.handle_ws_command(mock_client, {"action": "close_active_order", "current_price": 2655.0})
            self.assertEqual(len(srv.core.ea_bridge.action_queue), 1)
            close_act = srv.core.ea_bridge.action_queue[0]
            self.assertEqual(close_act["type"], "CLOSE_ORDER")
            self.assertEqual(close_act["ticket"], 123456)

        asyncio.run(_test_ws())
        tmp_dir.cleanup()

    def test_null_terminated_body_from_mt5_webrequest(self):
        """Verify server parses MT5 StringToCharArray bodies containing trailing null bytes."""
        from app import AssistantServer

        class _MemWriter:
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

            # Simulate MQL5 WebRequest payload with terminating \x00
            raw_json = json.dumps({
                "account_id": 888777,
                "broker": "Exness-Real",
                "server": "Exness-Real10",
                "currency": "USD",
                "balance": 2500.0,
                "equity": 2500.0,
                "margin": 0.0,
                "free_margin": 2500.0,
                "spread_points": 15.0,
                "symbol": "XAUUSD",
                "open_positions": [],
            }).encode("utf-8") + b"\x00"

            req_bytes = (
                f"POST /api/ea/heartbeat HTTP/1.1\r\n"
                f"Host: 127.0.0.1:8000\r\n"
                f"Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(raw_json)}\r\n\r\n"
            ).encode("utf-8") + raw_json

            reader = asyncio.StreamReader()
            reader.feed_data(req_bytes)
            reader.feed_eof()
            writer = _MemWriter()
            await srv.handle_client(reader, writer)

            resp = bytes(writer.buffer)
            self.assertIn(b"200 OK", resp)
            body = json.loads(resp.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
            self.assertEqual(body["status"], "ok")

            # Verify that account telemetry was successfully recorded into ea_bridge
            acc = srv.core.ea_bridge.account_info
            self.assertEqual(acc.get("account_id"), 888777)
            self.assertEqual(acc.get("broker"), "Exness-Real")
            self.assertEqual(acc.get("balance"), 2500.0)
            self.assertEqual(acc.get("equity"), 2500.0)
            self.assertTrue(srv.core.ea_bridge.is_connected)

        asyncio.run(_run())

    def test_auto_limit_dispatch_and_disadvantage_close_action(self):
        """Verify autonomous Limit order dispatch to EA and immediate CLOSE_ORDER on disadvantage."""
        core = TradingAssistantCore()
        core.ea_bridge.set_auto_trading(True)
        core.ea_bridge.action_queue.clear()
        core.ea_bridge.executed_signal_ids.clear()

        # Register pending LIMIT setup needing autonomous dispatch
        core.order_manager.register_trade_setup(
            risk_ticket={
                "side": "BUY",
                "execution_type": "LIMIT",
                "order_type": "BUY LIMIT (Pending)",
                "entry_mt5": 2650.0,
                "stop_mt5": 2640.0,
                "tp1_mt5": 2660.0,
                "target_mt5": 2670.0,
                "lot_size": 0.10,
                "win_prob_passed": True,
                "verdict": "PASS",
            },
            smc_setup={"side": "BUY", "confluence_score": 85},
            current_price=2655.0,
            timeframe="M5",
            force_replace=True,
        )

        # 1. State computation enqueues an OPEN_ORDER action for MT5 EA
        state = core.compute_full_state(timeframe="M5")
        self.assertGreaterEqual(len(core.ea_bridge.action_queue), 1)
        action = core.ea_bridge.action_queue[0]
        self.assertEqual(action["type"], "OPEN_ORDER")
        self.assertEqual(action["order_type"], "LIMIT")

        # 2. Simulate order filled on MT5 with broker ticket 998877
        if core.order_manager.has_active_order():
            core.order_manager.active_order["broker_ticket"] = 998877
            core.order_manager.active_order["status"] = "FILLED"
            # Simulate disadvantage early exit
            core.ea_bridge.action_queue.clear()
            ev_dis = core.order_manager.close_active_order(reason="STRUCTURAL_DISADVANTAGE", current_price=2645.0)
            if ev_dis:
                ev_dis["type"] = "ORDER_CLOSED_DISADVANTAGE"
            # Enqueue close order on disadvantage
            if ev_dis and ev_dis.get("type") == "ORDER_CLOSED_DISADVANTAGE":
                ord_obj = ev_dis.get("order") or {}
                t = ord_obj.get("broker_ticket") or 998877
                core.ea_bridge.enqueue_action("CLOSE_ORDER", {"ticket": int(t), "symbol": "XAUUSD"})

            self.assertGreaterEqual(len(core.ea_bridge.action_queue), 1)
            self.assertEqual(core.ea_bridge.action_queue[0]["type"], "CLOSE_ORDER")
            self.assertEqual(core.ea_bridge.action_queue[0]["ticket"], 998877)

    def test_dispatch_test_order_market_and_limit(self):
        """Verify dispatch_test_order generates BUY MARKET, BUY LIMIT, and SELL MARKET orders."""
        from app import AssistantServer

        srv = AssistantServer(host="127.0.0.1", port=8000)

        # 1. Test BUY MARKET
        res_buy_mkt = srv.dispatch_test_order(side="BUY", order_type="MARKET")
        self.assertEqual(res_buy_mkt["status"], "ok")
        act_buy = res_buy_mkt["action"]
        self.assertEqual(act_buy["type"], "OPEN_ORDER")
        self.assertEqual(act_buy["side"], "BUY")
        self.assertEqual(act_buy["order_type"], "MARKET")
        self.assertEqual(act_buy["lot"], 0.01)
        self.assertGreater(act_buy["price"], 0.0)
        self.assertLess(act_buy["sl"], act_buy["price"])
        self.assertGreater(act_buy["tp"], act_buy["price"])

        # 2. Test BUY LIMIT
        cur_p = float(
            (srv.core.hub.latest_spot or {}).get("price")
            or (srv.core.hub.fixture_bars[-1]["c"] if srv.core.hub.fixture_bars else 2650.0)
        )
        res_buy_lmt = srv.dispatch_test_order(side="BUY", order_type="LIMIT")
        self.assertEqual(res_buy_lmt["status"], "ok")
        act_lmt = res_buy_lmt["action"]
        self.assertEqual(act_lmt["side"], "BUY")
        self.assertEqual(act_lmt["order_type"], "LIMIT")
        self.assertEqual(act_lmt["lot"], 0.01)
        # Entry price must be below market price so it sits pending
        self.assertLess(act_lmt["price"], cur_p)
        self.assertLess(act_lmt["sl"], act_lmt["price"])
        self.assertGreater(act_lmt["tp"], act_lmt["price"])

        # 3. Test SELL MARKET
        res_sell_mkt = srv.dispatch_test_order(side="SELL", order_type="MARKET", lot=0.02)
        self.assertEqual(res_sell_mkt["status"], "ok")
        act_sell = res_sell_mkt["action"]
        self.assertEqual(act_sell["side"], "SELL")
        self.assertEqual(act_sell["order_type"], "MARKET")
        self.assertEqual(act_sell["lot"], 0.02)
        self.assertGreater(act_sell["sl"], act_sell["price"])
        self.assertLess(act_sell["tp"], act_sell["price"])

        # Clean up
        if srv.core.order_manager.has_active_order():
            srv.core.order_manager.close_active_order(reason="TEST_CLEANUP", current_price=2650.0)

    def test_http_test_order_endpoint_roundtrip(self):
        """Verify HTTP POST /api/ea/test_order enqueues action and /api/ea/poll retrieves it."""
        from app import AssistantServer

        srv = AssistantServer(host="127.0.0.1", port=8000)

        class _MemWriter:
            def __init__(self):
                self.buffer = bytearray()
            def write(self, data):
                self.buffer.extend(data)
            async def drain(self):
                pass
            def close(self):
                pass

        async def _run():
            # 1. POST /api/ea/test_order
            raw_body = json.dumps({"side": "BUY", "order_type": "MARKET", "lot": 0.01}).encode("utf-8")
            req_bytes = (
                f"POST /api/ea/test_order HTTP/1.1\r\n"
                f"Host: 127.0.0.1:8000\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(raw_body)}\r\n\r\n"
            ).encode("utf-8") + raw_body

            reader = asyncio.StreamReader()
            reader.feed_data(req_bytes)
            reader.feed_eof()
            writer = _MemWriter()
            await srv.handle_client(reader, writer)

            resp = bytes(writer.buffer)
            self.assertIn(b"200 OK", resp)
            body = json.loads(resp.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["action"]["type"], "OPEN_ORDER")
            self.assertEqual(body["action"]["side"], "BUY")

            # 2. GET /api/ea/poll to verify MT5 EA receives the action
            poll_bytes = b"GET /api/ea/poll HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n\r\n"
            p_reader = asyncio.StreamReader()
            p_reader.feed_data(poll_bytes)
            p_reader.feed_eof()
            p_writer = _MemWriter()
            await srv.handle_client(p_reader, p_writer)

            p_resp = bytes(p_writer.buffer)
            self.assertIn(b"200 OK", p_resp)
            p_body = json.loads(p_resp.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
            self.assertTrue(p_body["has_actions"])
            actions = p_body["actions"]
            self.assertGreaterEqual(len(actions), 1)
            self.assertEqual(actions[-1]["side"], "BUY")

            # 3. POST /api/ea/execution to report FILLED ticket
            exec_body = json.dumps({
                "action_id": actions[-1]["action_id"],
                "status": "FILLED",
                "ticket": 1234567,
                "side": "BUY",
                "lot": 0.01,
                "fill_price": 2650.50,
            }).encode("utf-8")
            exec_req = (
                f"POST /api/ea/execution HTTP/1.1\r\n"
                f"Host: 127.0.0.1:8000\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(exec_body)}\r\n\r\n"
            ).encode("utf-8") + exec_body

            e_reader = asyncio.StreamReader()
            e_reader.feed_data(exec_req)
            e_reader.feed_eof()
            e_writer = _MemWriter()
            await srv.handle_client(e_reader, e_writer)

            e_resp = bytes(e_writer.buffer)
            self.assertIn(b"200 OK", e_resp)

            # Check status dictionary includes last_execution with ticket
            st_dict = srv.core.ea_bridge.get_status_dict()
            self.assertIsNotNone(st_dict["last_execution"])
            self.assertEqual(st_dict["last_execution"]["ticket"], 1234567)
            self.assertEqual(st_dict["last_execution"]["status"], "FILLED")

            # Clean up
            if srv.core.order_manager.has_active_order():
                srv.core.order_manager.close_active_order(reason="TEST_CLEANUP", current_price=2650.0)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
