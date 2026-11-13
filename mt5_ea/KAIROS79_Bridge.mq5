//+------------------------------------------------------------------+
//|                                           KAIROS79_Bridge.mq5   |
//|                             Copyright 2026, KAIROS-79 AI Team   |
//|                       https://github.com/ThanarakCSW/KAIROS-79   |
//+------------------------------------------------------------------+
#property copyright   "Copyright 2026, KAIROS-79 AI Team"
#property link        "https://github.com/ThanarakCSW/KAIROS-79"
#property version     "1.00"
#property description "KAIROS-79 MetaTrader 5 Bridge Expert Advisor (EA)"
#property description "Connects MT5 on Windows/VPS directly to KAIROS-79 AI Web Platform."
#property description "Supports Auto ON/OFF, Multi-Order DCA/Scale-in, and Web Monitoring."
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\OrderInfo.mqh>

//--- EA Inputs
input group "=== KAIROS-79 Server Connection ==="
input string   InpServerUrl         = "http://127.0.0.1:8000"; // Server URL (or VPS Public IP)
input int      InpPollIntervalMs    = 500;                     // Command Poll Interval (ms)
input int      InpHeartbeatIntervalSec = 2;                    // Heartbeat Interval (seconds)

input group "=== Trading Safety & Execution ==="
input ulong    InpMagicNumber       = 797979;                  // Magic Number for KAIROS-79
input ulong    InpMaxSlippagePoints = 35;                      // Max Allowed Slippage (points)
input double   InpMaxSpreadPoints   = 50.0;                    // Max Allowed Spread (points)
input string   InpSymbolOverride    = "";                      // Symbol (blank = Chart Symbol)
input bool     InpAutoDetectFilling = true;                    // Auto detect FOK/IOC/Return

//--- Global Objects
CTrade         m_trade;
CPositionInfo  m_position;
COrderInfo     m_order;

string         g_symbol;
datetime       g_last_heartbeat_time = 0;
bool           g_server_online = false;
bool           g_auto_trading_enabled = false;
int            g_consecutive_errors = 0;

//+------------------------------------------------------------------+
//| Helper: Extract string value from simple JSON                    |
//+------------------------------------------------------------------+
string JsonExtractString(const string json, const string key)
{
   string search = "\"" + key + "\":\"";
   int pos = StringFind(json, search);
   if(pos >= 0)
   {
      pos += StringLen(search);
      int end_pos = StringFind(json, "\"", pos);
      if(end_pos > pos)
         return StringSubstr(json, pos, end_pos - pos);
   }
   // Try unquoted (numbers or bools or null)
   search = "\"" + key + "\":";
   pos = StringFind(json, search);
   if(pos >= 0)
   {
      pos += StringLen(search);
      // skip whitespace
      while(pos < StringLen(json) && (StringGetCharacter(json, pos) == ' ' || StringGetCharacter(json, pos) == '\t'))
         pos++;
      int end_pos = pos;
      while(end_pos < StringLen(json))
      {
         ushort ch = StringGetCharacter(json, end_pos);
         if(ch == ',' || ch == '}' || ch == ']' || ch == '\r' || ch == '\n')
            break;
         end_pos++;
      }
      string val = StringSubstr(json, pos, end_pos - pos);
      StringTrimLeft(val);
      StringTrimRight(val);
      return val;
   }
   return "";
}

double JsonExtractDouble(const string json, const string key, double default_val = 0.0)
{
   string val = JsonExtractString(json, key);
   if(val == "" || val == "null") return default_val;
   return StringToDouble(val);
}

long JsonExtractLong(const string json, const string key, long default_val = 0)
{
   string val = JsonExtractString(json, key);
   if(val == "" || val == "null") return default_val;
   return StringToInteger(val);
}

bool JsonExtractBool(const string json, const string key, bool default_val = false)
{
   string val = JsonExtractString(json, key);
   if(val == "true" || val == "1") return true;
   if(val == "false" || val == "0") return false;
   return default_val;
}

//+------------------------------------------------------------------+
//| Setup trade filling mode according to broker support             |
//+------------------------------------------------------------------+
void SetupFillingMode(const string symbol)
{
   uint filling = (uint)SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((filling & SYMBOL_FILLING_FOK) != 0)
      m_trade.SetTypeFilling(ORDER_FILLING_FOK);
   else if((filling & SYMBOL_FILLING_IOC) != 0)
      m_trade.SetTypeFilling(ORDER_FILLING_IOC);
   else
      m_trade.SetTypeFilling(ORDER_FILLING_RETURN);
}

//+------------------------------------------------------------------+
//| Send HTTP Request to KAIROS-79 Server                            |
//+------------------------------------------------------------------+
bool SendHttpRequest(const string method, const string endpoint, const string post_data, string &response_text)
{
   string url = InpServerUrl + endpoint;
   char post_bytes[];
   char result_bytes[];
   string result_headers;
   int timeout_ms = 2500;

   if(StringLen(post_data) > 0)
      StringToCharArray(post_data, post_bytes, 0, WHOLE_ARRAY, CP_UTF8);

   string headers = "Content-Type: application/json; charset=utf-8\r\n";
   ResetLastError();

   int res = WebRequest(method, url, headers, timeout_ms, post_bytes, result_bytes, result_headers);
   if(res == -1)
   {
      int err = GetLastError();
      g_consecutive_errors++;
      if(err == 4014) // ERR_FUNCTION_NOT_ALLOWED
      {
         Print("❌ WebRequest Error (4014): Please add '", InpServerUrl, "' in MT5 Menu -> Tools -> Options -> Expert Advisors -> Allow WebRequest for listed URL");
      }
      else
      {
         PrintFormat("⚠️ WebRequest to %s failed (Error %d)", url, err);
      }
      g_server_online = false;
      return false;
   }

   g_consecutive_errors = 0;
   g_server_online = true;
   response_text = CharArrayToString(result_bytes, 0, WHOLE_ARRAY, CP_UTF8);
   return true;
}

//+------------------------------------------------------------------+
//| Build Open Positions JSON Array                                  |
//+------------------------------------------------------------------+
string BuildOpenPositionsJson()
{
   string json = "[";
   int count = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if(!m_position.SelectByTicket(ticket)) continue;

      if(m_position.Magic() == InpMagicNumber || InpMagicNumber == 0)
      {
         if(count > 0) json += ",";
         json += StringFormat(
            "{\"ticket\":%I64u,\"symbol\":\"%s\",\"type\":\"%s\",\"lots\":%.2f,\"open_price\":%.2f,\"sl\":%.2f,\"tp\":%.2f,\"profit\":%.2f,\"comment\":\"%s\"}",
            ticket,
            m_position.Symbol(),
            (m_position.PositionType() == POSITION_TYPE_BUY ? "BUY" : "SELL"),
            m_position.Volume(),
            m_position.PriceOpen(),
            m_position.StopLoss(),
            m_position.TakeProfit(),
            m_position.Profit() + m_position.Swap(),
            m_position.Comment()
         );
         count++;
      }
   }
   json += "]";
   return json;
}

//+------------------------------------------------------------------+
//| Send Telemetry Heartbeat to KAIROS-79 Server                     |
//+------------------------------------------------------------------+
void SendHeartbeat()
{
   long   acc_id    = AccountInfoInteger(ACCOUNT_LOGIN);
   string broker    = AccountInfoString(ACCOUNT_COMPANY);
   string server    = AccountInfoString(ACCOUNT_SERVER);
   string currency  = AccountInfoString(ACCOUNT_CURRENCY);
   double balance   = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity    = AccountInfoDouble(ACCOUNT_EQUITY);
   double margin    = AccountInfoDouble(ACCOUNT_MARGIN);
   double free_mgn  = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double mgn_level = AccountInfoDouble(ACCOUNT_MARGIN_LEVEL);

   MqlTick tick;
   SymbolInfoTick(g_symbol, tick);
   double spread_pts = (tick.ask - tick.bid) / SymbolInfoDouble(g_symbol, SYMBOL_POINT);

   string pos_json = BuildOpenPositionsJson();

   string payload = StringFormat(
      "{\"account_id\":%I64d,\"broker\":\"%s\",\"server\":\"%s\",\"currency\":\"%s\",\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,\"free_margin\":%.2f,\"margin_level_pct\":%.2f,\"spread_points\":%.1f,\"symbol\":\"%s\",\"open_positions\":%s}",
      acc_id, broker, server, currency, balance, equity, margin, free_mgn, mgn_level, spread_pts, g_symbol, pos_json
   );

   string resp = "";
   if(SendHttpRequest("POST", "/api/ea/heartbeat", payload, resp))
   {
      g_auto_trading_enabled = JsonExtractBool(resp, "auto_trading_enabled", g_auto_trading_enabled);
   }
}

//+------------------------------------------------------------------+
//| Send Execution Confirmation Back to Server                       |
//+------------------------------------------------------------------+
void SendExecutionReport(
   const string action_id,
   const string signal_id,
   const string status,
   ulong ticket,
   const string side,
   double lot,
   double fill_price,
   double slippage_pts,
   double sl,
   double tp,
   double profit,
   const string comment,
   uint error_code = 0,
   const string error_msg = ""
)
{
   string payload = StringFormat(
      "{\"action_id\":\"%s\",\"signal_id\":\"%s\",\"status\":\"%s\",\"ticket\":%I64u,\"symbol\":\"%s\",\"side\":\"%s\",\"lot\":%.2f,\"fill_price\":%.2f,\"slippage_points\":%.1f,\"sl\":%.2f,\"tp\":%.2f,\"profit\":%.2f,\"comment\":\"%s\",\"error_code\":%u,\"error_msg\":\"%s\"}",
      action_id, signal_id, status, ticket, g_symbol, side, lot, fill_price, slippage_pts, sl, tp, profit, comment, error_code, error_msg
   );

   string resp = "";
   SendHttpRequest("POST", "/api/ea/execution", payload, resp);
}

//+------------------------------------------------------------------+
//| Execute OPEN_ORDER Action from KAIROS-79                         |
//+------------------------------------------------------------------+
void ExecuteOpenOrder(const string action_json)
{
   string action_id   = JsonExtractString(action_json, "action_id");
   string signal_id   = JsonExtractString(action_json, "signal_id");
   string side        = JsonExtractString(action_json, "side");
   string order_type  = JsonExtractString(action_json, "order_type");
   double lot         = JsonExtractDouble(action_json, "lot", 0.01);
   double price       = JsonExtractDouble(action_json, "price", 0.0);
   double sl          = JsonExtractDouble(action_json, "sl", 0.0);
   double tp          = JsonExtractDouble(action_json, "tp", 0.0);
   string comment     = JsonExtractString(action_json, "comment");
   if(comment == "") comment = "KAIROS-79";

   // Safety Check: Spread Filter
   MqlTick tick;
   if(!SymbolInfoTick(g_symbol, tick))
   {
      SendExecutionReport(action_id, signal_id, "FAILED", 0, side, lot, 0, 0, sl, tp, 0, comment, 10001, "Cannot get market tick");
      return;
   }
   double current_spread_pts = (tick.ask - tick.bid) / SymbolInfoDouble(g_symbol, SYMBOL_POINT);
   if(current_spread_pts > InpMaxSpreadPoints)
   {
      PrintFormat("🛡️ Order rejected: Spread %.1f points exceeds max allowed %.1f points", current_spread_pts, InpMaxSpreadPoints);
      SendExecutionReport(action_id, signal_id, "REJECTED_SPREAD", 0, side, lot, 0, 0, sl, tp, 0, comment, 10002, "Spread exceeded limit");
      return;
   }

   // Set Slippage and Magic
   m_trade.SetDeviationInPoints(InpMaxSlippagePoints);
   m_trade.SetExpertMagicNumber(InpMagicNumber);

   bool ok = false;
   ulong ticket = 0;
   double fill_p = 0.0;
   double slip_pts = 0.0;

   // 1. Market Order Execution
   if(order_type == "MARKET" || price <= 0.0 || MathAbs(price - (side == "BUY" ? tick.ask : tick.bid)) <= 0.50)
   {
      if(side == "BUY")
      {
         ok = m_trade.Buy(lot, g_symbol, 0.0, sl, tp, comment);
         fill_p = m_trade.ResultPrice();
         slip_pts = (fill_p > 0 && tick.ask > 0) ? (fill_p - tick.ask) / SymbolInfoDouble(g_symbol, SYMBOL_POINT) : 0.0;
      }
      else
      {
         ok = m_trade.Sell(lot, g_symbol, 0.0, sl, tp, comment);
         fill_p = m_trade.ResultPrice();
         slip_pts = (fill_p > 0 && tick.bid > 0) ? (tick.bid - fill_p) / SymbolInfoDouble(g_symbol, SYMBOL_POINT) : 0.0;
      }
   }
   // 2. Pending Limit Order Execution
   else
   {
      if(side == "BUY")
      {
         ok = m_trade.BuyLimit(lot, price, g_symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
         fill_p = price;
      }
      else
      {
         ok = m_trade.SellLimit(lot, price, g_symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
         fill_p = price;
      }
   }

   if(ok)
   {
      ticket = m_trade.ResultOrder();
      PrintFormat("✅ KAIROS-79 Order Sent: %s %.2f %s @ %.2f (Ticket #%I64u)", side, lot, g_symbol, fill_p, ticket);
      SendExecutionReport(action_id, signal_id, "FILLED", ticket, side, lot, fill_p, slip_pts, sl, tp, 0, comment, 0, "OK");
   }
   else
   {
      uint err = m_trade.ResultRetcode();
      string desc = m_trade.ResultRetcodeDescription();
      PrintFormat("❌ KAIROS-79 Order Failed: %d (%s)", err, desc);
      SendExecutionReport(action_id, signal_id, "FAILED", 0, side, lot, 0, 0, sl, tp, 0, comment, err, desc);
   }
}

//+------------------------------------------------------------------+
//| Execute CLOSE_ORDER Action                                       |
//+------------------------------------------------------------------+
void ExecuteCloseOrder(const string action_json)
{
   string action_id = JsonExtractString(action_json, "action_id");
   ulong ticket     = (ulong)JsonExtractLong(action_json, "ticket", 0);

   if(ticket <= 0) return;

   bool ok = false;
   if(m_position.SelectByTicket(ticket))
   {
      double profit = m_position.Profit() + m_position.Swap();
      ok = m_trade.PositionClose(ticket, InpMaxSlippagePoints);
      if(ok)
      {
         PrintFormat("🏁 KAIROS-79 Position #%I64u Closed. Profit: $%.2f", ticket, profit);
         SendExecutionReport(action_id, "", "CLOSED", ticket, "", 0, 0, 0, 0, 0, profit, "MANUAL_CLOSE", 0, "OK");
      }
      else
      {
         SendExecutionReport(action_id, "", "CLOSE_FAILED", ticket, "", 0, 0, 0, 0, 0, 0, "FAIL", m_trade.ResultRetcode(), m_trade.ResultRetcodeDescription());
      }
   }
   // Maybe it's a pending limit order
   else if(m_order.Select(ticket))
   {
      ok = m_trade.OrderDelete(ticket);
      SendExecutionReport(action_id, "", (ok ? "CANCELLED" : "CANCEL_FAILED"), ticket, "", 0, 0, 0, 0, 0, 0, "CANCEL", 0, "");
   }
}

//+------------------------------------------------------------------+
//| Execute CLOSE_ALL Action                                         |
//+------------------------------------------------------------------+
void ExecuteCloseAll(const string action_json)
{
   string action_id = JsonExtractString(action_json, "action_id");
   int closed = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if(m_position.SelectByTicket(ticket))
      {
         if(m_position.Magic() == InpMagicNumber || InpMagicNumber == 0)
         {
            if(m_trade.PositionClose(ticket, InpMaxSlippagePoints))
               closed++;
         }
      }
   }

   // Also delete all pending orders
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket <= 0) continue;
      if(m_order.Select(ticket))
      {
         if(m_order.Magic() == InpMagicNumber || InpMagicNumber == 0)
         {
            m_trade.OrderDelete(ticket);
         }
      }
   }

   PrintFormat("🚨 KAIROS-79 Closed All (%d positions closed)", closed);
   SendExecutionReport(action_id, "", "CLOSED_ALL", 0, "", 0, 0, 0, 0, 0, 0, "CLOSE_ALL", 0, "OK");
}

//+------------------------------------------------------------------+
//| Execute MODIFY_ORDER Action (Breakeven / SL-TP adjustment)       |
//+------------------------------------------------------------------+
void ExecuteModifyOrder(const string action_json)
{
   string action_id = JsonExtractString(action_json, "action_id");
   ulong ticket     = (ulong)JsonExtractLong(action_json, "ticket", 0);
   double new_sl    = JsonExtractDouble(action_json, "new_sl", 0.0);
   double new_tp    = JsonExtractDouble(action_json, "new_tp", 0.0);

   if(ticket <= 0) return;

   if(m_position.SelectByTicket(ticket))
   {
      if(new_sl <= 0.0) new_sl = m_position.StopLoss();
      if(new_tp <= 0.0) new_tp = m_position.TakeProfit();

      bool ok = m_trade.PositionModify(ticket, new_sl, new_tp);
      if(ok)
      {
         PrintFormat("🛡️ Position #%I64u Modified: SL %.2f, TP %.2f", ticket, new_sl, new_tp);
         SendExecutionReport(action_id, "", "MODIFIED", ticket, "", 0, 0, 0, new_sl, new_tp, 0, "OK", 0, "OK");
      }
   }
}

//+------------------------------------------------------------------+
//| Poll Actions from KAIROS-79 Server                               |
//+------------------------------------------------------------------+
void PollServerActions()
{
   string resp = "";
   if(!SendHttpRequest("GET", "/api/ea/poll", "", resp))
      return;

   // Update auto trading flag
   g_auto_trading_enabled = JsonExtractBool(resp, "auto_trading_enabled", g_auto_trading_enabled);

   // Check if actions exist
   int act_pos = StringFind(resp, "\"actions\":[");
   if(act_pos < 0) return;

   // Find individual action objects
   int search_pos = act_pos + 10;
   while(search_pos < StringLen(resp))
   {
      int start_obj = StringFind(resp, "{", search_pos);
      if(start_obj < 0) break;
      int end_obj = StringFind(resp, "}", start_obj);
      if(end_obj < 0) break;

      string action_json = StringSubstr(resp, start_obj, end_obj - start_obj + 1);
      string act_type = JsonExtractString(action_json, "type");

      PrintFormat("📥 KAIROS-79 Action Received: %s (Auto: %s)", act_type, (g_auto_trading_enabled ? "ON" : "OFF"));

      if(act_type == "OPEN_ORDER")
      {
         if(g_auto_trading_enabled)
            ExecuteOpenOrder(action_json);
         else
            Print("⚠️ OPEN_ORDER ignored: Auto-Trading is OFF on Web Dashboard");
      }
      else if(act_type == "CLOSE_ORDER")
      {
         ExecuteCloseOrder(action_json);
      }
      else if(act_type == "CLOSE_ALL")
      {
         ExecuteCloseAll(action_json);
      }
      else if(act_type == "MODIFY_ORDER")
      {
         ExecuteModifyOrder(action_json);
      }

      search_pos = end_obj + 1;
   }
}

//+------------------------------------------------------------------+
//| Update Chart Visual HUD Info                                     |
//+------------------------------------------------------------------+
void UpdateChartHUD()
{
   string conn_text = g_server_online ? "🟢 CONNECTED (Online)" : "🔴 DISCONNECTED (Check Server)";
   string auto_text = g_auto_trading_enabled ? "🟢 AUTO-TRADING: ON" : "⏸️ AUTO-TRADING: OFF";

   string hud = "========================================\n"
              + "  🤖 KAIROS-79 EA Bridge v1.00\n"
              + "========================================\n"
              + "  Server URL:    " + InpServerUrl + "\n"
              + "  Status:        " + conn_text + "\n"
              + "  Web Control:   " + auto_text + "\n"
              + "  Magic Number:  " + IntegerToString(InpMagicNumber) + "\n"
              + "  Symbol:        " + g_symbol + "\n"
              + "  Open Positions: " + IntegerToString(PositionsTotal()) + "\n"
              + "========================================";

   Comment(hud);
}

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   g_symbol = (InpSymbolOverride != "") ? InpSymbolOverride : _Symbol;

   // Setup CTrade
   m_trade.SetExpertMagicNumber(InpMagicNumber);
   m_trade.SetDeviationInPoints(InpMaxSlippagePoints);

   if(InpAutoDetectFilling)
      SetupFillingMode(g_symbol);

   Print("=======================================================");
   Print("🚀 Starting KAIROS-79 MT5 Bridge EA on ", g_symbol);
   Print("🔗 Server URL: ", InpServerUrl);
   Print("🛡️ Magic Number: ", InpMagicNumber);
   Print("=======================================================");

   // Initial heartbeat & check
   SendHeartbeat();
   UpdateChartHUD();

   // Set high-frequency timer for low latency execution
   EventSetMillisecondTimer(InpPollIntervalMs);

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   Comment("");
   Print("🛑 KAIROS-79 MT5 Bridge EA Stopped (Reason: ", reason, ")");
}

//+------------------------------------------------------------------+
//| Expert timer function                                            |
//+------------------------------------------------------------------+
void OnTimer()
{
   datetime now = TimeCurrent();

   // 1. Send Heartbeat Telemetry every N seconds
   if(now - g_last_heartbeat_time >= InpHeartbeatIntervalSec)
   {
      SendHeartbeat();
      g_last_heartbeat_time = now;
      UpdateChartHUD();
   }

   // 2. Poll pending actions from KAIROS-79
   PollServerActions();
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
   // On tick we can also poll or update HUD if needed
}
//+------------------------------------------------------------------+
