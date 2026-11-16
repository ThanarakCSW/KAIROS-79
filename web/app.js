(() => {
  let ws = null;
  let currentState = null;
  let lastReportedExecId = null;
  const CURRENT_SYMBOL =
    window.CURRENT_SYMBOL || (window.location.pathname.includes("btc") ? "BTCUSD" : "XAUUSD");

  const el = (id) => document.getElementById(id);

  function fmtPrice(val, digits = 2) {
    const n = Number(val);
    if (!Number.isFinite(n) || n === 0) return "—";
    return n.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function fmtUsd(val) {
    const n = Number(val || 0);
    const sign = n > 0 ? "+" : n < 0 ? "-" : "";
    return `${sign}$${Math.abs(n).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

  function showToast(msg) {
    const t = el("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.remove("hidden");
    setTimeout(() => t.classList.add("hidden"), 2600);
  }

  function getRoleMeta(purposeRaw) {
    const p = String(purposeRaw || "INITIAL").toUpperCase();
    if (p === "RECOVERY" || p.includes("DCA")) {
      return {
        code: "RECOVERY",
        badge: "🛡️ ไม้แก้ขาดทุน (Recovery / DCA)",
        shortBadge: "🛡️ ไม้แก้ขาดทุน",
        cssClass: "role-recovery",
        desc: "เปิดเพิ่มอัตโนมัติเมื่อราคาย่อเข้าโซนรับสำคัญ เพื่อดึงราคาทุนเฉลี่ยให้กลับมาปิดกำไรได้เร็วขึ้น",
      };
    }
    if (p === "SCALE_IN" || p.includes("SCALE")) {
      return {
        code: "SCALE_IN",
        badge: "🚀 ไม้ทบเพิ่มกำไร (Scale-In)",
        shortBadge: "🚀 ไม้ทบเพิ่มกำไร",
        desc: "เปิดทบเพิ่มอัตโนมัติเมื่อออเดอร์หลักบวกและแนวโน้มวิ่งต่อแข็งแกร่ง เพื่อขยายผลกำไร",
        cssClass: "role-scale",
      };
    }
    return {
      code: "INITIAL",
      badge: "🎯 ไม้ทำกำไรหลัก (Initial Entry)",
      shortBadge: "🎯 ไม้ทำกำไรหลัก",
      desc: "ไม้แรกที่ระบบตัดสินใจเข้าทำกำไรอัตโนมัติเมื่อโครงสร้างและค่าคาดหวังผ่านเกณฑ์",
      cssClass: "role-initial",
    };
  }

  function buildUnifiedActiveOrders(state, currentPrice) {
    const activeOrd = state.active_order || (state.risk_ticket && state.risk_ticket.active_order) || null;
    const ea = state.ea_bridge || {};
    const mt5Open = ea.open_positions || [];
    const mt5Pend = ea.pending_orders || [];

    const posByTicket = {};
    mt5Open.forEach((p) => {
      if (p.ticket) posByTicket[String(p.ticket)] = p;
    });
    const pendByTicket = {};
    mt5Pend.forEach((p) => {
      if (p.ticket) pendByTicket[String(p.ticket)] = p;
    });

    const unified = [];
    const seenTickets = new Set();

    if (activeOrd) {
      const rawOrders =
        Array.isArray(activeOrd.orders) && activeOrd.orders.length > 0
          ? activeOrd.orders
          : [activeOrd];

      rawOrders.forEach((o, idx) => {
        const ticketStr = o.broker_ticket ? String(o.broker_ticket) : "";
        if (ticketStr) seenTickets.add(ticketStr);

        const livePos = ticketStr ? posByTicket[ticketStr] : null;
        const livePend = ticketStr ? pendByTicket[ticketStr] : null;

        const side = String(o.side || (livePos && livePos.type) || "BUY").toUpperCase();
        const entry = Number(
          (livePos && livePos.open_price) ||
            o.filled_price ||
            o.entry_mt5 ||
            (livePend && livePend.open_price) ||
            0
        );
        const sl = Number((livePos && livePos.sl) || o.stop_mt5 || 0);
        const tp1 = Number(o.tp1_mt5 || 0);
        const tp2 = Number((livePos && livePos.tp) || o.target_mt5 || 0);
        const lot = Number((livePos && (livePos.lots || livePos.volume)) || o.lot_size || 0.01);
        const cSize = Number(o.contract_size || (CURRENT_SYMBOL.includes("BTC") ? 1 : 100));
        const riskUsd = Math.max(1, Number(o.actual_risk_usd || Math.abs(entry - sl) * lot * cSize || 50));

        let status = String(o.status || "PENDING").toUpperCase();
        if (livePos) status = "FILLED";

        let pnlUsd = Number(o.floating_pnl_usd || 0);
        let pnlR = Number(o.floating_pnl_r || 0);
        if (livePos && livePos.profit !== undefined) {
          pnlUsd = Number(livePos.profit);
          pnlR = riskUsd > 0 ? pnlUsd / riskUsd : 0;
        } else if (status === "FILLED" && currentPrice > 0 && entry > 0) {
          const delta = side === "BUY" ? currentPrice - entry : entry - currentPrice;
          pnlUsd = delta * lot * cSize;
          pnlR = riskUsd > 0 ? pnlUsd / riskUsd : 0;
        }

        unified.push({
          order_index: o.order_index || idx + 1,
          order_id: o.order_id || `ORD-${idx + 1}`,
          broker_ticket: o.broker_ticket || (livePos && livePos.ticket) || null,
          symbol: o.symbol || CURRENT_SYMBOL,
          side,
          purpose: o.purpose || (idx === 0 ? "INITIAL" : "RECOVERY"),
          execution_type: o.execution_type || (status === "FILLED" ? "MARKET" : "LIMIT"),
          status,
          entry_mt5: entry,
          stop_mt5: sl,
          tp1_mt5: tp1,
          target_mt5: tp2,
          breakeven_sl_mt5: Number(o.breakeven_sl_mt5 || entry),
          lot_size: lot,
          actual_risk_usd: riskUsd,
          potential_profit_usd: Number(o.potential_profit_usd || Math.abs(tp2 - entry) * lot * cSize || 0),
          reward_risk: Number(o.reward_risk || ( Math.abs(entry - sl) > 0 ? Math.abs(tp2 - entry) / Math.abs(entry - sl) : 0 )),
          floating_pnl_usd: pnlUsd,
          floating_pnl_r: pnlR,
          sl_moved_breakeven: Boolean(o.sl_moved_breakeven),
          tp1_hit: Boolean(o.tp1_hit),
          trailing_step_index: Number(o.trailing_step_index || 0),
          created_bkk: o.created_bkk || "อัตโนมัติ",
          management_note_th: o.management_note_th || "",
          trigger_reason: o.trigger_reason || "",
        });
      });
    }

    // Also include any direct MT5 open positions for this symbol not yet in activeOrd
    mt5Open.forEach((p) => {
      const sym = String(p.symbol || CURRENT_SYMBOL).toUpperCase();
      const isBtcPage = CURRENT_SYMBOL.includes("BTC");
      if (isBtcPage !== sym.includes("BTC")) return;
      const tStr = String(p.ticket || "");
      if (tStr && seenTickets.has(tStr)) return;
      if (tStr) seenTickets.add(tStr);

      const idx = unified.length + 1;
      const side = String(p.type || p.side || "BUY").toUpperCase().includes("SELL") ? "SELL" : "BUY";
      const entry = Number(p.open_price || p.price || 0);
      const sl = Number(p.sl || 0);
      const tp2 = Number(p.tp || 0);
      const lot = Number(p.lots || p.volume || 0.01);
      const cSize = isBtcPage ? 1 : 100;
      const riskUsd = Math.max(1, Math.abs(entry - sl) * lot * cSize || 50);
      const pnlUsd = Number(p.profit || 0);
      const pnlR = riskUsd > 0 ? pnlUsd / riskUsd : 0;
      const comment = String(p.comment || "").toUpperCase();
      let purp = idx === 1 ? "INITIAL" : "RECOVERY";
      if (comment.includes("RECOVERY") || comment.includes("DCA")) purp = "RECOVERY";
      else if (comment.includes("SCALE")) purp = "SCALE_IN";

      unified.push({
        order_index: idx,
        order_id: p.comment || `MT5-${tStr}`,
        broker_ticket: p.ticket,
        symbol: sym,
        side,
        purpose: purp,
        execution_type: "MARKET",
        status: "FILLED",
        entry_mt5: entry,
        stop_mt5: sl,
        tp1_mt5: 0,
        target_mt5: tp2,
        breakeven_sl_mt5: entry,
        lot_size: lot,
        actual_risk_usd: riskUsd,
        potential_profit_usd: tp2 > 0 ? Math.abs(tp2 - entry) * lot * cSize : 0,
        reward_risk: Math.abs(entry - sl) > 0 && tp2 > 0 ? Math.abs(tp2 - entry) / Math.abs(entry - sl) : 0,
        floating_pnl_usd: pnlUsd,
        floating_pnl_r: pnlR,
        sl_moved_breakeven: false,
        tp1_hit: false,
        trailing_step_index: 0,
        created_bkk: "MT5 Live",
        management_note_th: "ออเดอร์เปิดทำงานอยู่ในพอร์ต MetaTrader 5",
        trigger_reason: "",
      });
    });

    return unified;
  }

  function updateEABridgeUI(ea) {
    if (!ea) return;
    const mt5Pill = el("mt5StatusPill");
    const mt5Text = el("mt5StatusText");
    if (mt5Pill && mt5Text) {
      if (ea.is_connected && ea.account_id) {
        mt5Pill.className = "meta-pill pill-mt5-online";
        const eqStr = ea.equity ? `$${Number(ea.equity).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "";
        mt5Text.textContent = `MT5: #${ea.account_id} (${ea.broker || "Connected"}) ${eqStr ? `• ${eqStr}` : ""}`;
      } else if (ea.is_connected) {
        mt5Pill.className = "meta-pill pill-mt5-online";
        mt5Text.textContent = "MT5: Connected (Auto Polling)";
      } else {
        mt5Pill.className = "meta-pill pill-mt5-offline";
        mt5Text.textContent = "MT5: รอเชื่อมต่อ EA";
      }
    }

    const eqBadge = el("sumAccountEquityBadge");
    if (eqBadge) {
      const bal = Number(ea.equity || (currentState && currentState.config && currentState.config.account_balance) || 5000);
      eqBadge.textContent = `Equity: $${bal.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }

    const lastExec = ea.last_execution;
    if (lastExec && lastExec.action_id && lastExec.action_id !== lastReportedExecId) {
      lastReportedExecId = lastExec.action_id;
      const st = String(lastExec.status || "").toUpperCase();
      if (st === "FILLED" || st === "OPENED" || st === "PLACED") {
        showToast(
          `🟢 MT5 รับคำสั่งอัตโนมัติสำเร็จ! Ticket #${lastExec.ticket} (${lastExec.side} ${lastExec.lot} Lot @ ${fmtPrice(lastExec.fill_price)})`
        );
      } else if (st === "CLOSED" || st === "CLOSED_ALL" || st === "DEAL_CLOSED") {
        showToast(`🏁 MT5 ปิดออเดอร์เรียบร้อย (${fmtUsd(lastExec.profit)})`);
      }
    }
  }

  function renderActiveOrdersAndSummary(state, livePriceOverride) {
    const st = state.structure || {};
    const curPrice = Number(livePriceOverride || st.current_price || 0);
    if (curPrice > 0 && el("livePrice")) {
      el("livePrice").textContent = fmtPrice(curPrice);
    }

    const orders = buildUnifiedActiveOrders(state, curPrice);
    const totalCount = orders.length;
    const filledOrders = orders.filter((o) => o.status === "FILLED");
    const pendingOrders = orders.filter((o) => o.status === "PENDING");

    const cntInitial = orders.filter((o) => getRoleMeta(o.purpose).code === "INITIAL").length;
    const cntRecovery = orders.filter((o) => getRoleMeta(o.purpose).code === "RECOVERY").length;
    const cntScale = orders.filter((o) => getRoleMeta(o.purpose).code === "SCALE_IN").length;

    const totalLot = orders.reduce((acc, o) => acc + Number(o.lot_size || 0), 0);
    const totalPnlUsd = filledOrders.reduce((acc, o) => acc + Number(o.floating_pnl_usd || 0), 0);
    const totalPnlR = filledOrders.reduce((acc, o) => acc + Number(o.floating_pnl_r || 0), 0);
    const totalRiskUsd = orders.reduce((acc, o) => acc + Number(o.actual_risk_usd || 0), 0);
    const totalProfitTargetUsd = orders.reduce((acc, o) => acc + Number(o.potential_profit_usd || 0), 0);

    // Weighted average entry price across all orders
    const weightedEntrySum = orders.reduce((acc, o) => acc + Number(o.entry_mt5 || 0) * Number(o.lot_size || 0), 0);
    const avgEntry = totalLot > 0 ? weightedEntrySum / totalLot : 0;

    // Update Card 1: Count & Breakdown
    if (el("sumOrdersCount")) {
      if (totalCount === 0) {
        el("sumOrdersCount").textContent = "0 ไม้ (รอเข้าอัตโนมัติ)";
      } else {
        el("sumOrdersCount").textContent = `${totalCount} ไม้ (เปิด ${filledOrders.length} • รอดัก ${pendingOrders.length})`;
      }
    }
    if (el("cntInitial")) el("cntInitial").textContent = cntInitial;
    if (el("cntRecovery")) el("cntRecovery").textContent = cntRecovery;
    if (el("cntScale")) el("cntScale").textContent = cntScale;
    if (el("sumTotalLot")) {
      el("sumTotalLot").textContent = `ขนาดรวมทั้งหมด: ${totalLot.toFixed(2)} Lot`;
    }
    if (el("summaryStatusBadge")) {
      if (filledOrders.length > 0) {
        el("summaryStatusBadge").className = "status-tag tag-filled";
        el("summaryStatusBadge").textContent = `🚀 ถือครองใน MT5 (${filledOrders.length} ไม้)`;
      } else if (pendingOrders.length > 0) {
        el("summaryStatusBadge").className = "status-tag tag-pending";
        el("summaryStatusBadge").textContent = `⏳ ตั้งดัก LIMIT ใน MT5 (${pendingOrders.length} ไม้)`;
      } else {
        el("summaryStatusBadge").className = "status-tag tag-scanning";
        el("summaryStatusBadge").textContent = "🤖 AUTO SCANNING";
      }
    }

    // Update Card 2: Entry Price & Direction
    const primary = orders[0] || null;
    if (primary) {
      const side = primary.side;
      if (el("sumSideBadge")) {
        el("sumSideBadge").className = `side-tag ${side === "BUY" ? "side-buy" : "side-sell"}`;
        el("sumSideBadge").textContent = `${side} (${primary.execution_type})`;
      }
      if (el("sumEntryPrice")) {
        el("sumEntryPrice").textContent = `${side} @ ${fmtPrice(primary.entry_mt5)}`;
        el("sumEntryPrice").style.color = side === "BUY" ? "var(--bull)" : "var(--bear)";
      }
      if (el("sumAvgEntry")) {
        el("sumAvgEntry").textContent =
          totalCount > 1
            ? `ทุนเฉลี่ยรวม ${totalCount} ไม้: ${fmtPrice(avgEntry)} (${totalLot.toFixed(2)} Lot)`
            : `ราคาเข้าไม้หลัก: ${fmtPrice(primary.entry_mt5)} (${primary.lot_size.toFixed(2)} Lot)`;
      }
      if (el("sumEntryDistance")) {
        const dist = Math.abs(curPrice - primary.entry_mt5);
        if (primary.status === "FILLED") {
          el("sumEntryDistance").textContent = `ราคาปัจจุบัน ${fmtPrice(curPrice)} (ห่างจุดเข้า $${dist.toFixed(2)})`;
        } else {
          const dirWord = side === "SELL" ? "รอราคาเด้งขึ้นอีก" : "รอราคาย่อลงอีก";
          el("sumEntryDistance").textContent = `⏳ ตั้งดักรอใน MT5 • ${dirWord} $${dist.toFixed(2)} จะเกี่ยวเปิดไม้อัตโนมัติ`;
        }
      }
    } else {
      if (el("sumSideBadge")) {
        el("sumSideBadge").className = "side-tag side-neutral";
        el("sumSideBadge").textContent = "AUTO STANDBY";
      }
      if (el("sumEntryPrice")) {
        el("sumEntryPrice").textContent = "รอจังหวะเข้าออเดอร์";
        el("sumEntryPrice").style.color = "var(--text-primary)";
      }
      if (el("sumAvgEntry")) {
        el("sumAvgEntry").textContent = "ยังไม่มีออเดอร์เปิดค้างใน MT5";
      }
      if (el("sumEntryDistance")) {
        el("sumEntryDistance").textContent = "ระบบกำลังวิเคราะห์เบื้องหลังและจะส่งคำสั่งเข้า MT5 เองอัตโนมัติ";
      }
    }

    // Update Card 3: TP / SL
    if (primary) {
      const tp1Str = primary.tp1_mt5 > 0 ? `TP1: ${fmtPrice(primary.tp1_mt5)} • ` : "";
      const tp2Str = primary.target_mt5 > 0 ? `TP2: ${fmtPrice(primary.target_mt5)}` : "—";
      if (el("sumTpPrice")) el("sumTpPrice").textContent = `${tp1Str}${tp2Str}`;
      if (el("sumSlPrice")) el("sumSlPrice").textContent = fmtPrice(primary.stop_mt5);

      const slDist = Math.abs(primary.entry_mt5 - primary.stop_mt5);
      const tpDist = Math.abs(primary.target_mt5 - primary.entry_mt5);
      const rr = slDist > 0 ? (tpDist / slDist).toFixed(2) : primary.reward_risk.toFixed(2);
      const shieldStatus = primary.sl_moved_breakeven
        ? "🛡️ เลื่อน SL บังทุนแล้ว"
        : primary.trailing_step_index > 0
        ? `📈 Step Trailing ล็อคกำไรขั้นที่ ${primary.trailing_step_index}`
        : "🛡️ ตั้ง SL/TP คุมความเสี่ยงอัตโนมัติ";
      if (el("sumRiskControlBadge")) {
        el("sumRiskControlBadge").textContent = primary.sl_moved_breakeven ? "🛡️ BREAKEVEN LOCKED" : "🛡️ AUTO SL/TP";
      }
      if (el("sumTpSlMeta")) {
        el("sumTpSlMeta").textContent = `ระยะ TP $${tpDist.toFixed(2)} | ระยะ SL $${slDist.toFixed(2)} (R:R 1:${rr}) • ${shieldStatus}`;
      }
    } else {
      if (el("sumTpPrice")) el("sumTpPrice").textContent = "—";
      if (el("sumSlPrice")) el("sumSlPrice").textContent = "—";
      if (el("sumTpSlMeta")) {
        el("sumTpSlMeta").textContent = "ระบบตั้ง TP1, TP2, SL และเลื่อนบังทุนให้อัตโนมัติทันทีที่เข้าออเดอร์";
      }
    }

    // Update Card 4: Floating PnL
    if (el("sumTotalPnl")) {
      if (filledOrders.length > 0) {
        const signR = totalPnlR > 0 ? "+" : "";
        el("sumTotalPnl").textContent = `${fmtUsd(totalPnlUsd)} (${signR}${totalPnlR.toFixed(2)}R)`;
        el("sumTotalPnl").className =
          "summary-main-val mono " + (totalPnlUsd > 0 ? "pnl-pos" : totalPnlUsd < 0 ? "pnl-neg" : "");
      } else {
        el("sumTotalPnl").textContent = "$0.00 (0.00R)";
        el("sumTotalPnl").className = "summary-main-val mono";
      }
    }
    if (el("sumRiskBudget")) {
      el("sumRiskBudget").textContent =
        totalCount > 0
          ? `ความเสี่ยงรวมที่คุมไว้: $${totalRiskUsd.toFixed(2)} (${totalCount} ไม้)`
          : `ความเสี่ยงต่อไม้ที่ตั้งไว้: ${(state.config && state.config.risk_per_trade_pct) || 2.0}% ของพอร์ต`;
    }
    if (el("sumTargetProfit")) {
      el("sumTargetProfit").textContent =
        totalCount > 0 && totalProfitTargetUsd > 0
          ? `เป้าหมายกำไรรวม (ที่ TP2): +$${totalProfitTargetUsd.toFixed(2)}`
          : `ระบบแก้ไม้ (DCA) และทบกำไร (Scale-In) ทำงานอัตโนมัติ`;
    }

    // Render Section 2: Active Orders Cards
    const container = el("activeOrdersContainer");
    if (!container) return;

    if (orders.length === 0) {
      const tk = state.risk_ticket || {};
      const nextSide = tk.side || "BUY";
      const nextEntry = Number(tk.entry_mt5 || 0);
      const nextSl = Number(tk.stop_mt5 || 0);
      const nextTp1 = Number(tk.tp1_mt5 || 0);
      const nextTp2 = Number(tk.target_mt5 || 0);
      const nextLot = Number(tk.lot_size || 0.01);

      container.innerHTML = `
        <div class="empty-orders-card">
          <div class="empty-icon">🤖</div>
          <div class="empty-title">ขณะนี้ยังไม่มีออเดอร์เปิดอยู่ใน MT5 — ระบบกำลังสแกนและรอเข้าออเดอร์อัตโนมัติ</div>
          <p class="empty-desc">
            ระบบวิเคราะห์โครงสร้างราคา ความได้เปรียบทางสถิติ และบริหารความเสี่ยงอยู่ในเบื้องหลังทั้งหมด
            เมื่อเข้าเงื่อนไข ระบบจะยิงคำสั่งเข้า MT5 พร้อมตั้ง TP/SL และบริหารไม้ (ไม้หลัก • ไม้แก้ขาดทุน • ไม้ทบกำไร) ให้เองโดยที่คุณไม่ต้องกดปุ่มใดๆ
          </p>
          ${
            nextEntry > 0
              ? `
            <div class="next-auto-plan-pill">
              <span>🎯 แผนดักเข้าอัตโนมัติที่ระบบเล็งไว้ในเบื้องหลัง:</span>
              <strong class="${nextSide === "BUY" ? "text-bull" : "text-bear"}">${nextSide} (${tk.execution_type || "LIMIT"}) @ ${fmtPrice(nextEntry)}</strong>
              <span>• ขนาด <b>${nextLot.toFixed(2)} Lot</b></span>
              <span>• SL: <b>${fmtPrice(nextSl)}</b></span>
              <span>• TP1: <b>${fmtPrice(nextTp1)}</b> / TP2: <b>${fmtPrice(nextTp2)}</b></span>
            </div>
          `
              : ""
          }
        </div>
      `;
      return;
    }

    container.innerHTML = orders
      .map((o) => {
        const role = getRoleMeta(o.purpose);
        const isBuy = o.side === "BUY";
        const isFilled = o.status === "FILLED";
        const slDist = Math.abs(o.entry_mt5 - o.stop_mt5);
        const tpDist = o.target_mt5 > 0 ? Math.abs(o.target_mt5 - o.entry_mt5) : 0;
        const rr = slDist > 0 && tpDist > 0 ? (tpDist / slDist).toFixed(2) : Number(o.reward_risk || 0).toFixed(2);
        const pnlClass = o.floating_pnl_usd > 0 ? "pnl-pos" : o.floating_pnl_usd < 0 ? "pnl-neg" : "";
        const signR = o.floating_pnl_r > 0 ? "+" : "";
        const ticketLabel = o.broker_ticket ? `Ticket #${o.broker_ticket}` : "รอการเปิดใน MT5";

        const slNote = o.sl_moved_breakeven
          ? "🛡️ เลื่อนบังทุนแล้ว"
          : o.trailing_step_index > 0
          ? `📈 Trailing ขั้น ${o.trailing_step_index}`
          : `ห่างจุดเข้า $${slDist.toFixed(2)}`;

        const autoNote =
          o.trigger_reason
            ? `🤖 เหตุผลที่เปิดไม้นี้: ${o.trigger_reason}`
            : o.management_note_th || role.desc;

        const execStatusLabel = o.broker_ticket
          ? (isFilled ? "🚀 กำลังทำงานใน MT5 (FILLED)" : "⏳ ตั้งดักรอเกี่ยวใน MT5 (PENDING LIMIT)")
          : "⏳ แผนวิเคราะห์เบื้องหลัง (รอส่งคำสั่งเข้า MT5)";

        return `
          <div class="order-detail-card ${isBuy ? "card-buy" : "card-sell"} ${role.cssClass}">
            <div class="order-card-header">
              <div class="order-head-left">
                <span class="order-index-pill">ไม้ที่ #${o.order_index}</span>
                <span class="order-role-badge ${role.cssClass}">${role.badge}</span>
                <span class="order-ticket-tag mono">${ticketLabel}</span>
                <span class="order-time-tag">🕒 ${o.created_bkk || "-"}</span>
              </div>
              <div class="order-head-right">
                <span class="order-exec-status ${o.broker_ticket ? (isFilled ? "st-filled" : "st-pending") : "st-pending"}">
                  ${execStatusLabel}
                </span>
              </div>
            </div>

            <div class="order-metrics-grid">
              <div class="order-metric-box">
                <span class="om-label">ทิศทาง &amp; ขนาดไม้</span>
                <strong class="om-val ${isBuy ? "text-bull" : "text-bear"}">${o.side} ${o.lot_size.toFixed(2)} Lot</strong>
                <span class="om-sub">คำสั่ง ${o.execution_type} • เสี่ยง $${o.actual_risk_usd.toFixed(2)}</span>
              </div>

              <div class="order-metric-box">
                <span class="om-label">ราคาจุดเข้า (ENTRY PRICE)</span>
                <strong class="om-val mono">${fmtPrice(o.entry_mt5)}</strong>
                <span class="om-sub">${
                  isFilled
                    ? `เข้าที่ราคา ${fmtPrice(o.entry_mt5)}`
                    : `ห่างราคาปัจจุบัน $${Math.abs(curPrice - o.entry_mt5).toFixed(2)}`
                }</span>
              </div>

              <div class="order-metric-box box-sl">
                <span class="om-label">จุดตัดขาดทุน (STOP LOSS)</span>
                <strong class="om-val mono text-bear">${fmtPrice(o.stop_mt5)}</strong>
                <span class="om-sub">${slNote}</span>
              </div>

              <div class="order-metric-box box-tp">
                <span class="om-label">จุดทำกำไร (TP1 / TP2)</span>
                <strong class="om-val mono text-bull">
                  ${o.tp1_mt5 > 0 ? `${fmtPrice(o.tp1_mt5)} / ` : ""}${fmtPrice(o.target_mt5)}
                </strong>
                <span class="om-sub">ระยะ TP $${tpDist.toFixed(2)} • R:R 1:${rr}</span>
              </div>

              <div class="order-metric-box box-pnl">
                <span class="om-label">กำไร/ขาดทุนไม้นี้ (PnL)</span>
                <strong class="om-val mono ${pnlClass}">
                  ${isFilled ? `${fmtUsd(o.floating_pnl_usd)} (${signR}${o.floating_pnl_r.toFixed(2)}R)` : "รอเกี่ยวคำสั่ง"}
                </strong>
                <span class="om-sub">${
                  o.potential_profit_usd > 0 ? `เป้ากำไรไม้นี้ +$${o.potential_profit_usd.toFixed(2)}` : role.shortBadge
                }</span>
              </div>
            </div>

            <div class="order-card-footer">
              <span class="order-role-explain">${autoNote}</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function renderOrderHistory(historyList) {
    const container = el("orderHistoryContainer");
    const countBadge = el("historyCountBadge");
    if (!container) return;

    const clean = (historyList || [])
      .filter(
        (o) =>
          !String(o.order_id || "").startsWith("test_") &&
          o.close_reason !== "TEST_CLEANUP" &&
          o.close_reason !== "SYSTEM_INIT" &&
          Boolean(o.broker_ticket || o.broker_deal_id)
      )
      .slice()
      .reverse();

    if (countBadge) {
      countBadge.textContent = `${clean.length} รายการล่าสุด`;
    }

    if (clean.length === 0) {
      container.innerHTML = `<div class="empty-history">ยังไม่มีประวัติออเดอร์ที่ปิดแล้วในรอบนี้</div>`;
      return;
    }

    const rowsHtml = clean
      .map((o) => {
        const role = getRoleMeta(o.purpose);
        const isBuy = o.side === "BUY";
        const pnlUsd = Number(
          o.realized_pnl_usd !== undefined && o.realized_pnl_usd !== null
            ? o.realized_pnl_usd
            : o.floating_pnl_usd || 0
        );
        const pnlR = Number(
          o.realized_pnl_r !== undefined && o.realized_pnl_r !== null
            ? o.realized_pnl_r
            : o.floating_pnl_r || 0
        );
        const pnlClass = pnlUsd > 0 ? "pnl-pos" : pnlUsd < 0 ? "pnl-neg" : "";
        const signR = pnlR > 0 ? "+" : "";
        const statusStr = String(o.status || o.close_reason || "CLOSED");

        return `
          <tr>
            <td class="mono">${o.created_bkk || "-"}</td>
            <td><span class="order-role-badge ${role.cssClass}">${role.shortBadge}</span></td>
            <td><strong class="${isBuy ? "text-bull" : "text-bear"}">${o.side} ${Number(o.lot_size || 0.01).toFixed(2)} Lot</strong></td>
            <td class="mono">${fmtPrice(o.filled_price || o.entry_mt5)}</td>
            <td class="mono text-bear">${fmtPrice(o.stop_mt5)}</td>
            <td class="mono text-bull">${fmtPrice(o.target_mt5)}</td>
            <td class="mono">${o.close_price ? fmtPrice(o.close_price) : "—"}</td>
            <td class="mono ${pnlClass}"><strong>${fmtUsd(pnlUsd)} (${signR}${pnlR.toFixed(2)}R)</strong></td>
            <td><span class="hist-reason-tag">${statusStr}</span></td>
          </tr>
        `;
      })
      .join("");

    container.innerHTML = `
      <table class="history-table">
        <thead>
          <tr>
            <th>เวลาที่เข้า</th>
            <th>ประเภทไม้ (หน้าที่)</th>
            <th>ทิศทาง &amp; ขนาด</th>
            <th>ราคาจุดเข้า (Entry)</th>
            <th>จุดตัดขาดทุน (SL)</th>
            <th>จุดทำกำไร (TP)</th>
            <th>ราคาปิด</th>
            <th>กำไร/ขาดทุนสุทธิ</th>
            <th>สถานะจบไม้</th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml}
        </tbody>
      </table>
    `;
  }

  function populateSettingsForm(cfg) {
    if (!cfg) return;
    if (el("cfgBalance")) el("cfgBalance").value = cfg.account_balance ?? 5000;
    if (el("cfgLeverage")) {
      const curLev = cfg.leverage ?? 1000;
      el("cfgLeverage").value = curLev;
      document.querySelectorAll(".btn-lev-chip").forEach((chip) => {
        chip.classList.toggle("active", Number(chip.dataset.lev) === Number(curLev));
      });
    }
    if (el("cfgRiskPct")) el("cfgRiskPct").value = cfg.risk_per_trade_pct ?? 2.0;
    if (el("cfgSpread")) el("cfgSpread").value = cfg.broker_spread_points ?? 25;
    if (el("cfgTradingStyle")) el("cfgTradingStyle").value = cfg.trading_style || "micro_scalp";
  }

  function renderDashboard(state) {
    currentState = state;
    const st = state.structure || {};
    const quote = state.live_quote;

    // Render trading style / mode badge
    const modeBadge = el("tradingModeBadge");
    const style = String(state.trading_style || (state.config && state.config.trading_style) || "micro_scalp").toLowerCase();
    if (modeBadge) {
      if (style === "micro_scalp") {
        modeBadge.textContent = "⚡ MICRO-SCALP (เข้าเร็วออกเร็ว)";
        modeBadge.className = "meta-pill pill-mode-scalp";
      } else if (style === "scalping") {
        modeBadge.textContent = "⚡ FAST SCALP (M5)";
        modeBadge.className = "meta-pill pill-mode-scalp";
      } else {
        modeBadge.textContent = "🎯 INTRADAY SMC";
        modeBadge.className = "meta-pill pill-mode-intraday";
      }
    }

    if (el("livePrice")) {
      el("livePrice").textContent = fmtPrice(st.current_price);
    }
    if (quote && quote.bid && quote.ask && el("bidAskPill")) {
      el("bidAskPill").textContent = `Bid ${fmtPrice(quote.bid)} • Ask ${fmtPrice(quote.ask)}`;
    }

    updateEABridgeUI(state.ea_bridge || {});
    renderActiveOrdersAndSummary(state, st.current_price);
    renderOrderHistory(state.order_history || []);

    const modalEl = el("settingsModal");
    if (modalEl && modalEl.classList.contains("hidden")) {
      populateSettingsForm(state.config || {});
    }
  }

  function handleLiveTick(msg) {
    if (!msg.bar) return;
    if (msg.symbol && msg.symbol !== CURRENT_SYMBOL) return;
    const curP = Number(msg.bar.c || 0);
    if (curP > 0 && el("livePrice")) {
      el("livePrice").textContent = fmtPrice(curP);
    }
    if (msg.bid && msg.ask && el("bidAskPill")) {
      el("bidAskPill").textContent = `Bid ${fmtPrice(msg.bid)} • Ask ${fmtPrice(msg.ask)}`;
    }
    if (currentState) {
      if (msg.active_order !== undefined) {
        currentState.active_order = msg.active_order;
      }
      renderActiveOrdersAndSummary(currentState, curP);
    }
  }

  function connectWebSocket() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/ws?symbol=${encodeURIComponent(CURRENT_SYMBOL)}`;
    const badge = el("wsStatus");
    const badgeText = el("wsStatusText");

    ws = new WebSocket(wsUrl);
    ws.onopen = () => {
      if (badge) badge.classList.add("live");
      if (badgeText) badgeText.textContent = "Live Stream";
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: "subscribe", symbol: CURRENT_SYMBOL }));
      }
    };
    ws.onmessage = (ev) => {
      try {
        const doc = JSON.parse(ev.data);
        if (doc.type === "full_state") {
          renderDashboard(doc);
        } else if (doc.type === "tick") {
          handleLiveTick(doc);
        } else if (doc.type === "ea_heartbeat") {
          if (currentState) {
            currentState.ea_bridge = doc.ea_bridge || {};
            if (doc.active_order !== undefined) {
              currentState.active_order = doc.active_order;
            }
            if (doc.order_history !== undefined) {
              currentState.order_history = doc.order_history;
              renderOrderHistory(currentState.order_history);
            }
            updateEABridgeUI(currentState.ea_bridge);
            const cp = Number((currentState.structure && currentState.structure.current_price) || 0);
            renderActiveOrdersAndSummary(currentState, cp);
          }
        }
      } catch (_) {}
    };
    ws.onclose = () => {
      if (badge) badge.classList.remove("live");
      if (badgeText) badgeText.textContent = "Reconnecting...";
      setTimeout(connectWebSocket, 2000);
    };
  }

  function bindEvents() {
    const modal = el("settingsModal");
    const btnOpen = el("btnOpenSettings");
    const btnClose = el("btnCloseSettings");
    const btnCancel = el("btnCancelSettings");

    if (btnOpen && modal) {
      btnOpen.addEventListener("click", () => {
        populateSettingsForm(currentState ? currentState.config : null);
        modal.classList.remove("hidden");
      });
    }
    if (btnClose && modal) btnClose.addEventListener("click", () => modal.classList.add("hidden"));
    if (btnCancel && modal) btnCancel.addEventListener("click", () => modal.classList.add("hidden"));
    if (modal) {
      modal.addEventListener("click", (e) => {
        if (e.target === modal) modal.classList.add("hidden");
      });
    }

    document.querySelectorAll(".btn-lev-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const lev = Number(chip.dataset.lev);
        if (el("cfgLeverage")) el("cfgLeverage").value = lev;
        document.querySelectorAll(".btn-lev-chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
      });
    });

    const modeBtn = el("tradingModeBadge");
    if (modeBtn) {
      modeBtn.addEventListener("click", async () => {
        const curStyle = String(currentState?.trading_style || "micro_scalp").toLowerCase();
        const nextStyle = curStyle === "micro_scalp" ? "intraday" : "micro_scalp";
        try {
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ action: "change_style", trading_style: nextStyle, symbol: CURRENT_SYMBOL }));
          } else {
            await fetch("/api/settings", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ symbol: CURRENT_SYMBOL, trading_style: nextStyle }),
            });
          }
          showToast(`⚡ สลับโหมดการเทรดเป็น: ${nextStyle === "micro_scalp" ? "สเกลป์ความเร็วสูง (Micro-Scalp)" : "เทรดรอบปกติ (Intraday SMC)"}`);
        } catch (_) {
          showToast("❌ สลับโหมดไม่สำเร็จ");
        }
      });
    }

    const form = el("settingsForm");
    if (form) {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const payload = {
          symbol: CURRENT_SYMBOL,
          trading_style: el("cfgTradingStyle")?.value || "micro_scalp",
          account_balance: Number(el("cfgBalance")?.value || 5000),
          leverage: Number(el("cfgLeverage")?.value || 1000),
          risk_per_trade_pct: Number(el("cfgRiskPct")?.value || 2.0),
          broker_spread_points: Number(el("cfgSpread")?.value || 25),
          multi_order: { auto_mode_enabled: true },
        };
        const keyVal = (el("cfgGeminiKey")?.value || "").trim();
        if (keyVal) payload.gemini_api_key = keyVal;

        try {
          const resp = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          if (resp.ok) {
            const newState = await resp.json();
            renderDashboard(newState);
            if (modal) modal.classList.add("hidden");
            showToast("💾 บันทึกการตั้งค่าระบบอัตโนมัติเรียบร้อย");
          }
        } catch (_) {
          showToast("❌ ไม่สามารถบันทึกการตั้งค่าได้");
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    fetch(`/api/state?symbol=${encodeURIComponent(CURRENT_SYMBOL)}`)
      .then((r) => r.json())
      .then((doc) => renderDashboard(doc))
      .catch(() => {});
    connectWebSocket();
  });
})();
