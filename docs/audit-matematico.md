# Audit matematico di Meridiana — dal comune e dalla bolletta al verdetto

**Data:** 4 ottobre 2026 · **Perimetro:** flusso residenziale `POST /estimate/casa` e i moduli del motore che usa · **Modalità:** sola lettura, nessuna modifica al codice.

> **Nota sulla versione.** L'audit fotografa il *working tree* al 4/10/2026, che contiene modifiche non ancora committate (integrazione Google Solar: `residential/roof_google.py`, `api/routers/estimate_casa.py`, `api/schemas/estimate_casa.py`). I riferimenti `file:riga` valgono per quello stato.
>
> **Come sono stati ottenuti i numeri.** Tutti i numeri dell'esempio e dei grafici vengono da un'esecuzione reale del motore (script di audit fuori dal repo) sul TMY PVGIS di Albinea già in cache e sull'endpoint chiamato con `TestClient`. La geometria del tetto è quella della fixture "Borzano probe 2026-09-28" (`tests/test_estimate_casa_api.py:1111-1131`). **Google Solar non è stato interrogato** (nessuna chiave in ambiente).

Legenda delle etichette usate nel testo:

- 🟢 **fisica consolidata**: modello pubblicato, validato in letteratura e usato così com'è;
- 🟡 **modello semplificato**: fisica corretta ma approssimata (risoluzione, parametri medi);
- 🔴 **assunzione nostra**: un numero o una regola scelti da noi, plausibili ma non misurati.

---

## Sintesi per chi ha fretta

1. **Lo scheletro fisico è solido**: posizione solare, trasposizione di Perez, temperatura di cella di Faiman, bilancio orario su 8.760 ore e flussi di cassa attualizzati sono standard e implementati correttamente.
2. **I numeri che decidono il verdetto sono assunzioni nostre**: prezzo di cessione 0,08 €/kWh, escalation al 2,5 %/anno (applicata *anche* alla cessione), costo d'impianto a scaglioni, soglie del verdetto. Per Borzano basta spostarne uno qualsiasi per cambiare segno all'NPV.
3. **Undici discrepanze tra codice e documentazione o tra parti del codice** (elencate in §L.3). Le più pesanti:
   - **(a)** il TMY usato è **PVGIS-SARAH2** (API v5.2), non SARAH3 come dicono docstring, METHODOLOGY e CLAUDE.md;
   - **(b)** **pvlib non è installato**: né in locale né su Railway (`requirements.txt` non lo elenca). Gira sempre il percorso *standalone*, quindi i riferimenti a pvlib nei docstring non descrivono la produzione;
   - **(c)** la **capienza IRPEF non entra nell'NPV**: cambia solo un campo informativo;
   - **(d)** la giustificazione della soglia "marginale" ("≈0,4 % annuo") è **sbagliata**: NPV/capex = 10 % corrisponde a un IRR di circa il 6 %, non dello 0,4 %;
   - **(e)** nello scenario elettrificazione **senza dati gas**, lo SCOP mostrato all'utente (es. 1,97 con i termosifoni) **non viene usato nel calcolo**.
4. **Le bande di incertezza (±15 / ±30 / ±45 %) sono scelte, non misurate.** Coprono solo la domanda: non coprono produzione, prezzi, costi, ombre e norme.

---

# PARTE 1 — La risorsa solare

## 1.1 Da dove vengono i dati climatici

| Voce | Valore reale | Dove |
|---|---|---|
| Servizio | PVGIS (JRC, Commissione Europea), endpoint `/api/v5_2/tmy` | `engine/pvgis_client.py:47`, `:222-230` |
| Database irraggiamento | **PVGIS-SARAH2** (letto dal metadato `radiation_db` della risposta) | cache `/tmp/pvgis_cache/tmy_44.61_10.61.json` |
| Periodo | 2005–2020 (default TMY di PVGIS 5.2 per SARAH2) | non impostato nel client: si usa il default del servizio |
| Temperatura, vento, umidità | rianalisi ERA5 (combinata da PVGIS nel TMY) | — |
| Parametri richiesta | `usehorizon=1`, `outputformat=json` | `pvgis_client.py:223-229` |
| Risoluzione | oraria, 8.760 record, timestamp in UTC | `pvgis_client.py:63-77` |
| Cache | due livelli (LRU in memoria + disco `/tmp`), chiave = coordinate arrotondate a 0,01° | `api/utils/cache.py:32-42` |

**Discrepanza.** Il docstring (`pvgis_client.py:26`), METHODOLOGY.md (§1) e CLAUDE.md parlano di SARAH-3, ma l'API v5.2 restituisce SARAH2, e lo conferma il metadato del file in cache. SARAH3 è disponibile su PVGIS 5.3. L'effetto sui numeri non è quantificato in questo audit.

**Cos'è un TMY** 🟢. Un *Typical Meteorological Year* è un anno sintetico. Per ogni mese di calendario si sceglie, nella serie pluriennale, il mese "più tipico": quello le cui distribuzioni cumulative di irraggiamento globale, temperatura, umidità e vento sono più vicine a quelle di lungo periodo (metodo ISO 15927-4, statistica di Finkelstein-Schafer). I 12 mesi scelti, di anni diversi, vengono cuciti insieme.

- **Cosa cattura**: il clima medio del sito, con la variabilità *oraria* realistica (giornate nuvolose e serene si alternano davvero).
- **Cosa non cattura**: la variabilità *tra un anno e l'altro*. La produzione reale di un singolo anno oscilla di qualche punto percentuale attorno al TMY (ordine di grandezza ±5 %, valore di letteratura **non misurato da noi**).

**Orizzonte lontano.** Il client chiede `usehorizon=1` e si aspetta il profilo in `outputs["horizon"]` (`pvgis_client.py:300-310`). Nel TMY in cache il profilo è **vuoto** (`horizon_profile = []`), quindi `_is_horizon_shaded` restituisce sempre `False` (`solar.py:544-545`). In pratica l'ombra di colline e montagne **non è applicata** dal nostro modello. Non abbiamo verificato se PVGIS tenga già conto dell'orizzonte dentro i valori di irraggiamento del TMY: **da verificare**.

## 1.2 Quali grandezze arrivano

Per ciascuna ora (`HourlyRecord`, `pvgis_client.py:63-77`):

- $G_h$: irraggiamento globale orizzontale [W/m²]
- $G_{b,n}$ (DNI): diretto normale [W/m²]
- $G_{d,h}$ (DHI): diffuso orizzontale [W/m²]
- $T_{2m}$: temperatura dell'aria [°C]; $v_{10m}$: vento a 10 m [m/s]
- inoltre umidità, infrarosso, direzione del vento e pressione (non usati dal modello FV)

**Albinea (TMY in cache, 44,61 °N 10,61 °E, 165 m):**

| Grandezza | Valore |
|---|---|
| GHI annuo | 1.523,0 kWh/m² |
| DNI annuo | 1.605,7 kWh/m² |
| DHI annuo | 562,2 kWh/m² (37 % del globale) |
| Temperatura media | 13,6 °C |
| Vento medio | 1,5 m/s |

## 1.3 Dall'orizzontale al piano dei pannelli

Il percorso effettivamente eseguito è `_simulate_standalone` (`solar.py:797-890`), perché `pvlib` non è importabile (`solar.py:36-42`, `simulate_pv` a `:1126-1137`).

### Posizione del sole 🟢/🟡

Per ogni ora si calcola la posizione al centro dell'ora: $t_{UTC} = h + 0{,}5$ (`solar.py:813`).

Declinazione (`solar.py:305-310`). Il docstring dice "Spencer", ma la formula implementata è quella di **Cooper**, più semplice (errore ≲ 1°):

$$\delta = 23{,}45^\circ \cdot \sin\!\left(\frac{360}{365}(n-81)\right)$$

Equazione del tempo (Spencer, `solar.py:313-322`), con $B = 2\pi(n-1)/365$:

$$EoT = 229{,}18\,(0{,}000075 + 0{,}001868\cos B - 0{,}032077\sin B - 0{,}014615\cos 2B - 0{,}04089\sin 2B)\ \text{[min]}$$

Tempo solare vero e angolo orario (`solar.py:347-351`):

$$TST = t_{UTC} + \frac{\lambda}{15} + \frac{EoT}{60}, \qquad \omega = 15^\circ\,(TST - 12)$$

Zenit e azimut, con $\varphi$ = latitudine (`solar.py:353-372`):

$$\cos\theta_z = \sin\varphi\sin\delta + \cos\varphi\cos\delta\cos\omega$$

$$\cos\gamma_s = \frac{\sin\delta\cos\varphi - \cos\delta\sin\varphi\cos\omega}{\sin\theta_z}\quad(\gamma_s \to 360^\circ-\gamma_s\ \text{se}\ \omega>0)$$

### Angolo di incidenza 🟢

Con $\beta$ = inclinazione e $\gamma_p$ = azimut del pannello, entrambi gli azimut riferiti al sud (`solar.py:463-485`):

$$\cos\theta = \cos\theta_z\cos\beta + \sin\theta_z\sin\beta\cos(\gamma_s-\gamma_p)$$

### Trasposizione: Perez 1990 per il diffuso, isotropa per il riflesso 🟢

Le tre componenti sul piano del modulo (POA, *plane of array*):

$$G_{poa} = \underbrace{G_{b,n}\cos\theta\cdot IAM(\theta)}_{\text{diretto}} + \underbrace{D_{sky}\cdot 0{,}95}_{\text{diffuso cielo}} + \underbrace{D_{gnd}\cdot 0{,}97}_{\text{riflesso suolo}}$$

Modello di Perez (`solar.py:407-460`; coefficienti $f_{ij}$ a `:385-394`, presi dalla pubblicazione originale). Con $\theta_z$ in radianti, limitato a 87°, e $\kappa = 1{,}041$:

$$\varepsilon = \frac{(G_{d,h}+G_{b,n})/G_{d,h} + \kappa\theta_z^3}{1+\kappa\theta_z^3}, \qquad \Delta = \frac{G_{d,h}}{E_0\cos\theta_z}, \qquad E_0 = 1367\,[1+0{,}033\cos(360\,n/365)]$$

$$F_1 = \max(0,\ f_{11}+f_{12}\Delta+f_{13}\theta_z), \qquad F_2 = f_{21}+f_{22}\Delta+f_{23}\theta_z$$

$$D_{sky} = G_{d,h}\left[(1-F_1)\frac{1+\cos\beta}{2} + F_1\frac{\max(0,\cos\theta)}{\max(0{,}087,\cos\theta_z)} + F_2\sin\beta\right]$$

$$D_{gnd} = G_h\,\rho\,\frac{1-\cos\beta}{2},\qquad \rho = 0{,}20\ \text{(albedo, \texttt{solar.py:457})}$$

### Perdita per angolo di incidenza (IAM) 🟡

Modello ASHRAE sul solo diretto, con $b_0 = 0{,}05$ (vetro standard; `solar.py:498-528`, `PVSystem.iam_b0`):

$$IAM(\theta) = \max\!\left(0,\ 1 - b_0\left(\frac{1}{\cos\theta}-1\right)\right)$$

Per il diffuso si usano fattori fissi integrati sull'emisfero: 0,95 cielo e 0,97 suolo (Martin & Ruiz 2001, `solar.py:494-495`).

### pvlib o implementazione propria?

**In produzione gira solo l'implementazione propria.**

- Il ramo `_simulate_with_pvlib` (`solar.py:667-790`) esiste ma non viene mai eseguito: `pvlib` non è in `requirements.txt`, quindi `PVLIB_AVAILABLE = False`.
- Il docstring di testa (`solar.py:9-14`), che descrive "pvlib.solarposition" e "Sandia PVLIB", parla di un percorso che non gira.
- L'affermazione "standalone accurate to ±2–3 % vs pvlib" (`solar.py:804`) **non è stata verificata** in questo audit.
- Nota sul ramo pvlib, se mai venisse attivato: usa l'indice orario `HH:00` senza lo spostamento di +0,5 h del ramo standalone. I due rami non sono quindi equivalenti sul timestamp.

---

# PARTE 2 — Dalla radiazione all'energia

## 2.1 Produzione oraria: la catena completa

Per ogni ora, nell'ordine (`solar.py:797-890`):

**1. Temperatura di cella (Faiman)** 🟢 modello · 🔴 coefficienti (`solar.py:566-593`):

$$T_c = T_{2m} + \frac{G_{poa}}{U_0 + U_1\,v_{10m}}$$

Il residenziale usa `mounting="close_roof"`, cioè $U_0 = 29$ W/(m²K) e $U_1 = 0$ (`solar.py:568`, chiamata a `estimate_casa.py:686`).

**2. Potenza DC con correzione termica** 🟢, con $\gamma = -0{,}40$ %/K (`solar.py:596-618`):

$$P_{dc} = P_{STC}\cdot\frac{G_{poa}}{1000}\cdot\max\big(0,\ 1+\gamma(T_c-25)\big)$$

**3. Perdite DC di sistema** 🔴, moltiplicative (`solar.py:178-214`):

$$P_{dc,net} = P_{dc}\prod_i(1-L_i) = P_{dc}\times 0{,}8903$$

**4. Inverter** 🟡, con rapporto DC/AC = 1,15, quindi $P_{inv} = P_{STC}/1{,}15$ (`solar.py:624-661`):

$$r = \frac{P_{dc,net}}{P_{inv}},\quad \eta = \begin{cases}0{,}6\,\eta_{nom} & r<0{,}05\\ \eta_{nom}\,[1-0{,}03(1-\min(r,1))] & r\ge 0{,}05\end{cases},\quad \eta_{nom}=0{,}97$$

$$P_{ac} = \min(P_{dc,net}\,\eta,\ P_{inv}\,\eta_{nom})\cdot(1-0{,}01)$$

Il fattore finale $(1-0{,}01)$ è il cablaggio AC.

## 2.2 Effetto della temperatura

- **Coefficiente $\gamma = -0{,}40$ %/K** (`solar.py:158`). È il valore tipico del policristallino. I moduli monocristallini PERC/TOPCon venduti nel 2026 stanno tra −0,29 e −0,37 %/K, quindi il valore è **leggermente prudente**.
- **Coefficiente termico $U_0 = 29$** per il montaggio aderente al tetto. È invece **ottimistico**: 29 W/m²K è, per quanto ne sappiamo, il default PVsyst per moduli **liberi e ventilati**. Per moduli complanari al tetto si usano tipicamente valori intorno a 20 o meno.
- **Test sullo stesso caso Borzano** (1,5 kWp):

| Coefficiente termico | Produzione | Perdita termica |
|---|---|---|
| $U_0 = 29$ (`close_roof`, attuale) | 1.972 kWh | 185 kWh |
| $U_0 = 20$ (`flat_roof_ballast`) | 1.883 kWh (**−4,5 %**) | 284 kWh |
| $U_0 = 15$ (`integrated_roof`) | 1.788 kWh (−9,3 %) | 392 kWh |

## 2.3 Perdite considerate

| Perdita | Valore | Dove | Etichetta |
|---|---|---|---|
| Sporcamento | 2,0 % | `solar.py:186` | 🔴 tipico di letteratura |
| Ombre vicine (forfait) | 3,0 % | `solar.py:187` | 🔴 forfait: **non** è un modello d'ombra |
| Mismatch | 2,0 % | `solar.py:188` | 🔴 |
| Cablaggio DC | 1,5 % | `solar.py:189` | 🔴 |
| Connessioni | 0,5 % | `solar.py:191` | 🔴 |
| LID (primo anno) | 1,5 % | `solar.py:192` | 🔴 |
| Indisponibilità | 1,0 % | `solar.py:193` | 🔴 |
| **Fattore DC complessivo** | **0,8903** (−11 %) | `solar.py:196-214` | |
| Inverter (nominale e carico parziale) | 0,97 · curva | `solar.py:160`, `:645-653` | 🟡 |
| Cablaggio AC | 1,0 % | `solar.py:190` | 🔴 |
| IAM | modello ASHRAE | §1.3 | 🟡 |
| Temperatura | Faiman | §2.2 | 🟢/🔴 |

Il forfait del 3 % per le ombre vicine è applicato **sempre**, anche quando non c'è nessun ostacolo. Togliendolo, la produzione di Borzano sale da 1.972 a 2.035 kWh (+3,2 %). Per un tetto con ombre vere il 3 % può invece essere molto poco.

## 2.4 Degrado nei 25 anni

`simulate_lifetime` (`energy_balance.py:972-1014`) applica un **degrado lineare dello 0,5 %/anno** a tutte le 8.760 ore, partendo dall'anno 2:

$$E_y = E_1\,[1 - 0{,}005\,(y-1)]$$

- L'anno 25 vale l'88 % dell'anno 1. Per Borzano: 1.972 → 1.735 kWh.
- Il LID dell'1,5 % è già nel primo anno (§2.3).
- 🔴 Lo 0,5 %/anno è un valore di garanzia tipico, non misurato da noi.

## 2.5 kWp installabili e area di tetto

$$kWp_{max} = \mathrm{round}_{0,5}\!\left(\frac{A_{tetto}\cdot u}{5{,}5}\right),\quad u = 0{,}80\ (\text{falda inclinata})$$

(`buildings.py:317-345`)

- Senza Google: $A_{tetto} = 0{,}5 \times$ superficie abitabile (`estimate_casa.py:514`) 🔴.
- Con Google confermato: $A_{tetto}$ = somma delle falde entro ±90° dal sud (`roof_google.py:148-157`).
- In pratica servono 6,9 m² lordi di tetto per kWp. Con moduli da 430 Wp (~1,95 m²) basterebbero circa 4,5 m²/kWp di superficie di modulo. Il margine è ampio: scelta prudente.
- La regola "metà della superficie abitabile" funziona bene per una casa a un piano, ma è **ottimistica per una casa a due piani**. Esempio: 90 m² su due piani → impronta 45 m² → una falda ≈ 26 m² reali, contro i 45 m² assunti.

---

# PARTE 3 — La domanda elettrica

## 3.1 Dal dato utente al profilo orario

Il flusso è a due stadi.

1. **Il totale annuo** $E$ (kWh) arriva dalla bolletta, se l'utente la fornisce (vince sempre), oppure dal proxy (§3.3).
2. **La forma oraria** $s_h$ arriva da `calc_loads` (`engine/loads.py:1075`), con:
   - profilo `residential_working_family` se `presenza_diurna = per_lo_piu_vuota` (default), altrimenti `residential`;
   - illuminazione 8 W/m², fattore di luce diurna 0,15;
   - ACS, ventilazione, riscaldamento e raffrescamento elettrici tutti a zero (`estimate_casa.py:541-570`).

La forma viene poi **riscalata** per far tornare il totale:

$$D_h = E\cdot\frac{s_h}{\sum_{k=1}^{8760}s_k}\qquad(\texttt{\_scale},\ \texttt{estimate\_casa.py:572-574})$$

Il totale grezzo di `calc_loads` (per Borzano 4.077 kWh, a 30 kWh/m² di elettrodomestici) **non conta**: serve solo la forma.

## 3.2 Il profilo `residential_working_family`

- **Struttura** (`engine/loads.py:426-470`): 24 coefficienti per i feriali e 24 per i weekend.
  - Notte alta (0,50–0,55): carichi sempre accesi come frigo, router e standby.
  - Giorno feriale basso (0,32–0,45).
  - Sera media (0,50–0,58).
- **Il picco serale viene dalle luci** (`loads.py:523-532`, stessa forma del profilo `residential`): 0,7–0,8 tra le 18 e le 21.
- **Calendario**: anno di riferimento con 1° gennaio di domenica (`_JAN1_DOW = 6`, `loads.py:49`). Nessuna festività.
- **Stagionalità**: quasi assente. Gli elettrodomestici sono uniformi sui mesi; varia solo l'illuminazione, con le ore di luce. Borzano: domanda mensile tra 110,6 e 123,3 kWh.

**Calibrazione** 🔴. È stata fatta su **un solo caso reale**: la bolletta Borzano, 4 persone, tutto gas, 1.435 kWh/anno.

| | F1 | F2 | F3 |
|---|---|---|---|
| Obiettivo dalla bolletta | 28 % | 33 % | 39 % |
| Dichiarato in `loads.py:437-438` | 26,9 % | 36,1 % | 37,0 % |
| **Ricalcolato in questo audit** | **26,8 %** | **33,9 %** | **39,3 %** |

- Fasce ARERA usate nel ricalcolo: F1 = lun–ven 8–19; F2 = lun–ven 7–8 e 19–23, sabato 7–23; F3 = resto e domenica; senza festività.
- La differenza con i valori dichiarati è probabilmente dovuta a una diversa convenzione per il sabato o per le festività. In ogni caso entrambi i risultati stanno entro ±4 punti dall'obiettivo.
- **Limite serio**: tre percentuali di fascia, da una sola casa a basso consumo, vincolano poco una forma da 48 parametri. Molte forme diverse rispettano le stesse fasce ma producono autoconsumi diversi.

## 3.3 Il proxy di consumo senza bolletta

Formula a strati (`residential/estimate_defaults.py:188-294`):

$$E_{proxy} = \mathrm{round}_{10}\Big[B(n) + 0{,}5A + \mathbb{1}_{AC}\,3{,}0A + \mathbb{1}_{ACS}\,D(n) + \mathbb{1}_{risc}\,33A + \mathbb{1}_{EV}\,2200\Big]$$

con $n$ = occupanti (massimo 6) e $A$ = superficie in m².

| Strato | Valori | Fonte dichiarata | Giudizio |
|---|---|---|---|
| $B(n)$, base per persone | 580 / 830 / 1.030 / 1.180 / 1.310 / 1.420 kWh | "decomposizione" ARERA 2023 (~2.700 kWh medio) + ISTAT 2020 (2.508) | 🔴 numeri **nostri**: la decomposizione non è documentata. Il valore per 4 persone è dichiaratamente allineato a Borzano |
| Superficie | 0,5 kWh/m² | stima interna da EN 15193 | 🔴 "DA RIVALIDARE" (lo dice il codice stesso) |
| Condizionatore | 3,0 kWh/m² (1,5–5,5) | derivato da un aggregato ENEA | 🔴 fonte non primaria, ±50 %, nessuna zona climatica |
| ACS elettrica $D(n)$ | 700 / 1.100 / 1.480 / 1.780 / 2.020 / 2.220 kWh | UNI TS 11300-2 (40 L/persona/giorno, ΔT 35 K, η 0,90) | 🟡 formula normativa; i valori della tabella incorporano economie di scala **non documentate** |
| Riscaldamento con pompa di calore | 33 kWh_e/m² (18–55) | ENEA RAEE 2023: 80–120 kWh_t/m² ÷ SCOP 2,75 | 🟡 benchmark nazionale, nessuna zona climatica |
| Auto elettrica | 2.200 kWh (1.400–3.500) | 12.200 km × 0,17 kWh/km × 1,08 | 🟡 conto trasparente |

**Verifica su Borzano** (4 persone, 90 m², condizionatore): 1.180 + 45 + 270 = 1.495 kWh, contro 1.435 misurati (+4,2 %). È l'**unico** punto di verifica, ed è lo stesso caso su cui la tabella $B(n)$ è stata allineata: **non** è una validazione indipendente.

## 3.4 Lo scenario elettrificazione

Si attiva solo se il riscaldamento attuale **non** è elettrico (`estimate_casa.py:989-1171`). Ci sono due percorsi.

### Percorso A — l'utente fornisce `gas_smc_annui`

**Ripartizione del gas** 🔴:

- con il dato estivo: base estiva $=12\times$ `gas_smc_estivi_mese`; ACS = 60 % della base, cottura = 40 % (`:1007-1010`);
- senza: cottura $= 75 + 15\,\max(0,n-2)$ Smc e ACS $= 45\,n$ Smc, con un tetto al 50 % del totale annuo (`:1020-1028`).

**Conversione in energia elettrica**, con PCI = 9,45 kWh/Smc e $\eta_{caldaia} = 0{,}87$:

$$E_{el,u} = \frac{V_u\cdot PCI\cdot\eta_{caldaia}}{COP_u}$$

| Uso | COP usato |
|---|---|
| Cottura | 1,0 |
| ACS | 2,0 (termosifoni / non so) oppure 2,5 |
| Riscaldamento | SCOP dal TMY (sotto) |

**SCOP dal TMY** 🟡: modello di Carnot con rendimento di secondo principio $\eta_C = 0{,}42$ (`estimate_casa.py:252-280`). $T_{sup}$ è il punto medio della temperatura di mandata del terminale (`energy_vectors.py:60-69`): termosifoni 75 °C, ventilconvettori 42,5 °C, pavimento radiante 37,5 °C.

$$COP(T) = \eta_C\frac{T_{sup}+273{,}15}{T_{sup}-T}\times\begin{cases}0{,}85 & T<5^\circ C\ \text{(sbrinamento)}\\ 1 & \text{altrimenti}\end{cases}$$

$$SCOP = \frac{\sum_h (20-T_h)}{\sum_h (20-T_h)/COP(T_h)},\qquad h:\ -15 < T_h < 20$$

È una media armonica pesata sui gradi-ora. Valori ricalcolati sul TMY di Albinea: **pavimento 3,84 · ventilconvettori 3,37 · termosifoni 1,97**. CLAUDE.md riporta 3,64 / 3,21 / 1,90, probabilmente calcolati sul TMY sintetico dei test.

Giudizio sul modello:

- **$\eta_C = 0{,}42$** è coerente con i dati di targa EN 14511 delle macchine aria-acqua. Esempio: A7/W35 → Carnot 11,0 × 0,42 = 4,6, contro 4,5–5 reali.
- **La temperatura di mandata è fissa.** Manca la curva climatica: a temperature miti le macchine reali abbassano la mandata e il COP sale. Su questo punto il modello è **prudente sul COP**.
- **Lo sbrinamento è un gradino al 15 % a 5 °C**, quindi la curva del COP è discontinua (es. pavimento: 3,40 a 4,9 °C, 4,01 a 5 °C). È un artefatto, non fisica.

### Percorso B — senza dati gas (benchmark)

(`estimate_casa.py:1052-1067`)

- riscaldamento: $E = 33\,A$ kWh_e. **Implicitamente SCOP 2,75, indipendente dai terminali** dichiarati;
- ACS: tabella $D(n)$ dello **scaldabagno elettrico a resistenza** (η ≈ 0,9), non della pompa di calore;
- cottura: non considerata (resta a gas).

### Distribuzione oraria del carico termico 🟡

(`estimate_casa.py:296-312`, `engine/heat_pump.py:158-240`)

$$q_h = E_{risc}\cdot\frac{\max(20-T_h,\,0)}{\sum_k\max(20-T_k,\,0)}$$

ACS e cottura sono distribuite piatte su 8.760 ore. Problemi di questa scelta:

- **Soglia a 20 °C senza temperatura di bilancio.** Non si sottraggono gli apporti solari e interni (tipicamente la soglia reale è 14–16 °C) e non c'è stagione di riscaldamento. Sul TMY di Albinea, il **14,6 % dei gradi-ora cade fuori dalla stagione 15/10–15/4** della zona E, e il **9,8 % tra maggio e settembre**, cioè nei mesi di massimo fotovoltaico.
- **Il commento "SCOP cancels" (`:303-304`) è vero solo con COP costante.** Con COP variabile, il profilo elettrico è ancora più concentrato nelle ore fredde.
- **Direzione dell'errore**: entrambe le approssimazioni spostano carico verso le ore e i mesi con sole, quindi **gonfiano l'autoconsumo dello scenario elettrificazione**.

---

# PARTE 4 — Il bilancio energetico

## 4.1 Il bilancio ora per ora

Per ogni ora $h$ (`energy_balance.py:485-624`), con $P_h$ produzione AC e $D_h$ domanda:

$$sc^{dir}_h = \min(P_h, D_h),\qquad s_h = P_h - D_h$$

Senza batteria:

$$E^{exp}_h = \max(s_h, 0),\qquad E^{imp}_h = \max(-s_h, 0)$$

Gli indicatori annui (`energy_balance.py:686-687`):

$$SCR\ (\text{autoconsumo}) = \frac{\sum_h (sc^{dir}_h + dis_h)}{\sum_h P_h},\qquad SSR\ (\text{autosufficienza}) = \frac{\sum_h (sc^{dir}_h + dis_h)}{\sum_h D_h}$$

**Fuso orario.** La produzione è indicizzata in **ore UTC** (TMY), mentre la domanda è costruita in **ora locale** (le fasce ARERA sono in ora locale). Le due serie vengono accoppiate senza conversione: il fotovoltaico risulta anticipato di 1 ora d'inverno e 2 d'estate rispetto ai carichi. Allineando correttamente:

| Allineamento | SCR | SSR |
|---|---|---|
| Attuale (nessuno spostamento) | 23,7 % | 32,6 % |
| FV spostato di +1 h | 24,1 % | 33,1 % |
| FV spostato di +2 h | 25,2 % | 34,7 % |

**Direzione: prudente** (sottostima l'autoconsumo di 0,5–2 punti). Resta comunque un difetto da correggere.

Altri limiti del bilancio orario:

- **Risoluzione oraria**: si perdono i picchi sub-orari (bollitore, forno, lavatrice). Il bilancio orario **sovrastima** l'autoconsumo reale: è un effetto noto in letteratura, non quantificato qui.
- **Nessun limite di immissione** nel residenziale: `export_limit_pct = 100` (default).

## 4.2 La batteria

Parametri (`BatterySystem`, `energy_balance.py:96-105`) 🔴:

| Parametro | Valore |
|---|---|
| Rendimento di ciclo AC-AC | 0,93 → $\eta_c = \eta_d = \sqrt{0{,}93} = 0{,}964$ |
| Stato di carica utile | tra 5 % e 90 % → finestra utile **85 %**. Il docstring di `battery_scenario.py` dice "DoD 80 %": è incoerente |
| Potenza massima | carica C/2, scarica 1C |
| Stato di carica iniziale | 50 % |

**Strategia "autoconsumo"** (`energy_balance.py:340-411`), senza carica da rete e senza arbitraggio:

- **in carica**: $\Delta SoC = \min(s_h,\ P_{ch}/\eta_c,\ (SoC_{max}-SoC)/\eta_c)\cdot\eta_c$;
- **in scarica**: $out = \min(SoC - SoC_{min},\ P_{dis},\ |s_h|/\eta_d)\cdot\eta_d$.

**Degrado** 🔴 (`energy_balance.py:414-447`), con $t$ = anni trascorsi e $N$ = cicli/anno:

$$C(t) = C_0\,(1 - 0{,}02\,t)\,(1 - 0{,}00003\,N\,t)$$

- **Criterio di sostituzione**: la capacità scende sotto l'80 % (`find_battery_replacement_years`, `:877-925`).
  - Con $N = 230$ (`battery_scenario.py:108`) la sostituzione cade all'**anno 9**: $(1-0{,}16)(1-0{,}0552) = 0{,}794$.
  - Il docstring (`battery_scenario.py:19-22`) dice "anno 12-14": è **sbagliato**.
  - Nella simulazione energetica il degrado usa invece $N = 250$ (default di `_battery_aged_capacity`, `:417`): incoerenza minore.
- **Errore di modello: la capacità non si azzera dopo la sostituzione.** Il costo della sostituzione è contabilizzato all'anno 9, ma l'energia degli anni 10–25 è simulata con una batteria che continua a invecchiare: 76 % all'anno 10, **43 % all'anno 25**.
- **Una sola sostituzione.** Dopo la prima, la logica `was_above` impedisce di trovarne altre, mentre con questo modello di degrado ne servirebbe una seconda verso l'anno 17–18.
- **Effetto netto**:
  - beneficio energetico della batteria **sottostimato** dopo l'anno 9 (pessimista);
  - costo della seconda sostituzione **assente** (ottimista).
  - Il saldo non è quantificato.

**Taglia della batteria** 🔴 (`estimate_casa.py:133-137`): 3 kWh fino a 2.000 kWh/anno, 5 kWh fino a 4.000, poi 7 kWh. Costo 550 €/kWh, sostituzione a 380 €/kWh.

## 4.3 Come nasce l'autosufficienza

L'autosufficienza (SSR) è la quota della domanda coperta dal fotovoltaico, direttamente o tramite la batteria. Per Borzano con 1,5 kWp:

- il fotovoltaico produce il **137 %** del consumo annuo;
- ne copre solo il **32,6 %**, perché produce di giorno e d'estate, mentre la famiglia consuma la sera e in modo uniforme sui mesi (vedi i grafici 1 e 2).

---

# PARTE 5 — L'economia

## 5.1 Costo dell'impianto

(`api/utils/cost.py:11-36`)

$$C(k) = \mathrm{round}_{100}\Big[\sum_{\text{scaglioni}} \Delta k_i\cdot c_i + \max(800,\ 18k) + 1500\Big]$$

| Scaglione kWp | 0–3 | 3–6 | 6–20 | 20–50 | 50–100 | 100–250 | >250 |
|---|---|---|---|---|---|---|---|
| €/kWp | 2.100 | 1.800 | 1.500 | 1.250 | 1.050 | 900 | 800 |

| kWp | Scaglioni | Progetto e pratiche | Costi fissi | **Totale** | €/kWp |
|---|---|---|---|---|---|
| 1,5 | 3.150 | 800 | 1.500 | **5.400** | 3.600 |
| 3,0 | 6.300 | 800 | 1.500 | **8.600** | 2.867 |
| 4,0 | 8.100 | 800 | 1.500 | **10.400** | 2.600 |
| 6,0 | 11.700 | 800 | 1.500 | **14.000** | 2.333 |

Giudizio 🔴:

- **Fonte**: "listini installatori 2026 (media Italia centro-nord)" per il fisso, nessuna fonte per gli scaglioni. **Mai confrontato con preventivi reali.**
- **Il fisso effettivo è 2.300 €, non 1.500.** Gli 800 € minimi di "progetto e pratiche" si sommano ai 1.500. Il testo delle assunzioni dice "include €1.500 di costi fissi", quindi sottodichiara.
- **Possibile doppio conteggio dell'inverter.** La voce fissa nomina l'inverter, ma un prezzo €/kWp "chiavi in mano" lo include normalmente.
- **IVA** (10 % per il residenziale): non è detto se sia inclusa.
- **Direzione probabile: prudente** (costi alti), ma non verificata.

## 5.2 La detrazione fiscale

(`residential/incentives.py:143-244`; uso in `battery_scenario.py:266-294`)

$$\text{Detr} = a\cdot\min(capex,\ 96.000),\qquad T_y = \frac{\text{Detr}}{10}\ \ \text{per}\ y = 1..10$$

con $a$ = 50 % per l'abitazione principale, 36 % negli altri casi.

- **Tempistica**: la prima quota è all'anno 1. Nella realtà si recupera nella dichiarazione dei redditi dell'anno successivo, quindi c'è un leggero anticipo: ottimista di circa un anno di sconto sulle quote.
- **Capienza IRPEF**: `calc_irpef_deduction` calcola `detrazione_effettiva` (quote limitate all'IRPEF annua), ma al motore passa `detrazione_totale / capex` (`battery_scenario.py:273-276`). **L'NPV ignora la capienza.** Verifica su Borzano con `irpef_annua_stimata = 100 €`: detrazione effettiva 1.000 € contro 2.700 € totali, **NPV identico** (−112,31 €). La descrizione del campo nello schema (`api/schemas/estimate_casa.py:97-103`) lascia intendere il contrario.
- **Norma** (da verificare con un fiscalista):
  - il codice cita la L. 207/2024 per il 50 % sulle spese 2026, ma a nostra conoscenza quella legge prevedeva il 36 % dal 2026; il 50 % per la prima casa nel 2026 dovrebbe venire dalla legge di bilancio 2026;
  - il prezzo 0,08 €/kWh è descritto come "GSE scambio sul posto 2026" (`estimate_casa.py:957`), ma lo scambio sul posto non dovrebbe più essere attivabile per i nuovi impianti dal 2025: il regime per un impianto nuovo è il ritiro dedicato.

## 5.3 NPV

Flusso dell'anno $y$ (`engine/economics.py:810-870`), con $e = 2{,}5\,\%$ di escalation e $r = 5\,\%$ di sconto (`battery_scenario.py:114-121`):

$$CF_y = \underbrace{(D\,p_{imp} - E^{imp}_y\,p_{imp})(1+e)^{y-1}}_{\text{risparmio in bolletta}} + \underbrace{E^{exp}_y\,p_{exp}\,(1+e)^{y-1}}_{\text{vendita alla rete}} + T_y - \underbrace{0{,}01\,capex\,(1{,}02)^{y-1}}_{O\&M} - R_y$$

$$NPV = -capex + \sum_{y=1}^{25}\frac{CF_y}{(1+r)^y}\qquad(\texttt{economics.py:893})$$

- **Tasso di sconto 5 % nominale** 🔴. Non è documentato perché: per una famiglia è un costo opportunità alto rispetto a un deposito, ragionevole rispetto a un investimento rischioso. Sensibilità: 3 % → NPV +943 €; 7 % → −906 €.
- **Escalation 2,5 % nominale applicata anche al prezzo di cessione** (`economics.py:822-823`) 🔴. Il prezzo zonale nelle ore solari tende piuttosto a **calare** con la crescita del fotovoltaico. Per Borzano questa sola scelta vale **+436 €** di NPV a 1,5 kWp, **+1.007 €** a 3 kWp e **+1.392 €** a 4 kWp: abbastanza per rendere positivo l'NPV delle taglie grandi.
- **Prezzo di acquisto 0,25 €/kWh** 🔴: "tariffa media 2026, ARERA", senza delibera citata. Il risparmio va valorizzato alla **sola componente variabile** della bolletta (energia, oneri variabili, accise e IVA). Se 0,25 è un prezzo medio "tutto incluso", con le quote fisse, il risparmio è sovrastimato.
- **O&M 1 %/anno del capex, +2 %/anno.** Calcolato sul capex che include i fissi: per 1,5 kWp sono 54 €/anno, il 23 % dei benefici energetici del primo anno.
- **Sostituzione dell'inverter: assente** (`OperatingCost.inverter_replacement_cost_eur = 0`, `economics.py:141`) 🔴 **ottimista**. Un inverter residenziale dura in genere 10–15 anni. Con 900 € all'anno 13: NPV 1,5 kWp −112 → **−590 €**; 3 kWp +206 → **−271 €**; 4 kWp +641 → **+164 €**.

## 5.4 Payback

È un payback **semplice ma completo** (`economics.py:900-912`): il primo anno in cui il flusso cumulato **non scontato** diventa ≥ 0, con interpolazione lineare dentro l'anno.

$$PB = (y^*-1) + \frac{-\,CC_{y^*-1}}{CF_{y^*}},\qquad CC_y = -capex + \sum_{k\le y} CF_k$$

Perché differisce da $capex/\text{risparmio}$:

1. include la detrazione, ma **solo per 10 anni**;
2. include O&M, degrado ed escalation;
3. non attualizza i flussi.

Per Borzano a 1,5 kWp: $5.400/453 = 11{,}9$ anni (flusso del primo anno), mentre il payback vero è **12,9 anni**. Il salto avviene perché il flusso annuo scende da 492 € a 227 € quando finisce la detrazione, all'anno 11.

Il **payback attualizzato** è calcolato (`:914-924`) ma **non è mostrato** all'utente. Per Borzano è **infinito**: con NPV < 0 l'investimento non rientra mai al 5 %. Mostrare "rientro in ~13 anni" accanto a "NPV negativo" può confondere chi legge.

## 5.5 Tariffe e parametri

| Parametro | Valore | Dove | Fonte dichiarata |
|---|---|---|---|
| Prezzo di acquisto | 0,25 €/kWh | `battery_scenario.py:114` | "ARERA tariffa media 2026" (nessuna delibera) |
| Prezzo di cessione | 0,08 €/kWh | `battery_scenario.py:115` | "GSE scambio sul posto (stima)" |
| Escalation (acquisto **e** cessione) | 2,5 %/anno | `battery_scenario.py:120` | nessuna |
| Tasso di sconto | 5 % | `battery_scenario.py:119` | nessuna |
| Orizzonte | 25 anni | `battery_scenario.py:118` | garanzia moduli (implicita) |
| O&M | 1 % capex, +2 %/anno | `battery_scenario.py:121`, `economics.py:144` | nessuna |
| CO₂ di rete | 0,233 kg/kWh | `energy_vectors.py:415` | ENTSO-E 2023 (il router dice "2024": incoerenza) |

---

# PARTE 6 — Dimensionamento e verdetto

## 6.1 Come si sceglie la taglia

1. **Candidati** (`api/utils/cost.py:94-106`), con resa fissa $Y = 1.200$ kWh/kWp 🔴 (non quella del sito) e $E$ = consumo annuo:
   $$k_{min} = \max(1{,}5,\ \mathrm{round}_{0,5}(0{,}35E/Y)),\quad k_{rec} = \min(k_{max},\max(k_{min},\mathrm{round}_{0,5}(0{,}75E/Y))),\quad k_{max} = \text{tetto}$$
   Se restano meno di 3 valori distinti si aggiunge il punto medio (`estimate_casa.py:672-678`).
2. Ogni candidato viene **simulato davvero**: produzione, bilancio e NPV senza batteria.
3. **Tetto di produzione** 🔴: si tengono solo i candidati con produzione ≤ 150 % del consumo (`:146`, `:705`).
4. **Il migliore per NPV** tra quelli rimasti (`:709-712`). Se nessun candidato rispetta il tetto, si prende il più piccolo.
5. Se tutti i candidati ammessi hanno NPV ≤ 0, il verdetto è forzato a "negativo", con un testo dedicato (`:827-859`).

Il tetto al 150 % è una **regola di prudenza nostra**. Il codice la motiva bene: oltre quella soglia l'impianto vive di cessione alla rete e di detrazione. Non deriva da una norma né da un dato.

## 6.2 La falda, quando ci sono i dati Google — è un'euristica

(`residential/roof_google.py:120-145`)

$$score = A\cdot\underbrace{\tfrac12\big(1+\cos(\gamma-180^\circ)\big)}_{f_{az}}\cdot\underbrace{\max\!\left(0,\ 1-\tfrac{|\beta-30^\circ|}{90^\circ}\right)}_{f_{tilt}}$$

- Serve **solo per scegliere** la falda. La produzione si calcola poi con la simulazione vera.
- Le sue proporzioni **non sono fisiche**:

| Orientamento | Euristica | Rapporto reale sul TMY di Albinea (30°) |
|---|---|---|
| Est / Ovest | 0,50 | 0,78 / 0,81 |
| Nord | 0,00 | 0,56 |

- Una grande falda est può quindi perdere contro una piccola falda sud anche quando la seconda non basta a contenere l'impianto.
- **Incoerenza**: l'area usata per $k_{max}$ somma **tutte** le falde entro ±90° dal sud, ma l'intero impianto viene simulato con l'orientamento della falda migliore. Con più falde utili il modello è ottimista. Nel caso Borzano c'è una sola falda utile, quindi nessun effetto.

## 6.3 Le soglie del verdetto

(`estimate_casa.py:344-447`)

| Etichetta | Condizione |
|---|---|
| ottimo | payback ≤ 8 e NPV > 0 |
| buono | payback ≤ 12 e NPV > 0 |
| accettabile | payback < 20 e NPV > 0 |
| negativo | altrimenti |

**Declassamento**: se l'autosufficienza è < 35 % e il grado è almeno "buono", si scende di un gradino.

**"Marginale"**: se NPV > 0 ma NPV < 10 % del capex, l'etichetta diventa "marginale".

| Soglia | Origine | Giudizio |
|---|---|---|
| Payback 8 / 12 / 20 anni | non documentata | 🔴 arbitraria. Il 20 sta sotto l'orizzonte di 25 anni, il resto non è motivato |
| Autosufficienza 35 % | non documentata | 🔴 arbitraria. Ragione dichiarata: "dipende da detrazione ed export" |
| NPV/capex 10 % | "≈ 0,4 % nominale annuo, sotto l'inflazione" (`:376-378`) | 🔴 **giustificazione errata**: vedi sotto |
| Produzione ≤ 150 % | §6.1 | 🔴 motivata ma non calibrata |

**Perché la giustificazione di "marginale" è sbagliata.** L'NPV è già calcolato **al netto** di un rendimento del 5 %. NPV/capex = 10 % significa guadagnare *oltre* il 5 % annuo, non lo 0,4 % in totale. Verifica sul caso Borzano:

| Taglia | NPV/capex | IRR |
|---|---|---|
| 3 kWp | 2,4 % | 5,27 % |
| 4 kWp | 6,2 % | 5,70 % |

La soglia del 10 % corrisponde quindi a un IRR di circa il 6 %, cioè circa 1 punto sopra il tasso di sconto. L'etichetta "marginale" resta difendibile (margine sottile sopra il rendimento richiesto), ma va corretta la spiegazione nel codice e in CLAUDE.md.

---

# PARTE 7 — L'incertezza

## 7.1 Come nascono i range

- **Variabile su cui si fa lo sweep**: solo la **domanda annua**, in tre valori (basso / centrale / alto).
- **kWp fisso**: produzione e capex restano identici nei tre run (`estimate_casa.py:750-752`). Scelta coerente con il flusso scuole: l'impianto è la decisione, la casa è l'incertezza.
- **Range mostrati**: risparmio annuo e payback, come **min/max per metrica** sui tre run (`:764-776`).

## 7.2 Perché i range non sono scenari coerenti

- **Ogni metrica prende il suo min e il suo max indipendentemente**: il "basso" del risparmio e il "basso" del payback potrebbero venire da run diversi.
- **Oggi, di fatto, sono coerenti**: con solo fotovoltaico e $p_{imp} > p_{exp}$, il risparmio cresce con la domanda e il payback cala, quindi gli estremi vengono dagli stessi run. Ma è una proprietà emergente, non garantita né dichiarata. Con la batteria, o con tariffe a fasce, potrebbe non valere più.
- **Il verdetto, l'NPV e la taglia sono calcolati solo sul caso centrale.** L'utente vede un range sul payback, ma un'etichetta secca.

## 7.3 Le bande dichiarate

| Banda | Dove | Origine |
|---|---|---|
| ±15 % con bolletta | `estimate_casa.py:494-495` | 🔴 **scelta**. Una bolletta annua è una misura: l'incertezza vera riguarda il consumo *futuro* (variazione tra anni, cambi di abitudini), non il dato. Nessuna misura dietro il 15 % |
| ±30 % con il proxy | `estimate_defaults.py:146` | 🔴 **scelta**. Nessuna validazione statistica: il proxy è stato provato su un solo caso |
| ±45 % con riscaldamento elettrico | `estimate_defaults.py:147` | 🔴 **scelta**. Ragionamento qualitativo (manca la zona climatica, manca l'involucro) |
| ±50 % elettrificazione senza gas | `estimate_casa.py:1120` | 🔴 **scelta**, solo testo: **non** produce un range numerico |

## 7.4 Cosa NON è nell'incertezza

Il range mostrato all'utente **non copre**:

1. **Ombre**: vicine (oltre il forfait del 3 %) e lontane (profilo d'orizzonte vuoto, §1.1). Su un tetto con un albero o un camino a sud l'errore può superare il −20 %.
2. **Variabilità climatica tra anni**: il TMY è un anno medio (ordine di grandezza ±5 %/anno).
3. **Database e modello solare**: SARAH2 contro SARAH3, standalone contro pvlib, coefficiente termico (−4,5 % se si usa 20 invece di 29), parametri di perdita.
4. **Qualità dei componenti e dell'installazione**: γ del modulo, rendimento reale dell'inverter, degrado effettivo (0,5 %/anno assunto), guasti.
5. **Forma oraria della domanda**: il profilo è calibrato su un solo caso. Abitudini diverse (lavatrice di giorno, smart working) spostano l'autoconsumo di molti punti.
6. **Prezzi futuri**: acquisto, cessione, escalation. Per Borzano un solo parametro sposta l'NPV di ±1.000 € (§grafico 5).
7. **Norme**: aliquota della detrazione (50 % → 36 % = −584 € di NPV a 1,5 kWp), regime di cessione (fine dello scambio sul posto), eventuali oneri futuri sull'autoconsumo.
8. **Capienza IRPEF**: ignorata nell'NPV (§5.2).
9. **Costi**: scaglioni non validati, sostituzione dell'inverter assente, IVA non chiarita.
10. **Fuso orario e risoluzione oraria**: ±1–2 punti di autoconsumo.
11. **Comportamento reale**: manutenzione, pulizia, monitoraggio, tempi di allaccio.

---

# Tabella delle costanti

Legenda colonna "verificata":

- **A** = fonte autorevole primaria (norma, ente);
- **L** = valore tipico di letteratura o datasheet, non controllato su dati nostri;
- **N** = scelta nostra / stima interna;
- **✗** = documentazione in conflitto con il codice.

Colonna "se sbagliata": in quale direzione il valore attuale sposta il risultato, rispetto a ciò che si suppone sia vero.

### Sole e produzione

| Costante | Valore | Dove | Fonte dichiarata | Verificata? | Se sbagliata |
|---|---|---|---|---|---|
| Endpoint PVGIS | v5_2 / TMY | `pvgis_client.py:47` | JRC | A | — |
| Database irraggiamento | SARAH2 | risposta API | doc dice SARAH3 | **✗** | non quantificato |
| Profilo d'orizzonte | vuoto | `pvgis_client.py:300-310` | `usehorizon=1` | **✗** (non arriva) | ottimista in valle o collina |
| Declinazione | Cooper 23,45° | `solar.py:305-310` | "Spencer" | ✗ etichetta | trascurabile |
| Equazione del tempo | Spencer | `solar.py:313-322` | Spencer 1971 | A | — |
| Albedo ρ | 0,20 | `solar.py:457` | standard | L | ±1 % |
| Coefficienti Perez | tabella | `solar.py:385-394` | Perez 1990 | A | — |
| κ Perez | 1,041 | `solar.py:434` | Perez 1990 | A | — |
| IAM $b_0$ | 0,05 | `solar.py:164` | ASHRAE / IEC 61853-2 | L | ±1 % |
| IAM diffuso cielo / suolo | 0,95 / 0,97 | `solar.py:494-495` | Martin & Ruiz 2001 | L | <1 % |
| Faiman `close_roof` | $U_0 = 29$, $U_1 = 0$ | `solar.py:568` | "Faiman 2008" | L, **probabilmente fuori posto** | **ottimista ~4,5 %** |
| γ temperatura | −0,40 %/K | `solar.py:158` | "mono −0,35, poly −0,40" | L | prudente ~1–2 % |
| Efficienza modulo | 0,20 | `solar.py:157` | — | L | non usato nel residenziale |
| η inverter nominale | 0,97 | `solar.py:160` | tipico | L | ±1 % |
| Curva a carico parziale | −3 %, 0,6 sotto il 5 % | `solar.py:645-653` | "EN 50530 semplificata" | N | ±1 % |
| Rapporto DC/AC | 1,15 | `solar.py:161` | tipico | L | clipping ≈ 0 |
| Sporcamento | 2 % | `solar.py:186` | IEC 61724 (generico) | L | ±2 % |
| Ombre vicine (forfait) | 3 % | `solar.py:187` | — | N | prudente senza ombre, molto ottimista con ombre |
| Mismatch | 2 % | `solar.py:188` | — | L | ±1 % |
| Cablaggio DC / AC | 1,5 % / 1 % | `solar.py:189-190` | — | L | ±1 % |
| Connessioni | 0,5 % | `solar.py:191` | — | L | — |
| LID | 1,5 % | `solar.py:192` | — | L | prudente con N-type |
| Indisponibilità | 1 % | `solar.py:193` | — | L | ±1 % |
| Degrado | 0,5 %/anno | `energy_balance.py:975` | garanzie | L | ±2 % di NPV |
| Inclinazione di default | 30° | `estimate_casa.py:515` | "falda tipica" | N | ±3 % |
| Azimut `non_so` | 225° | `estimate_casa.py:161` | "prudente" | N | prudente se il tetto è a sud |
| Azimut `est_o_ovest` | 270° | `estimate_casa.py:159` | "E = O" | N, **falso**: E ≈ −4 % rispetto a O | ottimista per le falde a est |
| Area tetto | 0,5 × m² abitabili | `estimate_casa.py:514` | "falda inclinata" | N | ottimista con 2 piani |
| Utilizzo falda | 0,80 | `buildings.py:337` | — | N | prudente |
| m² per kWp | 5,5 | `buildings.py:342` | "~400 Wp + spazi" | L | prudente |
| Resa per candidati | 1.200 kWh/kWp | `cost.py:98` | — | N | sceglie solo i candidati |
| Fattori candidati | 0,35 / 0,75 / floor 1,5 | `cost.py:97-103` | — | N | — |

### Domanda

| Costante | Valore | Dove | Fonte dichiarata | Verificata? | Se sbagliata |
|---|---|---|---|---|---|
| Banda con bolletta | ±15 % | `estimate_casa.py:494-495` | — | N | solo ampiezza del range |
| $B(n)$ base per persone | 580…1.420 | `estimate_defaults.py:94-101` | ARERA / ISTAT "decomposti" | N (1 caso) | domanda più alta ⇒ verdetto più ottimista |
| Quota superficie | 0,5 kWh/m² | `:111` | EN 15193 interna | N | piccola |
| Condizionatore | 1,5 / 3,0 / 5,5 kWh/m² | `:116-118` | aggregato ENEA | N (non primaria) | ±50 % |
| Auto elettrica | 1.400 / 2.200 / 3.500 kWh | `:121-123` | ISTAT + WLTP | L | — |
| ACS elettrica $D(n)$ | 700…2.220 | `:127-134` | UNI TS 11300-2 | A (formula), N (scala) | — |
| Riscaldamento con pompa di calore | 18 / 33 / 55 kWh/m² | `:140-142` | ENEA RAEE 2023 | A/L, nessuna zona | ±45 % |
| Margine proxy | ±30 % / ±45 % | `:146-147` | — | N | — |
| Profilo working_family | 48 coefficienti | `loads.py:426-470` | 1 bolletta Borzano | N (1 caso) | forma ⇒ ±autoconsumo |
| Luci | 8 W/m², luce diurna 0,15 | `estimate_casa.py:559-563` | — | N | solo forma |
| Elettrodomestici | 30 kWh/m² | `loads.py:878` | UNI TS 11300-1 | — | solo forma (riscalato) |
| 1° gennaio | domenica (2023) | `loads.py:49` | — | N | trascurabile |
| Stagionalità domanda | ~nulla | `loads.py:868-891` | — | N | lieve ottimismo in estate |

### Elettrificazione

| Costante | Valore | Dove | Fonte dichiarata | Verificata? | Se sbagliata |
|---|---|---|---|---|---|
| PCI gas | 9,45 kWh/Smc | `estimate_casa.py:167` | DM 26/06/1993 | A (PCI convenzionale ≈ 9,5 kWh/Smc; il PCS ≈ 10,6 non va usato qui) | ±1 % |
| η caldaia | 0,87 | `:172` | EN 15316-4-1 | L | ±10 % sul fabbisogno |
| η Carnot | 0,42 | `:196` | "EN 14825 A++" | L, plausibile | ±10 % sullo SCOP |
| Penalità sbrinamento | ×0,85 sotto 5 °C | `:272-273` | — | N (gradino) | artefatto |
| Setpoint / T minima | 20 °C / −15 °C | `:199`, `:202` | EN 15316-4-2 | L | profilo troppo esteso |
| Mandata terminali | 37,5 / 42,5 / 75 °C | `energy_vectors.py:60-69` | — | L | termosifoni a 75 °C senza curva climatica: pessimista sullo SCOP |
| SCOP prudente / benchmark | 2,5 / 2,75 | `:178`, `:193` | ENEA RAEE | L | — |
| COP ACS | 2,0 / 2,5 | `:183-184` | — | N | — |
| COP cottura | 1,0 (dopo η 0,87!) | `:189` | — | N, **incoerente** | consumo +60 % ⇒ FV più conveniente |
| Cottura gas | 75 + 15/persona Smc | `:208-209` | ARERA / ISTAT | N | — |
| ACS gas | 45 Smc/persona | `:210` | UNI TS 11300-2 | L | — |
| Quota ACS sulla base estiva | 60 % | `:214` | ISTAT | N | — |
| Tetto cottura + ACS | 50 % del gas annuo | `:1023` | — | N | — |
| Soglia gradi-ora | 20 °C, tutto l'anno | `heat_pump.py:60` | — | N | ottimista sull'autoconsumo |

### Batteria e bilancio

| Costante | Valore | Dove | Fonte dichiarata | Verificata? | Se sbagliata |
|---|---|---|---|---|---|
| Rendimento di ciclo | 0,93 | `energy_balance.py:99` | LFP 0,92–0,95 | L | — |
| SoC max / min | 0,90 / 0,05 | `:100-101` | — | L (doc dice "DoD 80 %": ✗) | — |
| Potenza carica / scarica | C/2 / 1C | `:97-98` | — | L | — |
| Degrado calendario | 2 %/anno | `:103` | LFP 1,5–2,5 | L, pessimista per LFP moderne | sostituzione anticipata |
| Degrado per ciclo | 0,003 %/ciclo | `:104` | "≈ 3.000 cicli" | L | — |
| Cicli/anno | 230 (sostituzione) / 250 (simulazione) | `battery_scenario.py:108`, `energy_balance.py:417` | "stima interna TERNA" | N, **incoerente** | — |
| Soglia sostituzione | 80 % | `battery_scenario.py:111` | IRENA | L | — |
| Costo batteria / sostituzione | 550 / 380 €/kWh | `:102`, `:105` | ENEA RAEE 2023 / IEA 2024 | L | — |
| Taglie batteria | 3 / 5 / 7 kWh | `estimate_casa.py:133-137` | — | N | — |

### Economia e verdetto

| Costante | Valore | Dove | Fonte dichiarata | Verificata? | Se sbagliata |
|---|---|---|---|---|---|
| Scaglioni €/kWp | 2.100…800 | `cost.py:19-23` | — | **N, mai confrontati con preventivi** | probabilmente prudente |
| Progetto e pratiche | max(800, 18·kWp) | `cost.py:34` | — | N | prudente (si somma al fisso) |
| Fisso residenziale | 1.500 € | `cost.py:14` | "listini 2026" | N | prudente |
| Prezzo di acquisto | 0,25 €/kWh | `battery_scenario.py:114` | "ARERA" | N (nessuna delibera) | ottimista se include quote fisse |
| Prezzo di cessione | 0,08 €/kWh | `:115` | "GSE scambio sul posto" | **✗** (regime probabilmente sbagliato) | ±500 € NPV ogni 0,02 €/kWh |
| Escalation | 2,5 %/anno su acquisto **e** cessione | `:120`, `economics.py:821-823` | — | N | **ottimista** (+436…+1.392 € NPV solo dalla cessione) |
| Tasso di sconto | 5 % | `:119` | — | N | ±900 € per ±2 punti |
| Orizzonte | 25 anni | `:118` | — | L | — |
| O&M | 1 %/anno, +2 %/anno | `:121`, `economics.py:144` | — | L | prudente per i piccoli impianti |
| Sostituzione inverter | 0 € | `economics.py:141` | — | **N, assente** | **ottimista** (−480 € NPV per 900 € all'anno 13) |
| Aliquota detrazione | 50 % / 36 % | `incentives.py:86-87` | L. 207/2024 | A (riferimento da verificare) | −584 € NPV se 36 % |
| Tetto di spesa / quote | 96.000 € / 10 | `incentives.py:83`, `:90` | art. 16-bis TUIR | A | — |
| Capienza nell'NPV | ignorata | `battery_scenario.py:273-276` | — | **✗ bug** | ottimista per redditi bassi |
| CO₂ di rete | 0,233 kg/kWh | `energy_vectors.py:415` | ENTSO-E 2023/2024 | L, ✗ anno | — |
| Tetto produzione/consumo | 150 % | `estimate_casa.py:146` | — | N | — |
| Payback 8 / 12 / 20 | | `:361-368` | — | N | — |
| Soglia autosufficienza | 35 % | `:140` | — | N | — |
| Soglia "marginale" | NPV/capex 10 % | `:379` | "0,4 %/anno" | N, **motivazione errata** | — |
| Euristica falda | $f_{az}\cdot f_{tilt}$ | `roof_google.py:120-129` | — | N | solo ranking |
| Falde utili | ±90° dal sud | `roof_google.py:154` | — | N | ottimista con più falde |

---

# Esempio numerico tracciato — Borzano

**Input:**

- comune Albinea (RE), zona E;
- bolletta **1.435 kWh/anno**, 4 persone, 90 m²;
- gas per riscaldamento, ACS e cottura; termosifoni;
- famiglia fuori casa di giorno;
- tetto Google confermato: falda migliore 22,6° / az. 166,8°, 27,1 m²; seconda falda a nord, 25,1 m², esclusa;
- abitazione principale; nessun dato su gas o IRPEF.

**Passo 0 — tetto e taglie.**

- Punteggi delle falde: 27,1 × 0,906 = **24,5** (sud-sud-est) contro 25,1 × 0,023 = 0,6 (nord).
- Area utile 27,1 m² → $27{,}1\cdot0{,}8/5{,}5 = 3{,}94$ → **$k_{max} = 4{,}0$ kWp**.
- Candidati: min = rec = 1,5 (floor); max = 4,0; si aggiunge il punto medio 3,0. **Candidati: 1,5 / 3,0 / 4,0 kWp.**

**Passo 1 — irraggiamento sul piano (per m²).**

| | kWh/m²·anno | Δ |
|---|---|---|
| GHI orizzontale | 1.523,0 | |
| POA, Perez, prima dello IAM | **1.733,8** | +13,8 % |
| POA dopo IAM | 1.679,1 | −3,2 % |

**Passo 2 — produzione lorda e perdite, per la taglia scelta (1,5 kWp).**

| Voce | kWh | Cumulato |
|---|---|---|
| Lorda DC ($1{,}5\times1.733{,}8$) | 2.600,7 | 2.600,7 |
| − orizzonte | 0,7 | 2.600,0 |
| − IAM | 81,3 | 2.518,7 |
| − temperatura (T cella media luglio 43,9 °C) | 184,7 | 2.334,0 |
| − perdite DC di sistema (×0,8903) | 257,0 | 2.077,0 |
| − inverter | 94,6 | 1.982,4 |
| − clipping | 0,0 | 1.982,4 |
| − cablaggio AC | 19,9 | 1.962,5 |
| **Netta AC (dal motore)** | **1.972,2** | |

- La tabella delle perdite di `_aggregate` non chiude per circa 10 kWh (0,5 %). Il disallineamento è nel conteggio interno, non nella produzione.
- Resa specifica **1.314,8 kWh/kWp**.
- PR = **0,783** sul POA *dopo* IAM. Con la definizione IEC 61724 (POA prima dello IAM) sarebbe 0,758.

**Passo 3 — tetto di produzione.**

| Taglia | Produzione / consumo | Esito |
|---|---|---|
| 1,5 kWp | 1.972 / 1.435 = **137 %** | ammessa |
| 3,0 kWp | 3.944 kWh = 275 % | esclusa |
| 4,0 kWp | 5.259 kWh = 366 % | esclusa |

→ **kWp = 1,5**.

**Passo 4 — confronto orario con la domanda (anno 1, senza batteria).**

| Voce | kWh | Nota |
|---|---|---|
| Produzione | 1.972,0 | |
| Domanda | 1.435,1 | |
| Autoconsumo | **467,9** | SCR = **23,7 %** |
| Immissione in rete | **1.504,4** | 76 % della produzione |
| Prelievo dalla rete | 967,1 | |
| Autosufficienza | **32,6 %** | sotto la soglia del 35 % |

**Passo 5 — ricavi e risparmi (anno 1).**

- Bolletta senza FV: 1.435,1 × 0,25 = 358,77 €. Con FV: 967,1 × 0,25 = 241,78 €.
- **Risparmio in bolletta: 116,99 €.**
- **Vendita alla rete: 1.504,4 × 0,08 = 120,35 €.**
- O&M: 1 % × 5.400 = **54,00 €**.

**Passo 6 — detrazione.**

- Capex **5.400 €** = 3.150 (scaglioni) + 800 (pratiche) + 1.500 (fisso), arrotondato.
- Detrazione 50 % × 5.400 = **2.700 €** → **270 €/anno** per 10 anni.

**Passo 7 — flussi di cassa.**

- **Anno 1**: $CF_1 = 116{,}99 + 120{,}35 + 270 - 54 = \mathbf{453{,}34}$ €.
- **Anno 10**: 492,37 €.
- **Anno 11**: 227,11 € (finisce la detrazione).
- **Anno 25**: 305,53 € (produzione al 88 %, prezzi +80 %).

**Passo 8 — NPV, IRR, payback.**

| Indicatore | Valore |
|---|---|
| NPV (5 %) | **−112 €** |
| IRR | **4,76 %** (< 5 %) |
| LCOE | **0,239 €/kWh** (contro 0,25 di acquisto e 0,08 di cessione) |
| Payback semplice | **12,9 anni** (il cumulato passa da −218 € a +19 € nel corso dell'anno 13) |
| Payback attualizzato | **mai** |
| Senza detrazione | NPV −2.197 €, payback 23,0 anni |

**Passo 9 — verdetto.**

- Tutte le taglie ammesse (solo 1,5 kWp) hanno NPV ≤ 0 → **"negativo"**, con il testo "nessuna delle taglie proporzionate alla tua casa si ripaga…".
- Nel confronto taglie: 3 kWp NPV **+206 €** e 4 kWp **+641 €**, entrambe **"marginale"**, fuori dal tetto del 150 %.
- Senza l'escalation sulla cessione (§5.3), 3 e 4 kWp sarebbero negative. Con una sostituzione dell'inverter, 3 kWp sarebbe negativa.

**Passo 10 — contorno.**

| Voce | Valore |
|---|---|
| Range risparmio | 227–247 €/anno (domanda 1.220–1.650) |
| Range payback | 12,3–13,6 anni |
| CO₂ evitata | 467,9 × 0,233 = **109 kg/anno** (solo autoconsumo, per scelta prudente) |

Batteria da 3 kWh:

- capex 7.050 €; autoconsumo 58,8 %, autosufficienza 80,8 %;
- NPV **−545 €** (−433 € rispetto a senza batteria); payback 15,0 anni; sostituzione all'anno 9.

Scenario elettrificazione (percorso B, benchmark):

- consumo $1.435 + 90\cdot33 + 1.780 = \mathbf{6.185}$ kWh;
- ⚠ lo SCOP termosifoni 1,97 è **mostrato ma non usato**; l'ACS è contata a resistenza;
- risultato: 4 kWp, NPV +4.079 €, payback 8,9 anni → "accettabile".

---

# Dati per i grafici

Tutti i valori vengono dall'esecuzione reale (TMY Albinea SARAH2, 1,5 kWp, 22,6°/166,8°, bolletta 1.435 kWh).

## Grafico 1 — Produzione e consumo mensili (kWh)

| Mese | Produzione | Consumo | Autoconsumo | Immissione | Prelievo |
|---|---|---|---|---|---|
| Gen | 99,9 | 123,2 | 26,1 | 73,9 | 97,1 |
| Feb | 89,4 | 110,6 | 27,6 | 61,8 | 83,0 |
| Mar | 151,8 | 122,3 | 36,9 | 114,9 | 85,4 |
| Apr | 224,2 | 117,7 | 47,2 | 177,0 | 70,5 |
| Mag | 252,3 | 120,4 | 51,7 | 200,6 | 68,6 |
| Giu | 220,3 | 116,5 | 49,8 | 170,6 | 66,7 |
| Lug | 255,6 | 120,7 | 54,0 | 201,6 | 66,6 |
| Ago | 233,1 | 121,3 | 50,1 | 183,1 | 71,2 |
| Set | 191,5 | 117,6 | 42,3 | 149,1 | 75,2 |
| Ott | 115,2 | 122,4 | 32,5 | 82,8 | 90,0 |
| Nov | 66,3 | 119,1 | 25,1 | 41,2 | 94,1 |
| Dic | 72,4 | 123,3 | 24,6 | 47,8 | 98,7 |
| **Anno** | **1.972** | **1.435** | **468** | **1.504** | **967** |

## Grafico 2 — Giorno tipo (media oraria del mese, Wh)

⚠ Le ore del FV sono **UTC**, quelle della domanda sono **locali**: è il disallineamento del §4.1. L'"autoconsumo indicativo" è $\min$ delle due medie, un'approssimazione grafica, non il valore simulato.

| Ora | Lug FV | Lug domanda | Lug autocons. | Gen FV | Gen domanda | Gen autocons. |
|---|---|---|---|---|---|---|
| 0 | 0 | 121 | 0 | 0 | 122 | 0 |
| 1 | 0 | 110 | 0 | 0 | 111 | 0 |
| 2 | 0 | 110 | 0 | 0 | 111 | 0 |
| 3 | 0 | 110 | 0 | 0 | 111 | 0 |
| 4 | 8 | 110 | 8 | 0 | 111 | 0 |
| 5 | 165 | 135 | 135 | 0 | 137 | 0 |
| 6 | 390 | 173 | 173 | 0 | 179 | 0 |
| 7 | 609 | 152 | 152 | 5 | 156 | 5 |
| 8 | 797 | 136 | 136 | 129 | 136 | 129 |
| 9 | 917 | 125 | 125 | 477 | 125 | 125 |
| 10 | 983 | 125 | 125 | 595 | 125 | 125 |
| 11 | 1.028 | 124 | 124 | 617 | 124 | 124 |
| 12 | 956 | 140 | 140 | 576 | 141 | 141 |
| 13 | 870 | 122 | 122 | 463 | 122 | 122 |
| 14 | 664 | 114 | 114 | 319 | 114 | 114 |
| 15 | 473 | 133 | 133 | 42 | 135 | 42 |
| 16 | 274 | 166 | 166 | 0 | 169 | 0 |
| 17 | 88 | 223 | 88 | 0 | 230 | 0 |
| 18 | 25 | 273 | 25 | 0 | 283 | 0 |
| 19 | 0 | 280 | 0 | 0 | 292 | 0 |
| 20 | 0 | 290 | 0 | 0 | 302 | 0 |
| 21 | 0 | 260 | 0 | 0 | 270 | 0 |
| 22 | 0 | 199 | 0 | 0 | 206 | 0 |
| 23 | 0 | 162 | 0 | 0 | 166 | 0 |

## Grafico 3 — COP in funzione della temperatura esterna

Modello: $\eta_C = 0{,}42$, sbrinamento ×0,85 sotto 5 °C. Il gradino a 5 °C è un artefatto del modello.

| T esterna (°C) | Pavimento (37,5 °C) | Ventilconvettori (42,5 °C) | Termosifoni (75 °C) |
|---|---|---|---|
| −10 | 2,33 | 2,15 | 1,46 |
| −5 | 2,61 | 2,37 | 1,55 |
| 0 | 2,96 | 2,65 | 1,66 |
| 2 | 3,12 | 2,78 | 1,70 |
| 4,9 | 3,40 | 3,00 | 1,77 |
| 5 | 4,01 | 3,54 | 2,09 |
| 7 | 4,28 | 3,73 | 2,15 |
| 10 | 4,74 | 4,08 | 2,25 |
| 15 | 5,80 | 4,82 | 2,44 |
| **SCOP Albinea** | **3,84** | **3,37** | **1,97** |

Distribuzione dei gradi-ora (base 20 °C) per mese, in % del totale:

| Gen | Feb | Mar | Apr | Mag | Giu | Lug | Ago | Set | Ott | Nov | Dic |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 17,5 | 13,4 | 13,5 | 6,3 | 4,2 | 2,0 | 0,6 | 0,7 | 2,3 | 7,8 | 11,4 | 20,2 |

## Grafico 4 — Flussi di cassa cumulati (€, non attualizzati), 1,5 kWp

| Anno | Con detrazione 50 % | Senza detrazione |
|---|---|---|
| 0 | −5.400 | −5.400 |
| 1 | −4.947 | −5.217 |
| 2 | −4.489 | −5.029 |
| 3 | −4.028 | −4.838 |
| 4 | −3.562 | −4.642 |
| 5 | −3.093 | −4.443 |
| 6 | −2.618 | −4.238 |
| 7 | −2.140 | −4.030 |
| 8 | −1.657 | −3.817 |
| 9 | −1.169 | −3.599 |
| 10 | −677 | −3.377 |
| 11 | −450 | −3.150 |
| 12 | −218 | −2.918 |
| 13 | **+19** | −2.681 |
| 14 | 261 | −2.439 |
| 15 | 509 | −2.191 |
| 16 | 761 | −1.939 |
| 17 | 1.019 | −1.681 |
| 18 | 1.283 | −1.417 |
| 19 | 1.552 | −1.148 |
| 20 | 1.827 | −873 |
| 21 | 2.108 | −592 |
| 22 | 2.395 | −305 |
| 23 | 2.687 | −13 |
| 24 | 2.987 | **+287** |
| 25 | 3.292 | 592 |

Per riferimento, il cumulato **attualizzato** con detrazione all'anno 25 è −112 € (= NPV).

## Grafico 5 — Sensibilità dell'NPV (1,5 kWp, base −112 €)

| Prezzo di cessione (€/kWh) | 0,00 | 0,04 | 0,06 | **0,08** | 0,10 | 0,12 |
|---|---|---|---|---|---|---|
| NPV (€) | −2.143 | −1.127 | −620 | **−112** | +395 | +903 |

| Aliquota detrazione | 0 % | 36 % | **50 %** |
|---|---|---|---|
| NPV (€) | −2.197 | −696 | **−112** |

| Escalation prezzi | 0 % | 1 % | 2 % | **2,5 %** | 4 % |
|---|---|---|---|---|---|
| NPV (€) | −1.012 | −689 | −318 | **−112** | +599 |

| Tasso di sconto | 2 % | 3 % | **5 %** | 7 % |
|---|---|---|---|---|
| NPV (€) | +1.604 | +943 | **−112** | −906 |

Effetti strutturali stimati (§5.3), variazione di NPV per taglia:

| Effetto | 1,5 kWp | 3 kWp | 4 kWp |
|---|---|---|---|
| Escalation applicata alla cessione | +436 | +1.007 | +1.392 |
| Inverter 900 € all'anno 13 | −477 | −477 | −477 |

## Grafico 6 — Produzione per orientamento e inclinazione

kWh/kWp·anno, TMY Albinea, `close_roof`.

| Azimut \ Tilt | 0° | 10° | 20° | 30° | 40° | 60° | 90° |
|---|---|---|---|---|---|---|---|
| Est (90°) | 1.147 | 1.128 | 1.094 | 1.050 | 999 | 877 | 656 |
| Sud-Est (135°) | 1.147 | 1.207 | 1.241 | 1.251 | 1.235 | 1.136 | 844 |
| **Sud (180°)** | 1.147 | 1.246 | 1.314 | **1.349** | **1.352** | 1.264 | 917 |
| Sud-Ovest (225°) | 1.147 | 1.222 | 1.268 | 1.286 | 1.280 | 1.192 | 901 |
| Ovest (270°) | 1.147 | 1.148 | 1.127 | 1.091 | 1.047 | 933 | 711 |
| Nord (0°) | 1.147 | 1.030 | 892 | 760 | 630 | 406 | 311 |

A 30° di inclinazione, rispetto al sud pieno: SO **95,4 %**, O **80,9 %**, E **77,8 %**, N **56,4 %**. Ovest batte est di circa il 4 %, probabilmente per le nebbie mattutine della pianura.

---

# Limiti e onestà

## L.1 Cosa il modello fa bene e su cosa poggia solidamente

1. **Dati climatici di qualità e locali**: TMY PVGIS da satellite, orario, specifico del comune. È la base standard dell'industria.
2. **Trasposizione di Perez**, coefficienti originali: è il modello di riferimento per superfici inclinate.
3. **Bilancio orario reale su 8.760 ore**, non medie mensili. L'autoconsumo nasce dalla coincidenza vera tra sole e consumi, ora per ora.
4. **Produzione simulata davvero** per ogni taglia e orientamento. Il ventaglio delle taglie è calcolato, non interpolato.
5. **Flussi di cassa espliciti anno per anno**, con degrado, escalation, O&M, detrazione decennale e sostituzione della batteria. NPV, IRR e payback sono coerenti tra loro.
6. **Onestà strutturale delle scelte di prodotto**:
   - batteria sempre affiancata;
   - verdetto negativo mai nascosto;
   - tetto anti-sovradimensionamento;
   - CO₂ contata solo sull'autoconsumo;
   - blocco assunzioni in ogni risposta.
7. **Ordini di grandezza plausibili**: 1.315 kWh/kWp a 44,6 °N e 23° di inclinazione, PR circa 0,76–0,78, SCOP 1,97–3,84 con i terminali nell'ordine giusto.

## L.2 Cosa il modello NON modella, approssima o assume

| # | Semplificazione | Impatto stimato | Direzione |
|---|---|---|---|
| 1 | Ombre vicine come forfait del 3 %, orizzonte lontano assente | 0 % (tetto libero) … oltre −20 % (ombre a sud) | ottimista con ombre |
| 2 | Temperatura di cella con $U_0 = 29$ per moduli complanari | −4,5 % di produzione con $U_0 = 20$ | ottimista |
| 3 | Escalation del 2,5 % sul prezzo di cessione | +436 … +1.392 € di NPV (Borzano) | ottimista |
| 4 | Nessuna sostituzione dell'inverter | circa −480 € di NPV | ottimista |
| 5 | Capienza IRPEF ignorata nell'NPV | fino a −1.700 € nominali, circa −1.300 € attualizzati (Borzano con IRPEF 100 €/anno) | ottimista |
| 6 | Prezzo di cessione 0,08 basato su un regime (scambio sul posto) forse non più accessibile | ±500 € di NPV ogni 0,02 €/kWh | indeterminata |
| 7 | Prezzo di acquisto 0,25 forse comprensivo di quote fisse | sovrastima del risparmio fino al 10–20 % (non misurato) | ottimista |
| 8 | Costo a scaglioni mai validato, con fisso effettivo di 2.300 € | non quantificato | probabilmente prudente |
| 9 | Profilo orario calibrato su 1 casa; domanda senza stagionalità | ±5–10 punti di autoconsumo tra famiglie (stima qualitativa) | indeterminata |
| 10 | FV in UTC contro domanda in ora locale | −0,5 … −2 punti di autoconsumo | prudente |
| 11 | Risoluzione oraria (picchi sub-orari mediati) | sovrastima dell'autoconsumo di qualche punto (letteratura) | ottimista |
| 12 | Proxy di consumo validato su 1 caso | ±30 % dichiarato, ma non misurato | indeterminata |
| 13 | Falda est modellata come ovest | +4 % di produzione per le falde a est | ottimista |
| 14 | Area di tetto = metà della superficie abitabile | fino a 2× l'area reale per case a 2 piani (vale solo quando il tetto limita) | ottimista |
| 15 | Più falde Google sommate ma simulate con l'orientamento migliore | dipende dal tetto | ottimista |
| 16 | Batteria che non si azzera dopo la sostituzione; una sola sostituzione | segno netto incerto | misto |
| 17 | Elettrificazione: soglia a 20 °C tutto l'anno, COP costante nella forma oraria | 9,8 % del carico termico tra maggio e settembre | ottimista sull'autoconsumo |
| 18 | Elettrificazione senza gas: SCOP dei terminali ignorato, ACS a resistenza | consumo sovrastimato con ventilconvettori/pavimento, sottostimato con termosifoni | ⇒ verdetto FV distorto |
| 19 | Cottura: η caldaia 0,87 invece di η fornello ~0,45 | consumo di cottura circa +60 % | ottimista sul FV |
| 20 | "Prudente" = più consumo elettrico, nello scenario elettrificazione | sistematicamente rende il FV più conveniente | ottimista |
| 21 | TMY = anno medio, nessuna variabilità tra anni | ±5 %/anno sulla produzione | — |
| 22 | SARAH2 invece di SARAH3; percorso standalone invece di pvlib | non quantificato (il docstring dichiara ±2–3 %) | — |
| 23 | Soglie del verdetto (8/12/20 anni, 35 %, 10 %, 150 %) | arbitrarie: definiscono le etichette | — |
| 24 | Bande ±15/30/45 % | scelte, non misurate | — |

**Sul punto 20.** Nel router l'aggettivo "prudente" (`_SCOP_PRUDENTE`, `_COP_ACS_CONSERVATIVO`, `_COP_COTTURA`) indica la scelta che produce **più** consumo elettrico. Ma la domanda dello scenario è "quanto conviene il FV", e lì più consumo significa **più convenienza**. È l'opposto del principio scritto in `residential/estimate_defaults.py:17-19`: "un consumo sovrastimato … farebbe sembrare il FV più conveniente del vero — è l'errore da evitare".

## L.3 Discrepanze tra codice e documentazione

| # | Dove | Cosa dice | Cosa fa il codice / cosa risulta |
|---|---|---|---|
| a | `pvgis_client.py:26`, METHODOLOGY §1, CLAUDE.md | SARAH-3 | SARAH2 (API v5.2) |
| b | `solar.py:9-14` | pvlib, Spencer | standalone; declinazione di Cooper |
| c | `battery_scenario.py:19-22` | sostituzione anno 12-14 | anno 9 |
| d | `battery_scenario.py:39` | DoD 80 % | finestra utile 85 % (5–90 %) |
| e | `estimate_casa.py:376-378`, CLAUDE.md | NPV/capex 10 % ≈ 0,4 %/anno | ≈ IRR 6 % |
| f | `estimate_casa.py:957` | 0,08 = "scambio sul posto 2026" | regime probabilmente non più attivabile |
| g | `estimate_casa.py:941-942` | "include €1.500 di costi fissi" | fisso effettivo 2.300 € |
| h | `estimate_casa.py:154` | "east gives same annual total" | E ≈ −4 % rispetto a O |
| i | schema `irpef_annua_stimata` | lascia intendere che la capienza venga calcolata | NPV invariato |
| j | `estimate_casa.py:1148` + percorso B | "SCOP stimato 1,97" | nel percorso B non usato (SCOP implicito 2,75) |
| k | `energy_vectors.py:20` contro `estimate_casa.py:126` | CO₂ ENTSO-E 2023 / 2024 | stesso valore, anno incoerente |

---

*Fine dell'audit. Nessun file di codice è stato modificato; l'unico file creato è questo documento.*
