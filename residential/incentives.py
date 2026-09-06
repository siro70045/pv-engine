"""
residential/incentives.py

Modello della detrazione IRPEF per impianti FV residenziali.

Normativa di riferimento: spese sostenute nel 2026.
  - Art. 16-bis TUIR (DPR 917/1986), come prorogato dalla Legge di Bilancio
    2025 (L. 207/2024, art. 1, cc. 54-56).
  - Tetto di spesa: 96.000 € per unità immobiliare (invariato dal DL 83/2012).
  - Recupero in 10 quote annuali di pari importo (NON sconto immediato).
  - Sconto in fattura e cessione del credito aboliti per le nuove richieste
    dal DL 39/2024 (art. 1). Non applicabili agli impianti FV residenziali
    ordinari dal 17/02/2023 in poi.

Aliquote 2026 (L. 207/2024):
  - 50 % se abitazione PRINCIPALE (residenza anagrafica del proprietario
    o del titolare di altro diritto reale di godimento — art. 16-bis c. 1)
  - 36 % negli altri casi (seconda casa, locata, nuda proprietà con altro
    occupante, ecc.)

⚠ ATTENZIONE — CAPIENZA IRPEF:
  La quota annua di detrazione (detrazione_totale / 10) si può portare in
  detrazione SOLO fino a concorrenza dell'IRPEF lorda dovuta nell'anno.
  La parte eccedente NON è rimborsabile e NON si può riportare all'anno
  successivo: è persa definitivamente. Se l'IRPEF annua stimata è inferiore
  alla quota annua, la detrazione effettiva è minore di quella totale.
  Questo modulo la quantifica esplicitamente.

ESCLUSIONI (fuori perimetro):
  - Ammortamento accelerato, IRES, Transizione 5.0: uso aziendale/industriale,
    non residenziale. Il flusso /estimate/casa non li attiva.
  - CER (Comunità Energetica Rinnovabile): incentivo separato, non cumulabile
    con la detrazione sulla stessa quota di energia.

INTEGRAZIONE CON engine/economics.py
  Il motore applica detrazione_fiscale_pct su total_capex_eur senza cap a
  96.000 €. Il chiamante residenziale deve:
    1. Calcolare spesa_agevolabile = min(capex, 96_000)
    2. Derivare la quota_annua da questo modulo
    3. Passare al motore:
         ItalianIncentives(
             detrazione_fiscale_pct = detrazione_totale / total_capex * 100,
             detrazione_spread_years = 10,
             ammortamento_accelerato = False,
             transizione_5_0_pct = 0.0,
         )
       oppure, più semplicemente, scalare il capex all'agevolabile per la
       chiamata al motore (se il calcolo è solo la componente incentivi).
  La capienza IRPEF NON è modellata da engine/economics.py: se l'utente ha
  capienza ridotta, l'NPV prodotto dal motore sarà ottimistico. Il wrapper
  deve mostrare detrazione_effettiva_eur (da questo modulo) come valore
  di riferimento e segnalare la discrepanza.

# ── TODO: Reddito Energetico ─────────────────────────────────────────────────
# Struttura per l'aggiunta futura del Reddito Energetico (contributo a fondo
# perduto per famiglie a basso reddito ISEE che installano FV, decreto attuativo
# D.Lgs. 199/2021 art. 8, in fase di attuazione regionale al 2026).
#
# NON è cumulabile con la detrazione IRPEF sulla stessa spesa.
# Quando implementato, sarà una funzione separata:
#
#   def calc_reddito_energetico(
#       capex_eur: float,
#       isee_eur: float,
#       regione: str,            # le dotazioni variano per bando regionale
#       kwp: float,
#   ) -> RedditoEnergeticoResult:
#       ...
#
# e il chiamante dovrà presentare all'utente le due opzioni in modo esclusivo
# (detrazione IRPEF se contribuente IRPEF capiente vs Reddito Energetico se
# ISEE basso e il bando regionale è aperto).
# ─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Tetto di spesa per unità immobiliare [€] — art. 16-bis TUIR, invariato dal 2012
TETTO_SPESA_EUR: float = 96_000.0

# Aliquote — L. 207/2024 per spese 2026
ALIQUOTA_PRINCIPALE_PCT: float = 50.0
ALIQUOTA_ALTRI_CASI_PCT: float = 36.0

# Numero di quote annuali — invariato dal 2012
QUOTE_ANNUALI: int = 10


@dataclass
class IrpefDeductionResult:
    """
    Risultato del calcolo della detrazione IRPEF per impianto FV residenziale.

    Tutti i valori monetari in EUR.
    """
    # ── Input rispecchiati ────────────────────────────────────────────────────
    capex_eur: float
    abitazione_principale: bool
    irpef_annua_stimata: Optional[float]   # None → capienza piena assunta

    # ── Calcolo normativo ─────────────────────────────────────────────────────
    aliquota_pct: float                    # 50.0 o 36.0
    spesa_agevolabile_eur: float           # min(capex, 96.000)
    detrazione_totale_eur: float           # spesa_agevolabile × aliquota
    quota_annua_eur: float                 # detrazione_totale / 10

    # ── Capienza fiscale ──────────────────────────────────────────────────────
    detrazione_effettiva_eur: float
    """
    Somma delle quote effettivamente recuperabili su 10 anni, tenuto conto
    della capienza IRPEF. Se irpef_annua_stimata è None, coincide con
    detrazione_totale_eur (assunzione capienza piena — pessimistica per il
    contribuente, ottimistica per il ritorno dell'investimento).
    """
    perdita_fiscale_eur: float
    """
    detrazione_totale - detrazione_effettiva. 0 se capienza sufficiente.
    """
    assunzione_capienza_piena: bool
    """
    True se irpef_annua_stimata è None — segnala che la detrazione effettiva
    potrebbe essere inferiore se l'IRPEF reale è bassa.
    """

    # ── Profilo annuale ───────────────────────────────────────────────────────
    quote_per_anno: list[float] = field(default_factory=list)
    """
    Lista di 10 valori: quota effettivamente recuperata in ciascuno degli
    anni 1-10. Usarla per costruire il flusso da passare al motore economico.
    """

    # ── Testo per le assunzioni UI ────────────────────────────────────────────
    nota_assunzioni: str = ""
    """
    Stringa pronta per essere inserita nel blocco 'assunzioni' della response.
    """


def calc_irpef_deduction(
    capex_eur: float,
    abitazione_principale: bool = True,
    irpef_annua_stimata: Optional[float] = None,
) -> IrpefDeductionResult:
    """Calcola la detrazione IRPEF art. 16-bis TUIR per impianto FV residenziale.

    Parameters
    ----------
    capex_eur : float
        Costo totale dell'impianto (PV ± batteria) [€]. Se include anche
        altri interventi edilizi, passare solo la quota relativa al FV/batteria.
    abitazione_principale : bool
        True se l'immobile è l'abitazione principale del contribuente
        (residenza anagrafica), False altrimenti (seconda casa, affitto, ecc.).
    irpef_annua_stimata : float | None
        IRPEF lorda stimata annua [€]. Se None, si assume capienza piena
        (assunzione ottimistica: la detrazione è recuperata integralmente).
        Se fornita, le quote annue sono cappate all'IRPEF disponibile — la
        parte eccedente si perde definitivamente.

    Returns
    -------
    IrpefDeductionResult
        Struttura completa con importi, profilo annuale e nota per l'utente.

    Raises
    ------
    ValueError
        Se capex_eur ≤ 0 o irpef_annua_stimata < 0.

    Notes
    -----
    Normativa: art. 16-bis TUIR (DPR 917/1986), aliquote 2026 da L. 207/2024.
    La quota annua non recuperata per insufficiente IRPEF è definitivamente
    persa: non è rimborsabile né riportabile all'anno successivo.
    """
    if capex_eur <= 0:
        raise ValueError(f"capex_eur deve essere > 0, ricevuto {capex_eur!r}")
    if irpef_annua_stimata is not None and irpef_annua_stimata < 0:
        raise ValueError(
            f"irpef_annua_stimata deve essere ≥ 0, ricevuto {irpef_annua_stimata!r}"
        )

    aliquota = (ALIQUOTA_PRINCIPALE_PCT if abitazione_principale
                else ALIQUOTA_ALTRI_CASI_PCT)

    spesa_agevolabile = min(capex_eur, TETTO_SPESA_EUR)
    detrazione_totale = round(spesa_agevolabile * aliquota / 100.0, 2)
    quota_annua = round(detrazione_totale / QUOTE_ANNUALI, 2)

    # Capienza fiscale: ogni quota è cappata all'IRPEF annua disponibile
    if irpef_annua_stimata is None:
        quote = [quota_annua] * QUOTE_ANNUALI
        assunzione_capienza_piena = True
    else:
        # La quota non può superare l'IRPEF lorda annua (art. 15 + 16-bis TUIR)
        quota_effettiva = min(quota_annua, irpef_annua_stimata)
        quote = [round(quota_effettiva, 2)] * QUOTE_ANNUALI
        assunzione_capienza_piena = False

    detrazione_effettiva = round(sum(quote), 2)
    perdita_fiscale = round(detrazione_totale - detrazione_effettiva, 2)

    # Nota testuale per le assunzioni UI
    tipo_immobile = "abitazione principale" if abitazione_principale else "altro immobile"
    nota = (
        f"Detrazione IRPEF {aliquota:.0f}% ({tipo_immobile}) su spesa "
        f"agevolabile €{spesa_agevolabile:,.0f} (tetto 96.000 €/unità, "
        f"art. 16-bis TUIR — L. 207/2024): totale €{detrazione_totale:,.0f} "
        f"in {QUOTE_ANNUALI} quote annuali da €{quota_annua:,.0f}. "
        "Recuperata in dichiarazione dei redditi (no sconto in fattura). "
    )
    if assunzione_capienza_piena:
        nota += (
            "⚠ Assunzione capienza IRPEF piena: il recupero integrale "
            f"(€{detrazione_totale:,.0f}) richiede un'IRPEF annua ≥ €{quota_annua:,.0f}. "
            "Indicare l'IRPEF stimata per calcolare la perdita fiscale effettiva."
        )
    elif perdita_fiscale > 0:
        nota += (
            f"⚠ IRPEF stimata (€{irpef_annua_stimata:,.0f}/anno) inferiore alla "
            f"quota (€{quota_annua:,.0f}/anno): perdita fiscale stimata "
            f"€{perdita_fiscale:,.0f} su 10 anni."
        )
    else:
        nota += f"Capienza IRPEF sufficiente: nessuna perdita attesa."

    return IrpefDeductionResult(
        capex_eur=capex_eur,
        abitazione_principale=abitazione_principale,
        irpef_annua_stimata=irpef_annua_stimata,
        aliquota_pct=aliquota,
        spesa_agevolabile_eur=spesa_agevolabile,
        detrazione_totale_eur=detrazione_totale,
        quota_annua_eur=quota_annua,
        detrazione_effettiva_eur=detrazione_effettiva,
        perdita_fiscale_eur=perdita_fiscale,
        assunzione_capienza_piena=assunzione_capienza_piena,
        quote_per_anno=quote,
        nota_assunzioni=nota,
    )
