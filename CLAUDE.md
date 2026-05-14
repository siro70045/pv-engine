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
- ⚠ Zero test coprono `engine/envelope.py` (`_thermal_mass_parameter`, `calc_thermal_demand`, `calc_ground_heat_loss`, `calc_solar_gains` e funzioni correlate). Ogni modifica a envelope.py è formalmente cieca rispetto alla regressione numerica. Da risolvere prima di esercitare envelope.py su casi reali (preventivo per quando Meridiana inizierà a usare envelope per le scuole reali di Albinea).
- ⚠ Verifica esposizione del bug metabolico (commit `1f6854b`): il fix dimensionale ha cambiato la stima del consumo elettrico HP delle scuole nel flusso `/simulate/school` (+14 % sul totale annuo, +44 % sulla voce `heating_hp`). I materiali pubblici Meridiana (sito, presentazioni, PDF, magazine) NON dovrebbero citare numeri di consumo HP o payback HP derivati dall'API `/simulate/school` sulla versione pre-fix. Da verificare in revisione dei materiali pubblici prima del lancio Albinea (settembre 2026). NPV PV-scenario pressoché invariato (+0.3 %), payback invariato — quindi le raccomandazioni di investimento ("Excellent") non cambiano, solo le stime energetiche assolute.

- Critiche per la correttezza del motore
1. ✅ RISOLTA (commit `fee96c2`, 2026-05-14) — _REF_EFFICIENCY annotato come `ClassVar[float]` in `engine/energy_vectors.py:206`. Baseline Borzano identico.
2. ✅ RISOLTA (commit `f8d6799`, 2026-05-14) — rimossa la prima assegnazione duplicata di `co2_hp` in `engine/scenarios.py:689-691` (era sovrascritta dalla successiva). Baseline Borzano identico.
3. ✅ RISOLTA (commit `b1c561c`, 2026-05-14) — Massa termica ground floor: aggiunto campo `GroundFloor.surface_mass_kg_m2` (default 350.0 kg/m², mediana scuole italiane pre-1980 con soletta laterocemento; range 300-500 EN ISO 13790 "heavy"). `engine/envelope.py:588` ora legge il campo invece di usare il `200` hardcoded. Caratterizzazione precedente leggermente imprecisa: il dataclass `GroundFloor` non aveva il campo (non si "ignorava un valore configurato"), il bug era un'asimmetria col trattamento di walls/roof. Baseline Borzano identico — il flusso `compare_scenarios` non esercita `envelope.py`. Impatto reale solo per chi usa `main.py` legacy con `Envelope` esplicito.
4. Incoerenza _JAN1_DOW tra loads.py e school/calendar_it.py (loads.py:49 + calendar_it.py:26)
Uno usa reference year 2023 (DOW=6), l'altro 2025 (DOW=2). Se chi calcola una scuola usa un giorno della settimana riferito a 2023 e poi il calendario regionale a 2025, ottieni date sfalsate di qualche giorno. Su un anno intero la differenza è piccola, ma c'è.
Gravità: media. Da allineare.
5. Bolzano mancante nei REGION_ALIASES (calendar_it.py:147)
trentino_alto_adige → trento ignora Bolzano. Le scuole altoatesine usano il calendario di Trento perdendo la pausa autunnale lunga (30/10–08/11). Per Albinea (Reggio Emilia) non è un problema, ma se mai estendi a Bolzano è da fixare.
Gravità: bassa (per ora), media (in futuro).
Hack fragili (da pulire ma non urgente)
6. ✅ RISOLTA (commit `ac9ca49`, 2026-05-14) — aggiunto campo dedicato `InvestmentCost.heat_pump_cost_eur` (default 0.0) in `engine/economics.py`; tutte le 5 occorrenze di hack in `engine/scenarios.py` migrate al nuovo campo. `grid_connection_eur` torna al suo scopo originario. Baseline Borzano identico (PV-only e PV+HP).
7. Floor magico max(..., -99999) (scenarios.py:666)
Sta nascondendo un edge case invece di gestirlo. È un anti-pattern.
Gravità: bassa.
Sovrastime/sottostime nelle stime
8. AC kWh = area × W/m² × 60 h/anno (loads_school.py:644)
60 ore/anno di aria condizionata è davvero poco per scuole in zona D-E (Reggio, Bologna, Firenze). Probabile sottostima del consumo estivo.
Gravità: media per le scuole nelle zone climatiche più calde.
9. ✅ RISOLTA (commit `1f6854b`, 2026-05-15) — Apporti metabolici, errore dimensionale. Caratterizzazione originale (dossier): "asimmetria HP vs gas, gonfia il vantaggio HP". Caratterizzazione corretta (post-analisi): errore di unità dimensionale dentro `school/loads_school.py:calc_school_loads`. La riduzione veniva sottratta in kWh termici da un consumo in kWh elettrici, sovrastimando ~3× (fattore SCOP). Flussi affetti: solo `/simulate/school` API REST. Il flusso `compare_scenarios` (numeri pubblici sito Meridiana) non era affetto, baseline Borzano canonical invariato (PV 131 762.9, NPV solo_pv 86 763.56, NPV solo_hp 27 286.56, SCOP 3.52). Fix: SCOP estratto come variabile riusabile + conversione termico→elettrico via `/scop` prima della sottrazione. Per scuola Borzano-like: `heating_hp` 51 424→74 078 kWh (+44 %), `reduction_pct` 39.1 %→12.2 %, NPV PV scenario praticamente invariato (+0.3 %, anche se la stima del consumo elettrico totale +14 %). Nuovi test: `test_metabolic_reduction_dimensional_consistency_split` e `_central` in `tests/test_loads_school_dow.py`.
10. Asimmetria baseline/actual su TOU (energy_balance.py:695-712)
Stessa logica: il calcolo del risparmio non confronta scenari simmetrici. Sovrastima leggermente il risparmio nei casi con tariffa peak-pricing.
Gravità: media.
Approssimazioni dichiarate ma da migliorare
11. Formula cos_theta con hour angle fisso a 15° (envelope.py:444-450)
Sta calcolando la radiazione solare media usando un'approssimazione "media a noon ± 1 ora" invece dell'integrale corretto. Inoltre stima frazione diffusa come G_h × 0.55 rozzamente. Funziona ma è grezza.
Gravità: bassa (è già dichiarata come approssimazione nel codice).
12. Bounding box rettangolari sovrapposti per regioni italiane (calendar_it.py:155-240)
Misclassifica città di confine. È un'approssimazione dichiarata, ma comunque debolezza.
Gravità: bassa.
Magic numbers senza commento
13. 0.00116 kWh/(L·K), cycles_per_year = 250, fattore 0.03 design&permits
Sono valori corretti ma senza spiegazione. Refactoring di qualità.
Gravità: bassissima.


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
