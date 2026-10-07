"""
BB Breakout Alert -> Telegram
ตรวจราคาทองเทียบ Bollinger Bands บน M30 / H1 / H4 / D1 แล้วส่งแจ้งเตือนเข้า Telegram
บอกระดับว่าหลุดกี่ TF และแจ้งซ้ำเมื่อหลุดไปไกลมาก (escalation)
รันบน GitHub Actions ทุก ~5 นาที สถานะเก็บใน state.json

CHECK_MODE
  live   = ใช้ราคาล่าสุดตอนเช็ค
  closed = ใช้ราคาปิดของแท่งที่ปิดแล้ว
  wick   = ใช้ High/Low ของแท่งปัจจุบัน (จับไส้เทียนที่แทงออกนอกขอบระหว่างรอบเช็ค)
"""
import json
import math
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests


def _list(name, default):
    return [t.strip() for t in os.getenv(name, default).split(",") if t.strip()]


def _bool(name, default):
    return os.getenv(name, default).lower() == "true"


# ---------------- ตั้งค่า (แก้ผ่าน env / workflow ได้) ----------------
TICKER = os.getenv("TICKER", "GC=F")              # ทองฟิวเจอร์ส; ลอง "XAUUSD=X" ได้
LABEL = os.getenv("SYMBOL_LABEL", "XAUUSD")
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_DEV = float(os.getenv("BB_DEV", "2.0"))
CHECK_MODE = os.getenv("CHECK_MODE", "wick")      # live | closed | wick
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "900"))
NOTIFY_RETURN = _bool("NOTIFY_RETURN", "false")
USE_TFS = _list("USE_TFS", "M30,H1,H4,D1")
H4_OFFSET_HOURS = int(os.getenv("H4_OFFSET_HOURS", "1"))  # XM ฤดูร้อน (UTC+3)=1, ฤดูหนาว (UTC+2)=2
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

# แจ้งแบบเด่น 🚨 เมื่อหลุดครบทุก TF ในรายการนี้
FULL_TFS = [t for t in _list("FULL_TFS", "H1,H4,D1") if t in USE_TFS]
FULL_REPEAT = int(os.getenv("FULL_REPEAT", "3"))          # จำนวนครั้งที่ส่งซ้ำ
FULL_REPEAT_GAP = int(os.getenv("FULL_REPEAT_GAP", "4"))  # หน่วงระหว่างข้อความ (วินาที)

# แจ้งซ้ำเมื่อหลุดไปไกลมาก: วัดเป็นหน่วย σ (ขอบ BB = BB_DEV σ จากเส้นกลาง)
ESC_ENABLED = _bool("ESC_ENABLED", "true")
ESC_STEP = float(os.getenv("ESC_STEP", "1.0"))            # ทุกกี่ σ เกินขอบ ถึงแจ้งอีกครั้ง
ESC_MAX_LEVELS = int(os.getenv("ESC_MAX_LEVELS", "2"))    # แจ้งซ้ำได้สูงสุดกี่ครั้งต่อ 1 รอบการหลุด
ESC_TFS = _list("ESC_TFS", "H1,H4,D1")                    # TF ที่ใช้แจ้งซ้ำ (M30 ถี่เกินจึงไม่ใส่)

# ตำแหน่งบิตเดิมคงไว้ เพื่อให้ state.json เก่ายังใช้ได้
TF_BIT = {"H1": 0, "H4": 1, "D1": 2, "M30": 3}
DISPLAY = ["M30", "H1", "H4", "D1"]
COLS = ["High", "Low", "Close"]


# ---------------- ดึงข้อมูล ----------------
def _download(interval, period):
    import yfinance as yf
    df = yf.download(TICKER, interval=interval, period=period,
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC")
    return df


def fetch_frames():
    """คืน dict {'M30','H1','H4','D1': DataFrame} คอลัมน์ High/Low/Close"""
    out = {}
    h1 = _download("1h", "60d")
    if h1 is None:
        return None
    out["H1"] = h1[COLS]
    out["H4"] = (h1[COLS]
                 .resample("4h", offset=f"{H4_OFFSET_HOURS}h")
                 .agg({"High": "max", "Low": "min", "Close": "last"})
                 .dropna())
    if "M30" in USE_TFS:
        m30 = _download("30m", "60d")
        if m30 is not None:
            out["M30"] = m30[COLS]
    d1 = _download("1d", "1y")
    if d1 is not None:
        out["D1"] = d1[COLS]
    return out


# ---------------- คำนวณ ----------------
def band_state(df, live_price):
    """คืน dict ผลการเทียบกับ BB (หรือ None ถ้าข้อมูลไม่พอ)"""
    if CHECK_MODE == "closed":
        df = df.iloc[:-1]                 # ตัดแท่งที่ยังไม่ปิด
    close = df["Close"]
    if len(close) < BB_PERIOD:
        return None
    ma = float(close.rolling(BB_PERIOD).mean().iloc[-1])
    sd = float(close.rolling(BB_PERIOD).std(ddof=0).iloc[-1])   # std แบบเดียวกับ MT5
    upper = ma + BB_DEV * sd
    lower = ma - BB_DEV * sd

    if CHECK_MODE == "closed":
        price = float(close.iloc[-1])
        hi = lo = price
    elif CHECK_MODE == "wick":
        price = float(live_price)
        hi = max(float(df["High"].iloc[-1]), price)
        lo = min(float(df["Low"].iloc[-1]), price)
    else:  # live
        price = float(live_price)
        hi = lo = price

    z_up = (hi - ma) / sd if sd > 0 else 0.0
    z_dn = (ma - lo) / sd if sd > 0 else 0.0
    return {"is_up": hi > upper, "is_dn": lo < lower, "price": price,
            "upper": upper, "lower": lower, "hi": hi, "lo": lo,
            "z_up": z_up, "z_dn": z_dn}


def esc_level(z):
    """ระดับการหลุดไกล: 0 = เพิ่งหลุดขอบ, 1 = ไกลกว่าขอบ ESC_STEP σ, 2 = 2*ESC_STEP σ ..."""
    if z < BB_DEV + ESC_STEP:
        return 0
    return int(math.floor((z - BB_DEV) / ESC_STEP + 1e-9))


def mask_text(mask):
    return "+".join(tf for tf in DISPLAY if mask & (1 << TF_BIT[tf]))


def bits(mask):
    return bin(mask).count("1")


def evaluate(frames, state, now):
    """คืน (messages, new_state)"""
    live_price = float(frames["H1"]["Close"].iloc[-1])
    res = {}
    up_mask = lo_mask = 0
    last_price = live_price
    for tf in DISPLAY:
        if tf not in USE_TFS or tf not in frames:
            continue
        r = band_state(frames[tf], live_price)
        if r is None:
            print(f"[{tf}] ข้อมูลไม่พอ")
            continue
        res[tf] = r
        label = "เหนือบน" if r["is_up"] else "ต่ำกว่าล่าง" if r["is_dn"] else "ในกรอบ"
        print(f"[{tf}] ราคา={r['price']:.2f} สูงสุดแท่ง={r['hi']:.2f} ต่ำสุดแท่ง={r['lo']:.2f} "
              f"บน={r['upper']:.2f} ล่าง={r['lower']:.2f} "
              f"z_บน={r['z_up']:.2f} z_ล่าง={r['z_dn']:.2f} สถานะ={label}")
        if CHECK_MODE == "closed":
            last_price = r["price"]
        if r["is_up"]:
            up_mask |= 1 << TF_BIT[tf]
        if r["is_dn"]:
            lo_mask |= 1 << TF_BIT[tf]

    total = len([t for t in DISPLAY if t in USE_TFS])
    inited = bool(state.get("init"))
    prev_up = state.get("up", 0) if inited else 0
    prev_lo = state.get("lo", 0) if inited else 0

    new_state = {
        "up": up_mask, "lo": lo_mask,
        "last_up": dict(state.get("last_up", {})),
        "last_lo": dict(state.get("last_lo", {})),
        "esc_up": {}, "esc_lo": {},
        "init": True,
    }
    msgs = []

    # ---- escalation: หลุดไกลขึ้นกว่าที่เคยแจ้ง ----
    esc_hits = {"up": [], "lo": []}
    for side, mask, prev, key, zk in (("up", up_mask, prev_up, "esc_up", "z_up"),
                                      ("lo", lo_mask, prev_lo, "esc_lo", "z_dn")):
        old = state.get(key, {}) if inited else {}
        for tf, r in res.items():
            bit = 1 << TF_BIT[tf]
            if not (mask & bit):
                continue                              # กลับเข้ากรอบแล้ว: ล้างระดับ
            k = esc_level(r[zk])
            if not (prev & bit):
                new_state[key][tf] = k                # เพิ่งหลุด: แจ้งปกติไปแล้ว
                continue
            last_k = old.get(tf, 0)
            new_state[key][tf] = max(k, last_k)
            if (ESC_ENABLED and tf in ESC_TFS and k > last_k
                    and 1 <= k <= ESC_MAX_LEVELS):
                esc_hits[side].append((tf, r[zk]))

    if not inited:                                    # รันครั้งแรก: จำสถานะเฉยๆ
        return msgs, new_state

    def fresh(mask, prev, key):
        new = mask & ~prev
        for tf in DISPLAY:
            bit = 1 << TF_BIT[tf]
            if new & bit:
                if now - new_state[key].get(tf, 0) < COOLDOWN_SEC:
                    new &= ~bit
                else:
                    new_state[key][tf] = now
        return new

    def build(mask, up):
        full_bits = [1 << TF_BIT[t] for t in FULL_TFS]
        full = len(full_bits) >= 2 and all(mask & b for b in full_bits)
        if full:
            side = "เหนือ BB บน" if up else "ต่ำกว่า BB ล่าง"
            icon = "🟢" if up else "🔴"
            names = "+".join(FULL_TFS)
            text = (f"🚨🚨🚨 <b>{LABEL} หลุด {side} ครบ {names}</b> 🚨🚨🚨\n"
                    f"{icon} ระดับ {bits(mask)}/{total} | {mask_text(mask)} | ราคา {last_price:.2f}")
            return {"text": text, "strong": True}
        arrow, side = ("🔺", "เหนือ BB บน") if up else ("🔻", "ต่ำกว่า BB ล่าง")
        text = (f"{arrow} {LABEL} {side} | ระดับ {bits(mask)}/{total} | "
                f"{mask_text(mask)} | ราคา {last_price:.2f}")
        return {"text": text, "strong": False}

    prev_up_state, prev_lo_state = prev_up, prev_lo
    if fresh(up_mask, prev_up_state, "last_up"):
        msgs.append(build(up_mask, True))
    if fresh(lo_mask, prev_lo_state, "last_lo"):
        msgs.append(build(lo_mask, False))

    for side, name in (("up", "บน"), ("lo", "ล่าง")):
        if esc_hits[side]:
            detail = ", ".join(f"{tf} {z:.1f}σ" for tf, z in esc_hits[side])
            msgs.append({"text": f"🔥 {LABEL} ไปไกลจาก BB {name} มาก | {detail} | ราคา {last_price:.2f}",
                         "strong": False})

    if NOTIFY_RETURN:
        if prev_up & ~up_mask:
            msgs.append({"text": f"↩️ {LABEL} กลับเข้ากรอบจาก BB บน | {mask_text(prev_up & ~up_mask)} | ราคา {last_price:.2f}", "strong": False})
        if prev_lo & ~lo_mask:
            msgs.append({"text": f"↩️ {LABEL} กลับเข้ากรอบจาก BB ล่าง | {mask_text(prev_lo & ~lo_mask)} | ราคา {last_price:.2f}", "strong": False})

    return msgs, new_state


# ---------------- Telegram / state ----------------
def send_telegram(text, html=False):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    print(text)
    if not token or not chat_id:
        print("(ยังไม่ได้ตั้ง TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID จึงไม่ได้ส่ง)")
        return False
    payload = {"chat_id": chat_id, "text": text}
    if html:
        payload["parse_mode"] = "HTML"
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data=payload, timeout=20)
    if not r.ok:
        print("Telegram error:", r.status_code, r.text)
    return r.ok


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def main():
    if os.getenv("TEST_MESSAGE", "").lower() == "true":
        ok = send_telegram(f"✅ ทดสอบแจ้งเตือน {LABEL} BB Alert ทำงานปกติ")
        sys.exit(0 if ok else 1)

    frames = fetch_frames()
    if not frames or "H1" not in frames:
        print("ดึงข้อมูลไม่สำเร็จ (อาจเป็นช่วงตลาดปิดหรือ Yahoo ขัดข้อง) ข้ามรอบนี้")
        return

    h1 = frames["H1"]
    print(f"โหมด={CHECK_MODE} | TF={','.join(USE_TFS)} | แท่ง H1 ล่าสุด (UTC): {h1.index[-1]} | "
          f"ปิด={float(h1['Close'].iloc[-1]):.2f} | "
          f"เวลารัน (UTC): {pd.Timestamp.now('UTC'):%Y-%m-%d %H:%M}")
    state = load_state()
    print(f"สถานะที่จำไว้: up={state.get('up')} lo={state.get('lo')} init={state.get('init')}")
    msgs, new_state = evaluate(frames, state, int(time.time()))
    for m in msgs:
        if m["strong"]:
            for k in range(max(1, FULL_REPEAT)):
                send_telegram(m["text"], html=True)
                if k < FULL_REPEAT - 1:
                    time.sleep(FULL_REPEAT_GAP)
        else:
            send_telegram(m["text"])

    if new_state != state:
        STATE_FILE.write_text(json.dumps(new_state, indent=2))


if __name__ == "__main__":
    main()
