## Cosa è

Backend Python di Meridiana, piattaforma italiana di analisi tecno-economica del fotovoltaico per scuole/case/aziende. Engine validato che calcola produzione FV, economics (NPV/IRR/LCOE/payback), incentivi italiani, scenari multi-vettore (PV + heat pump + gas). Esposto via API FastAPI.

## Stack

Python 3.11+, `pvlib`, FastAPI, Pydantic v2, `shapely>=2.0` (poligoni regionali ISTAT). Streamlit (`app.py` + `app_school.py`) come prototipo dev interno, **NON prodotto finale**. Deploy Railway su https://pv-engine-production.up.railway.app. Dati: PVGIS-SARAH3 TMY, ENEA, calendario 21 regioni italiane, zone climatiche A-F, NTC 2018, poligoni amministrativi ISTAT (`school/data/regioni_istat.geojson`, ~131 KB semplificati).

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
- ⚠ `has_ac` (campo dataclass `SchoolLoadsInput`) è ridondante rispetto a `ac_type != "none"`. Dopo il fix AC parametrico (commit `afd1849`) il campo è ancora presente per retrocompatibilità ma andrebbe deprecato in un giro di pulizia futuro: ogni stato `has_ac=True` con `ac_type="none"` resta semanticamente inconsistente. Non urgente.
- ⚠ Tipi AC non noti silenziati: `school/loads_school.py:660-665` cade su `w_per_m2 = 0.0` se `ac_type` non è in `{"split", "central"}`. Pydantic protegge l'API REST (Literal), ma chiamate dataclass-dirette (script Python o test) con `ac_type="vrf"` o `"multi"` produrrebbero `ac_kwh = 0` silenziosamente. Da risolvere se mai si aggiungeranno nuovi tipi AC o si vorrà coerenza dataclass/Pydantic.
- ⚠ `ore_zona_base` per AC (`ORE_AC_BASE_PER_ZONE`, valori 600/450/320/220/150/20 per zone A-F) usa la zona DPR 412/93 come proxy del Cooling Degree Days locale. Sviluppo futuro: integrare CDD calcolato ora per ora dal TMY, per riflettere microclimi e annate reali invece dei macro-bucket regolatori.
- ⚠ `engine/scenarios.py:compare_scenarios` ricalcola `el_grid_cost = grid_import_kWh × import_price_eur_kWh` a mano alle righe 546, 645, 661, 835 invece di leggere `EnergyBalanceResult.annual_saving_eur`. Conseguenza: anche se si attivasse `time_of_use=True` in `cfg.tariff`, il calcolo del saving in `compare_scenarios` resterebbe sempre flat, bypassando la logica TOU di `_aggregate`. Incoerenza architetturale non bloccante, da valutare in un giro di pulizia futuro (richiede capire perché esiste la duplicazione — probabilmente per imporre il flat anche in scenari sperimentali).
- ⚠ `peak_price_multiplier: float = 1.5` duplicato in `main.py:191` e `engine/energy_balance.py:171`. Origine probabile: vecchio dataclass `GridTariff` in `main.py` non rimosso dopo il refactor. Da valutare cancellazione del duplicato in `main.py` (anomalia separata da Voce #10, candidata a pulizia successiva).
- ⚠ `peak_hours` default fisso `list(range(8, 21))` in `GridTariff`. Coerente con le fasce F1 ARERA standard, ma non parametrizzabile per fasce stagionali o per tariffe industriali con fascia F2 separata. Da estendere se mai si modellerà tariffe multi-fascia (F1/F2/F3 ARERA piene).

**Numeri canonici Borzano (al 2026-05-17 dopo i 7 fix Categoria 1+2+3)**

- `compare_scenarios` (numeri pubblici sito Meridiana) — INVARIATI bit-perfect dopo tutti i fix (inclusa Voce #10 TOU del 2026-05-17): PV `131 762.9` kWh/anno, NPV `solo_pv` `€86 763.56`, NPV `solo_hp` `€27 286.56`, IRR `solo_hp` `40.5 %`, payback `solo_hp` `2.7` anni, SCOP `solo_hp` `3.52`, HP coverage `98.94 %`, LCOE `0.0729` €/kWh.
- `/simulate/school` Borzano-like (3000 m², 555 occupanti, primaria, zona E, anno 1981, HP, AC split, default `few_zones`+`rare`, PV 100 kWp tilt 15° azimuth 180°, capex €110 000, prezzo elettricità 0.25 €/kWh): `breakdown.ac` `135.0` kWh, `breakdown.heating_hp` `74 077.8` kWh, `total_annual_kwh` `179 768.3` kWh, NPV `€228 751.63`, IRR `17.23 %`, payback `6.03` anni, self_consumption_rate `0.392`, self_sufficiency_rate `0.271`. Invariati post-fix Voce #10 (TOU non attivato in nessun flusso produttivo).

- Critiche per la correttezza del motore — **Categoria 1 COMPLETATA al 100% (3 di 3) al 2026-05-15**: voci 1, 2, 3, 8, 9 ✅ risolte; sostantivamente le 3 anomalie di Categoria 1 originali (massa ground floor, apporti metabolici, AC parametrico) sono chiuse. **Categoria 3** (sovrastime/sottostime): voce #10 ✅ risolta al 2026-05-17. **Categoria 4** (approssimazioni dichiarate): voce #12 ✅ risolta al 2026-05-17, voce #11 ancora aperta. **Hack fragili**: voce #5 (Bolzano alias) ✅ risolta al 2026-05-17 insieme alla #12 per sinergia di scope.
1. ✅ RISOLTA (commit `fee96c2`, 2026-05-14) — _REF_EFFICIENCY annotato come `ClassVar[float]` in `engine/energy_vectors.py:206`. Baseline Borzano identico.
2. ✅ RISOLTA (commit `f8d6799`, 2026-05-14) — rimossa la prima assegnazione duplicata di `co2_hp` in `engine/scenarios.py:689-691` (era sovrascritta dalla successiva). Baseline Borzano identico.
3. ✅ RISOLTA (commit `b1c561c`, 2026-05-14) — Massa termica ground floor: aggiunto campo `GroundFloor.surface_mass_kg_m2` (default 350.0 kg/m², mediana scuole italiane pre-1980 con soletta laterocemento; range 300-500 EN ISO 13790 "heavy"). `engine/envelope.py:588` ora legge il campo invece di usare il `200` hardcoded. Caratterizzazione precedente leggermente imprecisa: il dataclass `GroundFloor` non aveva il campo (non si "ignorava un valore configurato"), il bug era un'asimmetria col trattamento di walls/roof. Baseline Borzano identico — il flusso `compare_scenarios` non esercita `envelope.py`. Impatto reale solo per chi usa `main.py` legacy con `Envelope` esplicito.
4. Incoerenza _JAN1_DOW tra loads.py e school/calendar_it.py (loads.py:49 + calendar_it.py:26)
Uno usa reference year 2023 (DOW=6), l'altro 2025 (DOW=2). Se chi calcola una scuola usa un giorno della settimana riferito a 2023 e poi il calendario regionale a 2025, ottieni date sfalsate di qualche giorno. Su un anno intero la differenza è piccola, ma c'è.
Gravità: media. Da allineare.
5. ✅ RISOLTA (2026-05-17, stesso PR di #12) — Alias `trentino_alto_adige → trento` rimosso da `REGION_ALIASES` (`school/calendar_it.py:147`). L'alias cancellava silenziosamente la pausa autunnale lunga di Bolzano (30/10–08/11). Post-fix passare la stringa generica `trentino_alto_adige` a `_build_regional_vacations` solleva `ValueError` con un messaggio che indica esplicitamente `trento`/`bolzano`. L'unico alias rimasto è `friuli_venezia_giulia → friuli_vg`, puramente linguistico (una regione, un calendario). Nessun chiamante nel repo passava la stringa rimossa (grep esaustivo). Per Borzano (Emilia-Romagna) nessun impatto. Test: `test_trentino_alto_adige_alias_removed`, `test_ambiguous_region_raises_valueerror`, `test_explicit_bolzano_works_with_autumn_break` in `tests/test_region_from_coordinates.py`.
Hack fragili (da pulire ma non urgente)
6. ✅ RISOLTA (commit `ac9ca49`, 2026-05-14) — aggiunto campo dedicato `InvestmentCost.heat_pump_cost_eur` (default 0.0) in `engine/economics.py`; tutte le 5 occorrenze di hack in `engine/scenarios.py` migrate al nuovo campo. `grid_connection_eur` torna al suo scopo originario. Baseline Borzano identico (PV-only e PV+HP).
7. Floor magico max(..., -99999) (scenarios.py:666)
Sta nascondendo un edge case invece di gestirlo. È un anti-pattern.
Gravità: bassa.
Sovrastime/sottostime nelle stime
8. ✅ RISOLTA (commit `afd1849`, 2026-05-15) — Modello AC parametrico (era: `area × W/m² × 60 h/anno` hardcoded, `loads_school.py:644`). Caratterizzazione corretta dopo analisi pre-fix: il `60` hardcoded sovrastimava per scuole italiane medie con AC limitato e sottostimava per scuole moderne intensamente climatizzate — non solo "sottostima zona D-E" come da dossier originale. Fix: nuovo modello a 3 parametri `coverage` (4 livelli, 0.0/0.15/0.50/0.90) × `intensity` (3 livelli, 0.25/0.55/0.90) × `ore_zona_base` (zona DPR 412/93, 600/450/320/220/150/20). Formula lineare `floor_area × coverage × W/m² × ore_zona × intensity / 1000`, no effetti quadratici. Default conservativo `few_zones` + `rare` per richieste API senza i nuovi campi. File modificati: `school/loads_school.py` (3 mappe + 2 campi dataclass + sezione AC riscritta), `api/schemas/loads.py` (2 Pydantic Literal), `api/routers/loads.py` (mapper), `app_school.py` (2 selectbox condizionati a `has_ac`). 5 nuovi test (`test_ac_*` in `tests/test_loads_school_dow.py`). Impatto Borzano-like `/simulate/school` (zona E, 3000 m², primaria, default conservativo): `breakdown.ac` 1440→135 kWh (-90.6 %), `total_annual_kwh` 181 073→179 768 kWh (-0.7 %), NPV 229 319.50→228 751.63 (-0.25 %), payback 6.02→6.03 anni. Impatto `compare_scenarios` Borzano (numeri pubblici sito Meridiana): **ZERO**, verificato bit-perfect via diff.
9. ✅ RISOLTA (commit `1f6854b`, 2026-05-15) — Apporti metabolici, errore dimensionale. Caratterizzazione originale (dossier): "asimmetria HP vs gas, gonfia il vantaggio HP". Caratterizzazione corretta (post-analisi): errore di unità dimensionale dentro `school/loads_school.py:calc_school_loads`. La riduzione veniva sottratta in kWh termici da un consumo in kWh elettrici, sovrastimando ~3× (fattore SCOP). Flussi affetti: solo `/simulate/school` API REST. Il flusso `compare_scenarios` (numeri pubblici sito Meridiana) non era affetto, baseline Borzano canonical invariato (PV 131 762.9, NPV solo_pv 86 763.56, NPV solo_hp 27 286.56, SCOP 3.52). Fix: SCOP estratto come variabile riusabile + conversione termico→elettrico via `/scop` prima della sottrazione. Per scuola Borzano-like: `heating_hp` 51 424→74 078 kWh (+44 %), `reduction_pct` 39.1 %→12.2 %, NPV PV scenario praticamente invariato (+0.3 %, anche se la stima del consumo elettrico totale +14 %). Nuovi test: `test_metabolic_reduction_dimensional_consistency_split` e `_central` in `tests/test_loads_school_dow.py`.
10. ✅ RISOLTA (commit `878be4d`, 2026-05-17) — Asimmetria baseline/actual su TOU in `engine/energy_balance.py:_aggregate`. Caratterizzazione originale (dossier): "sovrastima leggermente il risparmio in casi peak-pricing". Caratterizzazione corretta (post-analisi): il bug **SOTTOSTIMAVA** il risparmio (direzione opposta a quanto scritto nel dossier). Sotto `time_of_use=True`, il `baseline_cost` era calcolato sempre con la tariffa flat off-peak mentre l'`actual_cost` applicava la regola TOU peak/off-peak — l'asimmetria faceva apparire il risparmio più basso del vero e, in scenari con FV concentrato su ore peak, poteva produrre risparmi negativi. Fix: simmetrizzato il ramo baseline applicando lo stesso `_import_price_at(h)` orario usato per l'actual. Ramo flat (`time_of_use=False`) lasciato bit-identico al codice precedente — verificato via snapshot pre/post-fix. Flussi affetti: solo chiamanti diretti di `simulate_energy_balance` con `time_of_use=True`. **Zero chiamanti in produzione** usano TOU attivo (verificato con grep esaustivo) → `compare_scenarios` Borzano e `/simulate/school` invariati bit-perfect. Nuovi test (`tests/test_tou_symmetry.py`): `test_tou_off_no_change_from_flat`, `test_tou_symmetric_baseline_and_actual`, `test_tou_with_unit_multiplier_equals_flat`. Aggiornato anche docstring `GridTariff.import_price_eur_kWh` per esplicitare che è prezzo base/off-peak (non "average"). Categoria 3 ora 1 risolta su 3 (voci #11, #12 ancora aperte).
Approssimazioni dichiarate ma da migliorare
11. Formula cos_theta con hour angle fisso a 15° (envelope.py:444-450)
Sta calcolando la radiazione solare media usando un'approssimazione "media a noon ± 1 ora" invece dell'integrale corretto. Inoltre stima frazione diffusa come G_h × 0.55 rozzamente. Funziona ma è grezza.
Gravità: bassa (è già dichiarata come approssimazione nel codice).
12. ✅ RISOLTA (2026-05-17) — Bounding box rettangolari sovrapposti per regioni italiane in `region_from_coordinates` (`school/calendar_it.py:155-240` ante-fix). Caratterizzazione originale (dossier): "misclassifica città di confine, gravità bassa". Caratterizzazione corretta (post-analisi): bug **strutturale molto più grave del dichiarato** — error rate **22 %** su un test set di 94 città italiane (21 misclassificate), inclusi 3 capoluoghi regionali (L'Aquila → lazio, Firenze → emilia_romagna, Perugia → toscana) e l'intero cluster Toscana nord (Pisa, Lucca, Pistoia, Prato, Massa, Carrara, La Spezia) attribuito all'Emilia-Romagna perché il bbox E-R era testato prima e includeva quasi tutta la Toscana settentrionale. Fix: sostituzione della catena di 85 righe di `if/bbox` con classificazione poligonale su `school/data/regioni_istat.geojson` (poligoni amministrativi ufficiali ISTAT, semplificati a tolleranza ~2 km, ~131 KB, area preservata > 99.2 % per regione) via `shapely.geometry.Point.contains`. Per punti fuori da ogni poligono (mare, confine, estero) si usa la regione più vicina per distanza poligonale. Il Trentino-Alto Adige, regione unica nel GeoJSON ISTAT, viene splittato per latitudine (soglia 46.45°N) nelle chiavi `trento`/`bolzano`. Post-fix: 0 % errore sulla stessa lista di 94 città. Dipendenza nuova: `shapely>=2.0` aggiunta a `requirements.txt`. Performance: ~14 µs/lookup a caldo (cache module-level, prima chiamata ~25 ms per parsing GeoJSON). **Impatto produzione: ZERO** — `compare_scenarios` (numeri pubblici Borzano) non importa `region_from_coordinates`; `/simulate/school` riceve `region` dal payload utente con default `emilia_romagna` (corretto per Borzano già pre-fix). Borzano canonical numbers bit-perfect invariati. Test: 107 nuovi (`tests/test_region_from_coordinates.py`) → totale pytest 28 → 135 verdi. Voce #5 (alias Bolzano) chiusa nello stesso PR per sinergia.
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
