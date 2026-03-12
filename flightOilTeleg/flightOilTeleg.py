import argparse
import asyncio
import csv
import html
import os
import re
import shutil
import socket
import time
import statistics
import zipfile
import json
import functools
from datetime import date, datetime, timedelta, timezone
import pytz
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from telethon import TelegramClient
from telethon import events as tg_events
from deep_translator import GoogleTranslator

# Load environment variables
load_dotenv()
console = Console()

LIVEUAMAP_IRAN_RESID = 7

# --- FPC CONSTANTS ---
AVG_START = date(2026, 1, 1)
AVG_END = date(2026, 2, 25)
OPA_BASE = "https://api.oilpriceapi.com/v1"

OPA_CODES = {
    "WTI_USD": ("WTI Crude", "$/bbl"),
    "BRENT_CRUDE_USD": ("Brent Crude", "$/bbl"),
    "DUBAI_CRUDE_USD": ("Dubai/Oman Crude", "$/bbl"),
    "JET_FUEL_USD": ("Jet Fuel (Kero)", "$/gal"),
    "VLSFO_USD": ("VLSFO 0.5%", "$/mt"),
    "MGO_05S_USD": ("MGO 0.5%", "$/mt"),
    "HFO_380_USD": ("HFO 380 cSt", "$/mt"),
    "GASOLINE_USD": ("RBOB Gasoline", "$/gal"),
    "DIESEL_USD": ("ULSD Diesel", "$/gal"),
    "HEATING_OIL_USD": ("Heating Oil No.2", "$/gal"),
}

YF_PROXIES = {
    "WTI Crude": ("CL=F", "$/bbl"),
    "Brent Crude": ("BZ=F", "$/bbl"),
    "RBOB Gasoline": ("RB=F", "$/gal"),
    "ULSD Diesel": ("HO=F", "$/gal"),
    "Heating Oil No.2": ("HO=F", "$/gal"),
}

DISPLAY_ORDER = [
    ("CRUDE", "WTI Crude"),
    ("CRUDE", "Brent Crude"),
    ("CRUDE", "Dubai/Oman Crude"),
    ("JET", "Jet Fuel (Kero)"),
    ("MARITIME", "VLSFO 0.5%"),
    ("MARITIME", "MGO 0.5%"),
    ("MARITIME", "HFO 380 cSt"),
    ("AUTO", "RBOB Gasoline"),
    ("AUTO", "ULSD Diesel"),
    ("AUTO", "Heating Oil No.2"),
]

FIXED_AVGS = {
    "WTI Crude": 63.50,
    "Brent Crude": 69.00,
    "Dubai/Oman Crude": 67.50,
    "Jet Fuel (Kero)": 2.15,
    "VLSFO 0.5%": 500.00,
    "MGO 0.5%": 598.00,
    "HFO 380 cSt": 402.10,
    "RBOB Gasoline": 2.05,
    "ULSD Diesel": 2.38,
    "Heating Oil No.2": 2.23,
}


# ---------------------

### Console Frameout Start
def generate_layout():
    layout = Layout()
    layout.split(
        Layout(name="header", size=3),
        Layout(name="main", ratio=1),
        Layout(name="footer", size=3)
    )

    layout["main"].split_row(
        Layout(name="left_col", ratio=6),
        Layout(name="right", ratio=4)
    )

    # Stacking FPC, Airports, and LiveUAMap vertically on the left
    layout["left_col"].split(
        Layout(name="fpc_tracker", ratio=2),
        Layout(name="airports_tracker", ratio=1),
        Layout(name="liveuamap_tracker", ratio=3)
    )

    layout["right"].split(
        Layout(name="raw_traffic", ratio=3),
        Layout(name="keyword_alerts", ratio=2)
    )

    return layout


def update_fpc_tracker(status):
    fpc_data = status.get("fpc_data", {})
    sources = status.get("fpc_sources", [])
    src_str = ", ".join(sources) if sources else "Waiting..."

    table = Table(
        title=f"Fuel Price Intelligence | Sources: {src_str} | Avg Window: Jan 1 – Feb 25 2026",
        border_style="bold magenta",
        expand=True
    )

    table.add_column("CAT", style="cyan bold")
    table.add_column("COMMODITY", style="white bold")
    table.add_column("SPOT", justify="right")
    table.add_column("UNIT", style="dim")
    table.add_column("J1–F25 AVG", justify="right", style="dim")
    table.add_column("VS AVG", justify="right")
    table.add_column("AS OF", style="dim")

    if not fpc_data:
        table.add_row("-", "Initializing Fuel Data...", "-", "-", "-", "-", "-")
        return table

    prev_cat = None
    for cat, lbl in DISPLAY_ORDER:
        d = fpc_data.get(lbl)

        # Separator for categories
        if cat != prev_cat and prev_cat is not None:
            table.add_row("[dim]·[/dim]", "[dim]·[/dim]", "", "", "", "", "")
        prev_cat = cat

        if d is None or "spot" not in d:
            table.add_row(cat, f"[yellow]{lbl}[/]", "[dim]no data[/]", "", "", "", "")
            continue

        if "error" in d:
            table.add_row(cat, f"[yellow]{lbl}[/]", f"[red]ERR: {d['error'][:15]}[/]", "", "", "", "")
            continue

        spot = d["spot"]
        avg = FIXED_AVGS.get(lbl)
        ul = d.get("unit_label", "")
        dt = str(d.get("spot_date", "—"))[:10]

        # Spot color logic
        spot_fmt = f"{spot:.3f}"
        if avg is None or avg == 0:
            spot_color = "cyan"
            vs_fmt = "[dim]—[/]"
        else:
            pct = (spot - avg) / avg * 100
            if pct > 2:
                spot_color = "red"
                vs_fmt = f"[red]{pct:>+6.1f}%[/]"
            elif pct < -2:
                spot_color = "green"
                vs_fmt = f"[green]{pct:>+6.1f}%[/]"
            else:
                spot_color = "yellow"
                vs_fmt = f"[yellow]{pct:>+6.1f}%[/]"

        avg_fmt = f"{avg:.2f}" if avg else "—"

        table.add_row(
            cat,
            lbl,
            f"[{spot_color}]{spot_fmt}[/]",
            ul,
            avg_fmt,
            vs_fmt,
            dt
        )

    return table


def update_airports_tracker(status):
    table = Table(title="Regional Airspace Activity (ADSB.fi)", border_style="bold blue", expand=True)
    table.add_column("Country/Airport", style="cyan")
    table.add_column("Ground", justify="center", style="white")
    table.add_column("Outbound", justify="center", style="green")
    table.add_column("Inbound", justify="center", style="yellow")

    airports = status.get("regional_airports", {})
    active_count = 0
    sorted_airports = sorted(airports.items(), key=lambda x: x[1]['country'])

    for key, data in sorted_airports:
        if data['ground'] > 0 or data['outbound'] > 0 or data['inbound'] > 0:
            display_name = f"{data['country']} - {data['name']} ({key})"
            table.add_row(display_name, str(data['ground']), str(data['outbound']), str(data['inbound']))
            active_count += 1

    if active_count == 0:
        table.add_row("Scanning Airspace...", "-", "-", "-")

    return table


def update_liveuamap_tracker(status):
    remaining = max(0, int(status.get("next_liveuamap_update", 0) - time.time()))
    mins, secs = divmod(remaining, 60)
    timer_str = f"[{mins:02d}:{secs:02d}]"

    last_status = status.get("liveuamap_last_status", "INIT")
    last_call = status.get("liveuamap_last_call", "Never")
    last_count = status.get("liveuamap_last_count", 0)

    if last_status == "200":
        status_markup = f"[bold green]{last_status}[/] ({last_count} rcvd)"
    elif last_status == "INIT":
        status_markup = f"[dim]{last_status}[/]"
    else:
        status_markup = f"[bold red]{last_status}[/]"

    newest_ts = status.get("liveuamap_newest_ts", "—")

    title_str = (
        f"LiveUAMap (Iran) | Last call: {last_call} {status_markup} | "
        f"Newest event: {newest_ts} | Next: {timer_str}"
    )

    table = Table(title=title_str, border_style="bold yellow", expand=True)

    table.add_column("Time (ET)", style="white", justify="center", no_wrap=True)
    table.add_column("Report", style="white", ratio=4)
    table.add_column("Location", style="yellow", ratio=2)
    table.add_column("Source", style="magenta", justify="center", no_wrap=True)

    map_events = status.get("liveuamap_events", [])
    if not map_events:
        table.add_row("-", "Initializing feed...", "-", "-")
    else:
        et_tz = pytz.timezone("US/Eastern")

        for e in map_events[:8]:
            ts = int(e.get("timestamp") or e.get("time", 0))
            dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
            dt_et = dt_utc.astimezone(et_tz)
            time_str = dt_et.strftime("%H:%M")

            report_text = str(e.get("name", "Unknown Event"))
            report_text = html.unescape(report_text)

            loc = e.get("location", "").strip()
            lat = e.get("lat", "")
            lng = e.get("lng", "")

            if loc:
                location_display = f"{loc}\n[dim]({lat}, {lng})[/dim]"
            elif lat and lng:
                location_display = f"Lat: {lat}\nLng: {lng}"
            else:
                location_display = "Unknown"

            source_raw = str(e.get("source", "N/A"))
            if source_raw.startswith("http"):
                try:
                    source_display = source_raw.split("/")[2]
                except IndexError:
                    source_display = source_raw
            else:
                source_display = source_raw[:15] + "..." if len(source_raw) > 15 else source_raw

            table.add_row(time_str, report_text, location_display, source_display)
            table.add_row("", "[dim]" + "─" * 20 + "[/dim]", "", "")

    return table


### API Calls Start
class DataOrchestrator:
    def __init__(self, config):
        self.config = config
        self.log_file = "osint_log.txt"
        self.liveuamap_file = "iran_events_baseline.json"

        self.status = {
            "internet": "OFFLINE", "adsb_status": "WAITING",
            "telegram": "DISCONNECTED", "last_tg_signal": "None",
            "analysis_logs": [], "raw_tg_logs": [], "liveuamap_events": [],
            "next_liveuamap_update": time.time() + 900,
            "regional_airports": self._initialize_airports_config(),
            "fpc_data": {}, "fpc_sources": []
        }

        self.translator = GoogleTranslator(source='auto', target='en')
        self.tg_client = TelegramClient('osint_session', config['tg_id'], config['tg_hash']) if config.get(
            'tg_id') else None

        self.target_chats = [
            '@farsna', '@Tasnimnews', '@akharinkhabar', '@iranintl',
            '@Middle_East_Spectator', '@snntv', '@FarsNewsInt',
            '@isna94', '@irna_1313', '@khabari',
            '@mehrnews', '@bbcpersian', '@manototv', '@NourNews_IR'
        ]

        self.keywords = {
            "URGENT": ["explosion", "انفجار", "attack", "حمله", "strike", "hit", "اصابت"],
            "MILITARY": ["border", "مرز", "irgc", "sepah", "سپاه", "drone", "پهپاد", "missile", "موشک",
                         "military movement", "نقل و انتقالات نظامی"],
            "LOGISTICS": ["port", "بندر", "oil", "نفت", "refinery", "پالایشگاه", "shutdown", "تعطیلی"],
            "CIVIL": ["internet shutdown", "قطع اینترنت", "power outage", "قطعی برق", "protest", "اعتراض", "strike",
                      "اعتصاب"],
            "GEOGRAPHIC": ["strait of hormuz", "تنگه هرمز", "shatt al-arab", "اروندرود", "persian gulf", "خلیج فارس",
                           "khuzestan", "خوزستان", "basra", "بصره"],
            "TACTICAL": ["air defense", "پدافند هوایی", "siren", "آژیر", "emergency", "اورژانس", "red alert",
                         "وضعیت قرمز"]
        }

    def _initialize_airports_config(self):
        return {
            "KBL": {"country": "AFG", "name": "Kabul Int'l", "lat": 34.566, "lon": 69.212, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "HEA": {"country": "AFG", "name": "Herat Int'l", "lat": 34.210, "lon": 62.228, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "KDH": {"country": "AFG", "name": "Kandahar Intl", "lat": 31.505, "lon": 65.848, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "MZR": {"country": "AFG", "name": "Mazar-i-Sharif", "lat": 36.707, "lon": 67.210, "ground": 0,
                    "outbound": 0, "inbound": 0},
            "DWC": {"country": "ARE", "name": "Al Maktoum Int", "lat": 24.896, "lon": 55.174, "ground": 0,
                    "outbound": 0, "inbound": 0},
            "DXB": {"country": "ARE", "name": "Dubai Intl", "lat": 25.253, "lon": 55.364, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "FJR": {"country": "ARE", "name": "Fujairah Intl", "lat": 25.112, "lon": 56.324, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "SHJ": {"country": "ARE", "name": "Sharjah Intl", "lat": 25.328, "lon": 55.517, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "AUH": {"country": "ARE", "name": "Zayed Intl", "lat": 24.433, "lon": 54.651, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "BAH": {"country": "BHR", "name": "Bahrain Intl", "lat": 26.270, "lon": 50.633, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "BGW": {"country": "IRQ", "name": "Baghdad Intl", "lat": 33.262, "lon": 44.234, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "KWI": {"country": "KWT", "name": "Kuwait Intl", "lat": 29.226, "lon": 47.969, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "KHS": {"country": "OMN", "name": "Khasab Airport", "lat": 26.171, "lon": 56.241, "ground": 0,
                    "outbound": 0, "inbound": 0},
            "MCT": {"country": "OMN", "name": "Muscat Intl", "lat": 23.593, "lon": 58.284, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "OHS": {"country": "OMN", "name": "Sohar Airport", "lat": 24.385, "lon": 56.635, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "GWD": {"country": "PAK", "name": "Gwadar Intl", "lat": 25.232, "lon": 62.328, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "ISB": {"country": "PAK", "name": "Islamabad Intl", "lat": 33.549, "lon": 72.827, "ground": 0,
                    "outbound": 0, "inbound": 0},
            "KHI": {"country": "PAK", "name": "Jinnah Intl", "lat": 24.906, "lon": 67.161, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "PEW": {"country": "PAK", "name": "Peshawar Intl", "lat": 33.994, "lon": 71.515, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "UET": {"country": "PAK", "name": "Quetta Intl", "lat": 30.251, "lon": 66.937, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "DOH": {"country": "QAT", "name": "Hamad Intl", "lat": 25.273, "lon": 51.608, "ground": 0, "outbound": 0,
                    "inbound": 0},
            "JED": {"country": "SAU", "name": "King Abdulaziz", "lat": 21.679, "lon": 39.156, "ground": 0,
                    "outbound": 0, "inbound": 0},
            "DMM": {"country": "SAU", "name": "King Fahd Intl", "lat": 26.471, "lon": 49.798, "ground": 0,
                    "outbound": 0, "inbound": 0},
            "RUH": {"country": "SAU", "name": "King Khalid", "lat": 24.957, "lon": 46.698, "ground": 0, "outbound": 0,
                    "inbound": 0}
        }

    def _write_to_file(self, source, category, text):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] SOURCE: {source} | CAT: {category} | MSG: {text}\n")

    def _add_log(self, source, category, color, text):
        timestamp = datetime.now()
        display_text = text.replace('\n', ' ')[:250]
        log_entry = {
            "time": timestamp,
            "display": f"[[dim]{timestamp.strftime('%H:%M:%S')}[/]] [{color}]{category}[/] ({source}): {display_text}"
        }
        self.status["analysis_logs"].insert(0, log_entry)
        if len(self.status["analysis_logs"]) > 12:
            self.status["analysis_logs"] = self.status["analysis_logs"][:12]
        self._write_to_file(source, category, text)

    def _add_raw_log(self, source, text):
        timestamp = datetime.now()
        display_text = text.replace('\n', ' ')[:250]
        log_entry = f"[[dim]{timestamp.strftime('%H:%M:%S')}[/]] [bold]{source}[/]: {display_text}"
        self.status["raw_tg_logs"].insert(0, log_entry)
        if len(self.status["raw_tg_logs"]) > 15:
            self.status["raw_tg_logs"].pop()
        self._write_to_file(source, "RAW_TG", text)

    # --- FPC FETCH LOGIC ---
    def _fetch_opa_sync(self):
        opa_key = self.config.get("opa_key")
        if not opa_key:
            return {}

        results = {}
        headers = {"Authorization": f"Token {opa_key}", "User-Agent": "osint-dashboard/1.0"}

        for code, (label, unit_label) in OPA_CODES.items():
            try:
                res = requests.get(f"{OPA_BASE}/prices/latest?by_code={code}", headers=headers, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    spot = float(data["data"]["price"])
                    dt = data["data"].get("created_at", "")[:10]
                    results[label] = {
                        "spot": spot, "spot_date": dt,
                        "unit_label": unit_label, "source": "OilPriceAPI"
                    }
            except Exception as e:
                results[label] = {"error": str(e), "source": "OilPriceAPI"}
        return results

    def _fetch_yfinance_sync(self, needed):
        if not needed:
            return {}
        try:
            import yfinance as yf
        except ImportError:
            self._add_log("System", "FPC WARN", "yellow", "yfinance not installed. Cannot gap-fill prices.")
            return {}

        results = {}
        for label in needed:
            if label not in YF_PROXIES:
                continue
            ticker, unit_label = YF_PROXIES[label]
            try:
                t = yf.Ticker(ticker)
                hist = t.history(start="2026-01-01", end=datetime.now().strftime("%Y-%m-%d"))
                if hist.empty:
                    continue

                spot = float(hist["Close"].iloc[-1])
                spot_date = str(hist.index[-1].date())

                results[label] = {
                    "spot": spot, "spot_date": spot_date,
                    "unit_label": unit_label, "source": f"yfinance ({ticker})"
                }
            except Exception as e:
                results[label] = {"error": str(e), "source": f"yfinance ({ticker})"}
        return results

    async def monitor_fuel_prices(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                # Fetch OPA
                opa_data = await loop.run_in_executor(None, self._fetch_opa_sync)
                sources = ["OilPriceAPI"] if opa_data else []

                # Check for missing to gap fill with YF
                all_labels = [lbl for _, lbl in DISPLAY_ORDER]
                missing = [lbl for lbl in all_labels if lbl not in opa_data or "error" in opa_data.get(lbl, {})]

                yf_data = await loop.run_in_executor(None, functools.partial(self._fetch_yfinance_sync, missing))
                if yf_data:
                    sources.append("yfinance")
                    opa_data.update(yf_data)

                self.status["fpc_data"] = opa_data
                self.status["fpc_sources"] = sources
            except Exception as e:
                self._add_log("System", "FPC ERR", "bold red", f"Fuel Monitor crashed: {e}")

            await asyncio.sleep(300)  # Refresh every 5 mins

    # -----------------------

    async def monitor_liveuamap(self):
        base_url = "https://a.liveuamap.com/api"
        api_key = os.getenv("LIVEUAMAP_KEY")
        if not api_key:
            self._add_log("LiveUAMap", "API ERROR", "bold red", "No LIVEUAMAP_KEY found in environment variables.")
            return

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://iran.liveuamap.com",
            "Referer": "https://iran.liveuamap.com/",
        }

        all_events = {}
        translated_names = {}

        self.status["liveuamap_last_status"] = "INIT"
        self.status["liveuamap_last_count"] = 0
        self.status["liveuamap_last_call"] = "Never"
        self.status["liveuamap_newest_ts"] = "—"

        loop = asyncio.get_running_loop()

        while True:
            try:
                params = {
                    "a": "mpts",
                    "key": api_key,
                    "resid": LIVEUAMAP_IRAN_RESID,
                    "dt": int(time.time()),
                    "count": 50,
                }

                call_time = datetime.now().strftime("%H:%M:%S")
                response = await loop.run_in_executor(
                    None,
                    functools.partial(requests.get, base_url, params=params, headers=headers, timeout=15)
                )

                self.status["liveuamap_last_call"] = call_time
                self.status["liveuamap_last_status"] = str(response.status_code)

                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError as json_err:
                        self._add_log("LiveUAMap", "PARSE ERROR", "bold red",
                                      f"JSON decode failed: {str(json_err)[:80]}")
                        self.status["liveuamap_last_status"] = "JSON ERR"
                        self.status["next_liveuamap_update"] = time.time() + 900
                        await asyncio.sleep(900)
                        continue

                    venues = data.get("venues", []) if isinstance(data, dict) else (
                        data if isinstance(data, list) else [])
                    self.status["liveuamap_last_count"] = len(venues)

                    for event in venues:
                        eid = event.get("id")
                        if eid is None:
                            continue

                        raw_text = event.get("name", "")
                        if raw_text:
                            raw_text = html.unescape(raw_text)

                        if eid not in translated_names:
                            if raw_text:
                                try:
                                    translated_text = await loop.run_in_executor(
                                        None, self.translator.translate, raw_text
                                    )
                                    translated_names[eid] = translated_text
                                except Exception as trans_err:
                                    self._add_log("LiveUAMap", "TRANS WARN", "dim yellow",
                                                  f"Translation fallback: {str(trans_err)[:60]}")
                                    translated_names[eid] = raw_text
                            else:
                                translated_names[eid] = raw_text

                        event["name"] = translated_names[eid]
                        all_events[eid] = event

                    cutoff = int(time.time()) - 86400
                    all_events = {
                        eid: e for eid, e in all_events.items()
                        if int(e.get("timestamp") or e.get("time", 0)) > cutoff
                    }

                    events_list = sorted(
                        all_events.values(),
                        key=lambda x: int(x.get("timestamp") or x.get("time", 0)),
                        reverse=True
                    )

                    disk_events = {}
                    if os.path.exists(self.liveuamap_file):
                        try:
                            with open(self.liveuamap_file, "r", encoding="utf-8") as f:
                                for e in json.load(f):
                                    eid = e.get("id")
                                    if eid is not None:
                                        disk_events[eid] = e
                        except Exception:
                            pass

                    disk_events.update({e["id"]: e for e in events_list if e.get("id") is not None})

                    merged_list = sorted(
                        disk_events.values(),
                        key=lambda x: int(x.get("timestamp") or x.get("time", 0)),
                        reverse=True
                    )

                    with open(self.liveuamap_file, "w", encoding="utf-8") as f:
                        json.dump(merged_list, f, indent=2, ensure_ascii=False)

                    self.status["liveuamap_events"] = events_list[:15]

                    if events_list:
                        newest_ts = int(events_list[0].get("timestamp") or events_list[0].get("time", 0))
                        et_tz = pytz.timezone("US/Eastern")
                        newest_et = datetime.fromtimestamp(newest_ts, tz=timezone.utc).astimezone(et_tz)
                        self.status["liveuamap_newest_ts"] = newest_et.strftime("%H:%M ET")

                elif response.status_code == 429:
                    self._add_log("LiveUAMap", "RATE LIMIT", "bold yellow", "Rate limited — backing off 5 minutes.")
                    self.status["next_liveuamap_update"] = time.time() + 300
                    await asyncio.sleep(300)
                    continue
                else:
                    self._add_log("LiveUAMap", "HTTP ERROR", "bold red",
                                  f"Unexpected HTTP {response.status_code} — raw: {response.text[:120]}")

            except requests.exceptions.Timeout:
                self.status["liveuamap_last_status"] = "TIMEOUT"
                self._add_log("LiveUAMap", "TIMEOUT", "dim red", "Request timed out after 15s")
            except requests.exceptions.ConnectionError as conn_err:
                self.status["liveuamap_last_status"] = "CONN ERR"
                self._add_log("LiveUAMap", "CONN ERROR", "dim red", f"Connection failed: {str(conn_err)[:60]}")
            except Exception as e:
                self.status["liveuamap_last_status"] = f"ERR:{type(e).__name__}"
                self._add_log("LiveUAMap", "UNEXPECTED ERR", "bold red", f"{type(e).__name__}: {str(e)[:80]}")

            self.status["next_liveuamap_update"] = time.time() + 900
            await asyncio.sleep(900)

    async def start_telegram_listener(self):
        if not self.tg_client:
            return

        @self.tg_client.on(tg_events.NewMessage())
        async def handler(event):
            msg_text = event.message.message
            if not msg_text:
                return

            chat = await event.get_chat()
            channel_username = getattr(chat, 'username', None)
            if not channel_username:
                return

            formatted_handle = f"@{channel_username}".lower()
            target_list_lower = [c.lower() for c in self.target_chats]
            if formatted_handle not in target_list_lower:
                return

            loop = asyncio.get_running_loop()
            try:
                english_text = await loop.run_in_executor(None, self.translator.translate, msg_text)
            except Exception:
                english_text = f"[Raw Farsi] {msg_text}"

            self._add_raw_log(formatted_handle, english_text)

            msg_lower = msg_text.lower()
            for category, words in self.keywords.items():
                if any(word.lower() in msg_lower for word in words):
                    color = ("bold white on red" if category in ["URGENT", "TACTICAL"]
                             else "yellow" if category == "MILITARY" else "cyan")
                    self.status["last_tg_signal"] = f"[{color}][{category}][/] {formatted_handle}"
                    self._add_log(formatted_handle, category, color, english_text)
                    return

        self.status["telegram"] = "LISTENING"

        while True:
            try:
                await self.tg_client.run_until_disconnected()
            except Exception as tg_err:
                self._add_log("Telegram", "RECONNECT", "bold yellow",
                              f"Disconnected ({str(tg_err)[:60]}), retrying in 30s...")
                self.status["telegram"] = "RECONNECTING"
                await asyncio.sleep(30)
                try:
                    await self.tg_client.connect()
                    self.status["telegram"] = "LISTENING"
                    self._add_log("Telegram", "RECONNECT", "bold green", "Reconnected successfully.")
                except Exception as reconnect_err:
                    self._add_log("Telegram", "RECONNECT FAIL", "bold red",
                                  f"Reconnect failed: {str(reconnect_err)[:60]}")

    async def monitor_airports_adsb(self):
        headers = {"User-Agent": "OSINT-Dashboard/2026.1"}
        radius_nm = 15

        loop = asyncio.get_running_loop()

        while True:
            for iata, data in self.status["regional_airports"].items():
                lat = data['lat']
                lon = data['lon']
                url = f"https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{radius_nm}"

                try:
                    response = await loop.run_in_executor(
                        None,
                        functools.partial(requests.get, url, headers=headers, timeout=10)
                    )

                    if response.status_code == 200:
                        aircraft = response.json().get('ac', [])
                        ground, inbound, outbound = 0, 0, 0
                        for ac in aircraft:
                            alt = ac.get('alt_baro')
                            rate = ac.get('baro_rate', 0)
                            if alt == "ground":
                                ground += 1
                            elif rate < -500:
                                inbound += 1
                            elif rate > 500:
                                outbound += 1

                        self.status["regional_airports"][iata].update({
                            "ground": ground, "inbound": inbound, "outbound": outbound
                        })
                        self.status["adsb_status"] = "LIVE"
                except Exception:
                    pass
                await asyncio.sleep(5)
            await asyncio.sleep(10)

    async def monitor_parseek(self):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OSINT-Dashboard/3.0"}
        seen_headlines = set()

        loop = asyncio.get_running_loop()

        while True:
            try:
                url = "https://www.parseek.ir/news/"
                response = await loop.run_in_executor(
                    None,
                    functools.partial(requests.get, url, headers=headers, timeout=10)
                )

                if response.status_code == 200:
                    soup = BeautifulSoup(response.content, 'html.parser')
                    news_items = soup.find_all(['a', 'div'], class_=['ns', 'title', 'headline', 'nt'])

                    for item in news_items[:20]:
                        title = item.get_text(strip=True)
                        if not title or title in seen_headlines:
                            continue
                        seen_headlines.add(title)

                        title_lower = title.lower()
                        for category, words in self.keywords.items():
                            if any(w.lower() in title_lower for w in words):
                                try:
                                    english_title = await loop.run_in_executor(
                                        None, self.translator.translate, title
                                    )
                                except Exception:
                                    english_title = f"[Raw] {title}"

                                color = ("bold white on red" if category in ["URGENT", "TACTICAL"]
                                         else "yellow")
                                self._add_log("Parseek", category, color, english_title)
                                break
            except Exception:
                pass

            await asyncio.sleep(120)

    async def daily_archiver(self):
        while True:
            now = datetime.now()
            next_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
            seconds_until_midnight = (next_midnight - now).total_seconds()
            await asyncio.sleep(seconds_until_midnight)

            archive_date = now.strftime("%Y-%m-%d")
            zip_filename = f"osint_archive_{archive_date}.zip"
            files_to_archive = [self.log_file]

            try:
                with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for file in files_to_archive:
                        if os.path.exists(file):
                            temp_name = f"{archive_date}_{file}"
                            shutil.move(file, temp_name)
                            zipf.write(temp_name, arcname=temp_name)
                            os.remove(temp_name)

                self._add_log("SYSTEM", "ARCHIVE", "bold green", f"Successfully zipped daily logs to {zip_filename}")
            except Exception as e:
                self._add_log("SYSTEM", "ERROR", "bold red", f"Failed to zip daily logs: {str(e)}")

    def check_internet(self, host="8.8.8.8", port=53, timeout=3):
        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
            self.status["internet"] = "ONLINE"
        except socket.error:
            self.status["internet"] = "OFFLINE"


def get_tehran_time():
    return datetime.now(pytz.timezone('Asia/Tehran')).strftime("%I:%M:%S %p")


async def main():
    console.print("\n[bold cyan]=== SYSTEM INITIALIZATION ===[/bold cyan]")

    config = {
        "adsb_key": os.getenv("ADS_B_KEY"),
        "opa_key": os.getenv("OILPRICEAPI"),
        "tg_id": int(os.getenv("TG_ID", 0)) if os.getenv("TG_ID") else None,
        "tg_hash": os.getenv("TG_HASH")
    }

    orchestrator = DataOrchestrator(config)
    orchestrator.check_internet()

    if config["tg_id"] and config["tg_hash"]:
        console.print("\n[bold yellow]>> MANUAL INTERVENTION REQUIRED <<[/bold yellow]")
        input("Press [ENTER] to initialize connection to Telegram infrastructure...")

        await orchestrator.tg_client.start()
        if await orchestrator.tg_client.is_user_authorized():
            orchestrator.status["telegram"] = "AUTHORIZED"
            console.print("[bold green][+] Telegram Session Authorized.[/bold green]")
            async for dialog in orchestrator.tg_client.iter_dialogs():
                if dialog.is_channel:
                    handle = f"@{dialog.entity.username}" if dialog.entity.username else "Private"
                    if handle.lower() in [c.lower() for c in orchestrator.target_chats]:
                        console.print(f" [green]✓[/green] Active Listener: [bold]{dialog.name}[/bold] ({handle})")
            console.print("[dim]Startup scan complete. Dashboard loading...[/dim]")

    await asyncio.sleep(2)
    layout = generate_layout()

    # Background Tasks
    asyncio.create_task(orchestrator.monitor_fuel_prices())
    asyncio.create_task(orchestrator.monitor_liveuamap())
    if config["tg_id"]:
        asyncio.create_task(orchestrator.start_telegram_listener())
    asyncio.create_task(orchestrator.monitor_parseek())
    asyncio.create_task(orchestrator.monitor_airports_adsb())
    asyncio.create_task(orchestrator.daily_archiver())

    with Live(layout, refresh_per_second=1, screen=True):
        while True:
            header_content = Text(
                f"This Is Going To Go Really Really Well - Tehran Time: {get_tehran_time()}",
                justify="center"
            )
            layout["header"].update(Panel(header_content, style="bold white on red", border_style="white"))

            layout["fpc_tracker"].update(Panel(update_fpc_tracker(orchestrator.status)))
            layout["airports_tracker"].update(Panel(update_airports_tracker(orchestrator.status)))
            layout["liveuamap_tracker"].update(Panel(update_liveuamap_tracker(orchestrator.status)))

            raw_text = "[bold cyan]Raw Telegram Firehose (Translated):[/bold cyan]\n"
            for log in orchestrator.status.get("raw_tg_logs", []):
                raw_text += f"{log}\n"
            layout["raw_traffic"].update(Panel(raw_text, title="Live Telegram Traffic"))

            filtered_text = "[bold cyan]Keyword Alerts from Telegram and Parseek Feeds:[/bold cyan]\n"
            for log in orchestrator.status.get("analysis_logs", []):
                filtered_text += f"{log['display']}\n"
            layout["keyword_alerts"].update(Panel(filtered_text, title="Keyword Alerts (Filtered)"))

            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[bold red]Dashboard Terminated.[/]")