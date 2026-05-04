"""
app.py — PV Engine  |  5-step wizard interface

Launch:
    streamlit run app.py

Dependencies:
    pip install streamlit plotly pandas folium streamlit-folium
"""

from __future__ import annotations
import os, sys, json, math, traceback
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).parent))

from main import (
    SimulationInput, LocationInput, BuildingInput,
    PVSystemInput, BatteryInput, TariffInput,
    GasSystemInput, HeatPumpInput,
    run_simulation, result_to_dict,
)
from engine.economics import (
    InvestmentCost, OperatingCost, FinancialParameters, ItalianIncentives,
)
from engine.pvgis_client import parse_pvgis_csv, PVGISError
from geocoding import (
    geocode, calc_bearing, bearing_to_label, azimuth_quality_note,
    GeocodingError,
)
from buildings import fetch_buildings, polygon_area_m2, kwp_from_area
from branding.meridiana_theme import apply_meridiana_theme, render_header

# ══════════════════════════════════════════════════════════════════════════════
# PAGE
# ══════════════════════════════════════════════════════════════════════════════

apply_meridiana_theme(page_title="Meridiana — Residenziale")
render_header(subtitle="Simulatore residenziale")

MESI = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
STEPS = ["Location","Building","Bills","System","Results"]

st.markdown("""
<style>
.block-container { max-width:900px; padding:1.5rem 2rem 4rem; }
header[data-testid="stHeader"]          { display:none; }
section[data-testid="stSidebar"]        { display:none; }
.pv-steps { display:flex; align-items:center; margin-bottom:2rem; }
.pv-step  { flex:1; text-align:center; position:relative; }
.pv-step-circle {
    width:30px; height:30px; border-radius:50%; margin:0 auto 6px;
    display:flex; align-items:center; justify-content:center;
    font-size:13px; font-weight:600;
}
.done   .pv-step-circle { background:#1565c0; color:#fff; }
.active .pv-step-circle { background:#1565c0; color:#fff;
    box-shadow:0 0 0 4px #dbeafe; }
.todo   .pv-step-circle { background:#e5e7eb; color:#9ca3af; }
.pv-step-label { font-size:11px; color:#6b7280; }
.active .pv-step-label  { color:#1565c0; font-weight:600; }
.done   .pv-step-label  { color:#1565c0; }
.pv-step::after {
    content:''; position:absolute; top:15px; left:58%; width:84%;
    height:2px; background:#e5e7eb; z-index:0;
}
.pv-step:last-child::after { display:none; }
.done::after { background:#1565c0; }
.good { background:#f0fdf4; border-left:3px solid #16a34a;
        padding:.7rem 1rem; border-radius:6px;
        font-size:14px; color:#14532d; margin:.4rem 0; }
.warn { background:#fffbeb; border-left:3px solid #d97706;
        padding:.7rem 1rem; border-radius:6px;
        font-size:14px; color:#78350f; margin:.4rem 0; }
.err  { background:#fef2f2; border-left:3px solid #dc2626;
        padding:.7rem 1rem; border-radius:6px;
        font-size:14px; color:#7f1d1d; margin:.4rem 0; }
.slbl { font-size:12px; font-weight:600; color:#374151;
        text-transform:uppercase; letter-spacing:.05em;
        margin:1.1rem 0 .4rem; }
.sc-card { border:1.5px solid #e5e7eb; border-radius:10px;
           padding:1.1rem; text-align:center; }
.sc-best  { border-color:#1565c0; background:#eff6ff; }
.sc-title { font-size:14px; font-weight:600; color:#111827; margin-bottom:.2rem; }
.sc-kwp   { font-size:26px; font-weight:700; color:#1565c0; }
.sc-sub   { font-size:12px; color:#6b7280; }
.badge    { display:inline-block; background:#1565c0; color:#fff;
            font-size:10px; font-weight:600; padding:2px 8px;
            border-radius:999px; margin-bottom:.4rem; }
.kpi { background:#f9fafb; border-radius:7px; padding:.65rem .9rem; text-align:center; }
.kpi-lbl { font-size:11px; color:#6b7280; margin-bottom:.15rem; }
.kpi-val { font-size:20px; font-weight:600; color:#111827; }
.green .kpi-val { color:#15803d; }
.blue  .kpi-val { color:#1565c0; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def ss(k, d=None): return st.session_state.get(k, d)
def sset(**kw):    st.session_state.update(kw)

def go_to(n):
    sset(step=n); st.rerun()

def fmt_eur(v): return f"€\u202f{v:,.0f}".replace(",",".")

def kpi_md(label, val, unit="", cls=""):
    return (f'<div class="kpi {cls}"><div class="kpi-lbl">{label}</div>'
            f'<div class="kpi-val">{val}'
            f'<span style="font-size:12px;font-weight:400;color:#6b7280">'
            f' {unit}</span></div></div>')


def estimate_pv_cost(kwp: float) -> int:
    """
    All-in residential PV installation cost [€] — monotonic marginal-cost model.
    Calibrated on Italian market prices 2024. Accuracy: ±15%.

    Residential: 1,600–2,500 €/kWp all-in
    """
    TIERS_RES = [(3, 2100), (6, 1800), (20, 1500),
                 (50, 1250), (100, 1050), (250, 900), (float("inf"), 800)]
    cost, remaining, prev = 0.0, kwp, 0.0
    for thresh, eur_kwp in TIERS_RES:
        if remaining <= 0:
            break
        in_tier = min(remaining, thresh - prev)
        cost += in_tier * eur_kwp
        remaining -= in_tier
        prev = thresh
    cost += max(800, kwp * 18)   # design + permits + grid connection
    return int(round(cost, -2))


def auto_scenarios(kwp_max, annual_kwh):
    Y = 1200.0
    mn = max(1.5, round(annual_kwh*0.35/Y*2)/2)
    op = min(kwp_max, max(mn, round(annual_kwh*0.75/Y*2)/2))
    mx = max(op, kwp_max)
    mn = min(mn, mx)
    op = max(mn, min(op, mx))
    return mn, op, mx


# ══════════════════════════════════════════════════════════════════════════════
# PROGRESS BAR
# ══════════════════════════════════════════════════════════════════════════════

def render_progress(cur):
    html = '<div class="pv-steps">'
    for i, lbl in enumerate(STEPS, 1):
        cls  = "done" if i < cur else ("active" if i == cur else "todo")
        icon = "✓" if i < cur else str(i)
        html += (f'<div class="pv-step {cls}">'
                 f'<div class="pv-step-circle">{icon}</div>'
                 f'<div class="pv-step-label">{lbl}</div></div>')
    html += '</div>'
    st.markdown(html, unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# MAP
# ══════════════════════════════════════════════════════════════════════════════

def _blds(m, buildings):
    try:
        import folium
        for b in buildings:
            if len(b.coords) >= 3:
                folium.Polygon(b.coords, color="#1565c0", weight=1.5,
                               fill=True, fill_color="#93c5fd",
                               fill_opacity=0.3).add_to(m)
    except Exception:
        pass


def map_phase1(geo_lat, geo_lon, buildings, h=520):
    try:
        import folium
        from streamlit_folium import st_folium
        m = folium.Map([geo_lat, geo_lon], zoom_start=18, tiles="OpenStreetMap")
        _blds(m, buildings)
        folium.CircleMarker([geo_lat, geo_lon], radius=5, color="#6b7280",
                            fill=True, fill_color="#d1d5db",
                            fill_opacity=0.8).add_to(m)
        return st_folium(m, height=h, use_container_width=True,
                         returned_objects=["last_clicked"])
    except ImportError:
        st.warning("Install `folium` and `streamlit-folium` to enable the map.")
        return None


def map_phase2(c_lat, c_lon, buildings, s_lat=None, s_lon=None, h=520):
    try:
        import folium
        from streamlit_folium import st_folium
        m = folium.Map([c_lat, c_lon], zoom_start=18, tiles="OpenStreetMap")
        _blds(m, buildings)
        folium.CircleMarker([c_lat, c_lon], radius=10, color="#1e40af",
                            weight=2.5, fill=True, fill_color="#3b82f6",
                            fill_opacity=0.9, tooltip="Building centre").add_to(m)
        if s_lat is not None:
            folium.CircleMarker([s_lat, s_lon], radius=8, color="#c2410c",
                                weight=2, fill=True, fill_color="#f97316",
                                fill_opacity=0.9, tooltip="Sun-facing side").add_to(m)
            folium.PolyLine([[c_lat,c_lon],[s_lat,s_lon]],
                            color="#f97316", weight=3, dash_array="6 4").add_to(m)
        return st_folium(m, height=h, use_container_width=True,
                         returned_objects=["last_clicked"])
    except ImportError:
        st.warning("Install `folium` and `streamlit-folium` to enable the map.")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — LOCATION
# ══════════════════════════════════════════════════════════════════════════════

def step_posizione():
    st.markdown("### Location")
    st.caption("Search for the address, then locate your building on the map.")

    # Geocoding
    ca, cb = st.columns([4,1])
    with ca:
        addr = st.text_input("Address", value=ss("address_text",""),
                             placeholder="E.g. 14 High Street, London",
                             label_visibility="collapsed")
    with cb:
        search = st.button("Search", use_container_width=True)

    if search and addr.strip():
        with st.spinner("Searching…"):
            try:
                results = geocode(addr, country_codes="it")
                if results:
                    b = results[0]
                    sset(geo_results=results, geo_selected=0,
                         geo_lat=b.lat, geo_lon=b.lon, address_text=addr,
                         map_phase="p1", center_lat=None, center_lon=None,
                         sun_lat=None, sun_lon=None, azimuth_from_map=None,
                         last_click_key="", buildings_cache=None,
                         tmy_data=None, tmy_key=None, tmy_error=None)
                    with st.spinner("Loading building outlines…"):
                        sset(buildings_cache=fetch_buildings(b.lat, b.lon, 150))
                else:
                    st.warning("No results — try adding city and postcode.")
            except GeocodingError as e:
                st.error(f"Geocoding error: {e}")

    # Disambiguazione
    geo_results = ss("geo_results", [])
    if len(geo_results) > 1:
        idx = st.selectbox("Multiple results found — choose the correct one:",
                           range(len(geo_results)),
                           format_func=lambda i: geo_results[i].display_name[:80],
                           key="geo_sel")
        if idx != ss("geo_selected", 0):
            b = geo_results[idx]
            sset(geo_selected=idx, geo_lat=b.lat, geo_lon=b.lon,
                 map_phase="p1", center_lat=None, center_lon=None,
                 sun_lat=None, sun_lon=None, azimuth_from_map=None,
                 last_click_key="", buildings_cache=None,
                 tmy_data=None, tmy_key=None, tmy_error=None)
            with st.spinner("Loading outlines…"):
                sset(buildings_cache=fetch_buildings(b.lat, b.lon, 150))

    geo_lat   = ss("geo_lat")
    geo_lon   = ss("geo_lon")
    phase     = ss("map_phase", "p1")
    buildings = ss("buildings_cache") or []

    if geo_lat is None:
        st.info("Enter the address above and press **Search**.")
        return

    # ── Fase 1 ────────────────────────────────────────────────────────────────
    if phase == "p1":
        nb = len(buildings)
        st.markdown(
            f"**Step 1 of 2** — Click on the centre of your building."
            + (f" ({nb} OSM building outlines loaded)" if nb else "")
        )
        md = map_phase1(geo_lat, geo_lon, buildings)
        if md and md.get("last_clicked"):
            ck  = md["last_clicked"]
            key = f"{ck['lat']:.6f},{ck['lng']:.6f}"
            if key != ss("last_click_key",""):
                sset(last_click_key=key,
                     center_lat=ck["lat"], center_lon=ck["lng"])

        c_lat = ss("center_lat")
        if c_lat is not None:
            st.markdown(
                f'<div class="good">✓ Centre selected: '
                f'{c_lat:.5f}°N, {ss("center_lon"):.5f}°E</div>',
                unsafe_allow_html=True)

            st.markdown('<div class="slbl">How much of the roof is yours?</div>',
                        unsafe_allow_html=True)
            share_opts = {
                "Entire property (100%)":          100,
                "Half — semi-detached (50%)":       50,
                "One third — 3-unit terrace (33%)": 33,
                "One quarter — 4-unit terrace (25%)": 25,
                "Custom":                            0,
            }
            slbl = st.radio("Roof share", list(share_opts.keys()),
                            index=0, label_visibility="collapsed",
                            horizontal=False)
            share = share_opts[slbl]
            if share == 0:
                share = st.slider("Custom percentage", 10, 100, 50, 5, format="%d%%")
            sset(roof_share_pct=share)

            if st.button("Next — indicate the sun-facing side →",
                         type="primary", use_container_width=True):
                sset(map_phase="p2", last_click_key="")
                st.rerun()
        else:
            st.caption("No point selected — click on the map.")

    # ── Fase 2 ────────────────────────────────────────────────────────────────
    elif phase == "p2":
        c_lat = ss("center_lat")
        c_lon = ss("center_lon")
        st.markdown("**Step 2 of 2** — Click on the roof side facing the sun.")
        st.caption("The orange arrow shows the direction you are selecting.")

        md = map_phase2(c_lat, c_lon, buildings, ss("sun_lat"), ss("sun_lon"))
        if md and md.get("last_clicked"):
            ck  = md["last_clicked"]
            key = f"{ck['lat']:.6f},{ck['lng']:.6f}"
            if key != ss("last_click_key",""):
                bearing = calc_bearing(c_lat, c_lon, ck["lat"], ck["lng"])
                sset(last_click_key=key, sun_lat=ck["lat"], sun_lon=ck["lng"],
                     azimuth_from_map=bearing)

        az = ss("azimuth_from_map")
        if az is not None:
            lbl        = bearing_to_label(az)
            level, note = azimuth_quality_note(az)
            icon = "✅" if level=="good" else ("⚠️" if level=="ok" else "❌")
            cls  = "good" if level=="good" else "warn"
            st.markdown(
                f'<div class="{cls}">{icon} <strong>Azimuth: {lbl}</strong>'
                f'<br><span style="font-size:13px">{note}</span></div>',
                unsafe_allow_html=True)

        na, nb_ = st.columns(2)
        with na:
            if st.button("← Back to step 1", use_container_width=True):
                sset(map_phase="p1", center_lat=None, center_lon=None,
                     sun_lat=None, sun_lon=None, azimuth_from_map=None,
                     last_click_key="")
                st.rerun()
        with nb_:
            ok = az is not None
            if st.button("Confirm", type="primary" if ok else "secondary",
                         use_container_width=True, disabled=not ok):
                with st.spinner("Downloading climate data from PVGIS…"):
                    try:
                        import engine.pvgis_client as _pvc
                        tmy = _pvc.fetch_tmy(lat=c_lat, lon=c_lon, use_cache=True)
                        sset(tmy_data=tmy,
                             tmy_key=f"{c_lat:.5f},{c_lon:.5f}",
                             tmy_error=None)
                    except Exception as e:
                        sset(tmy_data=None, tmy_key=None, tmy_error=str(e))
                sset(map_phase="done")
                st.rerun()

    # ── Done ──────────────────────────────────────────────────────────────────
    elif phase == "done":
        c_lat = ss("center_lat")
        c_lon = ss("center_lon")
        az    = ss("azimuth_from_map")
        lbl   = bearing_to_label(az)
        level, note = azimuth_quality_note(az)
        icon  = "✅" if level=="good" else "⚠️"
        tmy   = ss("tmy_data")
        terr  = ss("tmy_error")
        share = ss("roof_share_pct", 100)

        rc = st.columns(3)
        rc[0].metric("Coordinate", f"{c_lat:.4f}°N")
        rc[1].metric("Azimuth", f"{icon} {lbl}")
        rc[2].metric("Quota tetto", f"{share}%")

        if terr:
            st.markdown(
                f'<div class="err">❌ PVGIS unreachable: {terr}<br>'
                f'Use the manual file upload below.</div>',
                unsafe_allow_html=True)
            with st.expander("📂 Upload TMY file manually"):
                tmy_file = st.file_uploader("TMY CSV (from PVGIS website)", type=["csv"],
                                            label_visibility="collapsed")
                if tmy_file:
                    try:
                        tm = parse_pvgis_csv(
                            tmy_file.read().decode("utf-8","replace"))
                        sset(tmy_data=tm, tmy_key="manual", tmy_error=None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Invalid file: {e}")
        elif tmy:
            m_ = tmy.monthly
            ghi = sum(m_.GHI_monthly)
            t_m = sum(m_.T_avg)/12
            bm  = m_.GHI_monthly.index(max(m_.GHI_monthly))
            st.markdown(
                f'<div class="good"><strong>Climate data ready</strong> — '
                f'GHI {ghi:.0f} kWh/m²/yr · T̄ {t_m:.1f}°C · '
                f'Best month: {MESI[bm]} ({m_.GHI_monthly[bm]:.0f} kWh/m²)'
                f'</div>', unsafe_allow_html=True)

        ma, mb = st.columns([1,2])
        with ma:
            if st.button("Edit location", use_container_width=True):
                sset(map_phase="p1", center_lat=None, center_lon=None,
                     sun_lat=None, sun_lon=None, azimuth_from_map=None,
                     last_click_key="", tmy_data=None, tmy_key=None,
                     tmy_error=None)
                st.rerun()
        with mb:
            ok = ss("tmy_data") is not None
            if st.button("Continue →", type="primary" if ok else "secondary",
                         use_container_width=True, disabled=not ok):
                go_to(2)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — BUILDING
# ══════════════════════════════════════════════════════════════════════════════

def step_edificio():
    st.markdown("### Building")

    st.markdown('<div class="slbl">Residential building type</div>',
                unsafe_allow_html=True)
    subtype_opts = {
        "Detached house":  "detached",
        "Terraced house":  "terraced",
        "Apartment":       "apartment",
    }
    prev_sub = ss("building_subtype", "detached")
    sublbl = st.radio("Subtype", list(subtype_opts.keys()),
                      index=list(subtype_opts.values()).index(prev_sub),
                      label_visibility="collapsed", horizontal=True)
    bsubtype = subtype_opts[sublbl]
    sset(building_subtype=bsubtype, building_type="residential")

    # Floor and attic — only for apartment / terraced
    if bsubtype in ("apartment", "terraced"):
        st.markdown('<div class="slbl">Floor</div>', unsafe_allow_html=True)
        floor_opts = {
            "Ground floor": 0,
            "1st floor":    1,
            "2nd floor":    2,
            "3rd floor":    3,
            "4th or higher": 4,
        }
        prev_floor = ss("floor_number", 0)
        flbl = st.radio("Floor", list(floor_opts.keys()),
                        index=list(floor_opts.values()).index(
                            min(prev_floor, 4)),
                        label_visibility="collapsed", horizontal=True)
        floor_num = floor_opts[flbl]
        is_top = st.checkbox("Top-floor unit or attic",
                             value=bool(ss("is_top_floor", False)))
        sset(floor_number=floor_num, is_top_floor=is_top)
    else:
        # Detached house: all surfaces exposed
        sset(floor_number=0, is_top_floor=True)

    st.divider()

    st.markdown('<div class="slbl">Number of residents</div>',
                unsafe_allow_html=True)
    n_occ = st.number_input("Number of residents", min_value=1, max_value=10,
                             value=int(ss("n_occupants", 2)),
                             label_visibility="collapsed")
    sset(n_occupants=int(n_occ))

    st.divider()

    # Air conditioning
    has_ac = st.toggle("Air conditioning installed",
                       value=bool(ss("has_ac", False)))
    sset(has_ac=has_ac)
    if has_ac:
        ac_kw = st.slider("Estimated cooling capacity [kW]",
                          min_value=1.5, max_value=12.0,
                          value=float(ss("ac_kw", 3.5)), step=0.5)
        sset(ac_kw=ac_kw)

    st.divider()

    st.markdown('<div class="slbl">Building footprint area</div>',
                unsafe_allow_html=True)
    buildings = ss("buildings_cache") or []
    c_lat = ss("center_lat")
    c_lon = ss("center_lon")
    osm_area = None
    if buildings and c_lat:
        from buildings import closest_building
        cl = closest_building(c_lat, c_lon, buildings)
        if cl and len(cl.coords) >= 3:
            osm_area = polygon_area_m2(cl.coords)

    if osm_area and osm_area > 10:
        st.caption(f"Estimated from OSM building outline: **{osm_area:.0f} m²**")
        if st.checkbox("Use this estimate", value=True, key="use_osm_area"):
            floor_area = osm_area
        else:
            floor_area = st.number_input("Area [m²]",
                                         value=int(osm_area), min_value=10, step=10)
    else:
        floor_area = st.number_input("Gross floor area [m²]",
                                     value=int(ss("floor_area", 150)),
                                     min_value=10, step=10)
    sset(floor_area=float(floor_area))

    st.divider()

    st.markdown('<div class="slbl">How is the home occupied?</div>',
                unsafe_allow_html=True)

    # Residential occupancy profiles — the key dimension for PV self-consumption
    # is WHEN people are home during the day (when PV produces).
    # We define 5 realistic household types.
    #
    # (occ_days, hour_start)
    # occ_days   : average days/week with significant occupancy (scales kWh)
    # hour_start : hour from which full occupancy begins on occupied days
    #              (0 = all day, 14 = home from 14:00 onwards)
    #
    # Note: hours before hour_start use a 25% standby load
    # (fridge, router, standby devices — always present even when empty).

    occ_opts = {
        "Home all day — retired, remote worker, carer":
            (7.0, 0,
             "High daytime consumption → best match for PV. Expected self-consumption: 45–65%."),

        "Home mornings & evenings — short commute or school run":
            (6.5, 9,
             "Some daytime load (appliances, partial WFH). Expected self-consumption: 35–50%."),

        "Away most of the day — office worker (back ~17–18h)":
            (5.0, 17,
             "Low daytime consumption on weekdays, higher at weekends. Expected self-consumption: 20–35%."),

        "Mostly away until mid-afternoon — back ~14–15h":
            (5.5, 14,
             "Moderate daytime load (appliances, some WFH on some days). Expected self-consumption: 28–42%."),

        "Holiday home / rarely occupied":
            (1.5, 0,
             "Minimal consumption — mostly standby. PV economics depend almost entirely on export."),
    }

    # Recover previously saved selection
    prev_key = next(
        (k for k,(d,h,_) in occ_opts.items()
         if d==ss("occ_days",7.0) and h==ss("occ_hour",0)),
        list(occ_opts.keys())[0]
    )
    olbl = st.radio("Occupancy profile", list(occ_opts.keys()),
                    index=list(occ_opts.keys()).index(prev_key),
                    label_visibility="collapsed")
    occ_days, occ_hour, occ_hint = occ_opts[olbl]
    sset(occ_days=occ_days, occ_hour=occ_hour, occ_label=olbl)

    st.caption(occ_hint)

    st.divider()
    ca, cb = st.columns(2)
    with ca:
        if st.button("← Back", use_container_width=True): go_to(1)
    with cb:
        if st.button("Continue →", type="primary", use_container_width=True): go_to(3)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — BILLS
# ══════════════════════════════════════════════════════════════════════════════

def step_bollette():
    st.markdown("### Utility bills")
    st.caption(
        "Enter your consumption and the total amount you pay — "
        "no need to calculate unit prices."
    )

    # ── Input mode: monthly vs annual ────────────────────────────────────────
    st.markdown('<div class="slbl">Input mode</div>', unsafe_allow_html=True)
    mode = st.radio(
        "mode",
        ["Monthly breakdown", "Annual total only"],
        index=0 if ss("bill_mode","monthly") == "monthly" else 1,
        horizontal=True,
        label_visibility="collapsed",
    )
    sset(bill_mode="monthly" if mode == "Monthly breakdown" else "annual")

    st.divider()

    # ── Electricity ───────────────────────────────────────────────────────────
    st.markdown('<div class="slbl">Electricity</div>', unsafe_allow_html=True)

    if mode == "Monthly breakdown":
        st.caption("Consumption in kWh — from the monthly or quarterly bill.")
        prev_el = ss("monthly_el", [0]*12)
        el_vals = []
        row1, row2 = st.columns(6), st.columns(6)
        for i in range(12):
            col = row1[i] if i < 6 else row2[i-6]
            with col:
                v = st.number_input(MESI[i], value=int(prev_el[i]),
                                    min_value=0, step=10, key=f"el_{i}")
                el_vals.append(v)
        total_el = sum(el_vals)
        sset(monthly_el=el_vals)

        # Bill cost — monthly or annual
        st.caption("Total amount paid (all-in, as shown on bill).")
        bc1, bc2 = st.columns(2)
        with bc1:
            el_bill_annual = st.number_input(
                "Total annual electricity cost [€/year]",
                value=float(ss("el_bill_annual", 0)),
                min_value=0.0, step=10.0, format="%.0f",
                help="Sum of all electricity bills over 12 months",
            )
        with bc2:
            el_bill_monthly = st.number_input(
                "Average monthly electricity cost [€/month]",
                value=float(ss("el_bill_monthly", 0)),
                min_value=0.0, step=5.0, format="%.0f",
                help="Typical monthly bill — we will multiply by 12",
            )
        # Derive annual cost: prefer annual if filled, else monthly×12
        if el_bill_annual > 0:
            _el_cost = el_bill_annual
        elif el_bill_monthly > 0:
            _el_cost = el_bill_monthly * 12
        else:
            _el_cost = 0.0
        sset(el_bill_annual=el_bill_annual,
             el_bill_monthly=el_bill_monthly)

    else:  # Annual total only
        st.caption("Enter your annual electricity consumption and cost.")
        ac1, ac2 = st.columns(2)
        with ac1:
            total_el = st.number_input(
                "Annual consumption [kWh/year]",
                value=int(ss("annual_el_kwh", sum(ss("monthly_el",[0]*12)))),
                min_value=0, step=100,
            )
        with ac2:
            el_bill_annual_only = st.number_input(
                "Annual electricity cost [€/year]",
                value=float(ss("el_bill_annual", 0)),
                min_value=0.0, step=50.0, format="%.0f",
            )
        _el_cost = el_bill_annual_only
        el_vals = [total_el / 12] * 12
        sset(monthly_el=el_vals, annual_el_kwh=total_el,
             el_bill_annual=el_bill_annual_only)

    # Derive implied unit price for the engine (cost / kWh, fallback 0.22)
    if _el_cost > 0 and total_el > 0:
        _el_price = _el_cost / total_el
    else:
        _el_price = 0.22   # fallback: typical Italian all-in price
    sset(el_price=_el_price, monthly_el=el_vals)

    # Summary row
    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("Annual consumption", f"{total_el:,.0f} kWh")
    sc2.metric("Annual cost",
               f"€ {_el_cost:,.0f}" if _el_cost > 0 else "not entered")
    sc3.metric("Implied unit price",
               f"{_el_price:.3f} €/kWh" if _el_cost > 0 else "default 0.22 €/kWh")

    # Export rate (kept but with simpler label)
    st.caption("Grid export rate — check your contract or use the default.")
    exp_price = st.number_input(
        "Export / feed-in rate [€/kWh]",
        value=float(ss("export_price", 0.08)),
        min_value=0.0, max_value=0.30, format="%.3f",
        help="Typical range: 0.07–0.12 €/kWh (GSE ritiro dedicato or SSP)",
    )
    sset(export_price=exp_price)

    st.divider()

    # ── Gas ───────────────────────────────────────────────────────────────────
    has_gas = st.toggle("The building also uses gas",
                        value=bool(ss("has_gas", False)))
    sset(has_gas=has_gas)

    if has_gas:
        st.markdown('<div class="slbl">Gas</div>', unsafe_allow_html=True)

        if mode == "Monthly breakdown":
            st.caption("Consumption in Sm³ — from the bi-monthly or quarterly bill.")
            prev_gas = ss("monthly_gas", [0]*12)
            gas_vals = []
            gr1, gr2 = st.columns(6), st.columns(6)
            for i in range(12):
                col = gr1[i] if i < 6 else gr2[i-6]
                with col:
                    v = st.number_input(MESI[i], value=int(prev_gas[i]),
                                        min_value=0, step=5, key=f"gas_{i}")
                    gas_vals.append(v)
            total_gas = sum(gas_vals)
            sset(monthly_gas=gas_vals)

            st.caption("Total amount paid (all-in).")
            gb1, gb2 = st.columns(2)
            with gb1:
                gas_bill_annual = st.number_input(
                    "Total annual gas cost [€/year]",
                    value=float(ss("gas_bill_annual", 0)),
                    min_value=0.0, step=20.0, format="%.0f",
                )
            with gb2:
                gas_bill_monthly = st.number_input(
                    "Average monthly gas cost [€/month]",
                    value=float(ss("gas_bill_monthly", 0)),
                    min_value=0.0, step=10.0, format="%.0f",
                )
            if gas_bill_annual > 0:
                _gas_cost = gas_bill_annual
            elif gas_bill_monthly > 0:
                _gas_cost = gas_bill_monthly * 12
            else:
                _gas_cost = 0.0
            sset(gas_bill_annual=gas_bill_annual,
                 gas_bill_monthly=gas_bill_monthly)

        else:
            st.caption("Annual gas consumption and cost.")
            ga1, ga2 = st.columns(2)
            with ga1:
                total_gas = st.number_input(
                    "Annual consumption [Sm³/year]",
                    value=int(ss("annual_gas_sm3",
                                  sum(ss("monthly_gas",[0]*12)))),
                    min_value=0, step=50,
                )
            with ga2:
                gas_bill_annual_only = st.number_input(
                    "Annual gas cost [€/year]",
                    value=float(ss("gas_bill_annual", 0)),
                    min_value=0.0, step=50.0, format="%.0f",
                )
            _gas_cost = gas_bill_annual_only
            gas_vals  = [total_gas / 12] * 12
            sset(monthly_gas=gas_vals, annual_gas_sm3=total_gas,
                 gas_bill_annual=gas_bill_annual_only)

        # Derive gas unit price
        if _gas_cost > 0 and total_gas > 0:
            _gas_price = _gas_cost / total_gas
        else:
            _gas_price = 0.95   # fallback
        sset(gas_price=_gas_price)

        # Summary
        gs1, gs2, gs3 = st.columns(3)
        gs1.metric("Annual consumption", f"{total_gas:,.0f} Sm³")
        gs2.metric("Annual cost",
                   f"€ {_gas_cost:,.0f}" if _gas_cost > 0 else "not entered")
        gs3.metric("Implied unit price",
                   f"{_gas_price:.3f} €/Sm³" if _gas_cost > 0 else "default 0.95 €/Sm³")

        # Heating terminals
        terminal = st.selectbox(
            "Heating distribution system",
            ["fan_coil", "radiant_floor", "old_radiator"],
            index=["fan_coil","radiant_floor","old_radiator"].index(
                ss("terminal_type","fan_coil")),
            format_func=lambda x: {
                "fan_coil":      "Fan coil units (supply 40–45°C)",
                "radiant_floor": "Underfloor heating (supply 35°C)",
                "old_radiator":  "Traditional radiators (supply 70°C) — not ideal for heat pump",
            }[x],
        )
        sset(terminal_type=terminal)
        if terminal == "old_radiator":
            st.markdown(
                '<div class="warn">With traditional radiators at 70–80°C the heat pump '
                'seasonal COP drops to approximately 2.0, significantly reducing savings. '
                'Consider replacing with fan coil or underfloor before installing a heat pump.</div>',
                unsafe_allow_html=True)

    st.divider()
    ca, cb = st.columns(2)
    with ca:
        if st.button("← Back", use_container_width=True): go_to(2)
    with cb:
        ok = total_el > 0
        if st.button("Calculate system →",
                     type="primary" if ok else "secondary",
                     use_container_width=True, disabled=not ok):
            go_to(4)



def _run_sim(kwp, label):
    """Run one simulation and return KPI dict. Returns None on error."""
    tmy = ss("tmy_data")
    if tmy is None:
        return None
    try:
        import engine.pvgis_client as _pvc, main as _m
        _pvc.fetch_tmy = lambda lat=None, lon=None, use_cache=True: tmy
        _m.fetch_tmy   = lambda lat=None, lon=None, use_cache=True: tmy

        has_gas    = ss("has_gas", False)
        ann_el     = sum(ss("monthly_el",[0]*12))
        total_gas  = sum(ss("monthly_gas",[0]*12))
        capex      = estimate_pv_cost(kwp)

        inc = ItalianIncentives(
            detrazione_fiscale_pct  = 50.0,
            detrazione_spread_years = 10,
        )

        # ── Thermal demand correction based on building subtype ───────────────
        bsubtype   = ss("building_subtype", "detached")
        floor_num  = ss("floor_number", 0)
        is_top     = ss("is_top_floor", True)

        if bsubtype == "detached":
            thermal_factor = 1.0
        elif bsubtype == "terraced":
            if is_top:
                thermal_factor = 0.85
            elif floor_num == 0:
                thermal_factor = 0.82
            else:
                thermal_factor = 0.78
        else:  # apartment
            if is_top:
                thermal_factor = 0.70
            elif floor_num == 0:
                thermal_factor = 0.65
            else:
                thermal_factor = 0.55

        adjusted_gas_sm3 = total_gas * thermal_factor

        # ── AC load added to annual electricity demand ────────────────────────
        has_ac = ss("has_ac", False)
        ac_kw  = ss("ac_kw", 3.5)
        if has_ac:
            ac_hours_summer = ac_kw * 6 * 90  # 6 h/day × 90 summer days
            ann_el_final = ann_el + ac_hours_summer
        else:
            ac_hours_summer = 0.0
            ann_el_final = ann_el

        inp = SimulationInput(
            location  = LocationInput(
                lat=ss("center_lat") or ss("geo_lat") or 44.0,
                lon=ss("center_lon") or ss("geo_lon") or 10.0,
            ),
            building  = BuildingInput(
                building_type           = "residential",
                floor_area_m2           = ss("floor_area", 200.0),
                annual_demand_kWh       = float(ann_el_final),
                n_occupants             = int(ss("n_occupants", 2)),
                occupancy_days_per_week = ss("occ_days", 7.0),
                occupancy_hour_start    = ss("occ_hour", 0),
            ),
            pv_system = PVSystemInput(
                peak_power_kWp              = float(kwp),
                tilt_deg                    = float(ss("tilt_deg", 15)),
                azimuth_deg                 = float(ss("azimuth_from_map") or 180),
                mounting                    = ss("mounting","flat_roof_ballast"),
                degradation_rate_pct_per_yr = float(ss("degrad_pct", 0.5)),
            ),
            battery   = BatteryInput(capacity_kWh=0),
            investment= InvestmentCost(
                pv_cost_eur_kWp        = capex/kwp,
                peak_power_kWp         = kwp,
                design_and_permits_eur = 0,
            ),
            tariff    = TariffInput(
                import_price_eur_kWh = ss("el_price",0.22),
                export_price_eur_kWh = ss("export_price",0.08),
            ),
            opex      = OperatingCost(
                om_pct_of_capex_per_yr=1.0,
                inverter_replacement_year=0,  # fin-based mechanism below
            ),
            financial = FinancialParameters(
                discount_rate=0.05, energy_price_escalation=0.03,
                analysis_years=25,
                inverter_replacement_year=int(ss("inv_repl_year", 12)),
                inverter_replacement_cost_pct=float(ss("inv_repl_pct", 15)) / 100.0,
            ),
            incentives = inc,
            gas_system = GasSystemInput(
                has_gas=has_gas,
                annual_consumption_units=float(adjusted_gas_sm3) if has_gas else 0.0,
                gas_price_eur_sm3=ss("gas_price",0.95) if has_gas else 0.0,
                terminal_type=ss("terminal_type","fan_coil") if has_gas else "fan_coil",
                boiler_efficiency=0.88, dhw_fraction=0.12,
            ) if has_gas else GasSystemInput(has_gas=False),
            heat_pump = HeatPumpInput(
                terminal_type=ss("terminal_type","fan_coil") if has_gas else "fan_coil",
                use_bivalent=True,
            ) if has_gas else HeatPumpInput(),
            incentives_hp = ItalianIncentives(
                detrazione_fiscale_pct=65.0, detrazione_spread_years=10,
            ) if has_gas else ItalianIncentives(),
            lifetime_years=25, use_pvlib=False,
        )
        r = run_simulation(inp)
        d = result_to_dict(r)
        eco = d["economics"]
        pv  = d["pv_performance"]
        eb  = d["energy_balance_year1"]
        return dict(label=label, kwp=kwp, capex=capex,
                    net_capex=eco["net_capex_eur"],
                    incentive=eco["incentive_pv_eur"],
                    npv=eco["npv_eur"], irr=eco["irr_pct"],
                    payback=eco["payback_simple_yr"],
                    lcoe=eco["lcoe_eur_kWh"], score=eco["score"],
                    production=pv["production_annual_kWh"],
                    specific_yield=pv["specific_yield_kWh_kWp"],
                    scr=eb["self_consumption_rate"]*100,
                    ssr=eb["self_sufficiency_rate"]*100,
                    saving=eb["annual_saving_eur"],
                    cash_flows=d["cash_flows"],
                    result_dict=d)
    except Exception:
        st.error(f"Simulation error ({label}):\n```\n{traceback.format_exc()}\n```")
        return None


def step_impianto():
    st.markdown("### System sizing")
    st.caption("Based on your roof area and consumption, we calculate three system sizes. Choose the one that fits your goals.")

    # Advanced roof parameters
    with st.expander("Roof parameters — auto-estimated, adjust if needed",
                     expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            tilt = st.slider("Tilt angle [°]", 0, 60,
                             int(ss("tilt_deg",15)), 5)
            sset(tilt_deg=tilt)
        with tc2:
            az = st.slider("Azimuth [°]", 0, 360,
                           int(ss("azimuth_from_map") or 180), 5)
            sset(azimuth_from_map=az)
        with tc3:
            mounting = st.selectbox("Mounting type",
                ["flat_roof_ballast","roof_integrated","ground_fixed","bipv"],
                index=["flat_roof_ballast","roof_integrated",
                       "ground_fixed","bipv"].index(
                    ss("mounting","flat_roof_ballast")),
                format_func=lambda x: {
                    "flat_roof_ballast":"Flat roof","roof_integrated":"Pitched roof",
                    "ground_fixed":"Ground-mounted","bipv":"BIPV (facade)"}[x])
            sset(mounting=mounting)

    # Advanced financial parameters
    with st.expander("Advanced financial parameters", expanded=False):
        af1, af2, af3 = st.columns(3)
        with af1:
            deg = st.slider("Panel degradation [%/yr]", 0.3, 0.8,
                            float(ss("degrad_pct", 0.5)), 0.05, format="%.2f%%")
            sset(degrad_pct=deg)
        with af2:
            inv_yr = st.slider("Inverter replacement (year)", 8, 15,
                               int(ss("inv_repl_year", 12)), 1)
            sset(inv_repl_year=inv_yr)
        with af3:
            inv_pct = st.slider("Inverter replacement cost [% of CAPEX]", 10, 20,
                                int(ss("inv_repl_pct", 15)), 1)
            sset(inv_repl_pct=inv_pct)

    # Calcolo area e taglie
    buildings  = ss("buildings_cache") or []
    c_lat      = ss("center_lat")
    c_lon      = ss("center_lon")
    share_pct  = ss("roof_share_pct", 100)
    floor_area = ss("floor_area", 200.0)
    mounting   = ss("mounting","flat_roof_ballast")
    ann_el     = sum(ss("monthly_el",[0]*12))
    bsubtype   = ss("building_subtype", "detached")
    is_top     = ss("is_top_floor", True)

    osm_area = None
    if buildings and c_lat:
        from buildings import closest_building
        cl = closest_building(c_lat, c_lon, buildings)
        if cl and len(cl.coords) >= 3:
            osm_area = polygon_area_m2(cl.coords)
    roof_area = osm_area if (osm_area and osm_area > 20) else floor_area

    # Building-subtype roof access adjustments
    if bsubtype == "apartment" and not is_top:
        # Mid-floor apartment: no roof access — use floor area with low utilisation
        st.markdown(
            '<div class="warn">Mid-floor apartments typically cannot install '
            'rooftop PV. Check with your building administrator. '
            'Estimates below assume BIPV or balcony installation (15% utilisation).</div>',
            unsafe_allow_html=True)
        effective_share = 15
        effective_mounting = "bipv"
    else:
        effective_share    = share_pct
        effective_mounting = mounting
        # Top-floor apartment: default to roof_integrated
        if bsubtype == "apartment" and is_top and mounting == "flat_roof_ballast":
            effective_mounting = "roof_integrated"
            sset(mounting="roof_integrated")

    kwp_max_ = kwp_from_area(roof_area, effective_mounting, effective_share)
    kwp_min, kwp_opt, kwp_max = auto_scenarios(kwp_max_, ann_el)

    src = "OSM outline" if osm_area else "declared floor area"
    st.caption(
        f"Roof ({src}): **{roof_area:.0f} m²** × {effective_share}% = "
        f"**{roof_area*effective_share/100:.0f} m²** usable → "
        f"maximum **{kwp_max:.1f} kWp** installable"
    )

    # Cache scenarios
    _n_occ  = ss("n_occupants", 2)
    _has_ac = ss("has_ac", False)
    _ac_kw  = ss("ac_kw", 3.5)
    _subtype = bsubtype
    _floor   = ss("floor_number", 0)
    _top     = is_top
    cache_key = (f"{kwp_min}_{kwp_opt}_{kwp_max}_{ann_el}_{effective_share}"
                 f"_{effective_mounting}_{_n_occ}_{_has_ac}_{_ac_kw}"
                 f"_{_subtype}_{_floor}_{_top}")
    if ss("scenarios_key") != cache_key:
        with st.spinner("Calculating scenarios… (~20–40 sec)"):
            s1 = _run_sim(kwp_min, "Minima")
            s2 = _run_sim(kwp_opt, "Consigliata")
            s3 = _run_sim(kwp_max, "Massima")
        sset(scenarios=[s1,s2,s3], scenarios_key=cache_key, selected_scenario=1)

    scenarios = ss("scenarios",[])
    if not scenarios or any(s is None for s in scenarios):
        st.error("Calculation error — go back and check the data you entered.")
        if st.button("← Back"): go_to(3)
        return

    sel = ss("selected_scenario",1)

    # Cards scenari
    sc_cols = st.columns(3)
    for i, (col, sc) in enumerate(zip(sc_cols, scenarios)):
        with col:
            best    = (i == 1)
            pay_str = f"{sc['payback']:.1f}" if sc['payback']<99 else ">25"
            badge   = '<div class="badge">Recommended</div><br>' if best else ""
            card_cls = "sc-card sc-best" if best else "sc-card"
            sel_mark = "✅ " if i==sel else ""
            st.markdown(
                f'<div class="{card_cls}">{badge}'
                f'<div class="sc-title">{sel_mark}{sc["label"]}</div>'
                f'<div class="sc-kwp">{sc["kwp"]:.1f} kWp</div>'
                f'<div class="sc-sub">{sc["production"]:,.0f} kWh/yr</div>'
                f'</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div style="display:grid;grid-template-columns:1fr 1fr;'
                f'gap:6px;margin-top:8px">'
                f'{kpi_md("Investment", fmt_eur(sc["capex"]))}'
                f'{kpi_md("NPV 25 yr", fmt_eur(sc["npv"]), "", "green" if sc["npv"]>0 else "")}'
                f'{kpi_md("Payback", pay_str, "yr")}'
                f'{kpi_md("IRR", str(round(sc["irr"],1)), "%", "blue")}'
                f'</div>', unsafe_allow_html=True)
            if st.button("Select", key=f"sc_{i}",
                         type="primary" if i==sel else "secondary",
                         use_container_width=True):
                sset(selected_scenario=i)
                st.rerun()

    # Nota incentivi e fattori fisici
    sc_sel = scenarios[sel]
    if sc_sel["incentive"] > 0:
        st.markdown(
            f'<div class="good">💡 <strong>50% IRPEF tax deduction applied</strong> — '
            f'present value {fmt_eur(sc_sel["incentive"])} · '
            f'Net effective investment: <strong>{fmt_eur(sc_sel["net_capex"])}</strong>'
            f'</div>', unsafe_allow_html=True)

    # Physical adjustment notes
    bsubtype  = ss("building_subtype", "detached")
    floor_num = ss("floor_number", 0)
    is_top    = ss("is_top_floor", True)
    if bsubtype == "detached":
        tf = 1.0
    elif bsubtype == "terraced":
        tf = 0.85 if is_top else (0.82 if floor_num == 0 else 0.78)
    else:
        tf = 0.70 if is_top else (0.65 if floor_num == 0 else 0.55)
    if tf < 1.0:
        st.caption(f"Thermal demand adjusted for building type: ×{tf:.2f}")

    has_ac = ss("has_ac", False)
    if has_ac:
        ac_kw = ss("ac_kw", 3.5)
        ac_kwh = ac_kw * 6 * 90
        st.caption(f"AC load added: +{ac_kwh:.0f} kWh/yr (summer peak hours)")

    st.divider()
    ca, cb = st.columns(2)
    with ca:
        if st.button("← Back", use_container_width=True): go_to(3)
    with cb:
        if st.button("View full analysis →",
                     type="primary", use_container_width=True):
            sset(result=scenarios[sel]["result_dict"])
            go_to(5)


# ══════════════════════════════════════════════════════════════════════════════
# CLAUDE API — NARRATIVE AND CHART CAPTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _get_api_key() -> str | None:
    """Returns the Anthropic API key from st.secrets or environment."""
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY")


_SYSTEM_PROMPT = (
    "You are an expert solar PV analyst writing concise, honest, "
    "and jargon-free investment assessments for residential clients "
    "in Italy. Be direct and specific — always cite actual numbers. "
    "Never be generically optimistic. If the investment is marginal "
    "or poor, say so clearly and explain why, then suggest what "
    "would improve it. Write in English. Use plain paragraphs, "
    "no bullet points, no headers."
)


def _build_metrics_text(result_dict: dict, scenarios: list,
                        sel_idx: int) -> str:
    """Shared metrics block used by both narrative functions."""
    eco = result_dict["economics"]
    eb  = result_dict["energy_balance_year1"]
    pv  = result_dict["pv_performance"]
    loc = result_dict["location"]
    dem = result_dict.get("demand", {})
    sc  = result_dict.get("scenario_comparison")

    building_subtype = ss("building_subtype", "apartment")
    occ_label        = ss("occ_label", "")
    n_occupants      = ss("n_occupants", 2)
    occ_hour         = ss("occ_hour", 0)

    kwp = (scenarios[sel_idx]["kwp"]
           if scenarios and sel_idx < len(scenarios) and scenarios[sel_idx]
           else pv.get("peak_power_kWp", 0))

    ann_demand = (dem.get("annual_demand_kWh")
                  or dem.get("total_annual_kWh")
                  or sum(ss("monthly_el", [0] * 12)))

    pay     = eco["payback_simple_yr"]
    pay_str = f"{pay:.1f}" if pay < 99 else ">25"

    lines = [
        f"Building: {building_subtype}, {n_occupants} residents, "
        f"occupancy: {occ_label}",
        f"Location: {loc.get('latitude', 0):.3f}°N, "
        f"GHI {loc.get('annual_ghi_kWh_m2', 0):.0f} kWh/m²/yr",
        f"System: {kwp:.1f} kWp, "
        f"{pv['specific_yield_kWh_kWp']:.0f} kWh/kWp/yr",
        f"Annual production: {pv['production_annual_kWh']:,.0f} kWh",
        f"Annual demand: {ann_demand:,.0f} kWh",
        f"Self-consumption rate: {eb['self_consumption_rate']*100:.1f}%",
        f"Self-sufficiency rate: {eb['self_sufficiency_rate']*100:.1f}%",
        f"Annual saving year 1: €{eb['annual_saving_eur']:,.0f}",
        f"Total investment (gross): €{eco['total_capex_eur']:,.0f}",
        f"Net investment after 50% IRPEF deduction (present value): "
        f"€{eco['net_capex_eur']:,.0f}",
        f"NPV (25 years, 5% discount): €{eco['npv_eur']:,.0f}",
        f"IRR: {eco['irr_pct']:.1f}%",
        f"Simple payback: {pay_str} years",
        f"Score: {eco['score']:.1f}/10",
    ]

    if sc:
        total_gas = sum(ss("monthly_gas", [0] * 12))
        gas_price = ss("gas_price", 0.95)
        bep       = sc.get("gas_breakeven_price_eur_sm3", 0)
        rec       = sc.get("recommended_scenario", "solo_pv")
        lines += [
            f"Gas system: {total_gas:.0f} Sm³/yr at €{gas_price:.2f}/Sm³",
            f"Heat pump break-even gas price: €{bep:.2f}/Sm³",
            f"HP recommendation: {rec}",
        ]

    if occ_hour > 10:
        lines.append(
            f"Note: occupant is away from home until {occ_hour}:00 on weekdays, "
            f"reducing daytime self-consumption."
        )

    return "\n".join(lines)


def generate_narrative(result_dict: dict, scenarios: list,
                       selected_scenario_idx: int) -> str | None:
    """Call Claude API to generate a 2-sentence investment assessment.
    Returns the text, or None if the API key is missing or the call fails."""
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        import anthropic
        eco = result_dict["economics"]
        eb  = result_dict["energy_balance_year1"]
        pv  = result_dict["pv_performance"]
        sc  = result_dict.get("scenario_comparison")

        kwp = (scenarios[selected_scenario_idx]["kwp"]
               if scenarios and selected_scenario_idx < len(scenarios)
               and scenarios[selected_scenario_idx]
               else pv.get("peak_power_kWp", 0))

        if sc:
            gas_price = ss("gas_price", 0.95)
            bep       = sc.get("gas_breakeven_price_eur_sm3", 0)
            gas_line  = f"HP break-even {bep:.2f} vs current {gas_price:.2f} €/Sm³."
        else:
            gas_line = ""

        user_prompt = (
            "Write exactly 2 sentences about this residential PV simulation. "
            "First sentence: state clearly if it is a good investment or not, "
            "citing NPV and payback. Second sentence: name the single most "
            "important factor limiting or driving the result.\n\n"
            f"Data: {kwp:.1f} kWp, NPV €{eco['npv_eur']:,.0f}, "
            f"IRR {eco['irr_pct']:.1f}%, "
            f"payback {eco['payback_simple_yr']:.1f} yr, "
            f"SCR {eb['self_consumption_rate']*100:.1f}%, "
            f"saving €{eb['annual_saving_eur']:,.0f}/yr, "
            f"score {eco['score']:.1f}/10."
            + (f"\n{gas_line}" if gas_line else "")
        )
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=120,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return msg.content[0].text
    except Exception:
        return None


def generate_chart_captions(result_dict: dict) -> dict:
    """Call Claude API once to get 4 chart captions returned as a JSON dict.
    Keys: production, cashflow, energy_balance, gas_hp.
    Returns an empty dict if the call fails or the API key is missing."""
    api_key = _get_api_key()
    if not api_key:
        return {}
    try:
        import anthropic
        metrics = _build_metrics_text(
            result_dict,
            ss("scenarios", []),
            ss("selected_scenario", 1),
        )
        user_prompt = (
            "For a residential PV simulation with these results:\n"
            f"{metrics}\n\n"
            "Write exactly 4 short captions (2–3 sentences each), "
            "returned as a JSON object with these keys:\n"
            "- production: caption for monthly production bar chart\n"
            "- cashflow: caption for 25-year cumulative cash flow chart\n"
            "- energy_balance: caption for energy flow (Sankey diagram)\n"
            "- gas_hp: caption for gas/HP scenario comparison "
            "(write \"N/A\" if no gas system)\n\n"
            "Be specific to these numbers. Explain what the chart shows "
            "and what it means for this specific case. "
            "Return only the JSON object, no other text."
        )
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = msg.content[0].text.strip()
        # Strip markdown code fence if present
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — RISULTATI
# ══════════════════════════════════════════════════════════════════════════════

def step_risultati():
    d = ss("result")
    if d is None:
        st.warning("No results yet — go back to step 4.")
        if st.button("← Back"): go_to(4)
        return

    eco = d["economics"]
    eb  = d["energy_balance_year1"]
    pv  = d["pv_performance"]
    loc = d["location"]
    dem = d["demand"]
    cfs = d["cash_flows"]
    sc  = d.get("scenario_comparison")

    scenarios = ss("scenarios",[])
    sel       = ss("selected_scenario",1)
    kwp_lbl   = f"{scenarios[sel]['kwp']:.1f} kWp" if scenarios else ""
    pay       = eco["payback_simple_yr"]
    pay_str   = f"{pay:.1f}" if pay < 99 else ">25"
    score     = eco["score"]
    sc_color  = "#15803d" if score>=7 else ("#d97706" if score>=5 else "#dc2626")

    # ── Generate/cache Claude narrative ──────────────────────────────────────
    result_hash = f"{eco['npv_eur']:.0f}_{eco['irr_pct']:.2f}"
    if ss("narrative_hash") != result_hash:
        with st.spinner("Generating analysis…"):
            narrative = generate_narrative(d, scenarios, sel)
            captions  = generate_chart_captions(d)
            sset(narrative=narrative, captions=captions,
                 narrative_hash=result_hash)
    else:
        narrative = ss("narrative")
        captions  = ss("captions") or {}

    st.markdown(
        f'<div style="background:#f0fdf4;border-radius:12px;'
        f'padding:1.25rem 1.5rem;border:1px solid #bbf7d0;margin-bottom:1.5rem">'
        f'<div style="font-size:26px;font-weight:700;color:#14532d;margin-bottom:.2rem">'
        f'{kwp_lbl} — {eco["recommendation"]}</div>'
        f'<div style="font-size:14px;color:#166534">'
        f'Score <strong style="color:{sc_color}">{score:.1f}/10</strong> · '
        f'NPV {fmt_eur(eco["npv_eur"])} · IRR {eco["irr_pct"]:.1f}% · '
        f'Payback {pay_str} yr</div></div>',
        unsafe_allow_html=True)

    if narrative:
        st.markdown(
            f'<div style="background:#f8f8f8;border-left:3px solid #111;'
            f'padding:1.25rem 1.5rem;margin-bottom:1.5rem;'
            f'font-size:15px;line-height:1.8;color:#222;">'
            f'{narrative}</div>',
            unsafe_allow_html=True,
        )

    t_eco, t_prod, t_eb, t_hp, t_raw = st.tabs([
        "💰 Economics","☀️ Production","⚡ Energy balance","🔥 Gas/HP","📊 Raw data",
    ])

    # ── Economia ───────────────────────────────────────────────────────────────
    with t_eco:
        kc = st.columns(4)
        kc[0].markdown(kpi_md("Total CAPEX",     fmt_eur(eco["total_capex_eur"])),
                       unsafe_allow_html=True)
        kc[1].markdown(kpi_md("Incentive (PV)",  fmt_eur(eco["incentive_pv_eur"])),
                       unsafe_allow_html=True)
        kc[2].markdown(kpi_md("Net CAPEX",       fmt_eur(eco["net_capex_eur"]),"","blue"),
                       unsafe_allow_html=True)
        kc[3].markdown(kpi_md("LCOE",
                               f"{eco['lcoe_eur_kWh']:.4f}","€/kWh"),
                       unsafe_allow_html=True)

        st.markdown("##### Cumulative cash flows — 25 years")
        yrs    = [c["year"] for c in cfs]
        cum_cf = [c["cumulative_cf_eur"] for c in cfs]
        net_cf = [c["net_cf_eur"] for c in cfs]
        fig = go.Figure()
        fig.add_trace(go.Bar(x=yrs, y=net_cf, name="Annual CF", marker_color="#93c5fd", opacity=0.7))
        fig.add_trace(go.Scatter(x=yrs, y=cum_cf, name="Cumulative CF", line=dict(color="#1565c0",width=2.5),
                                 mode="lines+markers", marker_size=4))
        fig.add_hline(y=0, line_dash="dash", line_color="#9ca3af", line_width=1)
        _inv_yr = int(ss("inv_repl_year", 12))
        fig.add_vline(x=_inv_yr, line_dash="dot", line_color="#ef4444", line_width=1.5,
                      annotation_text="Inverter replacement",
                      annotation_position="top right",
                      annotation_font_size=10, annotation_font_color="#ef4444")
        fig.update_layout(yaxis_title="€", plot_bgcolor="white",
                          paper_bgcolor="white",
                          legend=dict(orientation="h",y=1.05),
                          margin=dict(t=30,b=40,l=70,r=20), height=300)
        fig.update_yaxes(gridcolor="#f3f4f6")
        st.plotly_chart(fig, use_container_width=True)
        cap = (captions or {}).get("cashflow")
        if cap and cap != "N/A":
            st.markdown(f'<p style="font-size:13px;color:#888;margin-top:4px">{cap}</p>',
                        unsafe_allow_html=True)

        if scenarios and all(s is not None for s in scenarios):
            st.markdown("##### Comparison of three system sizes")
            rows = []
            for s in scenarios:
                p_ = s["payback"]
                rows.append({
                    "Size":             f"{s['kwp']:.1f} kWp",
                    "Investment":       fmt_eur(s["capex"]),
                    "Incentive (PV)":   fmt_eur(s["incentive"]),
                    "NPV (25 yr)":      fmt_eur(s["npv"]),
                    "IRR":              f"{s['irr']:.1f}%",
                    "Payback (yr)":     f"{p_:.1f}" if p_<99 else ">25",
                    "Annual saving":    fmt_eur(s["saving"]),
                    "Score":            f"{s['score']:.1f}/10",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True,
                         hide_index=True)

    # ── Produzione ─────────────────────────────────────────────────────────────
    with t_prod:
        pc = st.columns(3)
        pc[0].markdown(kpi_md("Year-1 production",
                               f"{pv['production_annual_kWh']:,.0f}","kWh/yr","blue"),
                       unsafe_allow_html=True)
        pc[1].markdown(kpi_md("Specific yield",
                               f"{pv['specific_yield_kWh_kWp']:,.0f}","kWh/kWp"),
                       unsafe_allow_html=True)
        pc[2].markdown(kpi_md("Performance ratio",
                               f"{pv['performance_ratio']*100:.1f}","%"),
                       unsafe_allow_html=True)

        if pv.get("production_monthly_kWh"):
            st.markdown("##### Monthly production")
            fig2 = go.Figure(go.Bar(
                x=MESI, y=pv["production_monthly_kWh"],
                marker_color="#3b82f6",
                text=[f"{v:.0f}" for v in pv["production_monthly_kWh"]],
                textposition="outside"))
            fig2.update_layout(yaxis_title="kWh/month", plot_bgcolor="white",
                               paper_bgcolor="white", showlegend=False,
                               margin=dict(t=10,b=40,l=60,r=20), height=260)
            fig2.update_yaxes(gridcolor="#f3f4f6")
            st.plotly_chart(fig2, use_container_width=True)
            cap = (captions or {}).get("production")
            if cap and cap != "N/A":
                st.markdown(f'<p style="font-size:13px;color:#888;margin-top:4px">{cap}</p>',
                            unsafe_allow_html=True)

        ghi_m = loc.get("monthly_ghi_kWh_m2",[])
        if len(ghi_m)==12:
            st.markdown("##### Monthly GHI — site solar resource")
            fig3 = go.Figure(go.Bar(x=MESI, y=ghi_m, marker_color="#f97316",
                                    text=[f"{v:.0f}" for v in ghi_m],
                                    textposition="outside"))
            fig3.update_layout(yaxis_title="kWh/m²", plot_bgcolor="white",
                               paper_bgcolor="white", showlegend=False,
                               margin=dict(t=10,b=40,l=60,r=20), height=220)
            fig3.update_yaxes(gridcolor="#f3f4f6")
            st.plotly_chart(fig3, use_container_width=True)

    # ── Bilancio ────────────────────────────────────────────────────────────────
    with t_eb:
        bc = st.columns(4)
        bc[0].markdown(kpi_md("Self-consumption (SCR)",
                               f"{eb['self_consumption_rate']*100:.1f}","%"),
                       unsafe_allow_html=True)
        bc[1].markdown(kpi_md("Self-sufficiency (SSR)",
                               f"{eb['self_sufficiency_rate']*100:.1f}","%"),
                       unsafe_allow_html=True)
        bc[2].markdown(kpi_md("Grid export",
                               f"{eb['grid_export_kWh']:,.0f}","kWh/yr"),
                       unsafe_allow_html=True)
        bc[3].markdown(kpi_md("Year-1 saving",
                               fmt_eur(eb["annual_saving_eur"]),"/yr","green"),
                       unsafe_allow_html=True)

        prod  = pv["production_annual_kWh"]
        sc_kw = eb.get("self_consumed_kWh", prod*eb["self_consumption_rate"])
        fig_s = go.Figure(go.Sankey(
            node=dict(label=["PV production","Self-consumed","Grid export",
                              "Curtailment","Grid import","Building demand"],
                      color=["#3b82f6","#16a34a","#f97316",
                             "#dc2626","#9333ea","#374151"],
                      pad=15, thickness=20),
            link=dict(source=[0,0,0,4,1],target=[1,2,3,5,5],
                      value=[sc_kw, eb["grid_export_kWh"],
                             max(eb.get("curtailed_kWh",0),0.01),
                             eb["grid_import_kWh"], sc_kw],
                      color=["rgba(59,130,246,0.3)","rgba(249,115,22,0.3)",
                             "rgba(220,38,38,0.3)","rgba(147,51,234,0.3)",
                             "rgba(22,163,74,0.3)"]),
        ))
        fig_s.update_layout(paper_bgcolor="white",
                            margin=dict(t=10,b=10,l=0,r=0), height=280)
        st.plotly_chart(fig_s, use_container_width=True)
        cap = (captions or {}).get("energy_balance")
        if cap and cap != "N/A":
            st.markdown(f'<p style="font-size:13px;color:#888;margin-top:4px">{cap}</p>',
                        unsafe_allow_html=True)

    # ── Gas/HP ──────────────────────────────────────────────────────────────────
    with t_hp:
        if not sc:
            st.info("Enable gas in the Bills step to see the heat pump comparison.")
        else:
            SLABELS = {
                "baseline":"Baseline","solo_pv":"PV only",
                "solo_hp":"HP only","pv_hp":"PV+HP",
                "pv_hp_bivalente":"PV+HP bivalent",
            }
            rec = sc.get("recommended_scenario","solo_pv")
            bep = sc.get("gas_breakeven_price_eur_sm3",0)
            cur = sc.get("current_gas_price_eur_sm3",0)
            cls = "good" if bep <= cur and bep > 0 else "warn"
            msg = (f"💡 Heat pump already cheaper at current gas price ({cur:.2f} €/Sm³). "
                   f"Break-even: {bep:.2f} €/Sm³"
                   if bep <= cur and bep > 0 else
                   f"⚠️ HP break-even gas price: {bep:.2f} €/Sm³ "
                   f"(current: {cur:.2f} €/Sm³, "
                   f"+{(bep/cur-1)*100:.0f}% increase needed)" if bep>0 else "")
            if msg:
                st.markdown(f'<div class="{cls}">{msg}</div>',
                            unsafe_allow_html=True)
            rows = []
            for sid, sv in sc.get("scenarios",{}).items():
                p_ = sv["payback_simple_yr"]
                rows.append({
                    "Scenario":          SLABELS.get(sid,sid),
                    "CAPEX":             fmt_eur(sv["capex_eur"]),
                    "Energy cost/yr":    fmt_eur(sv["annual_total_energy_cost_eur"]),
                    "NPV":               fmt_eur(sv["npv_eur"]) if sv["npv_eur"]!=0 else "—",
                    "Payback":           f"{p_:.1f}" if p_<99 else ">25",
                    "CO₂ reduction":     f"{sv['co2_saving_pct']:.1f}%",
                    "★":                "★" if sid==rec else "",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True,
                         hide_index=True)
            cap = (captions or {}).get("gas_hp")
            if cap and cap != "N/A":
                st.markdown(f'<p style="font-size:13px;color:#888;margin-top:4px">{cap}</p>',
                            unsafe_allow_html=True)

    # ── Raw data ──────────────────────────────────────────────────────────────────
    with t_raw:
        st.download_button(
            "⬇️ Download results (JSON)",
            data=json.dumps(d, indent=2, ensure_ascii=False, default=str),
            file_name=f"pv_{loc.get('latitude',0):.3f}_{loc.get('longitude',0):.3f}.json",
            mime="application/json",
        )
        with st.expander("Annual cash flows"):
            cf_df = pd.DataFrame(cfs)[[
                "year","pv_production_kWh","energy_saving_eur",
                "export_revenue_eur","om_cost_eur","net_cf_eur","cumulative_cf_eur",
            ]].rename(columns={
                "year":"Year","pv_production_kWh":"Production(kWh)",
                "energy_saving_eur":"Saving(€)","export_revenue_eur":"Export(€)",
                "om_cost_eur":"O&M(€)","net_cf_eur":"Net CF(€)",
                "cumulative_cf_eur":"Cumulative CF(€)",
            })
            st.dataframe(cf_df, use_container_width=True, hide_index=True)

    st.divider()
    nav1, nav2 = st.columns([1,2])
    with nav1:
        if st.button("← Edit system", use_container_width=True): go_to(4)
    with nav2:
        if st.button("New analysis", use_container_width=True):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ══════════════════════════════════════════════════════════════════════════════

if "step" not in st.session_state:
    st.session_state["step"] = 1

st.markdown(
    '<div class="pv-header-title">Solar PV Analysis</div>'
    '<div class="pv-header-name">PV Engine</div>',
    unsafe_allow_html=True)

render_progress(st.session_state["step"])

STEP_FN = {1: step_posizione, 2: step_edificio,
           3: step_bollette,  4: step_impianto,  5: step_risultati}  # internal names kept
STEP_FN[st.session_state["step"]]()
