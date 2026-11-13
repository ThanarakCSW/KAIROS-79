#!/usr/bin/env python3
"""Free Real-Time Gold (XAU/USD) Market Data & Live Stream Engine (data_feed.py).

Real-time data pipeline (Mode 1 — Free Internet Feeds, No API Key Required):
  1. Live Spot XAU/USD Bid/Ask & Spread:
     - Primary: Swissquote Institutional Spot Feed (`instrument/XAU/USD`)
     - Secondary: Binance Spot Gold BookTicker (`PAXGUSDT`)
  2. Multi-Timeframe OHLCV Candles (M5, M15, H1, H4):
     - Primary: Binance Klines (`PAXGUSDT`) aligned to Spot XAU/USD price
     - Secondary: Yahoo Finance (`GC=F`) aligned to Spot XAU/USD price
  3. Live WebSocket Trade Stream:
     - Connects directly to `wss://stream.binance.com:9443/ws/paxgusdt@trade` over TLS
       plus 2-second Swissquote Spot XAU/USD Bid/Ask calibration
  4. Offline Replay Mode:
     - Only used when the user explicitly clicks Replay controls or has no internet connection
"""

import asyncio
import base64
import json
import os
import random
import socket
import ssl
import string
import struct
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    if os.path.exists("/etc/ssl/cert.pem"):
        SSL_CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    else:
        SSL_CTX = ssl.create_default_context()

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

SWISSQUOTE_XAU_URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
BINANCE_BOOK_URL = "https://api.binance.com/api/v3/ticker/bookTicker?symbol=PAXGUSDT"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval={interval}&limit={limit}"
YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}"
BINANCE_WS_HOST = "stream.binance.com"
BINANCE_WS_PORT = 9443
BINANCE_WS_PATH = "/ws/paxgusdt@trade"

TV_WS_HOST = "data.tradingview.com"
TV_WS_PORT = 443
TV_WS_PATH = "/socket.io/websocket?from=chart%2F&type=chart"
TV_DEFAULT_SYMBOL = "OANDA:XAUUSD"

TF_TO_TV = {
    "M1": "1",
    "M5": "5",
    "M15": "15",
    "H1": "60",
    "H4": "240",
}

_TV_BACKOFF_UNTIL: float = 0.0

TF_TO_BINANCE = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "H1": "1h",
    "H4": "4h",
}

TF_TO_YAHOO = {
    "M1": ("1d", "1m"),
    "M5": ("5d", "5m"),
    "M15": ("1mo", "15m"),
    "H1": ("1mo", "60m"),
    "H4": ("3mo", "60m"),
}

TF_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
}

DEFAULT_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "tests",
    "fixtures",
    "xauusd_m5_sample.json",
)


def _http_get_json(url: str, timeout: int = 5) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


DEFAULT_BARS_LIMIT = 1500  # Default history depth for 1,000 - 2,000 candles analysis


def expand_fixture_bars_to_target(bars: List[Dict[str, Any]], target_count: int = 1500) -> List[Dict[str, Any]]:
    """Ensure fixture has 1000-2000 bars by realistically extrapolating earlier historical candles."""
    if not bars or len(bars) >= target_count:
        return [dict(b) for b in bars]

    needed = target_count - len(bars)
    step_sec = 300
    if len(bars) >= 2:
        step_sec = max(60, int(bars[1]["t"]) - int(bars[0]["t"]))

    first_t = int(bars[0]["t"])
    first_o = float(bars[0]["o"])

    rngs = [abs(float(b["h"]) - float(b["l"])) for b in bars[:min(50, len(bars))]]
    avg_rng = sum(rngs) / max(1, len(rngs))

    synth_bars = []
    curr_c = first_o
    curr_t = first_t - (needed * step_sec)

    import random
    rng = random.Random(42)

    for _ in range(needed):
        drift = rng.uniform(-avg_rng * 0.45, avg_rng * 0.45)
        bar_o = curr_c
        bar_c = round(bar_o + drift, 2)
        bar_h = round(max(bar_o, bar_c) + rng.uniform(0.10, avg_rng * 0.55), 2)
        bar_l = round(min(bar_o, bar_c) - rng.uniform(0.10, avg_rng * 0.55), 2)
        vol = rng.randint(80, 500)
        synth_bars.append({
            "t": curr_t,
            "o": bar_o,
            "h": bar_h,
            "l": bar_l,
            "c": bar_c,
            "v": vol,
        })
        curr_c = bar_c
        curr_t += step_sec

    return synth_bars + [dict(b) for b in bars]


def load_fixture_bars(fixture_path: Optional[str] = None, target_count: int = 1500) -> List[Dict[str, Any]]:
    path = fixture_path or DEFAULT_FIXTURE
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    bars = doc["bars"] if isinstance(doc, dict) and "bars" in doc else doc
    raw = [dict(b) for b in bars]
    return expand_fixture_bars_to_target(raw, target_count=target_count)


def resample_bars(m5_bars: List[Dict[str, Any]], target_tf: str) -> List[Dict[str, Any]]:
    """Aggregate M5 bars into M15, H1, or H4 bars deterministically."""
    tf = target_tf.upper()
    if tf in ("M1", "M5") or not m5_bars:
        return [dict(b) for b in m5_bars]

    bucket_sec = TF_SECONDS.get(tf, 300)
    buckets: Dict[int, Dict[str, Any]] = {}
    for b in m5_bars:
        slot = (int(b["t"]) // bucket_sec) * bucket_sec
        if slot not in buckets:
            buckets[slot] = {
                "t": slot,
                "o": float(b["o"]),
                "h": float(b["h"]),
                "l": float(b["l"]),
                "c": float(b["c"]),
                "v": int(b.get("v") or 0),
            }
        else:
            cur = buckets[slot]
            cur["h"] = max(cur["h"], float(b["h"]))
            cur["l"] = min(cur["l"], float(b["l"]))
            cur["c"] = float(b["c"])
            cur["v"] += int(b.get("v") or 0)

    return [buckets[k] for k in sorted(buckets)]


def _tv_random_session(prefix: str) -> str:
    chars = string.ascii_lowercase + string.digits
    return f"{prefix}_{''.join(random.choice(chars) for _ in range(12))}"


def _tv_pack_msg(func: str, params: List[Any]) -> str:
    payload = json.dumps({"m": func, "p": params}, separators=(",", ":"))
    return f"~m~{len(payload)}~m~{payload}"


def _tv_unpack_msgs(raw_text: str) -> List[str]:
    packets: List[str] = []
    idx = 0
    n = len(raw_text)
    while idx < n:
        if not raw_text.startswith("~m~", idx):
            break
        end_len_idx = raw_text.find("~m~", idx + 3)
        if end_len_idx == -1:
            break
        try:
            length = int(raw_text[idx + 3 : end_len_idx])
        except ValueError:
            break
        body_start = end_len_idx + 3
        body_end = body_start + length
        packets.append(raw_text[body_start:body_end])
        idx = body_end
    return packets


def _build_masked_ws_frame(text: str, opcode: int = 0x81) -> bytes:
    raw = text.encode("utf-8")
    length = len(raw)
    mask_key = os.urandom(4)
    header = bytearray([opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask_key)
    masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(raw))
    return bytes(header) + masked_payload


def _recv_exact(sock: ssl.SSLSocket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("WebSocket closed early")
        buf.extend(chunk)
    return bytes(buf)


def _read_sync_ws_text(sock: ssl.SSLSocket) -> Optional[str]:
    head = _recv_exact(sock, 2)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask_key = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length > 0 else b""
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    if opcode == 0x8:
        return None
    if opcode == 0x9:
        sock.sendall(_build_masked_ws_frame("", opcode=0x8A))
        return ""
    return payload.decode("utf-8", "replace")


def fetch_tradingview_gold_bars(
    timeframe: str = "M5",
    symbol: str = TV_DEFAULT_SYMBOL,
    limit: int = DEFAULT_BARS_LIMIT,
    timeout: float = 4.0,
) -> Optional[Dict[str, Any]]:
    """Primary Tier 1: Fetch real-time XAUUSD OHLCV bars & Bid/Ask directly from TradingView WebSocket (`wss://data.tradingview.com`)."""
    global _TV_BACKOFF_UNTIL
    now = time.time()
    if now < _TV_BACKOFF_UNTIL:
        return None

    tv_interval = TF_TO_TV.get(timeframe.upper(), "5")
    cs = _tv_random_session("cs")
    qs = _tv_random_session("qs")

    raw_sock = None
    ssl_sock = None
    try:
        raw_sock = socket.create_connection((TV_WS_HOST, TV_WS_PORT), timeout=timeout)
        ssl_sock = SSL_CTX.wrap_socket(raw_sock, server_hostname=TV_WS_HOST)
        ssl_sock.settimeout(timeout)

        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {TV_WS_PATH} HTTP/1.1\r\n"
            f"Host: {TV_WS_HOST}\r\n"
            f"Origin: https://www.tradingview.com\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {UA}\r\n\r\n"
        )
        ssl_sock.sendall(handshake.encode("utf-8"))

        resp_buf = b""
        while b"\r\n\r\n" not in resp_buf:
            chunk = ssl_sock.recv(1024)
            if not chunk:
                break
            resp_buf += chunk
        if b"101" not in resp_buf.split(b"\r\n", 1)[0]:
            _TV_BACKOFF_UNTIL = time.time() + 15.0
            return None

        init_batch = (
            _tv_pack_msg("set_auth_token", ["unauthorized_user_token"])
            + _tv_pack_msg("chart_create_session", [cs, ""])
            + _tv_pack_msg("quote_create_session", [qs])
            + _tv_pack_msg("quote_set_fields", [qs, "lp", "bid", "ask", "ch", "chp", "volume"])
            + _tv_pack_msg("quote_add_symbols", [qs, symbol])
            + _tv_pack_msg(
                "resolve_symbol",
                [cs, "sds_sym_1", f'={{"adjustment":"splits","symbol":"{symbol}"}}'],
            )
            + _tv_pack_msg("create_series", [cs, "sds_1", "s1", "sds_sym_1", tv_interval, limit, ""])
        )
        ssl_sock.sendall(_build_masked_ws_frame(init_batch))

        bars_by_ts: Dict[int, Dict[str, Any]] = {}
        quote_data: Dict[str, Any] = {}
        deadline = time.time() + timeout

        while time.time() < deadline:
            frame_text = _read_sync_ws_text(ssl_sock)
            if frame_text is None:
                break
            if not frame_text:
                continue

            for pkt in _tv_unpack_msgs(frame_text):
                if pkt.startswith("~h~"):
                    hb = f"~m~{len(pkt)}~m~{pkt}"
                    ssl_sock.sendall(_build_masked_ws_frame(hb))
                    continue
                if not pkt.startswith("{"):
                    continue
                try:
                    doc = json.loads(pkt)
                except Exception:
                    continue

                msg_type = doc.get("m")
                params = doc.get("p") or []
                if msg_type == "qsd" and len(params) >= 2 and isinstance(params[1], dict):
                    v = params[1].get("v") or {}
                    for k in ("lp", "bid", "ask", "ch", "chp"):
                        if v.get(k) is not None:
                            quote_data[k] = float(v[k])

                elif msg_type in ("timescale_update", "du") and len(params) >= 2 and isinstance(params[1], dict):
                    sds = params[1].get("sds_1") or {}
                    series_rows = sds.get("s") or []
                    for item in series_rows:
                        vals = item.get("v") or []
                        if len(vals) >= 5:
                            ts_sec = int(float(vals[0]))
                            bars_by_ts[ts_sec] = {
                                "t": ts_sec,
                                "o": round(float(vals[1]), 2),
                                "h": round(float(vals[2]), 2),
                                "l": round(float(vals[3]), 2),
                                "c": round(float(vals[4]), 2),
                                "v": int(float(vals[5])) if len(vals) > 5 and vals[5] is not None else 0,
                            }

                elif msg_type == "series_completed":
                    break

            if len(bars_by_ts) >= limit:
                break

        if len(bars_by_ts) < 30:
            return None

        sorted_bars = [bars_by_ts[k] for k in sorted(bars_by_ts)][-limit:]
        last_close = float(sorted_bars[-1]["c"])
        bid = round(float(quote_data.get("bid") or (last_close - 0.12)), 2)
        ask = round(float(quote_data.get("ask") or (last_close + 0.12)), 2)
        mid = round(float(quote_data.get("lp") or last_close), 2)
        spread_usd = round(max(0.05, ask - bid), 2)

        return {
            "bars": sorted_bars,
            "quote": {
                "symbol": "XAUUSD",
                "bid": bid,
                "ask": ask,
                "price": mid,
                "spread_usd": spread_usd,
                "spread_points": int(round(spread_usd * 100)),
                "ts": int(time.time()),
                "source": f"tradingview_ws ({symbol})",
            },
            "source": f"tradingview_ws ({symbol})",
        }
    except Exception:
        _TV_BACKOFF_UNTIL = time.time() + 15.0
        return None
    finally:
        if ssl_sock:
            try:
                ssl_sock.close()
            except Exception:
                pass
        elif raw_sock:
            try:
                raw_sock.close()
            except Exception:
                pass


def fetch_live_spot_xauusd(timeout: int = 4) -> Optional[Dict[str, Any]]:
    """Fetch live real-time XAU/USD Bid, Ask, Mid, and Spread from Swissquote or Binance."""
    # 1. Try Swissquote Institutional Spot XAU/USD Feed (true OTC Spot Gold)
    try:
        data = _http_get_json(SWISSQUOTE_XAU_URL, timeout=timeout)
        if isinstance(data, list) and data:
            for entry in data:
                prices = entry.get("spreadProfilePrices") or []
                if prices:
                    # Pick standard/prime profile
                    p = prices[0]
                    bid = float(p["bid"])
                    ask = float(p["ask"])
                    mid = round((bid + ask) / 2.0, 2)
                    spread_usd = round(max(0.05, ask - bid), 2)
                    ts_ms = int(entry.get("ts") or (time.time() * 1000))
                    return {
                        "symbol": "XAUUSD",
                        "bid": round(bid, 2),
                        "ask": round(ask, 2),
                        "price": mid,
                        "spread_usd": spread_usd,
                        "spread_points": int(round(spread_usd * 100)),
                        "ts": ts_ms // 1000,
                        "source": "swissquote_spot_xauusd",
                    }
    except Exception:
        pass

    # 2. Fallback to Binance Spot PAXGUSDT BookTicker
    try:
        doc = _http_get_json(BINANCE_BOOK_URL, timeout=timeout)
        if isinstance(doc, dict) and doc.get("bidPrice"):
            bid = float(doc["bidPrice"])
            ask = float(doc["askPrice"])
            mid = round((bid + ask) / 2.0, 2)
            spread_usd = round(max(0.05, ask - bid), 2)
            return {
                "symbol": "XAUUSD",
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "price": mid,
                "spread_usd": spread_usd,
                "spread_points": int(round(spread_usd * 100)),
                "ts": int(time.time()),
                "source": "binance_paxgusdt_live",
            }
    except Exception:
        pass

    return None


def fetch_binance_gold_bars(
    timeframe: str = "M5",
    limit: int = DEFAULT_BARS_LIMIT,
    timeout: int = 5,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch free real-time gold candles from Binance public API (supports 1,000-2,000 bars via multi-batch)."""
    b_tf = TF_TO_BINANCE.get(timeframe.upper(), "5m")
    try:
        req_limit = max(50, min(limit, 2000))
        if req_limit <= 1000:
            url = BINANCE_KLINES_URL.format(interval=b_tf, limit=req_limit)
            rows = _http_get_json(url, timeout=timeout)
            if not isinstance(rows, list) or len(rows) < 30:
                return None
            all_rows = rows
        else:
            url1 = BINANCE_KLINES_URL.format(interval=b_tf, limit=1000)
            rows1 = _http_get_json(url1, timeout=timeout)
            if not isinstance(rows1, list) or len(rows1) < 30:
                return None
            first_ms = int(rows1[0][0])
            batch2_needed = min(1000, req_limit - len(rows1))
            url2 = f"{BINANCE_KLINES_URL.format(interval=b_tf, limit=batch2_needed)}&endTime={first_ms - 1}"
            rows2 = _http_get_json(url2, timeout=timeout)
            all_rows = (rows2 if isinstance(rows2, list) else []) + rows1

        bars: List[Dict[str, Any]] = []
        for r in all_rows:
            bars.append(
                {
                    "t": int(r[0]) // 1000,
                    "o": round(float(r[1]), 2),
                    "h": round(float(r[2]), 2),
                    "l": round(float(r[3]), 2),
                    "c": round(float(r[4]), 2),
                    "v": int(float(r[5])),
                }
            )
        seen = set()
        deduped = []
        for b in bars:
            if b["t"] not in seen:
                seen.add(b["t"])
                deduped.append(b)
        deduped.sort(key=lambda x: x["t"])
        return deduped[-req_limit:]
    except Exception:
        return None


def fetch_yahoo_gold_bars(
    timeframe: str = "M5",
    symbol: str = "GC=F",
    timeout: int = 5,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch free gold candles from Yahoo Finance v8 chart API."""
    rng, interval = TF_TO_YAHOO.get(timeframe.upper(), ("5d", "5m"))
    url = YAHOO_CHART_URL.format(symbol=symbol, range=rng, interval=interval)
    try:
        doc = _http_get_json(url, timeout=timeout)
        res = ((doc.get("chart") or {}).get("result") or [None])[0]
        if not res:
            return None
        ts_list = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
        opens = q.get("open") or []
        highs = q.get("high") or []
        lows = q.get("low") or []
        closes = q.get("close") or []
        vols = q.get("volume") or []

        bars: List[Dict[str, Any]] = []
        for i, ts in enumerate(ts_list):
            c = closes[i] if i < len(closes) else None
            if ts is None or c is None:
                continue
            o = opens[i] if i < len(opens) and opens[i] is not None else c
            h = highs[i] if i < len(highs) and highs[i] is not None else c
            l = lows[i] if i < len(lows) and lows[i] is not None else c
            v = vols[i] if i < len(vols) and vols[i] is not None else 0
            bars.append(
                {
                    "t": int(ts),
                    "o": round(float(o), 2),
                    "h": round(float(h), 2),
                    "l": round(float(l), 2),
                    "c": round(float(c), 2),
                    "v": int(v),
                }
            )
        if len(bars) < 30:
            return None
        if timeframe.upper() == "H4":
            bars = resample_bars(bars, "H4")
        return bars[-260:]
    except Exception:
        return None


def _align_bars_to_spot(
    bars: List[Dict[str, Any]],
    spot_quote: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Align COMEX Gold (GC=F) / PAXG historical candle basis to live Spot XAU/USD so chart matches TradingView Spot Gold."""
    if not bars or not spot_quote:
        return bars
    last_c = float(bars[-1]["c"])
    spot_p = float(spot_quote["price"])
    basis_diff = spot_p - last_c
    # Align Futures-to-Spot EFP basis (typically $5 to $150) directly to live Spot XAU/USD
    if abs(basis_diff) < 0.02 or abs(basis_diff) > 350.0:
        return bars
    aligned: List[Dict[str, Any]] = []
    for b in bars:
        aligned.append(
            {
                "t": int(b["t"]),
                "o": round(float(b["o"]) + basis_diff, 2),
                "h": round(float(b["h"]) + basis_diff, 2),
                "l": round(float(b["l"]) + basis_diff, 2),
                "c": round(float(b["c"]) + basis_diff, 2),
                "v": int(b.get("v") or 0),
            }
        )
    return aligned


class MarketDataHub:
    """Manages real-time multi-timeframe XAU/USD bars, live quotes, and interactive bar replay."""

    def __init__(self, fixture_path: Optional[str] = None):
        self.fixture_bars = load_fixture_bars(fixture_path)
        self.replay_mode = False
        self.replay_cursor = len(self.fixture_bars)
        self.source = "connecting_live"
        self.last_fetch_ts = 0.0
        self.last_fetch_ts_by_tf: Dict[str, float] = {}
        self.cached_bars_by_tf: Dict[str, List[Dict[str, Any]]] = {}
        self.latest_spot_quote: Optional[Dict[str, Any]] = None
        self.basis_shift: float = 0.0
        self.last_macro_ts: float = 0.0
        self.cached_macro: Optional[Dict[str, Any]] = None
        self._rng = random.Random(99)

    def get_bars(self, timeframe: str = "M5", force_refresh: bool = False, ttl_seconds: float = 12.0) -> Dict[str, Any]:
        tf = timeframe.upper()
        now = time.time()

        if not self.replay_mode:
            cached = self.cached_bars_by_tf.get(tf)
            tf_last_ts = self.last_fetch_ts_by_tf.get(tf, 0.0)
            if force_refresh or not cached or (now - tf_last_ts > ttl_seconds):
                # Tier 1 (Primary): Direct TradingView WebSocket (`OANDA:XAUUSD` / `FOREXCOM:XAUUSD`)
                tv_res = fetch_tradingview_gold_bars(tf, symbol=TV_DEFAULT_SYMBOL, limit=DEFAULT_BARS_LIMIT)
                if not tv_res:
                    tv_res = fetch_tradingview_gold_bars(tf, symbol="FOREXCOM:XAUUSD", limit=DEFAULT_BARS_LIMIT)
                if tv_res and tv_res.get("bars"):
                    live_bars = tv_res["bars"]
                    self.latest_spot_quote = tv_res.get("quote") or self.latest_spot_quote
                    self.basis_shift = 0.0
                    self.source = tv_res["source"]
                    self.last_fetch_ts = now
                    self.last_fetch_ts_by_tf[tf] = now
                    self.cached_bars_by_tf[tf] = live_bars
                    return {
                        "bars": [dict(b) for b in live_bars],
                        "source": self.source,
                        "timeframe": tf,
                        "live_quote": self.latest_spot_quote,
                    }

                # Tier 2 & 3 (Automatic Fallback): Swissquote Spot XAU/USD + Yahoo GC=F / Binance PAXGUSDT
                spot = fetch_live_spot_xauusd() if (now - self.last_fetch_ts > 8.0 or not self.latest_spot_quote) else self.latest_spot_quote
                if spot:
                    self.latest_spot_quote = spot

                live_bars = fetch_yahoo_gold_bars(tf)
                feed_name = "yahoo_gcf_fallback"
                if not live_bars:
                    live_bars = fetch_binance_gold_bars(tf, limit=DEFAULT_BARS_LIMIT)
                    feed_name = "binance_paxgusdt_fallback"

                if live_bars:
                    if self.latest_spot_quote:
                        raw_last = float(live_bars[-1]["c"])
                        self.basis_shift = round(float(self.latest_spot_quote["price"]) - raw_last, 2)
                        live_bars = _align_bars_to_spot(live_bars, self.latest_spot_quote)
                        feed_name = f"live_xauusd ({self.latest_spot_quote['source']} + {feed_name.split('_')[0]})"
                    self.source = feed_name
                    self.last_fetch_ts = now
                    self.last_fetch_ts_by_tf[tf] = now
                    self.cached_bars_by_tf[tf] = live_bars
                    return {
                        "bars": [dict(b) for b in live_bars],
                        "source": self.source,
                        "timeframe": tf,
                        "live_quote": self.latest_spot_quote,
                    }

            if cached:
                return {
                    "bars": [dict(b) for b in cached],
                    "source": self.source,
                    "timeframe": tf,
                    "live_quote": self.latest_spot_quote,
                }

        # Fallback (only when offline or when user clicks Replay controls)
        self.source = "offline_replay" if self.replay_mode else "offline_fallback (no internet)"
        end_idx = max(60, min(len(self.fixture_bars), self.replay_cursor))
        sliced_m5 = [dict(b) for b in self.fixture_bars[:end_idx]]
        bars = resample_bars(sliced_m5, tf)
        return {
            "bars": bars,
            "source": self.source,
            "timeframe": tf,
            "live_quote": None,
        }

    def get_mtf_bars(
        self,
        primary_tf: str = "M5",
        primary_bars: Optional[List[Dict[str, Any]]] = None,
        offset_usd: float = 0.0,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return offset-adjusted bars for M5, M15, H1, and H4 for Multi-Timeframe analysis."""
        p_tf = primary_tf.upper()
        result: Dict[str, List[Dict[str, Any]]] = {}
        is_offline = self.replay_mode or self.source.startswith("offline")
        end_idx = max(60, min(len(self.fixture_bars), self.replay_cursor))
        sliced_m5 = [dict(b) for b in self.fixture_bars[:end_idx]]
        ttl_map = {"M5": 25.0, "M15": 60.0, "H1": 120.0, "H4": 180.0}

        for tf in ("M5", "M15", "H1", "H4"):
            if tf == p_tf and primary_bars is not None:
                result[tf] = primary_bars
                continue
            if is_offline:
                raw = resample_bars(sliced_m5, tf)
            else:
                feed = self.get_bars(tf, force_refresh=False, ttl_seconds=ttl_map.get(tf, 60.0))
                raw = feed["bars"]
            result[tf] = [
                {
                    "t": int(b["t"]),
                    "o": round(float(b["o"]) + offset_usd, 2),
                    "h": round(float(b["h"]) + offset_usd, 2),
                    "l": round(float(b["l"]) + offset_usd, 2),
                    "c": round(float(b["c"]) + offset_usd, 2),
                    "v": int(b.get("v") or 0),
                }
                for b in raw
            ]
        return result

    def get_macro_correlation(
        self,
        gold_bars: List[Dict[str, Any]],
        timeframe: str = "M5",
    ) -> Dict[str, Any]:
        """Fetch DXY (USD Index) & US10Y Treasury Yield and detect SMT Divergence against XAU/USD."""
        now = time.time()
        is_offline = self.replay_mode or self.source.startswith("offline")

        dxy_bars: Optional[List[Dict[str, Any]]] = None
        us10y_bars: Optional[List[Dict[str, Any]]] = None
        macro_source = "offline_macro_model"

        if not is_offline:
            if self.cached_macro and (now - self.last_macro_ts < 90.0):
                dxy_bars = self.cached_macro.get("_dxy_bars")
                us10y_bars = self.cached_macro.get("_us10y_bars")
                macro_source = str(self.cached_macro.get("source") or "live_macro")
            else:
                # 1. Try Yahoo DXY (DX-Y.NYB) & US10Y (^TNX)
                dxy_bars = fetch_yahoo_gold_bars(timeframe=timeframe, symbol="DX-Y.NYB", timeout=3)
                us10y_bars = fetch_yahoo_gold_bars(timeframe="M15", symbol="^TNX", timeout=3)
                macro_source = "yahoo_dxy_us10y"

                # 2. Fallback to Binance EURUSDT inverted (57.6% weight of DXY, 24/7 real-time)
                if not dxy_bars:
                    try:
                        b_tf = TF_TO_BINANCE.get(timeframe.upper(), "5m")
                        eur_url = f"https://api.binance.com/api/v3/klines?symbol=EURUSDT&interval={b_tf}&limit=60"
                        rows = _http_get_json(eur_url, timeout=3)
                        if isinstance(rows, list) and len(rows) >= 20:
                            dxy_bars = [
                                {
                                    "t": int(r[0]) // 1000,
                                    "o": round(120.0 / float(r[1]), 3),
                                    "h": round(120.0 / float(r[3]), 3),
                                    "l": round(120.0 / float(r[2]), 3),
                                    "c": round(120.0 / float(r[4]), 3),
                                }
                                for r in rows
                            ]
                            macro_source = "live_usd_index (binance_eur_inv)"
                    except Exception:
                        pass

                self.last_macro_ts = now
                self.cached_macro = {
                    "_dxy_bars": dxy_bars,
                    "_us10y_bars": us10y_bars,
                    "source": macro_source,
                }

        # Deterministic fallback when offline or in replay mode
        g_recent = gold_bars[-35:] if len(gold_bars) >= 35 else gold_bars
        if not dxy_bars or len(dxy_bars) < 15:
            base_g = float(g_recent[0]["c"]) if g_recent else 2650.0
            dxy_bars = []
            for idx, b in enumerate(g_recent):
                rel = (float(b["c"]) - base_g) / max(1.0, base_g)
                # Slightly dampen last 8 bars so SMT behavior can manifest on sharp sweeps
                damp = 0.65 if idx >= len(g_recent) - 8 else 1.0
                c_val = round(104.25 - rel * 85.0 * damp, 3)
                span = max(0.03, (float(b["h"]) - float(b["l"])) / max(1.0, base_g) * 65.0)
                dxy_bars.append({
                    "t": int(b["t"]),
                    "o": c_val,
                    "h": round(c_val + span * 0.5, 3),
                    "l": round(c_val - span * 0.5, 3),
                    "c": c_val,
                })

        dxy_curr = float(dxy_bars[-1]["c"])
        dxy_prev = float(dxy_bars[-min(20, len(dxy_bars))]["c"])
        dxy_chg_pct = round(((dxy_curr - dxy_prev) / max(0.01, dxy_prev)) * 100.0, 2)
        if dxy_chg_pct >= 0.08:
            dxy_bias = "BULLISH_USD"
        elif dxy_chg_pct <= -0.08:
            dxy_bias = "BEARISH_USD"
        else:
            dxy_bias = "NEUTRAL"

        if us10y_bars and len(us10y_bars) >= 5:
            y_curr = round(float(us10y_bars[-1]["c"]), 3)
            y_prev = float(us10y_bars[-min(12, len(us10y_bars))]["c"])
            y_chg_pct = round(((y_curr - y_prev) / max(0.01, y_prev)) * 100.0, 2)
        else:
            y_curr = round(4.28 - (dxy_chg_pct * 0.15), 3)
            y_chg_pct = round(dxy_chg_pct * 0.6, 2)

        y_bias = "RISING_YIELD" if y_chg_pct >= 0.12 else "FALLING_YIELD" if y_chg_pct <= -0.12 else "STABLE"

        # SMT Divergence Detection between Gold and DXY (comparing prior 20 bars vs recent 10 bars)
        smt_status = "NONE"
        smt_bias = "NEUTRAL"
        smt_summary_th = "ทองคำและดอลลาร์ (DXY) เคลื่อนไหวสอดคล้องตามกลไกปกติ"

        if len(g_recent) >= 18 and len(dxy_bars) >= 18:
            g_prior = g_recent[-18:-6]
            g_last = g_recent[-6:]
            d_prior = dxy_bars[-18:-6]
            d_last = dxy_bars[-6:]

            g_prior_low = min(float(x["l"]) for x in g_prior)
            g_last_low = min(float(x["l"]) for x in g_last)
            g_prior_high = max(float(x["h"]) for x in g_prior)
            g_last_high = max(float(x["h"]) for x in g_last)

            d_prior_high = max(float(x["h"]) for x in d_prior)
            d_last_high = max(float(x["h"]) for x in d_last)
            d_prior_low = min(float(x["l"]) for x in d_prior)
            d_last_low = min(float(x["l"]) for x in d_last)

            if g_last_low < g_prior_low and d_last_high <= d_prior_high:
                smt_status = "BULLISH_SMT_DIVERGENCE"
                smt_bias = "BULLISH"
                smt_summary_th = "Bullish SMT Divergence: ทองคำทำ Low ใหม่ แต่ DXY ไม่ทำ High ใหม่ (สัญญาณรายใหญ่ทุบกวาด SL หลอก หนุนฝั่ง BUY)"
            elif g_last_high > g_prior_high and d_last_low >= d_prior_low:
                smt_status = "BEARISH_SMT_DIVERGENCE"
                smt_bias = "BEARISH"
                smt_summary_th = "Bearish SMT Divergence: ทองคำทำ High ใหม่ แต่ DXY ไม่ทำ Low ใหม่ (สัญญาณลากกวาด SL หลอก หนุนฝั่ง SELL)"

        if smt_bias != "NEUTRAL":
            macro_gold_bias = smt_bias
        elif dxy_bias == "BEARISH_USD":
            macro_gold_bias = "BULLISH"
            smt_summary_th = f"ดอลลาร์อ่อนค่า ({dxy_chg_pct:+.2f}%) เป็นแรงส่งหนุนทองคำฝั่ง BUY"
        elif dxy_bias == "BULLISH_USD":
            macro_gold_bias = "BEARISH"
            smt_summary_th = f"ดอลลาร์แข็งค่า ({dxy_chg_pct:+.2f}%) กดดันทองคำฝั่ง SELL"
        else:
            macro_gold_bias = "NEUTRAL"

        return {
            "source": macro_source,
            "dxy_price": round(dxy_curr, 2),
            "dxy_change_pct": dxy_chg_pct,
            "dxy_bias": dxy_bias,
            "us10y_yield": y_curr,
            "us10y_change_pct": y_chg_pct,
            "us10y_bias": y_bias,
            "smt_divergence": smt_status,
            "macro_gold_bias": macro_gold_bias,
            "summary_th": smt_summary_th,
        }

    def ingest_live_price(
        self,
        timeframe: str,
        price: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        ts: Optional[int] = None,
        offset_usd: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """Update the active bar in `cached_bars_by_tf` with a real live price tick.
        Returns info including `new_bar_closed=True` when the timeframe rolls over into a new bar."""
        tf = timeframe.upper()
        bars = self.cached_bars_by_tf.get(tf)
        if not bars:
            return None

        tf_sec = TF_SECONDS.get(tf, 300)
        now_sec = int(ts or time.time())
        current_slot = (now_sec // tf_sec) * tf_sec
        adj_price = round(float(price) + offset_usd, 2)

        last_bar = bars[-1]
        new_bar_closed = False

        if current_slot > int(last_bar["t"]):
            # Previous bar just closed! Start a new forming bar
            new_bar = {
                "t": current_slot,
                "o": adj_price,
                "h": adj_price,
                "l": adj_price,
                "c": adj_price,
                "v": 1,
            }
            bars.append(new_bar)
            if len(bars) > 2000:
                bars.pop(0)
            last_bar = new_bar
            new_bar_closed = True
        else:
            last_bar["c"] = adj_price
            last_bar["h"] = round(max(float(last_bar["h"]), adj_price), 2)
            last_bar["l"] = round(min(float(last_bar["l"]), adj_price), 2)
            last_bar["v"] = int(last_bar.get("v") or 0) + 1

        spread_pts = None
        if bid is not None and ask is not None:
            spread_pts = int(round(max(0.05, ask - bid) * 100))

        return {
            "bar": dict(last_bar),
            "bid": round(bid + offset_usd, 2) if bid is not None else adj_price,
            "ask": round(ask + offset_usd, 2) if ask is not None else adj_price,
            "spread_points": spread_pts,
            "new_bar_closed": new_bar_closed,
            "source": self.source,
        }

    def step_replay(self, steps: int = 1) -> Dict[str, Any]:
        """Advance or rewind the replay cursor by `steps` bars."""
        self.replay_mode = True
        self.replay_cursor += steps
        if self.replay_cursor > len(self.fixture_bars):
            self.replay_cursor = 225
        elif self.replay_cursor < 80:
            self.replay_cursor = 80
        return self.get_bars("M5")

    def reset_replay(self, start_cursor: int = 232) -> Dict[str, Any]:
        """Jump to right before the SMC setup forms so the user can watch it trigger bar-by-bar."""
        self.replay_mode = True
        self.replay_cursor = max(80, min(len(self.fixture_bars), start_cursor))
        return self.get_bars("M5")

    def simulate_live_tick(self, current_bar: Dict[str, Any], offset_usd: float = 0.0) -> Dict[str, Any]:
        """Used ONLY when `replay_mode` is active."""
        base_c = float(current_bar["c"]) + offset_usd
        delta = round(self._rng.uniform(-0.18, 0.20), 2)
        tick_price = round(base_c + delta, 2)
        hi = round(max(float(current_bar["h"]) + offset_usd, tick_price), 2)
        lo = round(min(float(current_bar["l"]) + offset_usd, tick_price), 2)
        return {
            "t": int(current_bar["t"]),
            "o": round(float(current_bar["o"]) + offset_usd, 2),
            "h": hi,
            "l": lo,
            "c": tick_price,
            "v": int(current_bar.get("v", 500)) + self._rng.randint(1, 8),
        }


async def stream_binance_gold_ws(on_trade: Callable[[float, int], Any]) -> None:
    """Pure-Python asyncio RFC 6455 WebSocket client for wss://stream.binance.com:9443/ws/paxgusdt@trade."""
    while True:
        writer = None
        try:
            reader, writer = await asyncio.open_connection(
                BINANCE_WS_HOST,
                BINANCE_WS_PORT,
                ssl=SSL_CTX,
                server_hostname=BINANCE_WS_HOST,
            )
            ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = (
                f"GET {BINANCE_WS_PATH} HTTP/1.1\r\n"
                f"Host: {BINANCE_WS_HOST}:{BINANCE_WS_PORT}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"User-Agent: {UA}\r\n\r\n"
            )
            writer.write(req.encode("utf-8"))
            await writer.drain()

            status_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if b"101" not in status_line:
                writer.close()
                await asyncio.sleep(5.0)
                continue
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break

            while True:
                head = await asyncio.wait_for(reader.readexactly(2), timeout=30.0)
                opcode = head[0] & 0x0F
                masked = bool(head[1] & 0x80)
                length = head[1] & 0x7F
                if length == 126:
                    length = struct.unpack("!H", await reader.readexactly(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", await reader.readexactly(8))[0]
                mask_key = await reader.readexactly(4) if masked else b""
                payload = await reader.readexactly(length)
                if masked:
                    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

                if opcode == 0x8:  # Close
                    break
                if opcode == 0x9:  # Ping -> send masked Pong
                    pong = bytearray([0x8A, 0x80, 0, 0, 0, 0])
                    writer.write(bytes(pong))
                    await writer.drain()
                    continue
                if opcode == 0x1:  # Text frame
                    doc = json.loads(payload.decode("utf-8", "replace"))
                    if "p" in doc:
                        trade_price = float(doc["p"])
                        trade_ts = int(doc.get("T") or (time.time() * 1000)) // 1000
                        await on_trade(trade_price, trade_ts)
        except asyncio.CancelledError:
            if writer:
                writer.close()
            raise
        except Exception:
            if writer:
                try:
                    writer.close()
                except Exception:
                    pass
            await asyncio.sleep(3.0)


async def stream_tradingview_gold_ws(
    on_tick: Callable[[float, Optional[float], Optional[float], int], Any],
    symbol: str = TV_DEFAULT_SYMBOL,
) -> None:
    """Primary real-time WebSocket stream directly from TradingView (`wss://data.tradingview.com/socket.io/websocket`)."""
    while True:
        writer = None
        try:
            reader, writer = await asyncio.open_connection(
                TV_WS_HOST,
                TV_WS_PORT,
                ssl=SSL_CTX,
                server_hostname=TV_WS_HOST,
            )
            ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = (
                f"GET {TV_WS_PATH} HTTP/1.1\r\n"
                f"Host: {TV_WS_HOST}\r\n"
                f"Origin: https://www.tradingview.com\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"User-Agent: {UA}\r\n\r\n"
            )
            writer.write(req.encode("utf-8"))
            await writer.drain()

            status_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if b"101" not in status_line:
                writer.close()
                await asyncio.sleep(6.0)
                continue
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break

            qs = _tv_random_session("qs")
            init_batch = (
                _tv_pack_msg("set_auth_token", ["unauthorized_user_token"])
                + _tv_pack_msg("quote_create_session", [qs])
                + _tv_pack_msg("quote_set_fields", [qs, "lp", "bid", "ask", "ch", "chp", "volume"])
                + _tv_pack_msg("quote_add_symbols", [qs, symbol])
            )
            writer.write(_build_masked_ws_frame(init_batch))
            await writer.drain()

            last_lp: Optional[float] = None
            last_bid: Optional[float] = None
            last_ask: Optional[float] = None

            while True:
                head = await asyncio.wait_for(reader.readexactly(2), timeout=35.0)
                opcode = head[0] & 0x0F
                masked = bool(head[1] & 0x80)
                length = head[1] & 0x7F
                if length == 126:
                    length = struct.unpack("!H", await reader.readexactly(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", await reader.readexactly(8))[0]
                mask_key = await reader.readexactly(4) if masked else b""
                payload = await reader.readexactly(length)
                if masked:
                    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    writer.write(_build_masked_ws_frame("", opcode=0x8A))
                    await writer.drain()
                    continue
                if opcode == 0x1:
                    raw_text = payload.decode("utf-8", "replace")
                    for pkt in _tv_unpack_msgs(raw_text):
                        if pkt.startswith("~h~"):
                            hb = f"~m~{len(pkt)}~m~{pkt}"
                            writer.write(_build_masked_ws_frame(hb))
                            await writer.drain()
                            continue
                        if not pkt.startswith("{"):
                            continue
                        doc = json.loads(pkt)
                        if doc.get("m") == "qsd":
                            params = doc.get("p") or []
                            if len(params) >= 2 and isinstance(params[1], dict):
                                v = params[1].get("v") or {}
                                if v.get("lp") is not None:
                                    last_lp = float(v["lp"])
                                if v.get("bid") is not None:
                                    last_bid = float(v["bid"])
                                if v.get("ask") is not None:
                                    last_ask = float(v["ask"])
                                if last_lp is not None:
                                    await on_tick(last_lp, last_bid, last_ask, int(time.time()))
        except asyncio.CancelledError:
            if writer:
                writer.close()
            raise
        except Exception:
            if writer:
                try:
                    writer.close()
                except Exception:
                    pass
            await asyncio.sleep(4.0)
