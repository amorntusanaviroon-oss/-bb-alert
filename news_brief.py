"""
News Brief -> Telegram
สรุปข่าวเศรษฐกิจสำคัญที่กระทบทอง (ค่าเริ่มต้น: USD, ผลกระทบสูง) พร้อมเวลาไทย
แหล่งข้อมูล: ปฏิทินสาธารณะของ ForexFactory (ฟีด JSON ฟรี ไม่มี API key)
ช่วงที่แสดง: จากเวลาที่รัน ไปอีก NEWS_HOURS ชั่วโมง (ค่าเริ่มต้น 24)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

FEEDS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
]
TZ = timezone(timedelta(hours=7))                     # เวลาไทย
CURRENCIES = [c.strip().upper() for c in os.getenv("NEWS_CURRENCIES", "USD").split(",") if c.strip()]
IMPACTS = [i.strip().lower() for i in os.getenv("NEWS_IMPACTS", "High").split(",") if i.strip()]
HOURS = int(os.getenv("NEWS_HOURS", "24"))
LABEL = os.getenv("SYMBOL_LABEL", "XAUUSD")

# --- โหมดเตือนล่วงหน้า (python news_brief.py remind) ---
NEWS_STATE = Path(os.getenv("NEWS_STATE_FILE", "news_state.json"))
REMIND_MINUTES = sorted({int(x) for x in os.getenv("REMIND_MINUTES", "30").split(",") if x.strip()},
                        reverse=True)                     # เช่น "30,10" = เตือน 2 ครั้ง
REFRESH_HOURS = int(os.getenv("NEWS_REFRESH_HOURS", "6"))  # ดึงฟีดใหม่ทุกกี่ชั่วโมง (กันโดนจำกัดการเรียก)

TH_DAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
TH_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
             "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]

# ---------------- ระดับความรุนแรงของข่าว (สี) ----------------
#  🟣 รุนแรงมาก = ข่าว High ที่เป็นตัวขยับทองแรงๆ (ดอกเบี้ย Fed, NFP, CPI, PCE, ประธาน Fed)
#  🔴 สูง       = ข่าว High อื่นๆ
#  🟠 ปานกลาง   = ข่าว Medium
#  🟡 เบา       = ข่าว Low
SEV_ICON = {1: "🟡", 2: "🟠", 3: "🔴", 4: "🟣"}
SEV_NAME = {1: "เบา", 2: "ปานกลาง", 3: "สูง", 4: "รุนแรงมาก"}
KEYWORDS = [k.strip().lower() for k in os.getenv(
    "NEWS_KEYWORDS",
    "non-farm,cpi,fomc,federal funds rate,fed chair,pce").split(",") if k.strip()]
LEGEND = "🟡 เบา · 🟠 ปานกลาง · 🔴 สูง · 🟣 รุนแรงมาก"


def news_severity(ev):
    impact = ev.get("impact", "")
    if impact == "high":
        title = str(ev.get("title", "")).lower()
        if any(re.search(r"\b" + re.escape(k) + r"\b", title) for k in KEYWORDS):
            return 4
        return 3
    return {"medium": 2, "low": 1}.get(impact, 1)


def fetch_events():
    last_err = None
    for url in FEEDS:
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    return r.json()
                last_err = f"{url} -> HTTP {r.status_code}"
            except Exception as e:      # noqa: BLE001
                last_err = f"{url} -> {e}"
            time.sleep(3)
    print("ดึงปฏิทินข่าวไม่สำเร็จ:", last_err)
    return None


def parse_events(raw, now, hours=None):
    """กรองข่าวตามสกุลเงิน/ผลกระทบ ในช่วง now .. now+hours คืน list เรียงตามเวลา"""
    end = now + timedelta(hours=hours or HOURS)
    out = []
    for e in raw:
        try:
            if str(e.get("country", "")).upper() not in CURRENCIES:
                continue
            if str(e.get("impact", "")).lower() not in IMPACTS:
                continue
            t = datetime.fromisoformat(e["date"]).astimezone(TZ)
        except Exception:               # noqa: BLE001
            continue
        if now <= t <= end:
            out.append({"time": t, "title": e.get("title", "?"),
                        "cur": str(e.get("country", "")).upper(),
                        "impact": str(e.get("impact", "")).lower(),
                        "forecast": (e.get("forecast") or "").strip(),
                        "previous": (e.get("previous") or "").strip()})
    out.sort(key=lambda x: x["time"])
    return out


def format_message(events, now):
    head = (f"📰 ข่าวที่กระทบ {LABEL} | {TH_DAYS[now.weekday()]} {now.day} {TH_MONTHS[now.month - 1]}\n"
            f"({'+'.join(CURRENCIES)} · {'/'.join(i.capitalize() for i in IMPACTS)} · เวลาไทย)")
    if not events:
        return head + "\n\nไม่มีข่าวสำคัญในช่วง 24 ชม.นี้"
    lines = [head, ""]
    for ev in events:
        day_note = ""
        if ev["time"].date() != now.date():
            day_note = " (เลยเที่ยงคืน)"
        detail = []
        if ev["forecast"]:
            detail.append(f"คาด {ev['forecast']}")
        if ev["previous"]:
            detail.append(f"ก่อนหน้า {ev['previous']}")
        tail = f"  [{' | '.join(detail)}]" if detail else ""
        icon = SEV_ICON[news_severity(ev)]
        lines.append(f"{icon} {ev['time']:%H:%M}{day_note}  {ev['cur']} {ev['title']}{tail}")
    top = max(news_severity(e) for e in events)
    lines += ["", f"แรงสุดวันนี้: {SEV_ICON[top]} {SEV_NAME[top]}", LEGEND]
    return "\n".join(lines)


def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    print(text)
    if not token or not chat_id:
        print("(ยังไม่ได้ตั้ง TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID จึงไม่ได้ส่ง)")
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text}, timeout=20)
    if not r.ok:
        print("Telegram error:", r.status_code, r.text)
    return r.ok


# ---------------- เตือนล่วงหน้าก่อนข่าวออก ----------------
def _event_id(ev):
    return f"{ev['time'].isoformat()}|{ev['cur']}|{ev['title']}"


def _load_news_state():
    if NEWS_STATE.exists():
        try:
            return json.loads(NEWS_STATE.read_text(encoding="utf-8"))
        except Exception:               # noqa: BLE001
            pass
    return {}


def remind(now=None):
    now = now or datetime.now(TZ)
    state = _load_news_state()
    before = json.dumps(state, sort_keys=True)

    # ดึงฟีดใหม่เฉพาะเมื่อแคชเก่าเกิน REFRESH_HOURS
    if time.time() - state.get("fetched_at", 0) > REFRESH_HOURS * 3600:
        raw = fetch_events()
        if raw is not None:
            evs = parse_events(raw, now - timedelta(hours=1), hours=24 * 8)
            state["events"] = [{**e, "time": e["time"].isoformat()} for e in evs]
            state["fetched_at"] = int(time.time())
        elif "events" not in state:
            return

    events = [{**e, "time": datetime.fromisoformat(e["time"])} for e in state.get("events", [])]
    reminded = set(state.get("reminded", []))

    due = {}
    for ev in events:
        mins = (ev["time"] - now).total_seconds() / 60
        if mins <= 0:
            continue
        keys = [f"{_event_id(ev)}@{m}" for m in REMIND_MINUTES if mins <= m]
        new = [k for k in keys if k not in reminded]
        if new:
            reminded.update(new)
            due.setdefault(ev["time"], []).append(ev)

    for t in sorted(due):
        group = due[t]
        mins = max(1, round((t - now).total_seconds() / 60))
        top = max(news_severity(e) for e in group)
        siren = "🚨🚨🚨 " if top >= 4 else ""
        lines = [f"{siren}⏰{SEV_ICON[top]} อีกประมาณ {mins} นาที ข่าว{SEV_NAME[top]} ({t:%H:%M} เวลาไทย)"]
        for ev in group:
            detail = []
            if ev["forecast"]:
                detail.append(f"คาด {ev['forecast']}")
            if ev["previous"]:
                detail.append(f"ก่อนหน้า {ev['previous']}")
            tail = f"  [{' | '.join(detail)}]" if detail else ""
            lines.append(f"{SEV_ICON[news_severity(ev)]} {ev['cur']} {ev['title']}{tail}")
        lines.append("⚠️ ระวังราคาแกว่งแรงและสเปรดกว้าง")
        send_telegram("\n".join(lines))

    live_ids = {_event_id(e) for e in events}
    state["reminded"] = sorted(k for k in reminded if k.rsplit("@", 1)[0] in live_ids)

    if json.dumps(state, sort_keys=True) != before:
        NEWS_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    now = datetime.now(TZ)
    raw = fetch_events()
    if raw is None:
        send_telegram("⚠️ ดึงปฏิทินข่าววันนี้ไม่สำเร็จ (แหล่งข้อมูลอาจขัดข้องชั่วคราว)")
        return
    events = parse_events(raw, now)
    if not events and now.weekday() >= 5:      # เสาร์-อาทิตย์ที่ไม่มีข่าว ไม่ต้องส่ง
        print("วันหยุดและไม่มีข่าว ข้าม")
        return
    send_telegram(format_message(events, now))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "remind":
        remind()
    else:
        main()
