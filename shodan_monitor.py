#!/usr/bin/env python3
"""
GDELT × Shodan Intelligence Terminal
======================================
Full-coverage Shodan monitor:
  - All 8 fusion signals across all 15 target countries
  - Total host count (all nodes) per country
  - ICS node counter per country (all 6 protocols)
  - City-level ICS + host counts: Iran (geo-precise), Israel, Pakistan
  - Inline fusion signal flags on every country and city row
  - JSON state written to shodan_signals.json each run

Designed to be called by an external scheduler — no internal timing loop.

Standalone:
  python3 shodan_monitor.py             # full scan
  python3 shodan_monitor.py --status    # print last saved state
  python3 shodan_monitor.py --cities-only
  python3 shodan_monitor.py --country IR,IL
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

EST = ZoneInfo("America/New_York")

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

WORK_DIR       = Path(__file__).parent
SIGNALS_FILE   = WORK_DIR / "shodan_signals.json"
BASELINE_FILE  = WORK_DIR / "shodan_baseline.json"
LOG_FILE       = WORK_DIR / "shodan_monitor.log"

SHODAN_API_KEY = os.environ.get("SHODAN_KEY", "")

# ── All 15 target countries: GDELT FIPS → ISO ───────────────
ALL_COUNTRIES = {
    "IR": "IR", "IZ": "IQ", "TU": "TR", "AF": "AF", "PK": "PK",
    "TX": "TM", "AJ": "AZ", "AM": "AM", "CY": "CY", "IS": "IL",
    "SY": "SY", "SA": "SA", "KU": "KW", "AE": "AE", "LB": "LB",
}

COUNTRY_NAMES = {
    "IR": "Iran",        "IQ": "Iraq",         "TR": "Turkey",
    "AF": "Afghanistan", "PK": "Pakistan",      "TM": "Turkmenistan",
    "AZ": "Azerbaijan",  "AM": "Armenia",       "CY": "Cyprus",
    "IL": "Israel",      "SY": "Syria",         "SA": "Saudi Arabia",
    "KW": "Kuwait",      "AE": "UAE",           "LB": "Lebanon",
}

# ── City targets ─────────────────────────────────────────────
# Iran uses a dict:  city_name → None  (query by city name)
#                               (lat, lon)  (query by geo radius, 50 km)
# Geo queries are used for strategic/remote/small sites where Shodan's
# city attribution is unreliable. All coordinates are WGS-84.
# Israel and Pakistan use plain lists (city-name queries are reliable).

CITY_TARGETS = {
    "IR": {
        # ── Major urban centres — name queryable ──────────────
        "Tehran":            None,
        "Isfahan":           None,
        "Shiraz":            None,
        "Karaj":             None,
        "Qeshm":             None,
        "Minab":             None,
        # ── Strategic / industrial — geo-queried (50 km) ──────
        "Kharg Island":      (29.24, 50.31),   # main oil export terminal
        "Bandar Abbas":      (27.18, 56.28),   # strait of hormuz naval base
        "Bushehr":           (28.92, 50.83),   # nuclear plant + port
        "Assaluyeh":         (27.48, 52.61),   # South Pars LNG hub
        "Chabahar Port":     (25.29, 60.64),   # Indian Ocean deep-water port
        "Ahvaz":             (31.31, 48.67),   # Khuzestan oil capital
        "Abadan":            (30.33, 48.30),   # historic refinery city
        "Tehran (Damavand)": (35.72, 52.06),   # eastern Tehran / missile sites
        "Jask":              (25.64, 57.77),   # IRGC naval expansion base
        "Qom":               (34.64, 50.87),   # nuclear enrichment / clerical
        "Tabriz":            (38.08, 46.29),   # NW industrial / drone production
        "Mashhad":           (36.29, 59.60),   # NE religious / logistics hub
        "Kermanshah":        (34.31, 47.06),   # Iraq border / IRGC logistics
        "Natanz":            (33.52, 51.92),   # main uranium enrichment facility
        "Siri Island":       (25.90, 54.54),   # offshore oil terminal
        "Larak Island":      (26.85, 56.35),   # Hormuz strait — IRGC naval
    },
    "IL": [
        "Tel Aviv", "Jerusalem", "Haifa",
    ],
    "PK": [
        "Islamabad", "Karachi", "Lahore",
    ],
}

# ── ICS protocol queries ──────────────────────────────────────
ICS_PROTOCOLS = {
    "Modbus":  "port:502",
    "DNP3":    "port:20000",
    "S7":      "port:102 Siemens",
    "BACnet":  "port:47808",
    "E/IP":    "port:44818",
    "IEC-104": "port:2404",
}

# ── Gov/Mil ASNs by ISO ───────────────────────────────────────
CRITICAL_ASNS = {
    "IR": ["AS48434", "AS12880", "AS44244", "AS16322", "AS58224"],
    "IQ": ["AS203214", "AS51113", "AS59588"],
    "IL": ["AS1680",   "AS8551",  "AS12400"],
    "SY": ["AS29256",  "AS50710"],
    "LB": ["AS41164",  "AS9051"],
    "SA": ["AS25019",  "AS35819"],
    "AE": ["AS15802",  "AS5384"],
    "PK": ["AS17557",  "AS23674", "AS45595"],
    "TR": ["AS9121",   "AS47524"],
    "KW": ["AS15802",  "AS21050"],
    "AZ": ["AS31721",  "AS29049"],
}

# Reverse map: ASN → ISO  (built at startup)
ASN_TO_ISO = {asn: iso for iso, asns in CRITICAL_ASNS.items() for asn in asns}

VPN_PROTOCOLS = {
    "OpenVPN":     "port:1194",
    "WireGuard":   "port:51820",
    "Shadowsocks": "port:8388",
    "L2TP":        "port:1701",
    "IKEv2":       "port:500 IKE",
}

WATCH_PORTS = [22, 23, 3389, 5900, 8080, 8443, 10000, 4444, 1194, 1723]

# ── Signal short codes for inline display ─────────────────────
# Order matters — this is the display order left→right
SIGNAL_SHORTS = [
    ("BLACKOUT_WATCH",   "BW"),
    ("CERT_CHURN",       "CC"),
    ("PORT_SHADOW",      "PS"),
    ("TELECOM_PULSE",    "TP"),
    ("VPN_SURGE",        "VS"),
    ("WEBCAM_BLINK",     "WB"),
    ("GRID_FINGERPRINT", "GF"),
    ("DARK_MIRROR",      "DM"),
]

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [SHODAN]  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

# ── ANSI ─────────────────────────────────────────────────────
R="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
RED="\033[91m"; ORG="\033[38;5;208m"; YLW="\033[93m"
GRN="\033[92m"; CYN="\033[96m"; WHT="\033[97m"
MAG="\033[95m"

def col_delta(d, width=8):
    if d is None:  return f"{DIM}{'—':>{width}}{R}"
    if d < -10:    return f"{RED}{d:>+{width}.0f}{R}"
    if d < 0:      return f"{ORG}{d:>+{width}.0f}{R}"
    if d > 10:     return f"{GRN}{d:>+{width}.0f}{R}"
    return             f"{DIM}{d:>+{width}.0f}{R}"

def ics_val(v, width=7):
    if v > 0: return f"{RED}{v:>{width},}{R}"
    return        f"{DIM}{'—':>{width}}{R}"

# ─────────────────────────────────────────────────────────────
# FUSION SIGNAL INDEX
# Builds a per-ISO dict of which signal short codes are firing.
# Called after scan_fusion_signals(), passed into render_terminal().
# ─────────────────────────────────────────────────────────────

def build_signal_index(fusion: dict, all_nodes: dict) -> dict:
    """
    Returns: { iso: set_of_short_codes }
    e.g. { "IR": {"BW", "GF"}, "IL": {"CC"} }
    """
    index = {}

    for sig_name, short in SIGNAL_SHORTS:
        sig_data = fusion.get(sig_name, {})
        alerts   = sig_data.get("alerts", [])

        for alert_key in alerts:
            alert_key = str(alert_key)
            iso = None

            # Plain ISO code (CC, VS, WB, GF, DM, TP)
            if len(alert_key) == 2 and alert_key.isupper():
                iso = alert_key
            # "IR_Modbus" style (BW, GF)
            elif "_" in alert_key:
                prefix = alert_key.split("_")[0]
                if len(prefix) == 2 and prefix.isupper():
                    iso = prefix
            # ASN string (PS)
            elif alert_key.startswith("AS"):
                iso = ASN_TO_ISO.get(alert_key)

            if iso:
                index.setdefault(iso, set()).add(short)

    # TELECOM_PULSE: derive from all_nodes direction flag
    for iso, an in all_nodes.items():
        if an.get("direction"):
            index.setdefault(iso, set()).add("TP")

    return index


def fmt_signal_flags(iso: str, index: dict) -> str:
    """
    Render a compact inline signal flag string for a given ISO.
    Fired signals shown in red, un-fired shown as dim dots.
    e.g.  BW ·  ·  TP ·  WB GF ·
    """
    parts = []
    fired = index.get(iso, set())
    for _, short in SIGNAL_SHORTS:
        if short in fired:
            parts.append(f"{RED}{BOLD}{short}{R}")
        else:
            parts.append(f"{DIM} · {R}")
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────
# BASELINE I/O
# ─────────────────────────────────────────────────────────────

def load_baseline() -> dict:
    if BASELINE_FILE.exists():
        try:   return json.loads(BASELINE_FILE.read_text())
        except: return {}
    return {}

def save_baseline(b: dict):
    BASELINE_FILE.write_text(json.dumps(b, indent=2, default=str))

def load_signals() -> dict:
    if SIGNALS_FILE.exists():
        try:   return json.loads(SIGNALS_FILE.read_text())
        except: return {}
    return {}

def save_signals(s: dict):
    SIGNALS_FILE.write_text(json.dumps(s, indent=2, default=str))

# ─────────────────────────────────────────────────────────────
# SAFE QUERY  —  1.1s base sleep, polite to Shodan servers
# ─────────────────────────────────────────────────────────────

def sq(api, query: str, facets=None, retries=3) -> dict:
    for attempt in range(retries):
        try:
            time.sleep(1.1)
            if facets:
                return api.count(query, facets=facets)
            return api.count(query)
        except Exception as e:
            msg = str(e).lower()
            if "rate limit" in msg or "429" in msg:
                wait = 5 + attempt * 3
                log.warning(f"Rate limit hit — backing off {wait}s")
                time.sleep(wait)
            elif attempt == retries - 1:
                log.debug(f"Query failed [{query[:60]}]: {e}")
                return {"total": 0}
            else:
                time.sleep(2)
    return {"total": 0}

# ─────────────────────────────────────────────────────────────
# QUERY HELPERS
# ─────────────────────────────────────────────────────────────

def _city_q(city: str, coords, iso: str, prefix: str = "") -> str:
    """Build a Shodan query for a city — geo radius or city name."""
    p = f"{prefix} " if prefix else ""
    if coords is not None:
        lat, lon = coords
        return f"{p}geo:{lat},{lon},50 country:{iso}"
    return f'{p}city:"{city}" country:{iso}'

# ─────────────────────────────────────────────────────────────
# SCAN FUNCTIONS
# ─────────────────────────────────────────────────────────────

def scan_all_nodes(api, iso_list, baseline):
    b = baseline.setdefault("all_nodes", {})
    results = {}
    for iso in iso_list:
        count = sq(api, f"country:{iso}")["total"]
        prev  = b.get(iso)
        delta = count - prev if prev is not None else None
        pct   = round(delta / prev * 100, 2) if prev and delta else None
        alert = bool(pct and (pct < -5 or pct > 10))
        direction = ("SHUTDOWN" if pct and pct < -5 else
                     "SURGE"    if pct and pct > 10 else None)
        results[iso] = {"count": count, "prev": prev, "delta": delta,
                        "pct": pct, "alert": alert, "direction": direction}
        b[iso] = count
    return results


def scan_ics_nodes(api, iso_list, baseline):
    b = baseline.setdefault("ics_nodes", {})
    results = {}
    for iso in iso_list:
        proto_data = {}
        total_ics  = 0
        for pname, pq in ICS_PROTOCOLS.items():
            count = sq(api, f"{pq} country:{iso}")["total"]
            bkey  = f"{iso}_{pname}"
            prev  = b.get(bkey)
            delta = count - prev if prev is not None else None
            proto_data[pname] = {"count": count, "prev": prev,
                                  "delta": delta, "alert": delta not in (None, 0)}
            b[bkey] = count
            total_ics += count
        results[iso] = {
            "protocols": proto_data,
            "total_ics": total_ics,
            "any_alert": any(v["alert"] for v in proto_data.values()),
        }
    return results


def scan_cities(api, baseline):
    b = baseline.setdefault("cities", {})
    results = {}

    for iso, city_spec in CITY_TARGETS.items():
        results[iso] = {}

        # Normalise to iterable of (city_name, coords_or_None)
        if isinstance(city_spec, dict):
            items = city_spec.items()
        else:
            items = [(c, None) for c in city_spec]

        for city, coords in items:
            total = sq(api, _city_q(city, coords, iso))["total"]

            ics_proto = {}
            ics_total = 0
            for pname, pq in ICS_PROTOCOLS.items():
                c = sq(api, _city_q(city, coords, iso, prefix=pq))["total"]
                ics_proto[pname] = c
                ics_total += c

            vpn_total = sum(
                sq(api, _city_q(city, coords, iso, prefix=vq))["total"]
                for vq in VPN_PROTOCOLS.values()
            )

            bt = f"{iso}_{city}_total"
            bi = f"{iso}_{city}_ics"
            bv = f"{iso}_{city}_vpn"

            prev_t = b.get(bt); prev_i = b.get(bi); prev_v = b.get(bv)
            dt = total     - prev_t if prev_t is not None else None
            di = ics_total - prev_i if prev_i is not None else None
            dv = vpn_total - prev_v if prev_v is not None else None

            alert = (
                (dt is not None and total > 0 and abs(dt) / total > 0.05) or
                (di is not None and di != 0)
            )

            results[iso][city] = {
                "total": total,      "prev_total": prev_t, "delta_total": dt,
                "ics_total": ics_total, "prev_ics": prev_i, "delta_ics": di,
                "ics_proto": ics_proto,
                "vpn_total": vpn_total, "prev_vpn": prev_v, "delta_vpn": dv,
                "alert": alert,
                "geo": coords is not None,   # flag so renderer can mark geo rows
            }
            b[bt] = total; b[bi] = ics_total; b[bv] = vpn_total

    return results


def scan_fusion_signals(api, iso_list, baseline):
    signals = {}

    # S1: Blackout Watch
    b = baseline.setdefault("blackout_watch", {})
    bw = {}
    for iso in iso_list[:10]:
        for pname, pq in ICS_PROTOCOLS.items():
            count = sq(api, f"{pq} country:{iso}")["total"]
            bkey  = f"{iso}_{pname}"
            prev  = b.get(bkey)
            delta = count - prev if prev is not None else None
            alert = bool(prev and prev > 0 and delta is not None
                         and delta < 0 and abs(delta) / prev > 0.20)
            bw[bkey] = {"count": count, "prev": prev, "delta": delta, "alert": alert}
            b[bkey]  = count
    signals["BLACKOUT_WATCH"] = {"data": bw, "alerts": [k for k, v in bw.items() if v["alert"]]}

    # S2: Cert Churn
    b = baseline.setdefault("cert_churn", {})
    cc = {}
    for iso in iso_list:
        count = sq(api, f"ssl.cert.expired:false country:{iso} port:443")["total"]
        prev  = b.get(iso)
        delta = count - prev if prev is not None else None
        alert = bool(prev and delta and delta > prev * 0.15)
        cc[iso] = {"count": count, "prev": prev, "delta": delta, "alert": alert}
        b[iso]  = count
    signals["CERT_CHURN"] = {"data": cc, "alerts": [k for k, v in cc.items() if v["alert"]]}

    # S3: Port Shadow
    b = baseline.setdefault("port_shadow", {})
    ps = {}
    for iso, asns in CRITICAL_ASNS.items():
        if iso not in iso_list: continue
        for asn in asns[:2]:
            res  = sq(api, f"asn:{asn}", facets=["port"])
            top  = {str(f["value"]): f["count"]
                    for f in res.get("facets", {}).get("port", [])[:20]}
            prev = b.get(asn, {})
            new_ = {p: c for p, c in top.items()
                    if p not in prev and int(p) in WATCH_PORTS}
            surg = {p: {"prev": prev[p], "now": c}
                    for p, c in top.items()
                    if p in prev and prev[p] > 0
                    and (c - prev[p]) / prev[p] > 0.50}
            ps[asn] = {"iso": iso, "new_ports": new_, "surged": surg,
                       "alert": bool(new_ or surg)}
            b[asn] = top
    signals["PORT_SHADOW"] = {"data": ps, "alerts": [k for k, v in ps.items() if v["alert"]]}

    # S4: Telecom Pulse — derived from all_nodes direction flag, no extra queries
    signals["TELECOM_PULSE"] = {"note": "Derived from ALL_NODES Δ", "alerts": []}

    # S5: VPN Surge
    b = baseline.setdefault("vpn_surge", {})
    vs = {}
    for iso in iso_list:
        protos = {}
        for vname, vq in VPN_PROTOCOLS.items():
            count = sq(api, f"{vq} country:{iso}")["total"]
            bkey  = f"{iso}_{vname}"
            prev  = b.get(bkey)
            delta = count - prev if prev is not None else None
            alert = bool(prev and delta and delta > 0 and delta / prev > 0.25)
            protos[vname] = {"count": count, "prev": prev, "delta": delta, "alert": alert}
            b[bkey] = count
        vs[iso] = {"protocols": protos, "alert": any(v["alert"] for v in protos.values())}
    signals["VPN_SURGE"] = {"data": vs, "alerts": [k for k, v in vs.items() if v["alert"]]}

    # S6: Webcam Blink
    b = baseline.setdefault("webcam_blink", {})
    wb = {}
    for iso in iso_list:
        count = sq(api, f'country:{iso} (port:554 OR port:8554 OR "webcam" OR "Hikvision" OR "Dahua")')["total"]
        prev  = b.get(iso)
        delta = count - prev if prev is not None else None
        alert = bool(prev and delta and delta < 0 and abs(delta) / prev > 0.10)
        wb[iso] = {"count": count, "prev": prev, "delta": delta, "alert": alert}
        b[iso]  = count
    signals["WEBCAM_BLINK"] = {"data": wb, "alerts": [k for k, v in wb.items() if v["alert"]]}

    # S7: Grid Fingerprint
    b = baseline.setdefault("grid_fp", {})
    gf = {}
    for iso in iso_list:
        pd_ = {}
        for pname in ["Modbus", "DNP3", "IEC-104"]:
            count = sq(api, f"{ICS_PROTOCOLS[pname]} country:{iso}")["total"]
            bkey  = f"{iso}_{pname}"
            prev  = b.get(bkey)
            delta = count - prev if prev is not None else None
            pd_[pname] = {"count": count, "prev": prev, "delta": delta,
                          "alert": delta not in (None, 0)}
            b[bkey] = count
        gf[iso] = {"protocols": pd_, "alert": any(v["alert"] for v in pd_.values())}
    signals["GRID_FINGERPRINT"] = {"data": gf, "alerts": [k for k, v in gf.items() if v["alert"]]}

    # S8: Dark Mirror
    b = baseline.setdefault("dark_mirror", {})
    dm = {}
    for iso in iso_list[:10]:
        res  = sq(api, f"country:{iso}", facets=["org"])
        top  = {f["value"]: f["count"]
                for f in res.get("facets", {}).get("org", [])[:5]}
        prev = b.get(iso, {})
        van  = {org: {"prev": prev[org], "now": c,
                      "pct": round((c - prev[org]) / prev[org] * 100, 1)}
                for org, c in top.items()
                if org in prev and prev[org] > 0
                and (c - prev[org]) / prev[org] < -0.30}
        dm[iso] = {"top_orgs": top, "vanishing": van, "alert": bool(van)}
        b[iso] = top
    signals["DARK_MIRROR"] = {"data": dm, "alerts": [k for k, v in dm.items() if v["alert"]]}

    return signals


# ─────────────────────────────────────────────────────────────
# TERMINAL RENDERER
# ─────────────────────────────────────────────────────────────

SIGNAL_DESC = {
    "BLACKOUT_WATCH":   "ICS/SCADA disappearance near strike zones (>20% drop)",
    "CERT_CHURN":       "SSL cert volume spike — emergency re-keying or replacement",
    "PORT_SHADOW":      "Novel/surging ports on gov & military ASNs",
    "TELECOM_PULSE":    "Country-wide host Δ — derived from all-nodes scan",
    "VPN_SURGE":        "VPN endpoint growth (>25%) — comms hardening signal",
    "WEBCAM_BLINK":     "Public camera drop (>10%) after kinetic events",
    "GRID_FINGERPRINT": "Power grid protocol (Modbus/DNP3/IEC-104) exposure Δ",
    "DARK_MIRROR":      "Top org host-count vanishing >30%",
}

# Legend line printed under each section header
_LEGEND = (f"  {DIM}Signals: "
           + "  ".join(f"{RED}{BOLD}{s}{R}{DIM}={n}{R}"
                       for n, s in SIGNAL_SHORTS)
           + f"   {RED}{BOLD}red=FIRING{R}  {DIM}· =nominal{R}")


def render_terminal(all_nodes, ics_nodes, cities, fusion, run_time):
    W = 148
    div = lambda c=WHT: f"{BOLD}{c}{'─'*W}{R}"

    # Build signal index once — used by both section renderers
    sig_index = build_signal_index(fusion, all_nodes)

    total_alerts = sum(len(s.get("alerts", [])) for s in fusion.values())
    # Add TP alerts from all_nodes direction
    total_alerts += sum(1 for an in all_nodes.values() if an.get("direction"))

    ac = RED if total_alerts > 4 else ORG if total_alerts > 0 else GRN

    print(f"\n{BOLD}{MAG}{'═'*W}{R}")
    print(f"{BOLD}{MAG}  ⬡  SHODAN INTELLIGENCE TERMINAL"
          f"   {DIM}{run_time}"
          f"   {ac}{total_alerts} ACTIVE SIGNAL FLAGS{R}")
    print(f"{BOLD}{MAG}{'═'*W}{R}")

    # ── SECTION 1: ALL NODES + ICS + INLINE SIGNALS ──────────
    print(f"\n{BOLD}{CYN}  SECTION 1 — ALL NODES & ICS EXPOSURE  ·  ALL 15 COUNTRIES{R}")
    print(_LEGEND)
    print(div())
    print(f"  {BOLD}{'COUNTRY':<18}{'ALL NODES':>12}{'Δ':>9}  "
          f"{'ICS TOT':>8}  "
          f"{'Modbus':>7}{'DNP3':>7}{'S7':>7}{'BACnet':>7}{'E/IP':>7}{'IEC-104':>8}"
          f"   {'BW':>2} {'CC':>2} {'PS':>2} {'TP':>2} {'VS':>2} {'WB':>2} {'GF':>2} {'DM':>2}{R}")
    print(div())

    for iso in sorted(all_nodes.keys()):
        an   = all_nodes.get(iso, {})
        ic   = ics_nodes.get(iso, {})
        name = COUNTRY_NAMES.get(iso, iso)
        p    = ic.get("protocols", {})

        mod = p.get("Modbus",  {}).get("count", 0)
        dnp = p.get("DNP3",    {}).get("count", 0)
        s7  = p.get("S7",      {}).get("count", 0)
        bac = p.get("BACnet",  {}).get("count", 0)
        eip = p.get("E/IP",    {}).get("count", 0)
        iec = p.get("IEC-104", {}).get("count", 0)

        flags = fmt_signal_flags(iso, sig_index)

        print(
            f"  {BOLD}{WHT}{name:<18}{R}"
            f"{CYN}{an.get('count', 0):>12,}{R}"
            f"{col_delta(an.get('delta'))}"
            f"  {ORG}{ic.get('total_ics', 0):>8,}{R}  "
            f"{ics_val(mod)}{ics_val(dnp)}{ics_val(s7)}"
            f"{ics_val(bac)}{ics_val(eip)}{ics_val(iec, 8)}"
            f"   {flags}"
        )
    print(div())

    # ── SECTION 2: CITY-LEVEL TABLES ─────────────────────────
    country_labels = {"IR": "IRAN", "IL": "ISRAEL", "PK": "PAKISTAN"}

    for iso, cdata in cities.items():
        label = country_labels.get(iso, iso)
        print(f"\n{BOLD}{YLW}  SECTION 2 — CITY-LEVEL INTELLIGENCE  ·  {label}{R}")
        print(_LEGEND)
        print(div())
        print(f"  {BOLD}{'CITY':<24}{'G':>2}{'NODES':>10}{'Δ':>9}  "
              f"{'ICS':>8}{'Δ':>7}  "
              f"{'Modbus':>7}{'DNP3':>7}{'S7':>7}{'BACnet':>7}{'E/IP':>7}{'IEC-104':>8}  "
              f"{'VPN':>7}   "
              f"{'BW':>2} {'CC':>2} {'PS':>2} {'TP':>2} {'VS':>2} {'WB':>2} {'GF':>2} {'DM':>2}{R}")
        print(div())

        for city, cd in cdata.items():
            p   = cd.get("ics_proto", {})
            mod = p.get("Modbus",  0); dnp = p.get("DNP3",    0)
            s7  = p.get("S7",      0); bac = p.get("BACnet",  0)
            eip = p.get("E/IP",    0); iec = p.get("IEC-104", 0)

            # City alert marker
            city_alert = cd.get("alert", False)
            city_mark  = f"{RED}!{R}" if city_alert else f"{DIM}·{R}"

            # Geo marker — show ⊕ if queried by coordinates
            geo_mark = f"{CYN}⊕{R}" if cd.get("geo") else f"{DIM}·{R}"

            # Inline flags: country-level signals for this city's parent country
            flags = fmt_signal_flags(iso, sig_index)

            print(
                f"  {city_mark}{BOLD}{WHT}{city:<23}{R}"
                f"{geo_mark}"
                f"{CYN}{cd['total']:>10,}{R}"
                f"{col_delta(cd['delta_total'])}"
                f"  {ORG}{cd['ics_total']:>8,}{R}"
                f"{col_delta(cd['delta_ics'], 7)}  "
                f"{ics_val(mod)}{ics_val(dnp)}{ics_val(s7)}"
                f"{ics_val(bac)}{ics_val(eip)}{ics_val(iec, 8)}  "
                f"{YLW}{cd['vpn_total']:>7,}{R}   "
                f"{flags}"
            )
        print(div())

    # ── SECTION 3: FUSION SIGNAL DETAIL ──────────────────────
    print(f"\n{BOLD}{MAG}  SECTION 3 — FUSION SIGNAL DETAIL{R}")
    print(div())

    for sname, desc in SIGNAL_DESC.items():
        sig    = fusion.get(sname, {})
        alerts = sig.get("alerts", [])
        # TP: pull from all_nodes
        if sname == "TELECOM_PULSE":
            alerts = [f"{iso}({an['direction']})"
                      for iso, an in all_nodes.items() if an.get("direction")]
        n      = len(alerts)
        sc     = RED if n > 0 else GRN
        status = f"{sc}{BOLD}⚠ {n} ALERT{'S' if n > 1 else ''}{R}" if n > 0 else f"{GRN}✓ NOMINAL{R}"
        short  = next(s for _, s in SIGNAL_SHORTS if _ == sname)
        print(f"  {BOLD}{RED if n > 0 else MAG}[{short}]{R} "
              f"{BOLD}{MAG}{sname:<22}{R}  {status:<32}  {DIM}{desc}{R}")
        for a in alerts[:3]:
            print(f"       {DIM}↳ {a}{R}")
        if len(alerts) > 3:
            print(f"       {DIM}    ... +{len(alerts)-3} more — see shodan_signals.json{R}")

    print(div())

    # ── Footer ────────────────────────────────────────────────
    total_ics  = sum(ics_nodes.get(iso, {}).get("total_ics", 0) for iso in all_nodes)
    total_nod  = sum(v.get("count", 0) for v in all_nodes.values())
    city_ics   = sum(
        cd.get("ics_total", 0)
        for iso_data in cities.values()
        for cd in iso_data.values()
    )
    n_geo = sum(
        1 for iso_data in cities.values()
        for cd in iso_data.values() if cd.get("geo")
    )

    print(
        f"\n  {DIM}Monitored nodes: {WHT}{total_nod:,}{R}  "
        f"{DIM}│  Country ICS exposed: "
        f"{RED if total_ics > 0 else DIM}{total_ics:,}{R}  "
        f"{DIM}│  City ICS exposed: "
        f"{RED if city_ics > 0 else DIM}{city_ics:,}{R}  "
        f"{DIM}│  Geo-queried sites: {CYN}{n_geo}{R}  "
        f"{DIM}│  ⊕ = geo radius query  · = city-name query{R}\n"
    )


# ─────────────────────────────────────────────────────────────
# MAIN CYCLE
# ─────────────────────────────────────────────────────────────

def run_shodan_cycle(cities_only=False, country_filter=None) -> dict:
    run_time = datetime.now(EST).strftime("%Y-%m-%d %H:%M EST")

    if not SHODAN_API_KEY:
        log.warning("SHODAN_KEY not set — skipping Shodan cycle")
        return {}

    try:
        import shodan as shodan_lib
        api = shodan_lib.Shodan(SHODAN_API_KEY)
        info = api.info()
        query_credits = info.get("query_credits", "?")
        scan_credits  = info.get("scan_credits",  "?")
        plan          = info.get("plan", "?")
        log.info(f"Shodan auth OK — plan:{plan}  query_credits:{query_credits}  scan_credits:{scan_credits}")
        print(f"\n{BOLD}{GRN}  ✓ Shodan API authenticated{R}"
              f"   {DIM}plan={plan}  query_credits={query_credits}  scan_credits={scan_credits}{R}\n")
    except Exception as e:
        log.error(f"Shodan auth failed: {e}")
        print(f"\n{RED}{BOLD}  ✗ Shodan auth failed: {e}{R}\n")
        return {}

    baseline = load_baseline()
    iso_list = sorted(ALL_COUNTRIES.values())
    if country_filter:
        iso_list = [c.strip().upper() for c in country_filter.split(",")]

    n_cities = sum(
        len(v) if isinstance(v, (list, dict)) else 0
        for v in CITY_TARGETS.values()
    )
    log.info(f"Full scan — {len(iso_list)} countries, {n_cities} city/site targets")

    all_nodes = {}; ics_nodes = {}; fusion = {}; cities = {}

    if not cities_only:
        log.info("  All-nodes scan...")
        all_nodes = scan_all_nodes(api, iso_list, baseline)

        log.info("  ICS/SCADA node counts...")
        ics_nodes = scan_ics_nodes(api, iso_list, baseline)

        log.info("  Running 8 fusion signals...")
        fusion = scan_fusion_signals(api, iso_list, baseline)

    log.info("  City/site-level scan (IR / IL / PK)...")
    cities = scan_cities(api, baseline)

    save_baseline(baseline)

    all_alerts = []
    for sname, sig in fusion.items():
        for ak in sig.get("alerts", []):
            all_alerts.append({"signal": sname, "key": ak, "ts": run_time})
    for iso, cdata in cities.items():
        for city, cd in cdata.items():
            if cd["alert"]:
                all_alerts.append({"signal": "CITY_ALERT",
                                   "key": f"{city},{iso}", "ts": run_time})

    output = {
        "last_run": run_time,
        "all_nodes": all_nodes, "ics_nodes": ics_nodes,
        "cities": cities, "fusion": fusion,
        "alerts": all_alerts, "total_alerts": len(all_alerts),
    }
    save_signals(output)
    render_terminal(all_nodes, ics_nodes, cities, fusion, run_time)
    log.info(f"Scan complete. {len(all_alerts)} alerts.")
    return output


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GDELT × Shodan Intelligence Terminal")
    parser.add_argument("--status",      action="store_true",
                        help="Print last saved shodan_signals.json and exit")
    parser.add_argument("--cities-only", action="store_true",
                        help="Skip country-level scans, run city/site scan only")
    parser.add_argument("--country",     help="Comma-separated ISO codes, e.g. IR,IL,PK")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(load_signals(), indent=2, default=str))
        return

    run_shodan_cycle(cities_only=args.cities_only, country_filter=args.country)


if __name__ == "__main__":
    main()
