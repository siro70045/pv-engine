"""
branding/meridiana_theme.py

Meridiana brand theme for Streamlit apps.
Applies palette + typography + component styles via CSS injection.

Usage:
    from branding.meridiana_theme import apply_meridiana_theme, render_header
    apply_meridiana_theme()  # call once at top of streamlit script
    render_header(subtitle="...")

Notes:
    - Newsreader and Inter are loaded from Google Fonts. If Google Fonts
      is unreachable, browsers fall back to Georgia (titles) and the
      system sans stack (body) as declared in the CSS font-family chains.
"""

from __future__ import annotations

import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# Palette constants — exported for use in app code (charts, plotly, etc.)
# ──────────────────────────────────────────────────────────────────────────────

# Background
BG_PRIMARY     = "#EFE7DA"  # crema, sfondo principale
BG_SECONDARY   = "#F5EFE6"  # crema più chiaro, card e widget
BG_ELEVATED    = "#FBF7F0"  # crema chiarissimo, elementi sopra

# Text
TEXT_PRIMARY   = "#1A1612"  # nero caldo
TEXT_SECONDARY = "#4A453E"  # marrone scuro
TEXT_TERTIARY  = "#8B857A"  # marrone medio (placeholder, hint)

# Accents
ACCENT_YELLOW       = "#E8B547"   # giallo caldo (CTA, link, focus)
ACCENT_YELLOW_HOVER = "#D49E2E"
ACCENT_GREEN        = "#5B7553"   # verde rosmarino (success)
ACCENT_RED          = "#B5523A"   # rosso terroso (error, warning)

# Borders
BORDER_LIGHT  = "#E5DCC9"
BORDER_MEDIUM = "#C9BCA0"


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def apply_meridiana_theme(page_title: str = "Meridiana",
                          page_icon: str = "☀") -> None:
    """Apply Meridiana branding to a Streamlit page.

    Sets page config + injects CSS for palette, fonts, and component
    styles. Call at the very top of the script, BEFORE any other
    Streamlit command.
    """
    st.set_page_config(
        page_title=page_title,
        page_icon=page_icon,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(_MERIDIANA_CSS, unsafe_allow_html=True)


def render_header(subtitle: str = "") -> None:
    """Render the Meridiana header at top of page."""
    suffix = f"— {subtitle}" if subtitle else ""
    html = f"""
    <div class="meridiana-header">
        ☀ <span class="accent">Meridiana</span> {suffix}
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────────────────────
# CSS payload
# ──────────────────────────────────────────────────────────────────────────────

_MERIDIANA_CSS = """
<style>
/* --- Google Fonts (fallback to Georgia / system sans on failure) --- */
@import url('https://fonts.googleapis.com/css2?family=Newsreader:ital,wght@0,400..700;1,400..600&family=Inter:wght@400..700&display=swap');

/* --- Reset & background --- */
.stApp {
    background-color: #EFE7DA;
    color: #1A1612;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}

/* --- Sidebar --- */
[data-testid="stSidebar"] {
    background-color: #F5EFE6;
    border-right: 1px solid #E5DCC9;
}

/* --- Titles in serif --- */
h1, h2, h3, h4 {
    font-family: 'Newsreader', Georgia, serif !important;
    color: #1A1612;
    font-weight: 600;
    letter-spacing: -0.01em;
}
h1 { font-size: 2.4rem; line-height: 1.15; }
h2 { font-size: 1.8rem; line-height: 1.2; }
h3 { font-size: 1.4rem; line-height: 1.25; }
h4 { font-size: 1.15rem; line-height: 1.3; }

/* --- Body text --- */
p, .stMarkdown, label, .stText {
    font-family: 'Inter', sans-serif;
    color: #4A453E;
    line-height: 1.6;
}

/* --- Buttons --- */
.stButton > button {
    background-color: #E8B547;
    color: #1A1612;
    border: none;
    border-radius: 6px;
    padding: 0.6rem 1.4rem;
    font-family: 'Inter', sans-serif;
    font-weight: 600;
    font-size: 0.95rem;
    transition: background-color 200ms ease;
}
.stButton > button:hover {
    background-color: #D49E2E;
    color: #1A1612;
}
.stButton > button:focus {
    box-shadow: 0 0 0 3px rgba(232, 181, 71, 0.3);
    outline: none;
}

/* --- Inputs --- */
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div {
    background-color: #FBF7F0;
    border: 1px solid #C9BCA0;
    border-radius: 4px;
    font-family: 'Inter', sans-serif;
    color: #1A1612;
}
.stTextInput > div > div > input:focus,
.stNumberInput > div > div > input:focus {
    border-color: #E8B547;
    box-shadow: 0 0 0 2px rgba(232, 181, 71, 0.15);
}

/* --- Metric (numeric cards) --- */
[data-testid="stMetricValue"] {
    font-family: 'Inter', sans-serif;
    font-feature-settings: 'tnum';
    color: #1A1612;
    font-size: 2rem;
    font-weight: 600;
}
[data-testid="stMetricLabel"] {
    font-family: 'Inter', sans-serif;
    color: #8B857A;
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
}
[data-testid="stMetricDelta"] {
    font-family: 'Inter', sans-serif;
}

/* --- Tabs --- */
.stTabs [data-baseweb="tab-list"] {
    gap: 1rem;
    border-bottom: 1px solid #E5DCC9;
}
.stTabs [data-baseweb="tab"] {
    font-family: 'Inter', sans-serif;
    font-weight: 500;
    color: #4A453E;
}
.stTabs [aria-selected="true"] {
    color: #1A1612 !important;
    border-bottom: 2px solid #E8B547;
}

/* --- Expanders --- */
.streamlit-expanderHeader {
    background-color: #F5EFE6;
    border: 1px solid #E5DCC9;
    border-radius: 6px;
    font-family: 'Inter', sans-serif;
    font-weight: 500;
    color: #1A1612;
}

/* --- Dataframe --- */
.stDataFrame {
    font-family: 'Inter', sans-serif;
    font-feature-settings: 'tnum';
}

/* --- Dividers / horizontal rules --- */
hr {
    border: none;
    border-top: 1px solid #C9BCA0;
    margin: 2rem 0;
}

/* --- Links --- */
a {
    color: #B5523A;
    text-decoration: none;
    border-bottom: 1px solid rgba(181, 82, 58, 0.3);
    transition: border-color 200ms ease;
}
a:hover {
    border-bottom-color: #B5523A;
}

/* --- Success / Info / Warning / Error boxes --- */
.stAlert[data-baseweb="notification"] {
    font-family: 'Inter', sans-serif;
    border-radius: 6px;
}

/* --- Hide Streamlit default footer + menu --- */
footer { visibility: hidden; }
#MainMenu { visibility: hidden; }

/* --- Custom Meridiana header bar (top of app) --- */
.meridiana-header {
    font-family: 'Newsreader', Georgia, serif;
    font-size: 1.3rem;
    font-weight: 600;
    color: #1A1612;
    padding: 0.5rem 0 1.5rem 0;
    border-bottom: 1px solid #E5DCC9;
    margin-bottom: 1.5rem;
}
.meridiana-header span.accent {
    color: #E8B547;
    font-style: italic;
}
</style>
"""
