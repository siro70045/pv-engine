"""
test_meter_map.py — Multi-meter map prototype for PV Engine School Edition.

Phase 1: validate polygon-per-meter interaction flow before integrating
into app_school.py.

Launch:
    streamlit run test_meter_map.py
"""

import json
import math

import folium
import requests
import streamlit as st
from folium.plugins import Draw
from streamlit_folium import st_folium

st.set_page_config(page_title="Meter Map Prototype", layout="wide")

# ── Color palette ─────────────────────────────────────────────────────────────

METER_COLORS = [
    ("blue",   "#1E88E5"),
    ("orange", "#FB8C00"),
    ("green",  "#43A047"),
    ("purple", "#8E24AA"),
    ("yellow", "#FDD835"),
    ("red",    "#E53935"),
]


def get_next_color(used_colors: list) -> tuple[str, str]:
    for name, hex_code in METER_COLORS:
        if hex_code not in used_colors:
            return name, hex_code
    return METER_COLORS[0]


# ── Session-state initialisation ──────────────────────────────────────────────

def init_state() -> None:
    if "meters" not in st.session_state:
        st.session_state.meters = []
    if "active_meter_id" not in st.session_state:
        st.session_state.active_meter_id = None
    if "view_mode" not in st.session_state:
        st.session_state.view_mode = "edit"
    if "map_center" not in st.session_state:
        st.session_state.map_center = [44.601, 10.636]  # Borzano di Albinea
    if "next_meter_id" not in st.session_state:
        st.session_state.next_meter_id = 1


init_state()


# ── Area calculation ──────────────────────────────────────────────────────────

def calc_polygon_area_m2(geojson_feature: dict) -> float:
    """
    Calcola l'area di un poligono GeoJSON in m².
    Usa la formula di Shoelace su coordinate geografiche
    convertite in metri (approssimazione piana valida per
    edifici < 1 km²).

    Il GeoJSON ha coordinate in formato [lon, lat].
    """
    try:
        coords = geojson_feature["geometry"]["coordinates"][0]
        if len(coords) < 3:
            return 0.0

        lat_avg = sum(c[1] for c in coords) / len(coords)
        lat_to_m = 111320.0
        lon_to_m = 111320.0 * math.cos(math.radians(lat_avg))

        points = [(c[0] * lon_to_m, c[1] * lat_to_m) for c in coords]

        n = len(points)
        area = 0.0
        for i in range(n - 1):
            area += points[i][0] * points[i + 1][1]
            area -= points[i + 1][0] * points[i][1]
        area = abs(area) / 2.0

        return area
    except Exception:
        return 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_active_meter() -> dict | None:
    for m in st.session_state.meters:
        if m["id"] == st.session_state.active_meter_id:
            return m
    return None


def get_active_meter_name() -> str:
    meter = get_active_meter()
    return meter["name"] if meter else "none"


# ── Sidebar panel ─────────────────────────────────────────────────────────────

def render_sidebar_panel() -> None:
    st.markdown("### Electrical meters")
    st.caption(
        "Create one meter per electricity bill (POD). "
        "Draw polygons on the map to mark which buildings "
        "are connected to each meter."
    )

    if not st.session_state.meters:
        st.info("No meters yet. Add one below to start.")
    else:
        for meter in st.session_state.meters:
            is_active = meter["id"] == st.session_state.active_meter_id
            border = "2px solid #fff" if is_active else "1px solid #444"
            st.markdown(
                f'<div style="border:{border}; padding:8px; '
                f'margin-bottom:6px; border-radius:4px;">',
                unsafe_allow_html=True,
            )

            col_a, col_b = st.columns([3, 1])
            with col_a:
                pv_icon = "⚡ " if meter.get("is_pv_target") else ""
                st.markdown(
                    f'<span style="color:{meter["color_hex"]}; font-size:20px">●</span> '
                    f'<strong>{pv_icon}{meter["name"]}</strong>',
                    unsafe_allow_html=True,
                )
            with col_b:
                n_poly = len(meter.get("polygons", []))
                total_area = sum(
                    calc_polygon_area_m2(p) for p in meter.get("polygons", [])
                )
                if total_area > 0:
                    st.caption(f"{n_poly} bldg · ~{total_area:.0f} m²")
                else:
                    st.caption(f"{n_poly} bldg")

            btn_cols = st.columns(3)
            with btn_cols[0]:
                label = "✓ Editing" if is_active else "Edit"
                if st.button(label, key=f"edit_{meter['id']}",
                             use_container_width=True):
                    st.session_state.active_meter_id = meter["id"]
                    st.session_state.view_mode = "edit"
                    st.rerun()
            with btn_cols[1]:
                pv_label = "⚡ PV" if meter.get("is_pv_target") else "Set PV"
                if st.button(pv_label, key=f"pv_{meter['id']}",
                             use_container_width=True):
                    currently_set = meter.get("is_pv_target", False)
                    for m in st.session_state.meters:
                        m["is_pv_target"] = (
                            m["id"] == meter["id"] and not currently_set
                        )
                    st.rerun()
            with btn_cols[2]:
                if st.button("🗑", key=f"del_{meter['id']}",
                             use_container_width=True):
                    st.session_state.meters = [
                        m for m in st.session_state.meters
                        if m["id"] != meter["id"]
                    ]
                    if st.session_state.active_meter_id == meter["id"]:
                        st.session_state.active_meter_id = None
                    st.rerun()

            st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("---")

    with st.expander(
        "➕ Add new meter",
        expanded=(len(st.session_state.meters) == 0),
    ):
        new_name = st.text_input(
            "Meter name",
            value=f"Meter {st.session_state.next_meter_id}",
            key="new_meter_name",
        )
        if st.button("Create meter", key="create_meter",
                     use_container_width=True, type="primary"):
            used_colors = [m["color_hex"] for m in st.session_state.meters]
            color_name, color_hex = get_next_color(used_colors)
            new_meter = {
                "id":          st.session_state.next_meter_id,
                "name":        new_name,
                "color_name":  color_name,
                "color_hex":   color_hex,
                "is_pv_target": len(st.session_state.meters) == 0,
                "polygons":    [],
            }
            st.session_state.meters.append(new_meter)
            st.session_state.active_meter_id = new_meter["id"]
            st.session_state.next_meter_id += 1
            st.session_state.view_mode = "edit"
            st.rerun()

    st.markdown("---")

    view_cols = st.columns(2)
    with view_cols[0]:
        if st.button(
            "👁 View all",
            use_container_width=True,
            disabled=(st.session_state.view_mode == "view_all"),
        ):
            st.session_state.view_mode = "view_all"
            st.session_state.active_meter_id = None
            st.rerun()
    with view_cols[1]:
        if st.button(
            "✏ Edit mode",
            use_container_width=True,
            disabled=(st.session_state.view_mode == "edit"),
        ):
            st.session_state.view_mode = "edit"
            if st.session_state.meters and not st.session_state.active_meter_id:
                st.session_state.active_meter_id = st.session_state.meters[0]["id"]
            st.rerun()

    if st.session_state.meters:
        total_all = sum(
            calc_polygon_area_m2(p)
            for m in st.session_state.meters
            for p in m.get("polygons", [])
        )
        pv_meter = next(
            (m for m in st.session_state.meters if m.get("is_pv_target")), None
        )
        pv_area = (
            sum(calc_polygon_area_m2(p) for p in pv_meter.get("polygons", []))
            if pv_meter
            else 0
        )

        st.markdown("---")
        st.caption(
            f"Total mapped area: ~{total_all:.0f} m²\n"
            f"PV target roof: ~{pv_area:.0f} m²"
        )

    with st.expander("🔍 Debug state"):
        st.json({
            "view_mode":       st.session_state.view_mode,
            "active_meter_id": st.session_state.active_meter_id,
            "n_meters":        len(st.session_state.meters),
            "total_polygons":  sum(
                len(m["polygons"]) for m in st.session_state.meters
            ),
        })


# ── Map panel ─────────────────────────────────────────────────────────────────

def render_map() -> None:
    mode_label = (
        "View all meters"
        if st.session_state.view_mode == "view_all"
        else f"Editing: {get_active_meter_name()}"
    )
    st.markdown(f"### Map — {mode_label}")

    col_search, col_btn = st.columns([4, 1])
    with col_search:
        search_query = st.text_input(
            "Search address",
            placeholder="Via Roma 1, Milano...",
            label_visibility="collapsed",
            key="address_search_input",
        )
    with col_btn:
        do_search = st.button("🔍", key="address_search_btn")

    if do_search and search_query:
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": search_query,
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "it",
                },
                headers={"User-Agent": "PVEngine-SchoolMap/1.0"},
                timeout=5,
            )
            results = resp.json()
            if results:
                lat = float(results[0]["lat"])
                lon = float(results[0]["lon"])
                st.session_state.map_center = [lat, lon]
                st.success(
                    f"Found: {results[0].get('display_name', '').split(',')[0]}"
                )
            else:
                st.warning("Address not found. Try a more specific search.")
        except Exception as e:
            st.warning(f"Search unavailable: {e}")

    m = folium.Map(
        location=st.session_state.map_center,
        zoom_start=18,
        tiles="OpenStreetMap",
    )

    folium.TileLayer(
        tiles=(
            "https://server.arcgisonline.com/ArcGIS/rest/services"
            "/World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        attr="Esri World Imagery",
        name="Satellite",
    ).add_to(m)
    folium.LayerControl().add_to(m)

    # Draw existing polygons
    for meter in st.session_state.meters:
        is_active = meter["id"] == st.session_state.active_meter_id

        if st.session_state.view_mode == "view_all":
            fill_opacity, stroke_opacity = 0.5, 1.0
        elif is_active:
            fill_opacity, stroke_opacity = 0.5, 1.0
        else:
            fill_opacity, stroke_opacity = 0.15, 0.3

        for poly_data in meter["polygons"]:
            area_m2 = calc_polygon_area_m2(poly_data)
            tooltip_text = (
                meter["name"]
                + (" ⚡ PV" if meter.get("is_pv_target") else "")
                + (f"\n~{area_m2:.0f} m²" if area_m2 > 0 else "")
            )
            folium.GeoJson(
                poly_data,
                style_function=lambda x,
                    c=meter["color_hex"],
                    fo=fill_opacity,
                    so=stroke_opacity: {
                    "fillColor": c,
                    "color":     c,
                    "weight":    2,
                    "fillOpacity": fo,
                    "opacity":   so,
                },
                tooltip=folium.Tooltip(tooltip_text),
            ).add_to(m)

    # Draw control — only in edit mode with an active meter
    if st.session_state.view_mode == "edit" and st.session_state.active_meter_id:
        active_meter = get_active_meter()
        c = active_meter["color_hex"]
        draw = Draw(
            export=False,
            position="topleft",
            draw_options={
                "polygon": {
                    "shapeOptions": {
                        "color":       c,
                        "fillColor":   c,
                        "fillOpacity": 0.5,
                    },
                    "allowIntersection": False,
                    "showArea": True,
                },
                "polyline":     False,
                "rectangle":    False,
                "circle":       False,
                "marker":       False,
                "circlemarker": False,
            },
            edit_options={"edit": True, "remove": True},
        )
        draw.add_to(m)

    map_data = st_folium(
        m,
        width=None,
        height=600,
        returned_objects=["last_active_drawing", "all_drawings"],
        key="main_school_map",
    )

    if map_data and map_data.get("all_drawings"):
        drawings = map_data["all_drawings"]
        if drawings:
            n_new = len(drawings)
            if st.button(
                f"💾 Save {n_new} drawn building(s) to {get_active_meter_name()}",
                key="save_drawings",
                type="primary",
            ):
                active_meter = get_active_meter()
                if active_meter:
                    active_meter["polygons"] = drawings
                    st.success(f"Saved {len(drawings)} building(s).")


# ── Page layout ───────────────────────────────────────────────────────────────

st.title("🗺️ Meter Map Prototype")
st.caption(
    "Multi-meter mapping prototype for PV Engine School Edition. "
    "This is a test page to validate the interaction flow "
    "before integrating into the main app."
)

col_map, col_panel = st.columns([3, 1])

with col_panel:
    render_sidebar_panel()

with col_map:
    render_map()

# ── Validation summary ────────────────────────────────────────────────────────

st.markdown("---")
st.markdown("### Validation summary")

total_polygons = sum(len(m["polygons"]) for m in st.session_state.meters)
has_pv_target  = any(m.get("is_pv_target") for m in st.session_state.meters)
empty_meters   = [
    m["name"] for m in st.session_state.meters if not m["polygons"]
]

col_v1, col_v2, col_v3 = st.columns(3)
with col_v1:
    st.metric("Total meters",   len(st.session_state.meters))
with col_v2:
    st.metric("Total polygons", total_polygons)
with col_v3:
    st.metric("PV target set",  "Yes" if has_pv_target else "No")

if empty_meters:
    st.warning(f"⚠️ Meters without polygons: {', '.join(empty_meters)}")

if not has_pv_target and st.session_state.meters:
    st.warning("⚠️ No meter marked as PV target. Click 'Set PV' on one.")

with st.expander("Export state (JSON)"):
    export_data = {
        "meters": [
            {
                "id":           m["id"],
                "name":         m["name"],
                "color":        m["color_hex"],
                "is_pv_target": m.get("is_pv_target", False),
                "polygons": [
                    {
                        "geometry":   p.get("geometry"),
                        "properties": p.get("properties", {}),
                    }
                    for p in m["polygons"]
                ],
            }
            for m in st.session_state.meters
        ]
    }
    st.json(export_data)
