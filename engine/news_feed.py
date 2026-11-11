#!/usr/bin/env python3
"""USD Macro News & Gold Trading Session Monitor (news_feed.py).

Features:
  - Identifies active XAU/USD Trading Session (Asia, London, London-NY Overlap, NY, Rollover)
  - Fetches weekly economic calendar from ForexFactory free JSON feed (with 15-min cache & certifi SSL)
  - Flags High-Impact (Red Folder) USD events (CPI, NFP, FOMC, PMI, Powell, etc.) within +-30 minutes
"""

import datetime
import json
import os
import ssl
import time
import urllib.request
from typing import Any, Dict, List, Optional

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    if os.path.exists("/etc/ssl/cert.pem"):
        SSL_CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    else:
        SSL_CTX = ssl.create_default_context()

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

HIGH_IMPACT_KEYWORDS = (
    "CPI",
    "Non-Farm",
    "NFP",
    "FOMC",
    "Federal Funds Rate",
    "ISM",
    "PMI",
    "PPI",
    "Powell",
    "Retail Sales",
    "GDP",
    "Unemployment",
    "Core PCE",
    "JOLTS",
)

_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "events": []}


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def get_gold_session(now_utc: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Return active Gold trading session and liquidity rating in UTC and Thai Time (UTC+7)."""
    dt = now_utc or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    thai_dt = dt.astimezone(datetime.timezone(datetime.timedelta(hours=7)))

    if dt.weekday() == 5 or (dt.weekday() == 4 and dt.hour >= 22) or (dt.weekday() == 6 and dt.hour < 22):
        return {
            "session": "WEEKEND_CLOSED",
            "label_th": "ตลาดทองคำปิดสุดสัปดาห์ (เปิดวันจันทร์ 05:00 น.)",
            "liquidity": "CLOSED",
            "utc_time": dt.strftime("%Y-%m-%d %H:%M UTC"),
            "bkk_time": thai_dt.strftime("%Y-%m-%d %H:%M น."),
            "is_rollover_danger": False,
        }

    minutes = dt.hour * 60 + dt.minute
    if 12 * 60 + 30 <= minutes < 16 * 60 + 30:
        return {
            "session": "LONDON_NY_OVERLAP",
            "label_th": "London + New York Overlap (สภาพคล่องสูงสุด)",
            "liquidity": "PEAK",
            "utc_time": dt.strftime("%Y-%m-%d %H:%M UTC"),
            "bkk_time": thai_dt.strftime("%Y-%m-%d %H:%M น."),
            "is_rollover_danger": False,
        }
    if 7 * 60 <= minutes < 12 * 60 + 30:
        return {
            "session": "LONDON",
            "label_th": "ตลาด London (วิ่งตามโครงสร้าง SMC ชัดเจน)",
            "liquidity": "HIGH",
            "utc_time": dt.strftime("%Y-%m-%d %H:%M UTC"),
            "bkk_time": thai_dt.strftime("%Y-%m-%d %H:%M น."),
            "is_rollover_danger": False,
        }
    if 16 * 60 + 30 <= minutes < 21 * 60:
        return {
            "session": "NEW_YORK",
            "label_th": "ตลาด New York ช่วงบ่าย",
            "liquidity": "MEDIUM_HIGH",
            "utc_time": dt.strftime("%Y-%m-%d %H:%M UTC"),
            "bkk_time": thai_dt.strftime("%Y-%m-%d %H:%M น."),
            "is_rollover_danger": False,
        }
    if 21 * 60 <= minutes <= 23 * 60 + 10:
        return {
            "session": "ROLLOVER_GAP",
            "label_th": "ช่วงปิดเปลี่ยนวัน (Rollover - Spread ถ่างสูง ห้ามเข้าออเดอร์)",
            "liquidity": "LOW_DANGER",
            "utc_time": dt.strftime("%Y-%m-%d %H:%M UTC"),
            "bkk_time": thai_dt.strftime("%Y-%m-%d %H:%M น."),
            "is_rollover_danger": True,
        }
    return {
        "session": "ASIAN",
        "label_th": "ตลาด Asia (เคลื่อนไหวในกรอบ / สะสมสภาพคล่อง)",
        "liquidity": "MODERATE",
        "utc_time": dt.strftime("%Y-%m-%d %H:%M UTC"),
        "bkk_time": thai_dt.strftime("%Y-%m-%d %H:%M น."),
        "is_rollover_danger": False,
    }


def fetch_ff_calendar(timeout: int = 6) -> List[Dict[str, Any]]:
    """Fetch ForexFactory weekly JSON calendar with 15-minute memory cache."""
    now_ts = time.time()
    if _CACHE["events"] and (now_ts - _CACHE["fetched_at"] < 900):
        return _CACHE["events"]

    try:
        req = urllib.request.Request(
            FF_CALENDAR_URL,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(raw, list):
            _CACHE["fetched_at"] = now_ts
            _CACHE["events"] = raw
            return raw
    except Exception:
        pass
    return _CACHE["events"]


def parse_event_utc(date_str: str) -> Optional[datetime.datetime]:
    if not date_str:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(date_str).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def evaluate_news_window(
    now_utc: Optional[datetime.datetime] = None,
    blackout_minutes: int = 30,
    events_override: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Check for upcoming or recent USD Red-Folder (High Impact) news events."""
    dt = now_utc or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)

    raw_events = events_override if events_override is not None else fetch_ff_calendar()
    red_events: List[Dict[str, Any]] = []
    active_blackout_events: List[Dict[str, Any]] = []

    for ev in raw_events:
        country = str(ev.get("country") or "").upper()
        impact = str(ev.get("impact") or "").capitalize()
        title = str(ev.get("title") or "").strip()
        if country != "USD":
            continue
        is_red = impact == "High" or any(k.lower() in title.lower() for k in HIGH_IMPACT_KEYWORDS)
        if not is_red:
            continue

        ev_dt = parse_event_utc(str(ev.get("date") or ""))
        if not ev_dt:
            continue

        diff_min = round((ev_dt - dt).total_seconds() / 60.0, 1)
        if -180 <= diff_min <= 1440:
            thai_ev = ev_dt.astimezone(datetime.timezone(datetime.timedelta(hours=7)))
            item = {
                "title": title,
                "country": "USD",
                "impact": "High",
                "time_utc": ev_dt.strftime("%Y-%m-%d %H:%M UTC"),
                "time_bkk": thai_ev.strftime("%H:%M น."),
                "minutes_away": diff_min,
                "forecast": str(ev.get("forecast") or "-"),
                "previous": str(ev.get("previous") or "-"),
            }
            red_events.append(item)
            if abs(diff_min) <= blackout_minutes:
                active_blackout_events.append(item)

    red_events.sort(key=lambda x: abs(x["minutes_away"]))
    session_info = get_gold_session(dt)

    return {
        "session": session_info,
        "blackout_minutes": blackout_minutes,
        "in_news_blackout": len(active_blackout_events) > 0,
        "blackout_events": active_blackout_events,
        "upcoming_usd_red_news": red_events[:6],
    }
