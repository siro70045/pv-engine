# Metodologia tecnica di Meridiana

> Documento di riferimento sul funzionamento interno del motore Meridiana. Pensato per ingegneri, consulenti, valutatori di fondazione, RUP di Comune e Provincia: chi legge deve poter risalire dalle formule al codice e contestare ogni assunzione.

Versione: maggio 2026 · Repo: `pv_engine` · Branch: `main`

---

## Premessa

Il fotovoltaico in Italia è un mercato dove la fiducia è scarsa. Le famiglie intenzionate a mettere i pannelli sul tetto ricevono preventivi diversi per lo stesso impianto, con gli incentivi che cambiano da un anno all'altro e poca chiarezza sull'effettivo valore dell'investimento che stanno valutando di fare. I decisori pubblici — sindaci, dirigenti di Provincia, RUP — devono valutare proposte tecniche complesse senza avere a disposizione gli strumenti indipendenti per verificare i numeri. Meridiana nasce dall'idea che la decisione di investire nel fotovoltaico debba poggiare su un'analisi tecnica seria, fatta da chi non ha interesse a venderti l'impianto. Questo documento spiega come Meridiana arriva ai suoi numeri, passo per passo, in modo che ogni lettore con competenze tecniche possa risalire dalla raccomandazione finale alle formule, e dalle formule al codice.

Meridiana è uno strumento di analisi, non un installatore. Non prende commissioni dagli installatori, non vende lead, non ha incentivi commerciali a dire "sì, conviene". Il suo lavoro è produrre un report tecno-economico onesto: a volte la risposta è che il fotovoltaico conviene molto, altre volte meno, perfino che è meglio aspettare. Il motore di calcolo è una libreria Python (engine/) esposta tramite un'API REST in produzione su infrastruttura cloud. Le simulazioni sono riproducibili: a parità di input, l'output è identico al millesimo.

Il documento si rivolge a chi deve valutare un'analisi di Meridiana o capirne il funzionamento interno: ingegneri, consulenti energetici, RUP di Comune e Provincia, valutatori di fondazione. Non è un manuale di marketing né un'illustrazione divulgativa: è il riferimento di metodo del motore. Ogni assunzione è dichiarata, ogni limitazione è riconosciuta, ogni numero ha una fonte tracciabile in norme tecniche (IEC, EN ISO, UNI), in letteratura scientifica peer-reviewed o in dati pubblici (PVGIS, ENTSO-E, ARERA). Se chi legge trova un errore o un'incoerenza, può segnalarlo con riferimento puntuale al file e alla riga di codice.

Il documento è strutturato in nove sezioni che partono dai dati climatici grezzi alle limitazioni note del modello, più una sezione finale di riferimenti normativi e bibliografici. Le sezioni si possono leggere in ordine — è un percorso logico che ricostruisce la catena dalle ore di sole alla raccomandazione di investimento — oppure consultare singolarmente, perché ognuna è autonoma. Dove ci sono ambiguità o decisioni progettuali non ovvie, le segnaliamo: l'obiettivo non è convincere che il modello è giusto, ma chiarire cosa fa e dove può sbagliare.


---

## 1. Dati climatici di input

Per dire se il fotovoltaico ha senso su un edificio bisogna prima sapere quanto sole quell'edificio riceve in un anno tipico — non un anno particolarmente fortunato, non uno particolarmente nuvoloso, ma una sintesi statistica basata su un ampio intervallo di tempo della radiazione solare in quel punto preciso della mappa. Per ricostruire l'andamento orario dell'irraggiamento, della temperatura e del vento abbiamo bisogno di dati climatici di qualità, possibilmente certificati e pubblici. Meridiana parte da qui — dai dati grezzi del Joint Research Centre della Commissione Europea — e li elabora con un percorso completamente tracciabile.

Le ore di un anno sono 8 760; ognuna di esse è accompagnata da un set di variabili: irraggiamento globale, diretto, diffuso, temperatura ambiente, vento. Senza questi dati nessun modello fisico di pannello fotovoltaico può funzionare. La scelta dei dati di partenza è la prima decisione critica del modello: tutto quello che segue è una conseguenza.

**Fonte primaria.** Tutti i calcoli partono dai dati del [PVGIS](https://re.jrc.ec.europa.eu/pvg_tools/en/) (Photovoltaic Geographical Information System) sviluppato dal Joint Research Centre della Commissione Europea. Endpoint usato: `https://re.jrc.ec.europa.eu/api/v5_2/tmy` (`engine/pvgis_client.py:47`). PVGIS aggrega dati satellitari SARAH-3 ed ERA5 e produce un **Typical Meteorological Year (TMY)**: un anno orario sintetico costruito scegliendo, per ciascun mese, l'anno più rappresentativo della serie storica 2005–2020.

**Cosa scarichiamo.** Per ogni coppia (latitudine, longitudine) PVGIS restituisce 8 760 record orari con i seguenti campi (`engine/pvgis_client.py:62-77`):

| Campo | Significato | Unità |
|---|---|---|
| `T2m` | Temperatura ambiente a 2 m | °C |
| `RH` | Umidità relativa | % |
| `G_h` | Irraggiamento globale orizzontale (GHI) | W/m² |
| `Gb_n` | Irraggiamento diretto normale (DNI) | W/m² |
| `Gd_h` | Irraggiamento diffuso orizzontale (DHI) | W/m² |
| `IR_h` | Irraggiamento infrarosso verso il basso | W/m² |
| `WS10m` | Velocità vento a 10 m | m/s |
| `WD10m` | Direzione vento | ° |
| `SP` | Pressione superficiale | Pa |

PVGIS restituisce inoltre l'**orizzonte locale**: 36 valori di altezza apparente dell'orizzonte ogni 10° di azimuth (passo di campionamento parametrico in `engine/solar.py:548`). Lo usiamo per calcolare lo **shading topografico**: ore in cui il sole sta sotto il profilo dell'orizzonte vengono escluse dalla produzione (`engine/solar.py:532-555`).

**Cache.** La risposta JSON grezza è scritta su disco in `.pvgis_cache/` (`engine/pvgis_client.py:54`) con chiave deterministica derivata dal MD5 delle coordinate arrotondate a 4 decimali (`engine/pvgis_client.py:193-196`). Le chiamate successive sulla stessa località saltano l'API. Una seconda cache di livello superiore (TMY parsificato) vive nel layer API (`api/main.py` — vedi commit `2704305`).

**Robustezza di rete.** `_fetch_raw` (`engine/pvgis_client.py:233`) implementa 3 retry con backoff fisso di 5 s; timeout per chiamata 30 s. Errori HTTP non sono riprovati (sintomi di parametri errati lato client), errori di rete sì.

**Limiti tecnici del dato.**

- **Risoluzione spaziale.** PVGIS produce TMY ricampionando griglia satellitare nominalmente ~0.1° (~11 km) sulle componenti SARAH-3. Microclimi inferiori a questa scala (foschia di valle ricorrente, isole urbane di calore, nebbie persistenti) **non sono catturati**.
- **TMY ≠ anno reale.** Il TMY è statistica, non meteo. Un singolo anno futuro sarà più scuro o più chiaro. Per studi di sensitività vera al rischio meteorologico servirebbe simulazione su 15+ anni reali — non implementata.
- **Orizzonte topografico, non costruito.** L'orizzonte di PVGIS è il rilievo geografico, non gli edifici circostanti. Lo shading da palazzi vicini, alberi alti, ciminiere **non è modellato** automaticamente: va inserito manualmente come `SystemLosses.shading_near` (default 3% sull'anno, `engine/solar.py:187`).
- **Formato timestamp.** PVGIS ha cambiato formato timestamp intorno al 2024; il parser supporta entrambi (`engine/pvgis_client.py:323-337`). Anno di riferimento del TMY è virtuale (es. `0001-MM-DD HH:MM` o `YYYYMMDD:HHMM`).

---

## 2. Modello solare

Avere i dati climatici è soltanto il primo passo. Il pannello fotovoltaico non vede l'irraggiamento orizzontale: lo riceve sul suo piano inclinato, in un certo orientamento, mentre il sole varia la sua posizione ora dopo ora nel cielo. Le 8 760 ore di dati grezzi devono essere trasformate in 8 760 valori di potenza elettrica al contatore, attraverso una catena fisica che include la posizione del sole, la decomposizione dell'irraggiamento sul piano dei moduli, l'angolo di incidenza, la temperatura delle celle, le perdite del cablaggio e dell'inverter. Ogni passaggio di questa catena è modellato secondo standard internazionali (IEC 61724-1, IEC 61853-1) e letteratura peer-reviewed.


Il modulo `engine/solar.py` (1 416 righe) trasforma le 8 760 ore TMY in 8 760 valori orari di potenza AC al contatore. Esistono **due percorsi di simulazione** intercambiabili:

1. `_simulate_with_pvlib` (`engine/solar.py:667`) — usa la libreria [pvlib-python](https://pvlib-python.readthedocs.io/) (default se installata).
2. `_simulate_standalone` (`engine/solar.py:797`) — riproduce ogni passaggio con sola libreria standard + `math`, scelto per portabilità (deploy minimo, niente dipendenze opzionali).

I due percorsi sono **bit-near-identical** (≤ 2–3 % di scarto annuale): lo standalone è scritto per essere una replica leggibile di pvlib, non un'alternativa concettualmente diversa.

### 2.1 Posizione del sole

Algoritmo Spencer (1971) per declinazione (`engine/solar.py:305`) e equazione del tempo (`engine/solar.py:313`); calcolo zenith/azimuth dalla declinazione, latitudine e angolo orario in `engine/solar.py:325-374`. Il percorso pvlib usa `pvlib.solarposition.get_solarposition` (NREL SPA equivalente, `engine/solar.py:694`). Per Italia entrambi danno errore < 0.1° su tutto l'anno.

### 2.2 Decomposizione GHI → POA (Plane of Array)

L'irraggiamento sul piano inclinato dei pannelli si ottiene con il **modello anisotropo di Perez 1990** (Solar Energy 44(5), 271–289). L'implementazione standalone (`engine/solar.py:407-460`) usa le 8 classi di "sky clearness" ε e i coefficienti F1/F2 tabulati (`engine/solar.py:385-395`) per pesare le componenti circumsolare e di brillanza dell'orizzonte. Il percorso pvlib chiama `pvlib.irradiance.get_total_irradiance(..., model="perez")` con stesso albedo.

**Albedo del suolo:** `0.20` hardcoded (`engine/solar.py:457`, e `albedo=0.20` in pvlib `engine/solar.py:714`). Valore generico per superficie naturale media; non distingue tetto in tegole, calcestruzzo, ghiaia chiara, neve. [VERIFICA] se servirà parametrizzarlo per progetti in alta quota.

### 2.3 Angolo di incidenza e IAM

L'**Angolo di Incidenza (AOI)** del raggio diretto sul piano del modulo è calcolato analiticamente (`engine/solar.py:463-483`). L'**Incidence Angle Modifier (IAM)** — perdita per riflessione del vetro a incidenze oblique — segue il modello ASHRAE / Souka–Safwat 1966:

> **IAM(θ) = 1 − b₀ · (1/cos θ − 1)**

con `b₀ = 0.05` default (vetro standard, `engine/solar.py:498-526`). Vetro antiriflesso usa `b₀ ≈ 0.02`. L'IAM è applicato solo al raggio diretto; per le componenti diffuse cielo e suolo si usano fattori integrali fissi:

- `_IAM_DIFFUSE_SKY = 0.950` (`engine/solar.py:494`)
- `_IAM_DIFFUSE_GND = 0.970` (`engine/solar.py:495`)

Riferimento: Martin & Ruiz 2001 Table 1, IEC 61853-2:2016.

### 2.4 Temperatura cella — modello Faiman 2008

La temperatura operativa delle celle è il driver principale della perdita estiva. Usiamo il modello **Faiman (2008)**, IEC 61853-1 compliant (`engine/solar.py:574-589`):

> **T_cell = T_amb + G_POA / (U₀ + U₁ · v_vento)**

I coefficienti U₀, U₁ dipendono dal montaggio (`FAIMAN_COEFFICIENTS`, `engine/solar.py:566-571`):

| Mounting | U₀ [W/m²K] | U₁ [W·s/m³K] |
|---|---:|---:|
| `open_rack` (a terra/free-standing) | 25.0 | 6.84 |
| `close_roof` (filo tetto, scarsa ventilazione) | 29.0 | 0.00 |
| `integrated_roof` (BIPV) | 15.0 | 0.00 |
| `flat_roof_ballast` (tetto piano zavorrato) | 20.0 | 0.00 |

Quando U₁=0 il vento non aiuta a raffreddare — assunzione conservativa per montaggi aderenti.

### 2.5 Potenza DC

Modello PVWatts-style con correzione termica (`engine/solar.py:596-617`):

> **P_dc = P_STC · (G_POA / 1000) · [1 + γ · (T_cell − 25)]**

con `γ` = coefficiente di temperatura della potenza, default `-0.40 %/K` (monocristallino comune, `engine/solar.py:158`). La correzione termica è clippata a zero: la potenza non può diventare negativa per temperature estreme.

### 2.6 Inverter, clipping, perdite AC

Il modello inverter (`engine/solar.py:624-660`) è una semplificazione di EN 50530:2010:

- Sotto il 5 % del carico nominale: efficienza = `η_nominal × 0.60` (regione di standby)
- Sopra: **η = η_nom · (1 − 0.03 · (1 − load_ratio))**, clippata a `η_nom`
- **Clipping di saturazione:** la potenza AC non può superare `P_inv_rated × η_nominal`. Il sovradimensionamento DC (DC/AC ratio, default 1.15, `engine/solar.py:161`) implica che nelle ore di picco si rinuncia volontariamente a una piccola frazione di DC per dimensionare l'inverter su una media.

Perdite di **cablaggio AC** (`SystemLosses.wiring_ac`, default 1.0 %) applicate moltiplicativamente in coda.

### 2.7 Perdite di sistema disaggregate

`SystemLosses` (`engine/solar.py:178-232`) modella otto perdite separate, espresse come frazioni annuali. Tutte sono **defaults raccomandati** sovrascrivibili in input:

| Voce | Default | Note |
|---|---:|---|
| `soiling` | 2.0 % | Sporco, polvere, fluttuazioni stagionali |
| `shading_near` | 3.0 % | Ostacoli ravvicinati non in orizzonte PVGIS |
| `mismatch` | 2.0 % | Variabilità modulo-modulo nella stringa |
| `wiring_dc` | 1.5 % | Resistenza cavi DC |
| `wiring_ac` | 1.0 % | Resistenza cavi AC |
| `connections` | 0.5 % | Contatti, connettori |
| `lid_degradation` | 1.5 % | Light Induced Degradation primo anno |
| `availability` | 1.0 % | Fermi pianificati e guasti |

Il fattore di perdita DC totale è il **prodotto** di `(1 − ciascuna_perdita)` (`engine/solar.py:200-213`), non la somma — questo evita di doppia-contare interazioni e mantiene il fattore ≤ 1.

### 2.8 Cascata delle perdite (waterfall)

Per ogni ora simulata, `_aggregate` (`engine/solar.py:895-1029`) decompone la potenza in **8 voci di perdita** che sommate al netto AC ritornano il DC lordo teorico:

```
gross_dc
 − horizon_shading_loss   (sole sotto profilo orizzonte)
 − iam_loss               (riflessione vetro a angoli obliqui)
 − temperature_loss       (T_cell > 25°C)
 − system_losses          (soiling + mismatch + wiring_dc + LID + availability)
 − inverter_loss          (conversione DC→AC)
 − clipping_loss          (saturazione inverter)
 − ac_wiring_loss         (cavi AC dopo inverter)
 = production_annual_kWh  (energia AC netta al contatore)
```

Il check `balance_ok = abs(somma − gross) < gross × 0.01` (`engine/solar.py:1407`) verifica la chiusura entro l'1 %.

### 2.9 Indicatori di output

Per ogni simulazione (`SolarResult`, `engine/solar.py:254-291`):

- `production_annual_kWh`: energia AC iniettata [kWh/anno]
- `specific_yield_kWh_kWp`: produzione specifica [kWh/kWp/anno] — confrontabile cross-sistema
- `performance_ratio` = E_AC / (H_POA × P_peak) — confronto a parità di sito
- `capacity_factor` = E_AC / (P_peak × 8760) — confronto con altre fonti
- Profili mensili (12 valori): produzione, POA, T_cell media nelle ore con sole

### 2.10 Proiezione pluriennale

`production_profile_by_year` (`engine/solar.py:1036-1074`) applica **degradazione lineare** a partire dall'anno 2:

> **P_anno_n = P_anno_1 · max(1 − r_deg · n, 0)**

con `r_deg = degradation_rate_pct_per_yr / 100` (default 0.50 %/anno, `engine/solar.py:163`). L'LID del primo anno è già contato in `SystemLosses.lid_degradation` (1.5 %) — non viene applicato due volte.

**Standard di riferimento:** IEC 61724-1:2021 (performance monitoring), IEC 61853-1:2011 (rating moduli), EN 50530:2010 (inverter).

---

## 3. Multi-edificio

Gli edifici non sono semplificabili in un singolo tetto piatto e orientato a sud. Una scuola occupa più corpi di fabbrica con falde diverse; una sede aziendale combina un capannone industriale a copertura sheds e una palazzina uffici a tetto piano; persino una casa monofamiliare ha quasi sempre due falde con orientamenti diversi. Modellare il fotovoltaico assumendo "un unico tetto medio" è un'approssimazione che falsa la stima di produzione e di autoconsumo, specialmente nelle ore di mattino e sera quando l'orientamento conta. Meridiana scompone ogni edificio in più footprint indipendenti — ciascuno con la propria area, azimuth, inclinazione e tipologia di montaggio — e ne aggrega la produzione oraria. Per un singolo footprint il comportamento è bit-perfect rispetto alla versione single-building precedente.

Un caso reale (es. complesso scolastico con palestra separata) ha più tetti con orientamento e inclinazione diversi. `simulate_pv_multi` (`engine/solar.py:1142-1342`) lo gestisce nativamente.

**Input:** una lista di `BuildingFootprint` (`engine/solar.py:51-92`), ognuno con `area_m2`, `azimuth_deg`, `tilt_deg`, `usable_fraction` (default 0.70 — esclude camini, accessi, setback perimetrali). La potenza di picco è derivata come `area · usable · 0.20 kWp/m²` (densità tipica di 200 W/m², modulo da 400 Wp ≈ 2 m²). Il campo `polygon_coords` è opzionale per uso futuro (visualizzazione frontend, shading 3D) — il motore non lo usa.

**Algoritmo:** per ogni footprint si clona il `system_template` sovrascrivendo `peak_power_kWp`, `tilt_deg`, `azimuth_deg` dal footprint (`engine/solar.py:1219-1224`); il resto (efficienza, perdite, DC/AC ratio, IAM, degradazione) è ereditato. Si chiama `simulate_pv` per ogni footprint indipendentemente e si aggrega.

**Regole di aggregazione (`engine/solar.py:1163-1192`):**

- Energie (kWh) → **somma** tra footprint
- Irraggiamenti POA e temperatura cella → **media pesata sull'area** (sono intensità, non energie)
- Boolean `horizon_shaded` → OR
- Specific yield = produzione totale / kWp totale
- Performance ratio, capacity factor: ricalcolati sull'aggregato

**Retrocompatibilità.** Un solo footprint dà output **identico** al `simulate_pv` single-building entro errore di arrotondamento (≤ 0.01 % sulla produzione annuale). Garantito da `tests/test_multi_footprint.py` (verificato nei test pytest verdi).

**Limite noto.** I footprint condividono il **medesimo TMY** (stessa orizzonte, stesso vento). Lo **shading reciproco** tra edifici sullo stesso sito **non è modellato**: non gestiamo il caso "la palestra getta ombra sul tetto scuola alle 17 di novembre".

---

## 4. Modello termico (pompa di calore)

Il fotovoltaico produce elettricità; la pompa di calore la consuma per produrre calore. La domanda strategica è se accoppiare le due tecnologie ha vantaggio economico in un edificio italiano, dove la maggior parte del fabbisogno termico è oggi coperta da caldaie a gas. Per rispondere bisogna stimare quanta elettricità servirà alla pompa di calore ora per ora, sapendo che il suo rendimento (COP) varia con la temperatura della sorgente: più fa freddo fuori, meno è efficiente. Meridiana usa un modello di COP basato sulla relazione di Carnot ridotta da un coefficiente empirico calibrato sui dati di mercato. Il calcolo è orario perché solo in questo modo è possibile confrontare onestamente la curva di produzione fotovoltaica con quella di consumo termico.


`engine/heat_pump.py` (596 righe) simula la pompa di calore in modalità riscaldamento. Il modulo è il cuore del confronto economico **gas vs. PV+HP**.

### 4.1 COP orario

Modello **Carnot con correzione empirica** (`engine/heat_pump.py:68-151`):

> **COP(h) = η · T_hot_K / (T_hot_K − T_source_K(h))**

dove:

- `T_hot_K` = temperatura di mandata del terminale [K] — dipende dal tipo di terminale idronico (`TERMINAL_SUPPLY_TEMP_C`, `engine/energy_vectors.py:60-69`): pavimento radiante 35–40 °C, fan-coil 40–45 °C, radiatori vecchi 70–80 °C
- `T_source_K(h)` = temperatura sorgente esterna oraria da TMY [K]
- `η` = fattore di efficienza Carnot calibrato su dati EN 14825 per classi reali (`engine/heat_pump.py:101-105`): A++ aria-acqua → `0.42`; A+++ aria-acqua → `0.46`; geotermico → `0.55`

Il modello riproduce i dati seasonal COP (SCOP) dichiarati dai costruttori entro **±10 %**. È la stessa approccio usato da PHPP, TABULA e EN 15316-4-2 (`engine/heat_pump.py:18`).

**Correzioni applicate ora per ora:**

1. **Defrost penalty.** Quando `T_ext < 5 °C` (`_DEFROST_THRESHOLD_C`, `engine/heat_pump.py:59`), il COP è ridotto del `defrost_loss_factor` configurabile. Modella i cicli di sbrinamento dell'evaporatore.
2. **Bound di Carnot.** COP non può superare il Carnot ideale T_hot_K / ΔT (`engine/heat_pump.py:142-144`).
3. **Spegnimento sotto minimo.** Se `T_ext ≤ T_min_operating_temp_c` → `COP = 0`: la PdC si spegne, dispatch su backup.
4. **Degrado annuale.** `deg_factor = max(1 − r_deg · (anno−1), 0.5)` (`engine/heat_pump.py:121`): degradazione lineare con pavimento al 50 %.

### 4.2 Domanda termica oraria

Approccio **quasi-stazionario** EN ISO 13790 (`engine/heat_pump.py:158-248`). Dato il fabbisogno annuale di riscaldamento `annual_heating_kWh`, si inverte:

> **H_tr [W/K] = annual_heating_kWh · 1000 / Σ_h max(T_set − T_ext(h), 0)**

`H_tr` è il coefficiente di scambio termico globale dell'edificio. Da lì, la domanda oraria:

> **Q_heat(h) = max(H_tr · (T_set_eff − T_ext(h)), 0)** [W]

con `T_set` setpoint indoor (default 20 °C, `engine/heat_pump.py:60`) e **setback notturno** opzionale modellato come riduzione del setpoint proporzionale a un fattore di occupazione orario (`engine/heat_pump.py:238`): `T_set_eff = T_set − (1 − occ) · 4 °C` (a `occ = 0` setback di 4 °C, cioè 16 °C effettivi).

Il profilo è poi **normalizzato** in modo che la somma annuale combaci esattamente con `annual_heating_kWh` (`engine/heat_pump.py:243-246`) — elimina il drift di virgola mobile.

### 4.3 Dispatch bivalente

Quando la pompa non può coprire da sola tutto l'inverno (Italia centro-nord), si abbina una **caldaia di backup** (`BivalentConfig`). Il dispatch orario classifica ogni ora in 4 zone (`engine/heat_pump.py:436-485`):

- **`hp_only`**: T_ext > T_bivalence → PdC sufficiente
- **`parallel`**: T_min ≤ T_ext ≤ T_bivalence → PdC al massimo + caldaia copre il delta
- **`backup_only`**: T_ext < T_min → solo caldaia
- **`idle`**: domanda termica = 0

**Sizing.** Se la potenza nominale della PdC non è data, viene stimata in `simulate_heat_pump_year` (`engine/heat_pump.py:367-385`):

- Monovalente: 95° percentile della domanda oraria
- Bivalente: domanda media nelle ore con `T_ext ≈ T_bivalence ± 2 °C`

`suggest_hp_sizing` (`engine/heat_pump.py:559-596`) restituisce entrambe le opzioni e la copertura % della PdC al sizing bivalente.

### 4.4 Output

`HeatPumpYearResult` (`engine/heat_pump.py:268-313`) include: domanda termica annuale, kWh termici da PdC, kWh elettrici consumati dalla PdC, kWh termici dal backup, fuel del backup in unità native (Sm³ gas o litri diesel), **SCOP** (Seasonal COP = E_termica / E_elettrica), copertura % della PdC, conteggio ore per zona, costi €, emissioni CO₂ kg, profili mensili.

### 4.5 Limiti dichiarati

- **Niente part-load efficiency.** La PdC è modellata come dispositivo che eroga `min(P_rated, Q_demand)`: nessun derating quando il carico parziale spinge la macchina lontano dal punto ottimale. In pratica sovrastima leggermente il SCOP su edifici sovradimensionati.
- **Niente isteresi né dinamica termica.** Risposta istantanea, accumulo termico dell'edificio ignorato. Per stime di SCOP annuale l'effetto è marginale.
- **Defrost generico.** Il `defrost_loss_factor` è una frazione media; non distingue tra cicli reali (più frequenti vicino a 0 °C che a −5 °C).

---

## 5. Bilanci energetici combinati

Aver calcolato la produzione fotovoltaica oraria e i consumi orari non è ancora sufficiente: bisogna decidere, ora per ora, che fine fa ogni kilowattora prodotto. Va prima nei consumi interni dell'edificio? Resta in batteria per essere usato la sera? Viene immesso in rete? Le scelte di priorità — tipicamente autoconsumo poi batteria poi export — determinano il valore economico effettivo dell'impianto, perché l'energia autoconsumata vale il prezzo dell'energia che non hai pagato, mentre quella esportata vale solo la tariffa che il gestore di rete ti riconosce. Il bilancio orario è il cuore della stima di convenienza: senza di esso, le valutazioni economiche del fotovoltaico restano inevitabilmente approssimative.

`engine/energy_vectors.py` (482 righe) definisce i **vettori energetici** e i prezzi; `engine/energy_balance.py` (1 213 righe) li combina ora per ora con la produzione PV.

**Vettori modellati** (`engine/energy_vectors.py:35-43`): `natural_gas`, `electricity`, `diesel`, `lpg`, `district_heat`, `wood_pellets`, `none`.

**Prezzi default 2024 Italia** (`engine/energy_vectors.py:126-157`):

| Vettore | Prezzo | Unità | Escalation | CO₂ |
|---|---:|---|---:|---:|
| Gas naturale | 0.95 | €/Sm³ | 3.0 %/anno | 1.96 kg/Sm³ |
| Elettricità | 0.25 | €/kWh | 2.5 %/anno | 0.233 kg/kWh |
| Diesel (gasolio risc.) | 1.45 | €/L | 2.5 %/anno | 2.64 kg/L |
| GPL | 0.85 | €/kg | 2.5 %/anno | 1.63 kg/kg |
| Teleriscaldamento | 0.09 | €/kWh_th | 2.0 %/anno | 0.070 kg/kWh_th |

I fattori di emissione elettricità (`0.233 kg/kWh`) sono ENTSO-E mix rete IT 2023; gas naturale `0.202 kg/kWh_th` combustione (LHV) o `0.252` includendo upstream ISPRA 2023 (`engine/energy_vectors.py:14-22`).

**Dispatch orario PV → utenze.** L'algoritmo `_simulate_hourly` (`engine/energy_balance.py:482`) per ogni ora:

1. Calcola produzione PV (estratta da `SolarResult.hourly` via `extract_pv_profile`, `engine/energy_balance.py:824`) e domanda elettrica complessiva.
2. **Autoconsumo diretto**: il PV copre fino a `min(P_PV, P_load)`.
3. **Accumulo batteria** (se presente): carica con surplus residuo, scarica quando carico > produzione (`_battery_step`, `engine/energy_balance.py:337`).
4. **Export rete**: solo dopo aver saturato autoconsumo e batteria, con cap configurabile `export_limit_pct` sulla potenza di picco PV (`engine/energy_balance.py:517`).
5. **Import rete**: per coprire deficit residuo.

Il bilancio è chiuso per ciascuna ora; nessuna energia "scompare" senza essere conteggiata in import, export o perdite di batteria.

### 5.1 Modello batteria

`BatterySystem` (`engine/energy_balance.py:52`) con default ragionevoli per LFP AC-coupled:

| Parametro | Default | Significato |
|---|---:|---|
| `efficiency_roundtrip` | 0.93 | Efficienza round-trip; `η_charge = η_discharge = √η_roundtrip` |
| `depth_of_discharge` | 0.90 | Profondità massima |
| `min_soc` | 0.05 | SOC minimo (protezione celle) |
| `initial_soc` | 0.50 | SOC iniziale simulazione |
| `calendar_deg_pct_per_yr` | 2.0 % | Degradazione calendariale lineare |
| `cycle_deg_pct_per_cycle` | 0.003 % | Degradazione per ciclo equivalente |

**Capacità invecchiata** all'anno *t* con *n* cicli: `C(t) = C_nom · max(1 − f_cal · t, 0) · max(1 − f_cyc · n_cycles, 0)` (`engine/energy_balance.py:411-441`). Il modello assume `cycles_per_year = 250` (`engine/energy_balance.py:414`); contiamo come ciclo solo l'energia di carica cumulativa. Cicli totali = energia caricata totale / capacità invecchiata.

**Anni di sostituzione batteria** sono identificati da `find_battery_replacement_years` (`engine/energy_balance.py:867`) come anni in cui la capacità invecchiata scende sotto `threshold = 0.80` di quella nominale. Ogni sostituzione resetta la degradazione.

Riferimenti normativi nei docstring: IEC 62619:2022 (sicurezza batterie), IEC 61724-1:2021 (performance), EN 50549:2019 (connessione rete), modello batteria ispirato a PVSYST.

### 5.2 Tariffe elettriche e net metering

`GridTariff` (`engine/energy_balance.py:130`) modella tariffa import/export con opzioni:

- **Prezzo base / off-peak.** `import_price_eur_kWh` è il prezzo base. Con `time_of_use=False` è la tariffa flat applicata a ogni kWh importato. Con `time_of_use=True` è il prezzo off-peak; il prezzo peak nelle `peak_hours` è `import_price_eur_kWh × peak_price_multiplier`.
- **TOU (Time-of-Use)** semplificato: ore peak configurabili (default 8:00–20:59) con moltiplicatore `peak_price_multiplier` (default 1.5×) sul prezzo base.
- **Net metering** (Scambio sul Posto): credito = `min(export, cap) × import_price`; sopra il cap, eccesso pagato a `export_price` (`engine/energy_balance.py:720-725`). Lo SSP è chiuso per nuovi impianti dal 2024 ma resta nel motore per stime su impianti esistenti.
- **Demand charge saving**: per utenze BT/MT con tariffa basata sulla potenza di picco mensile, il modello calcola il risparmio in € moltiplicando il delta-peak (con vs senza batteria) per `demand_charge_eur_kW_month` (`engine/energy_balance.py:765-770`).

**Simmetria baseline ↔ actual.** Il calcolo del risparmio economico applica la stessa regola tariffaria (flat o TOU) sia al baseline (`grid_cost_without_pv_eur`) sia allo scenario con FV (`grid_cost_with_pv_eur`). Sotto tariffa TOU le ore peak sono valutate a `import_price × peak_multiplier` e le ore off-peak al prezzo base, e lo stesso prezzo orario è usato per i due lati. Garantire questa simmetria è necessario perché il FV tende a coprire preferenzialmente le ore peak: usare TOU solo sull'actual e flat sul baseline produrrebbe un risparmio apparente sistematicamente *sottostimato* (e, in scenari con FV concentrata su ore peak, anche negativo). Il fix è documentato come anomalia ✅ Voce #10 (Categoria 3) nel `CLAUDE.md`.

### 5.3 Lifetime e degradazioni

`simulate_lifetime` (`engine/energy_balance.py:962`) produce 25 anni di `EnergyBalanceResult` applicando:

- **Degradazione PV** anno per anno (vedi §2.10), default 0.50 %/anno
- **Degradazione COP PdC** (vedi §4.1), pavimento 0.5×
- **Escalation prezzi** composta sui vettori coinvolti
- **Aging batteria** calendariale + ciclico

**Output annuale** in `EnergyBalanceResult` (`engine/energy_balance.py:262`): `pv_production_kWh`, `self_consumed_kWh`, `grid_export_kWh`, `grid_import_kWh`, **self-consumption rate** (% di PV autoconsumato), **self-sufficiency rate** (% di domanda coperta da PV), risparmio €, ricavo export €, eventuale demand-charge saving.

### 5.4 Ottimizzazione di taglia

`optimise_system_size` (`engine/energy_balance.py:1007`) supporta ricerca di taglia ottimale di PV e batteria (sweep multi-dimensionale). Esposta per uso in scripting, non parte della pipeline API di default.

---

## 6. Modello economico

I kilowattora prodotti, autoconsumati ed esportati hanno senso solo se tradotti in numeri economici interpretabili dal decisore. La domanda non è "quanti kWh fa l'impianto" ma "quanto vale, in euro, su un orizzonte di venticinque anni". Meridiana calcola gli indicatori standard di valutazione degli investimenti — valore attuale netto (NPV), tasso interno di rendimento (IRR), costo livellato dell'energia (LCOE), payback semplice e attualizzato — applicando uno scenario economico configurabile: tasso di sconto, escalation dei prezzi dell'energia, degradazione della produzione nel tempo. Sono inclusi gli incentivi italiani rilevanti — PNRR Scuola 4.0, IVA agevolata per enti pubblici, Comunità Energetiche Rinnovabili, Conto Energia per la pubblica amministrazione, Transizione 5.0 — modellati nelle loro forme specifiche e con i loro limiti temporali dichiarati.

`engine/economics.py` (1 156 righe) consuma i 25 anni di bilancio e produce un'**analisi tecno-economica completa**: cash flow, NPV, IRR, payback, LCOE, sensitivity, score.

### 6.1 CAPEX

`InvestmentCost` (`engine/economics.py:44-98`):

- **PV puro**: `pv_cost_eur_kWp · peak_power_kWp`. Default `1100 €/kWp` (commerciale 10–100 kWp). Ordini di grandezza tipici Italia 2024 documentati nelle docstring (`engine/economics.py:56-59`).
- **Batteria** (opzionale): `battery_cost_eur_kWh · battery_capacity_kWh`. Default `550 €/kWh` (LFP AC-coupled).
- **Progettazione/permessi/connessione/altro**: voci esplicite, default 0.

### 6.2 OPEX

`OperatingCost` (`engine/economics.py:101-149`):

- `om_pct_of_capex_per_yr` (default `1.0 %`)
- + `om_fixed_eur_yr` (contratto a quota fissa, default 0)
- + `insurance_eur_yr` (default 0)
- Tutto **escalato** all'`om_escalation_rate` (default `2 %/anno`, inflazione attesa)
- **Sostituzione inverter** all'anno `inverter_replacement_year` (default 12)
- **Sostituzione batteria** agli anni elencati in `battery_replacement_years`, prezzo `battery_replacement_cost_eur_kWh` (default `350 €/kWh`, ~60 % del prezzo iniziale dell'epoca)

### 6.3 Parametri finanziari

`FinancialParameters` (`engine/economics.py:152-189`): discount rate WACC (default 5 %), energy price escalation (default 2.5 %/anno), orizzonte (default 25 anni). Per investimento finanziato: `financing_equity_fraction`, `loan_interest_rate`, `loan_duration_years`. La rata annua è una rendita standard `PMT = P · r(1+r)ⁿ / ((1+r)ⁿ−1)` (`engine/economics.py:338-350`).

### 6.4 Incentivi italiani modellati

`ItalianIncentives` (`engine/economics.py:192-239`):

| Incentivo | Norma | Cosa fa | Quando |
|---|---|---|---|
| **Detrazione fiscale IRPEF/IRES** | L. 449/1997 + proroghe | % della CAPEX detratta dalle imposte, spalmata su N anni (default 50 % su 10 anni per residenziale) | Annuale per `detrazione_spread_years` |
| **Ammortamento accelerato** | DPR 917/1986 TUIR | Capex ammortizzato contro reddito imponibile (corporate). Scudo fiscale = `CAPEX · aliquota · IRES%` annuo | Per `1/aliquota` anni (default ~11) |
| **Transizione 5.0** | DM 2024 | Credito d'imposta su investimenti ESG (default 0 — va abilitato esplicitamente) | Lump sum in anno 1 |
| **CER (Comunità Energetica Rinnovabile)** | L. 234/2021 + DM 07/12/2023 | GSE paga `~0.11 €/kWh` su energia condivisa | Annuale per tutta la vita |

**Convenzione importante:** il modello applica gli incentivi al **valore attuale (PV)** lordo in `_incentive_present_value` (`engine/economics.py:357-400`) per il calcolo dei totali, **e separatamente** li distribuisce ora per ora nel cash flow annuale. Non si doppia-contano.

**Avvertenza nel codice:** la stringa nel docstring (`engine/economics.py:222`) avverte che "Transizione 5.0 must verify eligibility — set to 0 if uncertain". L'incentivo è uno strumento di stima, non una garanzia di accesso.

### 6.5 Indicatori finanziari

Per ogni anno (`AnnualCashFlow`, `engine/economics.py:246-264`) si calcola:

```
net_cf = energy_saving + export_revenue + cer_incentive + tax_benefit
        − om_cost − replacement_cost − loan_payment
```

dove `energy_saving` e `export_revenue` sono **escalati** dell'`energy_price_escalation` rispetto all'anno 1 (`engine/economics.py:815-817`).

- **NPV** = Σ DCF − equity outflow al t=0 (`engine/economics.py:887`). DCF = net_cf / (1+r)^anno.
- **IRR** risolto numericamente con Newton-Raphson e fallback bisezione (`engine/economics.py:407-459`). Restituisce −1.0 (= "non definito") se non esiste cambio di segno.
- **Payback semplice e attualizzato**: anno in cui il cumulato passa zero, con interpolazione lineare intra-anno (`engine/economics.py:894-919`).
- **LCOE** = `(CAPEX + Σ DOM) / Σ DE` (`engine/economics.py:466-487`), break-even del costo dell'energia: confronta con la tariffa di rete corrente per capire se conviene rispetto al "non investire".
- **ROI** = NPV / CAPEX, **B/C ratio** = PV(benefici) / CAPEX.

### 6.6 Analisi di sensibilità

Sempre calcolata, 5 scenari predefiniti (`engine/economics.py:943-949`):

| Scenario | Escalation | Discount |
|---|---:|---:|
| Pessimistic | 0.0 % | 7.0 % |
| Base case | input utente | input utente |
| Optimistic | 4.0 % | 4.0 % |
| High tariff | 5.0 % | input utente |
| High discount | input utente | 8.0 % |

Per ciascuno si ricomputano NPV, IRR, payback semplice e attualizzato via `_calc_economics_core` (`engine/economics.py:624-701`) che evita la ricorsione infinita (sensitivity non chiama se stessa).

### 6.7 Score composito 0–10

`_investment_score` (`engine/economics.py:494-617`) sintetizza in un numero. **Cinque componenti, 2 punti ciascuna**:

1. **IRR vs discount spread**: 2.0 se IRR ≥ 15 %, 1.5 se 10–15 %, 1.0 se 7–10 %, 0.5 se 4–7 %, 0 se < 4 %
2. **Payback semplice**: 2.0 se ≤ 5 anni, 1.5 ≤ 8, 1.0 ≤ 12, 0.5 ≤ 15, 0 oltre
3. **NPV/CAPEX**: 2.0 se ≥ 100 %, 1.5 ≥ 60 %, 1.0 ≥ 30 %, 0.5 ≥ 10 %, 0 sotto
4. **Self-consumption rate**: 2.0 se ≥ 80 %, 1.5 ≥ 60 %, 1.0 ≥ 40 %, 0.5 ≥ 20 %, 0 sotto
5. **Robustezza payback** (payback / orizzonte): 2.0 se payback ≤ N/3, 1.0 se ≤ N/2, 0 oltre

Verdetto associato (`engine/economics.py:608-615`):

- ≥ 8.0 → "Excellent investment"
- 6.0–8.0 → "Good investment"
- 4.0–6.0 → "Marginal — review assumptions"
- < 4.0 → "Not recommended at current conditions"

Il punteggio è uno strumento di sintesi, **non una raccomandazione regolamentare**: cinque criteri pesati ugualmente in modo arbitrario. Le sottoscore (`score_breakdown`) sono sempre restituite per permettere ispezione manuale.

### 6.8 Confronto multi-scenario

`engine/scenarios.py` (882 righe) orchestra il confronto economico tra **cinque scenari** definiti per stringa identificativa (`engine/scenarios.py:60-64`):

1. **`baseline`** — situazione attuale (gas + elettricità da rete, niente investimento, NPV non calcolato).
2. **`solo_pv`** — solo fotovoltaico, riscaldamento invariato.
3. **`solo_hp`** — solo pompa di calore, elettricità da rete.
4. **`pv_hp`** — fotovoltaico + PdC monovalente.
5. **`pv_hp_biv`** — fotovoltaico + PdC bivalente con caldaia di backup.

Per ciascuno, `compare_scenarios` (`engine/scenarios.py:443`) costruisce il bilancio (`energy_balance`), eventualmente la simulazione PdC (`heat_pump`), e infine l'analisi economica (`economics`). Il risultato (`ScenarioComparison`, `engine/scenarios.py:230`) include ranking per NPV, payback più breve, e una **sensitivity sul prezzo del gas** ai moltiplicatori `[0.50, 0.80, 0.95, 1.20, 1.50, 2.00]` rispetto al prezzo corrente (`engine/scenarios.py:137`). Per ogni multiplo si ricalcola l'NPV linearizzato con annuity factor — utile per decisori che vogliono sapere "a quale prezzo del gas conviene comunque la pompa".

Il break-even del gas è calcolato esplicitamente in `BuildingEnergyProfile.gas_price_breakeven_eur_sm3` (`engine/energy_vectors.py:460`): prezzo per Sm³ al quale costo annuale PdC = costo annuale gas attuale. La formula assume copertura termica equivalente (stesso fabbisogno utile dell'attuale boiler) — è il "prezzo di indifferenza" rispetto al sistema corrente.

---

## 7. Modulazione scolastica (segmento scuole)

Una scuola non consuma energia con lo stesso profilo temporale di un'azienda o di una casa. È chiusa quasi tre mesi l'estate, alterna giorni pieni e vuoti, riapre a settembre con esigenze di riscaldamento crescenti, e la sua curva oraria di domanda dipende fortemente dal calendario scolastico — che in Italia cambia da regione a regione. Modellare l'energia di un edificio scolastico con i profili generici di un'utenza commerciale produce stime inattendibili. 
Meridiana integra un overlay specifico per il segmento scuola: calendari delle 21 regioni italiane, festività nazionali e patronali, modulazione settimanale (lunedì-venerdì rispetto a sabato-domenica), zone climatiche del DPR 412/93 (A-F) che determinano i periodi di accensione del riscaldamento. Il risultato è una curva di consumo orario coerente con la realtà operativa di una scuola italiana.

### 7.1 Calendario scolastico italiano

`school/calendar_it.py` contiene **21 calendari**: 20 regioni amministrative + Trento e Bolzano splittate come province autonome (anno scolastico 2025/26). L'elenco completo è in `school/calendar_it.py:79-145`: abruzzo, basilicata, calabria, campania, emilia_romagna, friuli_vg, lazio, liguria, lombardia, marche, molise, piemonte, puglia, sardegna, sicilia, toscana, trento, bolzano, umbria, valle_daosta, veneto. L'unico alias accettato è `friuli_venezia_giulia → friuli_vg` (`REGION_ALIASES`, `school/calendar_it.py:147`): è un alias puramente linguistico — una regione, un calendario, nessuna ambiguità. Per il Trentino-Alto Adige il chiamante deve scegliere esplicitamente `trento` o `bolzano`, perché le due province autonome hanno calendari diversi (in particolare la pausa autunnale lunga del 30 ottobre–8 novembre, propria di Bolzano e non di Trento). Passare la stringa generica `trentino_alto_adige` solleva `ValueError` con un messaggio che indica l'azione corretta — comportamento attivato a maggio 2026 dopo che era emerso che il vecchio alias `trentino_alto_adige → trento` cancellava silenziosamente la pausa altoatesina.

**Classificazione regione da coordinate.** `region_from_coordinates(lat, lon)` (`school/calendar_it.py:230`) restituisce la chiave regione attraversando i poligoni amministrativi ufficiali dell'ISTAT (`school/data/regioni_istat.geojson`, ~131 KB, semplificati a tolleranza ~2 km con preservazione di area > 99.2 % per regione). La libreria `shapely` esegue il test di contenimento puntuale; per punti che cadono fuori da ogni poligono — mare, confine, territorio estero — la funzione ritorna la regione più vicina per distanza poligonale. Il Trentino-Alto Adige, che nel GeoJSON ISTAT è un'unica regione, viene splittato per latitudine (soglia 46.45°N) nelle chiavi `trento` e `bolzano`. Fino a maggio 2026 questa classificazione usava una catena di bounding-box rettangolari sovrapposti (`school/calendar_it.py` ante-rifattorizzazione): un test sistematico su 94 capoluoghi italiani aveva mostrato un tasso di errore del 22 % (3 capoluoghi regionali misclassificati — L'Aquila, Firenze, Perugia — più l'intero cluster nord della Toscana attribuito all'Emilia-Romagna). Con i poligoni ISTAT l'errore sulla stessa lista scende a zero.

**Campi per regione**:

- `start` / `end`: tupla `(mese, giorno)` per inizio/fine anno scolastico
- `autumn_break`: pausa autunnale se prevista (solo Trento `10/31–11/02` e Bolzano `10/30–11/08`)
- `carnival_days`: giorni di chiusura per Carnevale (Valle d'Aosta 4, alcune 2, altre 1 o 0)
- `patron_day`: festa del santo patrono

**Festività nazionali** hardcoded (`school/calendar_it.py:255-266`): Capodanno, Epifania, 25 aprile, 1° maggio, 2 giugno, Ferragosto, Ognissanti, Immacolata, Natale, Santo Stefano. **Pasqua** calcolata dall'algoritmo di Gauss per l'anno di riferimento + 1 (`_easter_date`, `school/calendar_it.py:49-70`); vacanze pasquali = `easter_doy ± 3 giorni`. **Vacanze natalizie**: 23–31 dicembre + 1–7 gennaio.

**Profili orari di occupazione**. `build_school_calendar` (`school/calendar_it.py:406`) restituisce un array 8 760 con frazioni di occupazione orarie [0,1]. I profili giornalieri sono diversi per tipologia (`_make_school_day_profile`, `school/calendar_it.py:306-354`):

- **Primary full-time**: 8–12h pieno (1.00), discesa progressiva fino a 16:00 (0.20)
- **Primary part-time / middle / high classic**: 8–11h pieno, pranzo 12 (0.80), uscita 13 (0.30)
- **High tech**: aggiunge laboratori il lunedì e mercoledì pomeriggio (`_LAB_DOWS = {0, 2}`)
- **University**: 8–18h pieno (10 ore), strascico fino a 19

Tre overlay configurabili: `extra_evening` (18–22h al 0.40, corsi serali), `extra_weekend_saturday` (sab 9–17 al 0.25), `extra_weekend_sunday` (dom 9–13 al 0.25).

Il `presence_factor` globale (default 0.87, `school/loads_school.py:103`) scala il profilo per tener conto di assenze tipiche.

### 7.2 Zone climatiche italiane (DPR 412/93)

`school/climate_zones.py` (168 righe) implementa le **sei zone A–F** del DPR 412/93, identificate dai Gradi Giorno annui:

| Zona | GG | Mesi accensione | Ore/giorno |
|---|---|---|---:|
| A | < 600 | 12 → 3 | 6 |
| B | 601–900 | 12 → 3 | 8 |
| C | 901–1 400 | 11 → 3 | 10 |
| D | 1 401–2 100 | 11 → 4 | 12 |
| E | 2 101–3 000 | 10 → 4 | 14 |
| F | > 3 000 | 10 → 4 | 14 |

**Auto-detezione da latitudine** (`zone_from_latitude`, `school/climate_zones.py:106`): B se lat < 38.5, C < 41.5, D < 44.5, E < 46, F altrimenti. Approssimazione utile per pre-popolare la zona di un comune ignoto; per stime ufficiali si dovrebbe usare la zona certificata da ENEA per quel comune.

**Benchmark elettrico** (`SCHOOL_ELECTRIC_BENCHMARK`, `school/climate_zones.py:58`): 6 zone × 5 tipologie scuola (primary, middle, high_classic, high_tech, university_dept) = 30 valori in kWh_el/m²/anno. Range: scuola primaria zona A 18 kWh/m² → università tecnica zona F 65 kWh/m².

**Benchmark termico** (`SCHOOL_THERMAL_BENCHMARK`, `school/climate_zones.py:81`): 6 zone × 4 ere di costruzione (`classify_construction_era`, `school/climate_zones.py:93`: pre_1973, 1973–1990, 1991–2005, post_2005) = 24 valori. Range: scuola post-2005 zona A 8 kWh_th/m² → edificio pre-1973 zona F 160 kWh_th/m².

**Fallback** se zona/tipo non trovato: `(E, era_match)` per termico, `(E, school_type)` per elettrico. Fonti citate nel codice: DPR 412/93, ENEA, RSE, FIRE, CTI/UNI TS 11300.

### 7.3 Profili di carico scolastici

`school/loads_school.py` (1 028 righe) costruisce profili orari ad alta risoluzione separati in **10 categorie d'uso**: lighting, computer/IT, technical labs, canteen, gym, administration, AC, heating, elevator, miscellaneous. Per ogni categoria un profilo giornaliero `[24]` viene replicato modulato dal calendario orario.

Caratteristiche distintive:

**Modulazione DOW (day-of-week) termica** (`school/loads_school.py:45-51`, commit `e008bd4`): peso lunedì 1.15, martedì 1.02, mercoledì 1.00, giovedì 0.98, venerdì 0.85. Somma = 5, preserva il budget settimanale. Modellizza l'inerzia termica: il lunedì la scuola parte fredda (~30 % in più di carico), il venerdì pomeriggio si lascia raffreddare.

**Illuminazione daylight-aware** (`school/loads_school.py:425-456`, commit `9d2844f`): curva GHI → fattore di accensione lights interpolata dalla tabella EN 12464-1 [150, 200, 300, 400, 500, 600] W/m² → [1.00, 0.90, 0.60, 0.40, 0.30, 0.25]. I corridoi hanno un'ulteriore riduzione del 20 % quando GHI > 400 W/m². Le luci in scuola seguono cioè effettivamente la luce naturale, contrariamente all'assunzione "accese 8–13 a prescindere".

**ACS realistica per scuola italiana** (`school/loads_school.py:118-205`):

- **Mensa**: `n_pasti/giorno × giorni × 0.30 × ΔT/(55−14)` kWh_th, dove ΔT è la differenza temperatura ACS–rete (commit `4539b20` ha corretto un sovrastima storica).
- **Docce palestra**: 45 L per doccia × ΔT × `0.00116 kWh/(L·K)` (= c_p·ρ/3600). Rate base 3 % degli alunni che si docciano (rara consuetudine italiana), saliente a 8 % con `extra_weekend` (commit `d2d94e4`).
- **Lavabi**: profilo stagionale 1.5×/1.0×/0.6×/0.3× (inverno-estate); base 0.7 L/persona se edificio post-1990, 0.3 L pre-1990.
- **Standby loss**: maggiorazione fissa del 15 % (`dhw_standby_losses_pct`, `school/loads_school.py:107`).

**Apporti interni metabolici** (`school/loads_school.py:541-543`, ASHRAE Fundamentals Ch.18): 70 W/studente in aula, 75 W/docente, 85 W/ATA. Modellati anche come riduzione del fabbisogno di riscaldamento (commit `9417f1c`): il calore corporeo abbatte il delta termico richiesto al sistema. Per scenari con pompa di calore la riduzione viene convertita da kWh termici a kWh elettrici equivalenti dividendo per il SCOP (`school/loads_school.py:701-707`) prima di sottrarla al consumo elettrico HP, così da garantire coerenza dimensionale.

**Modello parametrico per il condizionamento estivo (AC)** (`school/loads_school.py:647-668`): il consumo elettrico per raffrescamento è calcolato con una formula esplicita che combina tre fattori — la zona climatica DPR 412/93 dell'edificio, l'estensione dell'AC scelta dall'utente e l'intensità d'uso scelta dall'utente. Sostituisce il vecchio modello a "60 ore equivalenti hardcoded indipendentemente da zona e utilizzo", che sovrastimava grossolanamente il consumo per scuole con AC limitato (la stragrande maggioranza del parco italiano) e sottostimava per scuole moderne intensamente climatizzate. La formula è `consumo = floor_area × coverage_fraction × W/m² × ore_zona_base × intensity_multiplier / 1000`, lineare in entrambe le dimensioni utente — coverage modula soltanto l'area servita, intensity modula soltanto le ore di funzionamento, senza effetti quadratici. Le mappe sono: ore_zona_base da 600 (zona A) a 20 (zona F) calibrate su letteratura ENEA + osservazione diretta su scuole emiliane; coverage `none`/`few_zones`/`some_classrooms`/`almost_all` (0.0/0.15/0.50/0.90); intensity `rare`/`moderate`/`intensive` (0.25/0.55/0.90). I default conservativi per richieste API senza i nuovi campi sono `few_zones` + `rare`, coerenti con la scuola italiana media (segreteria climatizzata, uso saltuario). Il modello cattura una variabilità di consumo AC fino a un fattore ~100× tra l'estremo inferiore (poche zone, raro) e quello superiore (quasi tutte, intensivo) per la stessa zona climatica. Limite residuo dichiarato: l'`ore_zona_base` non integra esplicitamente i Cooling Degree Days locali da TMY, usa la zona DPR 412/93 come proxy; sviluppi futuri potranno raffinare con CDD calcolati ora per ora.

**Valutazione strutturale tetti** (`school/loads_school.py`, commit `cfcc013`): stima cost-of-readiness basata sull'era di costruzione, valuta se il tetto regge l'installazione PV senza opere strutturali aggiuntive.

### 7.4 Incentivi specifici scuole

`school/incentives_school.py` (126 righe) modella incentivi cumulabili per scuole pubbliche italiane:

- **IVA agevolata 10 %** (vs aliquota standard 22 %): risparmio in valore presente = `CAPEX × (0.22 − 0.10) / 1.22` (`school/incentives_school.py:61`). Si applica a enti pubblici per investimenti rispondenti all'art. 16 della Tabella A parte III DPR 633/72.
- **PNRR Scuola 4.0 M4C1** (`school/incentives_school.py:77`): contributo a fondo perduto, default modellato al 50 % della CAPEX IVA-corretta. Va verificato il bando specifico per il singolo istituto — il modello calcola, non garantisce ammissibilità.
- **CER (Comunità Energetica Rinnovabile)** GSE: incentivo `0.11 €/kWh` su frazione condivisa (default 60 % della produzione, `school/incentives_school.py:91`). Riferimento normativo: DM 07/12/2023.
- **Bonus GSE PA** menzionato nei commenti (`~0.02 €/kWh`) ma **NON implementato**: va attivato manualmente nei parametri economici se applicabile.

L'output (`school/incentives_school.py:113-116`): CAPEX netta dopo IVA e contributo PNRR, beneficio annuo CER, total_first_year_benefit. Il modello **non risolve** la cumulabilità: un funzionario deve verificare per ciascun bando i requisiti di esclusività.

**Fonte calendari e norme:** calendari ufficiali pubblicati dagli Uffici Scolastici Regionali (USR) e dal MIM (Ministero dell'Istruzione e del Merito); incentivi normati da DM 07/12/2023 (GSE/CER), DPR 633/72 (IVA), bandi PNRR M4C1 pubblicati da Unità di Missione PNRR Istruzione. I calendari vanno **aggiornati annualmente** prima di ogni anno scolastico (file `school/calendar_it.py` è dimensionato attualmente sull'anno 2025/26).

---

## 8. Validazione e calibrazione

Un modello fisico-economico, per quanto rigoroso nella sua costruzione, deve essere verificato contro casi reali. Meridiana usa Borzano di Albinea — un edificio scolastico in provincia di Reggio Emilia, a 44.601°N e 10.636°E — come caso di riferimento per il regression testing. Ogni refactor del motore, ogni aggiunta di funzionalità, ogni modifica architetturale deve produrre risultati identici al millesimo sul caso Borzano: questa è la garanzia che il comportamento del motore resta stabile tra versioni. Oltre al caso Borzano, una suite di test automatici copre i passaggi critici — bilancio energetico, conservazione dell'energia, edge case del calendario scolastico, retrocompatibilità multi-footprint. I test che falliscono bloccano il commit.

**Caso di riferimento.** Tutti i refactor del motore vanno validati su:

- **Borzano di Albinea (RE)**, 44.601° N, 10.636° E, elevazione 173 m
- Impianto 100 kWp, tilt 15°, azimuth sud
- Produzione di riferimento simulata: **131 763 kWh/anno** (motore standalone)

Questo è il "caso oro" della retrocompatibilità: ogni cambiamento al codice del motore deve dare lo stesso valore al millesimo, salvo refactor esplicitamente motivati. Un test pytest dedicato (`tests/test_multi_footprint.py` per il caso 1-footprint == single-building) presidia questa invariante.

**Test suite.** 18 test pytest verdi al 13 maggio 2026, tra cui:

- `test_multi_footprint.py` — multi-edificio = single-building per N=1
- `test_scenarios_level1.py` — confronto scenari smoke test
- `test_loads_school_dow.py` — modulazione giorno settimana scuole

Lanciabili con `pytest tests/`.

**Discrepanza nota — il "−10 %".** Il numero di riferimento del motore standalone (131 763 kWh/anno) e il valore conservativo pubblicato nei materiali pubblici Meridiana (banner sito ~120 000 kWh/anno) differiscono del ~9 %. La differenza è ancora **non riconciliata in produzione**:

- Il motore standalone è stato calibrato senza pvlib e con coefficienti Perez/Faiman al vincolo di letteratura.
- Il valore pubblicato è prudenziale; usa probabilmente assunzioni più stringenti su `shading_near`, `availability` o copertura.
- Quando pvlib girerà in produzione su Railway, il numero del motore potrebbe scendere a ~125 000 kWh/anno (il percorso pvlib usa modelli leggermente più conservativi su clipping e inverter low-load).
- Da risolvere prima del lancio commerciale settembre 2026 (registrato in `CLAUDE.md` repo backend, sezione "Problemi noti").

---

## 9. Limitazioni e zone di incertezza

Nessun modello cattura la realtà al 100%. Dichiarare esplicitamente cosa il modello non fa, dove approssima, dove perde precisione, non è un'ammissione di debolezza: è il fondamento della fiducia che lo strumento può chiedere al lettore. Un modello che pretende di essere esatto è un modello che mente. In questa sezione raccogliamo, divise per area tematica — solare, termico, economico, architetturale — le limitazioni note al momento di questa edizione del documento. Alcune sono intrinseche al tipo di approccio (per esempio, il TMY è una sintesi statistica, non il meteo di un anno specifico); altre sono compromessi pratici che contiamo di superare in versioni future del motore.

Pubblicate **integralmente** perché siano l'argomento di chi vuole contestare il modello.

### Climatiche

- **TMY ≠ anno reale.** Un singolo anno futuro può scostarsi del ±5–10 % dalla produzione TMY. Per banche/investitori istituzionali serve simulazione su 15+ anni storici (P50/P90) — non implementato.
- **Risoluzione spaziale 0.1°.** Microclimi sub-griglia non catturati.
- **Albedo 0.20 fisso** (`engine/solar.py:457`). In alta montagna con neve l'albedo reale può superare 0.6; in città su asfalto ~0.10. Non parametrizzato.

### Solare

- **Shading 3D non modellato.** L'orizzonte usato è solo quello geografico di PVGIS. Ostacoli costruiti (palazzi, alberi, ciminiere) entrano solo come perdita media `shading_near`.
- **`usable_fraction = 0.70` default** generico (`engine/solar.py:83`): nessun'analisi della geometria reale del tetto.
- **`200 W/m²` densità modulo** hardcoded (`engine/solar.py:92`) — corretto per moduli mainstream 2024, sottostima moduli alta efficienza (>23 %).
- **Bias pvlib vs standalone.** I due percorsi divergono di 2–3 % annuali su casi estremi (alte latitudini, alti tilt). Il default è pvlib se installato.

### Termico (pompa di calore)

- **No part-load efficiency.** Sovrastima leggermente il SCOP su PdC sovradimensionate (`engine/heat_pump.py:1-40` docstring lo dichiara esplicitamente).
- **No isteresi termica edificio.** L'edificio è "magrissimo": risposta istantanea alla differenza T_interna − T_esterna. Per edifici inerziali (scuole anni '70 in calcestruzzo) si perde un effetto smoothing.
- **`defrost_loss_factor` generico** non distingue tra regimi di sbrinamento diversi.
- **`carnot_efficiency_factor` empirico**: 0.42/0.46/0.55 sono medie di mercato. PdC specifiche possono discostare anche del 15 %.

### Economico

- **Prezzi vettori escalation uniforme.** Il modello non distingue tra "gas che sale del 5 %" e "elettricità ferma": ciascun vettore ha la sua escalation costante. La volatilità storica reale (es. 2022) non è catturabile con un singolo tasso.
- **Tariffa export hardcoded.** Il modello assume `ritiro_dedicato` con tariffa fissa configurabile; lo Scambio sul Posto (chiuso per nuovi impianti dal 2024) non è modellato.
- **Incentivi rappresentano lo "stato attuale".** Norme italiane cambiano spesso (Superbonus 110/90/65/50, Transizione 5.0 in evoluzione). Il modello fotografa lo stato al momento della stima — non garantisce che gli incentivi saranno ancora disponibili o invariati al momento dell'effettiva realizzazione.
- **CER fortemente semplificato.** Modellato come `tariffa fissa · frazione condivisa · produzione`. Non simula la dinamica oraria di una comunità reale (membri produttori vs consumatori, scambi GSE).
- **Score 0–10 arbitrario.** Le soglie e i pesi sono scelte editoriali, non derivate da analisi statistica. Servono come tassonomia, non come misura assoluta.

### Architetturali

- **Singolo TMY per tutti i footprint multi-edificio.** Stessa orizzonte, stesso vento. Ragionevole per edifici < 50 m di distanza, peggiora con la separazione.
- **Niente shading reciproco tra footprint.** Se uno edificio fa ombra all'altro nelle ore basse di novembre, non lo modelliamo.
- **`api_legacy.py` non più in uso** ma ancora presente nel repo. La logica corrente è in `api/main.py`.

---

## 10. Riferimenti

### Codice (questo repo)

Tutti i percorsi sono relativi alla root `pv_engine/`:

- `engine/solar.py` — modello solare orario, IAM, Faiman, perdite
- `engine/pvgis_client.py` — client TMY PVGIS
- `engine/heat_pump.py` — modello PdC oraria, dispatch bivalente
- `engine/energy_vectors.py` — vettori, prezzi, fattori CO₂
- `engine/energy_balance.py` — bilancio orario PV + carichi + batteria
- `engine/economics.py` — NPV, IRR, LCOE, incentivi italiani, score
- `engine/scenarios.py` — confronto scenari multi-vettore
- `engine/envelope.py` — modello d'involucro termico (UNI EN ISO 13790)
- `engine/loads.py` — profili orari di carico generici
- `schemas.py` (root) — schemi Pydantic legacy condivisi
- `school/` — overlay per il segmento scuole
- `tests/` — pytest verdi

### Standard e norme

**Fotovoltaico e inverter**
- **IEC 61724-1:2021** — Photovoltaic system performance — Monitoring
- **IEC 61853-1:2011** — PV module power rating
- **IEC 61853-2:2016** — IAM, spectral and angular effects
- **IEC 62670-3:2016** — PV performance — Energy evaluation
- **EN 50530:2010** — Inverter efficiency (modello semplificato)
- **EN 50549:2019** — Connessione di sistemi di generazione alla rete

**Pompe di calore e impianti termici**
- **EN 14825** — Heat pump seasonal performance (calibrazione η-Carnot)
- **EN 15316-3-1:2017** — Energy performance — Hot water needs
- **EN 15316-4-2** — Heat pump system methodology
- **EN 15193-1:2017** — Energy requirements for lighting
- **EN 13779:2008** — Ventilation for non-residential buildings
- **UNI EN 12464-1** — Lighting of indoor work places

**Involucro e fabbisogni termici edifici**
- **EN ISO 13790** — Quasi-steady thermal demand
- **EN ISO 13370** — Heat transfer via the ground
- **EN ISO 13789** — Heat loss coefficients
- **EN ISO 14683** — Linear thermal bridges
- **EN ISO 10211** — 3D thermal bridges
- **EN ISO 6946** — Thermal resistance of building elements
- **UNI TS 11300-1:2014** — Determination of thermal energy needs
- **UNI TS 11300-2:2019** — Determination of energy demand for DHW

**Batterie e storage**
- **IEC 62619:2022** — Safety requirements for secondary batteries

**Norme italiane**
- **NTC 2018** — Norme Tecniche per le Costruzioni
- **DPR 412/93** — Zone climatiche italiane (A–F) e periodi di accensione
- **DPR 633/72** — IVA (aliquota agevolata 10 % per enti pubblici)
- **DM 23/06/2016** — Scambio sul Posto (GSE)
- **L. 234/2021 + DM 07/12/2023** — Comunità Energetiche Rinnovabili
- **DM Piano Transizione 5.0 (2024)** — Credito d'imposta
- **DPR 917/1986 (TUIR)** — Ammortamenti fiscali
- **PNRR M4C1 (Istruzione)** — Bandi Scuola 4.0

**Economia dei sistemi edilizi**
- **EN 15459-1:2017** — Economic evaluation of building energy systems
- **ASHRAE 90.1-2019** — Energy Standard for Buildings (carichi commerciali)
- **ASHRAE Fundamentals Ch.18** — Internal heat gains (apporti metabolici)
- **IEA ECBCS Annex 42** — Building load profiles

### Letteratura scientifica

- Perez R., Ineichen P., Seals R., Michalsky J., Stewart R. (1990). *Modeling daylight availability and irradiance components from direct and global irradiance*. **Solar Energy 44(5), 271–289.**
- Faiman D. (2008). *Assessing the outdoor operating temperature of photovoltaic modules*. **Progress in Photovoltaics 16(4), 307–315.**
- Spencer J.W. (1971). *Fourier series representation of the position of the sun*. **Search 2(5), 172.**
- Souka A.F., Safwat H.H. (1966). *Determination of the optimum orientations for the double-exposure flat-plate collector and its reflectors*. **Solar Energy 10(4).** (modello IAM ASHRAE)
- Martin N., Ruiz J.M. (2001). *Calculation of the PV modules angular losses under field conditions by means of an analytical model*. **Solar Energy Materials and Solar Cells 70(1), 25–38.**

### Dati pubblici

- **PVGIS — JRC EU Commission**: <https://re.jrc.ec.europa.eu/pvg_tools/en/>
- **ENTSO-E Transparency Platform** (mix elettrico IT): <https://transparency.entsoe.eu>
- **ARERA — bollettini tariffari**: <https://www.arera.it>
- **ISPRA — Fattori di emissione**: <https://www.isprambiente.gov.it>
- **MIM — calendari scolastici regionali**: aggiornati ogni anno dagli Uffici Scolastici Regionali
- **pvlib python** (cross-check): <https://pvlib-python.readthedocs.io>

---

*Documento aggiornato al 17 maggio 2026. Aggiornare quando: (a) un coefficiente fisico cambia in `engine/`, (b) si aggiungono incentivi o si rimuovono quelli citati, (c) un test di retrocompatibilità (Borzano) cambia il valore di riferimento.*
