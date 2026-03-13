"""
main.py — FastAPI web wrapper for flightOilTeleg.py
=================================================
flightOilTeleg.py is NOT modified at all.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
import uvicorn

from flightOilTeleg import DataOrchestrator

app = FastAPI()
orchestrator: DataOrchestrator = None


def strip_markup(text: str) -> str:
    return re.sub(r'\[/?[^\]]*\]', '', str(text))


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
                    print("[gowell] Telegram session invalid. Skipping.")
            except Exception as e:
                orchestrator.status["telegram"] = "CONN_ERR"
                print(f"[gowell] Telegram connect failed: {e}")
        else:
            orchestrator.status["telegram"] = "NO_SESSION"
            print("[gowell] No session file — Telegram disabled.")

    asyncio.create_task(orchestrator.monitor_fuel_prices())
    asyncio.create_task(orchestrator.monitor_liveuamap())
    asyncio.create_task(orchestrator.monitor_airports_adsb())
    asyncio.create_task(orchestrator.monitor_parseek())
    asyncio.create_task(orchestrator.daily_archiver())

    if tg_enabled:
        asyncio.create_task(orchestrator.start_telegram_listener())

    print("[gowell] All monitors started.")


@app.get("/data")
def get_data():
    if orchestrator is None:
        return Response(
            content=json.dumps({"error": "initialising"}),
            media_type="application/json"
        )

    s = orchestrator.status

    raw_logs      = [strip_markup(l) for l in s.get("raw_tg_logs", [])]
    analysis_logs = [strip_markup(l.get("display", "")) for l in s.get("analysis_logs", [])]

    payload = {
        "internet":              s.get("internet", "?"),
        "adsb_status":           s.get("adsb_status", "?"),
        "telegram":              s.get("telegram", "?"),
        "last_tg_signal":        strip_markup(s.get("last_tg_signal", "")),
        "fpc_data":              s.get("fpc_data", {}),
        "fpc_sources":           s.get("fpc_sources", []),
        "regional_airports":     s.get("regional_airports", {}),
        "liveuamap_events":      s.get("liveuamap_events", []),
        "liveuamap_last_status": s.get("liveuamap_last_status", ""),
        "liveuamap_last_call":   s.get("liveuamap_last_call", ""),
        "liveuamap_last_count":  s.get("liveuamap_last_count", 0),
        "liveuamap_newest_ts":   s.get("liveuamap_newest_ts", "—"),
        "next_liveuamap_update": s.get("next_liveuamap_update", 0),
        "raw_tg_logs":           raw_logs,
        "analysis_logs":         analysis_logs,
        "server_time":           datetime.utcnow().isoformat(),
    }

    # json.dumps with default=str handles datetime, sets, and any other
    # non-serializable objects that might be in the status dict
    return Response(
        content=json.dumps(payload, default=str),
        media_type="application/json"
    )


@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("template.html", "r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
