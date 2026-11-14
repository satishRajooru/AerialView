"""HTTP API + static map UI.  Run:  uvicorn app.main:app --port 8000"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import config
from .geo import Geocoder, region_from_bbox
from .monitor import Monitor
from .query import QueryEngine
from .store import make_store

STATIC = Path(__file__).resolve().parent.parent / "static"

store = make_store()
geocoder = Geocoder(store)
monitor = Monitor(store, geocoder)
engine = QueryEngine(monitor)


@asynccontextmanager
async def lifespan(_app):
    monitor.start()
    yield
    monitor.stop()


app = FastAPI(title="Aerial Monitor", lifespan=lifespan)


class VercelRewriteFix:
    """vercel.json rewrites "/<path>" to "/api/index?__path=/<path>", and Vercel hands the app the
    rewritten path. Restore the original so routes match. No-op everywhere else."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            qs = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
            orig = next((v for k, v in qs if k == "__path"), None)
            if orig is not None:
                path = "/" + orig.lstrip("/")
                scope = dict(scope, path=path, raw_path=path.encode(),
                             query_string=urlencode([(k, v) for k, v in qs if k != "__path"]).encode())
            elif scope.get("path", "").startswith("/api/index"):
                path = scope["path"][len("/api/index"):] or "/"
                scope = dict(scope, path=path, raw_path=path.encode())
        await self.inner(scope, receive, send)


app.add_middleware(VercelRewriteFix)


@app.exception_handler(404)
async def not_found(request, exc):
    # Include what the app actually received: makes hosting/rewrite problems obvious.
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": getattr(exc, "detail", "Not Found"), "path_seen": request.url.path,
                         "root_path": request.scope.get("root_path", ""),
                         "query": request.url.query}, status_code=404)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def ready():
    """Idempotent startup + (on serverless) an on-demand snapshot refresh."""
    monitor.start()
    monitor.ensure_fresh()


@app.get("/api/status")
def status():
    ready()
    return {"serverless": config.SERVERLESS, "fallback_demo": monitor.using_fallback,
            "persistent_history": getattr(store, "persistent", True), "source": monitor.source.name, "snapshot_ts": monitor.snapshot_ts, "aircraft": len(monitor.states),
            "airborne": sum(1 for a in monitor.states if not a["on_ground"]), "last_error": monitor.last_error,
            "watching": [r.name for r in monitor.regions.values()], "poll_seconds": config.POLL_SECONDS,
            "llm": config.USE_LLM, "source_errors": getattr(monitor.source, "last_errors", [])[:10]}


@app.get("/api/states")
def states(s: float = -90, w: float = -180, n: float = 90, e: float = 180, limit: int = 15000):
    """Compact aircraft list for the map: [icao24, callsign, lat, lon, alt_m, heading, velocity, on_ground, origin]."""
    ready()
    out = []
    for a in monitor.states:
        if s <= a["lat"] <= n and (w <= a["lon"] <= e if w <= e else (a["lon"] >= w or a["lon"] <= e)):
            out.append([a["icao24"], a["callsign"], round(a["lat"], 4), round(a["lon"], 4), a.get("alt_m"),
                        a.get("heading"), a.get("velocity"), a["on_ground"], a.get("origin_country") or a.get("desc")])
            if len(out) >= limit:
                break
    return {"ts": monitor.snapshot_ts, "total": len(monitor.states), "aircraft": out}


class Ask(BaseModel):
    q: str


@app.post("/api/query")
async def query(body: Ask):
    if not body.q.strip():
        raise HTTPException(400, "Empty question")
    await run_in_threadpool(ready)
    res = await run_in_threadpool(engine.ask, body.q)
    res["aircraft"] = [[a["icao24"], a["callsign"], a["lat"], a["lon"], a.get("alt_m"), a.get("heading"),
                        a.get("velocity"), a["on_ground"], a.get("origin_country") or a.get("desc")] for a in res["aircraft"]]
    return res


@app.get("/api/count")
def count_bbox(s: float, w: float, n: float, e: float):
    """Aircraft inside an arbitrary box (used by the map's 'count visible area' button)."""
    ready()
    r = region_from_bbox("selected area", s, w, n, e)
    inside = [a for a in monitor.states if r.contains(a["lat"], a["lon"]) and not a["on_ground"]]
    return {"count": len(inside)}


@app.get("/api/history/{place}")
async def history(place: str, hours: int = 48):
    await run_in_threadpool(ready)
    r = await run_in_threadpool(geocoder.resolve, place)
    monitor.watch(r)
    import time as _t
    rows = store.counts(r.key, int(_t.time()) - hours * 3600)
    return {"region": r.name, "points": [{"ts": t, "inside": i, "inbound": b} for t, i, b in rows]}


@app.api_route("/api/collect", methods=["GET", "POST"])
def collect(authorization: str | None = Header(default=None)):
    """Record a snapshot of every watched region now. Hit this on a schedule on serverless
    hosts (see .github/workflows/collect.yml) so spike baselines keep building."""
    if config.COLLECT_TOKEN and authorization != f"Bearer {config.COLLECT_TOKEN}":
        raise HTTPException(401, "Bad or missing collect token")
    monitor.start()
    if config.SERVERLESS:
        monitor.ensure_fresh(force_record=True)
    else:
        monitor.poll_once()
    return {"snapshot_ts": monitor.snapshot_ts, "aircraft": len(monitor.states), "regions": len(monitor.regions),
            "fallback_demo": monitor.using_fallback, "last_error": monitor.last_error}

