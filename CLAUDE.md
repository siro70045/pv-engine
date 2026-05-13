## Cosa è

Backend Python di Meridiana, piattaforma italiana di analisi tecno-economica del fotovoltaico per scuole/case/aziende. Engine validato che calcola produzione FV, economics (NPV/IRR/LCOE/payback), incentivi italiani, scenari multi-vettore (PV + heat pump + gas). Esposto via API FastAPI.

## Stack

Python 3.11+, `pvlib`, FastAPI, Pydantic v2. Streamlit (`app.py` + `app_school.py`) come prototipo dev interno, **NON prodotto finale**. Deploy Railway su https://pv-engine-production.up.railway.app. Dati: PVGIS-SARAH3 TMY, ENEA, calendario 21 regioni italiane, zone climatiche A-F, NTC 2018.

## Struttura

- `engine/` — `solar.py` (`BuildingFootprint`, `simulate_pv`, `simulate_pv_multi`), `pvgis_client.py`, `economics.py`, `energy_vectors.py`, `energy_balance.py`, `envelope.py`, `heat_pump.py`, `loads.py`, `scenarios.py`
- `school/` — `calendar_it.py` (calendari regionali 21 regioni), `climate_zones.py` (zone A-F), `incentives_school.py`, `loads_school.py` (include day-of-week modulation)
- `api/` — `main.py` FastAPI (25 endpoint), `routers/` split per dominio (building, economics, loads, location, pv, simulate), `schemas/` Pydantic per dominio, `utils/`
- `branding/` — `meridiana_theme.py` per restyling Streamlit
- `app.py`, `app_school.py` — Streamlit dev tools (NON produzione)
- `.streamlit/config.toml` — theme Streamlit base Meridiana
- `schemas.py` (root) — schemi Pydantic condivisi legacy
- `tests/` — 18 test pytest verdi (`test_loads_school_dow.py`, `test_multi_footprint.py`, `test_scenarios_level1.py`)

## Decisioni architetturali

- **Engine separato dall'API**: motore importabile come libreria, API è wrapper.
- **Retrocompatibilità bit-perfect**: ogni refactor deve dare risultati identici al millesimo sul caso test Borzano (44.601°N, 10.636°E, 100 kWp, 131.763 kWh/yr).
- **Test prima del refactor**.
- **Multi-edificio modulare** (Fase A V1.1 completata 3 mag 2026, commit `3f5ce7e`).

## Stato attuale (13 mag 2026)

- Motore validato, 18 test verdi
- API FastAPI deployata su Railway (25 endpoint)
- Production hardening: JSON logs, rate limiting, two-level TMY cache (`2704305`)
- Multi-footprint support nel motore (`3f5ce7e`)
- Restyling Streamlit Meridiana applicato (`1061604`)

## Problemi noti

- Discrepanza ~10% tra produzione Borzano 100 kWp simulata (131.763 kWh/anno motore standalone, no pvlib) vs valore dichiarato nei banner sito/PDF (~120.000 kWh/anno). Da risolvere prima del lancio settembre 2026 con pvlib in produzione Railway.
- Variabile Railway `ENVIRONMENT=production` non ancora impostata (cosmetico).

## Convenzioni codice

- Codice in **inglese** (variabili, funzioni, commenti, docstring); comunicazione utente in **italiano**.
- Type hints obbligatori per funzioni pubbliche.
- `black` + `isort` prima di commit.
- `pytest` per ogni modulo nuovo.

## Workflow standard

- Test: `pytest tests/`
- Streamlit residenziale: `streamlit run app.py`
- Streamlit scuole: `streamlit run app_school.py`
- API locale: `uvicorn api.main:app --reload`
- Deploy: `git push origin main` → Railway redeploy auto

## Storia commit principali

- `3f5ce7e` (3 mag 2026) feat(engine): multi-footprint PV simulation support
- `5b6be55` test: integrate scenarios stress-test into pytest suite
- `2704305` feat(api): production hardening — JSON logs, rate limiting, two-level TMY cache
- `e008bd4` feat(school): add day-of-week modulation for thermal loads
- `1061604` feat(ui): apply Meridiana brand theme to Streamlit apps

## Come usare questo file

Leggi all'inizio di ogni sessione di lavoro. Aggiorna quando aggiungi moduli, cambi convenzioni, prendi decisioni architetturali, risolvi problemi noti (rimuovi dalla lista), identifichi nuovi debiti tecnici.
