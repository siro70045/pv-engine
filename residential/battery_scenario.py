"""
residential/battery_scenario.py

Scenari confrontati senza/con batteria per impianti FV residenziali.

Principio di onestà (non negoziabile)
--------------------------------------
La batteria tipicamente aumenta l'autoconsumo MA allunga il payback.
Questa funzione restituisce SEMPRE entrambi gli scenari affiancati con
il trade-off esplicito. Mai presentare solo la metà favorevole.

Passo 0 — Sostituzione batteria (verifica, sola lettura)
---------------------------------------------------------
engine/energy_balance.py modella la sostituzione: `find_battery_replacement_years`
calcola l'anno di rimpiazzo per degrado (calendario 2 %/anno + cicli 0.003 %/ciclo,
soglia default 80 % della capacità nominale). `OperatingCost.battery_replacement_years`
porta il costo nell'anno corretto. Il wrapper usa queste API — non le reimplementa.

Per LFP residenziale con ~230 cicli/anno (working_family: 1 ciclo/giorno × 365
× fattore di utilizzo parziale 0.63), il modello di degrado prevede la prima
sostituzione intorno all'anno 12-14, compatibile con la garanzia LFP tipica
(10 anni / 4000 cicli a 80 %).

Passo 1 — Dimensionamento di default
--------------------------------------
Criterio: coprire il consumo elettrico serale tipico del profilo working_family
(fascia F2+F3, ore 19-23 + notte 23-7 del giorno medio), al netto della quota
già coperta da piccolo surplus FV in tarda serata (quasi zero per latitudine
italiana a ottobre-febbraio).

Stima consumo serale:
  - Profilo working_family: ~36 % del consumo annuo in F2 + 37 % in F3
  - F3 = notti (23-7) + domenica (non utile la batteria per domenica);
    quota F3 "serale" (19-23, lun-sab) è effettivamente in F2
  - Consumo serale utile (ore 17-23 feriale): ~25-30 % del consumo giornaliero
  - Per famiglia 4 pers/90 m² (1.495 kWh/anno proxy): ~1.495 × 0.27 / 365 ×
    giorni_FV_utili ≈ 1.1 kWh/sera
  - Taglia batteria "1 ciclo al giorno": 1.1 kWh utilizzabili → nominale ~1.4 kWh
    (DoD 80 %, SoC min 5 %)
  - Arrotondato per eccesso a 3 kWh nominali: il mercato non offre tagli < 2.5
    kWh per prodotti residenziali certificati (catalogo 2026: Pylontech FORCE-H1
    2.4 kWh, Solax Triple Power 5.8 kWh, SolarEdge Home Battery 9.7 kWh).
    3 kWh è il punto di ingresso usabile; famiglie > 3.500 kWh/anno beneficiano
    di 5-7 kWh.
  - Per consumi < 2.000 kWh/anno (caso Borzano): 3 kWh sarà spesso over-dimensionato
    e il payback risulterà lungo — coerente con il principio di onestà.

Fonti:
  [B1] IRENA, Utility-scale batteries Innovation outlook 2023: LFP cycle life
       ~4000 cicli a 80 % capacity.
  [B2] ENEA RAEE 2023, Tab. 2.5: Prezzi storage residenziale Italia 2023
       400-700 €/kWh installato (sistema completo: celle + BMS + inverter AC-coupled
       + installazione). Valore centrale 550 €/kWh.
  [B3] IEA, World Energy Outlook 2024: previsione calo prezzi storage LFP ~15-20%
       al 2030 da 2024. Al 2026, valore sostituzione atteso ~350-450 €/kWh.
       Usato 380 €/kWh per il costo di rimpiazzo (più conservativo del -50%
       che alcuni analisti citano per il 2030).
  [B4] Dati cicli residenziali: ~230 cicli/anno per profilo serale working_family
       (carica da FV 11-16h, scarica 17-23h, ~0.63 cicli completi/giorno equivalenti
       tenuto conto di profondità di scarica parziale).
       Fonte: stima interna da profili TERNA (letteratura: 200-280 cicli/anno
       per usi residenziali italiani). DA RIVALIDARE con log reali.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.economics import (
    EconomicsResult,
    FinancialParameters,
    ItalianIncentives,
    InvestmentCost,
    OperatingCost,
    calc_economics,
)
from engine.energy_balance import (
    BatterySystem,
    EnergyBalanceInput,
    GridTariff,
    find_battery_replacement_years,
    simulate_lifetime,
)
from residential.incentives import calc_irpef_deduction


# ══════════════════════════════════════════════════════════════════════════════
# COSTANTI DI DEFAULT — residenziale 2026
# ══════════════════════════════════════════════════════════════════════════════

# Taglia batteria di default [kWh nominali] — punto di ingresso mercato 2026
# Criterio: coprire consumo serale medio profilo working_family su base giornaliera.
# v. docstring del modulo per la derivazione.
DEFAULT_BATTERY_KWH: float = 3.0

# Costo batteria installata [€/kWh] — ENEA RAEE 2023 [B2], valore centrale
BATTERY_COST_EUR_KWH: float = 550.0

# Costo sostituzione batteria [€/kWh] — IEA 2024, calo prezzi previsto [B3]
BATTERY_REPLACEMENT_COST_EUR_KWH: float = 380.0

# Cicli/anno stimati per profilo working_family [B4] — DA RIVALIDARE
BATTERY_CYCLES_PER_YEAR: float = 230.0

# Soglia capacità per sostituzione: 80 % — IRENA [B1], standard de facto
BATTERY_REPLACEMENT_THRESHOLD: float = 0.80

# Prezzi energia residenziale 2026 [€/kWh]
DEFAULT_IMPORT_PRICE_EUR_KWH: float = 0.25   # tariffa media 2026, ARERA
DEFAULT_EXPORT_PRICE_EUR_KWH: float = 0.08   # prezzo GSE scambio sul posto (stima)

# Parametri finanziari di default
DEFAULT_ANALYSIS_YEARS: int   = 25
DEFAULT_DISCOUNT_RATE: float  = 0.05
DEFAULT_ESC_RATE: float       = 0.025  # escalation prezzo elettricità
DEFAULT_OM_PCT: float         = 1.0    # O&M = 1% del CAPEX/anno


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScenarioResult:
    """Risultato di un singolo scenario (senza o con batteria)."""
    label: str                         # "senza_batteria" | "con_batteria"
    capex_eur: float                   # CAPEX totale (PV + batteria se presente)
    battery_kwh: float                 # capacità batteria nominale [kWh] (0 se assente)
    battery_replacement_years: list[int]   # anni di sostituzione previsti
    autoconsumo_pct: float             # self-consumption rate [%] anno 1
    autosufficienza_pct: float         # self-sufficiency rate [%] anno 1
    payback_anni: float                # payback semplice [anni]
    payback_scontato_anni: float       # payback scontato [anni]
    npv_eur: float                     # NPV a 25 anni [€]
    irr_pct: float                     # IRR [%]
    detrazione_totale_eur: float       # detrazione IRPEF totale (v. incentives.py)
    detrazione_effettiva_eur: float    # detrazione effettiva (capienza IRPEF)
    risparmio_annuo_anno1_eur: float   # risparmio anno 1 [€/anno]
    produzione_anno1_kwh: float        # produzione FV anno 1 [kWh]
    economics: EconomicsResult         # oggetto completo per drill-down


@dataclass
class BatteryTradeoff:
    """Trade-off esplicito tra scenario con e senza batteria."""
    delta_autoconsumo_pp: float        # +pp di autoconsumo con batteria
    delta_autosufficienza_pp: float    # +pp di autosufficienza con batteria
    delta_payback_anni: float          # Δ payback (+ = più lungo con batteria)
    delta_npv_eur: float               # Δ NPV (negativo se batteria non conviene)
    delta_capex_eur: float             # costo aggiuntivo della batteria
    batteria_conveniente: bool
    """True se NPV(con_batteria) > NPV(senza_batteria). In caso negativo
    l'utente deve vedere esplicitamente che la batteria non è conveniente
    nelle assunzioni correnti."""
    nota: str                          # Testo pronto per l'utente


@dataclass
class BatteryScenarioComparison:
    """Confronto completo senza/con batteria — sempre entrambi."""
    senza_batteria: ScenarioResult
    con_batteria: ScenarioResult
    tradeoff: BatteryTradeoff


# ══════════════════════════════════════════════════════════════════════════════
# FUNZIONE PRINCIPALE
# ══════════════════════════════════════════════════════════════════════════════

def compare_battery_scenarios(
    pv_hourly_Wh: list[float],
    demand_hourly_Wh: list[float],
    pv_kwp: float,
    pv_cost_eur_kwp: float,
    abitazione_principale: bool = True,
    irpef_annua_stimata: Optional[float] = None,
    battery_kwh: float = DEFAULT_BATTERY_KWH,
    battery_cost_eur_kwh: float = BATTERY_COST_EUR_KWH,
    import_price_eur_kwh: float = DEFAULT_IMPORT_PRICE_EUR_KWH,
    export_price_eur_kwh: float = DEFAULT_EXPORT_PRICE_EUR_KWH,
    analysis_years: int = DEFAULT_ANALYSIS_YEARS,
    discount_rate: float = DEFAULT_DISCOUNT_RATE,
    escalation_rate: float = DEFAULT_ESC_RATE,
) -> BatteryScenarioComparison:
    """Confronta due scenari residenziali: senza e con batteria.

    Entrambi gli scenari vengono simulati con lo stesso profilo di consumo,
    la stessa produzione FV e gli stessi parametri economici. L'unica
    differenza è la presenza/assenza della batteria.

    Parameters
    ----------
    pv_hourly_Wh : list[float]
        Produzione FV oraria [Wh], 8760 valori.
    demand_hourly_Wh : list[float]
        Domanda elettrica oraria [Wh], 8760 valori.
    pv_kwp : float
        Potenza di picco impianto FV [kWp].
    pv_cost_eur_kwp : float
        Costo FV installato [€/kWp].
    abitazione_principale : bool
        True = detrazione 50%, False = 36% (art. 16-bis TUIR).
    irpef_annua_stimata : float | None
        IRPEF annua stimata [€]. None = capienza piena assunta.
    battery_kwh : float
        Capacità batteria nominale [kWh]. Default 3 kWh (punto di ingresso 2026).
    battery_cost_eur_kwh : float
        Costo batteria installata [€/kWh]. Default 550 €/kWh (ENEA 2023).
    import_price_eur_kwh : float
        Prezzo acquisto da rete [€/kWh].
    export_price_eur_kwh : float
        Prezzo cessione in rete [€/kWh].
    analysis_years : int
        Orizzonte di analisi [anni].
    discount_rate : float
        Tasso di sconto [frazione].
    escalation_rate : float
        Escalation annua prezzo elettricità [frazione].

    Returns
    -------
    BatteryScenarioComparison
        Entrambi gli scenari e il trade-off esplicito.
    """
    if len(pv_hourly_Wh) != 8760:
        raise ValueError(f"pv_hourly_Wh deve avere 8760 valori, ricevuto {len(pv_hourly_Wh)}")
    if len(demand_hourly_Wh) != 8760:
        raise ValueError(f"demand_hourly_Wh deve avere 8760 valori, ricevuto {len(demand_hourly_Wh)}")
    if pv_kwp <= 0:
        raise ValueError(f"pv_kwp deve essere > 0, ricevuto {pv_kwp!r}")
    if battery_kwh < 0:
        raise ValueError(f"battery_kwh deve essere ≥ 0, ricevuto {battery_kwh!r}")

    tariff = GridTariff(
        import_price_eur_kWh=import_price_eur_kwh,
        export_price_eur_kWh=export_price_eur_kwh,
    )
    fin = FinancialParameters(
        discount_rate=discount_rate,
        energy_price_escalation=escalation_rate,
        analysis_years=analysis_years,
    )

    results = []
    for label, bat_kwh in [("senza_batteria", 0.0), ("con_batteria", battery_kwh)]:
        battery = BatterySystem(capacity_kWh=bat_kwh)

        # Anni di sostituzione per degrado
        repl_years = find_battery_replacement_years(
            battery,
            lifetime_years=analysis_years,
            threshold=BATTERY_REPLACEMENT_THRESHOLD,
            cycles_per_year=BATTERY_CYCLES_PER_YEAR,
        )

        # CAPEX
        pv_capex = pv_kwp * pv_cost_eur_kwp
        bat_capex = bat_kwh * battery_cost_eur_kwh
        total_capex = pv_capex + bat_capex

        # Detrazione IRPEF — sul CAPEX totale (PV + batteria, stesso tetto 96k)
        detr = calc_irpef_deduction(
            total_capex,
            abitazione_principale=abitazione_principale,
            irpef_annua_stimata=irpef_annua_stimata,
        )
        # Percentuale effettiva per il motore (già agganciata a spesa_agevolabile)
        detr_pct_for_engine = (
            detr.detrazione_totale_eur / total_capex * 100.0
            if total_capex > 0 else 0.0
        )

        investment = InvestmentCost(
            pv_cost_eur_kWp=pv_cost_eur_kwp,
            peak_power_kWp=pv_kwp,
            battery_cost_eur_kWh=battery_cost_eur_kwh if bat_kwh > 0 else 0.0,
            battery_capacity_kWh=bat_kwh,
        )
        opex = OperatingCost(
            om_pct_of_capex_per_yr=DEFAULT_OM_PCT,
            battery_replacement_years=repl_years,
            battery_replacement_cost_eur_kWh=BATTERY_REPLACEMENT_COST_EUR_KWH,
        )
        incentives = ItalianIncentives(
            detrazione_fiscale_pct=detr_pct_for_engine,
            detrazione_spread_years=10,
            ammortamento_accelerato=False,
            transizione_5_0_pct=0.0,
        )

        base_inp = EnergyBalanceInput(
            pv_production_hourly_Wh=pv_hourly_Wh,
            demand_hourly_Wh=demand_hourly_Wh,
            battery=battery,
            tariff=tariff,
            peak_power_kWp=pv_kwp,
        )
        yearly = simulate_lifetime(base_inp, lifetime_years=analysis_years)
        econ = calc_economics(yearly, investment, opex, fin, incentives)

        yr1 = yearly[0]
        results.append(ScenarioResult(
            label=label,
            capex_eur=round(total_capex, 0),
            battery_kwh=bat_kwh,
            battery_replacement_years=repl_years,
            autoconsumo_pct=round(yr1.self_consumption_rate * 100, 1),
            autosufficienza_pct=round(yr1.self_sufficiency_rate * 100, 1),
            payback_anni=econ.payback_simple_yr,
            payback_scontato_anni=econ.payback_discounted_yr,
            npv_eur=econ.npv_eur,
            irr_pct=econ.irr_pct,
            detrazione_totale_eur=detr.detrazione_totale_eur,
            detrazione_effettiva_eur=detr.detrazione_effettiva_eur,
            risparmio_annuo_anno1_eur=econ.year1_saving_eur + econ.year1_export_revenue_eur,
            produzione_anno1_kwh=econ.year1_production_kWh,
            economics=econ,
        ))

    senza, con = results[0], results[1]

    # Trade-off
    delta_ac   = con.autoconsumo_pct - senza.autoconsumo_pct
    delta_as   = con.autosufficienza_pct - senza.autosufficienza_pct
    delta_pb   = con.payback_anni - senza.payback_anni
    delta_npv  = con.npv_eur - senza.npv_eur
    delta_cap  = con.capex_eur - senza.capex_eur
    batt_conv  = delta_npv > 0

    if batt_conv:
        nota = (
            f"La batteria da {battery_kwh:.0f} kWh aumenta l'autoconsumo "
            f"di {delta_ac:+.1f} pp e il suo NPV a {analysis_years} anni è "
            f"superiore di €{delta_npv:,.0f} rispetto al solo FV. "
            f"Il payback si allunga di {delta_pb:+.1f} anni."
        )
    else:
        nota = (
            f"⚠ La batteria da {battery_kwh:.0f} kWh NON è conveniente "
            f"nelle assunzioni correnti: NPV inferiore di €{abs(delta_npv):,.0f} "
            f"rispetto al solo FV (payback più lungo di {delta_pb:+.1f} anni). "
            f"L'autoconsumo aumenta di {delta_ac:+.1f} pp ma il costo aggiuntivo "
            f"(€{delta_cap:,.0f}) non è recuperato nell'orizzonte di analisi."
        )
    if con.battery_replacement_years:
        nota += (
            f" Sostituzione batteria prevista all'anno "
            f"{con.battery_replacement_years[0]} (soglia 80% capacità, LFP)."
        )

    tradeoff = BatteryTradeoff(
        delta_autoconsumo_pp=round(delta_ac, 1),
        delta_autosufficienza_pp=round(delta_as, 1),
        delta_payback_anni=round(delta_pb, 1),
        delta_npv_eur=round(delta_npv, 0),
        delta_capex_eur=round(delta_cap, 0),
        batteria_conveniente=batt_conv,
        nota=nota,
    )

    return BatteryScenarioComparison(
        senza_batteria=senza,
        con_batteria=con,
        tradeoff=tradeoff,
    )
