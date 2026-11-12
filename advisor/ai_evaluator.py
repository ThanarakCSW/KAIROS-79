#!/usr/bin/env python3
"""Automated AI Master Chart Analyst & Risk Advisor (ai_evaluator.py).

Full Gemini Chart Analysis + 15,000-Path Monte Carlo Integration:
  - Sends recent raw OHLCV candlesticks + all detected SMC zones (OB, FVG, BOS/CHoCH, Sweeps,
    S&R, Liquidity Pools) + EMA/ATR + 15,000-path simulation stats to Google Gemini
  - Allows Gemini to analyze the chart directly and decide/refine the Trade Setup:
    (`side`, `status`, `entry`, `stop`, `target`, `confluence_score`, `confluence_factors`,
     `scenario_b_sweep_reentry`, `scenario_c_invalidation`)
  - Includes an intelligent per-bar cache so Gemini isn't spammed on unchanged bars,
    plus a deterministic fallback when no API key is configured yet
"""

import datetime
import hashlib
import json
import os
import ssl
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from engine.structure import r2

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    if os.path.exists("/etc/ssl/cert.pem"):
        SSL_CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    else:
        SSL_CTX = ssl.create_default_context()

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "smc_risk_advisor.md")

# Cache Gemini responses keyed by (timeframe, last_bar_time, close_price_bucket, api_key_hash)
_GEMINI_CACHE: Dict[str, Dict[str, Any]] = {}


def load_system_prompt() -> str:
    if os.path.exists(PROMPT_PATH):
        with open(PROMPT_PATH, encoding="utf-8") as fh:
            return fh.read()
    return "You are an institutional XAUUSD SMC & Price Action Chart Analyst."


def _validate_gemini_trade_decision(
    decision: Dict[str, Any],
    fallback_setup: Dict[str, Any],
    atr_val: float,
    spread_usd: float,
) -> Optional[Dict[str, Any]]:
    """Validate Gemini's proposed price levels so invalid/inverted SL or TP never break the engine."""
    try:
        side = str(decision.get("side") or fallback_setup["side"]).strip().upper()
        if side not in ("BUY", "SELL"):
            return None
        entry = float(decision.get("entry") or fallback_setup["entry"])
        stop = float(decision.get("stop") or fallback_setup["stop"])
        target = float(decision.get("target") or fallback_setup["target"])
        current_price = float(fallback_setup.get("current_price") or entry)

        if side == "BUY":
            if not (stop < entry < target):
                return None
            # Reject if entry is above current price (must be a forward-looking BUY LIMIT at or below current price)
            if current_price > 0 and entry > current_price + (atr_val * 0.10):
                return None
        else:
            if not (target < entry < stop):
                return None
            # Reject if entry is below current price (must be a forward-looking SELL LIMIT at or above current price)
            if current_price > 0 and entry < current_price - (atr_val * 0.10):
                return None

        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk < 0.25 or risk > atr_val * 8.0:
            return None

        rr = round(reward / max(0.01, risk), 2)
        scen_b = float(
            decision.get("scenario_b_sweep_reentry")
            or (stop - atr_val * 0.7 if side == "BUY" else stop + atr_val * 0.7)
        )
        scen_c = float(
            decision.get("scenario_c_invalidation")
            or (stop - atr_val * 0.5 if side == "BUY" else stop + atr_val * 0.5)
        )
        conf_score = int(decision.get("confluence_score") or fallback_setup.get("confluence_score", 70))
        conf_score = max(10, min(100, conf_score))
        factors = decision.get("confluence_factors")
        if not isinstance(factors, list) or not factors:
            factors = list(fallback_setup.get("confluence_factors", []))
        else:
            factors = [str(f) for f in factors[:6]]

        status = str(decision.get("status") or fallback_setup.get("status", "ACTIVE_IN_ZONE")).upper()
        if status not in ("ACTIVE_IN_ZONE", "WAITING_PULLBACK"):
            status = "ACTIVE_IN_ZONE"

        z_low = float(decision.get("zone_low") or min(entry, (entry + stop) / 2.0))
        z_high = float(decision.get("zone_high") or max(entry, (entry + stop) / 2.0))

        return {
            "symbol": "XAUUSD",
            "side": side,
            "order_type": f"{side} LIMIT",
            "is_advance_entry": True,
            "distance_to_entry_usd": r2(abs(current_price - entry)),
            "current_price": r2(current_price),
            "bias": "BULLISH" if side == "BUY" else "BEARISH",
            "status": status,
            "entry": r2(entry),
            "stop": r2(stop),
            "tp1": r2(entry + risk * 1.0) if side == "BUY" else r2(entry - risk * 1.0),
            "target": r2(target),
            "risk_usd": r2(risk),
            "reward_usd": r2(reward),
            "reward_risk": rr,
            "sl_atr_ratio": round(risk / max(0.01, atr_val), 2),
            "spread_included_usd": r2(spread_usd),
            "zone_low": r2(min(z_low, z_high)),
            "zone_high": r2(max(z_low, z_high)),
            "confluence_score": conf_score,
            "confluence_factors": factors,
            "scenario_b_sweep_reentry": r2(scen_b),
            "scenario_c_invalidation": r2(scen_c),
            "decided_by": "gemini_ai",
        }
    except (TypeError, ValueError):
        return None


def call_gemini_full_chart_analyst(
    bars: List[Dict[str, Any]],
    structure: Dict[str, Any],
    smc: Dict[str, Any],
    simulation: Dict[str, Any],
    news: Dict[str, Any],
    config: Dict[str, Any],
    api_key: str,
    model: str = "gemini-2.5-flash",
    timeout: int = 14,
    mtf: Optional[Dict[str, Any]] = None,
    macro: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Send raw recent candles + institutional levels + MTF + Macro DXY/US10Y + RSI Divergence + 15,000-path simulation to Gemini."""
    sys_prompt = load_system_prompt()
    recent_bars = [
        {
            "t": b["t"],
            "o": b["o"],
            "h": b["h"],
            "l": b["l"],
            "c": b["c"],
            "v": b.get("v", 0),
        }
        for b in bars[-40:]
    ]

    chart_package = {
        "symbol": "XAUUSD",
        "timeframe": structure["timeframe"],
        "trading_style": smc.get("trading_style") or config.get("trading_style", "intraday"),
        "min_win_prob_pct": config.get("min_win_prob_pct", 50.0),
        "current_price": structure["current_price"],
        "atr14": structure["atr14"],
        "broker_spread_points": config.get("broker_spread_points", 25),
        "trend_and_ema": structure["trend"],
        "volume_and_market_flow": structure.get("volume_flow") or {},
        "scalping_signals": structure.get("scalping") or {},
        "institutional_levels": structure.get("institutional_levels") or {},
        "rsi_divergence": structure.get("rsi_divergence") or {},
        "macro_correlation_dxy_us10y": macro or {},
        "historical_candle_behavior": structure.get("candle_behavior") or {},
        "multi_timeframe_confluence": mtf or {},
        "recent_swings": structure["swings"]["all"][-10:],
        "sr_zones": structure["sr_zones"][:6],
        "smc_structures": {
            "structure_breaks": smc["structure_breaks"][-6:],
            "liquidity_sweeps": smc["liquidity_sweeps"][-5:],
            "unmitigated_order_blocks": smc["order_blocks"][:5],
            "unmitigated_fvgs": smc["fvgs"][-5:],
            "liquidity_pools": smc["liquidity_pools"][:6],
            "engine_baseline_setup": smc["trade_setup"],
        },
        "monte_carlo_15000_baseline": {
            "replays": simulation["replays"],
            "bars_ahead": simulation["bars_ahead"],
            "probabilities": simulation["probabilities"],
            "expected_value_r": simulation["expected_value_r"],
            "terminal_price_percentiles": simulation["terminal_price"],
            "scanner_status": simulation.get("scanner_status"),
            "next_watch_zone": simulation.get("next_watch_zone"),
        },
        "news_and_session": {
            "session": news.get("session"),
            "in_news_blackout": news.get("in_news_blackout"),
            "upcoming_usd_red_news": news.get("upcoming_usd_red_news", [])[:3],
        },
        "recent_40_candles": recent_bars,
    }

    user_prompt = (
        "วิเคราะห์กราฟ XAUUSD จากข้อมูลแท่งเทียน 40 แท่งล่าสุด พฤติกรรมแท่งเทียนย้อนหลัง 60 แท่ง (historical_candle_behavior) "
        "ความสอดคล้องและแนวรับ-ต้านจากหลายไทม์เฟรม M5, M15, H1, H4 (multi_timeframe_confluence) "
        "โซน SMC ที่ยังไม่ถูกใช้งาน (Unmitigated OB, FVG) เส้น EMA 20/50/200 ค่า ATR(14) และผลจำลองกราฟจากสถิติจริง 15,000 เส้นทางด้านล่างนี้\n"
        "กฎเหล็กสำคัญ:\n"
        "1. ห้ามเมคตัวเลขหรือเร่งให้เปิดออเดอร์เด็ดขาด! อิงจากตัวเลขและสถิติจริงเท่านั้น หากโอกาสถึง TP ไม่เกิน 50% ให้สั่ง 'รอจังหวะ (WAIT)' ทันที\n"
        "2. ต้องบอกจุดเข้าล่วงหน้าเท่านั้น (BUY LIMIT ต้องอยู่ต่ำกว่าราคาปัจจุบันที่โซนรับที่ยังไม่ถูกใช้ / SELL LIMIT ต้องอยู่สูงกว่าราคาปัจจุบันที่โซนต้านที่ยังไม่ถูกใช้) ห้ามบอกจุดเข้าที่ราคาวิ่งผ่านไปแล้วเด็ดขาด\n"
        "ให้คุณตัดสินใจแผนเทรดที่ดีที่สุดด้วยตัวเอง แล้วตอบกลับเป็น JSON เท่านั้นตามโครงสร้างนี้:\n"
        "{\n"
        '  "trade_decision": {\n'
        '    "side": "BUY หรือ SELL",\n'
        '    "status": "ACTIVE_IN_ZONE หรือ WAITING_PULLBACK",\n'
        '    "entry": ตัวเลขราคาจุดเข้า,\n'
        '    "stop": ตัวเลขราคา SL (ต้องวางพ้นขอบโซนและปลอดภัยจาก ATR/Spread),\n'
        '    "target": ตัวเลขราคา TP (ต้องสอดคล้องกับแนวรับ-ต้าน MTF/Liquidity และได้ R:R >= 1.5),\n'
        '    "zone_low": ตัวเลขขอบล่างโซนเข้า,\n'
        '    "zone_high": ตัวเลขขอบบนโซนเข้า,\n'
        '    "scenario_b_sweep_reentry": ตัวเลขจุดดักเข้าไม้สองหากโดนกวาด SL แรก,\n'
        '    "scenario_c_invalidation": ตัวเลขราคาที่หากปิดแท่งทะลุต้องยกเลิกแผนทันที,\n'
        '    "confluence_score": ตัวเลข 0-100,\n'
        '    "confluence_factors": ["ปัจจัยที่ 1", "ปัจจัยที่ 2", "ปัจจัยที่ 3"]\n'
        "  },\n"
        '  "headline": "สรุปหัวข้อสัญญาณและเหตุผลหลัก 1 ประโยค",\n'
        '  "confluence_summary": "อธิบายการวิเคราะห์กราฟ พฤติกรรมแท่งเทียนย้อนหลัง แนวรับ-ต้านหลายไทม์เฟรม (M5/M15/H1/H4) โซน SMC และความน่าจะเป็นจาก 15,000 เส้นทางอย่างละเอียด",\n'
        '  "blind_spots": ["จุดบอดหรือความเสี่ยงข้อ 1", "จุดบอดข้อ 2"],\n'
        '  "scenario_a_detail": "แผนการเข้าเทรดหลักใน MT5 และเงื่อนไขแท่งเทียนยืนยัน",\n'
        '  "scenario_b_detail": "แผนรับมือหากเกิด Liquidity Sweep กวาดหางไปหาแนวรับ-ต้าน HTF ถัดไปก่อนวิ่งจริง",\n'
        '  "scenario_c_detail": "จุด Invalidation ที่ต้องยกเลิกแผนทันที"\n'
        "}\n\n"
        "ข้อมูลกราฟทั้งหมด:\n"
        + json.dumps(chart_package, ensure_ascii=False)
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {
        "system_instruction": {"parts": [{"text": sys_prompt}]},
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
        text = doc["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def build_deterministic_advisory(
    structure: Dict[str, Any],
    smc: Dict[str, Any],
    simulation: Dict[str, Any],
    risk_ticket: Dict[str, Any],
    news: Dict[str, Any],
    mtf: Optional[Dict[str, Any]] = None,
    macro: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Synthesize a comprehensive Thai Risk Advisory strictly from the pre-computed JSON."""
    trend = structure["trend"]
    setup = smc["trade_setup"]
    probs = simulation["probabilities"]
    ev_r = simulation["expected_value_r"]
    scenarios = simulation["scenarios"]
    session = news.get("session", {})
    cb = structure.get("candle_behavior") or {}
    dr = cb.get("dealing_range") or {}
    inst = structure.get("institutional_levels") or {}
    rsi_div = structure.get("rsi_divergence") or {}

    verdict = risk_ticket["verdict"]
    side = risk_ticket["side"]
    win_prob_passed = risk_ticket.get("win_prob_passed", True)
    trading_style = risk_ticket.get("trading_style", "intraday")
    style_label = "⚡ โหมดเทรดสั้น (Scalping)" if trading_style == "scalping" else "🎯 โหมดปกติ (Intraday SMC)"
    entry = risk_ticket["entry_mt5"]
    stop = risk_ticket["stop_mt5"]
    tp1 = risk_ticket.get("tp1_mt5") or entry
    target = risk_ticket["target_mt5"]
    lot = risk_ticket["lot_size"]
    rr = risk_ticket["reward_risk"]
    conf_score = setup["confluence_score"]
    factors = setup.get("confluence_factors", [])
    watch_zone = risk_ticket.get("next_watch_zone") or simulation.get("next_watch_zone")
    watch_price = (
        watch_zone.get("watch_entry")
        if isinstance(watch_zone, dict)
        else watch_zone
        if isinstance(watch_zone, (int, float))
        else None
    )

    if not win_prob_passed:
        watch_str = f" (กำลังเฝ้าดักโซน {float(watch_price):.2f})" if watch_price else ""
        headline = (
            f"⏳ [{style_label}] รอจังหวะเข้าเทรด — โอกาสถึง TP ตอนนี้อยู่ที่ {probs['win_pct']:.1f}% (ยังไม่เกิน 50.0%){watch_str} ระบบกำลังสแกนหาจุดเข้าที่ดีกว่าอย่างต่อเนื่อง"
        )
    elif verdict == "PASS":
        headline = (
            f"[{style_label}] สัญญาณแนะนำ {side} XAUUSD (โอกาสถึง TP {probs['win_pct']:.1f}% > 50% | Confluence {conf_score}/100) — "
            f"จำลอง {simulation['replays']:,} เส้นทางผ่านเกณฑ์ แนะนำขนาดไม้ {lot:.2f} Lot"
        )
    elif verdict == "CAUTION":
        headline = (
            f"[{style_label}] สัญญาณแนะนำ {side} XAUUSD (โอกาสถึง TP {probs['win_pct']:.1f}% > 50% | Confluence {conf_score}/100) — "
            f"ควรรอแท่งเทียนยืนยันในโซนก่อนกด {lot:.2f} Lot ใน MT5"
        )
    else:
        headline = (
            f"[{style_label}] ชะลอการเข้าออเดอร์ {side} XAUUSD (สถานะ REJECT) — "
            f"เงื่อนไขความเสี่ยงหรือค่าคาดหวังทางสถิติยังไม่ผ่านเกณฑ์ปลอดภัย"
        )

    mtf_note = ""
    if mtf and mtf.get("timeframes"):
        tfs = mtf["timeframes"]
        tf_parts = [f"{k}:{v['bias']}" for k, v in tfs.items()]
        htf_sup = mtf.get("nearest_htf_support")
        htf_res = mtf.get("nearest_htf_resistance")
        sup_str = f"{htf_sup['mid']:.2f} ({htf_sup['timeframe']})" if htf_sup else "-"
        res_str = f"{htf_res['mid']:.2f} ({htf_res['timeframe']})" if htf_res else "-"
        mtf_note = (
            f" | มุมมองหลายไทม์เฟรม ({', '.join(tf_parts)}): {mtf.get('summary_th', '')} "
            f"[แนวรับใหญ่ HTF ใกล้สุด: {sup_str} | แนวต้านใหญ่ HTF ใกล้สุด: {res_str}]"
        )

    inst_note = ""
    if inst:
        inst_note = (
            f" | ระดับสถาบัน: PDH={inst.get('pdh', 0):.2f}, PDL={inst.get('pdl', 0):.2f}, "
            f"VWAP={inst.get('vwap', 0):.2f}, Volume POC={inst.get('poc', 0):.2f} ({inst.get('asian_summary_th', '')})"
        )

    macro_rsi_note = ""
    if macro or rsi_div:
        m_str = f"DXY={macro.get('dxy_price', 0):.2f} ({macro.get('summary_th', '')})" if macro else ""
        r_str = f"RSI(14)={rsi_div.get('rsi14', 50):.1f} ({rsi_div.get('summary_th', '')})" if rsi_div else ""
        macro_rsi_note = f" | ปัจจัยมหภาค & โมเมนตัม: {m_str} • {r_str}"

    candle_note = ""
    if cb:
        pats = [p["label_th"] for p in (cb.get("recent_patterns") or [])[-2:]]
        pat_str = f" พบรูปแบบแท่งเทียนล่าสุด: {', '.join(pats)}" if pats else ""
        candle_note = (
            f" | พฤติกรรมแท่งเทียนย้อนหลัง {cb.get('lookback_bars', 60)} แท่ง: แรงซื้อ {cb.get('bullish_pressure_pct', 50):.0f}% "
            f"({cb.get('wick_summary_th', '')}, {dr.get('summary_th', '')}){pat_str}"
        )

    vf = structure.get("volume_flow") or {}
    flow_note = ""
    if vf and vf.get("headline_th"):
        flow_note = (
            f" | วอลุ่ม & กระแสตลาด (RVOL {vf.get('rvol', 1.0):.2f}x • แรงซื้อ {vf.get('buy_volume_pct', 50):.0f}% / แรงขาย {vf.get('sell_volume_pct', 50):.0f}%): "
            f"{vf.get('headline_th')}"
        )

    confluence_summary = (
        f"โครงสร้างหลัก ({structure['timeframe']}) อยู่ในสภาวะ {trend['regime']} ({trend['bias']}) โดยมีการเรียงตัวของเส้นค่าเฉลี่ยแบบ "
        f"{trend['ema_alignment']} (EMA20={trend['ema20']:.2f}, EMA50={trend['ema50']:.2f}, EMA200={trend['ema200']:.2f}) "
        f"โซนเข้าเทรดมีปัจจัยหนุน {len(factors)} ข้อ: {', '.join(factors)}"
        f"{flow_note}{mtf_note}{inst_note}{macro_rsi_note}{candle_note} "
        f"| เมื่อจำลองกราฟล่วงหน้า {simulation['replays']:,} ความเป็นไปได้ พบโอกาสชนะแตะ TP ก่อนที่ {probs['win_pct']:.1f}% "
        f"(คุ้มทุนที่ {probs['break_even_win_pct']:.1f}%) ให้ค่าคาดหวังทางสถิติ EV = {ev_r:+.2f}R (R:R 1:{rr:.2f})"
    )

    blind_spots: List[str] = []
    for r in risk_ticket.get("reject_reasons", []):
        blind_spots.append(f"[บล็อกความเสี่ยง] {r}")
    for w in risk_ticket.get("caution_warnings", []):
        blind_spots.append(f"[จุดต้องระวัง] {w}")
    if mtf:
        htf_res = mtf.get("nearest_htf_resistance")
        htf_sup = mtf.get("nearest_htf_support")
        if side == "BUY" and htf_res and htf_res["mid"] < target and htf_res["mid"] > entry:
            blind_spots.append(
                f"[ด่านแนวต้านไทม์เฟรมใหญ่] มีแนวต้าน {htf_res['timeframe']} ขวางอยู่ที่ {htf_res['mid']:.2f} ก่อนถึง TP ({target:.2f}) — แนะนำแบ่งปิดทำกำไร 50% ที่ TP1 ({tp1:.2f}) แล้วเลื่อน SL บังทุน"
            )
        elif side == "SELL" and htf_sup and htf_sup["mid"] > target and htf_sup["mid"] < entry:
            blind_spots.append(
                f"[ด่านแนวรับไทม์เฟรมใหญ่] มีแนวรับ {htf_sup['timeframe']} ขวางอยู่ที่ {htf_sup['mid']:.2f} ก่อนถึง TP ({target:.2f}) — แนะนำแบ่งปิดทำกำไร 50% ที่ TP1 ({tp1:.2f}) แล้วเลื่อน SL บังทุน"
            )
    if macro and macro.get("macro_gold_bias") not in ("NEUTRAL", setup.get("bias")):
        blind_spots.append(
            f"[แรงต้านจากดอลลาร์ DXY] ทิศทาง DXY ปัจจุบัน ({macro.get('dxy_change_pct', 0):+.2f}%) สวนทางกับฝั่ง {side} ควรเลื่อน SL บังทุนทันทีเมื่อราคาแตะ TP1 ({tp1:.2f})"
        )
    if probs["sweep_risk_pct"] >= 12.0:
        blind_spots.append(
            f"จากการจำลอง {simulation['replays']:,} เส้นทาง มีโอกาสเกิด Liquidity Sweep สะบัดกิน SL ที่ {stop:.2f} แล้วดึงกลับประมาณ {probs['sweep_risk_pct']:.1f}% "
            f"(โซนสภาพคล่องถัดไปอยู่ที่ {risk_ticket['scenario_b_reentry_mt5']:.2f})"
        )
    if not blind_spots:
        blind_spots.append(
            f"โครงสร้างและระยะ SL ({risk_ticket['sl_atr_ratio']:.2f}x ATR) สอดคล้องกับความผันผวนในช่วง {session.get('label_th', 'ตลาดปกติ')} "
            f"แต่ควรเช็กค่า Spread หน้างานใน MT5 ไม่ให้เกิน {risk_ticket['broker_spread_points']} points ก่อนกดคำสั่ง"
        )

    now_bkk = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%H:%M:%S น.")

    return {
        "provider": "deterministic_smc_advisor (ใส่ Gemini API Key เพื่อให้ Gemini วิเคราะห์กราฟเต็มรูปแบบ)",
        "generated_at_bkk": now_bkk,
        "verdict": verdict,
        "confluence_score": conf_score,
        "headline": headline,
        "confluence_summary": confluence_summary,
        "blind_spots": blind_spots,
        "scenarios": {
            "scenario_a": {
                "title": scenarios["scenario_a_primary"]["title"],
                "probability_pct": scenarios["scenario_a_primary"]["probability_pct"],
                "detail": (
                    f"โฟกัสเข้า {side} ที่โซน {entry:.2f} (ขนาด {lot:.2f} Lot) ตั้ง SL = {stop:.2f} | "
                    f"เมื่อแตะ TP1 = {tp1:.2f} (1:1R) ให้แบ่งปิด 50% (+${risk_ticket.get('tp1_partial_profit_usd', 0):.2f}) และขยับ SL มาบังทุนที่ {risk_ticket.get('breakeven_sl_mt5', entry):.2f} เพื่อถือลุ้น TP2 = {target:.2f}"
                ),
            },
            "scenario_b": {
                "title": scenarios["scenario_b_sweep_trap"]["title"],
                "probability_pct": scenarios["scenario_b_sweep_trap"]["probability_pct"],
                "detail": (
                    f"หากราคาทิ้งไส้เทียนกวาดทะลุ {stop:.2f} ลงไปทดสอบโซนสภาพคล่องหรือแนวรับ-ต้าน HTF ถัดไปที่ {risk_ticket['scenario_b_reentry_mt5']:.2f} "
                    f"แล้วปิดแท่งกลับเข้ามาได้ (Wick Sweep) สามารถพิจารณาแบ่งไม้รอดักที่ {risk_ticket['scenario_b_reentry_mt5']:.2f} แทนการไล่ราคา"
                ),
            },
            "scenario_c": {
                "title": scenarios["scenario_c_invalidation"]["title"],
                "probability_pct": scenarios["scenario_c_invalidation"]["probability_pct"],
                "detail": (
                    f"หากแท่งเทียน {structure['timeframe']} ปิดเนื้อเทียนหลุดระดับ {risk_ticket['scenario_c_invalidation_mt5']:.2f} "
                    f"แสดงว่าโครงสร้างฝั่ง {side} ถูกทำลายสมบูรณ์ (BOS ฝั่งตรงข้าม) ให้ยกเลิกแผนนี้ทันที ห้ามเปิดไม้แก้พอร์ต"
                ),
            },
        },
        "mt5_execution_checklist": [
            f"1. ตรวจสอบราคาบนจอ MT5 ว่าอยู่ใกล้โซน {entry:.2f} (รวม Offset {risk_ticket['broker_offset_usd']:+.2f} แล้ว)",
            f"2. กดตั้ง Volume = {lot:.2f} Lot | Stop Loss = {stop:.2f} | TP1 (บังทุน) = {tp1:.2f} | TP2 = {target:.2f}",
            f"3. ยืนยันว่าไม่มีข่าวกล่องแดง USD ภายใน 30 นาทีข้างหน้า (สถานะปัจจุบัน: {'ติดช่วงข่าว!' if news.get('in_news_blackout') else 'ปลอดข่าว'})",
        ],
    }


def evaluate_chart_with_gemini_or_fallback(
    bars: List[Dict[str, Any]],
    structure: Dict[str, Any],
    smc: Dict[str, Any],
    baseline_sim: Dict[str, Any],
    news: Dict[str, Any],
    config: Dict[str, Any],
    spread_usd: float,
    force_call: bool = False,
    mtf: Optional[Dict[str, Any]] = None,
    macro: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Let Gemini analyze the full chart and return (chosen_trade_setup, gemini_raw_response).
    If no API key is configured or Gemini is unreachable, returns (smc['trade_setup'], None)."""
    ai_cfg = config.get("ai") or {}
    env_name = str(ai_cfg.get("api_key_env") or "GEMINI_API_KEY")
    api_key = str(ai_cfg.get("api_key") or os.environ.get(env_name) or "").strip()
    model = str(ai_cfg.get("model") or "gemini-2.5-flash")

    if not api_key:
        return smc["trade_setup"], None

    cache_key = hashlib.sha256(
        f"{structure['timeframe']}:{structure['last_bar_time']}:{round(float(structure['current_price']), 0)}:{model}:{api_key[-6:]}".encode(
            "utf-8"
        )
    ).hexdigest()[:20]

    if not force_call and cache_key in _GEMINI_CACHE:
        cached_llm = _GEMINI_CACHE[cache_key]
    else:
        cached_llm = call_gemini_full_chart_analyst(
            bars=bars,
            structure=structure,
            smc=smc,
            simulation=baseline_sim,
            news=news,
            config=config,
            api_key=api_key,
            model=model,
            mtf=mtf,
            macro=macro,
        )
        if cached_llm:
            _GEMINI_CACHE[cache_key] = cached_llm

    if not cached_llm:
        return smc["trade_setup"], None

    decision = cached_llm.get("trade_decision")
    if isinstance(decision, dict):
        validated_setup = _validate_gemini_trade_decision(
            decision=decision,
            fallback_setup=smc["trade_setup"],
            atr_val=float(structure["atr14"] or 2.0),
            spread_usd=spread_usd,
        )
        if validated_setup:
            return validated_setup, cached_llm

    return smc["trade_setup"], cached_llm


def finalize_advisory(
    structure: Dict[str, Any],
    smc: Dict[str, Any],
    simulation: Dict[str, Any],
    risk_ticket: Dict[str, Any],
    news: Dict[str, Any],
    config: Dict[str, Any],
    gemini_raw: Optional[Dict[str, Any]] = None,
    mtf: Optional[Dict[str, Any]] = None,
    macro: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build final AI Advisory panel combining Gemini's full chart commentary with 15,000-path verified numbers."""
    base = build_deterministic_advisory(structure, smc, simulation, risk_ticket, news, mtf=mtf, macro=macro)
    model = str((config.get("ai") or {}).get("model") or "gemini-2.5-flash")

    if gemini_raw:
        base["provider"] = f"🟢 Gemini Full Chart AI ({model})"
        if gemini_raw.get("headline"):
            base["headline"] = str(gemini_raw["headline"])
        if gemini_raw.get("confluence_summary"):
            base["confluence_summary"] = str(gemini_raw["confluence_summary"])
        if isinstance(gemini_raw.get("blind_spots"), list) and gemini_raw["blind_spots"]:
            base["blind_spots"] = [str(x) for x in gemini_raw["blind_spots"]]
        if gemini_raw.get("scenario_a_detail"):
            base["scenarios"]["scenario_a"]["detail"] = str(gemini_raw["scenario_a_detail"])
        if gemini_raw.get("scenario_b_detail"):
            base["scenarios"]["scenario_b"]["detail"] = str(gemini_raw["scenario_b_detail"])
        if gemini_raw.get("scenario_c_detail"):
            base["scenarios"]["scenario_c"]["detail"] = str(gemini_raw["scenario_c_detail"])

    return base


def evaluate_with_ai(
    structure: Dict[str, Any],
    smc: Dict[str, Any],
    simulation: Dict[str, Any],
    risk_ticket: Dict[str, Any],
    news: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Backwards-compatible entry point for unit tests."""
    return finalize_advisory(structure, smc, simulation, risk_ticket, news, config, gemini_raw=None)
