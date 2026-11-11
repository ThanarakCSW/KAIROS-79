(() => {
  let ws = null;
  let currentState = null;
  let lwChart = null;
  let candleSeries = null;
  let ema20Series = null;
  let ema50Series = null;
  let ema200Series = null;
  let fanP95Series = null;
  let fanP50Series = null;
  let fanP5Series = null;
  let priceLines = [];
  let activeChartMode = "ai";
  let lastTvTf = null;
  const BKK_OFFSET = 7 * 3600; // UTC+7 Bangkok timezone alignment with TradingView

  const el = (id) => document.getElementById(id);

  function renderTradingViewWidget(tf) {
    const tvBox = el("tvWidgetContainer");
    if (!tvBox) return;
    const tfMap = { M1: "1", M5: "5", M15: "15", H1: "60", H4: "240" };
    const interval = tfMap[tf] || "5";
    if (lastTvTf === interval && tvBox.innerHTML.trim() !== "") return;
    lastTvTf = interval;
    const src =
      `https://s.tradingview.com/widgetembed/?frameElementId=tv_xauusd_frame` +
      `&symbol=OANDA%3AXAUUSD&interval=${interval}&hidesidetoolbar=0&symboledit=1` +
      `&saveimage=0&toolbarbg=0c111d&studies=%5B%5D&theme=dark&style=1` +
      `&timezone=Asia%2FBangkok&withdateranges=1&showpopupbutton=1&locale=th_TH`;
    tvBox.innerHTML = `<iframe src="${src}" style="width:100%;height:100%;border:0;" allowtransparency="true" allowfullscreen></iframe>`;
  }

  function showToast(msg) {
    const t = el("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.remove("hidden");
    setTimeout(() => t.classList.add("hidden"), 2200);
  }

  function playAlertBeep(verdict) {
    const soundCheck = el("soundEnabled");
    if (!soundCheck || !soundCheck.checked) return;
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(verdict === "PASS" ? 880 : 587, ctx.currentTime);
      gain.gain.setValueAtTime(0.08, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.35);
    } catch (_) {}
  }

  function initChart() {
    const container = el("chartContainer");
    const fallbackCanvas = el("fallbackCanvas");

    if (window.LightweightCharts) {
      fallbackCanvas.style.display = "none";
      lwChart = window.LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: container.clientHeight,
        layout: {
          background: { color: "#0c111d" },
          textColor: "#64748b",
          fontFamily: "'JetBrains Mono', monospace",
        },
        grid: {
          vertLines: { color: "rgba(255, 255, 255, 0.035)" },
          horzLines: { color: "rgba(255, 255, 255, 0.035)" },
        },
        crosshair: { mode: window.LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: "rgba(255, 255, 255, 0.07)" },
        timeScale: {
          borderColor: "rgba(255, 255, 255, 0.07)",
          timeVisible: true,
          secondsVisible: false,
        },
      });

      candleSeries = lwChart.addCandlestickSeries({
        upColor: "#10b981",
        downColor: "#f43f5e",
        borderUpColor: "#10b981",
        borderDownColor: "#f43f5e",
        wickUpColor: "#10b981",
        wickDownColor: "#f43f5e",
      });

      ema20Series = lwChart.addLineSeries({ color: "#38bdf8", lineWidth: 1, priceLineVisible: false });
      ema50Series = lwChart.addLineSeries({ color: "#f59e0b", lineWidth: 1, priceLineVisible: false });
      ema200Series = lwChart.addLineSeries({ color: "#a855f7", lineWidth: 2, priceLineVisible: false });

      fanP95Series = lwChart.addLineSeries({
        color: "rgba(16, 185, 129, 0.45)",
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
      });
      fanP50Series = lwChart.addLineSeries({
        color: "#f43f5e",
        lineWidth: 2,
        lineStyle: 2,
        priceLineVisible: false,
      });
      fanP5Series = lwChart.addLineSeries({
        color: "rgba(244, 63, 94, 0.45)",
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
      });

      window.addEventListener("resize", () => {
        if (lwChart) {
          lwChart.applyOptions({
            width: container.clientWidth,
            height: container.clientHeight,
          });
        }
      });
    } else {
      window.addEventListener("resize", () => {
        if (currentState) renderFallbackCanvas(currentState);
      });
    }
  }

  function clearPriceLines() {
    if (!candleSeries) return;
    priceLines.forEach((ln) => {
      try {
        candleSeries.removePriceLine(ln);
      } catch (_) {}
    });
    priceLines = [];
  }

  function addHorizontalLine(price, title, color, lineStyle = 2) {
    if (!candleSeries || !price) return;
    const ln = candleSeries.createPriceLine({
      price: Number(price),
      color,
      lineWidth: 1,
      lineStyle,
      axisLabelVisible: true,
      title,
    });
    priceLines.push(ln);
  }

  function renderChart(state) {
    const bars = state.bars || [];
    if (!bars.length) return;
    if (activeChartMode === "tv") {
      renderTradingViewWidget(state.timeframe || "M5");
    }

    if (lwChart && candleSeries) {
      candleSeries.setData(
        bars.map((b) => ({
          time: b.t + BKK_OFFSET,
          open: b.o,
          high: b.h,
          low: b.l,
          close: b.c,
        }))
      );

      const emas = (state.structure && state.structure.ema_series) || {};
      if (emas.ema20) ema20Series.setData(emas.ema20.map((p) => ({ time: p.time + BKK_OFFSET, value: p.value })));
      if (emas.ema50) ema50Series.setData(emas.ema50.map((p) => ({ time: p.time + BKK_OFFSET, value: p.value })));
      if (emas.ema200) ema200Series.setData(emas.ema200.map((p) => ({ time: p.time + BKK_OFFSET, value: p.value })));

      const markers = [];
      const breaks = (state.smc && state.smc.structure_breaks) || [];
      breaks.slice(-5).forEach((br) => {
        markers.push({
          time: br.break_t + BKK_OFFSET,
          position: br.direction === "BULLISH" ? "belowBar" : "aboveBar",
          color: br.direction === "BULLISH" ? "#10b981" : "#f43f5e",
          shape: br.direction === "BULLISH" ? "arrowUp" : "arrowDown",
          text: br.type,
        });
      });
      const sweeps = (state.smc && state.smc.liquidity_sweeps) || [];
      sweeps.slice(-3).forEach((sw) => {
        markers.push({
          time: sw.sweep_t + BKK_OFFSET,
          position: sw.implication.startsWith("BULLISH") ? "belowBar" : "aboveBar",
          color: "#f59e0b",
          shape: "circle",
          text: "SWEEP",
        });
      });
      markers.sort((a, b) => a.time - b.time);
      candleSeries.setMarkers(markers);

      const fan = (state.simulation && state.simulation.fan_chart) || [];
      if (fan.length) {
        fanP95Series.setData(fan.map((f) => ({ time: f.time + BKK_OFFSET, value: f.p95 })));
        fanP50Series.setData(fan.map((f) => ({ time: f.time + BKK_OFFSET, value: f.p50 })));
        fanP5Series.setData(fan.map((f) => ({ time: f.time + BKK_OFFSET, value: f.p5 })));
      }

      clearPriceLines();
      const tk = state.risk_ticket;
      if (tk) {
        addHorizontalLine(tk.entry_mt5, `ดัก ${tk.order_type || tk.side + " LIMIT"} ล่วงหน้า`, "#f5b841", 0);
        addHorizontalLine(tk.stop_mt5, `SL`, "#f43f5e", 2);
        if (tk.tp1_mt5) {
          addHorizontalLine(tk.tp1_mt5, `TP1 (1R บังทุน)`, "#38bdf8", 2);
        }
        addHorizontalLine(tk.target_mt5, `TP2 (${tk.reward_risk}R)`, "#10b981", 2);
      }

      const inst = (state.structure && state.structure.institutional_levels) || {};
      if (inst.vwap) {
        addHorizontalLine(inst.vwap, `VWAP`, "rgba(139,92,246,0.55)", 1);
      }
      if (inst.poc) {
        addHorizontalLine(inst.poc, `VP POC`, "rgba(245,184,65,0.45)", 1);
      }

      const topOb = ((state.smc && state.smc.order_blocks) || [])[0];
      if (topOb) {
        const obColor = topOb.bias === "BULLISH" ? "rgba(16,185,129,0.55)" : "rgba(244,63,94,0.55)";
        addHorizontalLine(topOb.high, `${topOb.kind}`, obColor, 1);
        addHorizontalLine(topOb.low, `${topOb.kind}`, obColor, 1);
      }

      const mtf = state.mtf || {};
      if (mtf.nearest_htf_resistance) {
        const r = mtf.nearest_htf_resistance;
        addHorizontalLine(r.mid, `HTF ${r.timeframe} RES`, "rgba(244,63,94,0.45)", 2);
      }
      if (mtf.nearest_htf_support) {
        const s = mtf.nearest_htf_support;
        addHorizontalLine(s.mid, `HTF ${s.timeframe} SUP`, "rgba(16,185,129,0.45)", 2);
      }
    } else {
      renderFallbackCanvas(state);
    }
  }

  function renderFallbackCanvas(state) {
    const canvas = el("fallbackCanvas");
    if (!canvas) return;
    const rect = canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = rect.height;
    ctx.fillStyle = "#0c111d";
    ctx.fillRect(0, 0, w, h);

    const allBars = state.bars || [];
    const bars = allBars.slice(-90);
    const fan = (state.simulation && state.simulation.fan_chart) || [];
    if (!bars.length) return;

    const tk = state.risk_ticket || {};
    let minP = Infinity;
    let maxP = -Infinity;
    bars.forEach((b) => {
      minP = Math.min(minP, b.l);
      maxP = Math.max(maxP, b.h);
    });
    fan.forEach((f) => {
      minP = Math.min(minP, f.p5);
      maxP = Math.max(maxP, f.p95);
    });
    if (tk.stop_mt5) {
      minP = Math.min(minP, tk.stop_mt5, tk.target_mt5);
      maxP = Math.max(maxP, tk.stop_mt5, tk.target_mt5);
    }
    const pad = (maxP - minP) * 0.08 || 2;
    minP -= pad;
    maxP += pad;

    const totalSlots = bars.length + fan.length + 2;
    const leftPad = 14;
    const rightPad = 68;
    const plotW = w - leftPad - rightPad;
    const xOf = (idx) => leftPad + (idx / totalSlots) * plotW;
    const yOf = (p) => h - 24 - ((p - minP) / (maxP - minP)) * (h - 44);

    ctx.strokeStyle = "rgba(255,255,255,0.04)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 6; i++) {
      const p = minP + ((maxP - minP) * i) / 6;
      const y = yOf(p);
      ctx.beginPath();
      ctx.moveTo(leftPad, y);
      ctx.lineTo(w - rightPad, y);
      ctx.stroke();
      ctx.fillStyle = "#64748b";
      ctx.font = "11px 'JetBrains Mono', monospace";
      ctx.fillText(p.toFixed(2), w - rightPad + 6, y + 4);
    }

    const obs = (state.smc && state.smc.order_blocks) || [];
    obs.slice(0, 2).forEach((ob) => {
      const y1 = yOf(ob.high);
      const y2 = yOf(ob.low);
      ctx.fillStyle = ob.bias === "BULLISH" ? "rgba(16, 185, 129, 0.12)" : "rgba(244, 63, 94, 0.12)";
      ctx.fillRect(leftPad, Math.min(y1, y2), plotW, Math.max(4, Math.abs(y2 - y1)));
    });

    const barW = Math.max(3, (plotW / totalSlots) * 0.68);
    bars.forEach((b, i) => {
      const x = xOf(i);
      const isUp = b.c >= b.o;
      ctx.strokeStyle = isUp ? "#10b981" : "#f43f5e";
      ctx.fillStyle = isUp ? "#10b981" : "#f43f5e";
      ctx.beginPath();
      ctx.moveTo(x, yOf(b.h));
      ctx.lineTo(x, yOf(b.l));
      ctx.stroke();
      const yOpen = yOf(b.o);
      const yClose = yOf(b.c);
      ctx.fillRect(x - barW / 2, Math.min(yOpen, yClose), barW, Math.max(2, Math.abs(yClose - yOpen)));
    });

    if (fan.length > 1) {
      const startIdx = bars.length - 1;
      ctx.fillStyle = "rgba(244, 63, 94, 0.12)";
      ctx.beginPath();
      fan.forEach((f, idx) => {
        const x = xOf(startIdx + idx);
        const y = yOf(f.p95);
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      for (let idx = fan.length - 1; idx >= 0; idx--) {
        ctx.lineTo(xOf(startIdx + idx), yOf(fan[idx].p5));
      }
      ctx.closePath();
      ctx.fill();

      ctx.strokeStyle = "#f43f5e";
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      fan.forEach((f, idx) => {
        const x = xOf(startIdx + idx);
        const y = yOf(f.p50);
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.setLineDash([]);
    }

    [
      { p: tk.entry_mt5, c: "#f5b841", lbl: "ENTRY" },
      { p: tk.stop_mt5, c: "#f43f5e", lbl: "SL" },
      { p: tk.target_mt5, c: "#10b981", lbl: "TP" },
    ].forEach((ln) => {
      if (!ln.p) return;
      const y = yOf(ln.p);
      ctx.strokeStyle = ln.c;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(leftPad, y);
      ctx.lineTo(w - rightPad, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = ln.c;
      ctx.fillText(`${ln.lbl} ${ln.p.toFixed(2)}`, leftPad + 8, y - 4);
    });
  }

  function renderDashboard(state) {
    currentState = state;
    renderChart(state);

    const st = state.structure || {};
    const trend = st.trend || {};
    const smc = state.smc || {};
    const setup = smc.trade_setup || {};
    const sim = state.simulation || {};
    const probs = sim.probabilities || {};
    const tk = state.risk_ticket || {};
    const ai = state.ai_advisory || {};
    const news = state.news || {};
    const session = news.session || {};
    const quote = state.live_quote;

    // Top bar
    el("livePrice").textContent = Number(st.current_price || 0).toFixed(2);
    if (quote && quote.bid && quote.ask) {
      el("bidAskPill").textContent = `Bid ${Number(quote.bid).toFixed(2)} • Ask ${Number(quote.ask).toFixed(2)}`;
    }
    el("sessionBadge").textContent = `${session.session || "MARKET"} • ${session.bkk_time || ""}`;
    el("feedSourceLabel").textContent = `Feed: ${state.data_source || "-"}`;

    const tBadge = el("trendBadge");
    tBadge.textContent = `${trend.regime || "-"} (${trend.bias || "-"})`;
    tBadge.className =
      "meta-pill " +
      (trend.bias === "BULLISH" ? "pill-bull" : trend.bias === "BEARISH" ? "pill-bear" : "pill-neutral");

    // Floating Chart HUD
    el("atrBadge").textContent = `ATR(14): $${Number(st.atr14 || 0).toFixed(2)}`;
    el("hudEma20").textContent = `EMA20: ${trend.ema20 || "-"}`;
    el("hudEma50").textContent = `EMA50: ${trend.ema50 || "-"}`;
    el("hudEma200").textContent = `EMA200: ${trend.ema200 || "-"}`;

    const mtf = state.mtf || {};
    const macro = state.macro || {};
    const tracker = state.signal_tracker || {};
    const inst = st.institutional_levels || {};
    const rsiDiv = st.rsi_divergence || {};
    const cb = st.candle_behavior || {};
    const dr = cb.get ? {} : cb.dealing_range || {};

    if (el("mtfHudRow")) {
      const tfs = mtf.timeframes || {};
      const tfBadges = ["M5", "M15", "H1", "H4"]
        .filter((k) => tfs[k])
        .map((k) => {
          const b = tfs[k].bias;
          const dotColor = b === "BULLISH" ? "#10b981" : b === "BEARISH" ? "#f43f5e" : "#94a3b8";
          const arrow = b === "BULLISH" ? "▲" : b === "BEARISH" ? "▼" : "◆";
          return `<span class="hud-tag"><i class="dot" style="background:${dotColor}"></i>${k}: ${arrow} ${b}</span>`;
        });
      if (macro.dxy_price) {
        const smtTag =
          macro.smt_divergence && macro.smt_divergence !== "NONE"
            ? ` • ⚡ ${macro.smt_divergence}`
            : "";
        tfBadges.push(
          `<span class="hud-tag">💵 DXY: ${Number(macro.dxy_price).toFixed(2)} (${macro.dxy_change_pct >= 0 ? "+" : ""}${macro.dxy_change_pct}%)${smtTag}</span>`
        );
      }
      if (rsiDiv.rsi14) {
        const divText = rsiDiv.divergence && rsiDiv.divergence !== "NONE" ? ` • ${rsiDiv.divergence}` : "";
        tfBadges.push(`<span class="hud-tag">📈 RSI(14): ${rsiDiv.rsi14}${divText}</span>`);
      }
      if (inst.vwap && inst.poc) {
        tfBadges.push(
          `<span class="hud-tag">⚖️ VWAP: ${inst.vwap.toFixed(2)} • POC: ${inst.poc.toFixed(2)}</span>`
        );
      }
      if (dr.zone) {
        tfBadges.push(
          `<span class="hud-tag">กรอบ 60 แท่ง: ${dr.zone} (${Number(dr.position_pct || 50).toFixed(0)}%)</span>`
        );
      }
      const scalp = st.scalping || {};
      if (state.trading_style === "scalping" && scalp.ema9) {
        tfBadges.unshift(
          `<span class="hud-tag" style="border-color:rgba(245,184,65,0.45);color:#fde68a">⚡ SCALP EMA9/21: ${scalp.ema9}/${scalp.ema21} • StochRSI: ${scalp.stoch_rsi_k} (${scalp.squeeze_state})</span>`
        );
      }
      el("mtfHudRow").innerHTML = tfBadges.join("");
    }

    // Sync active trading style and timeframe buttons
    document.querySelectorAll(".style-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.style === (state.trading_style || "intraday"));
    });
    document.querySelectorAll(".tf-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tf === state.timeframe);
    });

    const factors = setup.confluence_factors || [];
    el("zoneStrip").innerHTML = factors
      .slice(0, 5)
      .map((f) => `<span class="factor-pill">✦ ${f}</span>`)
      .join("");

    // Bottom Dock: SMC Zones
    const obCards = (smc.order_blocks || []).slice(0, 4).map(
      (ob) => `
      <div class="mini-zone-card ${ob.bias === "BULLISH" ? "bull" : "bear"}">
        <div class="mini-zone-top">
          <strong>${ob.kind}</strong>
          <span class="mini-zone-price">${ob.low.toFixed(2)} – ${ob.high.toFixed(2)}</span>
        </div>
        <div class="mini-zone-sub">Score ${ob.confluence_score}/100 • ${(ob.confluence_factors || [])[0] || ""}</div>
      </div>`
    );
    const fvgCards = (smc.fvgs || []).slice(-3).map(
      (f) => `
      <div class="mini-zone-card ${f.bias === "BULLISH" ? "bull" : "bear"}">
        <div class="mini-zone-top">
          <strong>${f.kind}</strong>
          <span class="mini-zone-price">${f.low.toFixed(2)} – ${f.high.toFixed(2)}</span>
        </div>
        <div class="mini-zone-sub">Imbalance Width: $${f.width_usd.toFixed(2)}</div>
      </div>`
    );
    el("smcZonesList").innerHTML =
      obCards.concat(fvgCards).join("") || `<div class="mini-zone-card">กำลังสแกนหาโซน SMC ใหม่...</div>`;

    // Bottom Dock: Multi-Timeframe S&R + Institutional Levels + Macro + Candlestick Behavior
    if (el("mtfCandlesList")) {
      const mtfCards = [];

      // 1. Macro DXY & US10Y + SMT Divergence
      if (macro.dxy_price) {
        const mClass =
          macro.macro_gold_bias === "BULLISH"
            ? "bull"
            : macro.macro_gold_bias === "BEARISH"
            ? "bear"
            : "warn";
        mtfCards.push(`
          <div class="mini-zone-card ${mClass}">
            <div class="mini-zone-top">
              <strong>💵 DXY ${Number(macro.dxy_price).toFixed(2)} &amp; US10Y ${Number(macro.us10y_yield || 4.25).toFixed(2)}%</strong>
              <span class="mini-zone-price">${macro.smt_divergence !== "NONE" ? "⚡ SMT DIV" : macro.dxy_bias}</span>
            </div>
            <div class="mini-zone-sub">${macro.summary_th || ""}</div>
          </div>
        `);
      }

      // 2. Institutional Levels: PDH/PDL, Asian Session Range, VWAP & Volume Profile POC
      if (inst.pdh) {
        mtfCards.push(`
          <div class="mini-zone-card ${inst.price_vs_vwap === "ABOVE_VWAP" ? "bull" : "bear"}">
            <div class="mini-zone-top">
              <strong>🏦 PDH ${inst.pdh.toFixed(2)} • PDL ${inst.pdl.toFixed(2)}</strong>
              <span class="mini-zone-price">POC ${inst.poc.toFixed(2)}</span>
            </div>
            <div class="mini-zone-sub">VWAP: <b>${inst.vwap.toFixed(2)}</b> • Value Area: ${inst.val.toFixed(2)}–${inst.vah.toFixed(2)} • ${inst.asian_summary_th || ""}</div>
          </div>
        `);
      }

      // 3. RSI(14) & Momentum Divergence
      if (rsiDiv.rsi14) {
        const rClass =
          rsiDiv.divergence_bias === "BULLISH"
            ? "bull"
            : rsiDiv.divergence_bias === "BEARISH"
            ? "bear"
            : "warn";
        mtfCards.push(`
          <div class="mini-zone-card ${rClass}">
            <div class="mini-zone-top">
              <strong>📈 RSI(14): ${rsiDiv.rsi14} (${rsiDiv.rsi_state})</strong>
              <span class="mini-zone-price">${rsiDiv.divergence}</span>
            </div>
            <div class="mini-zone-sub">${rsiDiv.summary_th || ""}</div>
          </div>
        `);
      }

      // 4. 4-Timeframe Trend & S&R Matrix
      const tfs = mtf.timeframes || {};
      ["H4", "H1", "M15", "M5"].forEach((tfKey) => {
        const info = tfs[tfKey];
        if (!info) return;
        const sup = info.nearest_support ? `${info.nearest_support.mid.toFixed(2)}` : "-";
        const res = info.nearest_resistance ? `${info.nearest_resistance.mid.toFixed(2)}` : "-";
        const cardClass = info.bias === "BULLISH" ? "bull" : info.bias === "BEARISH" ? "bear" : "warn";
        mtfCards.push(`
          <div class="mini-zone-card ${cardClass}">
            <div class="mini-zone-top">
              <strong>🌐 ${tfKey} (${info.bias})</strong>
              <span class="mini-zone-price">${info.regime}</span>
            </div>
            <div class="mini-zone-sub">แนวรับ ${tfKey}: <b>${sup}</b> • แนวต้าน ${tfKey}: <b>${res}</b></div>
          </div>
        `);
      });

      const htfZones = (mtf.htf_sr_zones || []).filter((z) => z.is_mtf_confluence).slice(0, 2);
      htfZones.forEach((hz) => {
        mtfCards.push(`
          <div class="mini-zone-card ${hz.role === "support" ? "bull" : "bear"}">
            <div class="mini-zone-top">
              <strong>⭐ โซนทับซ้อน ${(hz.confluent_timeframes || [hz.timeframe]).join("+")}</strong>
              <span class="mini-zone-price">${hz.low.toFixed(2)} – ${hz.high.toFixed(2)}</span>
            </div>
            <div class="mini-zone-sub">${hz.role === "support" ? "แนวรับร่วมหลาย TF" : "แนวต้านร่วมหลาย TF"} (ชน ${hz.touches} ครั้ง)</div>
          </div>
        `);
      });

      if (cb.lookback_bars) {
        mtfCards.push(`
          <div class="mini-zone-card ${cb.bullish_pressure_pct >= 52 ? "bull" : cb.bullish_pressure_pct <= 48 ? "bear" : "warn"}">
            <div class="mini-zone-top">
              <strong>🕯️ พฤติกรรมแท่งเทียน ${cb.lookback_bars} แท่ง</strong>
              <span class="mini-zone-price">แรงซื้อ ${cb.bullish_pressure_pct}%</span>
            </div>
            <div class="mini-zone-sub">${cb.wick_summary_th || ""} • ${dr.summary_th || ""}</div>
          </div>
        `);
      }

      const recentPats = (cb.recent_patterns || []).slice(-3).reverse();
      recentPats.forEach((p) => {
        mtfCards.push(`
          <div class="mini-zone-card ${p.bias === "BULLISH" ? "bull" : "bear"}">
            <div class="mini-zone-top">
              <strong>🕯️ ${p.pattern}</strong>
              <span class="mini-zone-price">@ ${Number(p.price).toFixed(2)}</span>
            </div>
            <div class="mini-zone-sub">${p.label_th}</div>
          </div>
        `);
      });

      el("mtfCandlesList").innerHTML = mtfCards.join("");
    }

    // Bottom Dock: Signal Performance Tracker (Walk-Forward Win-Rate & R Verification)
    if (el("signalTrackerList")) {
      const tCards = [];
      if (tracker.total_signals !== undefined) {
        const cumR = Number(tracker.cumulative_r || 0);
        tCards.push(`
          <div class="mini-zone-card ${cumR >= 0 ? "bull" : "bear"}">
            <div class="mini-zone-top">
              <strong>🏆 Win Rate: ${Number(tracker.win_rate_pct || 0).toFixed(1)}% (${tracker.win_count || 0}W / ${tracker.loss_count || 0}L)</strong>
              <span class="mini-zone-price">${cumR >= 0 ? "+" : ""}${cumR.toFixed(2)}R</span>
            </div>
            <div class="mini-zone-sub">Profit Factor: <b>${tracker.profit_factor || 1.0}</b> • ตรวจสัญญาณจริงบนกราฟ ${tracker.timeframe || ""} (รวมระบบแบ่งปิด TP1 + บังทุน)</div>
          </div>
        `);
      }
      ((tracker.recent_signals) || []).forEach((sig) => {
        const cls =
          sig.outcome.startsWith("WIN")
            ? "bull"
            : sig.outcome === "OPEN"
            ? "warn"
            : "bear";
        tCards.push(`
          <div class="mini-zone-card ${cls}">
            <div class="mini-zone-top">
              <strong>${sig.side} @ ${Number(sig.entry).toFixed(2)} (${sig.time_bkk})</strong>
              <span class="mini-zone-price">${sig.pnl_r > 0 ? `+${sig.pnl_r}R` : sig.pnl_r < 0 ? `${sig.pnl_r}R` : "OPEN"}</span>
            </div>
            <div class="mini-zone-sub">${sig.outcome_label_th} • TP1: ${Number(sig.tp1).toFixed(2)} | TP2: ${Number(sig.target).toFixed(2)} | SL: ${Number(sig.stop).toFixed(2)}</div>
          </div>
        `);
      });
      el("signalTrackerList").innerHTML = tCards.join("");
    }

    // Bottom Dock: USD Red News
    const redList = news.upcoming_usd_red_news || [];
    const sessionCard = `
      <div class="mini-zone-card ${session.is_rollover_danger ? "warn" : "bull"}">
        <div class="mini-zone-top">
          <strong>${session.session || "-"}</strong>
          <span>${session.liquidity || ""}</span>
        </div>
        <div class="mini-zone-sub">${session.label_th || "-"}</div>
      </div>`;
    const newsCards = redList.length
      ? redList
          .map(
            (ev) => `
        <div class="mini-zone-card ${Math.abs(ev.minutes_away) <= 30 ? "bear" : "warn"}">
          <div class="mini-zone-top">
            <strong>🔴 ${ev.title}</strong>
            <span>${ev.time_bkk}</span>
          </div>
          <div class="mini-zone-sub">${
            ev.minutes_away > 0 ? `อีก ${ev.minutes_away} นาที` : `${Math.abs(ev.minutes_away)} นาทีที่แล้ว`
          } • คาดการณ์: ${ev.forecast}</div>
        </div>`
          )
          .join("")
      : `<div class="mini-zone-card bull"><div class="mini-zone-top"><strong>✅ ปลอดข่าวกล่องแดง USD</strong></div><div class="mini-zone-sub">ไม่มีข่าว High Impact ในช่วงเวลานี้</div></div>`;
    el("newsPanel").innerHTML = sessionCard + newsCards;

    // Right Sidebar: Hero MT5 Ticket (with > 50% TP Win Probability Continuous Scanner)
    const winProbPassed = tk.win_prob_passed !== false;
    const styleTag = state.trading_style === "scalping" ? "⚡ SCALP" : "🎯 INTRADAY";
    if (!winProbPassed) {
      el("ticketSideTitle").textContent = `⏳ รอจังหวะ (WAIT)`;
      el("ticketSideTitle").style.color = "var(--warn)";
      const wz = tk.next_watch_zone;
      const rawWatchPrice = wz && typeof wz === "object" ? wz.watch_entry : wz;
      const watchPrice = rawWatchPrice ? Number(rawWatchPrice).toFixed(2) : Number(tk.entry_mt5 || 0).toFixed(2);
      el("statusSub").textContent = `🔍 [${styleTag}] โอกาสถึง TP ${probs.win_pct || 0}% (< 50%) • กำลังเฝ้าดักโซน ${tk.side} @ ${watchPrice}`;
    } else {
      el("ticketSideTitle").textContent = `${tk.side || "BUY"} XAUUSD`;
      el("ticketSideTitle").style.color = tk.side === "SELL" ? "var(--bear)" : "var(--bull)";
      el("statusSub").textContent =
        tk.status === "ACTIVE_IN_ZONE"
          ? `🟢 [${styleTag}] โอกาสถึง TP ${probs.win_pct || 0}% (> 50%) • พร้อมเข้าเทรด`
          : `⏳ [${styleTag}] โอกาสถึง TP ${probs.win_pct || 0}% (> 50%) • รอราคาย่อแตะจุดเข้า`;
    }

    const vBadge = el("verdictBadge");
    vBadge.textContent = !winProbPassed ? "SCANNING <50%" : tk.verdict || "PASS";
    vBadge.className =
      "verdict-pill " +
      (!winProbPassed ? "caution" : tk.verdict === "PASS" ? "pass" : tk.verdict === "CAUTION" ? "caution" : "reject");

    el("confluenceBadge").textContent = `${setup.confluence_score || 0}%`;
    el("valLot").textContent = Number(tk.lot_size || 0).toFixed(2);
    el("riskUsdSub").textContent = `ความเสี่ยง $${Number(tk.actual_risk_usd || 0).toFixed(2)} (${tk.risk_pct_configured || 1}%)`;
    el("valEntry").textContent = Number(tk.entry_mt5 || 0).toFixed(2);
    if (el("entryHeadLabel")) {
      el("entryHeadLabel").textContent = `ดัก ${tk.order_type || (tk.side || "BUY") + " LIMIT"} ล่วงหน้า`;
    }
    if (el("entryDistSub")) {
      const dist = Number(tk.distance_to_entry_usd || Math.abs((st.current_price || 0) - (tk.entry_mt5 || 0)));
      const dirWord = tk.side === "SELL" ? "รอราคาเด้งขึ้นไปอีก" : "รอราคาย่อลงมาอีก";
      el("entryDistSub").textContent = dist > 0.05 ? `${dirWord} $${dist.toFixed(2)} (ยังไม่ผ่านจุดเข้า)` : `ราคาแตะโซนดักเข้าใหม่พอดี`;
    }
    el("valSL").textContent = Number(tk.stop_mt5 || 0).toFixed(2);
    el("slDistSub").textContent = `ห่าง $${Number(tk.sl_distance_usd || 0).toFixed(2)} (${tk.sl_points || 0} pts)`;
    el("valTP").textContent = Number(tk.target_mt5 || 0).toFixed(2);
    const tp1Str = tk.tp1_mt5 ? `TP1 บังทุน ${Number(tk.tp1_mt5).toFixed(2)} • ` : "";
    el("tpProfitSub").textContent = `${tp1Str}R:R 1:${Number(tk.reward_risk || 0).toFixed(2)} (+$${Number(tk.potential_profit_usd || 0).toFixed(2)})`;

    // Monte Carlo Strip
    if (el("simTitleText") && sim.replays) {
      el("simTitleText").textContent = `🎲 จำลองกราฟ ${Number(sim.replays).toLocaleString()} ความเป็นไปได้`;
    }
    el("valEV").textContent = `EV ${sim.expected_value_r >= 0 ? "+" : ""}${Number(sim.expected_value_r || 0).toFixed(2)}R`;
    el("valWinPct").textContent = `${probs.win_pct || 0}%`;
    el("valStopPct").textContent = `${probs.stop_pct || 0}%`;
    el("valSweepPct").textContent = `${probs.sweep_risk_pct || 0}%`;
    el("barWin").style.width = `${Math.max(5, probs.win_pct || 0)}%`;
    el("barSweep").style.width = `${Math.max(3, probs.sweep_risk_pct || 0)}%`;
    el("barStop").style.width = `${Math.max(5, probs.stop_pct || 0)}%`;
    el("simVerdictText").textContent = sim.verdict || "";

    // Gate Checklist
    const gateItems = [];
    (tk.reject_reasons || []).forEach((r) => gateItems.push(`<div class="gate-item gate-reject">❌ ${r}</div>`));
    (tk.caution_warnings || []).forEach((w) => gateItems.push(`<div class="gate-item gate-warn">⚠️ ${w}</div>`));
    (tk.passed_checks || []).forEach((p) => gateItems.push(`<div class="gate-item gate-pass">✅ ${p}</div>`));
    el("gateChecklist").innerHTML = gateItems.join("");

    // AI 3-Scenario Advisor
    el("aiTimestamp").textContent = `${ai.provider || "AI"} • ${ai.generated_at_bkk || ""}`;
    el("aiHeadline").textContent = ai.headline || "-";
    el("aiSummary").textContent = ai.confluence_summary || "-";
    el("aiBlindSpots").innerHTML = (ai.blind_spots || []).map((b) => `<li>${b}</li>`).join("");

    const sc = ai.scenarios || {};
    if (sc.scenario_a) {
      el("scenATitle").textContent = `🟢 ${sc.scenario_a.title}`;
      el("scenAProb").textContent = `${sc.scenario_a.probability_pct}%`;
      el("scenADetail").textContent = sc.scenario_a.detail;
    }
    if (sc.scenario_b) {
      el("scenBTitle").textContent = `🟡 ${sc.scenario_b.title}`;
      el("scenBProb").textContent = `${sc.scenario_b.probability_pct}%`;
      el("scenBDetail").textContent = sc.scenario_b.detail;
    }
    if (sc.scenario_c) {
      el("scenCTitle").textContent = `🔴 ${sc.scenario_c.title}`;
      el("scenCProb").textContent = `${sc.scenario_c.probability_pct}%`;
      el("scenCDetail").textContent = sc.scenario_c.detail;
    }

    // Populate modal settings
    const cfg = state.config || {};
    if (document.activeElement !== el("cfgBalance")) el("cfgBalance").value = cfg.account_balance ?? 5000;
    if (document.activeElement !== el("cfgRiskPct")) el("cfgRiskPct").value = cfg.risk_per_trade_pct ?? 1.0;
    if (document.activeElement !== el("cfgOffset")) el("cfgOffset").value = cfg.broker_price_offset ?? 0.0;
    if (document.activeElement !== el("cfgSpread")) el("cfgSpread").value = cfg.broker_spread_points ?? 25;

    if (state.is_new_signal_alert) {
      playAlertBeep(tk.verdict);
      showToast(`🔔 แจ้งเตือนสัญญาณ ${tk.side} XAUUSD (${tk.verdict})`);
    }
  }

  function handleLiveTick(msg) {
    if (!msg.bar) return;
    const b = msg.bar;
    el("livePrice").textContent = Number(b.c).toFixed(2);
    if (msg.bid && msg.ask) {
      el("bidAskPill").textContent = `Bid ${Number(msg.bid).toFixed(2)} • Ask ${Number(msg.ask).toFixed(2)}`;
    }
    if (candleSeries) {
      candleSeries.update({
        time: b.t + BKK_OFFSET,
        open: b.o,
        high: b.h,
        low: b.l,
        close: b.c,
      });
    } else if (currentState && currentState.bars && currentState.bars.length) {
      const bars = currentState.bars;
      if (bars[bars.length - 1].t === b.t) {
        bars[bars.length - 1] = b;
      } else {
        bars.push(b);
      }
      renderFallbackCanvas(currentState);
    }
  }

  function connectWebSocket() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/ws`;
    const badge = el("wsStatus");
    const badgeText = el("wsStatusText");

    ws = new WebSocket(wsUrl);
    ws.onopen = () => {
      badge.classList.add("live");
      badgeText.textContent = "Live Stream";
    };
    ws.onmessage = (ev) => {
      try {
        const doc = JSON.parse(ev.data);
        if (doc.type === "full_state") {
          renderDashboard(doc);
        } else if (doc.type === "tick") {
          handleLiveTick(doc);
        } else if (doc.type === "auto_replay_status") {
          el("btnAutoReplay").textContent = doc.auto_replay ? "⏸ หยุดเล่น" : "▶ เล่นอัตโนมัติ";
        }
      } catch (_) {}
    };
    ws.onclose = () => {
      badge.classList.remove("live");
      badgeText.textContent = "Reconnecting";
      setTimeout(connectWebSocket, 2000);
    };
  }

  function sendWs(payload) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }

  function bindEvents() {
    // Trading Style (Intraday vs Scalping) segmented buttons
    document.querySelectorAll(".style-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".style-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const style = btn.dataset.style;
        // When switching to scalping on H1/H4, automatically switch to M5 for fast scalping
        const curTf = currentState ? currentState.timeframe : "M5";
        const targetTf = style === "scalping" && (curTf === "H1" || curTf === "H4") ? "M5" : curTf;
        sendWs({ action: "change_style", trading_style: style, timeframe: targetTf });
      });
    });

    // Timeframe segmented buttons
    document.querySelectorAll(".tf-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tf-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        if (activeChartMode === "tv") {
          renderTradingViewWidget(btn.dataset.tf);
        }
        sendWs({ action: "change_tf", timeframe: btn.dataset.tf });
      });
    });

    // Chart Mode (AI SMC Canvas vs Official TradingView XAUUSD)
    document.querySelectorAll(".chart-mode-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".chart-mode-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        activeChartMode = btn.dataset.chartMode;
        const aiBox = el("chartContainer");
        const tvBox = el("tvWidgetContainer");
        const hud = document.querySelector(".chart-hud");
        if (activeChartMode === "tv") {
          aiBox.style.display = "none";
          tvBox.style.display = "block";
          if (hud) hud.style.display = "none";
          renderTradingViewWidget(currentState ? currentState.timeframe : "M5");
        } else {
          tvBox.style.display = "none";
          aiBox.style.display = "block";
          if (hud) hud.style.display = "flex";
          if (currentState) renderChart(currentState);
        }
      });
    });

    // Bottom Dock Tabs
    document.querySelectorAll(".dock-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".dock-tab").forEach((t) => t.classList.remove("active"));
        document.querySelectorAll(".dock-pane").forEach((p) => p.classList.remove("active"));
        tab.classList.add("active");
        const pane = el(`dock-${tab.dataset.dock}`);
        if (pane) pane.classList.add("active");
      });
    });

    // Right Sidebar Advisor Tabs
    document.querySelectorAll(".adv-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".adv-tab").forEach((t) => t.classList.remove("active"));
        document.querySelectorAll(".adv-pane").forEach((p) => p.classList.remove("active"));
        tab.classList.add("active");
        const pane = el(`tab-${tab.dataset.tab}`);
        if (pane) pane.classList.add("active");
      });
    });

    // Sound icon visual state
    const soundInput = el("soundEnabled");
    soundInput.addEventListener("change", () => {
      el("soundIcon").textContent = soundInput.checked ? "🔔" : "🔕";
      showToast(soundInput.checked ? "เปิดเสียงแจ้งเตือนแล้ว" : "ปิดเสียงแจ้งเตือนแล้ว");
    });

    // Settings Modal open/close
    const modal = el("settingsModal");
    el("btnOpenSettings").addEventListener("click", () => modal.classList.remove("hidden"));
    el("btnCloseSettings").addEventListener("click", () => modal.classList.add("hidden"));
    el("btnCancelSettings").addEventListener("click", () => modal.classList.add("hidden"));
    modal.addEventListener("click", (e) => {
      if (e.target === modal) modal.classList.add("hidden");
    });

    // Replay & AI actions
    el("btnResetSetup").addEventListener("click", () => sendWs({ action: "reset_replay", cursor: 234 }));
    el("btnStepBar").addEventListener("click", () => sendWs({ action: "step_replay", steps: 1 }));
    el("btnAutoReplay").addEventListener("click", () => sendWs({ action: "toggle_auto_replay" }));
    el("btnLiveMode").addEventListener("click", () => {
      sendWs({ action: "live_mode" });
      showToast("สลับกลับสู่กราฟราคาจริง (Live Feed)");
    });
    el("btnAnalyzeNow").addEventListener("click", () => {
      sendWs({ action: "analyze_now" });
      showToast("กำลังส่งกราฟและผลจำลอง 15,000 เส้นทางให้ Gemini วิเคราะห์...");
    });

    // Click-to-copy MT5 tiles
    document.querySelectorAll(".copy-trigger").forEach((tile) => {
      tile.addEventListener("click", () => {
        const targetId = tile.dataset.copyTarget;
        const val = el(targetId).textContent.trim();
        navigator.clipboard.writeText(val);
        showToast(`คัดลอก ${val} เรียบร้อยแล้ว`);
      });
    });

    el("btnCopyAllTicket").addEventListener("click", () => {
      if (!currentState || !currentState.risk_ticket) return;
      const tk = currentState.risk_ticket;
      const sim = currentState.simulation || {};
      const text = [
        `[MT5 Trade Ticket] ${tk.side} XAUUSD (${tk.verdict})`,
        `Volume: ${Number(tk.lot_size).toFixed(2)} Lot (Risk $${Number(tk.actual_risk_usd).toFixed(2)})`,
        `Entry: ${Number(tk.entry_mt5).toFixed(2)}`,
        `Stop Loss: ${Number(tk.stop_mt5).toFixed(2)}`,
        `Take Profit: ${Number(tk.target_mt5).toFixed(2)} (R:R 1:${tk.reward_risk})`,
        `Monte Carlo (${sim.replays || 15000} paths) EV: ${sim.expected_value_r}R | Win: ${(sim.probabilities || {}).win_pct}% | Sweep Risk: ${(sim.probabilities || {}).sweep_risk_pct}%`,
        `Scenario B Re-entry: ${Number(tk.scenario_b_reentry_mt5).toFixed(2)} | Scenario C Invalidation: ${Number(tk.scenario_c_invalidation_mt5).toFixed(2)}`,
      ].join("\n");
      navigator.clipboard.writeText(text);
      showToast("คัดลอกโพยคำสั่ง MT5 ทั้งชุดเรียบร้อย!");
    });

    // Save Settings Form
    el("settingsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = {
        account_balance: parseFloat(el("cfgBalance").value),
        risk_per_trade_pct: parseFloat(el("cfgRiskPct").value),
        broker_price_offset: parseFloat(el("cfgOffset").value),
        broker_spread_points: parseFloat(el("cfgSpread").value),
      };
      const keyVal = el("cfgGeminiKey").value.trim();
      if (keyVal) body.gemini_api_key = keyVal;

      modal.classList.add("hidden");
      showToast("กำลังบันทึกและสั่งวิเคราะห์ใหม่...");

      const resp = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.ok) {
        const state = await resp.json();
        renderDashboard(state);
        showToast("บันทึกการตั้งค่าและอัปเดตแผนเทรดเรียบร้อย!");
      }
    });
  }

  window.addEventListener("DOMContentLoaded", async () => {
    initChart();
    bindEvents();
    try {
      const resp = await fetch("/api/state");
      if (resp.ok) {
        const initial = await resp.json();
        renderDashboard(initial);
      }
    } catch (_) {}
    connectWebSocket();
  });
})();
