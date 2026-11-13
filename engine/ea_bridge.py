#!/usr/bin/env python3
"""KAIROS-79 MetaTrader 5 (MT5) EA Bridge Manager (ea_bridge.py).

Provides a high-reliability bidirectional communication bridge between:
  1. KAIROS-79 Decision & Risk Engine (Python Backend)
  2. Web Monitoring & Control Center (Web UI Dashboard)
  3. MetaTrader 5 Expert Advisor (KAIROS79_Bridge.mq5 on Windows PC / VPS)

Capabilities:
  - Master Auto-Trading Switch (Auto ON/OFF)
  - Telemetry & Live Heartbeat (Balance, Equity, Margin, Free Margin, Floating PnL, Spread)
  - Action Queue (OPEN_ORDER, MODIFY_ORDER, CLOSE_ORDER, CLOSE_ALL)
  - Execution Tracking (Broker Ticket numbers, exact fill price, slippage)
  - Watchdog connection detection (online / offline)
  - Idempotent deduplication to prevent double execution
"""

from datetime import datetime, timezone
import json
import logging
import os
import statistics
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kairos.ea_bridge")


class EABridgeManager:
    """Manages MT5 EA telemetry, action queue, and broker execution state."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        heartbeat_timeout_sec: float = 12.0,
    ) -> None:
        self.config_path = config_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
        )
        self.heartbeat_timeout_sec = heartbeat_timeout_sec

        # State
        self.auto_trading_enabled: bool = False
        self.last_heartbeat_ts: float = 0.0
        self.last_poll_ts: float = 0.0
        self.account_info: Dict[str, Any] = {
            "account_id": None,
            "broker": "Waiting for MT5...",
            "server": "",
            "currency": "USD",
            "balance": 0.0,
            "equity": 0.0,
            "margin": 0.0,
            "free_margin": 0.0,
            "margin_level_pct": 0.0,
            "spread_points": 0.0,
            "spread_usd": 0.0,
            "symbol": "XAUUSD",
            "open_positions": [],
            "open_positions_count": 0,
            "pending_orders": [],
            "pending_orders_count": 0,
            "total_floating_pnl": 0.0,
            "ea_version": "",
            "daily_dd_pct": 0.0,
            "kill_switch_tripped": False,
            "dynamic_offset_usd": 0.0,
        }

        # Action Queue & Signal/Telemetry Buffers
        self.action_queue: List[Dict[str, Any]] = []
        self.action_history: List[Dict[str, Any]] = []
        self.executed_signal_ids: set = set()
        self.signal_states: Dict[str, Dict[str, Any]] = {}
        self.execution_history: List[Dict[str, Any]] = []
        self.basis_samples: List[float] = []
        self.spread_samples: List[float] = []
        self.day_start_equity: float = 0.0
        self.current_day_str: str = ""
        self._action_counter: int = 0

        self.load_config()

    def load_config(self) -> None:
        """Load auto_trading setting from config.json."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, encoding="utf-8") as fh:
                    cfg = json.load(fh)
                ea_cfg = cfg.get("auto_trading") or {}
                self.auto_trading_enabled = bool(ea_cfg.get("enabled", False))
            except Exception as e:
                logger.warning(f"Failed to load EA bridge config: {e}")

    def save_config(self) -> None:
        """Persist auto_trading setting to config.json."""
        if not os.path.exists(self.config_path):
            return
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if "auto_trading" not in cfg:
                cfg["auto_trading"] = {}
            cfg["auto_trading"]["enabled"] = self.auto_trading_enabled

            tmp = self.config_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.config_path)
        except Exception as e:
            logger.warning(f"Failed to save EA bridge config: {e}")

    def set_auto_trading(self, enabled: bool) -> bool:
        """Toggle Auto-Trading Master Switch."""
        self.auto_trading_enabled = bool(enabled)
        self.save_config()
        return self.auto_trading_enabled

    def toggle_auto_trading(self) -> bool:
        """Invert Auto-Trading Master Switch."""
        return self.set_auto_trading(not self.auto_trading_enabled)

    @property
    def is_connected(self) -> bool:
        """Check whether MT5 EA has sent a heartbeat or poll recently."""
        now = time.time()
        hb_active = (self.last_heartbeat_ts > 0) and ((now - self.last_heartbeat_ts) <= self.heartbeat_timeout_sec)
        poll_active = (self.last_poll_ts > 0) and ((now - self.last_poll_ts) <= self.heartbeat_timeout_sec)
        return hb_active or poll_active

    def record_poll(self) -> None:
        """Record live polling activity from MT5 EA."""
        self.last_poll_ts = time.time()

    def record_heartbeat(
        self,
        data: Dict[str, Any],
        reference_feed_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Record live telemetry sent by MT5 EA."""
        now = time.time()
        self.last_heartbeat_ts = now

        pos_list = data.get("open_positions") or []
        pending_list = data.get("pending_orders") or []
        tot_pnl = sum(float(p.get("profit", 0.0)) for p in pos_list)

        bid = float(data["bid"]) if data.get("bid") is not None else None
        ask = float(data["ask"]) if data.get("ask") is not None else None
        price = float(data["price"]) if data.get("price") is not None else (bid or None)
        spread_pts = float(data.get("spread_points") or 0.0)
        if bid is not None and ask is not None and ask > 0.0 and bid > 0.0:
            spread_usd = round(max(0.0, ask - bid), 3)
        else:
            spread_usd = round(max(0.0, spread_pts * 0.01), 3)

        self.spread_samples.append(spread_usd)
        self.spread_samples = self.spread_samples[-20:]

        balance = round(float(data.get("balance") or 0.0), 2)
        equity = round(float(data.get("equity") or 0.0), 2)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.current_day_str != today_str or self.day_start_equity <= 0.0:
            if equity > 0.0 or balance > 0.0:
                self.current_day_str = today_str
                self.day_start_equity = equity if equity > 0.0 else balance

        local_dd_pct = 0.0
        if self.day_start_equity > 0.0 and equity > 0.0:
            local_dd_pct = max(0.0, ((self.day_start_equity - equity) / self.day_start_equity) * 100.0)
        ea_dd_pct = float(data.get("daily_dd_pct") or 0.0)
        daily_dd_pct = round(max(local_dd_pct, ea_dd_pct), 2)

        dynamic_offset = self.account_info.get("dynamic_offset_usd", 0.0)
        if reference_feed_price is not None and reference_feed_price > 0.0 and price is not None and price > 0.0:
            self.basis_samples.append(price - float(reference_feed_price))
            self.basis_samples = self.basis_samples[-20:]
            dynamic_offset = round(statistics.median(self.basis_samples), 2)

        self.account_info = {
            "account_id": data.get("account_id"),
            "broker": str(data.get("broker") or "MetaTrader 5"),
            "server": str(data.get("server") or ""),
            "currency": str(data.get("currency") or "USD"),
            "balance": balance,
            "equity": equity,
            "margin": round(float(data.get("margin") or 0.0), 2),
            "free_margin": round(float(data.get("free_margin") or 0.0), 2),
            "margin_level_pct": round(float(data.get("margin_level_pct") or 0.0), 2),
            "spread_points": round(spread_pts, 1),
            "spread_usd": spread_usd,
            "symbol": str(data.get("symbol") or "XAUUSD"),
            "bid": bid,
            "ask": ask,
            "price": price,
            "open_positions": pos_list,
            "open_positions_count": len(pos_list),
            "pending_orders": pending_list,
            "pending_orders_count": len(pending_list),
            "total_floating_pnl": round(tot_pnl, 2),
            "ea_version": str(data.get("ea_version") or self.account_info.get("ea_version") or ""),
            "daily_dd_pct": daily_dd_pct,
            "kill_switch_tripped": bool(data.get("kill_switch_tripped", False)),
            "dynamic_offset_usd": dynamic_offset,
            "last_seen_ts": int(now),
        }
        return self.account_info

    def enqueue_action(
        self,
        action_type: str,
        payload: Dict[str, Any],
        signal_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Enqueue an execution command for the MT5 EA.
        
        Action Types:
          - OPEN_ORDER: payload={side, order_type, lot, price, sl, tp, comment}
          - MODIFY_ORDER: payload={ticket, new_sl, new_tp}
          - CLOSE_ORDER: payload={ticket}
          - CLOSE_PARTIAL: payload={ticket, close_lot, new_sl}
          - CLOSE_ALL: payload={symbol, magic}
        """
        # Idempotency check for new orders
        if signal_id:
            if signal_id in self.executed_signal_ids:
                logger.info(f"Signal {signal_id} already queued/executed. Skipping.")
                return None
            self.executed_signal_ids.add(signal_id)

        self._action_counter += 1
        now_ts = int(time.time())
        action_id = f"ACT-{now_ts}-{self._action_counter}"

        action = {
            "action_id": action_id,
            "type": str(action_type).upper(),
            "signal_id": signal_id or "",
            "created_at": now_ts,
            "status": "QUEUED",
            **payload,
        }

        if signal_id:
            self.signal_states[signal_id] = {
                "signal_id": signal_id,
                "action_id": action_id,
                "type": action["type"],
                "status": "QUEUED",
                "ticket": None,
                "updated_at": now_ts,
            }

        # Avoid duplicate close commands for the same ticket
        if action["type"] == "CLOSE_ORDER" and payload.get("ticket"):
            t = payload["ticket"]
            for existing in self.action_queue:
                if existing.get("type") == "CLOSE_ORDER" and existing.get("ticket") == t:
                    return existing

        self.action_queue.append(action)
        return action

    def poll_actions(self) -> List[Dict[str, Any]]:
        """Called by MT5 EA GET /api/ea/poll.
        Returns pending actions and advances them to IN_FLIGHT."""
        now_ts = int(time.time())
        pending = list(self.action_queue)
        for act in pending:
            act["status"] = "IN_FLIGHT"
            act["dispatched_at"] = now_ts
            sig_id = act.get("signal_id")
            if sig_id and sig_id in self.signal_states:
                self.signal_states[sig_id]["status"] = "IN_FLIGHT"
                self.signal_states[sig_id]["updated_at"] = now_ts
            # Merge payload keys to top level for ultra-simple EA parsing
            if isinstance(act.get("payload"), dict):
                for k, v in act["payload"].items():
                    if k not in act:
                        act[k] = v
        # Clear active queue so they are not sent twice
        self.action_history.extend(pending)
        self.action_history = self.action_history[-100:]
        self.action_queue.clear()
        return pending

    def record_execution(self, exec_data: Dict[str, Any]) -> Dict[str, Any]:
        """Record order execution confirmation sent back from MT5 EA."""
        now_ts = int(time.time())
        status = str(exec_data.get("status") or "SUCCESS").upper()
        signal_id = exec_data.get("signal_id") or ""
        ticket = exec_data.get("ticket")
        rec = {
            "received_at": now_ts,
            "action_id": exec_data.get("action_id"),
            "signal_id": signal_id,
            "status": status,
            "order_type": exec_data.get("order_type") or "",
            "ticket": ticket,
            "deal": exec_data.get("deal"),
            "symbol": exec_data.get("symbol") or "XAUUSD",
            "side": exec_data.get("side"),
            "lot": float(exec_data.get("lot") or 0.0),
            "fill_price": float(exec_data.get("fill_price") or 0.0),
            "slippage_points": float(exec_data.get("slippage_points") or 0.0),
            "sl": float(exec_data.get("sl") or 0.0),
            "tp": float(exec_data.get("tp") or 0.0),
            "profit": float(exec_data.get("profit") or 0.0),
            "comment": exec_data.get("comment") or "KAIROS-79",
            "error_code": exec_data.get("error_code") or 0,
            "error_msg": exec_data.get("error_msg") or "",
        }
        if signal_id:
            self.signal_states[signal_id] = {
                "signal_id": signal_id,
                "action_id": rec["action_id"],
                "status": status,
                "order_type": rec["order_type"],
                "ticket": ticket,
                "fill_price": rec["fill_price"],
                "error_code": rec["error_code"],
                "error_msg": rec["error_msg"],
                "updated_at": now_ts,
            }
        self.execution_history.append(rec)
        self.execution_history = self.execution_history[-100:]
        return rec

    def get_status_dict(self) -> Dict[str, Any]:
        """Summary dictionary for Web UI and API clients."""
        conn = self.is_connected
        acc = self.account_info
        recent_ts = max(self.last_heartbeat_ts, self.last_poll_ts)
        last_seen = round(time.time() - recent_ts, 1) if recent_ts > 0 else None
        return {
            "auto_trading_enabled": self.auto_trading_enabled,
            "is_connected": conn,
            "status_label": "CONNECTED" if conn else "OFFLINE",
            "status_label_th": "เชื่อมต่อแล้ว (Connected)" if conn else "รอการเชื่อมต่อ (Offline)",
            "account_id": acc.get("account_id"),
            "broker": acc.get("broker"),
            "server": acc.get("server"),
            "currency": acc.get("currency"),
            "balance": acc.get("balance"),
            "equity": acc.get("equity"),
            "margin": acc.get("margin"),
            "free_margin": acc.get("free_margin"),
            "spread_points": acc.get("spread_points"),
            "spread_usd": acc.get("spread_usd", 0.0),
            "open_positions_count": acc.get("open_positions_count", 0),
            "open_positions": acc.get("open_positions", []),
            "pending_orders_count": acc.get("pending_orders_count", 0),
            "pending_orders": acc.get("pending_orders", []),
            "total_floating_pnl": acc.get("total_floating_pnl", 0.0),
            "ea_version": acc.get("ea_version", ""),
            "daily_dd_pct": acc.get("daily_dd_pct", 0.0),
            "kill_switch_tripped": acc.get("kill_switch_tripped", False),
            "dynamic_offset_usd": acc.get("dynamic_offset_usd", 0.0),
            "pending_actions_count": len(self.action_queue),
            "recent_executions_count": len(self.execution_history),
            "last_execution": self.execution_history[-1] if self.execution_history else None,
            "last_heartbeat_ago_sec": round(time.time() - self.last_heartbeat_ts, 1)
            if self.last_heartbeat_ts > 0
            else None,
            "last_seen_sec": last_seen,
        }
