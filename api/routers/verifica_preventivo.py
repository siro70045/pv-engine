"""POST /verifica/preventivo — check up to three installer quotes (Livello 2).

Numbers only: price against the calibrated market band, promised production
against our simulation on the same roof, size against consumption. No
document is uploaded or stored.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from api.routers.estimate_casa import _ORIENTAMENTO_AZIMUTH
from api.schemas.estimate import CandidatoComune, ComuneRisolto, RangeLowHigh
from api.schemas.verifica_preventivo import (
    EsitoPreventivo,
    VerificaPreventivoRequest,
    VerificaPreventivoResponse,
)
from api.utils.cache import get_cached_tmy
from api.utils.limiter import limiter
from engine.pvgis_client import PVGISError
from engine.solar import PVSystem, SystemLosses, simulate_pv
from residential.quote_check import (
    BATTERY_BAND,
    OVERSIZE_RATIO,
    expected_cost,
    price_verdict,
    price_with_vat,
    production_verdict,
)
from residential.shading import classify as classify_shading
from school.comuni import Comune, lookup_comune

router = APIRouter(prefix="/verifica/preventivo", tags=["estimate"])


def _pv_only_price(price: float, battery_kwh: float) -> float:
    if battery_kwh <= 0:
        return price
    fixed, per_kwh = BATTERY_BAND["central"]
    return max(price - (fixed + per_kwh * battery_kwh), 0.0)

_AVVERTENZE_BASE = [
    "Verifichiamo i numeri del preventivo (prezzo, produzione promessa, taglia), non la qualità "
    "tecnica dei componenti né il progetto: per quello serve l'installatore o un tecnico abilitato.",
    "Fascia di prezzo: impianto chiavi in mano, IVA inclusa, su tetto a falda standard; tetti con "
    "vincoli o lavori extra (rimozione coppi, adeguamento del quadro elettrico) possono costare di più.",
    "Non conserviamo documenti: i dati inseriti servono solo per questo calcolo.",
]


@router.post("", response_model=VerificaPreventivoResponse,
             summary="Check installer quotes against market prices and our production estimate")
@limiter.limit("5/minute")
async def verifica_preventivo(request: Request, req: VerificaPreventivoRequest) -> VerificaPreventivoResponse:
    matches = lookup_comune(req.comune, req.provincia)
    if not matches:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Comune {req.comune!r} non trovato.")
    if len(matches) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": f"Esistono {len(matches)} comuni chiamati {req.comune!r}: specifica la provincia.",
                "candidati": [
                    CandidatoComune(nome=c.nome, provincia=c.sigla_provincia, regione=c.regione).model_dump()
                    for c in matches
                ],
            },
        )
    comune: Comune = matches[0]
    try:
        tmy, _ = get_cached_tmy(comune.lat, comune.lon)
    except PVGISError as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Il servizio meteo europeo PVGIS non è raggiungibile al momento ({e}). Riprova tra qualche minuto.",
        )

    azimuth = _ORIENTAMENTO_AZIMUTH[req.orientamento]
    ombra = (
        classify_shading(req.ombra_ostacoli, req.ombra_lati, req.ombra_durata)
        if req.ombra_ostacoli is not None else None
    )
    losses = SystemLosses(shading_near=ombra.loss_pct / 100.0) if ombra is not None else SystemLosses()

    prod_cache: dict[float, float] = {}

    def _production(kwp: float) -> float:
        key = round(kwp, 2)
        if key not in prod_cache:
            res = simulate_pv(
                tmy,
                PVSystem(peak_power_kWp=kwp, tilt_deg=req.inclinazione_deg, azimuth_deg=azimuth,
                         system_losses=losses),
                mounting="close_roof",
            )
            prod_cache[key] = res.production_annual_kWh
        return prod_cache[key]

    esiti: list[EsitoPreventivo] = []
    for i, p in enumerate(req.preventivi):
        label = p.etichetta.strip() or f"Preventivo {chr(ord('A') + i)}"
        price = price_with_vat(p.prezzo_totale_eur, p.prezzo_iva_inclusa)
        exp = expected_cost(p.kwp, p.batteria_kwh)
        pv_key, pv_msg = price_verdict(price, exp)
        prod = _production(p.kwp)

        g_prod = n_prod = None
        scarto_prod = None
        if p.produzione_promessa_kwh is not None:
            g_prod, ratio, n_prod = production_verdict(p.produzione_promessa_kwh, prod)
            scarto_prod = round(100.0 * (ratio - 1.0), 1)

        sovra = nota_taglia = None
        if req.consumo_annuo_kwh is not None:
            r = prod / req.consumo_annuo_kwh
            sovra = r > OVERSIZE_RATIO
            nota_taglia = (
                f"L'impianto produrrebbe circa {r:.1f} volte il tuo consumo annuo: gran parte dell'energia "
                "andrebbe venduta alla rete a circa 0,06 €/kWh, molto meno di quanto vale se la consumi. "
                "Chiedi anche un preventivo per una taglia più piccola."
                if sovra else
                f"La taglia è proporzionata al tuo consumo (produzione pari a circa il {100 * r:.0f}% del consumo annuo)."
            )

        esiti.append(EsitoPreventivo(
            etichetta=label,
            kwp=p.kwp,
            batteria_kwh=p.batteria_kwh,
            prezzo_iva_inclusa_eur=round(price, 0),
            eur_per_kwp=round(price / p.kwp, 0),
            prezzo_atteso_eur=round(exp.central, -2),
            prezzo_banda_eur=RangeLowHigh(low=round(exp.low, -2), high=round(exp.high, -2)),
            scarto_prezzo_pct=round(100.0 * (price / exp.central - 1.0), 1),
            giudizio_prezzo=pv_key,
            nota_prezzo=pv_msg,
            produzione_stimata_kwh=round(prod, 0),
            produzione_promessa_kwh=p.produzione_promessa_kwh,
            scarto_produzione_pct=scarto_prod,
            giudizio_produzione=g_prod,
            nota_produzione=n_prod,
            # PV-only price: the battery doesn't change production, so remove its
            # central estimated cost before comparing quotes on € per kWh.
            eur_per_kwh_annuo=round(_pv_only_price(price, p.batteria_kwh) / prod, 2) if prod > 0 else 0.0,
            sovradimensionato=sovra,
            nota_taglia=nota_taglia,
        ))

    piu_conveniente = nota_confronto = None
    if len(esiti) > 1:
        # An oversized system gets more kWh per euro but sells most of them at
        # ~0,06 €/kWh: never call it the best deal on € per kWh alone.
        candidati = [e for e in esiti if not e.sovradimensionato] or esiti
        best = min(candidati, key=lambda e: e.eur_per_kwh_annuo)
        piu_conveniente = best.etichetta
        nota_confronto = (
            f"{best.etichetta} è quello che ti dà più energia per ogni euro speso "
            f"({best.eur_per_kwh_annuo:.2f} € per ogni kWh prodotto in un anno, secondo la nostra stima"
            + (", al netto di una stima del costo della batteria" if any(e.batteria_kwh > 0 for e in esiti) else "")
            + "). "
        )
        esclusi = [e.etichetta for e in esiti if e.sovradimensionato and e is not best]
        if esclusi and len(candidati) < len(esiti):
            nota_confronto += (
                f"Abbiamo escluso dal confronto {', '.join(esclusi)}: produrrebbe molto più di quanto consumi, "
                "e l'energia in più vale poco. "
            )
        nota_confronto += (
            "È un confronto sul prezzo: contano anche garanzie, componenti e serietà dell'installatore. "
            "Il report completo calcola quanto conviene ogni preventivo con il tuo consumo."
        )

    avvertenze = list(_AVVERTENZE_BASE)
    if ombra is not None:
        avvertenze.append(
            f"Produzione stimata con ombre vicine: {ombra.label.lower()} "
            f"(perdita {ombra.loss_pct:.0f}%, stima nostra)."
        )
    if any(p.batteria_kwh > 0 for p in req.preventivi):
        avvertenze.append(
            "Per i preventivi con batteria la fascia di prezzo include la batteria; "
            "la produzione dei pannelli non cambia con la batteria."
        )

    return VerificaPreventivoResponse(
        comune_risolto=ComuneRisolto(
            nome=comune.nome, provincia=comune.sigla_provincia,
            regione=comune.regione, zona_climatica=comune.zona_climatica,
        ),
        esiti=esiti,
        piu_conveniente=piu_conveniente,
        nota_confronto=nota_confronto,
        avvertenze=avvertenze,
    )
