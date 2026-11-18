(() => {
  let ws = null;
  let currentState = null;
  let lastReportedExecId = null;
  const CURRENT_SYMBOL =
    window.CURRENT_SYMBOL || (window.location.pathname.includes("btc") ? "BTCUSD" : "XAUUSD");

  const el = (id) => document.getElementById(id);

  function fmt(val, digits = 2) {
    const n = Number(val);
    if (!Number.isFinite(n)) return (0).toFixed(digits);
    return n.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

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

  let actionLoadingTimer = null;
  function showActionLoading(title, subtitle, icon = "⚡") {
    if (actionLoadingTimer) {
      clearTimeout(actionLoadingTimer);
      actionLoadingTimer = null;
    }
    const modal = el("actionLoadingModal");
    if (!modal) return;
    if (el("loadingTitle")) el("loadingTitle").textContent = title || "กำลังประมวลผล...";
    if (el("loadingSubtitle")) el("loadingSubtitle").textContent = subtitle || "กรุณารอสักครู่ ระบบกำลังปรับปรุงและซิงค์ข้อมูลกับ MT5";
    if (el("loadingIcon")) el("loadingIcon").textContent = icon || "⚡";
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
  }

  function hideActionLoading(minDelayMs = 380) {
    const modal = el("actionLoadingModal");
    if (!modal) return;
    if (actionLoadingTimer) clearTimeout(actionLoadingTimer);
    if (minDelayMs > 0) {
      actionLoadingTimer = setTimeout(() => {
        modal.classList.add("hidden");
        modal.setAttribute("aria-hidden", "true");
        actionLoadingTimer = null;
      }, minDelayMs);
    } else {
      modal.classList.add("hidden");
      modal.setAttribute("aria-hidden", "true");
    }
  }

  function getRoleMeta(purposeRaw, strategyMode) {
    const p = String(purposeRaw || "INITIAL").toUpperCase();
    const sm = String(strategyMode || "").toUpperCase();
    if (p === "PROMOTED_RUNNER" || sm === "PROMOTED_RUNNER" || p.includes("RUNNER")) {
      return {
        code: "PROMOTED_RUNNER",
        badge: "👑 ไม้รันเทรนด์ (Promoted Runner)",
        shortBadge: "👑 ไม้รันเทรนด์",
        cssClass: "role-runner",
        desc: "เลื่อนขั้นจากไม้สเกลป์เป็นไม้รันเทรนด์ถือยาวตามโครงสร้าง BOS พร้อมขยายเป้าหมายกำไรสูงสุด",
      };
    }
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
    let stratLabel = "🎯 ไม้ทำกำไรหลัก";
    if (sm === "RAPID_FIRE") stratLabel = "⚡ Rapid Harvester";
    else if (sm === "SMC_SWING") stratLabel = "🏛️ SMC Swing";
    return {
      code: "INITIAL",
      badge: `${stratLabel} (Initial Entry)`,
      shortBadge: stratLabel,
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
          strategy_mode: o.strategy_mode || "SMC_SWING",
          strategy_badge: o.strategy_badge || "",
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
      let stratM = "SMC_SWING";
      let stratBadge = "🏛️ SMC Swing";
      if (comment.includes("RAP")) {
        stratM = "RAPID_FIRE";
        stratBadge = "⚡ Rapid Harvester";
      } else if (comment.includes("RUN") || comment.includes("PROMOTED")) {
        stratM = "PROMOTED_RUNNER";
        stratBadge = "👑 Promoted Runner";
        purp = "PROMOTED_RUNNER";
      }
      if (comment.includes("RECOVERY") || comment.includes("DCA")) purp = "RECOVERY";
      else if (comment.includes("SCALE")) purp = "SCALE_IN";

      unified.push({
        order_index: idx,
        order_id: p.comment || `MT5-${tStr}`,
        broker_ticket: p.ticket,
        symbol: sym,
        side,
        purpose: purp,
        strategy_mode: stratM,
        strategy_badge: stratBadge,
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
    const cfgBal = Number((currentState && currentState.config && currentState.config.account_balance) || 0);
    const balanceVal = Number(ea.balance ?? ea.equity ?? cfgBal);
    const equityVal = Number(ea.equity ?? balanceVal);
    const freeMarginVal = Number(ea.free_margin ?? equityVal);
    const fmtMoney = (v) => `$${Number(v || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

    // Legacy topbar pill fallback if present
    const mt5Pill = el("mt5StatusPill");
    const mt5Text = el("mt5StatusText");
    if (mt5Pill && mt5Text) {
      if (ea.is_connected && ea.account_id) {
        mt5Pill.className = "meta-pill pill-mt5-online";
        mt5Text.textContent = `MT5: #${ea.account_id} (${ea.broker || "Connected"}) • ${fmtMoney(equityVal)}`;
      } else if (ea.is_connected) {
        mt5Pill.className = "meta-pill pill-mt5-online";
        mt5Text.textContent = "MT5: Connected (Auto Polling)";
      } else {
        mt5Pill.className = "meta-pill pill-mt5-offline";
        mt5Text.textContent = "MT5: รอเชื่อมต่อ EA";
      }
    }

    // New Hub Card 1: Balance & MT5 Connection Status
    const hubConnBadge = el("hubConnBadge");
    const hubConnText = el("hubConnText");
    if (hubConnBadge && hubConnText) {
      if (ea.is_connected) {
        hubConnBadge.className = "conn-pill conn-online";
        hubConnText.textContent = ea.account_id ? `ONLINE • #${ea.account_id}` : "ONLINE";
      } else {
        hubConnBadge.className = "conn-pill conn-offline";
        hubConnText.textContent = "รอเชื่อมต่อ EA";
      }
    }

    if (el("hubBalanceVal")) {
      el("hubBalanceVal").textContent = fmtMoney(balanceVal);
    }
    if (el("hubEquityVal")) {
      el("hubEquityVal").textContent = fmtMoney(equityVal);
      if (equityVal > balanceVal + 0.005) {
        el("hubEquityVal").style.color = "var(--bull)";
      } else if (equityVal < balanceVal - 0.005) {
        el("hubEquityVal").style.color = "var(--bear)";
      } else {
        el("hubEquityVal").style.color = "#e2e8f0";
      }
    }
    if (el("hubFreeMarginVal")) {
      el("hubFreeMarginVal").textContent = fmtMoney(freeMarginVal);
    }
    if (el("hubBrokerInfo")) {
      if (ea.is_connected && (ea.broker || ea.account_id)) {
        const srv = ea.server ? ` (${ea.server})` : "";
        el("hubBrokerInfo").textContent = `${ea.broker || "MT5 Broker"}${srv}`;
      } else {
        el("hubBrokerInfo").textContent = "รอการเชื่อมต่อจาก MetaTrader 5 EA Bridge...";
      }
    }
    if (el("hubSpreadOffset")) {
      const spPts = ea.spread_points != null ? Math.round(Number(ea.spread_points)) : null;
      const spUsd = ea.spread_usd != null ? Number(ea.spread_usd).toFixed(2) : null;
      const offUsd = ea.dynamic_offset_usd != null ? `${Number(ea.dynamic_offset_usd) >= 0 ? "+" : ""}${Number(ea.dynamic_offset_usd).toFixed(2)}` : "0.00";
      const ver = ea.ea_version ? `v${ea.ea_version}` : "—";
      const hb = ea.last_heartbeat_ago_sec != null ? ` • ${Number(ea.last_heartbeat_ago_sec).toFixed(1)}s` : "";
      if (ea.is_connected && spPts != null) {
        el("hubSpreadOffset").textContent = `Spread: ${spPts}pts ($${spUsd}) • Offset: ${offUsd} • EA ${ver}${hb}`;
      } else {
        el("hubSpreadOffset").textContent = `Spread: — • Offset: ${offUsd} • EA ${ver}`;
      }
    }

    const eqBadge = el("sumAccountEquityBadge");
    if (eqBadge) {
      eqBadge.textContent = `Equity: ${fmtMoney(equityVal || 5000)}`;
    }

    const hubFloatingQuick = el("hubFloatingQuick");
    if (hubFloatingQuick && ea.total_floating_pnl != null) {
      const flt = Number(ea.total_floating_pnl || 0);
      hubFloatingQuick.textContent = `Floating: ${fmtUsd(flt)}`;
      hubFloatingQuick.style.color = flt > 0.005 ? "var(--bull)" : flt < -0.005 ? "var(--bear)" : "var(--text-secondary)";
    }

    const lastExec = ea.last_execution;
    if (lastExec && lastExec.action_id && lastExec.action_id !== lastReportedExecId) {
      lastReportedExecId = lastExec.action_id;
      const actId = String(lastExec.action_id || "");
      const isHistorySync = Boolean(
        lastExec.is_history_sync
        || actId.startsWith("HIST-")
        || actId.startsWith("SYNC-")
      );
      const recTs = Number(lastExec.received_at || 0);
      const isFreshLiveEvent = recTs > 0 && (Date.now() / 1000 - recTs) <= 6;

      // Only show live toast notifications for fresh, genuine open/close actions, NEVER for historical syncs!
      if (!isHistorySync && isFreshLiveEvent) {
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
        const role = getRoleMeta(o.purpose, o.strategy_mode);
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

        const regimeBadge = o.is_sideway
          ? `<span class="order-regime-badge badge-sideway" title="${o.regime_label_th || 'ตลาดไซด์เวย์/พักตัว'}">🦀 ไซด์เวย์ (สเกลป์สั้น)</span>`
          : (o.strategy_mode === "RAPID_FIRE"
              ? `<span class="order-regime-badge badge-trend" title="${o.regime_label_th || 'ตลาดมีเทรนด์ชัดเจน'}">🚀 มีเทรนด์ (รันรอบ)</span>`
              : "");

        return `
          <div class="order-detail-card ${isBuy ? "card-buy" : "card-sell"} ${role.cssClass}">
            <div class="order-card-header">
              <div class="order-head-left">
                <span class="order-index-pill">ไม้ที่ #${o.order_index}</span>
                <span class="order-strat-pill">${o.strategy_badge || (o.strategy_mode === "RAPID_FIRE" ? "⚡ Rapid Fire" : (o.strategy_mode === "PROMOTED_RUNNER" ? "👑 Promoted Runner" : "🏛️ SMC Swing"))}</span>
                ${regimeBadge}
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

  let rawOrderHistory = [];
  const historyFilterState = {
    dateRange: "all",
    customDate: "",
    category: "all",
    outcome: "all",
  };

  function toDateStr(d) {
    if (!d || isNaN(d.getTime())) return "";
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function getOrderDate(o) {
    const ts = o.closed_ts || o.created_ts;
    if (ts) {
      return new Date(ts > 1e11 ? ts : ts * 1000);
    }
    if (o.created_bkk) {
      const m = String(o.created_bkk).match(/(\d{1,2})\/(\d{1,2})/);
      if (m) {
        const now = new Date();
        return new Date(now.getFullYear(), Number(m[2]) - 1, Number(m[1]));
      }
      const d = new Date(String(o.created_bkk).replace(" ", "T"));
      if (!isNaN(d.getTime())) return d;
    }
    return null;
  }

  function matchesCategory(o, category) {
    if (!category || category === "all") return true;
    const role = getRoleMeta(o.purpose, o.strategy_mode);
    const purpose = String(o.purpose || "").toUpperCase();
    const strat = String(o.strategy_mode || "").toUpperCase();
    const badge = String(o.strategy_badge || "").toUpperCase();

    if (category === "INITIAL") {
      return role.code === "INITIAL" || purpose === "INITIAL";
    }
    if (category === "RECOVERY") {
      return role.code === "RECOVERY" || purpose === "RECOVERY" || purpose.includes("DCA");
    }
    if (category === "SCALE_IN") {
      return role.code === "SCALE_IN" || purpose === "SCALE_IN" || purpose.includes("SCALE");
    }
    if (category === "PROMOTED_RUNNER") {
      return role.code === "PROMOTED_RUNNER" || strat === "PROMOTED_RUNNER" || purpose.includes("RUNNER");
    }
    if (category === "RAPID_FIRE") {
      return strat === "RAPID_FIRE" || badge.includes("RAPID") || purpose.includes("RAPID");
    }
    if (category === "SMC_SWING") {
      return strat === "SMC_SWING" || badge.includes("SMC") || purpose.includes("SWING");
    }
    return true;
  }

  function matchesOutcome(pnlUsd, statusStr, outcome) {
    if (!outcome || outcome === "all") return true;
    if (outcome === "profit") {
      return pnlUsd > 0.001;
    }
    if (outcome === "loss") {
      return pnlUsd < -0.001;
    }
    if (outcome === "breakeven") {
      return Math.abs(pnlUsd) <= 0.001 || String(statusStr || "").includes("BREAKEVEN");
    }
    return true;
  }

  function applyAndRenderOrderHistory() {
    const container = el("orderHistoryContainer");
    const countBadge = el("historyCountBadge");
    if (!container) return;

    const clean = (rawOrderHistory || [])
      .filter((o) => {
        if (!o) return false;
        const oid = String(o.order_id || "");
        if (oid.startsWith("test_")) return false;
        if (o.close_reason === "TEST_CLEANUP" || o.close_reason === "SYSTEM_INIT") return false;
        // Strictly show only REAL MT5 executed trades (must have broker_ticket or broker_deal_id)
        if (!o.broker_ticket && !o.broker_deal_id) return false;
        return true;
      })
      .slice()
      .reverse();

    if (clean.length === 0) {
      if (countBadge) countBadge.textContent = "0 รายการ";
      if (el("fsCount")) el("fsCount").textContent = "0";
      if (el("fsPnl")) el("fsPnl").innerHTML = "$0.00 (+0.00R)";
      if (el("fsWinRate")) el("fsWinRate").textContent = "0%";
      container.innerHTML = `<div class="empty-history">ยังไม่มีประวัติออเดอร์ที่ปิดแล้วในรอบนี้</div>`;
      return;
    }

    const now = new Date();
    const todayStr = toDateStr(now);
    const yestDate = new Date();
    yestDate.setDate(yestDate.getDate() - 1);
    const yesterdayStr = toDateStr(yestDate);

    const filtered = clean.filter((o) => {
      // Date filter
      if (historyFilterState.dateRange !== "all") {
        const orderDate = getOrderDate(o);
        if (orderDate) {
          const ordStr = toDateStr(orderDate);
          if (historyFilterState.dateRange === "today") {
            if (ordStr !== todayStr) return false;
          } else if (historyFilterState.dateRange === "yesterday") {
            if (ordStr !== yesterdayStr) return false;
          } else if (historyFilterState.dateRange === "7d") {
            const diffMs = now.getTime() - orderDate.getTime();
            if (diffMs < -300000 || diffMs > 7 * 86400 * 1000) return false;
          } else if (historyFilterState.dateRange === "custom") {
            if (historyFilterState.customDate && ordStr !== historyFilterState.customDate) return false;
          }
        }
      }

      // Category filter
      if (!matchesCategory(o, historyFilterState.category)) {
        return false;
      }

      // Outcome filter
      const pnlUsd = Number(
        o.realized_pnl_usd !== undefined && o.realized_pnl_usd !== null
          ? o.realized_pnl_usd
          : o.floating_pnl_usd || 0
      );
      const statusStr = String(o.status || o.close_reason || "CLOSED");
      if (!matchesOutcome(pnlUsd, statusStr, historyFilterState.outcome)) {
        return false;
      }

      return true;
    });

    // Compute dynamic filter stats summary
    let sumPnlUsd = 0;
    let sumPnlR = 0;
    let winCount = 0;
    let lossCount = 0;

    filtered.forEach((o) => {
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
      sumPnlUsd += pnlUsd;
      sumPnlR += pnlR;
      if (pnlUsd > 0.001) winCount++;
      else if (pnlUsd < -0.001) lossCount++;
    });

    const decisiveTrades = winCount + lossCount;
    const winRate = decisiveTrades > 0 ? (winCount / decisiveTrades) * 100 : (filtered.length > 0 && winCount > 0 ? 100 : 0);

    if (countBadge) {
      countBadge.textContent =
        filtered.length === clean.length ? `${clean.length} รายการ` : `${filtered.length} / ${clean.length} รายการ`;
    }

    if (el("fsCount")) el("fsCount").textContent = String(filtered.length);
    if (el("fsPnl")) {
      const pnlClass = sumPnlUsd > 0 ? "pnl-pos" : sumPnlUsd < 0 ? "pnl-neg" : "";
      const signR = sumPnlR > 0 ? "+" : "";
      const signUsd = sumPnlUsd > 0 ? "+" : "";
      el("fsPnl").innerHTML = `<span class="${pnlClass}">${signUsd}${fmtUsd(sumPnlUsd)} (${signR}${sumPnlR.toFixed(2)}R)</span>`;
    }
    if (el("fsWinRate")) {
      const wrClass = winRate >= 50 ? "text-bull" : decisiveTrades > 0 ? "text-bear" : "";
      el("fsWinRate").innerHTML = `<span class="${wrClass}">${winRate.toFixed(1)}% (${winCount}W / ${lossCount}L)</span>`;
    }

    if (filtered.length === 0) {
      container.innerHTML = `
        <div class="empty-history" style="padding: 28px 14px; text-align: center;">
          <span style="font-size: 24px; display: block; margin-bottom: 8px;">🔍</span>
          <strong style="color: var(--text-primary); font-size: 13.5px;">ไม่พบรายการออเดอร์ที่ตรงกับเงื่อนไขตัวกรอง</strong>
          <p style="color: var(--text-secondary); font-size: 12px; margin-top: 4px;">ลองปรับช่วงวันที่ หมวดหมู่ หรือกดปุ่ม "ล้างตัวกรอง" เพื่อดูรายการทั้งหมด</p>
        </div>
      `;
      return;
    }

    const rowsHtml = filtered
      .map((o) => {
        const role = getRoleMeta(o.purpose, o.strategy_mode);
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
        const signUsd = pnlUsd > 0 ? "+" : "";
        const statusStr = String(o.status || o.close_reason || "CLOSED");

        const ticketStr = o.broker_ticket
          ? `#${o.broker_ticket}`
          : (o.broker_deal_id
            ? `Deal #${o.broker_deal_id}`
            : (o.order_id ? o.order_id.split("-").slice(-2).join("-") : "K79"));

        return `
          <tr>
            <td class="mono">${o.created_bkk || "-"}</td>
            <td><span class="mono" style="font-size: 11.5px; font-weight: 600; color: var(--accent);">${ticketStr}</span></td>
            <td><span class="order-role-badge ${role.cssClass}">${role.shortBadge}</span></td>
            <td><strong class="${isBuy ? "text-bull" : "text-bear"}">${o.side} ${Number(o.lot_size || 0.01).toFixed(2)} Lot</strong></td>
            <td class="mono">${fmtPrice(o.filled_price || o.entry_mt5)}</td>
            <td class="mono text-bear">${fmtPrice(o.stop_mt5)}</td>
            <td class="mono text-bull">${fmtPrice(o.target_mt5)}</td>
            <td class="mono">${o.close_price ? fmtPrice(o.close_price) : "—"}</td>
            <td class="mono ${pnlClass}"><strong>${signUsd}${fmtUsd(pnlUsd)} (${signR}${pnlR.toFixed(2)}R)</strong></td>
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
            <th>Ticket / ไม้</th>
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

  function renderOrderHistory(historyList) {
    rawOrderHistory = historyList || [];
    applyAndRenderOrderHistory();
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function renderReanalysisReport(report) {
    const container = el("reanalysisReportContent");
    const tsBadge = el("reanalysisTimestamp");
    if (!container) return;
    if (!report) {
      container.innerHTML = `<p class="empty-hint">ยังไม่มีการรันวิเคราะห์เชิงลึก — ระบบจะทำงานอัตโนมัติเมื่อเกิด Drawdown สะสม หรือกดปุ่มรันวิเคราะห์ด้านบน</p>`;
      if (tsBadge) tsBadge.textContent = "อัปเดตล่าสุด: -";
      return;
    }
    if (tsBadge && report.timestamp) {
      tsBadge.textContent = `อัปเดตล่าสุด: ${new Date(report.timestamp * 1000).toLocaleTimeString("th-TH")}`;
    }
    const dirsHtml = (report.strategic_directives || [])
      .map((d) => `<li class="directive-item">📌 ${escapeHtml(d)}</li>`)
      .join("");
    const ctx = report.market_context || {};
    container.innerHTML = `
      <div class="report-box">
        <div class="report-summary-text">${escapeHtml(report.diagnostic_summary_th || "การวิเคราะห์โครงสร้างความเสี่ยงเสร็จสิ้น")}</div>
        <div class="report-meta-grid">
          <div class="report-meta-item"><span>โครงสร้างแนวโน้ม:</span> <strong>${escapeHtml(ctx.trend || "—")} (${escapeHtml(ctx.structure_state || "—")})</strong></div>
          <div class="report-meta-item"><span>สหสัมพันธ์ดอลลาร์ (DXY):</span> <strong>${escapeHtml(ctx.dxy_trend || "—")}</strong></div>
          <div class="report-meta-item"><span>บอนด์ยิลด์ (US10Y):</span> <strong>${escapeHtml(ctx.us10y_trend || "—")}</strong></div>
          <div class="report-meta-item"><span>รูปแบบที่แพ้สะสมสูง:</span> <strong class="text-danger">${report.high_risk_clusters ? report.high_risk_clusters.length : 0} คลัสเตอร์</strong></div>
        </div>
        <div class="report-directives">
          <h5>🧭 คำสั่งยุทธศาสตร์เพื่อป้องกันความเสียหาย (Strategic Directives):</h5>
          <ul class="directives-list">${dirsHtml || "<li>เฝ้าระวังและเทรดเฉพาะ Setup ที่มีความคมชัดสูง (Confluence >= 60)</li>"}</ul>
        </div>
      </div>
    `;
  }

  function renderLossPatterns(patterns) {
    const container = el("lossPatternsContainer");
    if (!container) return;
    if (!patterns || patterns.length === 0) {
      container.innerHTML = `<p class="empty-hint">ยังไม่มีรูปแบบที่แพ้สะสม — ระบบกำลังเรียนรู้และเฝ้าระวังแบบเรียลไทม์</p>`;
      return;
    }
    const html = patterns.map((p) => {
      const isHighRisk = (p.failure_count || 0) >= 2;
      return `
        <div class="pattern-item ${isHighRisk ? "pattern-high-risk" : "pattern-moderate"}">
          <div class="pattern-header">
            <span class="pattern-badge ${p.side === 'BUY' ? 'badge-buy' : 'badge-sell'}">${escapeHtml(p.side || '')}</span>
            <span class="pattern-signature font-mono">${escapeHtml(p.signature || 'GENERAL')}</span>
            <span class="pattern-count">${p.failure_count || 1} ครั้ง (-$${fmt(p.total_loss_usd || 0, 2)})</span>
          </div>
          <div class="pattern-lesson">💡 ${escapeHtml(p.lesson_learned || p.signature || 'ระวังสภาวะตลาดไม่สอดคล้อง')}</div>
          ${isHighRisk ? '<span class="pattern-veto-tag">⛔ ระบบจะกรองและปฏิเสธ Setup รูปแบบนี้อัตโนมัติ</span>' : ''}
        </div>
      `;
    }).join("");
    container.innerHTML = html;
  }

  function renderDailyProfitAndLossCircuit(state) {
    const dpt = state.daily_profit_tracking || {};
    const cb = state.circuit_breaker_20pct || {};
    const lm = state.loss_memory || {};
    const cfg = state.config || {};
    const ea = state.ea_bridge || {};

    const targetUsd = dpt.daily_profit_target_usd != null
      ? Number(dpt.daily_profit_target_usd)
      : (cfg.daily_profit_target_usd != null ? Number(cfg.daily_profit_target_usd) : 10.0);
    const realizedUsd = Number(dpt.daily_realized_pnl_usd || 0.0);
    const equityPnlUsd = dpt.session_equity_pnl_usd != null ? Number(dpt.session_equity_pnl_usd) : realizedUsd;
    const displayPnlUsd = Math.abs(equityPnlUsd) > 0.005 || Math.abs(realizedUsd) < 0.005 ? equityPnlUsd : realizedUsd;
    const isTargetEnabled = Boolean(dpt.daily_profit_target_enabled ?? cfg.daily_profit_target_enabled ?? true);
    const isReached = Boolean(dpt.daily_profit_target_reached);
    const anchorBal = Number(dpt.anchor_balance ?? cb.anchor_balance ?? cfg.pnl_anchor_balance ?? ea.balance ?? cfg.account_balance ?? 0);
    const anchorBkk = String(dpt.anchor_bkk || cb.anchor_bkk || cfg.pnl_anchor_bkk || "");
    const targetGoalBal = Number(dpt.target_balance_goal ?? (anchorBal + targetUsd));
    const effectiveGain = Math.max(realizedUsd, equityPnlUsd);
    const progressPct = targetUsd > 0 ? Math.min(100, Math.max(0, (effectiveGain / targetUsd) * 100)) : 0;

    // 1. Legacy Daily Profit Pill fallback
    const profitPill = el("dailyProfitPill");
    if (profitPill) {
      if (isReached) {
        profitPill.className = "meta-pill pill-profit-reached";
        profitPill.textContent = `🎯 กำไรวันนี้: $${fmt(realizedUsd, 2)} / $${fmt(targetUsd, 2)} (ถึงเป้า • พักพอร์ต)`;
      } else {
        profitPill.className = "meta-pill pill-profit-tracking";
        profitPill.textContent = `🎯 กำไรวันนี้: $${fmt(realizedUsd, 2)} / $${fmt(targetUsd, 2)}`;
      }
    }

    // 2. Circuit Breaker / Anchored Loss Limit Status
    const cbPill = el("circuitBreakerPill");
    const cbAlertBox = el("circuitBreakerAlertBox");
    const cbStatusText = el("circuitBreakerStatusText");
    const btnResetCB = el("btnResetCircuitBreaker");
    const isTripped = Boolean(cb.tripped);

    if (cbPill) {
      if (isTripped) cbPill.classList.remove("hidden");
      else cbPill.classList.add("hidden");
    }
    if (btnResetCB) {
      if (isTripped) btnResetCB.classList.remove("hidden");
      else btnResetCB.classList.add("hidden");
    }

    const consecLoss = Number(cb.consecutive_loss_usd || 0.0);
    const consecLossPct = Number(cb.consecutive_loss_pct || 0.0);
    const thresholdPct = Number(cb.threshold_pct ?? cfg.max_consecutive_loss_pct ?? 20.0);
    const maxLossUsd = Number(cb.max_loss_usd ?? cfg.max_loss_usd ?? (anchorBal * thresholdPct / 100.0));
    const stopFloorBal = Number(cb.stop_floor_balance ?? Math.max(0, anchorBal - maxLossUsd));
    const anchorDdUsd = Number(cb.anchor_drawdown_usd ?? consecLoss);
    const anchorDdPct = Number(cb.anchor_drawdown_pct ?? consecLossPct);
    const consecCount = Number(cb.consecutive_loss_count || 0);
    const pctBar = maxLossUsd > 0
      ? Math.min(100, Math.max(0, (Math.max(anchorDdUsd, consecLoss) / maxLossUsd) * 100))
      : (thresholdPct > 0 ? Math.min(100, Math.max(0, (consecLossPct / thresholdPct) * 100)) : 0);

    // New Hub Card 2: Daily PnL & Portfolio Targets (Anchored to Balance at Setup)
    const hubDailyPnlVal = el("hubDailyPnlVal");
    if (hubDailyPnlVal) {
      hubDailyPnlVal.textContent = fmtUsd(displayPnlUsd);
      hubDailyPnlVal.className = `hub-big-val mono ${displayPnlUsd > 0.005 ? "val-profit" : displayPnlUsd < -0.005 ? "val-loss" : ""}`;
    }

    const hubAnchorMeta = el("hubAnchorMeta");
    if (hubAnchorMeta) {
      if (anchorBal > 0) {
        const tsStr = anchorBkk ? ` (${anchorBkk})` : "";
        hubAnchorMeta.textContent = `เริ่มนับจาก Balance: $${fmt(anchorBal, 2)}${tsStr}`;
      } else {
        hubAnchorMeta.textContent = "เริ่มนับจาก Balance: รอซิงค์ MT5";
      }
    }

    const hubTargetBadge = el("hubDailyTargetBadge");
    if (hubTargetBadge) {
      if (isTripped) {
        hubTargetBadge.className = "target-state-badge state-tripped";
        hubTargetBadge.textContent = "🚨 ตัดวงจรขาดทุน";
      } else if (isReached && isTargetEnabled) {
        hubTargetBadge.className = "target-state-badge state-reached";
        hubTargetBadge.textContent = "🏆 ถึงเป้าแล้ว • ล็อกพอร์ต";
      } else if (!isTargetEnabled || targetUsd <= 0) {
        hubTargetBadge.className = "target-state-badge state-unlocked";
        hubTargetBadge.textContent = "🔓 ไม่ล็อกเป้าอัตโนมัติ";
      } else {
        hubTargetBadge.className = "target-state-badge state-tracking";
        hubTargetBadge.textContent = "🎯 กำลังสะสมเป้า";
      }
    }

    if (el("hubProfitTargetText")) {
      if (targetUsd > 0) {
        el("hubProfitTargetText").textContent = `${fmtUsd(effectiveGain)} / +$${fmt(targetUsd, 2)} (${progressPct.toFixed(0)}% • เป้า $${fmt(targetGoalBal, 2)})`;
      } else {
        el("hubProfitTargetText").textContent = "ปิดการตั้งเป้า ($0.00)";
      }
    }
    if (el("hubProfitBarFill")) {
      el("hubProfitBarFill").style.width = `${progressPct}%`;
    }

    if (el("hubLossLimitText")) {
      const dispDdUsd = Math.max(anchorDdUsd, consecLoss);
      const dispDdPct = Math.max(anchorDdPct, consecLossPct);
      el("hubLossLimitText").textContent = `-$${fmt(dispDdUsd, 2)} / -$${fmt(maxLossUsd, 2)} (${fmt(dispDdPct, 1)}%/${fmt(thresholdPct, 1)}% • Floor $${fmt(stopFloorBal, 2)})`;
    }
    const hubLossBar = el("hubLossBarFill");
    if (hubLossBar) {
      hubLossBar.style.width = `${pctBar}%`;
      if (isTripped || pctBar >= 80) {
        hubLossBar.className = "hub-bar-fill fill-danger";
      } else if (pctBar >= 50) {
        hubLossBar.className = "hub-bar-fill fill-warn";
      } else {
        hubLossBar.className = "hub-bar-fill fill-safe";
      }
    }

    const hubFloatingQuick = el("hubFloatingQuick");
    if (hubFloatingQuick) {
      const flt = Number(ea.total_floating_pnl || 0);
      hubFloatingQuick.textContent = `Floating: ${fmtUsd(flt)}`;
      hubFloatingQuick.style.color = flt > 0.005 ? "var(--bull)" : flt < -0.005 ? "var(--bear)" : "var(--text-secondary)";
    }
    const adapt = cb.adaptive_tuning || lm.adaptive_tuning || {};
    const allocPct = Math.round(Number(adapt.lot_scale ?? 1.0) * 100);
    const adaptActive = Boolean(adapt.active);

    if (el("hubStreakQuick")) {
      el("hubStreakQuick").textContent = adaptActive
        ? `🧬 ปรับจูนอัตโนมัติ (แพ้ ${consecCount} ไม้ • Lot ${allocPct}%)`
        : `แพ้ติดกัน: ${consecCount} / 3 ไม้`;
      el("hubStreakQuick").style.color = adaptActive ? "var(--warn)" : consecCount >= 2 ? "#fcd34d" : "var(--text-muted)";
    }

    if (el("consecLossVal")) el("consecLossVal").textContent = `-$${fmt(consecLoss, 2)} (${fmt(consecLossPct, 1)}%)`;
    if (el("consecCountVal")) el("consecCountVal").textContent = `${consecCount} / 3 ไม้`;
    if (el("adaptiveAllocVal")) {
      el("adaptiveAllocVal").textContent = adaptActive
        ? `${allocPct}% (+${adapt.min_confluence_boost || 0} Conf • SL ${Number(adapt.sl_buffer_mult || 1).toFixed(2)}x)`
        : "100% (ปกติ)";
    }

    const streakBarPct = Math.min(100, Math.round((consecCount / 3) * 100));
    if (el("lossProgressBarFill")) {
      el("lossProgressBarFill").style.width = `${streakBarPct}%`;
      if (adaptActive) {
        el("lossProgressBarFill").style.backgroundColor = "var(--color-warning, #f59e0b)";
      } else if (consecCount >= 2) {
        el("lossProgressBarFill").style.backgroundColor = "#38bdf8";
      } else {
        el("lossProgressBarFill").style.backgroundColor = "var(--color-accent, #10b981)";
      }
    }
    if (el("lossProgressText")) {
      el("lossProgressText").textContent = adaptActive
        ? `🧬 ทำงานเบื้องหลัง: ปรับปรุงจุดเข้า & ลด Lot เหลือ ${allocPct}%`
        : `${consecCount} / 3 ไม้ (โหมดปกติ)`;
    }

    if (cbAlertBox && cbStatusText) {
      if (isTripped) {
        cbAlertBox.className = "cb-alert-box alert-tripped";
        cbStatusText.textContent = cb.reason || `🛑 แตะลิมิตขาดทุนพอร์ตที่ตั้งไว้ (Floor $${fmt(stopFloorBal, 2)})`;
      } else if (adaptActive) {
        cbAlertBox.className = "cb-alert-box normal";
        cbStatusText.textContent = adapt.label_th || `🧬 วิเคราะห์เบื้องหลังทำงาน (แพ้ติดกัน ${consecCount} ไม้) — ปรับเกณฑ์เข้าไม้และจัดสรร Lot อัตโนมัติ`;
      } else {
        cbAlertBox.className = "cb-alert-box normal";
        cbStatusText.textContent = "✅ สภาวะปกติ: ระบบพร้อมเทรดและจัดสรรออเดอร์ 100% (จะปรับจูนอัตโนมัติหากแพ้ติดกัน ≥ 3 ไม้)";
      }
    }

    // 3. Loss Memory & Deep Re-Analysis
    renderLossPatterns(lm.patterns || []);
    renderReanalysisReport(lm.latest_reanalysis);
  }

  function renderControlHubAndScanners(state) {
    if (!state) return;
    const ea = state.ea_bridge || {};
    const dpt = state.daily_profit_tracking || {};
    const cb = state.circuit_breaker_20pct || {};
    const smc = state.smc || {};
    const sim = state.simulation || {};
    const ai = state.ai_advisory || {};
    const cfg = state.config || {};
    const st = state.structure || {};

    const isAutoOn = Boolean(ea.auto_trading_enabled);
    const isRapidOn = Boolean(
      (state.rapid_fire_test && state.rapid_fire_test.enabled) ||
      (cfg.rapid_fire_test && cfg.rapid_fire_test.enabled)
    );
    const isTripped = Boolean(cb.tripped);
    const isProfitLocked = Boolean(
      dpt.daily_profit_target_reached && (dpt.daily_profit_target_enabled ?? cfg.daily_profit_target_enabled ?? true)
    );

    const curPrice = Number(st.current_price || 0);
    const activeOrders = buildUnifiedActiveOrders(state, curPrice);
    const smcOrders = activeOrders.filter((o) => String(o.strategy_mode || "").toUpperCase() !== "RAPID_FIRE");
    const rapidOrders = activeOrders.filter((o) => String(o.strategy_mode || "").toUpperCase() === "RAPID_FIRE");

    // 1. SMC Swing Scanner Row (M15)
    const smcRow = el("scannerRowSmc");
    const smcBadge = el("smcScanBadge");
    const smcText = el("smcScanText");
    if (smcRow && smcBadge && smcText) {
      if (smcOrders.length > 0) {
        smcRow.className = "scanner-item is-active-trade";
        smcBadge.textContent = `ACTIVE (${smcOrders.length})`;
        smcText.textContent = `ถือครองไม้ SMC ${smcOrders[0].side} @ ${fmtPrice(smcOrders[0].entry_mt5)}`;
      } else if (isTripped) {
        smcRow.className = "scanner-item is-paused";
        smcBadge.textContent = "LOCKED";
        smcText.textContent = "หยุดสแกน (Circuit Breaker ตัดวงจร)";
      } else if (isProfitLocked) {
        smcRow.className = "scanner-item is-paused";
        smcBadge.textContent = "TARGET HIT";
        smcText.textContent = "พักการสแกน (ทำกำไรถึงเป้าวันนี้แล้ว)";
      } else if (!isAutoOn) {
        smcRow.className = "scanner-item is-paused";
        smcBadge.textContent = "PAUSED";
        smcText.textContent = "หยุดสแกนชั่วคราว (Auto-Trading ปิดอยู่)";
      } else {
        smcRow.className = "scanner-item is-scanning";
        const setup = smc.trade_setup || {};
        const setupStatus = String(setup.status || "").toUpperCase();
        if (setupStatus === "ACTIVE_IN_ZONE" || setupStatus === "READY") {
          smcBadge.textContent = "SIGNAL";
          smcText.textContent = `พบโซน ${setup.side || ""} M15 • กำลังตรวจเงื่อนไขเข้าไม้...`;
        } else {
          smcBadge.textContent = "SCANNING";
          smcText.textContent = "กำลังสแกนโครงสร้าง M15 (OB / FVG / Sweep)...";
        }
      }
    }

    // 2. Rapid Scalp Scanner Row (M1)
    const rapidRow = el("scannerRowRapid");
    const rapidBadge = el("rapidScanBadge");
    const rapidText = el("rapidScanText");
    if (rapidRow && rapidBadge && rapidText) {
      if (rapidOrders.length > 0) {
        rapidRow.className = "scanner-item is-active-trade";
        rapidBadge.textContent = `ACTIVE (${rapidOrders.length})`;
        rapidText.textContent = `รันไม้ Rapid ${rapidOrders[0].side} @ ${fmtPrice(rapidOrders[0].entry_mt5)}`;
      } else if (!isRapidOn) {
        rapidRow.className = "scanner-item is-paused";
        rapidBadge.textContent = "OFF";
        rapidText.textContent = "ปิดโหมด Rapid Scalp (ใช้เฉพาะ SMC)";
      } else if (isTripped) {
        rapidRow.className = "scanner-item is-paused";
        rapidBadge.textContent = "LOCKED";
        rapidText.textContent = "หยุดสแกน (Circuit Breaker ตัดวงจร)";
      } else if (isProfitLocked) {
        rapidRow.className = "scanner-item is-paused";
        rapidBadge.textContent = "TARGET HIT";
        rapidText.textContent = "พักการสแกน (ทำกำไรถึงเป้าวันนี้แล้ว)";
      } else if (!isAutoOn) {
        rapidRow.className = "scanner-item is-paused";
        rapidBadge.textContent = "PAUSED";
        rapidText.textContent = "หยุดสแกนชั่วคราว (Auto-Trading ปิดอยู่)";
      } else {
        rapidRow.className = "scanner-item is-scanning";
        rapidBadge.textContent = "SCANNING";
        rapidText.textContent = "กำลังสแกนโมเมนตัมสเกลป์ M1 แบบเรียลไทม์...";
      }
    }

    // 3. LED 1: Gemini AI Status Light
    const ledGemini = el("ledGemini");
    const ledGeminiText = el("ledGeminiText");
    const ledWrapGemini = el("ledWrapGemini");
    if (ledGemini && ledGeminiText) {
      const hasKey = Boolean(cfg.has_gemini_key);
      const provider = String(ai.provider || "").toLowerCase();
      const verdict = String(ai.verdict || "").toUpperCase();
      const score = ai.confluence_score != null ? Number(ai.confluence_score) : null;
      const isLiveGemini = provider.includes("gemini") && !provider.includes("deterministic");
      const genTime = ai.generated_at_bkk ? ` (${ai.generated_at_bkk})` : "";

      if (!hasKey) {
        ledGemini.className = "status-led led-red";
        ledGeminiText.textContent = "ยังไม่ใส่ API Key";
      } else if (verdict === "BLOCK" || verdict === "REJECT" || verdict === "FAIL") {
        ledGemini.className = "status-led led-red";
        ledGeminiText.textContent = `BLOCK${score != null ? ` • ${score}/100` : ""}`;
      } else if (isLiveGemini) {
        ledGemini.className = verdict === "WAIT" ? "status-led led-amber" : "status-led led-green";
        ledGeminiText.textContent = `LIVE • ${verdict || "OK"}${score != null ? ` (${score})` : ""}`;
      } else {
        ledGemini.className = verdict === "PASS" ? "status-led led-green" : "status-led led-cyan";
        ledGeminiText.textContent = `พร้อมใช้งาน • ${verdict || "STANDBY"}${score != null ? ` (${score})` : ""}`;
      }

      if (ledWrapGemini) {
        ledWrapGemini.title = `Gemini AI${genTime}: ${ai.headline || ai.provider || "พร้อมวิเคราะห์เบื้องหลัง"}`;
      }
    }

    // 4. LED 2: Monte Carlo Chart Simulation Status Light
    const ledSim = el("ledSim");
    const ledSimText = el("ledSimText");
    const ledWrapSim = el("ledWrapSim");
    if (ledSim && ledSimText) {
      const probs = sim.probabilities || {};
      const winPct = Number(probs.win_pct || 0);
      const evR = Number(sim.expected_value_r || 0);
      const replays = Number(sim.replays || 0);

      if (replays > 0) {
        if (evR > 0 && winPct >= 50) {
          ledSim.className = "status-led led-green";
        } else if (evR >= -0.05) {
          ledSim.className = "status-led led-amber";
        } else {
          ledSim.className = "status-led led-red";
        }
        const evSign = evR >= 0 ? "+" : "";
        ledSimText.textContent = `Win ${winPct.toFixed(0)}% • EV ${evSign}${evR.toFixed(2)}R`;
        if (ledWrapSim) {
          ledWrapSim.title = `จำลองกราฟ ${replays.toLocaleString()} เส้นทาง: ${sim.verdict || ""}`;
        }
      } else {
        ledSim.className = "status-led led-idle";
        ledSimText.textContent = "รอจำลองเส้นทาง...";
        if (ledWrapSim) {
          ledWrapSim.title = "กำลังรอข้อมูลแท่งเทียนสำหรับจำลองกราฟ Monte Carlo";
        }
      }
    }
  }

  let quickLossInputMode = "pct";

  function getLiveAnchorBaseBalance() {
    const ea = currentState && currentState.ea_bridge ? currentState.ea_bridge : {};
    const cfg = currentState && currentState.config ? currentState.config : {};
    const liveBal = ea.is_connected && Number(ea.balance || 0) > 0
      ? Number(ea.balance)
      : Number(cfg.account_balance || 5000);
    return Math.max(1, Number(liveBal || 0));
  }

  function updateQuickPnlPreviews(source = "pct") {
    const baseBal = getLiveAnchorBaseBalance();
    if (el("quickAnchorBalanceVal")) {
      el("quickAnchorBalanceVal").textContent = `$${baseBal.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }

    const profitInp = el("quickDailyProfitTarget");
    const pctInp = el("quickMaxLossPct");
    const usdInp = el("quickMaxLossUsd");

    const rawProfit = profitInp && profitInp.value !== "" ? Number(profitInp.value) : 0;
    const targetUsd = Number.isFinite(rawProfit) ? Math.max(0, rawProfit) : 0;
    if (el("quickProfitGoalHint")) {
      if (targetUsd > 0) {
        const goalBal = baseBal + targetUsd;
        el("quickProfitGoalHint").textContent = `เป้าพอร์ตปลายทาง: $${fmt(goalBal, 2)} (+$${fmt(targetUsd, 2)} จาก Balance ปัจจุบัน $${fmt(baseBal, 2)})`;
      } else {
        el("quickProfitGoalHint").textContent = "ใส่ค่า > $0 เพื่อให้ระบบล็อกกำไรและหยุดเทรดเมื่อถึงเป้า";
      }
    }

    let lossPct = pctInp && pctInp.value !== "" ? Number(pctInp.value) : 20.0;
    let lossUsd = usdInp && usdInp.value !== "" ? Number(usdInp.value) : (baseBal * lossPct / 100);

    if (source === "usd") {
      quickLossInputMode = "usd";
      lossUsd = Number.isFinite(lossUsd) ? Math.max(0, Math.min(baseBal * 0.95, lossUsd)) : 0;
      lossPct = baseBal > 0 ? Number(((lossUsd / baseBal) * 100).toFixed(2)) : 20.0;
      if (pctInp) pctInp.value = lossPct;
    } else {
      quickLossInputMode = "pct";
      lossPct = Number.isFinite(lossPct) ? Math.max(1, Math.min(95, lossPct)) : 20.0;
      lossUsd = Number((baseBal * (lossPct / 100)).toFixed(2));
      if (usdInp) usdInp.value = lossUsd;
    }

    const stopFloor = Math.max(0, baseBal - lossUsd);
    if (el("quickLossFloorHint")) {
      el("quickLossFloorHint").textContent =
        `จุดตัดขาดทุน (Stop Floor): $${fmt(stopFloor, 2)} (ขาดทุนสูงสุด -$${fmt(lossUsd, 2)} / -${fmt(lossPct, 1)}% จาก Balance $${fmt(baseBal, 2)})`;
    }
  }

  function populatePnlTargetForm(cfg) {
    if (!cfg) return;
    const baseBal = getLiveAnchorBaseBalance();
    const targetVal = cfg.daily_profit_target_usd != null ? Number(cfg.daily_profit_target_usd) : 10.0;
    const lossPctVal = cfg.max_consecutive_loss_pct != null ? Number(cfg.max_consecutive_loss_pct) : 20.0;
    const lossUsdVal = Number((baseBal * (lossPctVal / 100)).toFixed(2));

    if (el("quickDailyProfitTarget")) el("quickDailyProfitTarget").value = targetVal;
    if (el("quickMaxLossPct")) el("quickMaxLossPct").value = lossPctVal;
    if (el("quickMaxLossUsd")) el("quickMaxLossUsd").value = lossUsdVal;
    if (el("quickRiskPct")) el("quickRiskPct").value = cfg.risk_per_trade_pct ?? 2.0;
    if (el("quickDailyProfitEnabled")) {
      el("quickDailyProfitEnabled").checked = Boolean(cfg.daily_profit_target_enabled ?? true);
    }
    quickLossInputMode = "pct";
    updateQuickPnlPreviews("pct");
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
    if (el("cfgDailyProfitTarget")) el("cfgDailyProfitTarget").value = cfg.daily_profit_target_usd ?? 10.0;
    if (el("cfgMaxConsecutiveLossPct")) el("cfgMaxConsecutiveLossPct").value = cfg.max_consecutive_loss_pct ?? 20.0;
    if (el("cfgAutoTrading")) {
      el("cfgAutoTrading").checked = Boolean(
        (currentState && currentState.ea_bridge && currentState.ea_bridge.auto_trading_enabled) ??
        (cfg && cfg.auto_trading && cfg.auto_trading.enabled) ?? true
      );
    }
    if (el("cfgDailyProfitTargetEnabled")) {
      el("cfgDailyProfitTargetEnabled").checked = Boolean(
        cfg?.daily_profit_target_enabled ?? true
      );
    }
  }

  // ---------------- Sideway Detection Popup (30s Safe Auto-Pause) & Auto-Resume ----------------
  let sidewayTimerId = null;
  let sidewayActivePromptId = 0;
  let sidewayDeadlineLocalMs = 0;
  let sidewayLastResumedTs = 0;
  let sidewaySendingDecision = false;

  async function sendSidewayDecision(decision) {
    if (sidewaySendingDecision) return;
    sidewaySendingDecision = true;
    const modal = el("sidewayDecisionModal");
    if (sidewayTimerId) {
      clearInterval(sidewayTimerId);
      sidewayTimerId = null;
    }
    try {
      const res = await fetch("/api/sideway/decision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: CURRENT_SYMBOL, decision }),
      });
      const data = await res.json();
      if (modal) {
        modal.classList.add("hidden");
        modal.setAttribute("aria-hidden", "true");
      }
      if (data.status === "ok") {
        if (data.state) renderDashboard(data.state);
        showToast(data.message || (decision === "continue" ? "▶️ ให้ระบบทำงานต่อใน Sideway" : "⏸️ หยุดออกไม้ เฝ้ารอหลุด Sideway"));
      }
    } catch (err) {
      showToast("⚠️ ส่งคำสั่ง Sideway ไม่สำเร็จ: " + err.message);
    } finally {
      sidewaySendingDecision = false;
    }
  }

  function updateSidewayCountdownUI() {
    const remMs = Math.max(0, sidewayDeadlineLocalMs - Date.now());
    const remSec = Math.ceil(remMs / 1000);
    const pct = Math.max(0, Math.min(100, (remMs / 30000) * 100));
    const cdText = el("sidewayCountdownText");
    const cdBar = el("sidewayCountdownBar");
    const btnSec = el("btnSidewayPauseSec");
    if (cdText) cdText.textContent = `${remSec} วินาที`;
    if (cdBar) cdBar.style.width = `${pct.toFixed(1)}%`;
    if (btnSec) btnSec.textContent = String(remSec);
    if (remMs <= 0 && !sidewaySendingDecision) {
      sendSidewayDecision("pause");
    }
  }

  function renderSidewayGuard(state) {
    const sg = (state && state.sideway_guard) || null;
    const modal = el("sidewayDecisionModal");
    const bar = el("hubSidewayStatusBar");
    const barIcon = el("hubSidewayIcon");
    const barText = el("hubSidewayText");
    const barBtn = el("btnHubSidewayAction");
    if (!sg) return;

    // Auto-resume celebration Toast when chart breaks out of Sideway into a clear Trend
    const resumedTs = Number(sg.just_resumed_ts || 0);
    if (resumedTs > 0 && resumedTs !== sidewayLastResumedTs) {
      sidewayLastResumedTs = resumedTs;
      if (sg.just_resumed_note_th) {
        showToast(sg.just_resumed_note_th);
      }
    }

    const status = String(sg.status || "NORMAL");

    // 1. Card 3 Status Banner
    if (bar) {
      if (status === "PAUSED_WAIT_BREAKOUT" || status === "PENDING_USER") {
        bar.classList.remove("hidden", "mode-continue");
        if (barIcon) barIcon.textContent = "⏸️";
        if (barText) {
          barText.textContent =
            status === "PENDING_USER"
              ? "🦀 ตรวจพบ Sideway — หยุดออกไม้ชั่วคราว รอการตัดสินใจ (30 วิ)"
              : "⏸️ หยุดออกไม้เฝ้ารอหลุด Sideway — จะกลับมาออกไม้อัตโนมัติเมื่อเข้าเทรนด์";
        }
        if (barBtn) {
          barBtn.textContent = "▶️ เปิดออกไม้ต่อทันที";
          barBtn.dataset.action = "resume";
        }
      } else if (status === "CONTINUE_ALLOWED") {
        bar.classList.remove("hidden");
        bar.classList.add("mode-continue");
        if (barIcon) barIcon.textContent = "🦀";
        if (barText) barText.textContent = "อนุญาตให้ระบบออกไม้ทำงานต่อในภาวะ Sideway";
        if (barBtn) {
          barBtn.textContent = "⏸️ หยุดรอจังหวะ";
          barBtn.dataset.action = "pause";
        }
      } else {
        bar.classList.add("hidden");
      }
    }

    // 2. 30-Second Decision Popup Modal
    if (!modal) return;
    if (status === "PENDING_USER") {
      const pid = Number(sg.prompt_id || 1);
      const remSecSrv = Math.max(1, Math.min(30, Number(sg.remaining_sec || 30)));
      if (el("sidewayReasonText")) {
        el("sidewayReasonText").textContent = sg.reason_th || "กราฟแกว่งตัวในกรอบ Sideway";
      }
      if (pid !== sidewayActivePromptId || modal.classList.contains("hidden")) {
        sidewayActivePromptId = pid;
        sidewayDeadlineLocalMs = Date.now() + remSecSrv * 1000;
        modal.classList.remove("hidden");
        modal.setAttribute("aria-hidden", "false");
        updateSidewayCountdownUI();
        if (sidewayTimerId) clearInterval(sidewayTimerId);
        sidewayTimerId = setInterval(updateSidewayCountdownUI, 250);
      }
    } else {
      if (!modal.classList.contains("hidden")) {
        modal.classList.add("hidden");
        modal.setAttribute("aria-hidden", "true");
      }
      if (sidewayTimerId) {
        clearInterval(sidewayTimerId);
        sidewayTimerId = null;
      }
    }
  }

  // ---------------- Online AI Self-Learning Trade Memory Renderer ----------------
  function renderAiMemoryTrainer(state) {
    const mem = (state && state.ai_memory_trainer) || null;
    const ev = (state && state.ai_memory_eval) || null;
    if (!mem) return;

    const badge = el("aiMemStatsBadge");
    if (badge) {
      const tot = Number(mem.total_trained_trades || 0);
      const w = Number(mem.total_wins || 0);
      const l = Number(mem.total_losses || 0);
      const wr = Number(mem.overall_win_rate_pct || 0).toFixed(1);
      const mat = Number(mem.maturity_pct || 0).toFixed(0);
      badge.textContent = `เทรนแล้ว: ${tot} ไม้ (ชนะ ${w} / แพ้ ${l} • Win ${wr}% • ความพร้อม ${mat}%)`;
    }

    const evalEl = el("aiMemCurrentEval");
    if (evalEl && ev && ev.insight_th) {
      evalEl.textContent = ev.insight_th;
      evalEl.className = "ai-mem-live-pill";
      if (ev.risk_level === "HIGH_CAUTION") evalEl.classList.add("level-caution");
      else if (ev.risk_level === "FAVORABLE") evalEl.classList.add("level-favorable");
    }

    const loseEl = el("aiMemLosingList");
    if (loseEl) {
      const list = Array.isArray(mem.top_losing_patterns) ? mem.top_losing_patterns : [];
      if (!list.length) {
        loseEl.innerHTML = `<p class="empty-hint">ยังไม่มีลักษณะเข้าไม้ที่แพ้สะสม</p>`;
      } else {
        loseEl.innerHTML = list
          .map((it) => {
            const pnl = Number(it.net_pnl_usd || 0);
            const pnlStr = `${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toFixed(2)}`;
            return `
              <div class="ai-mem-row">
                <span class="ai-mem-row-label">${it.label_th || it.combo_key}</span>
                <span class="ai-mem-row-meta">
                  <span>ชนะ ${it.wins}/แพ้ ${it.losses}</span>
                  <span class="ai-mem-badge-loss">AI ${Number(it.bayes_win_pct || 0).toFixed(0)}% (${pnlStr})</span>
                </span>
              </div>`;
          })
          .join("");
      }
    }

    const winEl = el("aiMemWinningList");
    if (winEl) {
      const list = Array.isArray(mem.top_winning_patterns) ? mem.top_winning_patterns : [];
      if (!list.length) {
        winEl.innerHTML = `<p class="empty-hint">ยังไม่มีลักษณะเข้าไม้ที่ทำกำไรสะสม</p>`;
      } else {
        winEl.innerHTML = list
          .map((it) => {
            const pnl = Number(it.net_pnl_usd || 0);
            const pnlStr = `${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toFixed(2)}`;
            return `
              <div class="ai-mem-row">
                <span class="ai-mem-row-label">${it.label_th || it.combo_key}</span>
                <span class="ai-mem-row-meta">
                  <span>ชนะ ${it.wins}/แพ้ ${it.losses}</span>
                  <span class="ai-mem-badge-win">AI ${Number(it.bayes_win_pct || 0).toFixed(0)}% (${pnlStr})</span>
                </span>
              </div>`;
          })
          .join("");
      }
    }

    const wEl = el("aiMemFeatureWeights");
    if (wEl) {
      const negs = Array.isArray(mem.top_negative_features) ? mem.top_negative_features : [];
      const poss = Array.isArray(mem.top_positive_features) ? mem.top_positive_features : [];
      const pills = [
        ...negs.map((f) => `<span class="ai-weight-pill w-neg">🔻 ${f.label_th} (${Number(f.weight).toFixed(2)})</span>`),
        ...poss.map((f) => `<span class="ai-weight-pill w-pos">🔺 ${f.label_th} (+${Number(f.weight).toFixed(2)})</span>`),
      ];
      wEl.innerHTML = pills.length ? pills.join("") : `<span class="empty-hint">กำลังรอสะสมข้อมูลเทรดจริง (อัปเดตอัตโนมัติทุกไม้ที่ปิด)</span>`;
    }
  }

  function renderDashboard(state) {
    currentState = state;
    const st = state.structure || {};
    const quote = state.live_quote;

    // Render Rapid Scalp production mode toggle switch & badge
    const switchRapid = el("switchRapidFire");
    const badgeRapid = el("badgeRapidFire");
    const isRapidOn = Boolean(
      (state.rapid_fire_test && state.rapid_fire_test.enabled) ||
      (state.config && state.config.rapid_fire_test && state.config.rapid_fire_test.enabled)
    );
    if (switchRapid && switchRapid.checked !== isRapidOn) {
      switchRapid.checked = isRapidOn;
    }
    if (badgeRapid) {
      badgeRapid.textContent = isRapidOn ? "ON" : "OFF";
      badgeRapid.className = `switch-status-pill ${isRapidOn ? "pill-on" : "pill-off"}`;
    }

    // Render MT5 Auto-Trading toggle switch & live dot indicator
    const switchAuto = el("switchAutoTrading");
    const dotAuto = el("dotAutoTrading");
    const badgeAuto = el("badgeAutoTrading");
    const isAutoOn = Boolean(state.ea_bridge && state.ea_bridge.auto_trading_enabled);
    if (switchAuto && switchAuto.checked !== isAutoOn) {
      switchAuto.checked = isAutoOn;
    }
    if (dotAuto) {
      dotAuto.className = `switch-live-dot ${isAutoOn ? "dot-live" : "dot-paused"}`;
    }
    if (badgeAuto) {
      badgeAuto.textContent = isAutoOn ? "ON" : "OFF";
      badgeAuto.className = `switch-status-pill ${isAutoOn ? "pill-on" : "pill-off"}`;
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
    renderDailyProfitAndLossCircuit(state);
    renderControlHubAndScanners(state);
    renderSidewayGuard(state);
    renderAiMemoryTrainer(state);

    const modalEl = el("settingsModal");
    if (modalEl && modalEl.classList.contains("hidden")) {
      populateSettingsForm(state.config || {});
    }
    const pnlModalEl = el("pnlTargetModal");
    if (pnlModalEl && pnlModalEl.classList.contains("hidden")) {
      populatePnlTargetForm(state.config || {});
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
            if (currentState.ea_bridge && currentState.ea_bridge.is_connected && Number(currentState.ea_bridge.balance || 0) > 0) {
              const liveB = Number(currentState.ea_bridge.balance);
              const liveE = Number(currentState.ea_bridge.equity || liveB);
              const dpt = currentState.daily_profit_tracking || {};
              const ancB = Number(dpt.anchor_balance || currentState.config?.pnl_anchor_balance || 0);
              if (ancB > 0) {
                dpt.daily_realized_pnl_usd = Number((liveB - ancB).toFixed(2));
                dpt.session_equity_pnl_usd = Number((liveE - ancB).toFixed(2));
                const cb = currentState.circuit_breaker_20pct || {};
                const ddUsd = Math.max(0, Number((ancB - Math.min(liveB, liveE)).toFixed(2)));
                cb.anchor_drawdown_usd = ddUsd;
                cb.anchor_drawdown_pct = Number(((ddUsd / ancB) * 100).toFixed(2));
                renderDailyProfitAndLossCircuit(currentState);
              }
            }
            renderControlHubAndScanners(currentState);
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

    // Dedicated Quick Modal: Portfolio Profit & Loss Target Settings (from Card 2)
    const pnlModal = el("pnlTargetModal");
    const btnOpenPnl = el("btnOpenPnlTargetModal");
    const btnClosePnl = el("btnClosePnlTarget");
    const btnCancelPnl = el("btnCancelPnlTarget");
    if (btnOpenPnl && pnlModal) {
      btnOpenPnl.addEventListener("click", () => {
        populatePnlTargetForm(currentState ? currentState.config : null);
        pnlModal.classList.remove("hidden");
      });
    }
    if (btnClosePnl && pnlModal) btnClosePnl.addEventListener("click", () => pnlModal.classList.add("hidden"));
    if (btnCancelPnl && pnlModal) btnCancelPnl.addEventListener("click", () => pnlModal.classList.add("hidden"));
    if (pnlModal) {
      pnlModal.addEventListener("click", (e) => {
        if (e.target === pnlModal) pnlModal.classList.add("hidden");
      });
    }

    const qProfitInp = el("quickDailyProfitTarget");
    const qLossPctInp = el("quickMaxLossPct");
    const qLossUsdInp = el("quickMaxLossUsd");
    if (qProfitInp) qProfitInp.addEventListener("input", () => updateQuickPnlPreviews(quickLossInputMode));
    if (qLossPctInp) qLossPctInp.addEventListener("input", () => updateQuickPnlPreviews("pct"));
    if (qLossUsdInp) qLossUsdInp.addEventListener("input", () => updateQuickPnlPreviews("usd"));

    const pnlForm = el("pnlTargetForm");
    if (pnlForm) {
      pnlForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const baseBal = getLiveAnchorBaseBalance();
        const rawProfit = qProfitInp && qProfitInp.value !== "" ? Number(qProfitInp.value) : 0;
        const rawPct = qLossPctInp && qLossPctInp.value !== "" ? Number(qLossPctInp.value) : 20.0;
        const rawUsd = qLossUsdInp && qLossUsdInp.value !== "" ? Number(qLossUsdInp.value) : (baseBal * rawPct / 100);
        const rawRisk = el("quickRiskPct") && el("quickRiskPct").value !== "" ? Number(el("quickRiskPct").value) : 2.0;

        const targetVal = Number.isFinite(rawProfit) ? Math.max(0, rawProfit) : 0;
        const lossPctVal = Number.isFinite(rawPct) ? Math.max(1, Math.min(95, rawPct)) : 20.0;
        const lossUsdVal = Number.isFinite(rawUsd) ? Math.max(0, rawUsd) : Number((baseBal * (lossPctVal / 100)).toFixed(2));
        const riskVal = Number.isFinite(rawRisk) ? Math.max(0.1, Math.min(100, rawRisk)) : 2.0;

        showActionLoading(
          "กำลังบันทึกเป้ากำไร / ขาดทุน...",
          `กำลังอ้างอิงจาก Balance $${fmt(baseBal, 2)} และอัปเดตเป้ากำไร/จุดตัดขาดทุน...`,
          "🎯"
        );
        const payload = {
          symbol: CURRENT_SYMBOL,
          reanchor_pnl: true,
          loss_input_mode: quickLossInputMode,
          daily_profit_target_usd: targetVal,
          daily_profit_target_enabled: Boolean(el("quickDailyProfitEnabled")?.checked ?? true),
          max_consecutive_loss_pct: lossPctVal,
          max_loss_usd: lossUsdVal,
          risk_per_trade_pct: riskVal,
        };
        try {
          const resp = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          if (resp.ok) {
            const newState = await resp.json();
            renderDashboard(newState);
            if (pnlModal) pnlModal.classList.add("hidden");
            const anc = newState?.daily_profit_tracking?.anchor_balance ?? baseBal;
            const maxL = newState?.circuit_breaker_20pct?.max_loss_usd ?? lossUsdVal;
            showToast(`🎯 บันทึกสำเร็จ! เริ่มนับจาก Balance $${fmt(anc, 2)} (เป้า +$${fmt(targetVal, 2)} / ตัดขาดทุน -$${fmt(maxL, 2)})`);
          } else {
            showToast("⚠️ ไม่สามารถบันทึกเป้ากำไร/ขาดทุนได้");
          }
        } catch (_) {
          showToast("❌ เชื่อมต่อล้มเหลวขณะบันทึกเป้ากำไร/ขาดทุน");
        } finally {
          hideActionLoading(400);
        }
      });
    }

    // Modern Switch: Rapid Scalp Toggle
    const switchRapid = el("switchRapidFire");
    if (switchRapid) {
      switchRapid.addEventListener("change", async (e) => {
        const nextRapidVal = e.target.checked;
        showActionLoading(
          nextRapidVal ? "กำลังเปิด Rapid Scalp..." : "กำลังปิด Rapid Scalp...",
          "กำลังปรับแต่งระบบสเกลป์ความถี่สูง SMC และซิงค์การบริหารความเสี่ยง...",
          "⚡"
        );
        try {
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ action: "toggle_rapid_fire", enabled: nextRapidVal, symbol: CURRENT_SYMBOL }));
          } else {
            await fetch("/api/rapid_fire/toggle", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ symbol: CURRENT_SYMBOL, enabled: nextRapidVal }),
            });
          }
          if (currentState) {
            if (!currentState.rapid_fire_test) currentState.rapid_fire_test = {};
            currentState.rapid_fire_test.enabled = nextRapidVal;
            if (currentState.config) {
              if (!currentState.config.rapid_fire_test) currentState.config.rapid_fire_test = {};
              currentState.config.rapid_fire_test.enabled = nextRapidVal;
            }
            renderControlHubAndScanners(currentState);
          }
          const badgeRapid = el("badgeRapidFire");
          if (badgeRapid) {
            badgeRapid.textContent = nextRapidVal ? "ON" : "OFF";
            badgeRapid.className = `switch-status-pill ${nextRapidVal ? "pill-on" : "pill-off"}`;
          }
          if (nextRapidVal) {
            showToast("⚡ เปิดระบบ Rapid Scalp (M1) • ทำงานควบคู่กับ SMC Swing");
          } else {
            showToast("🛑 ปิดระบบ Rapid Scalp แล้ว • ทำงานเฉพาะ SMC Swing (M15)");
          }
        } catch (_) {
          showToast("❌ สลับสถานะ Rapid Scalp ไม่สำเร็จ");
          e.target.checked = !nextRapidVal;
        } finally {
          hideActionLoading(400);
        }
      });
    }

    // Legacy Button Compatibility for Rapid Fire if present
    const toggleRapidBtn = el("btnToggleRapidFire");
    if (toggleRapidBtn) {
      toggleRapidBtn.addEventListener("click", async () => {
        const isRapidCurrentlyOn = Boolean(
          (currentState?.rapid_fire_test && currentState.rapid_fire_test.enabled) ||
          (currentState?.config && currentState.config.rapid_fire_test && currentState.config.rapid_fire_test.enabled)
        );
        const nextRapidVal = !isRapidCurrentlyOn;
        showActionLoading(
          nextRapidVal ? "กำลังเปิด Rapid Scalp..." : "กำลังปิด Rapid Scalp...",
          "กำลังปรับแต่งระบบสเกลป์ความถี่สูง SMC และซิงค์การบริหารความเสี่ยง...",
          "⚡"
        );
        try {
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ action: "toggle_rapid_fire", enabled: nextRapidVal, symbol: CURRENT_SYMBOL }));
          } else {
            await fetch("/api/rapid_fire/toggle", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ symbol: CURRENT_SYMBOL, enabled: nextRapidVal }),
            });
          }
          if (nextRapidVal) {
            showToast("⚡ เปิดโหมด Rapid Scalp (ใช้งานจริง) • สไนเปอร์ความถี่สูงตามโครงสร้าง SMC");
          } else {
            showToast("🛑 ปิดโหมด Rapid Scalp แล้ว • คืนสู่โหมดปกติ");
          }
        } catch (_) {
          showToast("❌ สลับสถานะโหมดไม่สำเร็จ");
        } finally {
          hideActionLoading(400);
        }
      });
    }

    // Modern Switch: Auto-Trading Toggle
    const switchAuto = el("switchAutoTrading");
    if (switchAuto) {
      switchAuto.addEventListener("change", async (e) => {
        const nextVal = e.target.checked;
        showActionLoading(
          nextVal ? "กำลังเปิดส่งคำสั่งเข้า MT5..." : "กำลังปิดส่งคำสั่งเข้า MT5...",
          "กำลังเชื่อมต่อและซิงค์คิวคำสั่งกับ MetaTrader 5 EA Bridge...",
          "🤖"
        );
        try {
          const resp = await fetch("/api/ea/toggle_auto", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbol: CURRENT_SYMBOL, enabled: nextVal }),
          });
          const res = await resp.json();
          if (res.status === "ok") {
            if (currentState && currentState.ea_bridge) {
              currentState.ea_bridge.auto_trading_enabled = res.auto_trading_enabled;
            }
            const dotAuto = el("dotAutoTrading");
            const badgeAuto = el("badgeAutoTrading");
            if (dotAuto) dotAuto.className = `switch-live-dot ${res.auto_trading_enabled ? "dot-live" : "dot-paused"}`;
            if (badgeAuto) {
              badgeAuto.textContent = res.auto_trading_enabled ? "ON" : "OFF";
              badgeAuto.className = `switch-status-pill ${res.auto_trading_enabled ? "pill-on" : "pill-off"}`;
            }
            if (res.auto_trading_enabled) {
              showToast("🟢 เปิดระบบส่งคำสั่งเข้า MT5 อัตโนมัติเรียบร้อย! (คำสั่งใหม่จะถูกส่งเข้า MT5 ทันที)");
            } else {
              showToast("⏸️ ปิดระบบส่งคำสั่งเข้า MT5 ชั่วคราวแล้ว");
            }
            renderDashboard(currentState);
          }
        } catch (_) {
          showToast("❌ สลับสถานะ Auto-Trading ไม่สำเร็จ");
          e.target.checked = !nextVal;
        } finally {
          hideActionLoading(400);
        }
      });
    }

    // Legacy Button Compatibility for Auto-Trading if present
    const toggleAutoBtn = el("btnToggleAutoTrading");
    if (toggleAutoBtn) {
      toggleAutoBtn.addEventListener("click", async () => {
        const curAuto = Boolean(currentState?.ea_bridge && currentState.ea_bridge.auto_trading_enabled);
        const nextVal = !curAuto;
        toggleAutoBtn.disabled = true;
        showActionLoading(
          nextVal ? "กำลังเปิดส่งคำสั่งเข้า MT5..." : "กำลังปิดส่งคำสั่งเข้า MT5...",
          "กำลังเชื่อมต่อและซิงค์คิวคำสั่งกับ MetaTrader 5 EA Bridge...",
          "🤖"
        );
        try {
          const resp = await fetch("/api/ea/toggle_auto", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbol: CURRENT_SYMBOL, enabled: nextVal }),
          });
          const res = await resp.json();
          if (res.status === "ok") {
            if (currentState && currentState.ea_bridge) {
              currentState.ea_bridge.auto_trading_enabled = res.auto_trading_enabled;
            }
            if (res.auto_trading_enabled) {
              showToast("🟢 เปิดระบบส่งคำสั่งเข้า MT5 อัตโนมัติเรียบร้อย! (คำสั่งใหม่จะถูกส่งเข้า MT5 ทันที)");
            } else {
              showToast("⏸️ ปิดระบบส่งคำสั่งเข้า MT5 ชั่วคราวแล้ว");
            }
            renderDashboard(currentState);
          }
        } catch (_) {
          showToast("❌ สลับสถานะ Auto-Trading ไม่สำเร็จ");
        } finally {
          toggleAutoBtn.disabled = false;
          hideActionLoading(400);
        }
      });
    }

    const btnReload = el("btnReloadSync");
    if (btnReload) {
      btnReload.addEventListener("click", async () => {
        btnReload.disabled = true;
        btnReload.innerHTML = "⏳ กำลังซิงค์...";
        showActionLoading(
          "กำลังซิงค์สถานะกับ MT5...",
          "กำลังดึงข้อมูลออเดอร์เปิด, ตรวจสอบ Pending Orders และเคลียร์สถานะคงค้าง...",
          "🔄"
        );
        try {
          const resp = await fetch("/api/order/reload?symbol=" + CURRENT_SYMBOL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbol: CURRENT_SYMBOL }),
          });
          const res = await resp.json();
          if (res.status === "ok") {
            showToast("✅ " + (res.message || "ซิงค์สถานะกับ MT5 สำเร็จ"));
          } else {
            showToast("⚠️ " + (res.message || "ซิงค์สถานะเรียบร้อย"));
          }
        } catch (err) {
          console.error("Reload sync error:", err);
          showToast("❌ เชื่อมต่อเซิร์ฟเวอร์ล้มเหลวขณะรีโหลด");
        } finally {
          btnReload.disabled = false;
          btnReload.innerHTML = "🔄 ซิงค์ MT5";
          hideActionLoading(380);
        }
      });
    }

    async function handlePurgeGhosts(triggerBtn) {
      if (triggerBtn) triggerBtn.disabled = true;
      showActionLoading(
        "กำลังเคลียร์ไม้ผี...",
        "กำลังตรวจสอบและกวาดล้างออเดอร์ตกค้างที่ไม่ตรงกับ MT5 พร้อมปลดล็อกระบบ...",
        "🧹"
      );
      try {
        const resp = await fetch("/api/order/purge_ghosts?symbol=" + CURRENT_SYMBOL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol: CURRENT_SYMBOL }),
        });
        const res = await resp.json();
        if (res.status === "ok") {
          showToast(`🧹 ${res.message || "เคลียร์ไม้ผีเรียบร้อย"}`);
          if (res.state) renderDashboard(res.state);
        } else {
          showToast("⚠️ เคลียร์ไม้ผีเรียบร้อย");
        }
      } catch (err) {
        console.error("Purge ghosts error:", err);
        showToast("❌ เชื่อมต่อล้มเหลวขณะเคลียร์ไม้ผี");
      } finally {
        if (triggerBtn) triggerBtn.disabled = false;
        hideActionLoading(380);
      }
    }

    const btnPurgeTop = el("btnPurgeGhosts");
    if (btnPurgeTop) btnPurgeTop.addEventListener("click", () => handlePurgeGhosts(btnPurgeTop));
    const btnPurgeSec = el("btnSectionPurgeGhosts");
    if (btnPurgeSec) btnPurgeSec.addEventListener("click", () => handlePurgeGhosts(btnPurgeSec));

    const btnSysReset = el("btnSystemReset");
    if (btnSysReset) {
      btnSysReset.addEventListener("click", async () => {
        const ok = window.confirm(
          "🔄 ยืนยันการรีเซ็ตระบบ KAIROS-79?\n\n" +
          "• ปลดล็อกเงื่อนไข Veto / สตรีคแพ้สะสม / บล็อกที่ค้างอยู่ทั้งหมด\n" +
          "• ล้างคิวคำสั่งค้างและไม้ผี (ไม่ปิดออเดอร์จริงที่เปิดอยู่ใน MT5)\n" +
          "• ตั้งฐาน Balance ปัจจุบันใหม่ และเปิด Auto-Trading พร้อมสแกนออกไม้ทันที"
        );
        if (!ok) return;
        const origHtml = btnSysReset.innerHTML;
        btnSysReset.disabled = true;
        btnSysReset.innerHTML = "⏳ กำลังรีเซ็ต...";
        showActionLoading(
          "กำลังรีเซ็ตระบบ & ปลดล็อกเงื่อนไข...",
          "กำลังล้าง Veto/คิวค้าง, ตั้งฐาน Balance ปัจจุบันใหม่ และรันสแกนตลาดรอบใหม่",
          "🔄"
        );
        try {
          const res = await fetch("/api/system/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbol: CURRENT_SYMBOL }),
          });
          const data = await res.json();
          if (data.status === "ok") {
            if (data.state) renderDashboard(data.state);
            showToast(data.message || "🔄 รีเซ็ตระบบสำเร็จ พร้อมสแกนและออกออเดอร์ใหม่ทันที");
          } else {
            showToast("⚠️ รีเซ็ตไม่สำเร็จ: " + (data.error || "Unknown error"));
          }
        } catch (err) {
          showToast("⚠️ เชื่อมต่อเซิร์ฟเวอร์ไม่ได้: " + err.message);
        } finally {
          hideActionLoading();
          btnSysReset.disabled = false;
          btnSysReset.innerHTML = origHtml;
        }
      });
    }

    const btnSwPause = el("btnSidewayPause");
    if (btnSwPause) btnSwPause.addEventListener("click", () => sendSidewayDecision("pause"));
    const btnSwCont = el("btnSidewayContinue");
    if (btnSwCont) btnSwCont.addEventListener("click", () => sendSidewayDecision("continue"));
    const btnHubSw = el("btnHubSidewayAction");
    if (btnHubSw) {
      btnHubSw.addEventListener("click", () => {
        const act = btnHubSw.dataset.action || "resume";
        sendSidewayDecision(act);
      });
    }

    const btnResetAiMem = el("btnResetAiMemory");
    if (btnResetAiMem) {
      btnResetAiMem.addEventListener("click", async () => {
        const ok = window.confirm(
          "🧹 ยืนยันการล้าง AI Trade Memory?\n\nระบบจะล้างค่าน้ำหนักที่ AI เรียนรู้ไว้ทั้งหมดเพื่อเริ่มเทรนใหม่ตั้งแต่ต้น (ไม่กระทบออเดอร์จริงใน MT5)"
        );
        if (!ok) return;
        btnResetAiMem.disabled = true;
        try {
          const res = await fetch("/api/ai_memory/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbol: CURRENT_SYMBOL }),
          });
          const data = await res.json();
          if (data.status === "ok") {
            if (data.state) renderDashboard(data.state);
            showToast(data.message || "🧹 รีเซ็ต AI Trade Memory เรียบร้อย");
          }
        } catch (err) {
          showToast("⚠️ รีเซ็ต AI Memory ไม่สำเร็จ: " + err.message);
        } finally {
          btnResetAiMem.disabled = false;
        }
      });
    }

    const btnReanalyze = el("btnRunDeepReanalysis");
    if (btnReanalyze) {
      btnReanalyze.addEventListener("click", async () => {
        btnReanalyze.disabled = true;
        btnReanalyze.innerHTML = "⏳ กำลังวิเคราะห์...";
        showActionLoading(
          "กำลังรันการวิเคราะห์เชิงลึก...",
          "กำลังประมวลผลคลัสเตอร์ความเสี่ยง, รูปแบบที่แพ้สะสม และสังเคราะห์คำสั่งยุทธศาสตร์...",
          "🔬"
        );
        try {
          const resp = await fetch("/api/reanalysis/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbol: CURRENT_SYMBOL }),
          });
          const res = await resp.json();
          if (res.status === "ok") {
            showToast("🔬 วิเคราะห์เชิงลึกสำเร็จ!");
            if (res.state) renderDashboard(res.state);
          }
        } catch (e) {
          showToast("❌ การวิเคราะห์เชิงลึกล้มเหลว");
        } finally {
          btnReanalyze.disabled = false;
          btnReanalyze.innerHTML = "🔬 รันการวิเคราะห์เชิงลึกใหม่";
          hideActionLoading(400);
        }
      });
    }

    const btnResetCB = el("btnResetCircuitBreaker");
    if (btnResetCB) {
      btnResetCB.addEventListener("click", async () => {
        btnResetCB.disabled = true;
        btnResetCB.innerHTML = "⏳ กำลังปลดล็อก...";
        showActionLoading(
          "กำลังปลดล็อก Circuit Breaker...",
          "กำลังรีเซ็ตสถิติ Drawdown และส่งคำสั่งปลดล็อกระบบเทรดไปยัง MT5...",
          "🔓"
        );
        try {
          const resp = await fetch("/api/circuit_breaker/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbol: CURRENT_SYMBOL }),
          });
          const res = await resp.json();
          if (res.status === "ok") {
            showToast("🔓 " + (res.message || "รีเซ็ต Circuit Breaker สำเร็จ"));
            if (res.state) renderDashboard(res.state);
          }
        } catch (e) {
          showToast("❌ ไม่สามารถรีเซ็ต Circuit Breaker ได้");
        } finally {
          btnResetCB.disabled = false;
          btnResetCB.innerHTML = "🔓 ปลดล็อก Circuit Breaker";
          hideActionLoading(380);
        }
      });
    }

    // History Filter Panel Event Listeners
    const selHistDate = el("filterHistoryDate");
    const inpHistCustomDate = el("filterHistoryCustomDate");
    const selHistCat = el("filterHistoryCategory");
    const selHistOutcome = el("filterHistoryOutcome");
    const btnResetFilters = el("btnResetHistoryFilters");

    if (selHistDate) {
      selHistDate.addEventListener("change", (e) => {
        historyFilterState.dateRange = e.target.value;
        if (inpHistCustomDate) {
          if (e.target.value === "custom") {
            inpHistCustomDate.classList.remove("hidden");
            inpHistCustomDate.focus();
          } else {
            inpHistCustomDate.classList.add("hidden");
          }
        }
        applyAndRenderOrderHistory();
      });
    }

    if (inpHistCustomDate) {
      inpHistCustomDate.addEventListener("change", (e) => {
        historyFilterState.customDate = e.target.value;
        applyAndRenderOrderHistory();
      });
      inpHistCustomDate.addEventListener("input", (e) => {
        historyFilterState.customDate = e.target.value;
        applyAndRenderOrderHistory();
      });
    }

    if (selHistCat) {
      selHistCat.addEventListener("change", (e) => {
        historyFilterState.category = e.target.value;
        applyAndRenderOrderHistory();
      });
    }

    if (selHistOutcome) {
      selHistOutcome.addEventListener("change", (e) => {
        historyFilterState.outcome = e.target.value;
        applyAndRenderOrderHistory();
      });
    }

    if (btnResetFilters) {
      btnResetFilters.addEventListener("click", () => {
        historyFilterState.dateRange = "all";
        historyFilterState.customDate = "";
        historyFilterState.category = "all";
        historyFilterState.outcome = "all";
        if (selHistDate) selHistDate.value = "all";
        if (inpHistCustomDate) {
          inpHistCustomDate.value = "";
          inpHistCustomDate.classList.add("hidden");
        }
        if (selHistCat) selHistCat.value = "all";
        if (selHistOutcome) selHistOutcome.value = "all";
        applyAndRenderOrderHistory();
      });
    }

    const form = el("settingsForm");
    if (form) {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        showActionLoading(
          "กำลังบันทึกการตั้งค่า...",
          "กำลังปรับแต่งพารามิเตอร์พอร์ต ความเสี่ยง และบันทึกลงระบบ...",
          "💾"
        );
        const rawTargetSet = el("cfgDailyProfitTarget") && el("cfgDailyProfitTarget").value !== ""
          ? Number(el("cfgDailyProfitTarget").value)
          : 10.0;
        const rawLossSet = el("cfgMaxConsecutiveLossPct") && el("cfgMaxConsecutiveLossPct").value !== ""
          ? Number(el("cfgMaxConsecutiveLossPct").value)
          : 20.0;
        const payload = {
          symbol: CURRENT_SYMBOL,
          account_balance: Number(el("cfgBalance")?.value || 5000),
          leverage: Number(el("cfgLeverage")?.value || 1000),
          risk_per_trade_pct: Number(el("cfgRiskPct")?.value || 2.0),
          broker_spread_points: Number(el("cfgSpread")?.value || 25),
          daily_profit_target_usd: Number.isFinite(rawTargetSet) ? Math.max(0, rawTargetSet) : 10.0,
          daily_profit_target_enabled: Boolean(el("cfgDailyProfitTargetEnabled")?.checked ?? true),
          max_consecutive_loss_pct: Number.isFinite(rawLossSet) ? Math.max(1, Math.min(95, rawLossSet)) : 20.0,
          auto_trading_enabled: Boolean(el("cfgAutoTrading")?.checked ?? true),
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
          } else {
            showToast("⚠️ ไม่สามารถบันทึกการตั้งค่าได้");
          }
        } catch (_) {
          showToast("❌ ไม่สามารถบันทึกการตั้งค่าได้");
        } finally {
          hideActionLoading(420);
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
