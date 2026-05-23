"""
HTTP server that exposes the Alexa Flash Briefing feed.

    GET /alexa-brief   -> the JSON feed Alexa polls
    GET /              -> health check

Run with:

    uvicorn server:app --host 0.0.0.0 --port 8000

Alexa requires the feed over HTTPS, so put this behind a reverse proxy
(Caddy, Nginx) or a tunnel (Cloudflare Tunnel, ngrok). See the README.
"""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

BASE_DIR = Path(__file__).resolve().parent
FEED_PATH = BASE_DIR / "output" / "alexa_feed.json"

app = FastAPI(title="Daily Brief — Alexa Flash Briefing")


@app.get("/")
def health():
    return {"status": "ok", "feed_exists": FEED_PATH.exists()}


@app.get("/alexa-brief")
def alexa_brief():
    if not FEED_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="No brief has been generated yet. Run `python main.py` first.",
        )
    try:
        data = json.loads(FEED_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail="Feed is unreadable: {}".format(exc))

    # Alexa accepts a single feed item or a list of them. We serve the single
    # item as written by main.py. If your Alexa skill insists on a list,
    # change the next line to: return JSONResponse([data])
    return JSONResponse(data)
