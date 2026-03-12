"""
main.py — FastAPI web wrapper for flightOilTeleg.py
=================================================
flightOilTeleg.py is NOT modified at all.
This file:
  • Imports DataOrchestrator directly and starts all async monitors
  • Skips the Rich Live renderer and the interactive input() prompt
  • Serves a live HTML dashboard at /
  • Exposes /data (JSON) so the page can poll for updates every 10s

Telegram note
─────────────
Telethon requires a pre-authorised session file to connect without
an interactive prompt. Create the session on your local machine first:

    python3 -c "
    import asyncio
    from telethon import TelegramClient
    import os
    from dotenv import load_dotenv
    load_dotenv()
    async def auth():
        c = TelegramClient('osint_session', int(os.getenv('TG_ID')), os.getenv('TG_HASH'))
        await c.start()
        print('Session saved.')
        await c.disconnect()
    asyncio.run(auth())
    "

Then upload the resulting osint_session.session file to the gowell/
folder in GitHub alongside this main.py. Railway will use it automatically.
If no session file is present, Telegram is skipped but all other monitors run.
"""

import asyncio
import json
import os
import time
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from flightOilTeleg import DataOrchestrator

app = FastAPI()
orchestrator: DataOrchestrator = None


# ─────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global orchestrator

    config = {
        "adsb_key":  os.getenv("ADS_B_KEY"),
        "opa_key":   os.getenv("OILPRICEAPI"),
        "tg_id":     int(os.getenv("TG_ID", 0)) if os.getenv("TG_ID") else None,
        "tg_hash":   os.getenv("TG_HASH"),
    }

    orchestrator = DataOrchestrator(config)
    orchestrator.check_internet()

    # ── Telegram ─────────────────────────────────────────────
    # Only attempt if a pre-authorised session file exists.
    # No interactive prompt — Railway can't handle that.
    tg_enabled = False
    if config["tg_id"] and config["tg_hash"]:
        session_file = "osint_session.session"
        if os.path.exists(session_file):
            try:
                await orchestrator.tg_client.connect()
                if await orchestrator.tg_client.is_user_authorized():
                    orchestrator.status["telegram"] = "AUTHORIZED"
                    tg_enabled = True
                    print("[gowell] Telegram session authorised.")
                else:
                    orchestrator.status["telegram"] = "SESSION_INVALID"
                    print("[gowell] Telegram session file found but not authorised. Skipping.")
            except Exception as e:
                orchestrator.status["telegram"] = f"CONN_ERR"
                print(f"[gowell] Telegram connect failed: {e}")
        else:
            orchestrator.status["telegram"] = "NO_SESSION"
            print("[gowell] No osint_session.session file found — Telegram disabled.")

    # ── Background tasks ──────────────────────────────────────
    asyncio.create_task(orchestrator.monitor_fuel_prices())
    asyncio.create_task(orchestrator.monitor_liveuamap())
    asyncio.create_task(orchestrator.monitor_airports_adsb())
    asyncio.create_task(orchestrator.monitor_parseek())
    asyncio.create_task(orchestrator.daily_archiver())

    if tg_enabled:
        asyncio.create_task(orchestrator.start_telegram_listener())

    print("[gowell] All monitors started.")


# ─────────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/data")
def get_data():
    """Returns current status dict as JSON — safe for browser polling."""
    if orchestrator is None:
        return JSONResponse({"error": "initialising"})

    s = orchestrator.status

    # Strip Rich markup from log strings before sending to browser
    def strip_markup(text: str) -> str:
        import re
        return re.sub(r'\[/?[^\]]*\]', '', str(text))

    raw_logs = [strip_markup(l) for l in s.get("raw_tg_logs", [])]
    analysis_logs = [strip_markup(l.get("display", "")) for l in s.get("analysis_logs", [])]

    return JSONResponse({
        "internet":           s.get("internet", "?"),
        "adsb_status":        s.get("adsb_status", "?"),
        "telegram":           s.get("telegram", "?"),
        "last_tg_signal":     strip_markup(s.get("last_tg_signal", "")),
        "fpc_data":           s.get("fpc_data", {}),
        "fpc_sources":        s.get("fpc_sources", []),
        "regional_airports":  s.get("regional_airports", {}),
        "liveuamap_events":   s.get("liveuamap_events", []),
        "liveuamap_last_status": s.get("liveuamap_last_status", ""),
        "liveuamap_last_call":   s.get("liveuamap_last_call", ""),
        "liveuamap_last_count":  s.get("liveuamap_last_count", 0),
        "liveuamap_newest_ts":   s.get("liveuamap_newest_ts", "—"),
        "next_liveuamap_update": s.get("next_liveuamap_update", 0),
        "raw_tg_logs":        raw_logs,
        "analysis_logs":      analysis_logs,
        "server_time":        datetime.utcnow().isoformat(),
    }, default=str)


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
