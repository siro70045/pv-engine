# PV Engine — REST API

FastAPI service exposing the PV feasibility engine as a set of granular HTTP
endpoints, plus a monolithic `/simulate` for backwards compatibility with the
Streamlit wizards (`app.py`, `app_school.py`).

## Run locally

```bash
pip install -r requirements.txt
uvicorn api.main:app --reload --port 8000
```

Interactive docs: http://localhost:8000/docs

To mimic the production environment (JSON logs, rate limiting on, env tag):

```bash
LOG_FORMAT=json LOG_LEVEL=INFO ENVIRONMENT=production \
  uvicorn api.main:app --port 8000 --proxy-headers
```

## Endpoints

| Group       | Method · Path                          | Rate limit  |
| ----------- | -------------------------------------- | ----------- |
| system      | `GET  /`                               | —           |
| system      | `GET  /health`                         | —           |
| location    | `POST /location/geocode`               | 60/min      |
| location    | `POST /location/azimuth`               | —           |
| location    | `POST /location/tmy`                   | 30/min      |
| location    | `POST /location/tmy/hourly`            | 10/min      |
| location    | `POST /location/climate-zone`          | —           |
| location    | `POST /location/region`                | —           |
| building    | `POST /building/osm-nearby`            | 30/min      |
| building    | `POST /building/roof-estimate`         | —           |
| building    | `POST /building/thermal-factor`        | —           |
| loads       | `POST /loads/residential`              | —           |
| loads       | `POST /loads/school`                   | 20/min      |
| loads       | `POST /loads/school/benchmarks`        | —           |
| pv          | `POST /pv/production`                  | 20/min      |
| pv          | `POST /pv/cost-estimate`               | —           |
| pv          | `POST /pv/auto-size`                   | —           |
| economics   | `POST /economics/balance`              | —           |
| economics   | `POST /economics/kpis`                 | —           |
| economics   | `POST /economics/incentives/residential` | —         |
| economics   | `POST /economics/incentives/school`    | —           |
| economics   | `POST /economics/scenario-comparison`  | (via /simulate, 10/min) |
| simulate    | `POST /simulate`                       | 10/min      |
| simulate    | `POST /simulate/school`                | 10/min      |

`/health` returns `200 OK` when PVGIS is reachable, `503 Service Unavailable`
otherwise. The body lists module-import status, app uptime, and TMY cache
statistics — Railway uses it for the health check probe.

## Environment variables

See `.env.example` for the canonical list. Summary:

| Variable             | Default              | Notes                                     |
| -------------------- | -------------------- | ----------------------------------------- |
| `ANTHROPIC_API_KEY`  | —                    | Required only by the Streamlit wizards.   |
| `ENVIRONMENT`        | `development`        | Echoed in `/health`. Set to `production`. |
| `LOG_LEVEL`          | `INFO`               | DEBUG / INFO / WARNING / ERROR.           |
| `LOG_FORMAT`         | `text`               | Use `json` on Railway.                    |
| `CORS_ORIGINS`       | (none)               | Comma-separated origins added to defaults.|
| `PVGIS_CACHE_DIR`    | `/tmp/pvgis_cache`   | Disk path for persisted TMY responses.    |
| `PVGIS_CACHE_SIZE`   | `500`                | In-memory LRU size.                       |
| `RATE_LIMIT_ENABLED` | `true`               | Set to `false` for load testing.          |

## Caching

TMY responses go through two layers:

1. **In-memory LRU** (`api/utils/cache.py`) — keyed on coordinates rounded to
   0.01°, so nearby points reuse the same fetch within a single process.
2. **On-disk JSON** at `PVGIS_CACHE_DIR` — survives Railway redeploys, which
   wipe the LRU.

A miss in both layers triggers a live PVGIS request (1–8 s typical). If the
disk is read-only or out of quota, persistence is skipped silently.

## Deploy on Railway

1. Push to a branch tracked by the Railway project — the `NIXPACKS` builder in
   `railway.json` runs `pip install -r requirements.txt` and starts the
   service via the configured `startCommand`.
2. In the Railway dashboard, set the environment variables you want to
   override (typically `ENVIRONMENT=production`, `LOG_FORMAT=json`,
   `CORS_ORIGINS=...`).
3. Railway probes `/health` on every deploy; a `503` keeps traffic on the
   previous revision.
4. Logs stream to the **Deployments** tab. With `LOG_FORMAT=json` they parse
   cleanly into Railway's filters.

The `--proxy-headers --forwarded-allow-ips="*"` flags in the start command
ensure that `X-Forwarded-For` from the Railway edge proxy is honoured — the
slowapi rate limiter relies on this to see real client IPs.
