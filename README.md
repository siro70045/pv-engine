# Meridiana: engine and API

Meridiana estimates whether a solar system on a specific roof is worth the money. This repository is the engine behind it: hour-by-hour solar physics on satellite weather data, the building's hourly energy balance, heat pumps and batteries, and a 25-year cash-flow model, exposed through a web API. The website lives in [pv-engine-web](https://github.com/siro70045/pv-engine-web). 

- Household estimator (first version): https://meridiana.site/casa/stima
- School estimator (first version): https://meridiana.site/scuole/stima

## How it works

| Step | Module | What it does |
| --- | --- | --- |
| Weather | `engine/pvgis_client.py` | Downloads a Typical Meteorological Year (8,760 hours) from PVGIS for the exact location, with a cache |
| Sun and panel | `engine/solar.py` | Sun position, Perez transposition to the panel plane, glass reflection losses, Faiman cell temperature, inverter |
| Demand | `engine/loads.py`, `school/`, `residential/` | Hourly demand profiles for schools and households |
| Heat pump | `engine/heat_pump.py` | COP from a Carnot model, seasonal COP from the local weather year |
| Energy balance | `engine/energy_balance.py` | Hour-by-hour self-consumption, export, import and battery |
| Economics | `engine/economics.py` | Cash flows, NPV, IRR, payback, Italian incentives |
| API | `api/` | FastAPI service used by the website (`/estimate`, `/estimate/casa`, ...) |

## Run it

Python 3.11. The prototype downloads weather data, so it needs an internet connection.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

streamlit run app_school.py       # the school prototype shown in the portfolio video
uvicorn api.main:app --reload     # the API, with interactive docs at http://localhost:8000/docs
pip install pytest && pytest -q   # the automated test suite
```

Optional: the prototype can write a short text summary of the results with Claude. To enable it, put `ANTHROPIC_API_KEY = "..."` in `.streamlit/secrets.toml` (ignored by git). Everything else works without it.

## Documentation

- `docs/METHODOLOGY.md`: the full technical methodology (in Italian)
- `docs/audit-matematico.md`: a line-by-line mathematical audit of the model, with every assumption labelled and every known limit listed (in Italian)

## Status and known limits

- **Complete:** the engine, the API, the school prototype. **First version:** the household estimator.
- The October 2026 audit found 24 simplifications and 11 places where code and documentation disagree; none is fixed yet. Two worth knowing: production runs on a standalone implementation of the published models (pvlib is not installed), and the weather data is PVGIS-SARAH2, not SARAH3 as some docstrings say.
- Comments and internal notes in the code are partly in Italian.

## How this was built

I chose the problem, worked out the physics and the economics, made the modelling decisions and judged the results. Most of the code was written by Claude Code, an AI coding assistant, from my specifications; I reviewed and tested it. `CLAUDE.md` is the project memory the assistant reads (in Italian), and it records many of the decisions as they were made.
