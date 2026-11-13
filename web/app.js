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
        const isMarket = tk.execution_type === "MARKET" || (tk.order_type && tk.order_type.includes("NOW"));
        const entryLabel = isMarket
          ? `⚡ เข้าทันที: ${tk.side || "BUY"} NOW`
          : `⏳ ดักเข้าล่วงหน้า: ${tk.side || "BUY"} LIMIT`;
        addHorizontalLine(tk.entry_mt5, entryLabel, isMarket ? "#38bdf8" : "#f5b841", 0);
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

      // Key Historical Support & Resistance reference lines
      const histSr = (state.structure && state.structure.historical_sr) || [];
      const curP = Number((state.structure && state.structure.current_price) || 0);
      const topRes = histSr.filter(z => z.effective_role === "resistance" || z.mid > curP).slice(0, 2);
      const topSup = histSr.filter(z => z.effective_role === "support" || z.mid <= curP).slice(0, 2);
      topRes.forEach(r => {
        addHorizontalLine(r.mid, `ต้านเดิม (${r.touches_count}x ${r.timeframes ? r.timeframes[0] : ""})`, "rgba(244,63,94,0.65)", 3);
      });
      topSup.forEach(s => {
        addHorizontalLine(s.mid, `รับเดิม (${s.touches_count}x ${s.timeframes ? s.timeframes[0] : ""})`, "rgba(16,185,129,0.65)", 3);
      });

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

    const isMarket = tk.execution_type === "MARKET" || (tk.order_type && tk.order_type.includes("NOW"));
    const entryCanvasLbl = isMarket ? "ENTRY (NOW)" : "ENTRY (LIMIT)";
    [
      { p: tk.entry_mt5, c: isMarket ? "#38bdf8" : "#f5b841", lbl: entryCanvasLbl },
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

  function updateActiveOrderDisplay(ord, curPrice) {
    const card = el("orderLifecycleCard");
    const statusPill = el("orderStatusPill");
    const execPill = el("orderExecTypePill");
    const pnlBadge = el("orderPnlBadge");
    const msg = el("orderStatusMsg");
    const btnFill = el("btnOrderFillNow");
    const btnAdd = el("btnAddSubOrder");
    const btnBE = el("btnOrderBreakeven");
    const btnClose = el("btnOrderCloseManual");
    const btnCancel = el("btnOrderCancel");
    const btnScan = el("btnScanNewSetup");
    const multiCard = el("multiOrdersCard");
    const multiList = el("multiOrdersList");
    const multiCount = el("multiOrdersCount");
    const multiAvg = el("multiOrdersAvgEntry");
    const ticketBadge = el("orderTicketBadge");

    if (!card || !statusPill) return;

    if (!ord || (ord.status !== "PENDING" && ord.status !== "FILLED")) {
      card.className = "order-lifecycle-bar status-idle";
      statusPill.className = "order-status-pill pill-idle";
      statusPill.textContent = "🔍 SCANNING";
      if (ticketBadge) ticketBadge.style.display = "none";
      if (execPill) {
        execPill.className = "order-exec-pill exec-waiting";
        execPill.textContent = "รอเงื่อนไข >50%";
      }
      if (pnlBadge) pnlBadge.style.display = "none";
      if (msg) msg.textContent = "กำลังติดตามโครงสร้างตลาด SMC เมื่อ Confluence และโอกาสถึง TP > 50% ระบบจะล็อคแผนเข้าทันที";
      if (btnFill) btnFill.style.display = "none";
      if (btnAdd) btnAdd.style.display = "none";
      if (btnBE) btnBE.style.display = "none";
      if (btnClose) btnClose.style.display = "none";
      if (btnCancel) btnCancel.style.display = "none";
      if (btnScan) btnScan.style.display = "inline-flex";
      if (multiCard) multiCard.style.display = "none";
      return;
    }

    const side = ord.side || "BUY";
    const status = ord.status;
    const isMarket = ord.execution_type === "MARKET";
    const entry = Number(ord.entry_mt5 || 0);

    if (ticketBadge) {
      if (ord.broker_ticket) {
        ticketBadge.textContent = `Ticket #${ord.broker_ticket}`;
        ticketBadge.style.display = "inline-flex";
      } else {
        ticketBadge.style.display = "none";
      }
    }

    // Multi-Order Rendering
    const hasMulti = ord.orders && ord.orders.length > 1;
    if (hasMulti) {
      if (multiCard) multiCard.style.display = "flex";
      if (multiCount) multiCount.textContent = ord.orders.length;
      if (multiAvg) {
        multiAvg.textContent = `ทุนเฉลี่ย: ${Number(ord.avg_entry_price || entry).toFixed(2)} (${Number(ord.total_lot_size || 0).toFixed(2)} Lot)`;
      }
      if (multiList) {
        multiList.innerHTML = ord.orders.map((o) => {
          const oEntry = Number(o.filled_price || o.entry_mt5 || 0).toFixed(2);
          const oLot = Number(o.lot_size || 0.1).toFixed(2);
          const pnlUsd = Number(o.floating_pnl_usd || 0);
          const pnlR = Number(o.floating_pnl_r || 0);
          const pnlClass = pnlUsd >= 0 ? "pnl-pos" : "pnl-neg";
          const pnlStr = o.status === "FILLED"
            ? `<span class="sub-order-pnl ${pnlClass}">${pnlUsd >= 0 ? "+" : ""}$${pnlUsd.toFixed(2)} (${pnlR >= 0 ? "+" : ""}${pnlR.toFixed(2)}R)</span>`
            : `<span class="sub-order-pending" style="color:var(--text-muted);font-size:10px;">⏳ รอเกี่ยว</span>`;
          const bClass = o.purpose === "RECOVERY" ? "badge-recovery" : o.purpose === "SCALE_IN" ? "badge-scale" : "badge-initial";

          return `
            <div class="sub-order-row">
              <span class="sub-order-badge ${bClass}">${o.purpose_th || ("ไม้ #" + o.order_index)}</span>
              <div class="sub-order-info">
                <span>${o.side} @ ${oEntry} (${oLot})</span>
                ${pnlStr}
              </div>
              <button class="btn-close-sub" data-order-id="${o.order_id}" title="จบการเทรดไม้นี้">✓ จบไม้นี้</button>
            </div>
          `;
        }).join("");

        multiList.querySelectorAll(".btn-close-sub").forEach((b) => {
          b.addEventListener("click", (e) => {
            e.stopPropagation();
            const oid = b.dataset.orderId;
            if (confirm("ยืนยันจบการเทรดไม้นี้?")) {
              sendWs({ action: "close_order_by_id", order_id: oid, current_price: curPrice });
              showToast("✓ จบการเทรดไม้นี้เรียบร้อย");
            }
          });
        });
      }
    } else {
      if (multiCard) multiCard.style.display = "none";
    }

    if (status === "FILLED") {
      card.className = "order-lifecycle-bar status-filled";
      statusPill.className = "order-status-pill pill-filled";
      statusPill.textContent = hasMulti ? `🚀 FILLED (${side} • ${ord.orders.length} ไม้)` : `🚀 FILLED (${side})`;
      if (execPill) {
        execPill.className = "order-exec-pill exec-holding";
        execPill.textContent = `🚀 ถือครอง (${ord.execution_type || "MARKET"})`;
      }

      const fltUsd = ord.floating_pnl_usd !== undefined ? Number(ord.floating_pnl_usd) : 0.0;
      const fltR = ord.floating_pnl_r !== undefined ? Number(ord.floating_pnl_r) : 0.0;

      if (pnlBadge) {
        pnlBadge.style.display = "inline-block";
        pnlBadge.className = "order-pnl-badge " + (fltUsd >= 0 ? "pnl-pos" : "pnl-neg");
        pnlBadge.textContent = `PnL: ${fltUsd >= 0 ? "+" : ""}$${fltUsd.toFixed(2)} (${fltR >= 0 ? "+" : ""}${fltR.toFixed(2)}R)`;
      }

      if (msg) {
        msg.textContent = ord.management_note_th || `ถือครอง ${side} @ ${entry.toFixed(2)} — ถึง TP1 แบ่งปิด 50% และเลื่อน SL บังทุน`;
      }

      if (btnFill) btnFill.style.display = "none";
      if (btnAdd) btnAdd.style.display = "inline-flex";
      if (btnBE) {
        btnBE.style.display = "inline-flex";
        if (ord.sl_moved_breakeven) {
          btnBE.textContent = "✓ เลื่อนบังทุนแล้ว";
          btnBE.disabled = true;
          btnBE.style.opacity = "0.7";
        } else {
          btnBE.textContent = "🛡️ บังทุนแล้ว";
          btnBE.disabled = false;
          btnBE.style.opacity = "1";
        }
      }
      if (btnClose) {
        btnClose.style.display = "inline-flex";
        btnClose.textContent = hasMulti ? `✓ จบการเทรดทุกไม้ (${ord.orders.length})` : "✓ จบการเทรดไม้นี้";
      }
      if (btnCancel) btnCancel.style.display = "none";
      if (btnScan) btnScan.style.display = "inline-flex";

    } else if (status === "PENDING") {
      card.className = "order-lifecycle-bar status-pending";
      statusPill.className = "order-status-pill pill-pending";
      statusPill.textContent = `⏳ รอดักเข้า (${side} LIMIT)`;
      if (execPill) {
        execPill.className = "order-exec-pill exec-limit";
        execPill.textContent = "⏳ ดัก LIMIT (Pending)";
      }
      if (pnlBadge) pnlBadge.style.display = "none";

      const dist = Math.abs(curPrice - entry);
      const dir = side === "SELL" ? "รอเด้งอีก" : "รอย่ออีก";
      if (msg) {
        msg.textContent = ord.management_note_th || `ตั้งคำสั่ง ${side} LIMIT ใน MT5 แล้ว ${dir} $${dist.toFixed(2)} (จุดเข้าล็อคถาวร)`;
      }

      if (btnFill) btnFill.style.display = "inline-flex";
      if (btnAdd) btnAdd.style.display = "inline-flex";
      if (btnBE) btnBE.style.display = "none";
      if (btnClose) btnClose.style.display = "none";
      if (btnCancel) btnCancel.style.display = "inline-flex";
      if (btnScan) btnScan.style.display = "inline-flex";
    }
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

    // MT5 EA Bridge Status & Auto-Trading Master Switch
    const ea = state.ea_bridge || {};
    const mt5Pill = el("mt5StatusPill");
    const mt5Text = el("mt5StatusText");
    if (mt5Pill && mt5Text) {
      if (ea.is_connected) {
        mt5Pill.className = "meta-pill pill-mt5-online";
        const eqStr = ea.equity ? `$${Number(ea.equity).toLocaleString()}` : "";
        mt5Text.textContent = `MT5: #${ea.account_id || ""} (${ea.broker || "Live"}) ${eqStr ? `• Eq ${eqStr}` : ""}`;
        mt5Pill.title = `MetaTrader 5 เชื่อมต่อแล้ว (Balance: $${ea.balance}, Margin: $${ea.margin}, Spread: ${ea.spread_points} pts)`;
      } else {
        mt5Pill.className = "meta-pill pill-mt5-offline";
        mt5Text.textContent = "MT5: Offline";
        mt5Pill.title = "MetaTrader 5 EA ยังไม่ได้เชื่อมต่อ (เปิด MT5 บน Windows/VPS แล้วรัน KAIROS79_Bridge.mq5)";
      }
    }

    const autoBtn = el("btnToggleAutoTrading");
    const autoText = el("autoTradingText");
    if (autoBtn && autoText) {
      const isAuto = ea.auto_trading_enabled === true;
      autoBtn.className = "btn-auto-trading " + (isAuto ? "auto-on" : "auto-off");
      autoText.textContent = isAuto ? "⚡ AUTO: ON" : "⏸️ AUTO: OFF";
      autoBtn.title = isAuto
        ? "ระบบกำลังส่งออเดอร์อัตโนมัติไปยัง MT5 เมื่อสัญญาณผ่านเกณฑ์ (คลิกเพื่อปิด)"
        : "ระบบหยุดส่งคำสั่งอัตโนมัติชั่วคราว (คลิกเพื่อเปิดใช้งาน Auto-Trading)";
    }

    if (el("cfgAutoTrading")) {
      el("cfgAutoTrading").checked = ea.auto_trading_enabled === true;
    }

    const tBadge = el("trendBadge");
    tBadge.textContent = `${trend.regime || "-"} (${trend.bias || "-"})`;
    tBadge.className =
      "meta-pill " +
      (trend.bias === "BULLISH" ? "pill-bull" : trend.bias === "BEARISH" ? "pill-bear" : "pill-neutral");

    const vf = state.market_flow || st.volume_flow || {};
    if (el("volFlowBadge") && vf.rvol !== undefined) {
      const rvolNum = Number(vf.rvol || 1.0).toFixed(2);
      el("volFlowBadge").textContent = `Vol ${rvolNum}x • ${vf.badge_th || vf.volume_label_th || ""}`;
      el("volFlowBadge").title = vf.headline_th || "";
      el("volFlowBadge").className =
        "meta-pill " +
        (vf.flow_type === "TREND_EXPANSION"
          ? trend.bias === "BEARISH"
            ? "pill-bear"
            : "pill-bull"
          : "pill-neutral");
    }

    // Compact Single-Row Floating Chart HUD
    const barCount = (state.bars && state.bars.length) || st.bars_count || 1500;
    if (el("barsCountBadge")) {
      el("barsCountBadge").textContent = `📊 ${Number(barCount).toLocaleString()} แท่ง`;
    }

    const cp = state.chart_patterns || st.chart_patterns || {};
    const activePat = cp.active_pattern || st.active_chart_pattern;
    if (el("chartPatternBadge")) {
      if (activePat) {
        el("chartPatternBadge").textContent = `📐 ${activePat.name_th || activePat.name}: ${activePat.status_th || activePat.status}`;
        el("chartPatternBadge").className =
          "hud-tag " +
          (activePat.bias === "BULLISH"
            ? "hud-tag-bull"
            : activePat.bias === "BEARISH"
            ? "hud-tag-bear"
            : "");
      } else {
        el("chartPatternBadge").textContent = `📐 Pattern: ปกติ`;
        el("chartPatternBadge").className = "hud-tag";
      }
    }

    el("atrBadge").textContent = `ATR: $${Number(st.atr14 || 0).toFixed(2)}`;
    el("hudEma20").textContent = `20: ${trend.ema20 || "-"}`;
    el("hudEma50").textContent = `50: ${trend.ema50 || "-"}`;
    el("hudEma200").textContent = `200: ${trend.ema200 || "-"}`;

    const mtf = state.mtf || {};
    const macro = state.macro || {};
    const tracker = state.signal_tracker || {};
    const inst = st.institutional_levels || {};
    const rsiDiv = st.rsi_divergence || {};
    const cb = st.candle_behavior || {};
    const dr = cb.dealing_range || {};

    if (el("hudMtfCompact")) {
      const tfs = mtf.timeframes || {};
      const mtfMini = ["H4", "H1", "M15", "M5"]
        .filter((k) => tfs[k])
        .map((k) => `${k}${tfs[k].bias === "BULLISH" ? "▲" : tfs[k].bias === "BEARISH" ? "▼" : "◆"}`)
        .join(" ");
      const scalp = st.scalping || {};
      if (state.trading_style === "scalping" && scalp.ema9) {
        el("hudMtfCompact").textContent = `⚡ EMA9/21: ${scalp.ema9}/${scalp.ema21} • ${mtfMini || "Scalping"}`;
      } else {
        el("hudMtfCompact").textContent = `MTF: ${mtfMini || "-"}`;
      }
    }

    // Sync active trading style and timeframe buttons
    document.querySelectorAll(".style-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.style === (state.trading_style || "intraday"));
    });
    document.querySelectorAll(".tf-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tf === state.timeframe);
    });

    // Bottom Dock Tab 1 (Default): Volume & Causal Market Flow Overview
    if (el("marketFlowList")) {
      const rvol = Number(vf.rvol || 1.0);
      const buyPct = Number(vf.buy_volume_pct || 50);
      const sellPct = Number(vf.sell_volume_pct || 50);
      const curVol = Number(vf.current_volume || 0).toLocaleString();
      const avgVol = Number(vf.avg_volume_20 || 0).toLocaleString();
      const volClass = buyPct >= 55 ? "bull" : sellPct >= 55 ? "bear" : "warn";
      const flowClass =
        vf.flow_type === "TREND_EXPANSION"
          ? trend.bias === "BEARISH"
            ? "bear"
            : "bull"
          : "warn";
      const causes = (vf.causes_th || []).slice(0, 3);

      el("marketFlowList").innerHTML = `
        <div class="flow-card ${volClass}">
          <div class="flow-card-head">
            <strong>📊 วอลุ่ม &amp; แรงซื้อ-ขาย (RVOL ${rvol.toFixed(2)}x)</strong>
            <span class="flow-pill">${vf.volume_label_th || "-"}</span>
          </div>
          <div class="vol-meter-track">
            <div class="vol-meter-buy" style="width:${Math.max(4, buyPct)}%"></div>
            <div class="vol-meter-sell" style="width:${Math.max(4, sellPct)}%"></div>
          </div>
          <div class="flow-sub-meta">
            <span>🟢 ซื้อ <b>${buyPct.toFixed(0)}%</b> • 🔴 ขาย <b>${sellPct.toFixed(0)}%</b></span>
            <span>Vol: <b>${curVol}</b> / เฉลี่ย ${avgVol}</span>
          </div>
          <div class="flow-sub-meta">
            <span>${vf.pressure_summary_th || "-"}</span>
            <span>แกว่ง ${Number(vf.range_expansion_ratio || 1.0).toFixed(2)}x ATR</span>
          </div>
        </div>

        <div class="flow-card ${flowClass}">
          <div class="flow-card-head">
            <strong>${vf.badge_th || "🌊 สรุปสาเหตุกระแสตลาด"}</strong>
            <span class="flow-pill">${state.timeframe || "M5"} • ${trend.regime || "-"}</span>
          </div>
          <div class="flow-headline">${vf.headline_th || "กำลังวิเคราะห์ข้อมูลปริมาณซื้อขายและกระแสตลาด..."}</div>
          <div class="flow-causes-row">
            ${causes.map((c) => `<span class="cause-chip">• ${c}</span>`).join("")}
          </div>
        </div>
      `;
    }

    // Bottom Dock Tab 2: Top SMC Zones + Key MTF/Institutional/Macro Structure
    if (el("structureOverviewList")) {
      const structCards = [];

      // Active Chart Pattern Card (Deep Pattern Recognition)
      if (activePat) {
        const isBull = activePat.bias === "BULLISH";
        structCards.push(`
          <div class="mini-zone-card ${isBull ? "bull" : "bear"}">
            <div class="mini-zone-top">
              <strong>📐 ${activePat.name_th || activePat.name}</strong>
              <span class="mini-zone-price">เป้า $${Number(activePat.target).toFixed(2)}</span>
            </div>
            <div class="mini-zone-sub">
              <span class="badge-hist-touches">${activePat.status_th || activePat.status}</span>
              <span>Neckline $${Number(activePat.neckline).toFixed(2)} • ${activePat.description_th || ""}</span>
            </div>
          </div>
        `);
      }

      // 4-TF + Institutional summary card
      const tfs = mtf.timeframes || {};
      const tfLine = ["H4", "H1", "M15", "M5"]
        .filter((k) => tfs[k])
        .map((k) => `${k}:<b>${tfs[k].bias === "BULLISH" ? "▲UP" : tfs[k].bias === "BEARISH" ? "▼DN" : "◆FLAT"}</b>`)
        .join(" • ");
      const htfSup = mtf.nearest_htf_support ? `${mtf.nearest_htf_support.mid.toFixed(2)} (${mtf.nearest_htf_support.timeframe})` : "-";
      const htfRes = mtf.nearest_htf_resistance ? `${mtf.nearest_htf_resistance.mid.toFixed(2)} (${mtf.nearest_htf_resistance.timeframe})` : "-";
      structCards.push(`
        <div class="mini-zone-card ${mtf.overall_bias === "BULLISH" ? "bull" : mtf.overall_bias === "BEARISH" ? "bear" : "warn"}" style="min-width:290px">
          <div class="mini-zone-top">
            <strong>🌐 โครงสร้าง 4 ไทม์เฟรม (${mtf.overall_bias || "-"})</strong>
            <span class="mini-zone-price">รับ ${htfSup} | ต้าน ${htfRes}</span>
          </div>
          <div class="mini-zone-sub">${tfLine || "-"}<br/>${inst.vwap ? `VWAP: <b>${inst.vwap.toFixed(2)}</b> • POC: <b>${inst.poc.toFixed(2)}</b> • PDH/L: ${inst.pdh.toFixed(2)}/${inst.pdl.toFixed(2)}` : ""}</div>
        </div>
      `);

      // Top 2 Unmitigated Order Blocks
      (smc.order_blocks || []).slice(0, 2).forEach((ob) => {
        structCards.push(`
          <div class="mini-zone-card ${ob.bias === "BULLISH" ? "bull" : "bear"}">
            <div class="mini-zone-top">
              <strong>📐 ${ob.kind}</strong>
              <span class="mini-zone-price">${ob.low.toFixed(2)} – ${ob.high.toFixed(2)}</span>
            </div>
            <div class="mini-zone-sub">Score ${ob.confluence_score}/100 • ${(ob.confluence_factors || [])[0] || "โซนรอเข้าล่วงหน้า"}</div>
          </div>
        `);
      });

      // Top FVG
      const topFvg = (smc.fvgs || []).slice(-1)[0];
      if (topFvg) {
        structCards.push(`
          <div class="mini-zone-card ${topFvg.bias === "BULLISH" ? "bull" : "bear"}">
            <div class="mini-zone-top">
              <strong>⚡ ${topFvg.kind}</strong>
              <span class="mini-zone-price">${topFvg.low.toFixed(2)} – ${topFvg.high.toFixed(2)}</span>
            </div>
            <div class="mini-zone-sub">ช่องว่าง Imbalance กว้าง $${topFvg.width_usd.toFixed(2)}</div>
          </div>
        `);
      }

      // Macro DXY + RSI
      if (macro.dxy_price) {
        const mClass = macro.macro_gold_bias === "BULLISH" ? "bull" : macro.macro_gold_bias === "BEARISH" ? "bear" : "warn";
        structCards.push(`
          <div class="mini-zone-card ${mClass}">
            <div class="mini-zone-top">
              <strong>💵 DXY ${Number(macro.dxy_price).toFixed(2)} (${macro.dxy_change_pct >= 0 ? "+" : ""}${macro.dxy_change_pct}%)</strong>
              <span class="mini-zone-price">RSI ${rsiDiv.rsi14 || "-"}</span>
            </div>
            <div class="mini-zone-sub">${macro.summary_th || ""}${rsiDiv.divergence && rsiDiv.divergence !== "NONE" ? ` • ${rsiDiv.divergence}` : ""}</div>
          </div>
        `);
      }

      // Key Historical Support & Resistance Zones
      const histSr = (st.historical_sr || []);
      histSr.slice(0, 4).forEach((hz) => {
        const isFlip = hz.is_flip;
        const cls = isFlip ? "sr-flip" : hz.effective_role === "support" ? "bull" : "bear";
        const roleIcon = isFlip ? "🔄" : hz.effective_role === "support" ? "🛡️" : "🧱";
        structCards.push(`
          <div class="mini-zone-card ${cls}">
            <div class="mini-zone-top">
              <strong>${roleIcon} ${hz.label_th}</strong>
              <span class="mini-zone-price">${hz.mid.toFixed(2)}</span>
            </div>
            <div class="mini-zone-sub">
              <span class="badge-hist-touches">ทดสอบ ${hz.touches_count} ครั้ง</span>
              ${isFlip ? '<span class="badge-hist-flip">S/R Flip</span>' : ''}
              <span>${hz.description_th || ""}</span>
            </div>
          </div>
        `);
      });

      el("structureOverviewList").innerHTML = structCards.join("") || `<div class="mini-zone-card">กำลังสแกนหาโซน SMC ใหม่...</div>`;
    }

    // Bottom Dock Tab 3: Signal Win-Rate Stats & Upcoming USD Red News
    if (el("statsNewsList")) {
      const sCards = [];
      const cumR = Number(tracker.cumulative_r || 0);
      sCards.push(`
        <div class="mini-zone-card ${cumR >= 0 ? "bull" : "bear"}" style="min-width:270px">
          <div class="mini-zone-top">
            <strong>🏆 Win Rate: ${Number(tracker.win_rate_pct || 0).toFixed(1)}% (${tracker.win_count || 0}W / ${tracker.loss_count || 0}L)</strong>
            <span class="mini-zone-price">${cumR >= 0 ? "+" : ""}${cumR.toFixed(2)}R</span>
          </div>
          <div class="mini-zone-sub">Profit Factor: <b>${tracker.profit_factor || 1.0}</b> • ตรวจสัญญาณจริงบนกราฟ ${tracker.timeframe || state.timeframe}</div>
        </div>
      `);

      const redList = news.upcoming_usd_red_news || [];
      if (redList.length) {
        redList.slice(0, 2).forEach((ev) => {
          sCards.push(`
            <div class="mini-zone-card ${Math.abs(ev.minutes_away) <= 30 ? "bear" : "warn"}">
              <div class="mini-zone-top">
                <strong>🔴 ${ev.title}</strong>
                <span class="mini-zone-price">${ev.time_bkk}</span>
              </div>
              <div class="mini-zone-sub">${ev.minutes_away > 0 ? `อีก ${ev.minutes_away} นาที` : `${Math.abs(ev.minutes_away)} นาทีที่แล้ว`} • คาดการณ์: ${ev.forecast}</div>
            </div>
          `);
        });
      } else {
        sCards.push(`
          <div class="mini-zone-card ${session.is_rollover_danger ? "warn" : "bull"}">
            <div class="mini-zone-top">
              <strong>✅ ปลอดข่าวกล่องแดง USD (${session.session || "-"})</strong>
              <span class="mini-zone-price">${session.liquidity || ""}</span>
            </div>
            <div class="mini-zone-sub">${session.label_th || "ไม่มีข่าว High Impact ในช่วงเวลานี้"}</div>
          </div>
        `);
      }

      ((tracker.recent_signals) || []).slice(0, 2).forEach((sig) => {
        const cls = sig.outcome.startsWith("WIN") ? "bull" : sig.outcome === "OPEN" ? "warn" : "bear";
        sCards.push(`
          <div class="mini-zone-card ${cls}">
            <div class="mini-zone-top">
              <strong>${sig.side} @ ${Number(sig.entry).toFixed(2)} (${sig.time_bkk})</strong>
              <span class="mini-zone-price">${sig.pnl_r > 0 ? `+${sig.pnl_r}R` : sig.pnl_r < 0 ? `${sig.pnl_r}R` : "OPEN"}</span>
            </div>
            <div class="mini-zone-sub">${sig.outcome_label_th} • TP2 ${Number(sig.target).toFixed(2)} | SL ${Number(sig.stop).toFixed(2)}</div>
          </div>
        `);
      });

      el("statsNewsList").innerHTML = sCards.join("");
    }

    // Active Order Lifecycle Integration
    const activeOrd = state.active_order || tk.active_order;
    updateActiveOrderDisplay(activeOrd, Number(st.current_price || 0));

    // Right Sidebar: Hero MT5 Ticket (with > 50% TP Win Probability Continuous Scanner)
    const winProbPassed = tk.win_prob_passed !== false;
    const ticketCard = el("ticketCard");
    if (ticketCard) {
      if (activeOrd && activeOrd.status === "FILLED") {
        ticketCard.className = "ticket-hero " + (activeOrd.side === "SELL" ? "is-ready-bear" : "is-ready-bull");
      } else {
        ticketCard.className =
          "ticket-hero " +
          (!winProbPassed ? "is-wait" : tk.side === "SELL" ? "is-ready-bear" : "is-ready-bull");
      }
    }
    const styleTag = state.trading_style === "scalping" ? "⚡ SCALP" : "🎯 INTRADAY";

    const multiCfg = (state.config && state.config.multi_order) || {};
    const autoMultiPill = el("autoMultiOrderPill");
    if (autoMultiPill) {
      const isAuto = multiCfg.auto_mode_enabled !== false;
      autoMultiPill.textContent = isAuto ? "🤖 AUTO MULTI" : "MANUAL MULTI";
      autoMultiPill.style.opacity = isAuto ? "1" : "0.5";
      autoMultiPill.title = isAuto
        ? "ระบบกำลังสแกนหาโอกาสแก้ไม้ (DCA) และทำกำไรเพิ่ม (Scale) อัตโนมัติ"
        : "โหมดแมนนวล: ผู้ใช้กดปุ่มเปิดไม้เพิ่มเอง";
    }

    const isMarketTk = tk.execution_type === "MARKET" || (tk.order_type && tk.order_type.includes("NOW"));
    const sideTk = tk.side || "BUY";
    const entryTk = Number(tk.entry_mt5 || 0).toFixed(2);

    if (activeOrd && activeOrd.status === "FILLED") {
      const ordersCnt = (activeOrd.orders && activeOrd.orders.length) || 1;
      if (ordersCnt > 1) {
        el("ticketSideTitle").textContent = `🚀 ${activeOrd.side} (${ordersCnt} ไม้)`;
        el("statusSub").textContent = `🚀 ถือครอง ${ordersCnt} ไม้ • ทุนเฉลี่ย ${Number(activeOrd.avg_entry_price || activeOrd.entry_mt5).toFixed(2)}`;
      } else {
        el("ticketSideTitle").textContent = `🚀 ${activeOrd.side} XAUUSD (เปิดออเดอร์แล้ว)`;
        el("statusSub").textContent = `🚀 ถือครอง @ ${Number(activeOrd.entry_mt5).toFixed(2)}`;
      }
      el("ticketSideTitle").style.color = activeOrd.side === "SELL" ? "var(--bear)" : "var(--bull)";
      if (el("orderExecTypePill")) {
        el("orderExecTypePill").className = "order-exec-pill exec-holding";
        el("orderExecTypePill").textContent = `🚀 ถือครอง (${activeOrd.execution_type || "MARKET"})`;
      }
    } else if (activeOrd && activeOrd.status === "PENDING") {
      el("ticketSideTitle").textContent = `⏳ ${activeOrd.side} LIMIT (ดักเข้า)`;
      el("ticketSideTitle").style.color = activeOrd.side === "SELL" ? "var(--bear)" : "var(--bull)";
      el("statusSub").textContent = `⏳ ดักเข้า @ ${Number(activeOrd.entry_mt5).toFixed(2)}`;
      if (el("orderExecTypePill")) {
        el("orderExecTypePill").className = "order-exec-pill exec-limit";
        el("orderExecTypePill").textContent = "⏳ ดัก LIMIT (Pending)";
      }
    } else if (!winProbPassed) {
      el("ticketSideTitle").textContent = `⏳ WAIT (รอจังหวะ)`;
      el("ticketSideTitle").style.color = "var(--warn)";
      const wz = tk.next_watch_zone;
      const rawWatchPrice = wz && typeof wz === "object" ? wz.watch_entry : wz;
      const watchPrice = rawWatchPrice ? Number(rawWatchPrice).toFixed(2) : Number(tk.entry_mt5 || 0).toFixed(2);
      el("statusSub").textContent = `[${styleTag}] โอกาสชนะ ${probs.win_pct || 0}% (<50%)`;
      if (el("orderExecTypePill")) {
        el("orderExecTypePill").className = "order-exec-pill exec-waiting";
        el("orderExecTypePill").textContent = "🔍 เฝ้าระวัง";
      }
      if (el("orderStatusPill")) {
        el("orderStatusPill").className = "order-status-pill pill-idle";
        el("orderStatusPill").textContent = "🔍 SCANNING";
      }
      if (el("orderStatusMsg")) {
        el("orderStatusMsg").textContent = `โอกาสชนะตามสถิติ ${probs.win_pct || 0}% ยังไม่เกิน 50% — กำลังเฝ้ารอโซน ${watchPrice} และรอความได้เปรียบ`;
      }
      if (el("btnOrderFillNow")) el("btnOrderFillNow").style.display = "none";
      if (el("btnOrderCancel")) el("btnOrderCancel").style.display = "none";
    } else {
      if (isMarketTk) {
        el("ticketSideTitle").textContent = `⚡ ${sideTk} NOW (เข้าทันที)`;
        el("ticketSideTitle").style.color = sideTk === "SELL" ? "var(--bear)" : "var(--bull)";
        el("statusSub").textContent = `⚡ [${styleTag}] เข้าทันทีที่ราคาตลาด @ ${entryTk}`;
        if (el("orderExecTypePill")) {
          el("orderExecTypePill").className = "order-exec-pill exec-market";
          el("orderExecTypePill").textContent = "⚡ เข้าทันที (MARKET)";
        }
        if (el("orderStatusPill")) {
          el("orderStatusPill").className = "order-status-pill pill-market-ready";
          el("orderStatusPill").textContent = "⚡ สัญญาณเข้าทันที";
        }
        if (el("orderStatusMsg")) {
          el("orderStatusMsg").textContent = `⚡ สัญญาณยืนยันในโซนแล้ว — เปิดออเดอร์ใน MT5 ทันทีที่ราคาตลาด @ ${entryTk} (ไม่ต้องตั้งรอดัก)`;
        }
        if (el("orderLifecycleCard")) {
          el("orderLifecycleCard").className = "order-lifecycle-bar status-market-ready";
        }
      } else {
        el("ticketSideTitle").textContent = `⏳ ${sideTk} LIMIT (ดักล่วงหน้า)`;
        el("ticketSideTitle").style.color = sideTk === "SELL" ? "var(--bear)" : "var(--bull)";
        el("statusSub").textContent = `⏳ [${styleTag}] ดักเข้าล่วงหน้า @ ${entryTk}`;
        if (el("orderExecTypePill")) {
          el("orderExecTypePill").className = "order-exec-pill exec-limit";
          el("orderExecTypePill").textContent = "⏳ ดัก LIMIT (Pending)";
        }
        if (el("orderStatusPill")) {
          el("orderStatusPill").className = "order-status-pill pill-pending";
          el("orderStatusPill").textContent = "⏳ รอดักเข้า";
        }
        const dist = Number(tk.distance_to_entry_usd || Math.abs((st.current_price || 0) - (tk.entry_mt5 || 0)));
        const dirWord = sideTk === "SELL" ? "รอเด้งอีก" : "รอย่ออีก";
        if (el("orderStatusMsg")) {
          el("orderStatusMsg").textContent = `⏳ ราคายังไม่ถึงโซน — ตั้งคำสั่ง ${sideTk} LIMIT รอเกี่ยวใน MT5 ที่ ${entryTk} (${dirWord} $${dist.toFixed(2)})`;
        }
        if (el("orderLifecycleCard")) {
          el("orderLifecycleCard").className = "order-lifecycle-bar status-pending";
        }
      }
      if (el("btnOrderFillNow")) el("btnOrderFillNow").style.display = "inline-flex";
      if (el("btnOrderCancel")) el("btnOrderCancel").style.display = "inline-flex";
    }

    const vBadge = el("verdictBadge");
    vBadge.textContent = (activeOrd && activeOrd.status === "FILLED")
      ? "FILLED"
      : !winProbPassed
      ? "SCANNING <50%"
      : tk.verdict || "PASS";
    vBadge.className =
      "verdict-pill " +
      (!winProbPassed ? "caution" : (tk.verdict === "PASS" || (activeOrd && activeOrd.status === "FILLED")) ? "pass" : tk.verdict === "CAUTION" ? "caution" : "reject");

    el("confluenceBadge").textContent = `${setup.confluence_score || 0}%`;

    const activeLot = (activeOrd && activeOrd.orders && activeOrd.orders.length > 1)
      ? Number(activeOrd.total_lot_size || activeOrd.lot_size || 0).toFixed(2)
      : Number(tk.lot_size || 0).toFixed(2);
    el("valLot").textContent = activeLot;

    const lev = tk.leverage || (state.config && state.config.leverage) || 100;
    const marginInfo = tk.margin_required_usd ? ` • Margin $${Number(tk.margin_required_usd).toFixed(2)} (1:${Math.round(lev)})` : "";
    el("riskUsdSub").textContent = `เสี่ยง $${Number(tk.actual_risk_usd || 0).toFixed(2)} (${tk.risk_pct_configured || 1}%)${marginInfo}`;

    const activeEntry = (activeOrd && activeOrd.orders && activeOrd.orders.length > 1)
      ? Number(activeOrd.avg_entry_price || activeOrd.entry_mt5 || 0).toFixed(2)
      : Number(tk.entry_mt5 || 0).toFixed(2);
    el("valEntry").textContent = activeEntry;

    if (el("entryHeadLabel")) {
      const isMarket = (activeOrd && activeOrd.execution_type === "MARKET") || isMarketTk;
      if (activeOrd && activeOrd.orders && activeOrd.orders.length > 1) {
        el("entryHeadLabel").textContent = `ENTRY (ทุนเฉลี่ย ${activeOrd.orders.length} ไม้)`;
      } else if (activeOrd && activeOrd.status === "FILLED") {
        el("entryHeadLabel").textContent = `ENTRY (เปิดสำเร็จ @ ${activeEntry})`;
      } else if (isMarket) {
        el("entryHeadLabel").textContent = "ENTRY (เข้าทันที MARKET)";
      } else {
        el("entryHeadLabel").textContent = `ENTRY (ดัก ${tk.side || "BUY"} LIMIT)`;
      }
    }
    if (el("entryDistSub")) {
      const isMarket = (activeOrd && activeOrd.execution_type === "MARKET") || isMarketTk;
      if (activeOrd && activeOrd.status === "FILLED") {
        el("entryDistSub").textContent = "🚀 ออเดอร์กำลังทำงานในตลาด";
      } else if (isMarket) {
        el("entryDistSub").textContent = "⚡ เข้าทันทีที่ราคาตลาดปัจจุบัน";
      } else {
        const dist = Number(tk.distance_to_entry_usd || Math.abs((st.current_price || 0) - (tk.entry_mt5 || 0)));
        const dirWord = tk.side === "SELL" ? "รอเด้งอีก" : "รอย่ออีก";
        el("entryDistSub").textContent = dist > 0.05 ? `⏳ ${dirWord} $${dist.toFixed(2)} (ดักล่วงหน้า)` : `แตะโซนเข้าแล้ว`;
      }
    }
    el("valSL").textContent = Number(tk.stop_mt5 || 0).toFixed(2);
    el("slDistSub").textContent = `ห่าง $${Number(tk.sl_distance_usd || 0).toFixed(2)} (${tk.sl_points || 0} pts)`;
    el("valTP").textContent = Number(tk.target_mt5 || 0).toFixed(2);
    const tp1Str = tk.tp1_mt5 ? `TP1 ${Number(tk.tp1_mt5).toFixed(2)} • ` : "";
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

    // Only populate modal settings if modal is closed, to avoid overwriting user edits
    const modalEl = el("settingsModal");
    if (modalEl && modalEl.classList.contains("hidden")) {
      populateSettingsForm(state.config || {});
    }

    renderChartPatterns(state);

    if (state.is_new_signal_alert) {
      playAlertBeep(tk.verdict);
      showToast(`🔔 แจ้งเตือนสัญญาณ ${tk.side} XAUUSD (${tk.verdict})`);
    }
  }

  function populateSettingsForm(cfg) {
    if (!cfg) return;
    if (el("cfgBalance")) el("cfgBalance").value = cfg.account_balance ?? 5000;
    if (el("cfgLeverage")) {
      const curLev = cfg.leverage ?? 100;
      el("cfgLeverage").value = curLev;
      document.querySelectorAll(".btn-lev-chip").forEach((chip) => {
        chip.classList.toggle("active", Number(chip.dataset.lev) === Number(curLev));
      });
    }
    if (el("cfgRiskPct")) el("cfgRiskPct").value = cfg.risk_per_trade_pct ?? 1.0;
    if (el("cfgOffset")) el("cfgOffset").value = cfg.broker_price_offset ?? 0.0;
    if (el("cfgSpread")) el("cfgSpread").value = cfg.broker_spread_points ?? 25;
    if (el("cfgAutoMultiOrder")) {
      const multiCfg = cfg.multi_order || {};
      el("cfgAutoMultiOrder").checked = multiCfg.auto_mode_enabled !== false;
    }
  }

  function renderChartPatterns(state) {
    const st = state.structure || {};
    const cp = state.chart_patterns || st.chart_patterns || {};
    const activePat = cp.active_pattern || st.active_chart_pattern;
    const recentPatterns = cp.recent_patterns || st.recent_chart_patterns || [];
    const totalDetected = cp.total_detected || recentPatterns.length || 0;
    const completedCount = cp.completed_count || 0;
    const winRate = cp.success_rate_pct !== undefined ? cp.success_rate_pct : 0;
    const barsCount = (state.bars && state.bars.length) || st.bars_count || 1500;

    // Header badge
    if (el("patternsHeaderBadge")) {
      el("patternsHeaderBadge").textContent = `${Number(barsCount).toLocaleString()} แท่ง`;
    }

    // Stats summary pill
    if (el("patternStatsSummary")) {
      if (totalDetected > 0) {
        el("patternStatsSummary").textContent = `สำเร็จ ${completedCount}/${totalDetected} (${winRate}% Win Rate)`;
      } else {
        el("patternStatsSummary").textContent = `สแกน ${barsCount} แท่ง`;
      }
    }

    // Active Pattern Highlight Card
    const activeCardEl = el("patternActiveCard");
    if (activeCardEl) {
      if (activePat) {
        const isBull = activePat.bias === "BULLISH";
        const statusClass =
          activePat.status === "completed"
            ? "status-completed"
            : activePat.status === "breakout"
            ? "status-breakout"
            : "status-forming";
        const statusBadgeIcon =
          activePat.status === "completed"
            ? "🏆"
            : activePat.status === "breakout"
            ? "⚡"
            : "⏳";

        activeCardEl.className = `pattern-active-card ${isBull ? "bull" : "bear"}`;
        activeCardEl.innerHTML = `
          <div class="pat-head">
            <div class="pat-title-grp">
              <span class="pat-icon">${isBull ? "📈" : "📉"}</span>
              <strong>${activePat.name_th || activePat.name}</strong>
            </div>
            <span class="pat-status-badge ${statusClass}">
              ${statusBadgeIcon} ${activePat.status_th || activePat.status}
            </span>
          </div>
          <div class="pat-metrics-grid">
            <div class="pat-metric">
              <span class="pat-metric-label">NECKLINE</span>
              <strong class="pat-metric-val">$${Number(activePat.neckline || 0).toFixed(2)}</strong>
            </div>
            <div class="pat-metric">
              <span class="pat-metric-label">เป้าหมาย (TARGET)</span>
              <strong class="pat-metric-val highlight">$${Number(activePat.target || 0).toFixed(2)}</strong>
            </div>
            <div class="pat-metric">
              <span class="pat-metric-label">ระยะทำกำไร</span>
              <strong class="pat-metric-val">${activePat.height_usd ? `+$${Number(activePat.height_usd).toFixed(2)}` : "-"}</strong>
            </div>
          </div>
          <p class="pat-desc">${activePat.description_th || activePat.description || "-"}</p>
        `;
      } else {
        activeCardEl.className = "pattern-active-card neutral";
        activeCardEl.innerHTML = `
          <div class="pattern-active-empty">
            <span>🔍 ไม่พบ Chart Pattern ขนาดใหญ่ที่กำลังฟอร์มตัวใน 40 แท่งล่าสุด</span>
            <small>ระบบติดตามแนวรับต้านและโครงสร้าง SMC เป็นหลัก</small>
          </div>
        `;
      }
    }

    // Recent Historical Patterns List
    const listEl = el("recentPatternsList");
    if (listEl) {
      if (recentPatterns.length > 0) {
        listEl.innerHTML = recentPatterns
          .map((p) => {
            const isBull = p.bias === "BULLISH";
            const outcomeBadge =
              p.status === "completed"
                ? `<span class="pat-pill-done">✓ สำเร็จ</span>`
                : p.status === "breakout"
                ? `<span class="pat-pill-breakout">⚡ เบรคเอาท์</span>`
                : p.status === "invalidated"
                ? `<span class="pat-pill-cancel">✕ ยกเลิก</span>`
                : `<span class="pat-pill-forming">⏳ กำลังฟอร์ม</span>`;

            return `
            <div class="pat-item ${isBull ? "bull" : "bear"}">
              <div class="pat-item-left">
                <span class="pat-item-icon">${isBull ? "🟢" : "🔴"}</span>
                <div>
                  <strong>${p.name_th || p.name}</strong>
                  <div class="pat-item-sub">
                    Neckline $${Number(p.neckline).toFixed(2)} • เป้า $${Number(p.target).toFixed(2)}
                  </div>
                </div>
              </div>
              <div class="pat-item-right">
                ${outcomeBadge}
                <span class="pat-time-ago">${p.bars_ago !== undefined ? `${p.bars_ago} แท่งก่อน` : ""}</span>
              </div>
            </div>
          `;
          })
          .join("");
      } else {
        listEl.innerHTML = `<div class="empty-hint">ยังไม่พบประวัติ Pattern ในแท่งเทียนชุดนี้</div>`;
      }
    }
  }

  function handleLiveTick(msg) {
    if (!msg.bar) return;
    const b = msg.bar;
    const curP = Number(b.c);
    el("livePrice").textContent = curP.toFixed(2);
    if (msg.bid && msg.ask) {
      el("bidAskPill").textContent = `Bid ${Number(msg.bid).toFixed(2)} • Ask ${Number(msg.ask).toFixed(2)}`;
    }
    if (msg.active_order) {
      updateActiveOrderDisplay(msg.active_order, curP);
    } else if (currentState && currentState.active_order) {
      updateActiveOrderDisplay(currentState.active_order, curP);
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
        showToast(
          style === "scalping"
            ? "⚡ สลับสู่โหมดเทรดสั้น (Scalping) — กำลังวิเคราะห์จุดเข้าแบบรวดเร็ว..."
            : "🎯 สลับสู่โหมดปกติ (Intraday SMC) — กำลังวิเคราะห์จุดเข้ารอบสวิง..."
        );
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

    // Right Sidebar Collapsible Accordion Panels (ย่อหุบได้)
    document.querySelectorAll(".acc-head").forEach((btn) => {
      btn.addEventListener("click", () => {
        const card = btn.closest(".acc-card");
        if (!card) return;
        const body = card.querySelector(".acc-body");
        const indicator = card.querySelector(".acc-indicator");
        const isOpen = card.classList.contains("active");

        if (isOpen) {
          card.classList.remove("active");
          if (body) body.style.display = "none";
          if (indicator) indicator.textContent = "▸";
        } else {
          card.classList.add("active");
          if (body) body.style.display = "flex";
          if (indicator) indicator.textContent = "▾";
        }
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
    el("btnOpenSettings").addEventListener("click", () => {
      populateSettingsForm(currentState ? currentState.config : null);
      modal.classList.remove("hidden");
    });
    el("btnCloseSettings").addEventListener("click", () => modal.classList.add("hidden"));
    el("btnCancelSettings").addEventListener("click", () => modal.classList.add("hidden"));
    modal.addEventListener("click", (e) => {
      if (e.target === modal) modal.classList.add("hidden");
    });

    // Auto-Trading Master Switch Listeners (MT5 EA Bridge)
    const btnAuto = el("btnToggleAutoTrading");
    if (btnAuto) {
      btnAuto.addEventListener("click", async () => {
        try {
          const resp = await fetch("/api/ea/toggle_auto", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
          });
          const doc = await resp.json();
          const isNowOn = doc.auto_trading_enabled;
          showToast(
            isNowOn
              ? "⚡ เปิดใช้งาน Auto-Trading (MT5 EA) แล้ว"
              : "⏸️ ปิดการส่งออเดอร์ Auto-Trading แล้ว"
          );
          if (currentState) {
            currentState.ea_bridge = doc.ea_bridge || currentState.ea_bridge || {};
            currentState.ea_bridge.auto_trading_enabled = isNowOn;
            renderDashboard(currentState);
          }
        } catch (e) {
          showToast("❌ ไม่สามารถเปลี่ยนสถานะ Auto-Trading ได้");
        }
      });
    }

    const cfgAuto = el("cfgAutoTrading");
    if (cfgAuto) {
      cfgAuto.addEventListener("change", async () => {
        try {
          const resp = await fetch("/api/ea/toggle_auto", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: cfgAuto.checked }),
          });
          const doc = await resp.json();
          showToast(
            cfgAuto.checked
              ? "⚡ เปิดใช้งาน Auto-Trading แล้ว"
              : "⏸️ ปิด Auto-Trading แล้ว"
          );
          if (currentState) {
            currentState.ea_bridge = doc.ea_bridge || currentState.ea_bridge || {};
            currentState.ea_bridge.auto_trading_enabled = cfgAuto.checked;
            renderDashboard(currentState);
          }
        } catch (e) {}
      });
    }

    // AI action (Replay mode removed per user request)
    el("btnAnalyzeNow").addEventListener("click", () => {
      sendWs({ action: "analyze_now" });
      showToast("กำลังส่งกราฟและผลจำลอง 15,000 เส้นทางให้ Gemini วิเคราะห์...");
    });

    // Order Action Buttons (Fill Now, Breakeven, Close, Cancel, Scan New)
    const getCurPrice = () => {
      if (currentState && currentState.structure && currentState.structure.current_price) {
        return Number(currentState.structure.current_price);
      }
      return parseFloat(el("livePrice").textContent) || 0;
    };

    if (el("btnOrderFillNow")) {
      el("btnOrderFillNow").addEventListener("click", () => {
        sendWs({ action: "fill_active_order_now", current_price: getCurPrice() });
        showToast("⚡ บันทึกสถานะ: เปิดออเดอร์แล้ว!");
      });
    }

    if (el("btnAddSubOrder")) {
      el("btnAddSubOrder").addEventListener("click", () => {
        const cp = getCurPrice();
        sendWs({ action: "add_sub_order", purpose: "AUTO", execution_type: "MARKET", current_price: cp });
        showToast("⚡ ส่งคำสั่งเปิดไม้เพิ่ม (แก้ไม้/สเกล) เรียบร้อย!");
      });
    }

    if (el("btnOrderBreakeven")) {
      el("btnOrderBreakeven").addEventListener("click", () => {
        sendWs({ action: "set_breakeven" });
        showToast("🛡️ บันทึกสถานะ: เลื่อน Stop Loss บังทุนเรียบร้อย!");
      });
    }

    if (el("btnOrderCloseManual")) {
      el("btnOrderCloseManual").addEventListener("click", () => {
        const ord = (currentState && currentState.risk_ticket && currentState.risk_ticket.active_order) || {};
        const hasMulti = ord.orders && ord.orders.length > 1;
        const confirmMsg = hasMulti
          ? `ยืนยันว่าต้องการจบการเทรดทุกไม้ (${ord.orders.length} ไม้)?`
          : "ยืนยันว่าต้องการจบการเทรดไม้นี้?";
        if (confirm(confirmMsg)) {
          sendWs({ action: "close_active_order", current_price: getCurPrice() });
          showToast("✓ จบการเทรดเรียบร้อย — เริ่มสแกนรอบใหม่");
        }
      });
    }

    if (el("btnOrderCancel")) {
      el("btnOrderCancel").addEventListener("click", () => {
        if (confirm("ยืนยันยกเลิกคำสั่งดักเข้าที่รออยู่?")) {
          sendWs({ action: "cancel_active_order", reason: "MANUAL_CANCEL" });
          showToast("✕ ยกเลิกคำสั่งเรียบร้อย — ระบบจะเริ่มสแกนหาจังหวะใหม่");
        }
      });
    }

    if (el("btnScanNewSetup")) {
      el("btnScanNewSetup").addEventListener("click", () => {
        sendWs({ action: "scan_new_setup" });
        showToast("🔄 กำลังรีเซ็ตและสั่ง AI สแกนวิเคราะห์หาแผนใหม่...");
      });
    }

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
      const isMarket = tk.execution_type === "MARKET" || (tk.order_type && tk.order_type.includes("NOW"));
      const actionType = isMarket ? "เข้าทันทีที่ราคาตลาด (MARKET)" : "ตั้งคำสั่งดักล่วงหน้า (PENDING LIMIT)";
      const text = [
        `[MT5 Trade Ticket] ${tk.side} XAUUSD — ${actionType} (${tk.verdict})`,
        `Volume: ${Number(tk.lot_size).toFixed(2)} Lot (Risk $${Number(tk.actual_risk_usd).toFixed(2)}) • Margin $${Number(tk.margin_required_usd || 0).toFixed(2)} (Leverage 1:${Math.round(tk.leverage || 100)})`,
        `Entry: ${Number(tk.entry_mt5).toFixed(2)}`,
        `Stop Loss: ${Number(tk.stop_mt5).toFixed(2)}`,
        `Take Profit: ${Number(tk.target_mt5).toFixed(2)} (R:R 1:${tk.reward_risk})`,
        `Monte Carlo (${sim.replays || 15000} paths) EV: ${sim.expected_value_r}R | Win: ${(sim.probabilities || {}).win_pct}% | Sweep Risk: ${(sim.probabilities || {}).sweep_risk_pct}%`,
        `Scenario B Re-entry: ${Number(tk.scenario_b_reentry_mt5).toFixed(2)} | Scenario C Invalidation: ${Number(tk.scenario_c_invalidation_mt5).toFixed(2)}`,
      ].join("\n");
      navigator.clipboard.writeText(text);
      showToast("คัดลอกโพยคำสั่ง MT5 ทั้งชุดเรียบร้อย!");
    });

    // Leverage Preset Buttons in Settings Modal
    document.querySelectorAll(".btn-lev-chip").forEach((chip) => {
      chip.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const levVal = chip.dataset.lev;
        const levInput = el("cfgLeverage");
        if (levInput) {
          levInput.value = levVal;
          levInput.dispatchEvent(new Event("input", { bubbles: true }));
        }
        document.querySelectorAll(".btn-lev-chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
      });
    });

    if (el("cfgLeverage")) {
      el("cfgLeverage").addEventListener("input", () => {
        const val = Number(el("cfgLeverage").value);
        document.querySelectorAll(".btn-lev-chip").forEach((chip) => {
          chip.classList.toggle("active", Number(chip.dataset.lev) === val);
        });
      });
    }

    // Save Settings Form
    el("settingsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const rawLev = parseFloat(el("cfgLeverage") ? el("cfgLeverage").value : 100);
      const lev = Math.max(1, Math.min(5000, isNaN(rawLev) ? 100 : rawLev));
      const body = {
        account_balance: parseFloat(el("cfgBalance").value) || 5000,
        leverage: lev,
        risk_per_trade_pct: parseFloat(el("cfgRiskPct").value) || 1.0,
        broker_price_offset: parseFloat(el("cfgOffset").value) || 0.0,
        broker_spread_points: parseFloat(el("cfgSpread").value) || 25,
        multi_order: {
          auto_mode_enabled: el("cfgAutoMultiOrder") ? el("cfgAutoMultiOrder").checked : true,
        },
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
