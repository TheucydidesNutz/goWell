"""
main.py — FastAPI web wrapper for shodan_monitor.py
=====================================================
shodan_monitor.py is NOT modified at all.
This file:
  • Imports run_shodan_cycle() and load_signals() from it
  • Runs the scan in a background thread every 15 minutes
  • Serves a live HTML dashboard at /
  • Exposes /data (JSON) so the page can poll for updates

Railway reads PORT from the environment automatically.
"""

import json
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# ── Import existing logic — zero changes to shodan_monitor.py ──
from shodan_monitor import run_shodan_cycle, load_signals

EST = ZoneInfo("America/New_York")
app = FastAPI()

# ── Shared scan state ──────────────────────────────────────────
_state = {
    "last_run":       "Never",
    "next_run":       None,       # epoch float
    "scan_running":   False,
    "signals":        {},
}
SCAN_INTERVAL = 900   # 15 minutes


# ─────────────────────────────────────────────────────────────
# BACKGROUND SCAN THREAD
# ─────────────────────────────────────────────────────────────

def _scan_loop():
    """Runs forever: scan → sleep 15 min → repeat."""
    while True:
        _state["scan_running"] = True
        _state["last_run"]     = datetime.now(EST).strftime("%Y-%m-%d %H:%M EST")
        try:
            result = run_shodan_cycle()
            if result:
                _state["signals"] = result
        except Exception as e:
            print(f"[scan_loop] ERROR: {e}")
        finally:
            _state["scan_running"] = False

        _state["next_run"] = time.time() + SCAN_INTERVAL
        time.sleep(SCAN_INTERVAL)


@app.on_event("startup")
async def startup():
    # Load any cached signals from the last run (survives restarts)
    cached = load_signals()
    if cached:
        _state["signals"]  = cached
        _state["last_run"] = cached.get("last_run", "cached")

    # Set next_run so the countdown shows something immediately
    _state["next_run"] = time.time() + 30   # first scan in 30s

    t = threading.Thread(target=_scan_loop, daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/data")
def get_data():
    """Returns current scan results + metadata as JSON."""
    secs_left = max(0, int((_state["next_run"] or 0) - time.time()))
    return JSONResponse({
        "last_run":     _state["last_run"],
        "scan_running": _state["scan_running"],
        "next_run_secs": secs_left,
        "signals":      _state["signals"],
    })


@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("template.html", "r", encoding="utf-8") as f:
        return f.read()


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
