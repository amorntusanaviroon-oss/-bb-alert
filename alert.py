"""
BB Breakout Alert -> Telegram
ตรวจราคาทองเทียบ Bollinger Bands บน H1 / H4 / D1 แล้วส่งแจ้งเตือนเข้า Telegram
บอกระดับว่าหลุดกี่ TF (เหมือน EA ตัว MT5)
รันบน GitHub Actions ทุก ~5 นาที สถานะเก็บใน state.json
"""
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

# ---------------- ตั้งค่า (แก้ผ่าน env / workflow ได้) ----------------
TICKER = os.getenv("TICKER", "GC=F")              # ทองฟิวเจอร์ส; ลอง "XAUUSD=X" ได้
LABEL = os.getenv("SYMBOL_LABEL", "XAUUSD")
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_DEV = float(os.getenv("BB_DEV", "2.0"))
CHECK_MODE = os.getenv("CHECK_MODE", "live")      # live = แท่งที่กำลังวิ่ง | closed = แท่งที่ปิดแล้ว
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "900"))
NOTIFY_RETURN = os.getenv("NOTIFY_RETURN", "false").lower() == "true"
USE_TFS = [t.strip() for t in os.getenv("USE_TFS", "H1,H4,D1").split(",") if t.strip()]
H4_OFFSET_HOURS = int(os.getenv("H4_OFFSET_HOURS", "1"))  # XM ฤดูร้อน (UTC+3)=1, ฤดูหนาว (UTC+2)=2
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
FULL_REPEAT = int(os.getenv("FULL_REPEAT", "3"))      # จำนวนครั้งที่ส่งซ้ำเมื่อหลุดครบทุก TF
FULL_REPEAT_GAP = int(os.getenv("FULL_REPEAT_GAP", "4"))  # หน่วงระหว่างข้อความ (วินาที)

TF_ORDER = ["H1", "H4", "D1"]


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


def fetch_closes():
    """คืน dict {'H1': Series, 'H4': Series, 'D1': Series} ของราคาปิด"""
    out = {}
    h1 = _download("1h", "60d")
    if h1 is None:
        return None
    out["H1"] = h1["Close"]
    out["H4"] = (h1["Close"]
                 .resample("4h", offset=f"{H4_OFFSET_HOURS}h")
                 .last().dropna())
    d1 = _download("1d", "1y")
    if d1 is not None:
        out["D1"] = d1["Close"]
    return out


# ---------------- คำนวณ ----------------
def band_state(close, live_price):
    """คืน (state, price, upper, lower)  state: 1 เหนือบน, -1 ต่ำกว่าล่าง, 0 ในกรอบ, None ข้อมูลไม่พอ"""
    if CHECK_MODE == "closed":
        close = close.iloc[:-1]           # ตัดแท่งที่ยังไม่ปิด
    if len(close) < BB_PERIOD:
        return None, None, None, None
    ma = close.rolling(BB_PERIOD).mean()
    sd = close.rolling(BB_PERIOD).std(ddof=0)   # ใช้ std แบบเดียวกับ MT5
    upper = float(ma.iloc[-1] + BB_DEV * sd.iloc[-1])
    lower = float(ma.iloc[-1] - BB_DEV * sd.iloc[-1])
    price = float(close.iloc[-1]) if CHECK_MODE == "closed" else float(live_price)
    if price > upper:
        return 1, price, upper, lower
    if price < lower:
        return -1, price, upper, lower
    return 0, price, upper, lower


def mask_text(mask):
    return "+".join(tf for i, tf in enumerate(TF_ORDER) if mask & (1 << i))


def bits(mask):
    return bin(mask).count("1")


def evaluate(closes, state, now):
    """คืน (messages, new_state)"""
    live_price = float(closes["H1"].iloc[-1])
    up_mask = lo_mask = 0
    last_price = live_price
    for i, tf in enumerate(TF_ORDER):
        if tf not in USE_TFS or tf not in closes:
            continue
        st, price, _, _ = band_state(closes[tf], live_price)
        if st is None:
            continue
        last_price = price if CHECK_MODE == "closed" else live_price
        if st == 1:
            up_mask |= 1 << i
        elif st == -1:
            lo_mask |= 1 << i

    total = len([t for t in TF_ORDER if t in USE_TFS])
    new_state = {
        "up": up_mask, "lo": lo_mask,
        "last_up": dict(state.get("last_up", {})),
        "last_lo": dict(state.get("last_lo", {})),
        "init": True,
    }
    msgs = []

    if not state.get("init"):          # รันครั้งแรก: จำสถานะเฉยๆ
        return msgs, new_state

    prev_up, prev_lo = state.get("up", 0), state.get("lo", 0)

    def fresh(mask, prev, key):
        new = mask & ~prev
        for i, tf in enumerate(TF_ORDER):
            if new & (1 << i):
                if now - new_state[key].get(tf, 0) < COOLDOWN_SEC:
                    new &= ~(1 << i)
                else:
                    new_state[key][tf] = now
        return new

    def build(mask, up):
        full = total >= 2 and bits(mask) == total
        if full:
            side = "เหนือ BB บน" if up else "ต่ำกว่า BB ล่าง"
            icon = "🟢" if up else "🔴"
            text = (f"🚨🚨🚨 <b>{LABEL} หลุด {side} ครบ {total}/{total} TF</b> 🚨🚨🚨\n"
                    f"{icon} {mask_text(mask)} | ราคา {last_price:.2f}")
            return {"text": text, "strong": True}
        if up:
            text = (f"🔺 {LABEL} เหนือ BB บน | ระดับ {bits(mask)}/{total} | "
                    f"{mask_text(mask)} | ราคา {last_price:.2f}")
        else:
            text = (f"🔻 {LABEL} ต่ำกว่า BB ล่าง | ระดับ {bits(mask)}/{total} | "
                    f"{mask_text(mask)} | ราคา {last_price:.2f}")
        return {"text": text, "strong": False}

    if fresh(up_mask, prev_up, "last_up"):
        msgs.append(build(up_mask, True))
    if fresh(lo_mask, prev_lo, "last_lo"):
        msgs.append(build(lo_mask, False))

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

    closes = fetch_closes()
    if not closes or "H1" not in closes:
        print("ดึงข้อมูลไม่สำเร็จ (อาจเป็นช่วงตลาดปิดหรือ Yahoo ขัดข้อง) ข้ามรอบนี้")
        return

    state = load_state()
    msgs, new_state = evaluate(closes, state, int(time.time()))
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
