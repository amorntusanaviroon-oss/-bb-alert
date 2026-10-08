"""
BB Breakout Alert -> Telegram
ตรวจราคาทองเทียบ Bollinger Bands บน M30 / H1 / H4 / D1 แล้วส่งแจ้งเตือนเข้า Telegram
บอกระดับว่าหลุดกี่ TF / แจ้งเมื่อหลุดต่อเนื่องหลายแท่ง / แจ้งซ้ำเมื่อหลุดไปไกลมาก / เตือนย้ำ
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
from datetime import datetime, timedelta, timezone
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
# ต้องเห็นการหลุดติดกันกี่รอบก่อนแจ้ง (1 = แจ้งทันที, 2 = กันข้อมูลเพี้ยนครั้งเดียวจาก Yahoo แต่แจ้งช้าลง 1 รอบ)
CONFIRM_RUNS = int(os.getenv("CONFIRM_RUNS", "1"))
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

# หลุดต่อเนื่อง: TF ที่หลุดอยู่ แล้วแท่งถัดไปยังหลุดต่อโดยไม่กลับเข้ากรอบ จะแจ้ง "แท่งที่ N"
CONT_ENABLED = _bool("CONT_ENABLED", "true")
CONT_TFS = _list("CONT_TFS", "H1,H4,D1")                  # M30 ถี่เกินจึงไม่ใส่

# เตือนย้ำ: ถ้ายังหลุดอยู่และไม่มีข้อความใหม่นานเกินกำหนด จะส่งสรุปสถานะอีกครั้ง
REPEAT_HOURS = float(os.getenv("REPEAT_HOURS", "1"))          # 0 = ปิดเตือนย้ำ
REPEAT_SEC = int(REPEAT_HOURS * 3600)
REPEAT_MIN_LEVEL = int(os.getenv("REPEAT_MIN_LEVEL", "2"))    # เตือนย้ำเฉพาะระดับ >= นี้ (2 = 🟠)
# นับจำนวนครั้งที่หลุด (หลุด-กลับ-หลุดใหม่) ภายในช่วงเวลานี้ แล้วแจ้งในข้อความ เช่น "หลุดครั้งที่ 2 ใน 2 ชม."
WINDOW_HOURS = float(os.getenv("REPEAT_WINDOW_HOURS", "2"))
WINDOW_SEC = int(WINDOW_HOURS * 3600)

# คำสั่งใน Telegram (/trend): ตอบในรอบถัดไปที่ระบบรัน (ช้าได้ไม่เกินความถี่ของ cron-job.org)
CMD_STALE_SEC = int(os.getenv("CMD_STALE_SEC", "1800"))   # ข้ามคำสั่งที่เก่าเกินกี่วินาที (กันตอบย้อนหลังหลังระบบหยุด)
TZ_TH = timezone(timedelta(hours=7))

# ตำแหน่งบิตเดิมคงไว้ เพื่อให้ state.json เก่ายังใช้ได้
TF_BIT = {"H1": 0, "H4": 1, "D1": 2, "M30": 3, "M15": 4, "M5": 5}
DISPLAY = ["M5", "M15", "M30", "H1", "H4", "D1"]
COLS = ["High", "Low", "Close"]

# ---------------- ระดับความรุนแรง (สี) ----------------
# น้ำหนักตาม TF: ยิ่ง TF ใหญ่ ยิ่งสำคัญ แล้วรวมคะแนนของ TF ที่หลุดอยู่
TF_WEIGHT = {"M5": 0.5, "M15": 0.75, "M30": 1, "H1": 2, "H4": 4, "D1": 8}
SEV_ICON = {1: "🟡", 2: "🟠", 3: "🔴", 4: "🟣"}
SEV_NAME = {1: "เบา", 2: "ปานกลาง", 3: "สูง", 4: "รุนแรงมาก"}
STRONG_LEVEL = int(os.getenv("STRONG_LEVEL", "4"))   # ระดับที่ส่งแบบเด่น 🚨 ซ้ำหลายครั้ง


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
    for tf, (interval, period) in (("M15", ("15m", "60d")), ("M5", ("5m", "60d"))):
        if tf in USE_TFS:
            d = _download(interval, period)
            if d is not None:
                out[tf] = d[COLS]
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


def fmt_dur(sec):
    m = max(0, int(sec // 60))
    h, m = divmod(m, 60)
    return f"{h} ชม. {m} น." if h else f"{m} น."


def severity(mask, far=False):
    """ระดับ 1-4 จาก TF ที่หลุด; far=True (หลุดไกลมาก) ขยับขึ้น 1 ระดับ
    คะแนน: M30=1 H1=2 H4=4 D1=8
      1-2 -> 🟡 เบา | 3-5 -> 🟠 ปานกลาง | 6-9 -> 🔴 สูง | 10+ -> 🟣 รุนแรงมาก"""
    score = sum(TF_WEIGHT[tf] for tf in DISPLAY if mask & (1 << TF_BIT[tf]))
    if score <= 2:
        lvl = 1
    elif score <= 5:
        lvl = 2
    elif score <= 9:
        lvl = 3
    else:
        lvl = 4
    return min(4, lvl + 1) if far else lvl


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

    raw_up, raw_lo = up_mask, lo_mask
    if CONFIRM_RUNS >= 2 and inited:
        # หลุดใหม่ต้องเห็นซ้ำในรอบก่อนหน้าด้วย (หรือเป็น TF ที่ยืนยันแล้ว) จึงจะนับ
        up_mask &= state.get("raw_up", 0) | prev_up
        lo_mask &= state.get("raw_lo", 0) | prev_lo
        if (raw_up, raw_lo) != (up_mask, lo_mask):
            print(f"(รอยืนยันอีก 1 รอบ: up={raw_up & ~up_mask} lo={raw_lo & ~lo_mask})")

    new_state = {
        "raw_up": raw_up, "raw_lo": raw_lo,
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

    # ---- สถานะ "หลุดอยู่จริงตอนนี้" และเวลาที่เริ่มหลุด (ใช้กับเตือนย้ำ) ----
    cur = {"up": 0, "lo": 0}
    for tf, r in res.items():
        bit = 1 << TF_BIT[tf]
        if r["price"] > r["upper"]:
            cur["up"] |= bit
        if r["price"] < r["lower"]:
            cur["lo"] |= bit

    for side, mask, prev in (("up", up_mask, prev_up), ("lo", lo_mask, prev_lo)):
        if mask == 0:
            new_state[f"since_{side}"] = new_state[f"sent_{side}"] = new_state[f"px_{side}"] = 0
        elif prev == 0 or not state.get(f"since_{side}"):
            new_state[f"since_{side}"] = now
            new_state[f"sent_{side}"] = now
            new_state[f"px_{side}"] = last_price
        else:
            for k in ("since", "sent", "px"):
                new_state[f"{k}_{side}"] = state.get(f"{k}_{side}", 0)

    # ---- หลุดต่อเนื่อง: แท่งใหม่เริ่มแล้ว TF เดิมยังหลุดอยู่ (ไม่เคยกลับเข้ากรอบ) ----
    def bar_ts(tf):
        idx = frames[tf].index
        return str(idx[-2] if (CHECK_MODE == "closed" and len(idx) > 1) else idx[-1])

    cont_hits = {"up": [], "lo": []}
    for side, mask, prev, key in (("up", up_mask, prev_up, "cont_up"),
                                  ("lo", lo_mask, prev_lo, "cont_lo")):
        old_c = state.get(key, {}) if inited else {}
        new_state[key] = {}
        for tf in res:
            bit = 1 << TF_BIT[tf]
            if not (mask & bit):
                continue                              # กลับเข้ากรอบแล้ว: ล้างตัวนับ
            ts = bar_ts(tf)
            o = old_c.get(tf)
            if not (prev & bit) or not o:
                new_state[key][tf] = {"ts": ts, "n": 1}
            elif o["ts"] != ts:
                n = o["n"] + 1
                new_state[key][tf] = {"ts": ts, "n": n}
                if inited and CONT_ENABLED and tf in CONT_TFS:
                    cont_hits[side].append((tf, n))
            else:
                new_state[key][tf] = o

    # ---- ประวัติการหลุดรอบใหม่ (นับแม้ข้อความถูกกัน cooldown) ----
    hist = {}
    for side, mask, prev in (("up", up_mask, prev_up), ("lo", lo_mask, prev_lo)):
        h = [t for t in state.get(f"hist_{side}", []) if now - t < WINDOW_SEC] if inited else []
        if inited and (mask & ~prev):
            h.append(now)
        hist[side] = h
        new_state[f"hist_{side}"] = h

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

    def is_far(mask, up):
        zk = "z_up" if up else "z_dn"
        return any(esc_level(res[tf][zk]) >= 1
                   for tf in res if tf in ESC_TFS and mask & (1 << TF_BIT[tf]))

    def build(mask, up):
        lvl = severity(mask, is_far(mask, up))
        icon, sev = SEV_ICON[lvl], SEV_NAME[lvl]
        arrow, side = ("🔺", "เหนือ BB บน") if up else ("🔻", "ต่ำกว่า BB ล่าง")
        full_bits = [1 << TF_BIT[t] for t in FULL_TFS]
        full = len(full_bits) >= 2 and all(mask & b for b in full_bits)
        detail = f"หลุด {bits(mask)}/{total} TF: {mask_text(mask)} | ราคา {last_price:.2f}"
        trig = ""
        if CHECK_MODE == "wick":
            parts = []
            for tf in DISPLAY:
                if tf in res and mask & (1 << TF_BIT[tf]):
                    r = res[tf]
                    parts.append(f"{tf} สูงสุด {r['hi']:.1f} (บน {r['upper']:.1f})" if up
                                 else f"{tf} ต่ำสุด {r['lo']:.1f} (ล่าง {r['lower']:.1f})")
            trig = "\n└ " + " · ".join(parts) if parts else ""
        n = len(hist["up" if up else "lo"])
        if n >= 2:
            detail += f" | ⚠️ หลุดครั้งที่ {n} ใน {WINDOW_HOURS:g} ชม."
        if full or lvl >= STRONG_LEVEL:
            text = (f"🚨🚨🚨 <b>{icon} {LABEL} {side} | ความรุนแรง: {sev}</b> 🚨🚨🚨\n"
                    f"{arrow} {detail}{trig}")
            return {"text": text, "strong": True}
        text = f"{icon}{arrow} {LABEL} {side} | ความรุนแรง: {sev} | {detail}{trig}"
        return {"text": text, "strong": False}

    sent_now = {"up": False, "lo": False}
    if fresh(up_mask, prev_up, "last_up"):
        msgs.append(build(up_mask, True))
        sent_now["up"] = True
    if fresh(lo_mask, prev_lo, "last_lo"):
        msgs.append(build(lo_mask, False))
        sent_now["lo"] = True

    for side, name, mask in (("up", "บน", up_mask), ("lo", "ล่าง", lo_mask)):
        if esc_hits[side] and not cont_hits[side]:    # ถ้ามีข้อความ "หลุดต่อเนื่อง" อยู่แล้ว จะรวม σ ไว้ในข้อความนั้น
            lvl = severity(mask, far=True)
            detail = ", ".join(f"{tf} {z:.1f}σ" for tf, z in esc_hits[side])
            text = (f"🔥{SEV_ICON[lvl]} {LABEL} ไปไกลจาก BB {name} มาก | "
                    f"ความรุนแรง: {SEV_NAME[lvl]} | {detail} | ราคา {last_price:.2f}")
            msgs.append({"text": text, "strong": lvl >= STRONG_LEVEL})
            sent_now[side] = True

    for side, up, mask in (("up", True, up_mask), ("lo", False, lo_mask)):
        if cont_hits[side]:
            lvl = severity(mask, is_far(mask, up))
            arrow, where = ("🔺", "BB บน") if up else ("🔻", "BB ล่าง")
            detail = ", ".join(f"{tf} แท่งที่ {n}" for tf, n in cont_hits[side])
            if esc_hits[side]:
                detail += " (ไกล " + ", ".join(f"{tf} {z:.1f}σ" for tf, z in esc_hits[side]) + ")"
            text = (f"🔂{SEV_ICON[lvl]}{arrow} {LABEL} หลุดต่อเนื่อง {where} | {detail} | "
                    f"ความรุนแรง: {SEV_NAME[lvl]} | หลุด {bits(mask)}/{total} TF: {mask_text(mask)} | "
                    f"ราคา {last_price:.2f}")
            msgs.append({"text": text, "strong": False})
            sent_now[side] = True

    # ข้อความใดๆ ที่ส่งไปแล้ว ให้เริ่มนับเวลาเตือนย้ำใหม่
    for side in ("up", "lo"):
        if sent_now[side]:
            new_state[f"sent_{side}"] = now

    # ---- เตือนย้ำ: ยังหลุดอยู่จริง และเงียบมานานเกิน REPEAT_HOURS ----
    if REPEAT_SEC > 0:
        for side, up, mask_all in (("up", True, up_mask), ("lo", False, lo_mask)):
            mask = cur[side]
            if not mask or not mask_all or sent_now[side]:
                continue
            if now - new_state[f"sent_{side}"] < REPEAT_SEC:
                continue
            lvl = severity(mask, is_far(mask, up))
            if lvl < REPEAT_MIN_LEVEL:
                continue
            arrow, where = ("🔺", "เหนือ BB บน") if up else ("🔻", "ต่ำกว่า BB ล่าง")
            diff = last_price - new_state[f"px_{side}"]
            text = (f"🔁{SEV_ICON[lvl]}{arrow} {LABEL} ยังอยู่{where} | ความรุนแรง: {SEV_NAME[lvl]} | "
                    f"หลุด {bits(mask)}/{total} TF: {mask_text(mask)} | "
                    f"นาน {fmt_dur(now - new_state[f'since_{side}'])} | "
                    f"ราคา {last_price:.2f} ({diff:+.1f} จากตอนหลุด)")
            msgs.append({"text": text, "strong": False})
            new_state[f"sent_{side}"] = now

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


# ---------------- คำสั่งใน Telegram: /trend ----------------
def swings(df, n=2, look=120):
    """คืน (swing highs, swing lows) ที่ยืนยันแล้ว (มีแท่งประกบ n แท่งทั้งสองข้าง) จากไส้เทียน"""
    h = df["High"].values[-look:]
    l = df["Low"].values[-look:]
    sh, sl = [], []
    for i in range(n, len(h) - n):
        if h[i] > max(h[i - n:i]) and h[i] > max(h[i + 1:i + n + 1]):
            sh.append(float(h[i]))
        if l[i] < min(l[i - n:i]) and l[i] < min(l[i + 1:i + n + 1]):
            sl.append(float(l[i]))
    return sh, sl


def calc_adx(df, n=14):
    """ADX (Wilder) วัดความแรงของเทรนด์ ไม่บอกทิศทาง: <20 อ่อน/ไซด์เวย์, 20-30 เริ่มมีเทรนด์, >30 แรง"""
    h, l, c = df["High"], df["Low"], df["Close"]
    up, dn = h.diff(), -l.diff()
    plus = up.where((up > dn) & (up > 0), 0.0)
    minus = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = (100 * (pdi - mdi).abs() / (pdi + mdi)).fillna(0.0)
    return float(dx.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


STRENGTH_WORD = {1: "อ่อน", 2: "ปานกลาง", 3: "แรง", 4: "แรงมาก"}


def trend_for(df):
    """เทรนด์ของ 1 TF
    base   : +1 ขึ้น / -1 ลง / 0 ไซด์เวย์ (EMA20 เทียบ EMA50 และราคาเทียบ EMA50)
    level  : ความแรง 1-4 (0 ถ้าไซด์เวย์) นับจากคะแนน
             ADX>=20 (+1), ADX>=30 (+1 เพิ่ม), โครงสร้าง swing ตรงทิศ (+1), ราคาห่างเส้นกลาง BB >=1σ ตามทิศ (+1)
    struct : HH·HL / LH·LL / ผสม"""
    close = df["Close"]
    if len(close) < 60:
        return None
    e20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    e50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    price = float(close.iloc[-1])
    if e20 > e50 and price > e50:
        base = 1
    elif e20 < e50 and price < e50:
        base = -1
    else:
        base = 0
    sh, sl = swings(df)
    struct = "ผสม"
    if len(sh) >= 2 and len(sl) >= 2:
        hh, hl = sh[-1] > sh[-2], sl[-1] > sl[-2]
        if hh and hl:
            struct = "HH·HL"
        elif not hh and not hl:
            struct = "LH·LL"
    ma = float(close.rolling(BB_PERIOD).mean().iloc[-1])
    sd = float(close.rolling(BB_PERIOD).std(ddof=0).iloc[-1])
    z = (price - ma) / sd if sd > 0 else 0.0
    adx = calc_adx(df)

    level = 0
    if base != 0:
        pts = (adx >= 20) + (adx >= 30)
        pts += (struct == ("HH·HL" if base == 1 else "LH·LL"))
        pts += (z >= 1 if base == 1 else z <= -1)
        level = min(4, max(1, int(pts)))
    return {"base": base, "struct": struct, "z": z, "adx": adx, "level": level}


def trend_line(tf, r):
    if r["base"] == 0:
        return f"{tf}  ⚪⚪⚪⚪ ไซด์เวย์ · ADX {r['adx']:.0f}"
    dot = "🟢" if r["base"] == 1 else "🔴"
    bar = dot * r["level"] + "⚪" * (4 - r["level"])
    name = "ขาขึ้น" if r["base"] == 1 else "ขาลง"
    return (f"{tf}  {bar} {name}{STRENGTH_WORD[r['level']]} · {r['struct']} · "
            f"ADX {r['adx']:.0f} · BB {r['z']:+.1f}σ")


def trend_summary(res):
    """สรุปภาพรวมจากกลุ่ม TF ใหญ่ (H4, D1) เทียบกับกลุ่ม TF เล็ก (M30, H1)"""
    def group(tfs):
        rs = [res[t] for t in tfs if t in res]
        if not rs:
            return None, 0
        if all(r["base"] == rs[0]["base"] for r in rs):
            return rs[0]["base"], min(r["level"] for r in rs)
        return 0, 0
    (big, big_lv), (small, small_lv) = group(["H4", "D1"]), group(["M30", "H1"])
    if big is None or small is None:
        return "ข้อมูลบาง TF ไม่พอ"
    name = {1: "ขาขึ้น", -1: "ขาลง", 0: "ไม่ชัด/ไซด์เวย์"}
    big_txt = name[big] + (f"({STRENGTH_WORD[big_lv]})" if big else "")
    if big != 0 and big == small:
        return f"ทุก TF ไปทางเดียวกัน: {big_txt}"
    if big != 0 and small == -big:
        return (f"TF ใหญ่{big_txt} แต่ TF เล็กสวนทาง → "
                f"{'เด้งในขาลง' if big == -1 else 'ย่อในขาขึ้น'}")
    if big != 0 and small == 0:
        return f"TF ใหญ่{big_txt} ส่วน TF เล็กยังไม่ชัด"
    if big == 0 and small != 0:
        return f"TF ใหญ่ยังไม่ชัด ส่วน TF เล็ก{name[small]}"
    return "สัญญาณผสม ยังไม่ชัด"


def micro_summary(res):
    """บรรทัดสรุปของ TF สั้นมาก (M5, M15) ถ้ามีข้อมูล"""
    rs = [res[t] for t in ("M5", "M15") if t in res]
    if not rs:
        return ""
    if all(r["base"] == rs[0]["base"] for r in rs):
        b = rs[0]["base"]
        txt = "ไซด์เวย์" if b == 0 else ("ขาขึ้น" if b == 1 else "ขาลง") + f"({STRENGTH_WORD[min(r['level'] for r in rs)]})"
    else:
        txt = "สวนทางกันเอง ยังไม่ชัด"
    return f"ระยะสั้นมาก (M5·M15): {txt}"


def build_trend_message(frames):
    res = {}
    for tf in DISPLAY:
        if tf in frames:
            r = trend_for(frames[tf])
            if r:
                res[tf] = r
    price = float(frames["H1"]["Close"].iloc[-1])
    now_th = datetime.now(TZ_TH)
    lines = [f"📊 {LABEL} เทรนด์ทุก TF",
             f"ราคา {price:.1f} (Yahoo) · {now_th:%H:%M} น.", ""]
    for tf in DISPLAY:
        lines.append(trend_line(tf, res[tf]) if tf in res else f"{tf}  ข้อมูลไม่พอ")
    micro = micro_summary(res)
    lines += ["", f"สรุป: {trend_summary(res)}"]
    if micro:
        lines.append(micro)
    lines.append("จุดสี 1-4 = ความแรงเทรนด์ (ADX+โครงสร้าง+BB) · ไม่ใช่คำแนะนำเทรด")
    return "\n".join(lines)


HELP_TEXT = ("คำสั่งที่ใช้ได้\n/trend  สรุปเทรนด์ทุก TF\n/help  แสดงคำสั่ง\n"
             "(บอทตอบในรอบถัดไปที่ระบบรัน อาจช้าไม่กี่นาที)")


def parse_cmd(text):
    t = (text or "").strip().lower()
    if not t:
        return None
    first = t.split()[0].split("@")[0]
    if first in ("/trend", "/t", "trend", "เทรนด์", "เทรน", "สรุป"):
        return "trend"
    if first in ("/help", "/start", "help"):
        return "help"
    return None


def tg_api(method, **params):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=15)
        return r.json() if r.ok else None
    except Exception as e:                      # noqa: BLE001
        print(f"Telegram {method} error: {e}")
        return None


def handle_commands(frames, state, now):
    """อ่านข้อความใหม่จากแชทของเรา ตอบคำสั่ง แล้วคืน dict ที่ต้องเก็บใน state"""
    out = {}
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id or not os.getenv("TELEGRAM_BOT_TOKEN"):
        return out
    if not state.get("cmds_set"):
        ok = tg_api("setMyCommands", commands=[
            {"command": "trend", "description": "สรุปเทรนด์ทุก TF"},
            {"command": "help", "description": "แสดงคำสั่งที่ใช้ได้"}])
        if ok and ok.get("ok"):
            out["cmds_set"] = True
    params = {"timeout": 0, "allowed_updates": ["message"]}
    if state.get("tg_offset"):
        params["offset"] = state["tg_offset"]
    res = tg_api("getUpdates", **params)
    if not res or not res.get("ok") or not res.get("result"):
        return out
    updates = res["result"]
    out["tg_offset"] = max(u["update_id"] for u in updates) + 1
    wanted = set()
    for u in updates:
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(chat_id):
            continue                              # ไม่ตอบแชทอื่น
        if now - m.get("date", 0) > CMD_STALE_SEC:
            continue                              # คำสั่งเก่าเกินไป
        cmd = parse_cmd(m.get("text"))
        if cmd:
            wanted.add(cmd)
    if "trend" in wanted:
        frames = dict(frames)
        for tf, (interval, period) in (("M15", ("15m", "60d")), ("M5", ("5m", "60d"))):
            if tf not in frames:                      # ดึงเฉพาะตอนมีคนสั่ง ไม่ให้รอบปกติช้าลง
                d = _download(interval, period)
                if d is not None:
                    frames[tf] = d[COLS]
        send_telegram(build_trend_message(frames))
    if "help" in wanted:
        send_telegram(HELP_TEXT)
    return out


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
    print("H1 ล่าสุด 3 แท่ง (เวลา UTC | High / Low / Close):")
    for ts, row in h1.tail(3).iterrows():
        print(f"  {ts:%m-%d %H:%M} | {row['High']:.2f} / {row['Low']:.2f} / {row['Close']:.2f}")
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

    # คำสั่งจาก Telegram (/trend, /help) และจำตำแหน่งข้อความที่อ่านแล้ว
    for k in ("tg_offset", "cmds_set"):
        if k in state:
            new_state[k] = state[k]
    new_state.update(handle_commands(frames, state, int(time.time())))

    if new_state != state:
        STATE_FILE.write_text(json.dumps(new_state, indent=2))


if __name__ == "__main__":
    main()
