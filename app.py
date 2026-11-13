#!/usr/bin/env python3
"""KAIROS-79 (K79): Precision Gold Timing & Risk System — Web & WebSocket Server (app.py).

Zero-extra-dependency Async HTTP + RFC 6455 WebSocket Server:
  - Runs directly with `python3 app.py` (uses Python 3 standard library asyncio)
  - Serves the interactive Web UI (`web/index.html`, `web/style.css`, `web/app.js`)
  - Connects to Real-Time Free Gold Feeds in Live Mode:
      1) Swissquote Institutional Spot XAU/USD Bid/Ask/Spread feed (every 2s)
      2) Binance Live Trade WebSocket (`wss://stream.binance.com:9443/ws/paxgusdt@trade`)
      3) Multi-timeframe OHLCV candles (M5, M15, H1, H4) aligned to Spot XAU/USD
  - Automatically re-evaluates SMC + Monte Carlo + AI Risk Advisor when a candle closes
"""

import argparse
import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import signal
import struct
import subprocess
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Set

from advisor.ai_evaluator import (
    evaluate_chart_with_gemini_or_fallback,
    finalize_advisory,
)
from engine.data_feed import (
    MarketDataHub,
    fetch_live_spot_xauusd,
    stream_binance_gold_ws,
    stream_tradingview_gold_ws,
)
from engine.news_feed import evaluate_news_window
from engine.order_lifecycle import OrderLifecycleManager
from engine.risk_gate import evaluate_risk_and_size
from engine.signal_tracker import evaluate_signal_performance
from engine.simulate import optimize_setup_for_win_prob, run_simulation
from engine.smc import analyze_smc
from engine.structure import (
    analyze_multi_timeframe,
    analyze_structure,
    build_market_flow_narrative,
)

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
WEB_DIR = os.path.join(ROOT_DIR, "web")
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_config(cfg: Dict[str, Any]) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)


class TradingAssistantCore:
    """Coordinates MarketDataHub, Deterministic Engines, and Automated AI Advisor."""

    def __init__(self) -> None:
        self.config = load_config()
        self.hub = MarketDataHub()
        self.order_manager = OrderLifecycleManager()
        self.timeframe = str(self.config.get("default_timeframe") or "M5").upper()
        self.trading_style = str(self.config.get("trading_style") or "intraday").lower()
        if self.trading_style not in ("intraday", "scalping"):
            self.trading_style = "intraday"
        self.last_state: Optional[Dict[str, Any]] = None
        self.last_alert_fingerprint: Optional[str] = None

    def compute_full_state(
        self,
        timeframe: Optional[str] = None,
        force_refresh: bool = False,
        force_ai: bool = False,
        trading_style: Optional[str] = None,
    ) -> Dict[str, Any]:
        tf = (timeframe or self.timeframe).upper()
        self.timeframe = tf
        if trading_style in ("intraday", "scalping"):
            self.trading_style = trading_style
            self.config["trading_style"] = trading_style

        feed = self.hub.get_bars(tf, force_refresh=force_refresh)
        raw_bars = feed["bars"]
        offset = float(self.config.get("broker_price_offset") or 0.0)
        spread_pts = float(self.config.get("broker_spread_points") or 25.0)
        point_size = float(self.config.get("point_size") or 0.01)
        spread_usd = round(spread_pts * point_size, 2)

        # Apply broker offset to displayed chart bars so the chart & zones match MT5 directly
        chart_bars = [
            {
                "t": int(b["t"]),
                "o": round(float(b["o"]) + offset, 2),
                "h": round(float(b["h"]) + offset, 2),
                "l": round(float(b["l"]) + offset, 2),
                "c": round(float(b["c"]) + offset, 2),
                "v": int(b.get("v") or 0),
            }
            for b in raw_bars
        ]

        mtf_bars = self.hub.get_mtf_bars(primary_tf=tf, primary_bars=chart_bars, offset_usd=offset)
        structure = analyze_structure(chart_bars, timeframe=tf, mtf_bars_by_tf=mtf_bars)
        mtf = analyze_multi_timeframe(mtf_bars, primary_tf=tf, current_price=structure["current_price"])
        macro = self.hub.get_macro_correlation(chart_bars, timeframe=tf)
        smc = analyze_smc(
            chart_bars,
            timeframe=tf,
            spread_usd=spread_usd,
            structure_res=structure,
            mtf_res=mtf,
            macro_res=macro,
            trading_style=self.trading_style,
        )
        replays_cnt = max(15000, int(self.config.get("simulation_replays") or 15000))
        self.config["simulation_replays"] = replays_cnt
        bars_ahead_cnt = 18 if self.trading_style == "scalping" else int(self.config.get("simulation_bars_ahead") or 36)
        sim_seed = int(self.config.get("simulation_seed") or 19)
        min_win_prob = float(self.config.get("min_win_prob_pct") or 50.0)
        self.config["min_win_prob_pct"] = min_win_prob

        opt_setup, sim = optimize_setup_for_win_prob(
            bars=chart_bars,
            trade_setup=smc["trade_setup"],
            structure_res=structure,
            replays=replays_cnt,
            bars_ahead=bars_ahead_cnt,
            seed=sim_seed,
            min_win_prob_pct=min_win_prob,
        )
        smc["trade_setup"] = opt_setup
        news = evaluate_news_window(
            blackout_minutes=int(self.config.get("news_blackout_minutes") or 30)
        )
        market_flow = build_market_flow_narrative(
            structure=structure,
            mtf=mtf,
            macro=macro,
            smc=smc,
            news=news,
        )
        structure["volume_flow"] = market_flow

        # Let Gemini analyze the full chart (raw bars + MTF + Macro + SMC + 15,000-path baseline) and decide the setup
        chosen_setup, gemini_raw = evaluate_chart_with_gemini_or_fallback(
            bars=chart_bars,
            structure=structure,
            smc=smc,
            baseline_sim=sim,
            news=news,
            config=self.config,
            spread_usd=spread_usd,
            force_call=force_ai,
            mtf=mtf,
            macro=macro,
        )
        if chosen_setup is not smc["trade_setup"]:
            chosen_setup["trading_style"] = self.trading_style
            opt_gemini_setup, sim = optimize_setup_for_win_prob(
                bars=chart_bars,
                trade_setup=chosen_setup,
                structure_res=structure,
                replays=replays_cnt,
                bars_ahead=bars_ahead_cnt,
                seed=sim_seed,
                min_win_prob_pct=min_win_prob,
            )
            smc["trade_setup"] = opt_gemini_setup

        cfg_for_risk = dict(
            self.config,
            broker_price_offset=0.0,
            trading_style=self.trading_style,
            min_win_prob_pct=min_win_prob,
        )
        risk_ticket = evaluate_risk_and_size(
            trade_setup=smc["trade_setup"],
            simulation=sim,
            news_status=news,
            config=cfg_for_risk,
        )
        risk_ticket["broker_offset_usd"] = round(offset, 2)

        cur_p = float(structure["current_price"])
        last_b = chart_bars[-1] if chart_bars else {}
        cur_hi = float(last_b.get("h", cur_p))
        cur_lo = float(last_b.get("l", cur_p))
        cur_ts = int(last_b.get("t", time.time()))

        order_event = self.order_manager.update_price_tick(
            current_price=cur_p,
            high=cur_hi,
            low=cur_lo,
            ts=cur_ts,
        )

        min_conf = int((self.config.get("ai") or {}).get("auto_trigger_min_confluence") or 55)
        if self.order_manager.has_active_order():
            active_ord = self.order_manager.get_active_order_dict(cur_p)
            if active_ord:
                risk_ticket["side"] = active_ord["side"]
                risk_ticket["execution_type"] = active_ord.get("execution_type", "LIMIT")
                risk_ticket["order_type"] = active_ord.get("order_type", f"{active_ord['side']} LIMIT")
                risk_ticket["entry_mt5"] = active_ord["entry_mt5"]
                risk_ticket["stop_mt5"] = active_ord["stop_mt5"]
                risk_ticket["tp1_mt5"] = active_ord["tp1_mt5"]
                risk_ticket["target_mt5"] = active_ord["target_mt5"]
                risk_ticket["breakeven_sl_mt5"] = active_ord.get("breakeven_sl_mt5", active_ord["entry_mt5"])
                risk_ticket["lot_size"] = active_ord["lot_size"]
                risk_ticket["actual_risk_usd"] = active_ord["actual_risk_usd"]
                risk_ticket["potential_profit_usd"] = active_ord["potential_profit_usd"]
                risk_ticket["reward_risk"] = active_ord["reward_risk"]
                risk_ticket["status"] = active_ord["status"]
                risk_ticket["win_prob_passed"] = True
                risk_ticket["floating_pnl_usd"] = active_ord.get("floating_pnl_usd", 0.0)
                risk_ticket["floating_pnl_r"] = active_ord.get("floating_pnl_r", 0.0)
                risk_ticket["management_note_th"] = active_ord.get("management_note_th", "")
                risk_ticket["active_order"] = active_ord
                risk_ticket["verdict"] = "ACTIVE_ORDER" if active_ord["status"] == "FILLED" else "PENDING_ORDER"
                risk_ticket["badge_th"] = (
                    f"🚀 ถือครองออเดอร์ใน MT5 ({active_ord['side']} @ {active_ord['entry_mt5']:.2f})"
                    if active_ord["status"] == "FILLED"
                    else f"⏳ รอเกี่ยวคำสั่ง {active_ord['order_type']} @ {active_ord['entry_mt5']:.2f}"
                )
        else:
            if (
                risk_ticket.get("win_prob_passed", False)
                and risk_ticket.get("verdict") in ("PASS", "CAUTION")
                and smc["trade_setup"].get("confluence_score", 0) >= min_conf
            ):
                self.order_manager.register_trade_setup(
                    risk_ticket=risk_ticket,
                    smc_setup=smc["trade_setup"],
                    current_price=cur_p,
                    timeframe=tf,
                    trading_style=self.trading_style,
                )
                risk_ticket["active_order"] = self.order_manager.get_active_order_dict(cur_p)
                order_event = self.order_manager.last_event
            else:
                risk_ticket["active_order"] = None

        signal_tracker = evaluate_signal_performance(
            bars=chart_bars,
            timeframe=tf,
            current_ticket=risk_ticket,
            current_smc=smc,
            spread_usd=spread_usd,
        )

        ai_advisory = finalize_advisory(
            structure=structure,
            smc=smc,
            simulation=sim,
            risk_ticket=risk_ticket,
            news=news,
            config=self.config,
            gemini_raw=gemini_raw,
            mtf=mtf,
            macro=macro,
        )

        fp = risk_ticket["fingerprint"]
        is_new_signal_alert = False
        min_conf = int((self.config.get("ai") or {}).get("auto_trigger_min_confluence") or 55)
        if order_event and order_event.get("type") in (
            "NEW_ORDER_REGISTERED",
            "ORDER_FILLED",
            "TP1_REACHED",
            "TRADE_CLOSED_TP2",
            "TRADE_CLOSED_BREAKEVEN",
            "TRADE_CLOSED_SL",
        ):
            is_new_signal_alert = True
        elif (
            force_ai
            or (
                fp != self.last_alert_fingerprint
                and smc["trade_setup"]["confluence_score"] >= min_conf
                and risk_ticket.get("win_prob_passed", False)
                and risk_ticket["verdict"] in ("PASS", "CAUTION", "PENDING_ORDER", "ACTIVE_ORDER")
            )
        ):
            self.last_alert_fingerprint = fp
            is_new_signal_alert = True

        live_quote = feed.get("live_quote")
        state = {
            "type": "full_state",
            "symbol": "XAUUSD",
            "timeframe": tf,
            "trading_style": self.trading_style,
            "data_source": feed["source"],
            "live_quote": live_quote,
            "replay_mode": self.hub.replay_mode,
            "replay_cursor": self.hub.replay_cursor,
            "replay_total": len(self.hub.fixture_bars),
            "is_new_signal_alert": is_new_signal_alert,
            "active_order": self.order_manager.get_active_order_dict(cur_p),
            "order_event": order_event,
            "order_history": self.order_manager.order_history[-10:],
            "bars": chart_bars,
            "structure": structure,
            "market_flow": market_flow,
            "mtf": mtf,
            "macro": macro,
            "signal_tracker": signal_tracker,
            "smc": smc,
            "simulation": {
                k: v for k, v in sim.items() if k != "sample_paths"
            },
            "risk_ticket": risk_ticket,
            "news": news,
            "ai_advisory": ai_advisory,
            "config": {
                "account_balance": self.config.get("account_balance", 5000.0),
                "leverage": float(self.config.get("leverage") or 100.0),
                "risk_per_trade_pct": self.config.get("risk_per_trade_pct", 1.0),
                "min_reward_risk": self.config.get("min_reward_risk", 1.5),
                "broker_price_offset": offset,
                "broker_spread_points": int(spread_pts),
                "has_gemini_key": bool(
                    (self.config.get("ai") or {}).get("api_key")
                    or os.environ.get("GEMINI_API_KEY")
                ),
            },
        }
        self.last_state = state
        return state

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        for key in (
            "account_balance",
            "leverage",
            "risk_per_trade_pct",
            "min_reward_risk",
            "broker_price_offset",
            "broker_spread_points",
        ):
            if key in payload and payload[key] is not None:
                self.config[key] = float(payload[key])
        self.config["simulation_replays"] = max(
            15000, int(self.config.get("simulation_replays") or 15000)
        )
        if "gemini_api_key" in payload:
            raw_key = str(payload["gemini_api_key"]).strip()
            if " " not in raw_key and not raw_key.startswith("[MT5"):
                self.config.setdefault("ai", {})["api_key"] = raw_key
        save_config(self.config)
        return self.compute_full_state(force_ai=True)


# ---------------------------------------------------------------------------
# Pure-Python Async HTTP + RFC 6455 WebSocket Server
# ---------------------------------------------------------------------------

class WebSocketClient:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.closed = False

    async def send_json(self, data: Dict[str, Any]) -> None:
        if self.closed:
            return
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        length = len(raw)
        header = bytearray([0x81])  # FIN + text frame
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        try:
            self.writer.write(bytes(header) + raw)
            await self.writer.drain()
        except Exception:
            self.closed = True


async def read_ws_frame(reader: asyncio.StreamReader) -> Optional[str]:
    try:
        head = await reader.readexactly(2)
    except Exception:
        return None
    opcode = head[0] & 0x0F
    if opcode == 0x8:  # Close frame
        return None
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    try:
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
        mask_key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length)
    except Exception:
        return None
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    if opcode == 0x9:  # Ping
        return "__PING__"
    return payload.decode("utf-8", "replace")


class AssistantServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.host = host
        self.port = port
        self.core = TradingAssistantCore()
        self.clients: Set[WebSocketClient] = set()
        self.auto_replay = False
        self._last_ws_broadcast_ts = 0.0

    async def broadcast(self, message: Dict[str, Any]) -> None:
        dead: List[WebSocketClient] = []
        for c in list(self.clients):
            await c.send_json(message)
            if c.closed:
                dead.append(c)
        for c in dead:
            self.clients.discard(c)

    async def tv_ws_loop(self) -> None:
        """Primary Live Stream: real-time XAUUSD ticks directly from TradingView WebSocket (`OANDA:XAUUSD`)."""
        async def on_tv_tick(
            price: float,
            bid: Optional[float],
            ask: Optional[float],
            ts: int,
        ) -> None:
            if self.core.hub.replay_mode or not self.clients:
                return
            now = time.time()
            if now - self._last_ws_broadcast_ts < 0.25:
                return
            self._last_ws_broadcast_ts = now

            offset = float(self.core.config.get("broker_price_offset") or 0.0)
            ingested = self.core.hub.ingest_live_price(
                timeframe=self.core.timeframe,
                price=price,
                bid=bid,
                ask=ask,
                ts=ts,
                offset_usd=offset,
            )
            if not ingested:
                return
            b = ingested["bar"]
            ev = self.core.order_manager.update_price_tick(
                current_price=float(b["c"]),
                high=float(b["h"]),
                low=float(b["l"]),
                ts=int(b["t"]),
            )
            if ingested["new_bar_closed"] or ev:
                state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
                await self.broadcast(state)
            else:
                await self.broadcast(
                    {
                        "type": "tick",
                        "timeframe": self.core.timeframe,
                        "bar": ingested["bar"],
                        "bid": ingested["bid"],
                        "ask": ingested["ask"],
                        "spread_points": ingested["spread_points"],
                        "active_order": self.core.order_manager.get_active_order_dict(float(b["c"])),
                        "source": "tradingview_ws (OANDA:XAUUSD)",
                    }
                )

        await stream_tradingview_gold_ws(on_tv_tick)

    async def binance_ws_loop(self) -> None:
        """Fallback Stream: real-time gold trades from wss://stream.binance.com:9443/ws/paxgusdt@trade when TradingView WS is idle."""
        async def on_trade(trade_price: float, trade_ts: int) -> None:
            if self.core.hub.replay_mode or not self.clients:
                return
            # Only use Binance fallback stream if TradingView primary stream hasn't pushed recently
            if str(self.core.hub.source).startswith("tradingview_ws"):
                return
            now = time.time()
            # Throttle WebSocket push to at most 4 frames/sec for smooth browser rendering
            if now - self._last_ws_broadcast_ts < 0.25:
                return
            self._last_ws_broadcast_ts = now

            spot_equivalent = trade_price + self.core.hub.basis_shift
            offset = float(self.core.config.get("broker_price_offset") or 0.0)
            ingested = self.core.hub.ingest_live_price(
                timeframe=self.core.timeframe,
                price=spot_equivalent,
                ts=trade_ts,
                offset_usd=offset,
            )
            if not ingested:
                return
            b = ingested["bar"]
            ev = self.core.order_manager.update_price_tick(
                current_price=float(b["c"]),
                high=float(b["h"]),
                low=float(b["l"]),
                ts=int(b["t"]),
            )
            if ingested["new_bar_closed"] or ev:
                state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
                await self.broadcast(state)
            else:
                await self.broadcast(
                    {
                        "type": "tick",
                        "timeframe": self.core.timeframe,
                        "bar": ingested["bar"],
                        "active_order": self.core.order_manager.get_active_order_dict(float(b["c"])),
                        "source": ingested["source"],
                    }
                )

        await stream_binance_gold_ws(on_trade)

    async def tick_loop(self) -> None:
        """Poll real Spot XAU/USD Bid/Ask every 2.0s in Live Mode (or step Replay in Replay Mode)."""
        loop_counter = 0
        while True:
            await asyncio.sleep(2.0)
            if not self.clients:
                continue
            loop_counter += 1

            # 1. Replay Mode handling
            if self.core.hub.replay_mode:
                if self.auto_replay and loop_counter % 2 == 0:
                    self.core.hub.step_replay(1)
                    state = await asyncio.to_thread(self.core.compute_full_state)
                    await self.broadcast(state)
                elif self.core.last_state and self.core.last_state.get("bars"):
                    last_bar = self.core.last_state["bars"][-1]
                    tick = self.core.hub.simulate_live_tick(last_bar, offset_usd=0.0)
                    await self.broadcast(
                        {
                            "type": "tick",
                            "timeframe": self.core.timeframe,
                            "bar": tick,
                            "source": "offline_replay",
                        }
                    )
                continue

            # 2. Live Mode: if TradingView WS is active, periodically refresh full state every 20s;
            #    otherwise use Swissquote / Binance fallback quote polling
            if str(self.core.hub.source).startswith("tradingview_ws"):
                if loop_counter % 10 == 0:
                    state = await asyncio.to_thread(
                        self.core.compute_full_state, None, True, False
                    )
                    await self.broadcast(state)
                continue

            spot = await asyncio.to_thread(fetch_live_spot_xauusd)
            if spot:
                self.core.hub.latest_spot_quote = spot
                offset = float(self.core.config.get("broker_price_offset") or 0.0)
                ingested = self.core.hub.ingest_live_price(
                    timeframe=self.core.timeframe,
                    price=spot["price"],
                    bid=spot["bid"],
                    ask=spot["ask"],
                    ts=spot["ts"],
                    offset_usd=offset,
                )
                if ingested:
                    b = ingested["bar"]
                    ev = self.core.order_manager.update_price_tick(
                        current_price=float(b["c"]),
                        high=float(b["h"]),
                        low=float(b["l"]),
                        ts=int(b["t"]),
                    )
                    if ingested["new_bar_closed"] or ev or loop_counter % 10 == 0:
                        # Recompute full SMC + Monte Carlo state when candle closes, order event occurs, or every 20s
                        state = await asyncio.to_thread(
                            self.core.compute_full_state, None, ingested["new_bar_closed"], False
                        )
                        await self.broadcast(state)
                    else:
                        await self.broadcast(
                            {
                                "type": "tick",
                                "timeframe": self.core.timeframe,
                                "bar": ingested["bar"],
                                "bid": ingested["bid"],
                                "ask": ingested["ask"],
                                "spread_points": ingested["spread_points"],
                                "active_order": self.core.order_manager.get_active_order_dict(float(b["c"])),
                                "source": spot["source"],
                            }
                        )

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            req_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not req_line:
                writer.close()
                return
            parts = req_line.decode("utf-8", "replace").strip().split(" ")
            if len(parts) < 2:
                writer.close()
                return
            method, full_path = parts[0].upper(), parts[1]
            parsed_url = urllib.parse.urlparse(full_path)
            path = parsed_url.path
            query = urllib.parse.parse_qs(parsed_url.query)

            headers: Dict[str, str] = {}
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
                decoded = line.decode("utf-8", "replace").strip()
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            # Check for WebSocket Upgrade on /ws
            if path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
                ws_key = headers.get("sec-websocket-key", "")
                accept = base64.b64encode(
                    hashlib.sha1((ws_key + WS_MAGIC).encode("utf-8")).digest()
                ).decode("ascii")
                handshake = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                )
                writer.write(handshake.encode("utf-8"))
                await writer.drain()

                client = WebSocketClient(writer)
                self.clients.add(client)
                state = self.core.last_state or self.core.compute_full_state()
                await client.send_json(state)

                while not client.closed:
                    msg_text = await read_ws_frame(reader)
                    if msg_text is None:
                        break
                    if msg_text == "__PING__":
                        continue
                    try:
                        cmd = json.loads(msg_text)
                        await self.handle_ws_command(client, cmd)
                    except Exception:
                        pass
                self.clients.discard(client)
                writer.close()
                return

            content_len = int(headers.get("content-length") or 0)
            body_bytes = await reader.readexactly(content_len) if content_len > 0 else b""

            await self.handle_http_request(writer, method, path, query, body_bytes)
        except Exception:
            try:
                writer.close()
            except Exception:
                pass

    async def handle_ws_command(self, client: WebSocketClient, cmd: Dict[str, Any]) -> None:
        action = str(cmd.get("action") or "")
        if action == "change_tf":
            tf = str(cmd.get("timeframe") or "M5")
            state = await asyncio.to_thread(self.core.compute_full_state, tf, True, False)
            await self.broadcast(state)
        elif action == "change_style":
            style = str(cmd.get("trading_style") or "intraday").lower()
            tf = cmd.get("timeframe")
            state = await asyncio.to_thread(self.core.compute_full_state, tf, True, False, style)
            await self.broadcast(state)
        elif action == "step_replay":
            self.auto_replay = False
            steps = int(cmd.get("steps") or 1)
            self.core.hub.step_replay(steps)
            state = await asyncio.to_thread(self.core.compute_full_state)
            await self.broadcast(state)
        elif action == "reset_replay":
            self.auto_replay = False
            self.core.hub.reset_replay(int(cmd.get("cursor") or 234))
            state = await asyncio.to_thread(self.core.compute_full_state, None, False, True)
            await self.broadcast(state)
        elif action == "toggle_auto_replay":
            self.core.hub.replay_mode = True
            self.auto_replay = not self.auto_replay
            await self.broadcast({"type": "auto_replay_status", "auto_replay": self.auto_replay})
        elif action == "live_mode":
            self.auto_replay = False
            self.core.hub.replay_mode = False
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, True)
            await self.broadcast(state)
        elif action == "analyze_now":
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, True)
            await self.broadcast(state)
        elif action == "cancel_active_order":
            reason = str(cmd.get("reason") or "MANUAL_CANCEL")
            self.core.order_manager.cancel_active_order(reason=reason)
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "close_active_order":
            cp = float(cmd.get("current_price") or self.core.hub.latest_spot.get("price") or 0.0)
            self.core.order_manager.close_active_order(reason="MANUAL_CLOSE", current_price=cp)
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "fill_active_order_now":
            cp = float(cmd.get("current_price") or self.core.hub.latest_spot.get("price") or 0.0)
            self.core.order_manager.fill_now(current_price=cp)
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "set_breakeven":
            self.core.order_manager.set_breakeven()
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, False)
            await self.broadcast(state)
        elif action == "scan_new_setup":
            self.core.order_manager.cancel_active_order(reason="SCAN_NEW_SETUP")
            state = await asyncio.to_thread(self.core.compute_full_state, None, True, True)
            await self.broadcast(state)

    async def handle_http_request(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        body_bytes: bytes,
    ) -> None:
        if path == "/api/state" and method == "GET":
            tf = (query.get("tf") or [self.core.timeframe])[0]
            state = self.core.compute_full_state(timeframe=tf)
            await self.send_http_json(writer, 200, state)
            return

        if path == "/api/config" and method == "POST":
            payload = json.loads(body_bytes.decode("utf-8", "replace") or "{}")
            state = self.core.update_settings(payload)
            await self.broadcast(state)
            await self.send_http_json(writer, 200, state)
            return

        if path == "/api/replay" and method == "POST":
            payload = json.loads(body_bytes.decode("utf-8", "replace") or "{}")
            mode = payload.get("mode", "step")
            if mode == "reset":
                self.auto_replay = False
                self.core.hub.reset_replay(int(payload.get("cursor") or 234))
            elif mode == "live":
                self.auto_replay = False
                self.core.hub.replay_mode = False
            else:
                self.auto_replay = False
                self.core.hub.step_replay(int(payload.get("steps") or 1))
            state = self.core.compute_full_state(force_refresh=(mode == "live"), force_ai=True)
            await self.broadcast(state)
            await self.send_http_json(writer, 200, state)
            return

        if path == "/api/analyze" and method == "POST":
            state = self.core.compute_full_state(force_ai=True)
            await self.broadcast(state)
            await self.send_http_json(writer, 200, state)
            return

        if path == "/api/order/action" and method == "POST":
            payload = json.loads(body_bytes.decode("utf-8", "replace") or "{}")
            action = str(payload.get("action") or "")
            cp = float(payload.get("current_price") or self.core.hub.latest_spot.get("price") or 0.0)
            if action == "cancel":
                self.core.order_manager.cancel_active_order(reason="MANUAL_CANCEL")
            elif action == "close":
                self.core.order_manager.close_active_order(reason="MANUAL_CLOSE", current_price=cp)
            elif action == "fill_now":
                self.core.order_manager.fill_now(current_price=cp)
            elif action == "set_breakeven":
                self.core.order_manager.set_breakeven()
            elif action == "scan_new_setup":
                self.core.order_manager.cancel_active_order(reason="SCAN_NEW_SETUP")
            state = await asyncio.to_thread(
                self.core.compute_full_state, None, True, action == "scan_new_setup"
            )
            await self.broadcast(state)
            await self.send_http_json(writer, 200, state)
            return

        rel_path = "index.html" if path in ("/", "") else path.lstrip("/")
        file_path = os.path.abspath(os.path.join(WEB_DIR, rel_path))
        if not file_path.startswith(WEB_DIR) or not os.path.isfile(file_path):
            await self.send_http_json(writer, 404, {"error": f"Not found: {path}"})
            return

        mime, _ = mimetypes.guess_type(file_path)
        content_type = mime or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        with open(file_path, "rb") as fh:
            data = fh.read()
        resp = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Cache-Control: no-cache\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + data
        writer.write(resp)
        await writer.drain()
        writer.close()

    async def send_http_json(
        self, writer: asyncio.StreamWriter, status: int, doc: Dict[str, Any]
    ) -> None:
        raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        status_text = "OK" if status == 200 else "Not Found" if status == 404 else "Error"
        resp = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(raw)}\r\n"
            f"Cache-Control: no-cache\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + raw
        writer.write(resp)
        await writer.drain()
        writer.close()

    async def serve_forever(self) -> None:
        self.core.compute_full_state(force_refresh=True)
        try:
            server = await asyncio.start_server(
                self.handle_client, self.host, self.port, reuse_address=True
            )
        except OSError:
            # Automatically terminate any old server instance still holding the port
            try:
                out = subprocess.check_output(
                    ["lsof", "-ti", f"tcp:{self.port}"], text=True
                ).strip()
                my_pid = os.getpid()
                for line in out.splitlines():
                    pid = int(line.strip())
                    if pid != my_pid:
                        os.kill(pid, signal.SIGTERM)
                await asyncio.sleep(0.5)
            except Exception:
                pass
            server = await asyncio.start_server(
                self.handle_client, self.host, self.port, reuse_address=True
            )
        tick_task = asyncio.create_task(self.tick_loop())
        tv_task = asyncio.create_task(self.tv_ws_loop())
        ws_task = asyncio.create_task(self.binance_ws_loop())
        print(f"KAIROS-79 (Precision Gold Timing & Risk System) running at http://{self.host}:{self.port}")
        print(f"Active Data Feed: {self.core.last_state.get('data_source') if self.core.last_state else 'initializing'}")
        try:
            async with server:
                await server.serve_forever()
        finally:
            tick_task.cancel()
            tv_task.cancel()
            ws_task.cancel()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run KAIROS-79 (Precision Gold Timing & Risk System) Web Server.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args(argv)

    srv = AssistantServer(host=args.host, port=args.port)
    try:
        asyncio.run(srv.serve_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
