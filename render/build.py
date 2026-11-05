"""
Render the life dashboard to an 800x480 1-bit frame for the TRMNL DIY kit.

Outputs (in ../out):
  preview.png  - what the screen will look like (open it to check your design)
  screen.bin   - 48,000 bytes, 1 bit per pixel, MSB first, 1 = BLACK.
                 The device downloads this and draws it directly.

Run locally:   python render/build.py --mock     (no network calls, sample data)
Run for real:  LAT=.. LON=.. ICS_URL=.. python render/build.py
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
from playwright.sync_api import sync_playwright

W, H = 800, 480
FRAME_BYTES = W * H // 8  # 48,000
THRESHOLD = 160           # 0-255; pixels darker than this become black

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
OUT = REPO / "out"
TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Chicago"))
UNITS = os.getenv("UNITS", "fahrenheit")  # or "celsius"

WMO = {  # Open-Meteo weather codes -> short text
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorms", 96: "Thunderstorms", 99: "Thunderstorms",
}


# Inline SVG glyphs (stroke-only so they survive the 1-bit threshold at any size).
ICONS = {
    "sun": '<circle cx="24" cy="24" r="8.5"/><path d="M24 3v6M24 39v6M3 24h6M39 24h6M9.2 9.2l4.3 4.3M34.5 34.5l4.3 4.3M38.8 9.2l-4.3 4.3M13.5 34.5l-4.3 4.3"/>',
    "part": '<circle cx="17" cy="16" r="6.5"/><path d="M17 2v4M17 26v1M3 16h4M27 16h4M7.5 6.5l2.8 2.8M24 8.5l2.5-2.5"/><path d="M19 43h17a7.5 7.5 0 0 0 .5-15A10.5 10.5 0 0 0 17.5 30 6.5 6.5 0 0 0 19 43z"/>',
    "cloud": '<path d="M15 40h21a8.5 8.5 0 0 0 .6-17A11.5 11.5 0 0 0 14 25a7.5 7.5 0 0 0 1 15z"/>',
    "rain": '<path d="M15 34h21a8.5 8.5 0 0 0 .6-17A11.5 11.5 0 0 0 14 19a7.5 7.5 0 0 0 1 15z"/><path d="M17 39l-3 7M26 39l-3 7M35 39l-3 7"/>',
    "snow": '<path d="M15 34h21a8.5 8.5 0 0 0 .6-17A11.5 11.5 0 0 0 14 19a7.5 7.5 0 0 0 1 15z"/><path d="M15 43h6M18 40v6M30 43h6M33 40v6"/>',
    "fog": '<path d="M6 14h36M6 24h36M10 34h32M6 44h30"/>',
    "storm": '<path d="M15 32h21a8.5 8.5 0 0 0 .6-17A11.5 11.5 0 0 0 14 17a7.5 7.5 0 0 0 1 15z"/><path d="M26 36l-9 0 7 10"/>',
}


def wx_icon(code):
    g = ("sun" if code == 0 else "part" if code in (1, 2) else "cloud" if code == 3
         else "fog" if code in (45, 48) else "storm" if code >= 95
         else "snow" if code in (71, 73, 75, 77, 85, 86) else "rain")
    return ICONS[g]


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "life-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


# ---------- data sources ----------

def get_weather():
    lat, lon = os.getenv("LAT"), os.getenv("LON")
    if not (lat and lon):
        return None
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weather_code"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&temperature_unit={UNITS}&timezone=auto&forecast_days=1"
    )
    d = json.loads(http_get(url))
    return {
        "temp": round(d["current"]["temperature_2m"]),
        "cond": WMO.get(d["current"]["weather_code"], "—"),
        "hi": round(d["daily"]["temperature_2m_max"][0]),
        "lo": round(d["daily"]["temperature_2m_min"][0]),
        "rain": d["daily"]["precipitation_probability_max"][0],
        "unit": "°F" if UNITS == "fahrenheit" else "°C",
        "icon": wx_icon(d["current"]["weather_code"]),
    }


def _to_local(v, is_end=False):
    """ICS values can be dates (all-day) or datetimes (naive or aware)."""
    if isinstance(v, dt.datetime):
        return (v if v.tzinfo else v.replace(tzinfo=TZ)).astimezone(TZ), False
    return dt.datetime.combine(v, dt.time(), TZ), True


def _event(title, start, end, all_day):
    return {
        "title": title,
        "time": "All day" if all_day else start.strftime("%-I:%M %p"),
        "short": "" if all_day else start.strftime("%-I:%M").replace(":00", "") + start.strftime("%p")[0].lower(),
        "all_day": all_day,
        "h0": start.hour + start.minute / 60, "h1": end.hour + end.minute / 60 if end.date() == start.date() else 24,
        "sort": (0 if all_day else 1, start),
    }


def get_events(now, ndays=7):
    """Returns a list of ndays lists of events: [today, tomorrow, ...]. Past events today are dropped."""
    days = [[] for _ in range(ndays)]
    url = os.getenv("ICS_URL")
    if not url:
        return days
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(http_get(url))
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for e in recurring_ical_events.of(cal).between(day0, day0 + dt.timedelta(days=ndays)):
        start, all_day = _to_local(e["DTSTART"].dt)
        end = _to_local(e["DTEND"].dt)[0] if e.get("DTEND") else start
        if end <= now and not all_day:
            continue
        item = _event(str(e.get("SUMMARY", "(busy)")), start, end, all_day)
        for i in range(ndays):   # all-day events can span several days
            d0 = day0 + dt.timedelta(days=i)
            d1 = d0 + dt.timedelta(days=1)
            if (start < d1 and end > d0) or (not all_day and d0 <= start < d1):
                days[i].append(item)
                if not all_day:
                    break
    for d in days:
        d.sort(key=lambda x: x["sort"])
    return days


def get_macros(now, target):
    """MACROS var is "YYYY-MM-DD,kcal,protein,carbs,fat" from the iPhone Shortcut
    (MyFitnessPal -> Apple Health -> Shortcut). Stale days count as zero."""
    raw = (os.getenv("MACROS") or "").strip()
    if not raw:
        return None
    try:
        day, *vals = raw.split(",")
        k, p, c, f = (round(float(v)) for v in vals[:4])
    except ValueError:
        return None
    if day != now.strftime("%Y-%m-%d"):
        k = p = c = f = 0
    return _macro_rows(k, p, c, f, target)


def _macro_rows(k, p, c, f, target):
    t = target or {"kcal": 2400, "protein": "125", "carbs": 320, "fat": 70}
    tp = int(str(t["protein"]).split("-")[-1])
    rows = [("Calories", k, t["kcal"], ""), ("Protein", p, tp, "g"), ("Carbs", c, t["carbs"], "g"), ("Fat", f, t.get("fat", 70), "g")]
    return [{"name": n, "val": f"{v:,}", "goal": f"{g:,}", "unit": u, "pct": min(100, round(v * 100 / g)) if g else 0,
             "left": f"{max(0, g - v):,}{u} left" if v < g else "done"} for n, v, g, u in rows]


def get_steps(now):
    """STEPS var is "YYYY-MM-DD,1234", pushed by an iPhone Shortcut.
    A value from a previous day counts as 0 so the bar resets at midnight."""
    raw = (os.getenv("STEPS") or "").strip()
    goal = int(os.getenv("STEPS_GOAL") or 10000)
    if not raw:
        return None
    try:
        day, count = raw.split(",", 1)
        count = int(float(count))
    except ValueError:
        return None
    if day != now.strftime("%Y-%m-%d"):
        count = 0
    return {"count": f"{count:,}", "goal": f"{goal:,}", "pct": min(100, round(count * 100 / goal))}


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _session(plan, week, wk, dkey):
    """One day's session as {title, detail, kind}."""
    base = plan["weekly"][dkey]
    s = {"title": base["title"], "detail": base.get("detail", ""), "kind": base["kind"]}
    if base["kind"] == "run":
        s["detail"] = wk.get(base["key"], "")
    elif base["kind"] == "lift":
        if dkey in wk:                       # per-week override (e.g. race week)
            s = {"title": wk[dkey], "detail": "", "kind": "rest" if wk[dkey] == "Rest" else "lift"}
        elif wk.get("tag") == "deload":
            s["detail"] = plan["deload_lift"]
    return s


def get_training(now):
    p = REPO / "training.yaml"
    if not p.exists():
        return None
    plan = yaml.safe_load(p.read_text())
    start, race = plan["start"], plan["race"]
    today = now.date()
    days_to_race = (race - today).days

    if today < start:
        return {"pre": True, "starts": start.strftime("%b %-d"), "days_to_race": days_to_race}
    if today > race:
        return {"done": True}

    week = (today - start).days // 7 + 1
    wk = plan["weeks"].get(week, {})
    phase = next((n for a, b, n in plan["phases"] if a <= week <= b), "")
    dkey = DAYS[today.weekday()]
    tkey = DAYS[(today.weekday() + 1) % 7]
    tweek = week + 1 if tkey == "mon" else week

    s = _session(plan, week, wk, dkey)
    overridden = dkey in wk
    if s["kind"] == "lift" and not overridden and wk.get("tag") != "deload" \
            and phase not in ("Rebuild", "Base"):
        s["detail"] = plan["lift_notes"].get(phase, s["detail"])   # later phases: show the volume change
    race_text = next((v for v in (s["detail"], s["title"]) if v.upper().startswith("RACE")), None)
    if race_text:   # race day can fall on a run day (detail) or override a lift day (title)
        s = {"title": race_text, "detail": "Go get it", "kind": "run"}
    tomorrow = (_session(plan, tweek, plan["weeks"].get(tweek, {}), tkey)
                if tweek <= len(plan["weeks"]) and today < race else None)

    if s["kind"] == "rest":
        eat = plan["eat"]["rest"]
    elif dkey == "sat":
        eat = plan["eat"]["long_late" if today.month in (3, 4, 5) else "long_early"]
    else:
        eat = plan["eat"][s["kind"]]

    tag = wk.get("tag", "")
    return {
        "week": week, "weeks": len(plan["weeks"]), "phase": phase,
        "tag": tag.capitalize() if tag in ("deload", "taper") else ("Race week" if tag == "race" else ""),
        "today": s, "tomorrow": tomorrow, "eat": eat,
        "days_to_race": days_to_race, "race_label": plan.get("race_label", "Race"),
    }


def _as_text(item):
    """`- Thu: Change my trays` is valid YAML for a dict, so accept that shape too
    (quoting it is easy to forget when editing from a phone)."""
    if isinstance(item, dict):
        return "; ".join(f"{k}: {v}" for k, v in item.items())
    return str(item)


def _for_today(item, dkey):
    """"Thu: Change my trays" shows on Thursdays only; "Mon,Wed: Gym" on either.
    Anything without a day tag shows every day. Returns the text, or None."""
    head, sep, rest = item.partition(":")
    tags = [t.strip().lower()[:3] for t in head.replace("/", ",").split(",")]
    if sep and tags and all(t in DAYS for t in tags):
        return rest.strip() if dkey in tags else None
    return item


def get_todos(content, now):
    """REMINDERS var is one reminder per line, pushed by the iPhone Shortcut.
    Falls back to the todos in content.yaml when it is not set."""
    raw = (os.getenv("REMINDERS") or "").strip()
    if raw:
        lines = raw.replace(";", "\n").replace("|", "\n").splitlines()
        items = [s for s in (ln.strip(" -\u2022\t") for ln in lines) if s]
    else:
        items = content.get("todos") or []
    dkey = DAYS[now.weekday()]
    return [t for t in (_for_today(_as_text(i), dkey) for i in items) if t]


def get_content():
    p = REPO / "content.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def mock_data(now):
    def ev(title, h, m, mins):
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return _event(title, start, start + dt.timedelta(minutes=mins), False)

    return (
        {"temp": 72, "cond": "Partly cloudy", "hi": 78, "lo": 61, "rain": 20,
         "unit": "°F", "icon": ICONS["part"]},
        [ev("Morning run", 7, 0, 45), ev("Team standup", 9, 30, 30),
         ev("Lunch with Sam", 12, 0, 60), ev("Dentist", 16, 15, 45)],
        [ev("Grocery pickup", 10, 0, 30)],
    )


# ---------- render ----------

def render_html(ctx) -> str:
    env = Environment(
        loader=FileSystemLoader(ROOT), autoescape=select_autoescape(["html"])
    )
    return env.get_template("dashboard.html").render(**ctx)


def screenshot(html: str, path: Path):
    channel = os.getenv("PW_CHANNEL") or None  # "chrome" on GitHub runners
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel)
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        page.set_content(html, wait_until="networkidle")
        page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": W, "height": H})
        browser.close()


def to_frame(png: Path) -> bytes:
    gray = Image.open(png).convert("L").resize((W, H))
    bw = gray.point(lambda v: 255 if v > THRESHOLD else 0, mode="1")
    bw.save(png)  # overwrite preview with the exact 1-bit result
    # Pillow mode "1" packs MSB-first with 1 = white; the device wants 1 = black.
    raw = bytes(b ^ 0xFF for b in bw.tobytes())
    assert len(raw) == FRAME_BYTES, len(raw)
    return raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="sample data, no API calls")
    args = ap.parse_args()

    now = dt.datetime.now(TZ)
    content = get_content()
    training = get_training(now)
    eat = (training or {}).get("eat")
    if args.mock:
        weather, today, tomorrow = mock_data(now)
        steps = {"count": "6,482", "goal": "10,000", "pct": 65}
        macros = _macro_rows(1840, 96, 210, 54, eat)
    else:
        weather, (today, tomorrow, *_) = get_weather(), get_events(now)
        steps = get_steps(now)
        macros = get_macros(now, eat)

    ctx = {
        "now": now,
        "dow": now.strftime("%A"),
        "day": now.day,
        "month": now.strftime("%B %Y"),
        "updated": now.strftime("%-I:%M %p"),
        "weather": weather,
        "steps": steps,
        "training": training,
        "macros": macros,
        "today": today,
        "tomorrow": tomorrow,
        "focus": content.get("focus", ""),
        "todos": get_todos(content, now),
    }

    OUT.mkdir(exist_ok=True)
    preview = OUT / "preview.png"
    screenshot(render_html(ctx), preview)
    (OUT / "screen.bin").write_bytes(to_frame(preview))
    print(f"Wrote {OUT/'screen.bin'} ({FRAME_BYTES} bytes) and {preview}")


if __name__ == "__main__":
    sys.exit(main())