//+------------------------------------------------------------------+
//|                                           KAIROS79_Bridge.mq5   |
//|                             Copyright 2026, KAIROS-79 AI Team   |
//|                       https://github.com/ThanarakCSW/KAIROS-79   |
//+------------------------------------------------------------------+
#property copyright   "Copyright 2026, KAIROS-79 AI Team"
#property link        "https://github.com/ThanarakCSW/KAIROS-79"
#property version     "1.08"
#property description "KAIROS-79 MetaTrader 5 Bridge Expert Advisor (EA) v1.08 (Ultra-Fast 100ms & Multi-Strategy)"
#property description "Connects MT5 on Windows/VPS directly to KAIROS-79 AI Web Platform with Multi-Symbol Support."
#property description "Supports Auto ON/OFF, Hard SL, Daily Kill Switch, OnTradeTransaction Deal Sync, and Step Trailing."
#property strict

#define EA_VERSION "1.08"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\OrderInfo.mqh>

//--- EA Inputs
input group "=== KAIROS-79 Server Connection ==="
input string   InpServerUrl         = "http://127.0.0.1:8000"; // Server URL (or VPS Public IP)
input int      InpPollIntervalMs    = 100;                     // Command Poll Interval (ms) - Ultra-Fast 100ms
input int      InpHeartbeatIntervalSec = 1;                    // Heartbeat Interval (seconds)

input group "=== Trading Safety & Execution ==="
input ulong    InpMagicNumber       = 797979;                  // Magic Number for KAIROS-79
input ulong    InpMaxSlippagePoints = 35;                      // Legacy Slippage Fallback (points)
input double   InpMaxSpreadPoints   = 150.0;                   // Legacy Spread Fallback (points)
input string   InpSymbolOverride    = "";                      // Symbol (blank = Chart Symbol)
input bool     InpAutoDetectFilling = true;                    // Auto detect FOK/IOC/Return

input group "=== Institutional Risk & Protection Guards ==="
input double   InpMaxSpreadUsd         = 0.80;   // Max Allowed Spread in USD ($0.80)
input double   InpMaxSlippageUsd       = 0.50;   // Max Allowed Slippage in USD ($0.50)
input double   InpMaxMarketDevUsd      = 3.00;   // Max Market Price Deviation from Signal ($ USD)
input double   InpMaxDailyLossPct      = 0.0;    // Disabled (0.0) — Single Loss Limit controlled by Dashboard Portfolio Stop Floor
input double   InpMinDailyLossUsdFloor = 0.0;    // Unused when InpMaxDailyLossPct = 0.0
input double   InpMaxLotPerOrder       = 2.00;   // Max Lot Cap per Single Order
input int      InpMaxOpenPositions     = 10;     // Max Concurrent Open Positions (Supports Rapid-Fire Test & Scaling)
input bool     InpEnforceHardSL        = true;   // Enforce Hard SL on Every Position (Naked Guard)
input double   InpEmergencySLUsd       = 6.00;   // Emergency SL Distance ($ USD) if Naked
input bool     InpEnableLocalBreakEven = true;   // Auto Local Break-Even on Tick
input double   InpBreakEvenTriggerUsd  = 3.50;   // Profit Distance ($ USD) to Trigger Local BE
input double   InpBreakEvenLockUsd     = 0.25;   // Lock Profit ($ USD) at Break-Even
input bool     InpEnableRolloverGuard  = true;   // Block New Orders During Rollover Window
input int      InpRolloverStartHour    = 23;     // Rollover Guard Start Hour (Server Time)
input int      InpRolloverStartMin     = 50;     // Rollover Guard Start Minute
input int      InpRolloverEndHour      = 0;      // Rollover Guard End Hour (Server Time)
input int      InpRolloverEndMin       = 20;     // Rollover Guard End Minute

//--- Global Objects
CTrade         m_trade;
CPositionInfo  m_position;
COrderInfo     m_order;

string         g_symbol;
string         g_server_url = "http://127.0.0.1:8000";
ulong          g_last_heartbeat_ms = 0;
bool           g_server_online = false;
bool           g_auto_trading_enabled = false;
int            g_consecutive_errors = 0;
int            g_last_error_code = 0;
int            g_last_http_code = 0;
string         g_last_error_desc = "Initializing connection...";
ulong          g_last_success_ms = 0;
long           g_last_server_time = 0;
int            g_next_poll_ms = 100;
ulong          g_last_poll_ms = 0;

string         g_processed_action_ids[100];
int            g_processed_idx = 0;
ulong          g_processed_deal_ids[100];
int            g_processed_deal_idx = 0;
int            g_current_day_of_year = -1;
double         g_day_start_equity = 0.0;
bool           g_daily_kill_switch_tripped = false;
double         g_current_daily_dd_pct = 0.0;
ulong          g_last_tick_guard_ms = 0;

bool IsActionProcessed(const string act_id)
{
   if(act_id == "") return false;
   for(int i = 0; i < 100; i++)
   {
      if(g_processed_action_ids[i] == act_id)
         return true;
   }
   return false;
}

void MarkActionProcessed(const string act_id)
{
   if(act_id == "") return;
   g_processed_action_ids[g_processed_idx] = act_id;
   g_processed_idx = (g_processed_idx + 1) % 100;
}

bool IsDealProcessed(ulong deal_id)
{
   if(deal_id <= 0) return false;
   for(int i = 0; i < 100; i++)
   {
      if(g_processed_deal_ids[i] == deal_id)
         return true;
   }
   return false;
}

void MarkDealProcessed(ulong deal_id)
{
   if(deal_id <= 0) return;
   g_processed_deal_ids[g_processed_deal_idx] = deal_id;
   g_processed_deal_idx = (g_processed_deal_idx + 1) % 100;
}

bool IsInRolloverWindow()
{
   MqlDateTime dt;
   if(!TimeCurrent(dt)) return false;
   int now_mins = dt.hour * 60 + dt.min;
   int start_mins = InpRolloverStartHour * 60 + InpRolloverStartMin;
   int end_mins = InpRolloverEndHour * 60 + InpRolloverEndMin;
   if(start_mins <= end_mins)
      return (now_mins >= start_mins && now_mins <= end_mins);
   return (now_mins >= start_mins || now_mins <= end_mins);
}

int CountMagicPositions()
{
   int cnt = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if(!m_position.SelectByTicket(ticket)) continue;
      if(InpMagicNumber == 0 || m_position.Magic() == InpMagicNumber)
         cnt++;
   }
   return cnt;
}

ulong GetSlippagePoints()
{
   double pt = SymbolInfoDouble(g_symbol, SYMBOL_POINT);
   if(pt <= 0.0) pt = _Point;
   if(pt <= 0.0) pt = 0.01;
   if(InpMaxSlippageUsd > 0.0)
      return (ulong)MathMax(5, (int)MathRound(InpMaxSlippageUsd / pt));
   return InpMaxSlippagePoints;
}

//+------------------------------------------------------------------+
//| Sanitize and Normalize Server URL                                |
//+------------------------------------------------------------------+
string SanitizeServerUrl(const string raw_url)
{
   string url = raw_url;
   StringTrimLeft(url);
   StringTrimRight(url);

   // Prepend http:// if missing protocol
   if(StringFind(url, "http://") != 0 && StringFind(url, "https://") != 0)
   {
      url = "http://" + url;
   }

   // Strip trailing slashes
   while(StringLen(url) > 0 && StringSubstr(url, StringLen(url) - 1, 1) == "/")
   {
      url = StringSubstr(url, 0, StringLen(url) - 1);
   }

   return url;
}

//+------------------------------------------------------------------+
//| Helper: Extract string value from JSON (Whitespace & Format Robust)
//+------------------------------------------------------------------+
string JsonExtractString(const string json, const string key)
{
   string key_search = "\"" + key + "\"";
   int kpos = StringFind(json, key_search);
   if(kpos < 0) return "";

   int cpos = StringFind(json, ":", kpos + StringLen(key_search));
   if(cpos < 0) return "";

   int vpos = cpos + 1;
   int len = StringLen(json);
   // Skip any whitespace after colon
   while(vpos < len)
   {
      ushort ch = StringGetCharacter(json, vpos);
      if(ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n')
         vpos++;
      else
         break;
   }
   if(vpos >= len) return "";

   // Check if quoted string value
   if(StringGetCharacter(json, vpos) == '\"')
   {
      vpos++; // skip opening quote
      int end_pos = StringFind(json, "\"", vpos);
      if(end_pos >= vpos)
         return StringSubstr(json, vpos, end_pos - vpos);
      return "";
   }

   // Unquoted value (number, bool, null)
   int end_pos = vpos;
   while(end_pos < len)
   {
      ushort ch = StringGetCharacter(json, end_pos);
      if(ch == ',' || ch == '}' || ch == ']' || ch == '\r' || ch == '\n' || ch == ' ')
         break;
      end_pos++;
   }
   string val = StringSubstr(json, vpos, end_pos - vpos);
   StringTrimLeft(val);
   StringTrimRight(val);
   return val;
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
   string url = g_server_url + endpoint;
   char post_bytes[];
   char result_bytes[];
   string result_headers;
   int timeout_ms = 5000;

   if(StringLen(post_data) > 0)
      StringToCharArray(post_data, post_bytes, 0, StringLen(post_data), CP_UTF8);

   string headers = "Content-Type: application/json; charset=utf-8\r\n";
   ResetLastError();

   int res = WebRequest(method, url, headers, timeout_ms, post_bytes, result_bytes, result_headers);
   if(res == -1)
   {
      int err = GetLastError();
      g_consecutive_errors++;
      g_server_online = false;
      g_last_error_code = err;
      g_last_http_code = 0;

      if(err == 4014) // ERR_FUNCTION_NOT_ALLOWED
      {
         g_last_error_desc = "Blocked by MT5 (Error 4014)";
         if(g_consecutive_errors <= 3 || (g_consecutive_errors % 20 == 0))
         {
            Print("❌ WebRequest Error (4014): Blocked by MT5 WebRequest Whitelist!");
            Print("   👉 Step 1: MT5 Menu -> Tools (เครื่องมือ) -> Options (ตัวเลือก)");
            Print("   👉 Step 2: Tab 'Expert Advisors'");
            Print("   👉 Step 3: Check [✓] 'Allow WebRequest for listed URL'");
            PrintFormat("   👉 Step 4: Add URL: %s", g_server_url);
            Print("   👉 Step 5: Add URL: http://localhost:8000");
         }
      }
      else if(err == 5203) // ERR_NETSOCKET_FAILED / Connection refused
      {
         if(StringFind(g_server_url, ":8080") >= 0)
            g_last_error_desc = "Port 8080 Refused (Press F7 -> change to :8000)";
         else
            g_last_error_desc = "Connection Refused (Error 5203)";

         if(g_consecutive_errors <= 3 || (g_consecutive_errors % 20 == 0))
         {
            PrintFormat("⚠️ WebRequest to %s failed (Error 5203: Connection Refused).", url);
            Print("   👉 Make sure Python server is running on Windows (run 'python app.py')");
            Print("   👉 Check that server port matches EA: " + g_server_url);
         }
      }
      else
      {
         g_last_error_desc = StringFormat("Network Error %d", err);
         if(g_consecutive_errors <= 3 || (g_consecutive_errors % 20 == 0))
         {
            PrintFormat("⚠️ WebRequest to %s failed (Error %d)", url, err);
         }
      }
      return false;
   }

   if(res < 200 || res >= 300)
   {
      g_consecutive_errors++;
      g_server_online = false;
      g_last_error_code = 0;
      g_last_http_code = res;
      g_last_error_desc = StringFormat("HTTP %d Error (%s)", res, endpoint);
      if(g_consecutive_errors <= 3 || (g_consecutive_errors % 20 == 0))
      {
         PrintFormat("⚠️ WebRequest to %s returned HTTP %d", url, res);
      }
      return false;
   }

   // Request Succeeded
   g_consecutive_errors = 0;
   g_server_online = true;
   g_last_error_code = 0;
   g_last_http_code = res;
   g_last_error_desc = "Online (Synchronized)";
   g_last_success_ms = GetTickCount64();
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
//| Build Pending Orders JSON Array                                  |
//+------------------------------------------------------------------+
string BuildPendingOrdersJson()
{
   string json = "[";
   int count = 0;

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket <= 0) continue;
      if(!m_order.Select(ticket)) continue;

      if(m_order.Magic() == InpMagicNumber || InpMagicNumber == 0)
      {
         if(count > 0) json += ",";
         json += StringFormat(
            "{\"ticket\":%I64u,\"symbol\":\"%s\",\"type\":\"%s\",\"lots\":%.2f,\"open_price\":%.2f,\"sl\":%.2f,\"tp\":%.2f,\"comment\":\"%s\"}",
            ticket,
            m_order.Symbol(),
            EnumToString(m_order.OrderType()),
            m_order.VolumeInitial(),
            m_order.PriceOpen(),
            m_order.StopLoss(),
            m_order.TakeProfit(),
            m_order.Comment()
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
   double pt = SymbolInfoDouble(g_symbol, SYMBOL_POINT);
   if(pt <= 0.0) pt = _Point;
   if(pt <= 0.0) pt = 0.01;
   double spread_pts = 0.0;
   if(SymbolInfoTick(g_symbol, tick))
   {
      if(tick.ask > tick.bid && pt > 0.0)
         spread_pts = (tick.ask - tick.bid) / pt;
   }
   if(spread_pts < 0.0 || spread_pts > 9999.0) spread_pts = 0.0;

   string pos_json = BuildOpenPositionsJson();
   string ord_json = BuildPendingOrdersJson();

   double cur_price = 0.0;
   if(tick.last > 0.0) cur_price = tick.last;
   else if(tick.bid > 0.0) cur_price = tick.bid;

   string ks_str = g_daily_kill_switch_tripped ? "true" : "false";
   string part1 = StringFormat(
      "{\"ea_version\":\"%s\",\"account_id\":%I64d,\"broker\":\"%s\",\"server\":\"%s\",\"currency\":\"%s\",\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,\"free_margin\":%.2f,\"margin_level_pct\":%.2f",
      EA_VERSION, acc_id, broker, server, currency, balance, equity, margin, free_mgn, mgn_level
   );
   string part2 = StringFormat(
      ",\"spread_points\":%.1f,\"symbol\":\"%s\",\"bid\":%.2f,\"ask\":%.2f,\"price\":%.2f,\"daily_dd_pct\":%.2f,\"kill_switch_tripped\":%s,\"open_positions\":%s,\"pending_orders\":%s}",
      spread_pts, g_symbol, tick.bid, tick.ask, cur_price, g_current_daily_dd_pct, ks_str, pos_json, ord_json
   );
   string payload = part1 + part2;

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
   const string error_msg = "",
   const string order_type = "",
   const string symbol_name = ""
)
{
   string rep_sym = (symbol_name != "") ? symbol_name : g_symbol;
   int p_digits = (int)SymbolInfoInteger(rep_sym, SYMBOL_DIGITS);
   if(p_digits <= 0) p_digits = (StringFind(rep_sym, "EUR") >= 0 || StringFind(rep_sym, "GBP") >= 0) ? 5 : 2;

   string part1 = StringFormat(
      "{\"action_id\":\"%s\",\"signal_id\":\"%s\",\"status\":\"%s\",\"ticket\":%I64u,\"symbol\":\"%s\",\"side\":\"%s\",\"order_type\":\"%s\",\"lot\":%.2f",
      action_id, signal_id, status, ticket, rep_sym, side, order_type, lot
   );
   string part2 = StringFormat(
      ",\"fill_price\":%.*f,\"slippage_points\":%.1f,\"sl\":%.*f,\"tp\":%.*f,\"profit\":%.2f,\"comment\":\"%s\",\"error_code\":%u,\"error_msg\":\"%s\"}",
      p_digits, fill_price, slippage_pts, p_digits, sl, p_digits, tp, profit, comment, error_code, error_msg
   );
   string payload = part1 + part2;

   string resp = "";
   SendHttpRequest("POST", "/api/ea/execution", payload, resp);
}

//+------------------------------------------------------------------+
//| Send Real Deal Exit Report from OnTradeTransaction               |
//+------------------------------------------------------------------+
void SendDealExecutionReport(
   ulong deal,
   ulong position_id,
   const string sym,
   const string side,
   double lot,
   double fill_price,
   double profit,
   double swap,
   double commission,
   const string comment,
   const string deal_reason_str,
   bool is_history_sync = false
)
{
   long deal_time = 0;
   if(deal > 0 && HistoryDealSelect(deal))
   {
      deal_time = HistoryDealGetInteger(deal, DEAL_TIME);
      if(deal_time > 0 && (TimeCurrent() - (datetime)deal_time) > 15)
         is_history_sync = true;
   }

   string act_prefix = is_history_sync ? "HIST" : "DEAL";
   string payload = StringFormat(
      "{\"action_id\":\"%s-%I64u\",\"signal_id\":\"\",\"status\":\"DEAL_CLOSED\",\"is_history_sync\":%s,\"deal\":%I64u,\"ticket\":%I64u,\"symbol\":\"%s\",\"side\":\"%s\",\"order_type\":\"DEAL\",\"lot\":%.2f,\"fill_price\":%.2f,\"profit\":%.2f,\"swap\":%.2f,\"commission\":%.2f,\"comment\":\"%s\",\"close_reason\":\"%s\",\"deal_time\":%I64d,\"error_code\":0,\"error_msg\":\"OK\"}",
      act_prefix, deal, (is_history_sync ? "true" : "false"), deal, position_id, sym, side, lot, fill_price, profit, swap, commission, comment, deal_reason_str, deal_time
   );

   string resp = "";
   SendHttpRequest("POST", "/api/ea/execution", payload, resp);
}

//+------------------------------------------------------------------+
//| Sync Historical Real Deals from MT5 to KAIROS-79 Server          |
//+------------------------------------------------------------------+
void SyncHistoryDeals()
{
   datetime from_time = (datetime)MathMax(0, (long)TimeCurrent() - 86400 * 2); // past 48 hours
   if(!HistorySelect(from_time, TimeCurrent())) return;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong deal = HistoryDealGetTicket(i);
      if(deal <= 0 || IsDealProcessed(deal)) continue;
      long deal_entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
      long deal_magic = HistoryDealGetInteger(deal, DEAL_MAGIC);
      if(InpMagicNumber != 0 && deal_magic != InpMagicNumber) continue;
      string sym = HistoryDealGetString(deal, DEAL_SYMBOL);
      if(sym != "" && sym != g_symbol) continue;
      if(deal_entry == DEAL_ENTRY_OUT || deal_entry == DEAL_ENTRY_OUT_BY)
      {
         MarkDealProcessed(deal);
         ulong pos_id = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
         double profit = HistoryDealGetDouble(deal, DEAL_PROFIT);
         double swap = HistoryDealGetDouble(deal, DEAL_SWAP);
         double comm = HistoryDealGetDouble(deal, DEAL_COMMISSION);
         double price = HistoryDealGetDouble(deal, DEAL_PRICE);
         double vol = HistoryDealGetDouble(deal, DEAL_VOLUME);
         long deal_type = HistoryDealGetInteger(deal, DEAL_TYPE);
         long deal_reason = HistoryDealGetInteger(deal, DEAL_REASON);
         string comment = HistoryDealGetString(deal, DEAL_COMMENT);
         string reason_str = "DEAL_CLOSE";
         if(deal_reason == DEAL_REASON_SL) reason_str = "DEAL_SL";
         else if(deal_reason == DEAL_REASON_TP) reason_str = "DEAL_TP";
         else if(deal_reason == DEAL_REASON_CLIENT) reason_str = "DEAL_MANUAL";
         string side_str = (deal_type == DEAL_TYPE_BUY) ? "BUY" : ((deal_type == DEAL_TYPE_SELL) ? "SELL" : "");
         SendDealExecutionReport(deal, pos_id, sym, side_str, vol, price, profit, swap, comm, comment, reason_str, true);
      }
   }
}

//+------------------------------------------------------------------+
//| Helper: Normalize lot size according to broker constraints       |
//+------------------------------------------------------------------+
double NormalizeLot(double lot, const string sym)
{
   double min_lot  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double max_lot  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double step_lot = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   if(min_lot <= 0.0) min_lot = 0.01;
   if(max_lot <= 0.0) max_lot = 100.0;
   if(step_lot <= 0.0) step_lot = 0.01;
   if(lot < min_lot) lot = min_lot;
   if(lot > max_lot) lot = max_lot;
   lot = MathFloor((lot - min_lot) / step_lot + 0.00001) * step_lot + min_lot;
   return NormalizeDouble(lot, 2);
}

ulong ResolveOpenedTicket(ulong raw_order, ulong raw_deal, bool is_market)
{
   if(!is_market) return raw_order;
   if(raw_deal > 0 && HistoryDealSelect(raw_deal))
   {
      ulong pos_id = (ulong)HistoryDealGetInteger(raw_deal, DEAL_POSITION_ID);
      if(pos_id > 0) return pos_id;
   }
   return raw_order;
}

//+------------------------------------------------------------------+
//| Resolve Target Symbol from Request or Broker/Chart Mapping       |
//+------------------------------------------------------------------+
string ResolveTargetSymbol(const string sym_req)
{
   if(sym_req == "" || sym_req == g_symbol)
      return g_symbol;

   // 1. Direct check: can we get a tick on the requested symbol directly?
   if(SymbolInfoInteger(sym_req, SYMBOL_SELECT) || SymbolSelect(sym_req, true))
   {
      MqlTick test_tick;
      if(SymbolInfoTick(sym_req, test_tick) && test_tick.ask > 0.0 && test_tick.bid > 0.0)
         return sym_req;
   }

   string req_u = sym_req;
   StringToUpper(req_u);
   string g_u = g_symbol;
   StringToUpper(g_u);

   // 2. Gold alias mapping (XAUUSD <-> GOLD / GOLDmicro / XAUUSD# etc.)
   bool req_is_gold = (StringFind(req_u, "XAU") >= 0 || StringFind(req_u, "GOLD") >= 0);
   bool chart_is_gold = (StringFind(g_u, "XAU") >= 0 || StringFind(g_u, "GOLD") >= 0);
   if(req_is_gold && chart_is_gold)
   {
      PrintFormat("ℹ️ Mapping requested Gold symbol '%s' to chart symbol '%s'", sym_req, g_symbol);
      return g_symbol;
   }

   // 3. Bitcoin alias mapping (BTCUSD <-> BITCOIN / BTCUSD# etc.)
   bool req_is_btc = (StringFind(req_u, "BTC") >= 0 || StringFind(req_u, "BITCOIN") >= 0);
   bool chart_is_btc = (StringFind(g_u, "BTC") >= 0 || StringFind(g_u, "BITCOIN") >= 0);
   if(req_is_btc && chart_is_btc)
   {
      PrintFormat("ℹ️ Mapping requested BTC symbol '%s' to chart symbol '%s'", sym_req, g_symbol);
      return g_symbol;
   }

   // 4. Fallback to chart symbol if chart has market tick
   MqlTick chart_tick;
   if(SymbolInfoTick(g_symbol, chart_tick))
   {
      PrintFormat("⚠️ Requested symbol '%s' not tradable on broker, falling back to chart symbol '%s'", sym_req, g_symbol);
      return g_symbol;
   }

   return sym_req;
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
   string sym_req     = JsonExtractString(action_json, "symbol");
   string target_sym  = ResolveTargetSymbol(sym_req);
   double lot         = JsonExtractDouble(action_json, "lot", 0.01);
   double price       = JsonExtractDouble(action_json, "price", 0.0);
   double sl          = JsonExtractDouble(action_json, "sl", 0.0);
   double tp          = JsonExtractDouble(action_json, "tp", 0.0);
   long   created_at  = JsonExtractLong(action_json, "created_at", 0);
   bool   is_manual   = JsonExtractBool(action_json, "manual", false);
   bool   allow_mkt_fallback = JsonExtractBool(action_json, "allow_market_fallback", false);
   double max_dev_usd = JsonExtractDouble(action_json, "max_deviation_usd", InpMaxMarketDevUsd);
   string comment     = JsonExtractString(action_json, "comment");
   if(comment == "") comment = "KAIROS-79";

   if(!SymbolInfoInteger(target_sym, SYMBOL_SELECT))
      SymbolSelect(target_sym, true);

   // Deduplication Guard: never process same action_id twice
   if(IsActionProcessed(action_id))
   {
      PrintFormat("⏭️ Skipping duplicate ActionID=%s", action_id);
      return;
   }
   MarkActionProcessed(action_id);

   PrintFormat("🚀 KAIROS-79 Processing OPEN_ORDER [%s]: Side=%s, Type=%s, Lot=%.2f, Price=%.2f, SL=%.2f, TP=%.2f (ActionID=%s)",
      target_sym, side, order_type, lot, price, sl, tp, action_id);

   // 0. Safety Check: Algo Trading must be enabled in MT5
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      Print("❌ MT5 Algo Trading is DISABLED! Please click the 'Algo Trading' button in MT5 toolbar to make it GREEN.");
      SendExecutionReport(action_id, signal_id, "FAILED_ALGO_DISABLED", 0, side, lot, 0, 0, sl, tp, 0, comment, 10027, "Algo Trading button is disabled in MT5", order_type, target_sym);
      return;
   }

   // 0.1 Daily Drawdown Kill Switch Check
   if(g_daily_kill_switch_tripped)
   {
      Print("🚨 Order rejected: Daily Drawdown Kill Switch is TRIPPED!");
      SendExecutionReport(action_id, signal_id, "REJECTED_DAILY_KILL_SWITCH", 0, side, lot, 0, 0, sl, tp, 0, comment, 10090, "Daily loss kill switch tripped", order_type, target_sym);
      return;
   }

   // 0.2 Rollover Window Guard (skip manual test orders only if desired? No, protect all unless manual test? Let's guard non-manual, or all auto)
   if(!is_manual && InpEnableRolloverGuard && IsInRolloverWindow())
   {
      Print("🛡️ Order rejected: Inside broker rollover spread danger window");
      SendExecutionReport(action_id, signal_id, "REJECTED_ROLLOVER", 0, side, lot, 0, 0, sl, tp, 0, comment, 10091, "Rollover spread window", order_type, target_sym);
      return;
   }

   // 0.3 Stale Signal Expiry Check (ignore if server clock synced & > 15s old)
   if(!is_manual && created_at > 0 && g_last_server_time > 0 && (g_last_server_time - created_at) > 15)
   {
      PrintFormat("⏳ Order rejected: Action %s expired (%I64d sec old)", action_id, (g_last_server_time - created_at));
      SendExecutionReport(action_id, signal_id, "REJECTED_EXPIRED", 0, side, lot, 0, 0, sl, tp, 0, comment, 10092, "Order command expired", order_type, target_sym);
      return;
   }

   // 0.4 Max Open Positions Check
   if(InpMaxOpenPositions > 0 && CountMagicPositions() >= InpMaxOpenPositions)
   {
      PrintFormat("🛡️ Order rejected: Max open positions (%d) reached", InpMaxOpenPositions);
      SendExecutionReport(action_id, signal_id, "REJECTED_MAX_POSITIONS", 0, side, lot, 0, 0, sl, tp, 0, comment, 10093, "Max concurrent positions reached", order_type, target_sym);
      return;
   }

   // 1. Market Tick & USD Spread Filter
   MqlTick tick;
   if(!SymbolInfoTick(target_sym, tick))
   {
      SendExecutionReport(action_id, signal_id, "FAILED", 0, side, lot, 0, 0, sl, tp, 0, comment, 10001, "Cannot get market tick", order_type, target_sym);
      return;
   }
   double pt = SymbolInfoDouble(target_sym, SYMBOL_POINT);
   if(pt <= 0.0) pt = _Point;
   if(pt <= 0.0) pt = 0.01;
   double spread_usd = (tick.ask > tick.bid) ? (tick.ask - tick.bid) : 0.0;
   double current_spread_pts = (pt > 0.0) ? (spread_usd / pt) : 0.0;

   if(InpMaxSpreadUsd > 0.0 && spread_usd > InpMaxSpreadUsd)
   {
      PrintFormat("🛡️ Order rejected: Spread $%.2f (%.1f pts) exceeds max allowed $%.2f", spread_usd, current_spread_pts, InpMaxSpreadUsd);
      SendExecutionReport(action_id, signal_id, "REJECTED_SPREAD", 0, side, lot, 0, 0, sl, tp, 0, comment, 10002, StringFormat("Spread $%.2f exceeded limit $%.2f", spread_usd, InpMaxSpreadUsd), order_type, target_sym);
      return;
   }

   // Digits, Lot Cap & Normalization
   int digits = (int)SymbolInfoInteger(target_sym, SYMBOL_DIGITS);
   if(digits <= 0) digits = _Digits;
   if(digits <= 0) digits = 2;
   if(InpMaxLotPerOrder > 0.0 && lot > InpMaxLotPerOrder)
   {
      PrintFormat("⚠️ Lot %.2f capped to InpMaxLotPerOrder %.2f", lot, InpMaxLotPerOrder);
      lot = InpMaxLotPerOrder;
   }
   price = NormalizeDouble(price, digits);
   if(sl > 0.0) sl = NormalizeDouble(sl, digits);
   if(tp > 0.0) tp = NormalizeDouble(tp, digits);
   lot = NormalizeLot(lot, target_sym);

   // Set Slippage in USD -> Points, Magic and Filling
   ulong eff_slip_pts = GetSlippagePoints();
   m_trade.SetDeviationInPoints(eff_slip_pts);
   m_trade.SetExpertMagicNumber(InpMagicNumber);
   SetupFillingMode(target_sym);

   bool ok = false;
   ulong opened_ticket = 0;
   double fill_p = 0.0;
   double slip_pts = 0.0;

   // Check broker minimum stop distance for pending orders
   long stops_lvl = SymbolInfoInteger(target_sym, SYMBOL_TRADE_STOPS_LEVEL);
   long freeze_lvl = SymbolInfoInteger(target_sym, SYMBOL_TRADE_FREEZE_LEVEL);
   double broker_min_dist = MathMax((double)MathMax(stops_lvl, freeze_lvl) * pt, pt);
   double min_stop_dist = MathMax(broker_min_dist, spread_usd + 0.25);

   // Determine whether to execute as Market or Pending Limit
   bool execute_as_market = (order_type == "MARKET");
   if(order_type == "LIMIT")
   {
      execute_as_market = false;
      if(price <= 0.0)
      {
         price = (side == "BUY") ? NormalizeDouble(tick.ask - 2.0 * min_stop_dist, digits) : NormalizeDouble(tick.bid + 2.0 * min_stop_dist, digits);
      }
      // Ensure Limit order price strictly complies with MT5 pending order rule:
      // BUY LIMIT must be < Ask; SELL LIMIT must be > Bid.
      if(side == "BUY" && price >= (tick.ask - broker_min_dist))
      {
         if(allow_mkt_fallback)
         {
            PrintFormat("⚡ BUY Limit @ %.2f is at/above Ask (%.2f) -> Fallback to MARKET BUY", price, tick.ask);
            execute_as_market = true;
         }
         else
         {
            double adjusted_p = NormalizeDouble(tick.ask - broker_min_dist, digits);
            PrintFormat("ℹ️ BUY Limit price adjusted %.2f -> %.2f to place strictly as Limit (< Ask %.2f)", price, adjusted_p, tick.ask);
            price = adjusted_p;
         }
      }
      else if(side == "SELL" && price <= (tick.bid + broker_min_dist))
      {
         if(allow_mkt_fallback)
         {
            PrintFormat("⚡ SELL Limit @ %.2f is at/below Bid (%.2f) -> Fallback to MARKET SELL", price, tick.bid);
            execute_as_market = true;
         }
         else
         {
            double adjusted_p = NormalizeDouble(tick.bid + broker_min_dist, digits);
            PrintFormat("ℹ️ SELL Limit price adjusted %.2f -> %.2f to place strictly as Limit (> Bid %.2f)", price, adjusted_p, tick.bid);
            price = adjusted_p;
         }
      }
   }
   else if(price <= 0.0)
   {
      execute_as_market = true;
   }

   // Market Deviation Check for non-manual signals
   if(!is_manual && execute_as_market && price > 0.0 && max_dev_usd > 0.0)
   {
      double cur_mkt = (side == "BUY" ? tick.ask : tick.bid);
      if(MathAbs(cur_mkt - price) > max_dev_usd)
      {
         PrintFormat("⏳ Market order rejected: price moved %.2f -> %.2f (dev > $%.2f)", price, cur_mkt, max_dev_usd);
         SendExecutionReport(action_id, signal_id, "REJECTED_DEVIATION", 0, side, lot, 0, 0, sl, tp, 0, comment, 10094, "Market price deviated too far", "MARKET", target_sym);
         return;
      }
   }

   // Pre-validate & Enforce Hard Stop Loss with Spread Cushion (NEVER clamp SL to $0.01 away!)
   double ref_p = execute_as_market ? (side == "BUY" ? tick.bid : tick.ask) : price;
   double entry_ref = execute_as_market ? (side == "BUY" ? tick.ask : tick.bid) : price;
   double intended_sl_dist = (price > 0.0 && sl > 0.0) ? MathAbs(price - sl) : 0.0;
   double intended_tp_dist = (price > 0.0 && tp > 0.0) ? MathAbs(tp - price) : 0.0;
   double safe_min_sl = MathMax(min_stop_dist, spread_usd + 1.50);
   double safe_min_tp = MathMax(min_stop_dist, spread_usd + 0.80);

   if(InpEnforceHardSL && sl <= 0.0)
   {
      sl = (side == "BUY") ? (ref_p - MathMax(InpEmergencySLUsd, safe_min_sl)) : (ref_p + MathMax(InpEmergencySLUsd, safe_min_sl));
      sl = NormalizeDouble(sl, digits);
      PrintFormat("🛡️ Enforced Hard Emergency SL @ %.2f (no SL was provided)", sl);
   }
   if(sl > 0.0)
   {
      double eff_sl_dist = MathMax(intended_sl_dist, safe_min_sl);
      if(side == "BUY")
      {
         // BUY SL triggers on BID: must sit at least safe_min_sl BELOW current Bid
         if(sl > (ref_p - safe_min_sl))
         {
            double new_sl = NormalizeDouble(ref_p - eff_sl_dist, digits);
            PrintFormat("🛡️ BUY SL Protected: %.2f -> %.2f (Bid=%.2f, Spread=$%.2f, Dist=$%.2f)", sl, new_sl, ref_p, spread_usd, eff_sl_dist);
            sl = new_sl;
         }
      }
      else if(side == "SELL")
      {
         // SELL SL triggers on ASK (= Bid + Spread): add full spread cushion so Bid candles never prematurely stop out!
         if(sl < (ref_p + safe_min_sl))
         {
            double new_sl = NormalizeDouble(ref_p + eff_sl_dist, digits);
            PrintFormat("🛡️ SELL SL Protected: %.2f -> %.2f (Ask=%.2f, Spread=$%.2f, Dist=$%.2f)", sl, new_sl, ref_p, spread_usd, eff_sl_dist);
            sl = new_sl;
         }
         else if(execute_as_market && (sl - tick.ask) < eff_sl_dist)
         {
            double new_sl = NormalizeDouble(tick.ask + eff_sl_dist, digits);
            if(new_sl > sl) sl = new_sl;
         }
      }
   }
   if(tp > 0.0)
   {
      double eff_tp_dist = MathMax(intended_tp_dist, safe_min_tp);
      if(side == "BUY" && tp < (entry_ref + safe_min_tp))
      {
         tp = NormalizeDouble(entry_ref + eff_tp_dist, digits);
      }
      else if(side == "SELL" && tp > (entry_ref - safe_min_tp))
      {
         tp = NormalizeDouble(entry_ref - eff_tp_dist, digits);
      }
   }

   // Detect supported expiration mode
   ENUM_ORDER_TYPE_TIME exp_mode = ORDER_TIME_GTC;
   uint exp_flags = (uint)SymbolInfoInteger(target_sym, SYMBOL_EXPIRATION_MODE);
   if(exp_flags != 0 && (exp_flags & SYMBOL_EXPIRATION_GTC) == 0)
   {
      if((exp_flags & SYMBOL_EXPIRATION_DAY) != 0)
         exp_mode = ORDER_TIME_DAY;
      else
         exp_mode = ORDER_TIME_SPECIFIED;
   }

   // 1. Primary Order Execution (always with SL/TP attached)
   if(execute_as_market)
   {
      if(side == "BUY")
      {
         ok = m_trade.Buy(lot, target_sym, 0.0, sl, tp, comment);
         fill_p = m_trade.ResultPrice();
         slip_pts = (fill_p > 0.0 && tick.ask > 0.0 && pt > 0.0) ? (fill_p - tick.ask) / pt : 0.0;
      }
      else
      {
         ok = m_trade.Sell(lot, target_sym, 0.0, sl, tp, comment);
         fill_p = m_trade.ResultPrice();
         slip_pts = (fill_p > 0.0 && tick.bid > 0.0 && pt > 0.0) ? (tick.bid - fill_p) / pt : 0.0;
      }
   }
   else
   {
      if(side == "BUY")
         ok = m_trade.BuyLimit(lot, price, target_sym, sl, tp, exp_mode, 0, comment);
      else
         ok = m_trade.SellLimit(lot, price, target_sym, sl, tp, exp_mode, 0, comment);
      fill_p = price;
   }

   if(ok)
   {
      opened_ticket = ResolveOpenedTicket(m_trade.ResultOrder(), m_trade.ResultDeal(), execute_as_market);
   }
   else
   {
      uint err = m_trade.ResultRetcode();
      string desc = m_trade.ResultRetcodeDescription();

      // Retry Strategy A: Invalid Filling mode (10030) -> retry IOC then RETURN *with SL/TP intact*
      if(err == 10030)
      {
         Print("⚠️ Broker rejected filling mode (10030). Retrying with IOC / RETURN while keeping SL/TP...");
         m_trade.SetTypeFilling(ORDER_FILLING_IOC);
         if(execute_as_market)
            ok = (side == "BUY") ? m_trade.Buy(lot, target_sym, 0.0, sl, tp, comment) : m_trade.Sell(lot, target_sym, 0.0, sl, tp, comment);
         else
            ok = (side == "BUY") ? m_trade.BuyLimit(lot, price, target_sym, sl, tp, exp_mode, 0, comment) : m_trade.SellLimit(lot, price, target_sym, sl, tp, exp_mode, 0, comment);

         if(!ok)
         {
            m_trade.SetTypeFilling(ORDER_FILLING_RETURN);
            if(execute_as_market)
               ok = (side == "BUY") ? m_trade.Buy(lot, target_sym, 0.0, sl, tp, comment) : m_trade.Sell(lot, target_sym, 0.0, sl, tp, comment);
            else
               ok = (side == "BUY") ? m_trade.BuyLimit(lot, price, target_sym, sl, tp, exp_mode, 0, comment) : m_trade.SellLimit(lot, price, target_sym, sl, tp, exp_mode, 0, comment);
         }
         if(ok)
         {
            opened_ticket = ResolveOpenedTicket(m_trade.ResultOrder(), m_trade.ResultDeal(), execute_as_market);
            fill_p = (execute_as_market ? m_trade.ResultPrice() : price);
         }
         else
         {
            err = m_trade.ResultRetcode();
            desc = m_trade.ResultRetcodeDescription();
         }
      }

      // Retry Strategy B: Invalid Stops/Price (10016 / 10014 / 10015) -> Open/Place and verify Hard SL attached
      if(!ok && (err == 10016 || err == 10014 || err == 10015))
      {
         PrintFormat("⚠️ Broker rejected initial Stops/Price (Error %d: %s). Retrying without initial SL/TP and modifying...", err, desc);
         if(execute_as_market)
            ok = (side == "BUY") ? m_trade.Buy(lot, target_sym, 0.0, 0.0, 0.0, comment) : m_trade.Sell(lot, target_sym, 0.0, 0.0, 0.0, comment);
         else
         {
            if(side == "BUY")
               ok = m_trade.BuyLimit(lot, price, target_sym, 0.0, 0.0, exp_mode, 0, comment);
            else
               ok = m_trade.SellLimit(lot, price, target_sym, 0.0, 0.0, exp_mode, 0, comment);

            if(!ok && allow_mkt_fallback)
            {
               Print("⚠️ Limit retry failed, fallback to Market as allow_market_fallback=true");
               execute_as_market = true;
               ok = (side == "BUY") ? m_trade.Buy(lot, target_sym, 0.0, 0.0, 0.0, comment) : m_trade.Sell(lot, target_sym, 0.0, 0.0, 0.0, comment);
            }
         }

         if(ok)
         {
            opened_ticket = ResolveOpenedTicket(m_trade.ResultOrder(), m_trade.ResultDeal(), execute_as_market);
            fill_p = (execute_as_market ? m_trade.ResultPrice() : price);

            // Mandatory Hard SL attachment verification (up to 3 attempts)
            bool sl_attached = (sl <= 0.0);
            if(sl > 0.0 || tp > 0.0)
            {
               for(int attempt = 0; attempt < 3; attempt++)
               {
                  Sleep(100);
                  double adj_sl = sl;
                  if(attempt > 0 && adj_sl > 0.0)
                  {
                     // Widen SL slightly in case StopsLevel widened on tick
                     adj_sl = NormalizeDouble((side == "BUY") ? (fill_p - MathMax(min_stop_dist, InpEmergencySLUsd)) : (fill_p + MathMax(min_stop_dist, InpEmergencySLUsd)), digits);
                  }
                  if(execute_as_market)
                  {
                     if(m_trade.PositionModify(opened_ticket, adj_sl, tp))
                     {
                        sl = adj_sl;
                        sl_attached = true;
                        break;
                     }
                  }
                  else
                  {
                     if(m_trade.OrderModify(opened_ticket, price, adj_sl, tp, exp_mode, 0))
                     {
                        sl = adj_sl;
                        sl_attached = true;
                        break;
                     }
                  }
               }
            }

            // If we could NOT attach a Hard SL and InpEnforceHardSL is ON -> close immediately! Never leave naked!
            if(execute_as_market && InpEnforceHardSL && !sl_attached)
            {
               PrintFormat("🚨 CRITICAL: Failed to attach Hard SL on Ticket #%I64u — Closing position immediately!", opened_ticket);
               m_trade.PositionClose(opened_ticket, eff_slip_pts);
               SendExecutionReport(action_id, signal_id, "FAILED_NO_SL", opened_ticket, side, lot, fill_p, 0, 0, 0, 0, comment, 10095, "Closed immediately because Hard SL could not be attached", "MARKET", target_sym);
               return;
            }
         }
      }
   }

   string exec_type_label = (execute_as_market ? "MARKET" : "LIMIT");
   if(ok && opened_ticket > 0)
   {
      string rep_status = (execute_as_market ? "FILLED" : "PLACED");
      PrintFormat("✅ KAIROS-79 Order %s: %s (%s) %.2f %s @ %.2f (Ticket #%I64u)", rep_status, side, exec_type_label, lot, target_sym, fill_p, opened_ticket);
      SendExecutionReport(action_id, signal_id, rep_status, opened_ticket, side, lot, fill_p, slip_pts, sl, tp, 0, comment, 0, "OK", exec_type_label, target_sym);
   }
   else
   {
      uint err = m_trade.ResultRetcode();
      string desc = m_trade.ResultRetcodeDescription();
      PrintFormat("❌ KAIROS-79 Order Failed: %d (%s)", err, desc);
      SendExecutionReport(action_id, signal_id, "FAILED", 0, side, lot, 0, 0, sl, tp, 0, comment, err, desc, exec_type_label, target_sym);
   }
}

//+------------------------------------------------------------------+
//| Execute CLOSE_ORDER Action                                       |
//+------------------------------------------------------------------+
void ExecuteCloseOrder(const string action_json)
{
   string action_id = JsonExtractString(action_json, "action_id");
   string signal_id = JsonExtractString(action_json, "signal_id");
   ulong ticket     = (ulong)JsonExtractLong(action_json, "ticket", 0);

   if(IsActionProcessed(action_id)) return;
   MarkActionProcessed(action_id);

   if(ticket <= 0) return;

   ulong slippage_pts = GetSlippagePoints();
   m_trade.SetDeviationInPoints(slippage_pts);

   bool ok = false;
   if(m_position.SelectByTicket(ticket))
   {
      double profit = m_position.Profit() + m_position.Swap();
      ok = m_trade.PositionClose(ticket, slippage_pts);
      if(ok)
      {
         PrintFormat("🏁 KAIROS-79 Position #%I64u Closed. Profit: $%.2f", ticket, profit);
         SendExecutionReport(action_id, signal_id, "CLOSED", ticket, m_position.Symbol(), m_position.Volume(), 0, 0, 0, 0, profit, "MANUAL_CLOSE", 0, "OK");
      }
      else
      {
         SendExecutionReport(action_id, signal_id, "CLOSE_FAILED", ticket, m_position.Symbol(), m_position.Volume(), 0, 0, 0, 0, 0, "FAIL", m_trade.ResultRetcode(), m_trade.ResultRetcodeDescription());
      }
   }
   // Maybe it's a pending limit order
   else if(m_order.Select(ticket))
   {
      ok = m_trade.OrderDelete(ticket);
      SendExecutionReport(action_id, signal_id, (ok ? "CANCELLED" : "CANCEL_FAILED"), ticket, m_order.Symbol(), m_order.VolumeInitial(), m_order.PriceOpen(), 0, 0, 0, 0, "CANCEL", m_trade.ResultRetcode(), m_trade.ResultRetcodeDescription());
   }
}

//+------------------------------------------------------------------+
//| Execute CLOSE_ALL Action                                         |
//+------------------------------------------------------------------+
void ExecuteCloseAll(const string action_json)
{
   string action_id = JsonExtractString(action_json, "action_id");
   string signal_id = JsonExtractString(action_json, "signal_id");

   if(IsActionProcessed(action_id)) return;
   MarkActionProcessed(action_id);

   int closed = 0;
   ulong slippage_pts = GetSlippagePoints();
   m_trade.SetDeviationInPoints(slippage_pts);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if(m_position.SelectByTicket(ticket))
      {
         if(m_position.Magic() == InpMagicNumber || InpMagicNumber == 0)
         {
            if(m_trade.PositionClose(ticket, slippage_pts))
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
   SendExecutionReport(action_id, signal_id, "CLOSED_ALL", 0, "", 0, 0, 0, 0, 0, 0, "CLOSE_ALL", 0, "OK");
}

//+------------------------------------------------------------------+
//| Execute MODIFY_ORDER Action (Position or Pending SL/TP adjust)   |
//+------------------------------------------------------------------+
void ExecuteModifyOrder(const string action_json)
{
   string action_id = JsonExtractString(action_json, "action_id");
   string signal_id = JsonExtractString(action_json, "signal_id");
   ulong ticket     = (ulong)JsonExtractLong(action_json, "ticket", 0);
   double new_sl    = JsonExtractDouble(action_json, "new_sl", 0.0);
   double new_tp    = JsonExtractDouble(action_json, "new_tp", 0.0);

   if(IsActionProcessed(action_id)) return;
   MarkActionProcessed(action_id);

   if(ticket <= 0) return;

   int digits = (int)SymbolInfoInteger(g_symbol, SYMBOL_DIGITS);
   if(digits <= 0) digits = 2;

   if(m_position.SelectByTicket(ticket))
   {
      int pos_digits = (int)SymbolInfoInteger(m_position.Symbol(), SYMBOL_DIGITS);
      if(pos_digits <= 0) pos_digits = digits;
      if(new_sl <= 0.0) new_sl = m_position.StopLoss();
      if(new_tp <= 0.0) new_tp = m_position.TakeProfit();
      new_sl = (new_sl > 0.0) ? NormalizeDouble(new_sl, pos_digits) : 0.0;
      new_tp = (new_tp > 0.0) ? NormalizeDouble(new_tp, pos_digits) : 0.0;

      bool ok = m_trade.PositionModify(ticket, new_sl, new_tp);
      if(ok)
      {
         PrintFormat("🛡️ Position #%I64u Modified: SL %.2f, TP %.2f", ticket, new_sl, new_tp);
         SendExecutionReport(action_id, signal_id, "MODIFIED", ticket, m_position.Symbol(), m_position.Volume(), m_position.PriceOpen(), m_position.PriceCurrent(), new_sl, new_tp, 0, "OK", 0, "OK");
      }
      else
      {
         PrintFormat("⚠️ Position #%I64u Modify failed: %d (%s)", ticket, m_trade.ResultRetcode(), m_trade.ResultRetcodeDescription());
         SendExecutionReport(action_id, signal_id, "MODIFY_FAILED", ticket, m_position.Symbol(), m_position.Volume(), m_position.PriceOpen(), m_position.PriceCurrent(), new_sl, new_tp, 0, "FAIL", m_trade.ResultRetcode(), m_trade.ResultRetcodeDescription());
      }
   }
   else if(m_order.Select(ticket))
   {
      int ord_digits = (int)SymbolInfoInteger(m_order.Symbol(), SYMBOL_DIGITS);
      if(ord_digits <= 0) ord_digits = digits;
      if(new_sl <= 0.0) new_sl = m_order.StopLoss();
      if(new_tp <= 0.0) new_tp = m_order.TakeProfit();
      new_sl = (new_sl > 0.0) ? NormalizeDouble(new_sl, ord_digits) : 0.0;
      new_tp = (new_tp > 0.0) ? NormalizeDouble(new_tp, ord_digits) : 0.0;

      bool ok = m_trade.OrderModify(ticket, m_order.PriceOpen(), new_sl, new_tp, m_order.TypeTime(), m_order.TimeExpiration());
      if(ok)
      {
         PrintFormat("🛡️ Pending Order #%I64u Modified: SL %.2f, TP %.2f", ticket, new_sl, new_tp);
         SendExecutionReport(action_id, signal_id, "MODIFIED", ticket, m_order.Symbol(), m_order.VolumeInitial(), m_order.PriceOpen(), m_order.PriceOpen(), new_sl, new_tp, 0, "PENDING_MODIFIED", 0, "OK");
      }
      else
      {
         SendExecutionReport(action_id, signal_id, "MODIFY_FAILED", ticket, m_order.Symbol(), m_order.VolumeInitial(), m_order.PriceOpen(), m_order.PriceOpen(), new_sl, new_tp, 0, "FAIL", m_trade.ResultRetcode(), m_trade.ResultRetcodeDescription());
      }
   }
   else
   {
      SendExecutionReport(action_id, signal_id, "MODIFY_FAILED", ticket, g_symbol, 0, 0, 0, new_sl, new_tp, 0, "TICKET_NOT_FOUND", 0, "Ticket not found");
   }
}

//+------------------------------------------------------------------+
//| Execute CLOSE_PARTIAL Action (TP1 Partial Close + BE SL Move)    |
//+------------------------------------------------------------------+
void ExecutePartialClose(const string action_json)
{
   string action_id = JsonExtractString(action_json, "action_id");
   string signal_id = JsonExtractString(action_json, "signal_id");
   ulong ticket     = (ulong)JsonExtractLong(action_json, "ticket", 0);
   double close_lot = JsonExtractDouble(action_json, "close_lot", 0.0);
   double new_sl    = JsonExtractDouble(action_json, "new_sl", 0.0);

   if(IsActionProcessed(action_id)) return;
   MarkActionProcessed(action_id);

   if(ticket <= 0) return;
   if(!m_position.SelectByTicket(ticket))
   {
      SendExecutionReport(action_id, signal_id, "PARTIAL_CLOSE_FAILED", ticket, g_symbol, close_lot, 0, 0, new_sl, 0, 0, "TICKET_NOT_FOUND", 0, "Position not found");
      return;
   }

   string sym = m_position.Symbol();
   double pos_vol = m_position.Volume();
   double min_lot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double lot_step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   if(min_lot <= 0.0) min_lot = 0.01;
   if(lot_step <= 0.0) lot_step = 0.01;

   if(close_lot <= 0.0) close_lot = pos_vol * 0.5;
   close_lot = MathFloor(close_lot / lot_step) * lot_step;
   close_lot = NormalizeDouble(close_lot, 2);

   ulong slippage_pts = GetSlippagePoints();
   m_trade.SetDeviationInPoints(slippage_pts);

   if(close_lot < min_lot || (pos_vol - close_lot) < min_lot)
   {
      // Remaining volume would be below broker min lot -> close full or clamp
      if(pos_vol - min_lot >= min_lot)
         close_lot = NormalizeDouble(pos_vol - min_lot, 2);
      else
         close_lot = pos_vol;
   }

   bool ok = false;
   string exec_status = "PARTIAL_CLOSED";
   if(close_lot >= pos_vol - 0.0001)
   {
      ok = m_trade.PositionClose(ticket, slippage_pts);
      exec_status = "CLOSED";
   }
   else
   {
      ok = m_trade.PositionClosePartial(ticket, close_lot, slippage_pts);
   }

   if(!ok)
   {
      SendExecutionReport(action_id, signal_id, "PARTIAL_CLOSE_FAILED", ticket, sym, close_lot, m_position.PriceOpen(), m_position.PriceCurrent(), m_position.StopLoss(), m_position.TakeProfit(), 0, "FAIL", m_trade.ResultRetcode(), m_trade.ResultRetcodeDescription());
      return;
   }

   // Optional SL adjustment (e.g. move remaining position SL to Breakeven)
   if(exec_status == "PARTIAL_CLOSED" && new_sl > 0.0 && m_position.SelectByTicket(ticket))
   {
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      if(digits <= 0) digits = 2;
      double norm_sl = NormalizeDouble(new_sl, digits);
      m_trade.PositionModify(ticket, norm_sl, m_position.TakeProfit());
   }

   double fill_px = m_trade.ResultPrice();
   PrintFormat("✂️ KAIROS-79 Partial Close #%I64u: closed %.2f lots @ %.2f (status=%s)", ticket, close_lot, fill_px, exec_status);
   SendExecutionReport(action_id, signal_id, exec_status, ticket, sym, close_lot, 0, fill_px, new_sl, 0, 0, "TP1_PARTIAL", 0, "OK");
}

//+------------------------------------------------------------------+
//| Execute RESET_KILL_SWITCH Action from KAIROS-79                  |
//+------------------------------------------------------------------+
void ExecuteResetKillSwitch(const string action_json)
{
   string action_id = JsonExtractString(action_json, "action_id");
   if(IsActionProcessed(action_id)) return;
   MarkActionProcessed(action_id);

   double cur_eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(cur_eq <= 0.0) cur_eq = AccountInfoDouble(ACCOUNT_BALANCE);

   g_daily_kill_switch_tripped = false;
   g_day_start_equity = cur_eq;
   g_current_daily_dd_pct = 0.0;
   PrintFormat("🔄 KAIROS-79 Daily Kill Switch RESET. Equity re-anchored to $%.2f", cur_eq);
   SendExecutionReport(action_id, "", "RESET_SUCCESS", 0, "", 0, 0, 0, 0, 0, 0, "Kill switch reset successfully", 0, "OK", "", g_symbol);
}

//+------------------------------------------------------------------+
//| Check Daily Drawdown Kill Switch (UTC/Server Midnight Reset)     |
//+------------------------------------------------------------------+
void CheckDailyDrawdownGuard()
{
   MqlDateTime dt;
   if(!TimeCurrent(dt)) return;
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double base_eq = (equity > 0.0) ? equity : balance;

   if(g_current_day_of_year != dt.day_of_year || g_day_start_equity <= 0.0)
   {
      g_current_day_of_year = dt.day_of_year;
      g_day_start_equity = base_eq;
      g_daily_kill_switch_tripped = false;
      g_current_daily_dd_pct = 0.0;
   }

   if(g_day_start_equity > 0.0 && equity > 0.0)
   {
      double loss_usd = g_day_start_equity - equity;
      double dd_pct = (loss_usd / g_day_start_equity) * 100.0;
      g_current_daily_dd_pct = (dd_pct > 0.0) ? dd_pct : 0.0;
      // Duplicate EA Kill Switch disabled by default (InpMaxDailyLossPct <= 0.0):
      // Portfolio Loss Limit is controlled solely by the KAIROS-79 Dashboard Stop Floor!
      if(InpMaxDailyLossPct <= 0.0)
      {
         g_daily_kill_switch_tripped = false;
      }
      else if(g_current_daily_dd_pct >= InpMaxDailyLossPct && loss_usd >= InpMinDailyLossUsdFloor)
      {
         if(!g_daily_kill_switch_tripped)
         {
            PrintFormat("🛑 DAILY KILL SWITCH TRIPPED: DD %.2f%% (Loss $%.2f) >= %.2f%% (Floor $%.2f)",
                        g_current_daily_dd_pct, loss_usd, InpMaxDailyLossPct, InpMinDailyLossUsdFloor);
         }
         g_daily_kill_switch_tripped = true;
      }
   }
}

//+------------------------------------------------------------------+
//| Local OnTick Guards: Emergency Naked SL + Auto Break-Even        |
//+------------------------------------------------------------------+
void CheckLocalPositionGuards()
{
   double bid = SymbolInfoDouble(g_symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(g_symbol, SYMBOL_ASK);
   double pt  = SymbolInfoDouble(g_symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(g_symbol, SYMBOL_DIGITS);
   int stops_pts = (int)SymbolInfoInteger(g_symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(pt <= 0.0) pt = 0.01;
   if(digits <= 0) digits = 2;
   double min_stop_dist = MathMax(stops_pts * pt, 0.50);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if(!m_position.SelectByTicket(ticket)) continue;
      if(InpMagicNumber != 0 && m_position.Magic() != InpMagicNumber) continue;
      if(m_position.Symbol() != g_symbol) continue;

      long pos_type = m_position.PositionType();
      double entry  = m_position.PriceOpen();
      double cur_sl = m_position.StopLoss();
      double cur_tp = m_position.TakeProfit();

      // 1. Emergency Hard SL Repair if a position has no SL
      if(InpEnforceHardSL && cur_sl <= 0.0 && InpEmergencySLUsd > 0.0)
      {
         double emerg_sl = (pos_type == POSITION_TYPE_BUY)
                           ? NormalizeDouble(entry - InpEmergencySLUsd, digits)
                           : NormalizeDouble(entry + InpEmergencySLUsd, digits);
         if(pos_type == POSITION_TYPE_BUY && bid > 0.0 && (bid - emerg_sl) < min_stop_dist)
            emerg_sl = NormalizeDouble(bid - min_stop_dist - 0.50, digits);
         if(pos_type == POSITION_TYPE_SELL && ask > 0.0 && (emerg_sl - ask) < min_stop_dist)
            emerg_sl = NormalizeDouble(ask + min_stop_dist + 0.50, digits);

         if(m_trade.PositionModify(ticket, emerg_sl, cur_tp))
         {
            PrintFormat("🛡️ Emergency Hard SL Attached to #%I64u: SL=%.2f", ticket, emerg_sl);
            cur_sl = emerg_sl;
         }
      }

      // 2. Local Auto Break-Even Guard (works even if server disconnects)
      if(InpEnableLocalBreakEven && cur_sl > 0.0 && InpBreakEvenTriggerUsd > 0.0)
      {
         if(pos_type == POSITION_TYPE_BUY && bid > 0.0)
         {
            double target_sl = NormalizeDouble(entry + InpBreakEvenLockUsd, digits);
            if((bid - entry) >= InpBreakEvenTriggerUsd && cur_sl < (target_sl - pt) && (bid - target_sl) >= min_stop_dist)
            {
               if(m_trade.PositionModify(ticket, target_sl, cur_tp))
                  PrintFormat("🔒 Local Break-Even Set #%I64u LONG: SL=%.2f", ticket, target_sl);
            }
         }
         else if(pos_type == POSITION_TYPE_SELL && ask > 0.0)
         {
            double target_sl = NormalizeDouble(entry - InpBreakEvenLockUsd, digits);
            if((entry - ask) >= InpBreakEvenTriggerUsd && cur_sl > (target_sl + pt) && (target_sl - ask) >= min_stop_dist)
            {
               if(m_trade.PositionModify(ticket, target_sl, cur_tp))
                  PrintFormat("🔒 Local Break-Even Set #%I64u SHORT: SL=%.2f", ticket, target_sl);
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Poll Actions from KAIROS-79 Server                               |
//+------------------------------------------------------------------+
void PollServerActions()
{
   g_last_poll_ms = GetTickCount64();
   string resp = "";
   if(!SendHttpRequest("GET", "/api/ea/poll", "", resp))
      return;

   // Update auto trading flag, server_time, and adaptive poll interval
   g_auto_trading_enabled = JsonExtractBool(resp, "auto_trading_enabled", g_auto_trading_enabled);
   long srv_t = JsonExtractLong(resp, "server_time", 0);
   if(srv_t > 0) g_last_server_time = srv_t;

   int next_poll = (int)JsonExtractLong(resp, "next_poll_ms", g_next_poll_ms);
   if(next_poll >= 80 && next_poll <= 2000 && next_poll != g_next_poll_ms)
   {
      g_next_poll_ms = next_poll;
      EventSetMillisecondTimer(g_next_poll_ms);
   }

   // Find "actions" array robustly, handling any whitespace around ':' and '['
   int key_pos = StringFind(resp, "\"actions\"");
   if(key_pos < 0) return;

   int colon_pos = StringFind(resp, ":", key_pos + 9);
   if(colon_pos < 0) return;

   int start_arr = StringFind(resp, "[", colon_pos);
   if(start_arr < 0) return;

   int resp_len = StringLen(resp);
   int search_pos = start_arr + 1;

   while(search_pos < resp_len)
   {
      int start_obj = StringFind(resp, "{", search_pos);
      if(start_obj < 0) break;

      // Ensure { is within the actions array [ ... ]
      int end_arr = StringFind(resp, "]", search_pos);
      if(end_arr >= 0 && start_obj > end_arr)
         break;

      int depth = 0;
      int end_obj = -1;
      for(int i = start_obj; i < resp_len; i++)
      {
         ushort ch = StringGetCharacter(resp, i);
         if(ch == '{') depth++;
         else if(ch == '}')
         {
            depth--;
            if(depth == 0)
            {
               end_obj = i;
               break;
            }
         }
      }
      if(end_obj < 0) break;

      string action_json = StringSubstr(resp, start_obj, end_obj - start_obj + 1);
      string act_type = JsonExtractString(action_json, "type");
      string action_id = JsonExtractString(action_json, "action_id");

      PrintFormat("📥 KAIROS-79 Action Received: %s (Auto: %s, ActionID: %s)", act_type, (g_auto_trading_enabled ? "ON" : "OFF"), action_id);

      if(act_type == "OPEN_ORDER")
      {
         ExecuteOpenOrder(action_json);
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
      else if(act_type == "CLOSE_PARTIAL")
      {
         ExecutePartialClose(action_json);
      }
      else if(act_type == "RESET_KILL_SWITCH" || act_type == "RESET_GUARD")
      {
         ExecuteResetKillSwitch(action_json);
      }

      search_pos = end_obj + 1;
   }
}

//+------------------------------------------------------------------+
//| Update Chart Visual HUD Info                                     |
//+------------------------------------------------------------------+
void UpdateChartHUD()
{
   string conn_text;
   if(g_server_online)
      conn_text = "🟢 CONNECTED (Online)";
   else
      conn_text = "🔴 DISCONNECTED: " + g_last_error_desc;

   string auto_text = g_auto_trading_enabled ? "🟢 AUTO-TRADING: ON" : "⏸️ AUTO-TRADING: OFF";

   bool terminal_trade_allowed = (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) != 0);
   bool ea_trade_allowed = (MQLInfoInteger(MQL_TRADE_ALLOWED) != 0);
   string trade_btn_status = (terminal_trade_allowed && ea_trade_allowed)
                              ? "✅ MT5 Algo Trading: ALLOWED"
                              : "⚠️ MT5 Algo Trading: DISABLED (Click Algo Trading button!)";

   string kill_text = g_daily_kill_switch_tripped
                      ? StringFormat("🛑 TRIPPED (DD %.2f%% >= %.2f%%, Floor $%.2f)", g_current_daily_dd_pct, InpMaxDailyLossPct, InpMinDailyLossUsdFloor)
                      : StringFormat("🟢 ARMED (DD %.2f%% / Max %.2f%%, Floor $%.2f)", g_current_daily_dd_pct, InpMaxDailyLossPct, InpMinDailyLossUsdFloor);

   string rollover_text = (InpEnableRolloverGuard && IsInRolloverWindow()) ? "🌙 ACTIVE (Blocked)" : "✅ Clear";

   string trouble_hint = "";
   if(!g_server_online)
   {
      if(g_last_error_code == 4014)
      {
         trouble_hint = "  ⚠️ FIX ERROR 4014 (BLOCKED BY MT5):\n"
                      + "  1) Menu: Tools (เครื่องมือ) -> Options (ตัวเลือก)\n"
                      + "  2) Tab: 'Expert Advisors'\n"
                      + "  3) Check: [✓] Allow WebRequest for listed URL\n"
                      + "  4) Add URL: " + g_server_url + "\n"
                      + "  5) Add URL: http://localhost:8000\n"
                      + "  6) Click OK";
      }
      else if(StringFind(g_server_url, ":8080") >= 0)
      {
         trouble_hint = "  ⚠️ FIX PORT MISMATCH:\n"
                      + "  Chart uses old port 8080! Server is on port 8000.\n"
                      + "  👉 Press F7 on chart -> Change Server URL to: http://127.0.0.1:8000";
      }
      else if(g_last_error_code == 5203)
      {
         trouble_hint = "  ⚠️ FIX ERROR 5203 (CANNOT CONNECT):\n"
                      + "  1) Make sure Python server is running on Windows:\n"
                      + "     python app.py\n"
                      + "  2) Open Windows browser and test: http://127.0.0.1:8000\n"
                      + "  3) Press F7 on chart to verify URL: " + g_server_url;
      }
      else
      {
         trouble_hint = "  ⚠️ FIX CONNECTION:\n"
                      + "  1) Check 'python app.py' terminal on Windows\n"
                      + "  2) Verify chart URL (press F7): " + g_server_url;
      }
   }

   string hud = "========================================\n"
              + "  🤖 KAIROS-79 EA Bridge v1.08 (Ultra-Fast 100ms)\n"
              + "========================================\n"
              + "  Server URL:     " + g_server_url + "\n"
              + "  Status:         " + conn_text + "\n"
              + "  Web Control:    " + auto_text + "\n"
              + "  Algo Trading:   " + trade_btn_status + "\n"
              + "  Daily DD Guard: " + kill_text + "\n"
              + "  Rollover Guard: " + rollover_text + "\n"
              + "  Magic Number:   " + IntegerToString(InpMagicNumber) + "\n"
              + "  Symbol:         " + g_symbol + " (Cross-Asset Active)\n"
              + "  Open/Pending:   " + IntegerToString(CountMagicPositions()) + " / " + IntegerToString(OrdersTotal()) + "\n"
              + "  Poll Interval:  " + IntegerToString(g_next_poll_ms) + "ms\n"
              + "========================================\n"
              + (g_server_online ? "  ✅ Multi-Symbol, Hard-SL & Deal Sync Active" : trouble_hint);

   Comment(hud);
}

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   g_symbol = (InpSymbolOverride != "") ? InpSymbolOverride : _Symbol;
   g_server_url = SanitizeServerUrl(InpServerUrl);
   g_next_poll_ms = InpPollIntervalMs;

   // Setup CTrade
   m_trade.SetExpertMagicNumber(InpMagicNumber);
   m_trade.SetDeviationInPoints(GetSlippagePoints());

   if(InpAutoDetectFilling)
      SetupFillingMode(g_symbol);

   CheckDailyDrawdownGuard();

   Print("=======================================================");
   Print("🚀 Starting KAIROS-79 MT5 Bridge EA v1.06 (Cross-Asset Breadth) on ", g_symbol);
   Print("🔗 Configured Server URL: ", g_server_url);
   Print("🛡️ Magic Number: ", InpMagicNumber, " | MaxSpread=$", InpMaxSpreadUsd, " | HardSL=", (InpEnforceHardSL ? "ON" : "OFF"));
   if(StringFind(g_server_url, ":8080") >= 0)
   {
      Print("⚠️ CAUTION: Configured URL uses port 8080! If KAIROS-79 is on port 8000, please press F7 and update to http://127.0.0.1:8000");
   }
   Print("=======================================================");

   // Initial heartbeat & check
   g_last_heartbeat_ms = GetTickCount64();
   SendHeartbeat();
   SyncHistoryDeals();
   UpdateChartHUD();

   // Set millisecond timer
   EventSetMillisecondTimer(g_next_poll_ms);

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
   ulong now_ms = GetTickCount64();
   CheckDailyDrawdownGuard();

   // 1. Send Heartbeat Telemetry every N seconds
   if(now_ms - g_last_heartbeat_ms >= (ulong)(InpHeartbeatIntervalSec * 1000))
   {
      SendHeartbeat();
      g_last_heartbeat_ms = now_ms;
      UpdateChartHUD();
   }

   // 2. Poll pending actions from KAIROS-79
   // If offline, throttle polling to avoid freezing UI
   if(g_server_online || (now_ms - g_last_heartbeat_ms >= 2000))
   {
      PollServerActions();
   }
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
   ulong now_ms = GetTickCount64();
   if(now_ms - g_last_tick_guard_ms >= 200)
   {
      g_last_tick_guard_ms = now_ms;
      CheckDailyDrawdownGuard();
      CheckLocalPositionGuards();
   }

   // Ultra-Fast Action Poll on Tick (throttled to g_next_poll_ms e.g. 100ms)
   if(g_server_online && (now_ms - g_last_poll_ms >= (ulong)g_next_poll_ms))
   {
      PollServerActions();
   }
}

//+------------------------------------------------------------------+
//| Trade Transaction Event Handler (Real Deal History Capture)      |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                         const MqlTradeRequest &request,
                         const MqlTradeResult &result)
{
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
   {
      ulong deal = trans.deal;
      if(deal > 0 && !IsDealProcessed(deal) && HistoryDealSelect(deal))
      {
         long deal_entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
         long deal_magic = HistoryDealGetInteger(deal, DEAL_MAGIC);
         if(InpMagicNumber != 0 && deal_magic != InpMagicNumber) return;

         string sym = HistoryDealGetString(deal, DEAL_SYMBOL);
         if(sym != "" && sym != g_symbol) return;

         // Check if this is an exit deal (DEAL_ENTRY_OUT or DEAL_ENTRY_OUT_BY)
         if(deal_entry == DEAL_ENTRY_OUT || deal_entry == DEAL_ENTRY_OUT_BY)
         {
            MarkDealProcessed(deal);
            ulong pos_id = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
            double profit = HistoryDealGetDouble(deal, DEAL_PROFIT);
            double swap = HistoryDealGetDouble(deal, DEAL_SWAP);
            double comm = HistoryDealGetDouble(deal, DEAL_COMMISSION);
            double price = HistoryDealGetDouble(deal, DEAL_PRICE);
            double vol = HistoryDealGetDouble(deal, DEAL_VOLUME);
            long deal_type = HistoryDealGetInteger(deal, DEAL_TYPE);
            long deal_reason = HistoryDealGetInteger(deal, DEAL_REASON);
            string comment = HistoryDealGetString(deal, DEAL_COMMENT);

            string reason_str = "DEAL_CLOSE";
            if(deal_reason == DEAL_REASON_SL) reason_str = "DEAL_SL";
            else if(deal_reason == DEAL_REASON_TP) reason_str = "DEAL_TP";
            else if(deal_reason == DEAL_REASON_CLIENT) reason_str = "DEAL_MANUAL";

            string side_str = (deal_type == DEAL_TYPE_BUY) ? "BUY" : ((deal_type == DEAL_TYPE_SELL) ? "SELL" : "");

            PrintFormat("💰 Real MT5 Deal #%I64u Position #%I64u: PnL=$%.2f, Swap=$%.2f, Comm=$%.2f, Reason=%s",
               deal, pos_id, profit, swap, comm, reason_str);

            SendDealExecutionReport(deal, pos_id, sym, side_str, vol, price, profit, swap, comm, comment, reason_str, false);
         }
      }
   }
}
//+------------------------------------------------------------------+
