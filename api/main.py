"""PV Engine REST API — FastAPI app entry point.

Run locally:
    uvicorn api.main:app --reload --port 8000

Interactive docs: http://localhost:8000/docs
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Make sibling modules (main.py, schemas.py, geocoding.py, ...) importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pv_engine_api")

API_VERSION = "2.0.0"

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

# ── CORS ──────────────────────────────────────────────────────────────────────
# Use a regex so wildcard subdomains (vercel previews) and explicit dev origins
# both work. CORSMiddleware does NOT support glob patterns in `allow_origins`.
ALLOWED_ORIGINS = [
    "http://localhost:3000",       # Next.js dev
    "http://localhost:3001",       # Next.js dev (alt)
    "http://localhost:8501",       # Streamlit dev
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8501",
]
ALLOWED_ORIGIN_REGEX = (
    r"https://([a-z0-9-]+\.)*vercel\.app"
    r"|https://([a-z0-9-]+\.)*pvengine\.it"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routers ───────────────────────────────────────────────────────────────────
from api.routers import location as location_router
from api.routers import building as building_router
from api.routers import loads as loads_router
from api.routers import pv as pv_router
from api.routers import economics as economics_router
from api.routers import simulate as simulate_router

app.include_router(location_router.router)
app.include_router(building_router.router)
app.include_router(loads_router.router)
app.include_router(pv_router.router)
app.include_router(economics_router.router)
app.include_router(simulate_router.router)


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
async def health() -> dict:
    from api.utils.cache import cache_size

    modules = {"engine": False, "school": False, "pvgis": False}
    try:
        import engine.solar  # noqa: F401
        modules["engine"] = True
    except Exception:
        pass
    try:
        import school.loads_school  # noqa: F401
        modules["school"] = True
    except Exception:
        pass
    try:
        import engine.pvgis_client  # noqa: F401
        modules["pvgis"] = True
    except Exception:
        pass

    return {
        "status": "ok",
        "version": API_VERSION,
        "modules": modules,
        "cache_size": cache_size(),
    }
