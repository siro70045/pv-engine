"""
school/incentives_school.py

Italian incentive calculation for school PV installations.
"""

from __future__ import annotations


def calc_school_incentives(
    capex_eur: float,
    kwp: float,
    is_public: bool = True,
    has_pnrr: bool = False,
    pnrr_grant_pct: float = 0.50,
    cer_participation: bool = False,
    cer_incentive_eur_kwh: float = 0.11,
    cer_shared_fraction: float = 0.60,
    annual_production_kwh: float = 0.0,
) -> dict:
    """
    Calcola gli incentivi applicabili a un impianto FV scolastico italiano.

    Parameters
    ----------
    capex_eur : float
        Costo totale dell'impianto IVA inclusa al 22% [€].
    kwp : float
        Potenza di picco dell'impianto [kWp].
    is_public : bool
        True = scuola statale (IVA agevolata al 10%).
    has_pnrr : bool
        True = accesso ai fondi PNRR per l'efficienza energetica delle scuole.
    pnrr_grant_pct : float
        Percentuale del contributo PNRR a fondo perduto [0–1].
    cer_participation : bool
        True = partecipazione a una Comunità Energetica Rinnovabile (CER).
    cer_incentive_eur_kwh : float
        Incentivo GSE per energia condivisa in CER [€/kWh].
    cer_shared_fraction : float
        Frazione della produzione condivisa in CER [0–1].
    annual_production_kwh : float
        Produzione annua stimata del sistema FV [kWh/anno].

    Returns
    -------
    dict with keys:
        capex_iva_corrected      float  — capex con IVA al 10% (se pubblica)
        iva_saving               float  — risparmio IVA
        pnrr_grant               float  — contributo PNRR a fondo perduto
        net_capex_after_grants   float  — capex netto dopo contributi
        cer_annual_incentive     float  — incentivo GSE CER annuo
        total_first_year_benefit float  — beneficio totale primo anno
        notes                    list[str]
    """
    notes: list[str] = []

    # ── IVA agevolata per scuole pubbliche ────────────────────────────────────
    # capex input è IVA inclusa al 22%; le scuole pubbliche pagano IVA al 10%
    if is_public:
        iva_saving = capex_eur * (0.22 - 0.10) / 1.22
        capex_iva_corrected = capex_eur - iva_saving
        notes.append(
            "IVA agevolata al 10% per ente pubblico "
            f"(risparmio €{iva_saving:,.0f})."
        )
    else:
        iva_saving = 0.0
        capex_iva_corrected = capex_eur
        notes.append(
            "IVA al 22% applicata (scuola paritaria/privata). "
            "Verificare possibilità di IVA agevolata."
        )

    # ── Contributo PNRR ───────────────────────────────────────────────────────
    if has_pnrr:
        pnrr_grant = capex_iva_corrected * pnrr_grant_pct
        notes.append(
            f"Contributo PNRR a fondo perduto {pnrr_grant_pct:.0%}: "
            f"€{pnrr_grant:,.0f}. Verificare bando specifico con il Comune."
        )
    else:
        pnrr_grant = 0.0
        notes.append(
            "Nessun contributo PNRR incluso. "
            "Verificare l'accesso ai bandi PNRR M4C1 per l'edilizia scolastica."
        )

    # ── Incentivo CER (GSE, DM 07/12/2023) ───────────────────────────────────
    if cer_participation and annual_production_kwh > 0:
        cer_annual = annual_production_kwh * cer_shared_fraction * cer_incentive_eur_kwh
        notes.append(
            f"Incentivo CER GSE: {cer_incentive_eur_kwh:.3f} €/kWh × "
            f"{cer_shared_fraction:.0%} energia condivisa → "
            f"€{cer_annual:,.0f}/anno."
        )
    else:
        cer_annual = 0.0
        if not cer_participation:
            notes.append(
                "CER non attivata. La partecipazione a una Comunità Energetica "
                "Rinnovabile genera incentivi GSE aggiuntivi (~0.10–0.11 €/kWh)."
            )

    # ── Export bonus GSE per PA ───────────────────────────────────────────────
    # Modellato come 0.02 €/kWh sull'energia ceduta (forfettario PA)
    # Non calcolato qui perché dipende dall'energy balance — incluso nel motore
    notes.append(
        "Bonus export GSE (PA): ~0.02 €/kWh sull'energia ceduta in aggiunta "
        "al prezzo di mercato (verificare contratto GSE)."
    )

    net_capex_after_grants = capex_iva_corrected - pnrr_grant

    # First-year benefit = PNRR (immediate) + CER year 1
    total_first_year_benefit = pnrr_grant + cer_annual

    return {
        "capex_iva_corrected":     round(capex_iva_corrected,   0),
        "iva_saving":              round(iva_saving,             0),
        "pnrr_grant":              round(pnrr_grant,             0),
        "net_capex_after_grants":  round(net_capex_after_grants, 0),
        "cer_annual_incentive":    round(cer_annual,             0),
        "total_first_year_benefit":round(total_first_year_benefit, 0),
        "notes": notes,
    }
