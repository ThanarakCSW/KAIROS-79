# KAIROS-79 (K79) — Precision Gold Timing & Risk System

**ระบบจับจังหวะทองคำและคัดกรองความเสี่ยงเชิงรุก**

- **Project:** `kairos-79`
- **System Name (EN):** Kairos-79: Precision Gold Timing & Risk System
- **System Name (TH):** ระบบจับจังหวะทองคำและคัดกรองความเสี่ยงเชิงรุก
- **Abbreviation:** `KAIROS-79` / `K79`

> *Kairos* (ไครอส) ในภาษากรีกแปลว่า "จังหวะเวลาที่ลงตัวที่สุดเพียงชั่วพริบตา" รวมกับ *79* ซึ่งเป็นเลขอะตอมของธาตุทองคำ (`Au`)

KAIROS-79 เป็นเว็บแอปพลิเคชันผู้ช่วยวิเคราะห์กราฟทองคำ (`XAU/USD`) และประเมินความเสี่ยงก่อนเข้าออเดอร์ใน MT5 โดยรวมการอ่านโครงสร้างราคา Smart Money Concepts (SMC), แบบจำลองความน่าจะเป็น Monte Carlo 15,000 เส้นทาง, ด่านตรวจความเสี่ยงแบบ Deterministic Risk Gate และแผนรับมือ 3 Scenarios จาก Gemini AI เข้าด้วยกันบนหน้าจอ Real-time ผ่าน WebSocket

---

## ความสามารถหลัก

- **Real-Time XAU/USD Data Hub ([`engine/data_feed.py`](engine/data_feed.py)):** รับราคาสดผ่าน TradingView WebSocket (`OANDA:XAUUSD`), Swissquote Institutional Spot Bid/Ask และ Binance (`PAXGUSDT`) พร้อมโหมด Replay สำหรับทดสอบย้อนหลัง
- **Structure, MTF & SMC Engine ([`engine/structure.py`](engine/structure.py), [`engine/smc.py`](engine/smc.py)):** วิเคราะห์ EMA 20/50/200, ATR(14), BOS/CHoCH, Order Block, Fair Value Gap, Liquidity Sweep, ระดับราคาสถาบัน (PDH/PDL, VWAP, POC), Multi-Timeframe (`M1`–`H4`) และ Macro Correlation (DXY / US10Y)
- **15,000-Path Monte Carlo Simulator ([`engine/simulate.py`](engine/simulate.py)):** จำลองความน่าจะเป็นล่วงหน้า 15,000 เส้นทาง เพื่อคำนวณ Win Probability, Sweep Risk, Expected Value (`EV`) และ Fan Chart (`P5`–`P95`)
- **Gemini 3-Scenario Risk Advisor ([`advisor/ai_evaluator.py`](advisor/ai_evaluator.py)):** วิเคราะห์แท่งเทียนร่วมกับโซน SMC และสถิติจำลอง เพื่อวางแผนหลัก (Scenario A), จุดดักกวาดสภาพคล่อง (Scenario B) และจุดยกเลิกแผน (Scenario C) พร้อม fallback เมื่อยังไม่ใส่ API Key
- **Deterministic Risk Gate & MT5 Ticket ([`engine/risk_gate.py`](engine/risk_gate.py)):** คำนวณ `Lot Size` อัตโนมัติ, ชดเชย Broker Offset/Spread, เช็กปฏิทินข่าวกล่องแดง USD ([`engine/news_feed.py`](engine/news_feed.py)) และสรุปโพยคำสั่ง (`Entry`, `SL`, `TP`, `Lot`) ให้คัดลอกไปกดใน MT5 ด้วยตัวเอง
- **Walk-Forward Signal Tracker ([`engine/signal_tracker.py`](engine/signal_tracker.py)):** ติดตามและประเมินผลย้อนหลังของสัญญาณ (`WIN_TP2`, `WIN_TP1_BE`, `LOSS_SWEEP`, `LOSS_SL`)

---

## โครงสร้างโปรเจกต์

```text
kairos-79/
├── app.py                  # Async HTTP + RFC 6455 WebSocket Server (Python stdlib)
├── config.example.json     # เทมเพลตตั้งค่าพอร์ต ความเสี่ยง และ AI (คัดลอกเป็น config.json)
├── requirements.txt        # แพ็กเกจเสริม (รันตัวหลักได้ด้วย Python 3 stdlib)
├── advisor/
│   ├── ai_evaluator.py     # วิเคราะห์แผนเทรด 3 Scenarios ด้วย Gemini / Fallback
│   └── prompts/            # System prompt สำหรับการวิเคราะห์ SMC & Risk
├── engine/
│   ├── data_feed.py        # ฟีดราคาทองคำ Real-time และโหมด Replay
│   ├── news_feed.py        # ปฏิทินข่าวกล่องแดง USD และ Session ตลาด
│   ├── risk_gate.py        # ด่านตรวจความเสี่ยงและคำนวณ Lot Size สำหรับ MT5
│   ├── signal_tracker.py   # บันทึกและให้คะแนนความแม่นยำย้อนหลัง
│   ├── simulate.py         # Monte Carlo Simulation 15,000 เส้นทาง
│   ├── smc.py              # ตรวจจับ BOS, CHoCH, OB, FVG และ Liquidity Sweep
│   └── structure.py        # โครงสร้างราคา EMA, ATR, S&R, MTF และ VWAP/POC
├── web/
│   ├── index.html          # หน้าเว็บ Terminal หลัก
│   ├── style.css           # ธีม Dark/Gold และ Responsive Topbar
│   └── app.js              # WebSocket client, กราฟ Lightweight Charts และ MT5 Ticket
└── tests/                  # Unit & Integration Tests (รันแบบ Offline ได้ทั้งหมด)
```

---

## การติดตั้งและเริ่มต้นใช้งาน

เซิร์ฟเวอร์หลักเขียนด้วย Python 3 Standard Library สามารถรันได้ทันทีโดยไม่ต้องติดตั้งแพ็กเกจเพิ่ม (หรือติดตั้ง `requirements.txt` เพิ่มเติมหากต้องการ)

1. **เตรียมไฟล์ตั้งค่า** (ระบบจะไม่ติดตาม `config.json` ใน Git เพื่อป้องกัน API Key หลุด)

```bash
cp config.example.json config.json
```

2. **ตั้งค่า Google Gemini API Key** (เลือกทำอย่างใดอย่างหนึ่ง หรือใช้งานโหมดคำนวณ Deterministic โดยไม่ใส่คีย์ก็ได้)
   - ตั้งผ่าน Environment Variable:
     ```bash
     export GEMINI_API_KEY="your_api_key_here"
     ```
   - หรือเปิดหน้าเว็บแล้วกรอกผ่านเมนู **⚙️ ตั้งค่าพอร์ต & API** ซึ่งจะบันทึกลง `config.json` ภายในเครื่อง

3. **รันเซิร์ฟเวอร์**

```bash
python3 app.py
```

เปิดเบราว์เซอร์ไปที่ **`http://localhost:8000`** หรือ **`http://127.0.0.1:8000`**

*(หากใช้งานร่วมกับ MetaTrader 5 EA บน Windows PC / VPS สามารถดู IP วง LAN หรือ Public IP ที่ระบบแสดงขึ้นมาบน Terminal เพื่อนำไปใส่ใน MT5 WebRequest ได้ทันที)*

---

## การทดสอบ (Unit & Integration Tests)

```bash
python3 -m unittest discover -s tests -t . -v
```

### รันแยกแต่ละ Engine ผ่าน CLI (โหมด Offline)

```bash
python3 -m engine.structure --offline tests/fixtures/xauusd_m5_sample.json
python3 -m engine.smc --offline tests/fixtures/xauusd_m5_sample.json
python3 -m engine.simulate --offline tests/fixtures/xauusd_m5_sample.json
```

---

## ความปลอดภัยและข้อจำกัดความรับผิดชอบ

- **ห้ามอัปโหลดไฟล์ลับ:** ไฟล์ `config.json`, `.env` และคีย์ส่วนตัวถูกกันไว้ใน `.gitignore` แล้ว ควรตรวจสอบ `git status` ก่อน commit ทุกครั้ง
- **ไม่ส่งคำสั่งเทรดอัตโนมัติ:** ระบบออกเพียงโพยคำสั่งเพื่อช่วยตัดสินใจ ผู้ใช้ต้องตรวจสอบราคาและกดเข้าออเดอร์ใน MT5 ด้วยตัวเอง
- **ไม่ใช่คำแนะนำทางการเงิน:** ข้อมูลและการจำลองความน่าจะเป็นเป็นการคำนวณเชิงสถิติจากข้อมูลราคาในอดีต ไม่สามารถรับประกันผลกำไรในอนาคตได้
