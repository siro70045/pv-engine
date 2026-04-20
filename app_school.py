"""
app_school.py — PV Engine  |  School edition  (6-step wizard)

Launch:
    streamlit run app_school.py
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
    result_to_dict,
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
from school.loads_school import SchoolLoadsInput, calc_school_loads, run_simulation_school
from school.incentives_school import calc_school_incentives
from school.calendar_it import region_from_coordinates, REGIONAL_CALENDARS

# ══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG
# ══════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="PV Engine — Scuole",
    page_icon="🏫",
    layout="wide",
    initial_sidebar_state="collapsed",
)

MESI = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
STEPS = ["Location","Institution","Occupancy","Energy","System","Results"]

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

def ss(k, d=None):  return st.session_state.get(k, d)
def sset(**kw):     st.session_state.update(kw)
def go_to(n):       sset(step=n); st.rerun()
def fmt_eur(v):     return f"€\u202f{v:,.0f}".replace(",", ".")

def kpi_md(label, val, unit="", cls=""):
    return (f'<div class="kpi {cls}"><div class="kpi-lbl">{label}</div>'
            f'<div class="kpi-val">{val}'
            f'<span style="font-size:12px;font-weight:400;color:#6b7280"> {unit}</span>'
            f'</div></div>')


def estimate_pv_cost_school(kwp: float) -> tuple[int, int]:
    """
    Returns (total_capex_eur, structural_assessment_eur).
    Uses commercial/industrial pricing curve (cheaper than residential for large flat roofs).
    Adds structural assessment (perizia strutturale) required for public buildings.
    """
    TIERS = [
        (20,  900), (50,  820), (100, 780),
        (250, 720), (float("inf"), 670),
    ]
    cost = 0.0
    remaining = kwp
    prev = 0.0
    for thresh, eur_kwp in TIERS:
        if remaining <= 0:
            break
        in_tier = min(remaining, thresh - prev)
        cost += in_tier * eur_kwp
        remaining -= in_tier
        prev = thresh
    structural = max(2000.0, kwp * 15.0)
    cost += structural
    return int(round(cost, -2)), int(round(structural, -2))


def auto_scenarios(kwp_max: float, annual_kwh: float) -> tuple[float, float, float]:
    Y = 1200.0
    mn = max(5.0, round(annual_kwh * 0.35 / Y * 2) / 2)
    op = min(kwp_max, max(mn, round(annual_kwh * 0.75 / Y * 2) / 2))
    mx = max(op, kwp_max)
    mn = min(mn, mx)
    op = max(mn, min(op, mx))
    return mn, op, mx


# ══════════════════════════════════════════════════════════════════════════════
# PROGRESS BAR
# ══════════════════════════════════════════════════════════════════════════════

def render_progress(cur: int) -> None:
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
# MAP HELPERS  (reused from app.py)
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


def map_phase1(geo_lat, geo_lon, buildings, h=480):
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
        st.warning("Install `folium` and `streamlit-folium` for the map.")
        return None


def map_phase2(c_lat, c_lon, buildings, s_lat=None, s_lon=None, h=480):
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
            folium.PolyLine([[c_lat, c_lon], [s_lat, s_lon]],
                            color="#f97316", weight=3, dash_array="6 4").add_to(m)
        return st_folium(m, height=h, use_container_width=True,
                         returned_objects=["last_clicked"])
    except ImportError:
        st.warning("Install `folium` and `streamlit-folium` for the map.")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — LOCATION
# ══════════════════════════════════════════════════════════════════════════════

def step_location():
    st.markdown("### Location")
    st.caption("Search for the school address, then identify the building on the map.")

    # Listed/protected building warning
    listed = st.checkbox(
        "This is a listed / heritage-protected building (vincolo soprintendenza)",
        value=bool(ss("listed_building", False)),
        help="A heritage listing may restrict rooftop installations. "
             "Check with the local Soprintendenza before proceeding.",
    )
    sset(listed_building=listed)
    if listed:
        st.markdown(
            '<div class="warn">⚠️ Heritage-listed buildings may require '
            'special authorisation from the Soprintendenza for rooftop PV. '
            'BIPV (building-integrated) solutions may be considered instead.</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    ca, cb = st.columns([4, 1])
    with ca:
        addr = st.text_input("Address", value=ss("address_text", ""),
                             placeholder="E.g. Via Roma 1, Bologna",
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

    geo_results = ss("geo_results", [])
    if len(geo_results) > 1:
        idx = st.selectbox(
            "Multiple results — choose the correct one:",
            range(len(geo_results)),
            format_func=lambda i: geo_results[i].display_name[:80],
            key="geo_sel",
        )
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

    if phase == "p1":
        nb = len(buildings)
        st.markdown(
            f"**Step 1 of 2** — Click on the centre of the school building."
            + (f" ({nb} OSM building outlines loaded)" if nb else "")
        )
        md = map_phase1(geo_lat, geo_lon, buildings)
        if md and md.get("last_clicked"):
            ck  = md["last_clicked"]
            key = f"{ck['lat']:.6f},{ck['lng']:.6f}"
            if key != ss("last_click_key", ""):
                sset(last_click_key=key,
                     center_lat=ck["lat"], center_lon=ck["lng"])

        c_lat = ss("center_lat")
        if c_lat is not None:
            st.markdown(
                f'<div class="good">✓ Centre selected: '
                f'{c_lat:.5f}°N, {ss("center_lon"):.5f}°E</div>',
                unsafe_allow_html=True,
            )
            share_opts = {
                "Entire building (100%)":  100,
                "Half of roof (50%)":       50,
                "One third (33%)":          33,
                "Custom":                    0,
            }
            slbl  = st.radio("Usable roof share", list(share_opts.keys()),
                             index=0, label_visibility="collapsed")
            share = share_opts[slbl]
            if share == 0:
                share = st.slider("Custom percentage", 10, 100, 70, 5, format="%d%%")
            sset(roof_share_pct=share)

            if st.button("Next — indicate the sun-facing side →",
                         type="primary", use_container_width=True):
                sset(map_phase="p2", last_click_key="")
                st.rerun()
        else:
            st.caption("No point selected — click on the map.")

    elif phase == "p2":
        c_lat = ss("center_lat")
        c_lon = ss("center_lon")
        st.markdown("**Step 2 of 2** — Click on the roof side facing the sun.")
        md = map_phase2(c_lat, c_lon, buildings, ss("sun_lat"), ss("sun_lon"))
        if md and md.get("last_clicked"):
            ck  = md["last_clicked"]
            key = f"{ck['lat']:.6f},{ck['lng']:.6f}"
            if key != ss("last_click_key", ""):
                bearing = calc_bearing(c_lat, c_lon, ck["lat"], ck["lng"])
                sset(last_click_key=key, sun_lat=ck["lat"], sun_lon=ck["lng"],
                     azimuth_from_map=bearing)

        az = ss("azimuth_from_map")
        if az is not None:
            lbl        = bearing_to_label(az)
            level, note = azimuth_quality_note(az)
            icon = "✅" if level == "good" else ("⚠️" if level == "ok" else "❌")
            cls  = "good" if level == "good" else "warn"
            st.markdown(
                f'<div class="{cls}">{icon} <strong>Azimuth: {lbl}</strong>'
                f'<br><span style="font-size:13px">{note}</span></div>',
                unsafe_allow_html=True,
            )

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
                        sset(tmy_data=tmy, tmy_key=f"{c_lat:.5f},{c_lon:.5f}",
                             tmy_error=None)
                    except Exception as e:
                        sset(tmy_data=None, tmy_key=None, tmy_error=str(e))
                sset(map_phase="done")
                st.rerun()

    elif phase == "done":
        c_lat = ss("center_lat")
        c_lon = ss("center_lon")
        az    = ss("azimuth_from_map")
        lbl   = bearing_to_label(az)
        level, note = azimuth_quality_note(az)
        icon  = "✅" if level == "good" else "⚠️"
        tmy   = ss("tmy_data")
        terr  = ss("tmy_error")
        share = ss("roof_share_pct", 100)

        rc = st.columns(3)
        rc[0].metric("Coordinate", f"{c_lat:.4f}°N")
        rc[1].metric("Azimuth", f"{icon} {lbl}")
        rc[2].metric("Roof share", f"{share}%")

        if terr:
            st.markdown(
                f'<div class="err">❌ PVGIS unreachable: {terr}'
                f'<br>Use manual file upload below.</div>',
                unsafe_allow_html=True,
            )
            with st.expander("📂 Upload TMY file manually"):
                tmy_file = st.file_uploader("TMY CSV (PVGIS export)", type=["csv"],
                                            label_visibility="collapsed")
                if tmy_file:
                    try:
                        tm = parse_pvgis_csv(
                            tmy_file.read().decode("utf-8", "replace"))
                        sset(tmy_data=tm, tmy_key="manual", tmy_error=None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Invalid file: {e}")
        elif tmy:
            m_  = tmy.monthly
            ghi = sum(m_.GHI_monthly)
            t_m = sum(m_.T_avg) / 12
            bm  = m_.GHI_monthly.index(max(m_.GHI_monthly))
            st.markdown(
                f'<div class="good"><strong>Climate data ready</strong> — '
                f'GHI {ghi:.0f} kWh/m²/yr · T̄ {t_m:.1f}°C · '
                f'Best month: {MESI[bm]} ({m_.GHI_monthly[bm]:.0f} kWh/m²)'
                f'</div>', unsafe_allow_html=True)

        ma, mb = st.columns([1, 2])
        with ma:
            if st.button("Edit location", use_container_width=True):
                sset(map_phase="p1", center_lat=None, center_lon=None,
                     sun_lat=None, sun_lon=None, azimuth_from_map=None,
                     last_click_key="", tmy_data=None, tmy_key=None, tmy_error=None)
                st.rerun()
        with mb:
            ok = ss("tmy_data") is not None
            if st.button("Continue →", type="primary" if ok else "secondary",
                         use_container_width=True, disabled=not ok):
                go_to(2)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — INSTITUTION
# ══════════════════════════════════════════════════════════════════════════════

def step_institution():
    st.markdown("### The institution")

    inst_name = st.text_input(
        "Institution name (optional — for the report)",
        value=ss("inst_name", ""),
        placeholder="E.g. I.T.I.S. Leonardo da Vinci",
    )
    sset(inst_name=inst_name)

    st.markdown('<div class="slbl">School type</div>', unsafe_allow_html=True)
    school_type_opts = {
        "Primary school (ages 6–11)":           "primary",
        "Middle school (ages 11–14)":           "middle",
        "Technical/vocational high school":     "high_tech",
        "Classical/scientific high school":     "high_classic",
        "University department":                "university_dept",
    }
    prev_st = ss("school_type", "high_tech")
    st_lbl  = st.radio(
        "School type",
        list(school_type_opts.keys()),
        index=list(school_type_opts.values()).index(prev_st),
        label_visibility="collapsed",
    )
    school_type = school_type_opts[st_lbl]
    sset(school_type=school_type)

    if school_type == "primary":
        full_time = st.checkbox(
            "Full-time schedule (tempo pieno — until 16:00)",
            value=bool(ss("full_time", False)),
        )
        sset(full_time=full_time)
    else:
        sset(full_time=False)

    st.divider()

    st.markdown('<div class="slbl">Construction year</div>', unsafe_allow_html=True)
    year_opts = {
        "Before 1973 (no insulation standards)": 1960,
        "1973–1990 (first insulation law)":      1981,
        "1991–2005":                             1998,
        "2006–2015":                             2010,
        "After 2015 (near-zero energy)":         2018,
    }
    prev_yr = ss("construction_year", 1981)
    yr_lbl  = st.radio(
        "Construction year",
        list(year_opts.keys()),
        index=list(year_opts.values()).index(
            min(year_opts.values(), key=lambda v: abs(v - prev_yr))),
        label_visibility="collapsed",
    )
    construction_year = year_opts[yr_lbl]
    sset(construction_year=construction_year)

    st.divider()

    # ── Climate zone ──────────────────────────────────────────────────────────
    from school.climate_zones import CLIMATE_ZONES, zone_from_latitude

    # Auto-detect from latitude if available
    _auto_zone = "E"
    _lat = ss("center_lat")
    if _lat:
        _auto_zone = zone_from_latitude(float(_lat))

    _zone_labels = {z: d["label"] for z, d in CLIMATE_ZONES.items()}
    _prev_zone = ss("climate_zone", _auto_zone)
    _zone_idx  = list(_zone_labels.keys()).index(_prev_zone) if _prev_zone in _zone_labels else 4

    st.markdown('<div class="slbl">Climate zone (DPR 412/93)</div>', unsafe_allow_html=True)
    climate_zone = st.selectbox(
        "Climate zone",
        list(_zone_labels.keys()),
        index=_zone_idx,
        format_func=lambda z: _zone_labels[z],
        label_visibility="collapsed",
        help="Italian climate zone per DPR 412/93. Determines heating season and thermal benchmarks.",
    )
    sset(climate_zone=climate_zone)
    if _lat and _auto_zone != climate_zone:
        st.caption(f"Auto-detected zone from latitude: **{_auto_zone}** — overridden to {climate_zone}.")
    elif _lat:
        st.caption(f"Auto-detected from latitude {float(_lat):.2f}°N.")

    st.divider()

    # ── Region (school calendar) ──────────────────────────────────────────────
    _auto_region = "emilia_romagna"
    _c_lon = ss("center_lon")
    if _lat and _c_lon:
        _auto_region = region_from_coordinates(float(_lat), float(_c_lon))

    _region_labels = {k: k.replace("_", " ").title() for k in REGIONAL_CALENDARS}
    _prev_region   = ss("region", _auto_region)
    if _prev_region not in _region_labels:
        _prev_region = _auto_region

    st.markdown('<div class="slbl">Regione (calendario scolastico)</div>', unsafe_allow_html=True)
    region = st.selectbox(
        "Regione",
        list(_region_labels.keys()),
        index=list(_region_labels.keys()).index(_prev_region),
        format_func=lambda r: _region_labels[r],
        label_visibility="collapsed",
        help="Determina il calendario scolastico regionale (data inizio/fine, festività locali).",
    )
    sset(region=region)
    if _lat and _c_lon and _auto_region != region:
        st.caption(f"Regione rilevata automaticamente: **{_auto_region}** — sovrascritta con {region}.")
    elif _lat and _c_lon:
        st.caption(f"Rilevata automaticamente dalle coordinate.")

    st.divider()

    c1, c2 = st.columns(2)
    with c1:
        is_public = st.toggle(
            "Public (state) school",
            value=bool(ss("is_public", True)),
            help="Public schools qualify for 10% VAT and PNRR grants.",
        )
        sset(is_public=is_public)
        n_floors = st.slider("Number of floors", 1, 6,
                             int(ss("n_floors", 3)))
        sset(n_floors=n_floors)
    with c2:
        floor_area = st.number_input(
            "Total floor area [m²]",
            value=int(ss("school_floor_area", 3000)),
            min_value=100, max_value=50000, step=100,
        )
        sset(school_floor_area=float(floor_area))

    # Auto-detect area from OSM if available
    buildings = ss("buildings_cache") or []
    c_lat     = ss("center_lat")
    c_lon     = ss("center_lon")
    if buildings and c_lat:
        from buildings import closest_building
        cl = closest_building(c_lat, c_lon, buildings)
        if cl and len(cl.coords) >= 3:
            osm_footprint = polygon_area_m2(cl.coords)
            if osm_footprint and osm_footprint > 50:
                osm_total = osm_footprint * n_floors
                st.caption(
                    f"OSM building footprint: {osm_footprint:.0f} m² × "
                    f"{n_floors} floors = {osm_total:.0f} m² total. "
                    f"Adjust the floor area above if needed."
                )

    st.divider()
    ca, cb = st.columns(2)
    with ca:
        if st.button("← Back", use_container_width=True): go_to(1)
    with cb:
        if st.button("Continue →", type="primary", use_container_width=True): go_to(3)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — OCCUPANCY & SPACES
# ══════════════════════════════════════════════════════════════════════════════

def step_occupancy():
    st.markdown("### Occupancy and spaces")

    col_l, col_r = st.columns(2)

    with col_l:
        st.markdown('<div class="slbl">People</div>', unsafe_allow_html=True)
        n_students = st.number_input(
            "Number of students",
            min_value=50, max_value=3000,
            value=int(ss("n_students", 300)),
            step=10,
        )
        n_teachers = st.number_input(
            "Number of teaching staff",
            min_value=1, max_value=500,
            value=int(ss("n_teachers", 25)),
            step=1,
        )
        n_ata = st.number_input(
            "Admin / technical staff (ATA)",
            min_value=1, max_value=100,
            value=int(ss("n_ata_staff", 10)),
            step=1,
        )
        sset(n_students=int(n_students), n_teachers=int(n_teachers),
             n_ata_staff=int(n_ata))

    with col_r:
        st.markdown('<div class="slbl">Special spaces</div>', unsafe_allow_html=True)
        has_gym       = st.checkbox("Gym / sports hall",          value=bool(ss("has_gym", False)))
        has_canteen   = st.checkbox("Canteen (internal kitchen)",  value=bool(ss("has_canteen", False)))
        has_it_lab    = st.checkbox("IT laboratory (computers)",   value=bool(ss("has_it_lab", False)))
        has_tech_lab  = st.checkbox("Technical/workshop lab",      value=bool(ss("has_tech_lab", False)))
        has_science   = st.checkbox("Science laboratory",          value=bool(ss("has_science_lab", False)))
        has_auditorium= st.checkbox("Auditorium / assembly hall",  value=bool(ss("has_auditorium", False)))
        has_elevator  = st.checkbox("Elevator",                    value=bool(ss("has_elevator", False)))
        sset(has_gym=has_gym, has_canteen=has_canteen, has_it_lab=has_it_lab,
             has_tech_lab=has_tech_lab, has_science_lab=has_science,
             has_auditorium=has_auditorium, has_elevator=has_elevator)

    if has_it_lab:
        n_pc = st.slider("Number of computers (total in all labs)",
                         10, 200, int(ss("n_computers", 30)), 5)
        sset(n_computers=int(n_pc))
    else:
        sset(n_computers=0)

    if has_tech_lab:
        tech_kw = st.number_input(
            "Workshop machinery peak load [kW]",
            min_value=0.0, max_value=50.0,
            value=float(ss("tech_lab_peak_kW", 5.0)),
            step=0.5,
        )
        sset(tech_lab_peak_kW=float(tech_kw))
    else:
        sset(tech_lab_peak_kW=0.0)

    st.divider()
    st.markdown('<div class="slbl">Extended use</div>', unsafe_allow_html=True)
    extra_evening = st.toggle(
        "Evening use (courses, gym rental, meetings) — Mon–Fri 18:00–22:00",
        value=bool(ss("extra_evening", False)),
    )
    extra_weekend = st.toggle(
        "Weekend use (events, sports) — Sat 9:00–17:00, Sun 9:00–13:00",
        value=bool(ss("extra_weekend", False)),
    )
    sset(extra_evening=extra_evening, extra_weekend=extra_weekend)

    st.divider()
    ca, cb = st.columns(2)
    with ca:
        if st.button("← Back", use_container_width=True): go_to(2)
    with cb:
        if st.button("Continue →", type="primary", use_container_width=True): go_to(4)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — ENERGY & HVAC
# ══════════════════════════════════════════════════════════════════════════════

def step_energy():
    st.markdown("### Energy consumption and systems")

    # ── HVAC ─────────────────────────────────────────────────────────────────
    st.markdown('<div class="slbl">Heating system</div>', unsafe_allow_html=True)
    heat_opts = {
        "Gas boiler":       "gas_boiler",
        "Heat pump":        "heat_pump",
        "District heating": "district",
        "None / other":     "none",
    }
    prev_hs  = ss("heating_system", "gas_boiler")
    heat_lbl = st.selectbox(
        "Heating system",
        list(heat_opts.keys()),
        index=list(heat_opts.values()).index(prev_hs),
        label_visibility="collapsed",
    )
    heating_system = heat_opts[heat_lbl]
    sset(heating_system=heating_system)

    cool_opts = {
        "No cooling":          "none",
        "Split units":         "split",
        "Central AC system":   "central",
    }
    prev_ac  = ss("ac_type", "none")
    cool_lbl = st.selectbox(
        "Cooling / AC",
        list(cool_opts.keys()),
        index=list(cool_opts.values()).index(prev_ac),
        label_visibility="collapsed",
    )
    ac_type  = cool_opts[cool_lbl]
    has_ac   = ac_type != "none"
    sset(ac_type=ac_type, has_ac=has_ac)

    light_opts = {
        "Full LED":            "led",
        "Mostly fluorescent":  "fluorescent",
        "Mixed":               "mixed",
    }
    prev_lt  = ss("lighting_type", "fluorescent")
    lt_lbl   = st.selectbox(
        "Lighting",
        list(light_opts.keys()),
        index=list(light_opts.values()).index(prev_lt),
        label_visibility="collapsed",
    )
    sset(lighting_type=light_opts[lt_lbl])

    st.markdown('<div class="slbl">Hot water system</div>', unsafe_allow_html=True)
    dhw_opts = {
        "Electric water heater (boiler)": "electric",
        "Heat pump water heater":         "heat_pump",
        "Gas water heater":               "gas",
        "No hot water / cold only":       "none",
    }
    prev_dhw = ss("dhw_system", "electric")
    dhw_lbl  = st.selectbox(
        "Hot water supply",
        list(dhw_opts.keys()),
        index=list(dhw_opts.values()).index(prev_dhw),
        label_visibility="collapsed",
    )
    sset(dhw_system=dhw_opts[dhw_lbl])
    if dhw_opts[dhw_lbl] != "none":
        st.caption(
            "Hot water is used by canteen (dishwashing), gym (showers), "
            "and washrooms. Winter consumption is significantly higher "
            "than summer due to colder inlet water and increased washroom usage."
        )

    st.divider()

    # ── Electricity bills ─────────────────────────────────────────────────────
    st.markdown('<div class="slbl">Electricity bills</div>', unsafe_allow_html=True)
    st.caption(
        "School bills are often split across multiple meters or paid by the "
        "municipality — enter what you have available."
    )

    mode = st.radio(
        "mode", ["Monthly breakdown", "Annual total only"],
        index=0 if ss("bill_mode", "monthly") == "monthly" else 1,
        horizontal=True, label_visibility="collapsed",
    )
    sset(bill_mode="monthly" if mode == "Monthly breakdown" else "annual")

    if mode == "Monthly breakdown":
        prev_el = ss("monthly_el", [0] * 12)
        el_vals = []
        row1, row2 = st.columns(6), st.columns(6)
        for i in range(12):
            col = row1[i] if i < 6 else row2[i - 6]
            with col:
                v = st.number_input(MESI[i], value=int(prev_el[i]),
                                    min_value=0, step=100, key=f"el_{i}")
                el_vals.append(v)
        total_el = sum(el_vals)
        sset(monthly_el=el_vals)

        bc1, bc2 = st.columns(2)
        with bc1:
            el_annual_cost = st.number_input(
                "Annual electricity cost [€/year]",
                value=float(ss("el_bill_annual", 0)), min_value=0.0, step=100.0, format="%.0f",
            )
        with bc2:
            el_monthly_cost = st.number_input(
                "Average monthly cost [€/month]",
                value=float(ss("el_bill_monthly", 0)), min_value=0.0, step=50.0, format="%.0f",
            )
        _el_cost = el_annual_cost if el_annual_cost > 0 else el_monthly_cost * 12
        sset(el_bill_annual=el_annual_cost, el_bill_monthly=el_monthly_cost)
    else:
        ac1, ac2 = st.columns(2)
        with ac1:
            total_el = st.number_input(
                "Annual consumption [kWh/year]",
                value=int(ss("annual_el_kwh", sum(ss("monthly_el", [0] * 12)))),
                min_value=0, step=500,
            )
        with ac2:
            el_cost_only = st.number_input(
                "Annual electricity cost [€/year]",
                value=float(ss("el_bill_annual", 0)), min_value=0.0, step=100.0, format="%.0f",
            )
        _el_cost = el_cost_only
        el_vals  = [total_el / 12] * 12
        sset(monthly_el=el_vals, annual_el_kwh=total_el, el_bill_annual=el_cost_only)

    _el_price = (_el_cost / total_el) if (_el_cost > 0 and total_el > 0) else 0.22
    sset(el_price=_el_price, monthly_el=el_vals)

    exp_price = st.number_input(
        "Export / feed-in rate [€/kWh]",
        value=float(ss("export_price", 0.08)),
        min_value=0.0, max_value=0.30, format="%.3f",
    )
    sset(export_price=exp_price)

    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("Annual consumption (bill)", f"{total_el:,.0f} kWh" if total_el > 0 else "—")
    sc2.metric("Annual cost", f"€ {_el_cost:,.0f}" if _el_cost > 0 else "not entered")
    sc3.metric("Implied unit price",
               f"{_el_price:.3f} €/kWh" if _el_cost > 0 else "default 0.22 €/kWh")

    # ── Bottom-up estimate ────────────────────────────────────────────────────
    st.divider()
    with st.spinner("Computing bottom-up estimate…"):
        _school_inp = SchoolLoadsInput(
            floor_area_m2     = ss("school_floor_area", 3000),
            n_floors          = ss("n_floors", 3),
            construction_year = ss("construction_year", 1981),
            school_type       = ss("school_type", "high_tech"),
            n_students        = ss("n_students", 300),
            n_teachers        = ss("n_teachers", 25),
            n_ata_staff       = ss("n_ata_staff", 10),
            full_time         = ss("full_time", False),
            extra_evening     = ss("extra_evening", False),
            extra_weekend     = ss("extra_weekend", False),
            has_gym           = ss("has_gym", False),
            has_canteen       = ss("has_canteen", False),
            has_it_lab        = ss("has_it_lab", False),
            has_tech_lab      = ss("has_tech_lab", False),
            has_science_lab   = ss("has_science_lab", False),
            has_auditorium    = ss("has_auditorium", False),
            has_elevator      = ss("has_elevator", False),
            n_computers       = ss("n_computers", 0),
            it_lab_watts_per_pc= 120.0,
            tech_lab_peak_kW  = ss("tech_lab_peak_kW", 0.0),
            heating_system    = heating_system,
            has_ac            = has_ac,
            ac_type           = ac_type,
            lighting_type     = ss("lighting_type", "fluorescent"),
            climate_zone      = ss("climate_zone", "E"),
            region            = ss("region", "emilia_romagna"),
            annual_bill_kwh   = float(total_el) if total_el > 0 else 0.0,
            dhw_system        = ss("dhw_system", "electric"),
        )
        _tmy = ss("tmy_data")
        _tmy_ghi = [rec.G_h for rec in _tmy.hourly] if _tmy is not None else None
        _loads_est = calc_school_loads(_school_inp, tmy_hourly_ghi=_tmy_ghi)
        sset(school_loads_result=_loads_est)

    est_kwh  = _loads_est["total_annual_kwh"]
    bill_kwh = float(total_el) if total_el > 0 else 0.0
    used_kwh = bill_kwh if bill_kwh > 0 else est_kwh

    st.markdown(
        f'<div class="good">'
        f'Estimated annual consumption (bottom-up model): '
        f'<strong>{est_kwh:,.0f} kWh/yr</strong> '
        f'({est_kwh / ss("school_floor_area", 3000):.1f} kWh/m²/yr)'
        + (f'<br>Based on your bill: <strong>{bill_kwh:,.0f} kWh/yr</strong>' if bill_kwh > 0 else "")
        + f'<br>Model will use: <strong>{used_kwh:,.0f} kWh/yr</strong>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.divider()
    ca, cb = st.columns(2)
    with ca:
        if st.button("← Back", use_container_width=True): go_to(3)
    with cb:
        if st.button("Continue →", type="primary", use_container_width=True): go_to(5)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — SYSTEM & INCENTIVES
# ══════════════════════════════════════════════════════════════════════════════

def step_system():
    st.markdown("### PV system and incentives")

    loads_result = ss("school_loads_result")
    if loads_result is None:
        st.warning("Go back to step 4 to compute the load estimate first.")
        if st.button("← Back"): go_to(4)
        return

    ann_kwh = loads_result["total_annual_kwh"]

    # ── Roof parameters ───────────────────────────────────────────────────────
    with st.expander("Roof parameters — adjust if needed", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            tilt = st.slider("Tilt [°]", 0, 30, int(ss("tilt_deg", 10)), 2)
            sset(tilt_deg=tilt)
        with tc2:
            az = st.slider("Azimuth [°]", 0, 360,
                           int(ss("azimuth_from_map") or 180), 5)
            sset(azimuth_from_map=az)
        with tc3:
            mounting = st.selectbox(
                "Mounting",
                ["flat_roof_ballast", "roof_integrated", "ground_fixed"],
                index=0,
                format_func=lambda x: {
                    "flat_roof_ballast": "Flat roof (ballast)",
                    "roof_integrated":   "Pitched roof",
                    "ground_fixed":      "Ground-mounted",
                }[x],
            )
            sset(mounting=mounting)

    # ── Advanced financial parameters ─────────────────────────────────────────
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

    # ── Structural assessment ─────────────────────────────────────────────────
    st.markdown('<div class="slbl">Roof structural assessment</div>',
                unsafe_allow_html=True)

    _cy = ss("construction_year", 1981)
    if _cy >= 2010:
        _default_structural = 2500
    elif _cy >= 1990:
        _default_structural = 5000
    elif _cy >= 1973:
        _default_structural = 12000
    else:
        _default_structural = 22000

    structural_status = st.radio(
        "Has a structural assessment been done?",
        [
            "Not yet assessed",
            "Assessed — no reinforcement needed",
            "Assessed — minor reinforcement needed",
            "Assessed — major reinforcement needed",
        ],
        index=0,
        label_visibility="visible",
        key="structural_status_radio",
    )

    if structural_status == "Not yet assessed":
        st.markdown(
            f'<div style="background:#1a1a2e; border-left:2px solid #888; '
            f'padding:0.8rem 1rem; font-size:13px; color:#ccc;">'
            f'Based on the construction year ({_cy}), '
            f'estimated structural costs are approximately '
            f'<strong>€{_default_structural:,}</strong>. '
            f'This includes the mandatory structural survey (€2,000–3,000) '
            f'and any reinforcement work. Budget accordingly.'
            f'</div>', unsafe_allow_html=True)
        structural_cost = _default_structural

    elif structural_status == "Assessed — no reinforcement needed":
        structural_cost = st.number_input(
            "Structural survey cost [€]",
            value=2500, min_value=0, max_value=10000, step=500,
        )

    elif structural_status == "Assessed — minor reinforcement needed":
        structural_cost = st.number_input(
            "Total structural cost (survey + reinforcement) [€]",
            value=8000, min_value=2000, max_value=30000, step=1000,
        )

    else:
        structural_cost = st.number_input(
            "Total structural cost (survey + reinforcement) [€]",
            value=22000, min_value=5000, max_value=50000, step=1000,
        )

    sset(structural_cost=int(structural_cost))

    if _cy < 1973:
        st.markdown(
            '<div style="background:#1a1a2e; border-left:2px solid #c0392b; '
            'padding:0.8rem 1rem; font-size:13px; color:#e0a0a0;">'
            '<strong>Pre-1973 building:</strong> structures built before the '
            'first seismic code (1974) often require significant reinforcement '
            'to support the additional roof load of PV panels (15–25 kg/m²). '
            'Consider a ground-mounted system as an alternative if the '
            'school has available land.'
            '</div>', unsafe_allow_html=True)

    # ── Auto-size from OSM / floor area
    buildings  = ss("buildings_cache") or []
    c_lat      = ss("center_lat")
    c_lon      = ss("center_lon")
    share_pct  = ss("roof_share_pct", 100)
    floor_area = ss("school_floor_area", 3000.0)

    osm_area = None
    if buildings and c_lat:
        from buildings import closest_building
        cl = closest_building(c_lat, c_lon, buildings)
        if cl and len(cl.coords) >= 3:
            osm_area = polygon_area_m2(cl.coords)
    roof_area = osm_area if (osm_area and osm_area > 20) else floor_area / ss("n_floors", 3)

    kwp_max_ = kwp_from_area(roof_area, mounting, share_pct)
    kwp_min, kwp_opt, kwp_max = auto_scenarios(kwp_max_, ann_kwh)

    src = "OSM outline" if osm_area else "footprint estimate"
    st.caption(
        f"Roof ({src}): **{roof_area:.0f} m²** × {share_pct}% = "
        f"**{roof_area * share_pct / 100:.0f} m²** usable → "
        f"max **{kwp_max:.1f} kWp** installable"
    )

    if ss("listed_building"):
        st.markdown(
            '<div class="warn">⚠️ Heritage-listed building — rooftop installation '
            'may require Soprintendenza authorisation. Proceed only after confirmation.</div>',
            unsafe_allow_html=True,
        )

    # ── Incentives ────────────────────────────────────────────────────────────
    st.markdown("#### Incentives")

    has_pnrr = st.toggle(
        "PNRR funding available?",
        value=bool(ss("has_pnrr", False)),
    )
    sset(has_pnrr=has_pnrr)
    pnrr_pct = 0.50
    if has_pnrr:
        pnrr_pct = st.slider("Estimated PNRR grant [%]",
                             30, 80, int(ss("pnrr_pct", 50)), 5) / 100.0
        sset(pnrr_pct=int(pnrr_pct * 100))
        st.caption(
            "PNRR grants for school energy efficiency can cover 50–80% of investment. "
            "Verify with your municipality and the current PNRR call."
        )

    has_cer = st.toggle(
        "Participate in an Energy Community (CER)?",
        value=bool(ss("has_cer", False)),
    )
    sset(has_cer=has_cer)
    cer_fraction = 0.0
    cer_incentive = 0.0
    if has_cer:
        cer_frac_pct = st.slider("Fraction of production shared in CER [%]",
                                 0, 100, int(ss("cer_fraction_pct", 60)), 5)
        sset(cer_fraction_pct=cer_frac_pct)
        cer_fraction = cer_frac_pct / 100.0

        region = st.selectbox(
            "Region (for GSE incentive rate)",
            ["Nord", "Centro", "Sud", "Isole"],
            index=["Nord", "Centro", "Sud", "Isole"].index(ss("cer_region", "Nord")),
        )
        sset(cer_region=region)
        cer_incentive = {"Nord": 0.100, "Centro": 0.105, "Sud": 0.110, "Isole": 0.110}[region]
        sset(cer_incentive_rate=cer_incentive)
        st.caption(f"GSE CER incentive for {region}: {cer_incentive:.3f} €/kWh on shared energy.")

    # ── Run scenarios ─────────────────────────────────────────────────────────
    cache_key = (f"{kwp_min:.1f}_{kwp_opt:.1f}_{kwp_max:.1f}_{ann_kwh:.0f}"
                 f"_{has_pnrr}_{pnrr_pct:.2f}_{has_cer}_{cer_fraction:.2f}"
                 f"_{cer_incentive:.3f}_{ss('azimuth_from_map', 180)}_{tilt}")

    if ss("scenarios_key_school") != cache_key:
        with st.spinner("Calculating scenarios… (~20–40 sec)"):
            s1 = _run_sim_school(kwp_min, "Minimal", pnrr_pct if has_pnrr else 0.0,
                                 cer_incentive if has_cer else 0.0, cer_fraction)
            s2 = _run_sim_school(kwp_opt, "Recommended", pnrr_pct if has_pnrr else 0.0,
                                 cer_incentive if has_cer else 0.0, cer_fraction)
            s3 = _run_sim_school(kwp_max, "Maximum", pnrr_pct if has_pnrr else 0.0,
                                 cer_incentive if has_cer else 0.0, cer_fraction)
        sset(scenarios_school=[s1, s2, s3], scenarios_key_school=cache_key,
             selected_scenario_school=1)

    scenarios = ss("scenarios_school", [])
    if not scenarios or any(s is None for s in scenarios):
        st.error("Calculation error — check the inputs above.")
        if st.button("← Back"): go_to(4)
        return

    sel = ss("selected_scenario_school", 1)

    sc_cols = st.columns(3)
    for i, (col, sc) in enumerate(zip(sc_cols, scenarios)):
        with col:
            best     = (i == 1)
            pay_str  = f"{sc['payback']:.1f}" if sc['payback'] < 99 else ">25"
            badge    = '<div class="badge">Recommended</div><br>' if best else ""
            card_cls = "sc-card sc-best" if best else "sc-card"
            sel_mark = "✅ " if i == sel else ""
            st.markdown(
                f'<div class="{card_cls}">{badge}'
                f'<div class="sc-title">{sel_mark}{sc["label"]}</div>'
                f'<div class="sc-kwp">{sc["kwp"]:.1f} kWp</div>'
                f'<div class="sc-sub">{sc["production"]:,.0f} kWh/yr</div>'
                f'</div>', unsafe_allow_html=True,
            )
            st.markdown(
                f'<div style="display:grid;grid-template-columns:1fr 1fr;'
                f'gap:6px;margin-top:8px">'
                f'{kpi_md("Investment (net)", fmt_eur(sc["net_capex"]))}'
                f'{kpi_md("NPV 25 yr", fmt_eur(sc["npv"]), "", "green" if sc["npv"] > 0 else "")}'
                f'{kpi_md("Payback", pay_str, "yr")}'
                f'{kpi_md("IRR", str(round(sc["irr"], 1)), "%", "blue")}'
                f'</div>', unsafe_allow_html=True,
            )
            if sc.get("pnrr_grant", 0) > 0:
                st.caption(f"PNRR grant: {fmt_eur(sc['pnrr_grant'])}")
            if st.button("Select", key=f"sc_school_{i}",
                         type="primary" if i == sel else "secondary",
                         use_container_width=True):
                sset(selected_scenario_school=i)
                st.rerun()

    st.divider()
    ca, cb = st.columns(2)
    with ca:
        if st.button("← Back", use_container_width=True): go_to(4)
    with cb:
        if st.button("View full analysis →", type="primary", use_container_width=True):
            sset(result_school=scenarios[sel]["result_dict"])
            go_to(6)


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def _run_sim_school(
    kwp: float,
    label: str,
    pnrr_pct: float,
    cer_incentive: float,
    cer_fraction: float,
) -> dict | None:
    tmy = ss("tmy_data")
    if tmy is None:
        return None

    try:
        import engine.pvgis_client as _pvc
        _pvc.fetch_tmy = lambda lat=None, lon=None, use_cache=True: tmy

        loads_result = ss("school_loads_result")
        if loads_result is None:
            return None

        floor_area = ss("school_floor_area", 3000.0)
        capex, auto_structural = estimate_pv_cost_school(kwp)
        # Replace auto structural estimate with user-provided value (fixed per building)
        structural_cost = int(ss("structural_cost", int(auto_structural)))
        capex = capex - int(auto_structural) + structural_cost
        is_public = ss("is_public", True)

        production_est = kwp * 1150.0   # rough estimate for incentive calc
        inc = calc_school_incentives(
            capex_eur             = float(capex),
            kwp                   = kwp,
            is_public             = is_public,
            has_pnrr              = pnrr_pct > 0,
            pnrr_grant_pct        = pnrr_pct,
            cer_participation     = cer_fraction > 0,
            cer_incentive_eur_kwh = cer_incentive,
            cer_shared_fraction   = cer_fraction,
            annual_production_kwh = production_est,
        )

        r = run_simulation_school(
            tmy                       = tmy,
            school_loads              = loads_result,
            floor_area_m2             = floor_area,
            kwp                       = kwp,
            azimuth_deg               = float(ss("azimuth_from_map") or 180),
            tilt_deg                  = float(ss("tilt_deg", 10)),
            import_price              = ss("el_price", 0.22),
            export_price              = ss("export_price", 0.08),
            capex_eur                 = float(inc["capex_iva_corrected"]),
            structural_eur            = float(structural_cost),
            pnrr_grant                = float(inc["pnrr_grant"]),
            cer_incentive_eur_kwh     = cer_incentive if cer_fraction > 0 else 0.0,
            cer_fraction              = cer_fraction,
            lifetime_years            = 25,
            pv_degradation_pct_per_yr = float(ss("degrad_pct", 0.5)),
            inverter_replacement_year = int(ss("inv_repl_year", 12)),
            inverter_replacement_cost_pct = float(ss("inv_repl_pct", 15)) / 100.0,
        )

        d   = result_to_dict(r)
        eco = d["economics"]
        pv  = d["pv_performance"]
        eb  = d["energy_balance_year1"]

        return dict(
            label       = label,
            kwp         = kwp,
            capex       = capex,
            capex_net_iva = int(inc["capex_iva_corrected"]),
            net_capex   = int(inc["net_capex_after_grants"]),
            pnrr_grant  = int(inc["pnrr_grant"]),
            cer_annual  = float(inc["cer_annual_incentive"]),
            inc_notes   = inc["notes"],
            npv         = eco["npv_eur"],
            irr         = eco["irr_pct"],
            payback     = eco["payback_simple_yr"],
            lcoe        = eco["lcoe_eur_kWh"],
            score       = eco["score"],
            production  = pv["production_annual_kWh"],
            specific_yield = pv["specific_yield_kWh_kWp"],
            scr         = eb["self_consumption_rate"] * 100,
            ssr         = eb["self_sufficiency_rate"] * 100,
            saving      = eb["annual_saving_eur"],
            cash_flows  = d["cash_flows"],
            result_dict = d,
        )
    except Exception:
        st.error(f"Simulation error ({label}):\n```\n{traceback.format_exc()}\n```")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# CLAUDE API — NARRATIVE
# ══════════════════════════════════════════════════════════════════════════════

def _get_api_key() -> str | None:
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY")


_SYSTEM_PROMPT_SCHOOL = (
    "You are an expert solar PV analyst writing concise, honest assessments "
    "for public school institutions in Italy. Use an institutional, not consumer, "
    "tone. Always cite actual numbers. Mention specific incentives (PNRR, CER) where relevant. "
    "Be direct — if the investment is marginal, say so. Write in English. "
    "Use plain paragraphs, no bullet points, no headers."
)


def generate_narrative_school(result_dict: dict, scenarios: list, sel: int,
                               loads_result: dict, n_students: int) -> str | None:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        import anthropic
        eco = result_dict["economics"]
        eb  = result_dict["energy_balance_year1"]
        pv  = result_dict["pv_performance"]
        sc  = scenarios[sel] if scenarios and sel < len(scenarios) and scenarios[sel] else {}
        kwp = sc.get("kwp", 0)
        pnrr = sc.get("pnrr_grant", 0)
        cer  = sc.get("cer_annual", 0)
        ann_demand = loads_result.get("total_annual_kwh", 0)
        saving_per_student = (eb["annual_saving_eur"] / n_students) if n_students > 0 else 0
        pay = eco["payback_simple_yr"]
        pay_str = f"{pay:.1f}" if pay < 99 else ">25"

        user_prompt = (
            "Write exactly 3 sentences about this school PV simulation in an institutional tone. "
            "Sentence 1: is it a sound investment? Cite NPV and payback. "
            "Sentence 2: describe the energy impact (SSR, saving per student, CO₂). "
            "Sentence 3: highlight the key incentive or risk factor.\n\n"
            f"Data: {kwp:.1f} kWp, NPV €{eco['npv_eur']:,.0f}, "
            f"IRR {eco['irr_pct']:.1f}%, payback {pay_str} yr, "
            f"SCR {eb['self_consumption_rate']*100:.1f}%, "
            f"SSR {eb['self_sufficiency_rate']*100:.1f}%, "
            f"annual demand {ann_demand:,.0f} kWh, "
            f"annual saving €{eb['annual_saving_eur']:,.0f}, "
            f"saving per student €{saving_per_student:.0f}/yr, "
            f"score {eco['score']:.1f}/10."
            + (f" PNRR grant: €{pnrr:,.0f}." if pnrr > 0 else "")
            + (f" CER annual incentive: €{cer:,.0f}/yr." if cer > 0 else "")
        )
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model      = "claude-sonnet-4-6",
            max_tokens = 180,
            system     = _SYSTEM_PROMPT_SCHOOL,
            messages   = [{"role": "user", "content": user_prompt}],
        )
        return msg.content[0].text
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 — RESULTS
# ══════════════════════════════════════════════════════════════════════════════

def step_results():
    d = ss("result_school")
    if d is None:
        st.warning("No results yet — go back to step 5.")
        if st.button("← Back"): go_to(5)
        return

    eco      = d["economics"]
    eb       = d["energy_balance_year1"]
    pv       = d["pv_performance"]
    loc      = d["location"]
    cfs      = d["cash_flows"]
    loads_r  = ss("school_loads_result") or {}
    scenarios= ss("scenarios_school", [])
    sel      = ss("selected_scenario_school", 1)
    sc       = scenarios[sel] if scenarios and sel < len(scenarios) else {}
    kwp_lbl  = f"{sc.get('kwp', 0):.1f} kWp"
    n_students = int(ss("n_students", 1) or 1)
    floor_area = float(ss("school_floor_area", 1) or 1)
    ann_demand = loads_r.get("total_annual_kwh", 0.0)

    pay      = eco["payback_simple_yr"]
    pay_str  = f"{pay:.1f}" if pay < 99 else ">25"
    score    = eco["score"]
    sc_color = "#15803d" if score >= 7 else ("#d97706" if score >= 5 else "#dc2626")

    # ── Generate narrative ────────────────────────────────────────────────────
    result_hash = f"{eco['npv_eur']:.0f}_{eco['irr_pct']:.2f}"
    if ss("narrative_hash_school") != result_hash:
        with st.spinner("Generating analysis…"):
            narrative = generate_narrative_school(d, scenarios, sel, loads_r, n_students)
            sset(narrative_school=narrative, narrative_hash_school=result_hash)
    else:
        narrative = ss("narrative_school")

    # ── Header card ───────────────────────────────────────────────────────────
    st.markdown(
        f'<div style="background:#f0fdf4;border-radius:12px;'
        f'padding:1.25rem 1.5rem;border:1px solid #bbf7d0;margin-bottom:1.5rem">'
        f'<div style="font-size:26px;font-weight:700;color:#14532d;margin-bottom:.2rem">'
        f'{kwp_lbl} — {eco["recommendation"]}</div>'
        f'<div style="font-size:14px;color:#166534">'
        f'Score <strong style="color:{sc_color}">{score:.1f}/10</strong> · '
        f'NPV {fmt_eur(eco["npv_eur"])} · IRR {eco["irr_pct"]:.1f}% · '
        f'Payback {pay_str} yr</div></div>',
        unsafe_allow_html=True,
    )

    if narrative:
        st.markdown(
            f'<div style="background:#f8f8f8;border-left:3px solid #111;'
            f'padding:1.25rem 1.5rem;margin-bottom:1.5rem;'
            f'font-size:15px;line-height:1.8;color:#222;">'
            f'{narrative}</div>',
            unsafe_allow_html=True,
        )

    # ── Breakdown table ────────────────────────────────────────────────────────
    if loads_r.get("breakdown"):
        st.markdown("#### Estimated consumption breakdown")
        bd    = loads_r["breakdown"]
        total = sum(bd.values()) or 1.0
        rows  = [
            {"Category": k.replace("_", " ").title(),
             "kWh/yr": f"{v:,.0f}",
             "Share": f"{v / total * 100:.1f}%"}
            for k, v in bd.items() if v > 0
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        if loads_r.get("dhw_details"):
            dhw = loads_r["dhw_details"]
            if dhw.get("total_electric_kwh", 0) > 0:
                st.markdown('<div class="slbl">Hot water breakdown</div>',
                            unsafe_allow_html=True)
                dhw_rows = []
                if dhw["mensa_kwh"] > 0:
                    dhw_rows.append({"Use": "Canteen (dishwashing)",
                                     "kWh_th/yr": int(dhw["mensa_kwh"])})
                if dhw["gym_kwh"] > 0:
                    dhw_rows.append({"Use": "Gym showers",
                                     "kWh_th/yr": int(dhw["gym_kwh"])})
                if dhw["washroom_kwh"] > 0:
                    dhw_rows.append({"Use": "Washrooms (seasonal)",
                                     "kWh_th/yr": int(dhw["washroom_kwh"])})
                if dhw_rows:
                    st.dataframe(dhw_rows, use_container_width=True, hide_index=True)
                st.caption(
                    f"Thermal demand: {dhw['total_thermal_kwh']:,.0f} kWh_th/yr "
                    f"(before system efficiency). System: {dhw['dhw_system']}. "
                    f"Electric equivalent: {dhw['total_electric_kwh']:,.0f} kWh/yr."
                )

        if loads_r.get("lighting_daylight_aware"):
            details = loads_r["lighting_details"]
            st.caption(
                f"Lighting adjusted for natural daylight: "
                f"{details['classroom_kwh']:,.0f} kWh classrooms (daylight-aware), "
                f"{details['corridor_kwh']:,.0f} kWh corridors. "
                f"Winter mornings consume ~{details['winter_vs_summer_ratio']:.1f}x "
                f"more than summer."
            )

        if loads_r.get("internal_gains"):
            ig = loads_r["internal_gains"]
            n_occ = int(ss("n_students", 0) or 0) + int(ss("n_teachers", 0) or 0) + int(ss("n_ata_staff", 0) or 0)
            reduct_str = (f" ({round(ig['reduction_pct'])}% of HP heating demand)"
                          if ig.get("reduction_pct") else "")
            st.markdown(
                f'<div style="background:#0f2027; border-left:3px solid #16a34a; '
                f'padding:0.8rem 1rem; margin:0.5rem 0; font-size:13px; color:#ccc;">'
                f'<strong>Internal heat gains:</strong> {ig["peak_kW"]:.1f} kW peak '
                f'from {n_occ} occupants. '
                f'Annual offset: {ig["annual_kWh"]:,.0f} kWh{reduct_str}.'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.divider()

    # ── Tabs ──────────────────────────────────────────────────────────────────
    t_eco, t_prod, t_eb, t_inst, t_raw = st.tabs([
        "💰 Economics", "☀️ Production", "⚡ Energy balance",
        "🏫 Institutional", "📊 Raw data",
    ])

    # ── Economics tab ─────────────────────────────────────────────────────────
    with t_eco:
        kc = st.columns(4)
        kc[0].markdown(kpi_md("Total CAPEX",    fmt_eur(sc.get("capex", eco["total_capex_eur"]))),
                       unsafe_allow_html=True)
        kc[1].markdown(kpi_md("PNRR grant",     fmt_eur(sc.get("pnrr_grant", 0)), "", "green"),
                       unsafe_allow_html=True)
        kc[2].markdown(kpi_md("Net investment", fmt_eur(sc.get("net_capex", eco["net_capex_eur"])), "", "blue"),
                       unsafe_allow_html=True)
        kc[3].markdown(kpi_md("LCOE",           f"{eco['lcoe_eur_kWh']:.4f}", "€/kWh"),
                       unsafe_allow_html=True)

        _struct_cost = ss("structural_cost", 0)
        if _struct_cost > 0:
            st.caption(
                f"Includes €{_struct_cost:,} for structural assessment"
                f"{' and reinforcement' if _struct_cost > 5000 else ''}."
            )

        if sc.get("cer_annual", 0) > 0:
            st.markdown(
                f'<div class="good">💡 CER annual incentive: '
                f'<strong>{fmt_eur(sc["cer_annual"])}/yr</strong></div>',
                unsafe_allow_html=True,
            )

        if sc.get("inc_notes"):
            with st.expander("Incentive details"):
                for note in sc["inc_notes"]:
                    st.caption(f"• {note}")

        st.markdown("##### Cumulative cash flows — 25 years")
        yrs    = [c["year"] for c in cfs]
        cum_cf = [c["cumulative_cf_eur"] for c in cfs]
        net_cf = [c["net_cf_eur"] for c in cfs]
        fig = go.Figure()
        fig.add_trace(go.Bar(x=yrs, y=net_cf, name="Annual CF",
                             marker_color="#93c5fd", opacity=0.7))
        fig.add_trace(go.Scatter(x=yrs, y=cum_cf, name="Cumulative CF",
                                 line=dict(color="#1565c0", width=2.5),
                                 mode="lines+markers", marker_size=4))
        fig.add_hline(y=0, line_dash="dash", line_color="#9ca3af", line_width=1)
        _inv_yr = int(ss("inv_repl_year", 12))
        fig.add_vline(x=_inv_yr, line_dash="dot", line_color="#ef4444", line_width=1.5,
                      annotation_text="Inverter replacement",
                      annotation_position="top right",
                      annotation_font_size=10, annotation_font_color="#ef4444")
        fig.update_layout(yaxis_title="€", plot_bgcolor="white",
                          paper_bgcolor="white",
                          legend=dict(orientation="h", y=1.05),
                          margin=dict(t=30, b=40, l=70, r=20), height=300)
        fig.update_yaxes(gridcolor="#f3f4f6")
        st.plotly_chart(fig, use_container_width=True)

        if scenarios and all(s is not None for s in scenarios):
            st.markdown("##### Comparison of three system sizes")
            rows = []
            for s in scenarios:
                p_ = s["payback"]
                rows.append({
                    "Size":           f"{s['kwp']:.1f} kWp",
                    "Gross CAPEX":    fmt_eur(s["capex"]),
                    "PNRR grant":     fmt_eur(s.get("pnrr_grant", 0)),
                    "Net investment": fmt_eur(s["net_capex"]),
                    "NPV (25 yr)":    fmt_eur(s["npv"]),
                    "IRR":            f"{s['irr']:.1f}%",
                    "Payback (yr)":   f"{p_:.1f}" if p_ < 99 else ">25",
                    "Annual saving":  fmt_eur(s["saving"]),
                    "Score":          f"{s['score']:.1f}/10",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ── Production tab ────────────────────────────────────────────────────────
    with t_prod:
        pc = st.columns(3)
        pc[0].markdown(kpi_md("Year-1 production",
                               f"{pv['production_annual_kWh']:,.0f}", "kWh/yr", "blue"),
                       unsafe_allow_html=True)
        pc[1].markdown(kpi_md("Specific yield",
                               f"{pv['specific_yield_kWh_kWp']:,.0f}", "kWh/kWp"),
                       unsafe_allow_html=True)
        pc[2].markdown(kpi_md("Performance ratio",
                               f"{pv['performance_ratio']*100:.1f}", "%"),
                       unsafe_allow_html=True)

        if pv.get("production_monthly_kWh"):
            st.markdown("##### Monthly production")
            fig2 = go.Figure(go.Bar(
                x=MESI, y=pv["production_monthly_kWh"],
                marker_color="#3b82f6",
                text=[f"{v:.0f}" for v in pv["production_monthly_kWh"]],
                textposition="outside",
            ))
            fig2.update_layout(yaxis_title="kWh/month", plot_bgcolor="white",
                               paper_bgcolor="white", showlegend=False,
                               margin=dict(t=10, b=40, l=60, r=20), height=260)
            fig2.update_yaxes(gridcolor="#f3f4f6")
            st.plotly_chart(fig2, use_container_width=True)

        if loads_r.get("total_monthly_kwh"):
            st.markdown("##### Monthly demand profile")
            fig3 = go.Figure(go.Bar(
                x=MESI, y=loads_r["total_monthly_kwh"],
                marker_color="#f97316",
                text=[f"{v:.0f}" for v in loads_r["total_monthly_kwh"]],
                textposition="outside",
            ))
            fig3.update_layout(yaxis_title="kWh/month", plot_bgcolor="white",
                               paper_bgcolor="white", showlegend=False,
                               margin=dict(t=10, b=40, l=60, r=20), height=240)
            fig3.update_yaxes(gridcolor="#f3f4f6")
            st.plotly_chart(fig3, use_container_width=True)
            st.caption(
                "Demand peaks Sep–Jun (school in session); "
                "drops sharply Jun–Aug (summer: ATA staff only)."
            )

    # ── Energy balance tab ────────────────────────────────────────────────────
    with t_eb:
        bc = st.columns(4)
        bc[0].markdown(kpi_md("Self-consumption (SCR)",
                               f"{eb['self_consumption_rate']*100:.1f}", "%"),
                       unsafe_allow_html=True)
        bc[1].markdown(kpi_md("Self-sufficiency (SSR)",
                               f"{eb['self_sufficiency_rate']*100:.1f}", "%"),
                       unsafe_allow_html=True)
        bc[2].markdown(kpi_md("Grid export",
                               f"{eb['grid_export_kWh']:,.0f}", "kWh/yr"),
                       unsafe_allow_html=True)
        bc[3].markdown(kpi_md("Year-1 saving",
                               fmt_eur(eb["annual_saving_eur"]), "/yr", "green"),
                       unsafe_allow_html=True)

        prod   = pv["production_annual_kWh"]
        sc_kwh = eb.get("self_consumed_kWh", prod * eb["self_consumption_rate"])
        fig_s  = go.Figure(go.Sankey(
            node=dict(
                label=["PV production", "Self-consumed", "Grid export",
                       "Curtailment", "Grid import", "Building demand"],
                color=["#3b82f6", "#16a34a", "#f97316",
                       "#dc2626", "#9333ea", "#374151"],
                pad=15, thickness=20,
            ),
            link=dict(
                source=[0, 0, 0, 4, 1],
                target=[1, 2, 3, 5, 5],
                value=[sc_kwh, eb["grid_export_kWh"],
                       max(eb.get("curtailed_kWh", 0), 0.01),
                       eb["grid_import_kWh"], sc_kwh],
                color=["rgba(59,130,246,0.3)", "rgba(249,115,22,0.3)",
                       "rgba(220,38,38,0.3)", "rgba(147,51,234,0.3)",
                       "rgba(22,163,74,0.3)"],
            ),
        ))
        fig_s.update_layout(paper_bgcolor="white",
                            margin=dict(t=10, b=10, l=0, r=0), height=280)
        st.plotly_chart(fig_s, use_container_width=True)

    # ── Institutional report tab ───────────────────────────────────────────────
    with t_inst:
        st.markdown("#### Institutional indicators")

        prod_kwh    = pv["production_annual_kWh"]
        saving_eur  = eb["annual_saving_eur"]
        co2_kg_kwh  = 0.233   # Italian grid emission factor kg CO₂/kWh
        co2_saved_t = prod_kwh * co2_kg_kwh / 1000.0
        trees       = co2_saved_t * 1000 / 22   # 1 tree ≈ 22 kg CO₂/yr
        ssr_pct     = eb["self_sufficiency_rate"] * 100

        saving_per_student = saving_eur / n_students if n_students > 0 else 0
        saving_per_m2      = saving_eur / floor_area if floor_area > 0 else 0
        prod_per_student   = prod_kwh / n_students if n_students > 0 else 0

        ic = st.columns(4)
        ic[0].markdown(kpi_md("CO₂ saved", f"{co2_saved_t:.1f}", "t/yr", "green"),
                       unsafe_allow_html=True)
        ic[1].markdown(kpi_md("≈ Trees planted", f"{trees:.0f}", "trees/yr"),
                       unsafe_allow_html=True)
        ic[2].markdown(kpi_md("Saving / student", f"{saving_per_student:.0f}", "€/student/yr"),
                       unsafe_allow_html=True)
        ic[3].markdown(kpi_md("Saving / m²", f"{saving_per_m2:.2f}", "€/m²/yr"),
                       unsafe_allow_html=True)

        ic2 = st.columns(3)
        ic2[0].markdown(kpi_md("Production / student", f"{prod_per_student:.0f}", "kWh/student/yr"),
                        unsafe_allow_html=True)
        ic2[1].markdown(kpi_md("Self-sufficiency", f"{ssr_pct:.1f}", "%"),
                        unsafe_allow_html=True)
        ic2[2].markdown(kpi_md("CO₂ intensity", f"{co2_kg_kwh*1000:.0f}", "g/kWh grid"),
                        unsafe_allow_html=True)

        cov_pct = min(prod_kwh / ann_demand * 100, 100.0) if ann_demand > 0 else 0
        st.markdown(
            f'<div class="good">This installation would cover approximately '
            f'<strong>{cov_pct:.0f}%</strong> of the school\'s annual '
            f'electricity demand ({ann_demand:,.0f} kWh/yr).</div>',
            unsafe_allow_html=True,
        )

        # CO₂ chart
        st.markdown("##### Cumulative CO₂ savings — 25 years")
        cum_co2 = [prod_kwh * co2_kg_kwh / 1000 * (1 - 0.005) ** yr
                   for yr in range(25)]
        cum_co2_total = [sum(cum_co2[:y + 1]) for y in range(25)]
        fig_co2 = go.Figure(go.Scatter(
            x=list(range(1, 26)), y=cum_co2_total,
            fill="tozeroy", line=dict(color="#16a34a", width=2),
            fillcolor="rgba(22,163,74,0.15)",
        ))
        fig_co2.update_layout(
            xaxis_title="Year", yaxis_title="Cumulative CO₂ saved [t]",
            plot_bgcolor="white", paper_bgcolor="white",
            margin=dict(t=10, b=40, l=60, r=20), height=220,
        )
        fig_co2.update_yaxes(gridcolor="#f3f4f6")
        st.plotly_chart(fig_co2, use_container_width=True)

        # Summary JSON download
        st.markdown("##### Download report data")
        inst_name = ss("inst_name", "School")
        summary = {
            "institution":     inst_name,
            "school_type":     ss("school_type"),
            "location":        loc,
            "system_kwp":      sc.get("kwp", 0),
            "annual_production_kwh": prod_kwh,
            "annual_demand_kwh": ann_demand,
            "coverage_pct":    round(cov_pct, 1),
            "self_sufficiency_pct": round(ssr_pct, 1),
            "annual_saving_eur": round(saving_eur, 0),
            "saving_per_student_eur": round(saving_per_student, 1),
            "saving_per_m2_eur": round(saving_per_m2, 2),
            "co2_saved_t_yr":  round(co2_saved_t, 2),
            "trees_equivalent": round(trees, 0),
            "payback_yr":      round(pay, 1),
            "npv_eur":         eco["npv_eur"],
            "irr_pct":         eco["irr_pct"],
            "pnrr_grant_eur":  sc.get("pnrr_grant", 0),
            "cer_annual_eur":  sc.get("cer_annual", 0),
            "loads_breakdown": loads_r.get("breakdown", {}),
        }
        st.download_button(
            "⬇ Download report (JSON)",
            data=json.dumps(summary, indent=2, ensure_ascii=False),
            file_name=f"pv_school_{inst_name.replace(' ','_')}.json",
            mime="application/json",
        )

    # ── Raw data tab ──────────────────────────────────────────────────────────
    with t_raw:
        st.json(d)

    st.divider()
    ca, cb = st.columns(2)
    with ca:
        if st.button("← Back to system sizing", use_container_width=True): go_to(5)
    with cb:
        if st.button("🔄 Start over", use_container_width=True):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if "step" not in st.session_state:
        st.session_state["step"] = 1

    step = ss("step", 1)

    st.markdown(
        '<div style="font-size:22px;font-weight:700;color:#111827;'
        'margin-bottom:.2rem">🏫 PV Engine — School Edition</div>'
        '<div style="font-size:13px;color:#6b7280;margin-bottom:1.5rem">'
        'Solar PV feasibility analysis for educational institutions</div>',
        unsafe_allow_html=True,
    )

    render_progress(step)

    if step == 1:
        step_location()
    elif step == 2:
        step_institution()
    elif step == 3:
        step_occupancy()
    elif step == 4:
        step_energy()
    elif step == 5:
        step_system()
    elif step == 6:
        step_results()


if __name__ == "__main__":
    main()
