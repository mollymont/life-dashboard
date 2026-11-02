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
    }


def _to_local(v, is_end=False):
    """ICS values can be dates (all-day) or datetimes (naive or aware)."""
    if isinstance(v, dt.datetime):
        return (v if v.tzinfo else v.replace(tzinfo=TZ)).astimezone(TZ), False
    return dt.datetime.combine(v, dt.time(), TZ), True


def get_events(now):
    url = os.getenv("ICS_URL")
    if not url:
        return [], []
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(http_get(url))
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day1, day2 = day0 + dt.timedelta(days=1), day0 + dt.timedelta(days=2)

    today, tomorrow = [], []
    for e in recurring_ical_events.of(cal).between(day0, day2):
        start, all_day = _to_local(e["DTSTART"].dt)
        end = _to_local(e["DTEND"].dt)[0] if e.get("DTEND") else start
        if end <= now and not all_day:
            continue  # already over
        item = {
            "title": str(e.get("SUMMARY", "(busy)")),
            "time": "All day" if all_day else start.strftime("%-I:%M %p"),
            "sort": (0 if all_day else 1, start),
        }
        if start < day1 or (all_day and start <= day0 < end):
            today.append(item)
        elif start < day2:
            tomorrow.append(item)
    today.sort(key=lambda x: x["sort"])
    tomorrow.sort(key=lambda x: x["sort"])
    return today, tomorrow


def get_content():
    p = REPO / "content.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def mock_data():
    return (
        {"temp": 72, "cond": "Partly cloudy", "hi": 78, "lo": 61, "rain": 20, "unit": "°F"},
        [
            {"title": "Morning run", "time": "7:00 AM"},
            {"title": "Team standup", "time": "9:30 AM"},
            {"title": "Lunch with Sam", "time": "12:00 PM"},
            {"title": "Dentist", "time": "4:15 PM"},
        ],
        [{"title": "Grocery pickup", "time": "10:00 AM"}],
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
    if args.mock:
        weather, today, tomorrow = mock_data()
    else:
        weather, (today, tomorrow) = get_weather(), get_events(now)

    ctx = {
        "now": now,
        "dow": now.strftime("%A"),
        "day": now.day,
        "month": now.strftime("%B %Y"),
        "updated": now.strftime("%-I:%M %p"),
        "weather": weather,
        "today": today,
        "tomorrow": tomorrow,
        "focus": content.get("focus", ""),
        "todos": content.get("todos", []) or [],
    }

    OUT.mkdir(exist_ok=True)
    preview = OUT / "preview.png"
    screenshot(render_html(ctx), preview)
    (OUT / "screen.bin").write_bytes(to_frame(preview))
    print(f"Wrote {OUT/'screen.bin'} ({FRAME_BYTES} bytes) and {preview}")


if __name__ == "__main__":
    sys.exit(main())
