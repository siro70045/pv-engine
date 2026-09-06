"""PV Engine REST API — FastAPI app entry point.

Run locally:
    uvicorn api.main:app --reload --port 8000

Production-like (JSON logs):
    LOG_FORMAT=json LOG_LEVEL=INFO uvicorn api.main:app --port 8000

Interactive docs: http://localhost:8000/docs
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

# Make sibling modules (main.py, schemas.py, geocoding.py, ...) importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT_JSON = os.getenv("LOG_FORMAT", "text").lower() == "json"


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# Reset root handlers (uvicorn may install its own before we run)
root_logger = logging.getLogger()
for h in list(root_logger.handlers):
    root_logger.removeHandler(h)

_handler = logging.StreamHandler()
if LOG_FORMAT_JSON:
    _handler.setFormatter(JSONFormatter())
else:
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
root_logger.addHandler(_handler)
root_logger.setLevel(LOG_LEVEL)

logger = logging.getLogger("pv_engine_api")


# ── App ───────────────────────────────────────────────────────────────────────
API_VERSION = "2.0.0"
APP_START_TIME = time.time()
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

app = FastAPI(
    title="PV Engine API",
    description=(
        "Modular REST API for the PV Engine.\n\n"
        "Six endpoint groups expose granular building blocks (location, "
        "building geometry, loads, PV, economics) plus a monolithic `/simulate` "
        "endpoint preserved for backwards compatibility with the Streamlit wizards."
    ),
    version=API_VERSION,
    contact={"name": "PV Engine"},
)


# ── Rate limiting ─────────────────────────────────────────────────────────────
from api.utils.limiter import limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── CORS ──────────────────────────────────────────────────────────────────────
def get_allowed_origins() -> list[str]:
    origins = [
        "http://localhost:3000",       # Next.js dev
        "http://localhost:3001",       # Next.js dev (alt)
        "http://localhost:8501",       # Streamlit dev
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8501",
    ]
    env_origins = os.getenv("CORS_ORIGINS", "")
    if env_origins:
        origins.extend(o.strip() for o in env_origins.split(",") if o.strip())
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_origin_regex=r"https://([a-z0-9-]+\.)*vercel\.app|https://([a-z0-9-]+\.)*pvengine\.it",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ── Request logging + error handler ──────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = (time.time() - start) * 1000
        logger.exception(
            "%s %s -> ERROR (%.0fms)",
            request.method, request.url.path, duration_ms,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error",
                     "error_type": type(exc).__name__},
        )
    duration_ms = (time.time() - start) * 1000
    logger.info(
        "%s %s -> %d (%.0fms)",
        request.method, request.url.path, response.status_code, duration_ms,
    )
    response.headers["X-Process-Time"] = f"{duration_ms:.0f}ms"
    return response


# ── Routers ───────────────────────────────────────────────────────────────────
from api.routers import location as location_router
from api.routers import building as building_router
from api.routers import loads as loads_router
from api.routers import pv as pv_router
from api.routers import economics as economics_router
from api.routers import simulate as simulate_router
from api.routers import estimate as estimate_router
from api.routers import estimate_casa as estimate_casa_router

app.include_router(location_router.router)
app.include_router(building_router.router)
app.include_router(loads_router.router)
app.include_router(pv_router.router)
app.include_router(economics_router.router)
app.include_router(simulate_router.router)
app.include_router(estimate_router.router)
app.include_router(estimate_casa_router.router)


# ── Root + advanced health ────────────────────────────────────────────────────
@app.get("/", tags=["system"], summary="API banner")
async def root() -> dict:
    return {
        "name": "PV Engine API",
        "version": API_VERSION,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["system"], summary="Detailed health check")
async def health():
    from api.utils.cache import cache_stats

    modules = {"engine": False, "school": False, "pvgis_reachable": False}
    try:
        import engine.solar  # noqa: F401
        modules["engine"] = True
    except Exception:
        logger.exception("engine import failed")
    try:
        import school.loads_school  # noqa: F401
        modules["school"] = True
    except Exception:
        logger.exception("school import failed")

    # Quick PVGIS reachability probe — any HTTP response (incl. 4xx) means the
    # server is up; only network errors / timeouts count as unreachable.
    try:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            "https://re.jrc.ec.europa.eu/api/v5_2/",
            method="HEAD",
            headers={"User-Agent": "pv-engine-healthcheck/1.0"},
        )
        urllib.request.urlopen(req, timeout=2)
        modules["pvgis_reachable"] = True
    except urllib.error.HTTPError:
        modules["pvgis_reachable"] = True
    except Exception as e:
        logger.warning("PVGIS probe failed: %s", e)

    payload = {
        "status": "ok" if modules["pvgis_reachable"] else "degraded",
        "version": API_VERSION,
        "uptime_seconds": int(time.time() - APP_START_TIME),
        "environment": ENVIRONMENT,
        "modules": modules,
        "cache": cache_stats(),
    }
    status_code = 200 if modules["pvgis_reachable"] else 503
    return JSONResponse(status_code=status_code, content=payload)
