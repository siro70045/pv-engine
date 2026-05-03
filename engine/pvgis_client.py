"""
engine/pvgis_client.py

Client for the PVGIS REST API (European Commission, JRC).
https://re.jrc.ec.europa.eu/api/v5_2/

Fetches the Typical Meteorological Year (TMY) for any location
and aggregates the 8 760 hourly records into the monthly arrays
required by the envelope and solar modules.

API documentation:
    https://joint-research-centre.ec.europa.eu/pvgis-photovoltaic-geographical-information-system/getting-started-pvgis/api-non-interactive-service_en

Endpoint used:
    GET /api/v5_2/tmy
    Parameters:
        lat          Latitude  [°]  WGS84
        lon          Longitude [°]  WGS84
        usehorizon   1 = include horizon profile in shading calculation
        outputformat json
        browser      0 (machine request)

Notes
-----
- PVGIS TMY covers Europe, Africa, Asia and the Americas using
  SARAH-3 / ERA5 satellite datasets (2005–2020 typical).
- Irradiance values in the TMY are instantaneous [W/m²] measured
  at the centre of each hour.  Multiplying by 1 h gives [Wh/m²].
- The module works entirely with standard-library modules
  (urllib, json, pathlib) so no extra dependencies are needed.
"""

from __future__ import annotations

import json
import time
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── API settings ──────────────────────────────────────────────────────────────
PVGIS_BASE_URL   = "https://re.jrc.ec.europa.eu/api/v5_2"
REQUEST_TIMEOUT  = 30        # seconds
MAX_RETRIES      = 3
RETRY_DELAY_S    = 5         # seconds between retries

# ── Cache settings ────────────────────────────────────────────────────────────
# TMY data rarely changes; caching avoids redundant API calls during dev.
CACHE_DIR        = Path(".pvgis_cache")
CACHE_ENABLED    = True      # set to False to always hit the live API


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class HourlyRecord:
    """One hour of TMY data as returned by PVGIS."""
    month: int            # 1–12
    day: int              # 1–31
    hour: int             # 0–23  (UTC)
    T2m: float            # Dry-bulb temperature at 2 m  [°C]
    RH: float             # Relative humidity            [%]
    G_h: float            # Global horizontal irradiance [W/m²]
    Gb_n: float           # Direct (beam) normal irr.   [W/m²]
    Gd_h: float           # Diffuse horizontal irr.     [W/m²]
    IR_h: float           # Infrared downward irr.      [W/m²]
    WS10m: float          # Wind speed at 10 m           [m/s]
    WD10m: float          # Wind direction               [°]
    SP: float             # Surface pressure             [Pa]


@dataclass
class MonthlyClimate:
    """
    Monthly aggregated climate values derived from the TMY.
    All irradiance values are in [kWh/m²/month] (sum over all hours).
    Temperature values are monthly averages [°C].
    """
    # Temperatures
    T_avg: list[float]         # monthly mean dry-bulb  [°C]       — 12 values
    T_min: list[float]         # monthly minimum        [°C]
    T_max: list[float]         # monthly maximum        [°C]

    # Horizontal irradiance
    GHI_monthly: list[float]   # global horizontal      [kWh/m²/month]
    DNI_monthly: list[float]   # direct normal          [kWh/m²/month]
    DHI_monthly: list[float]   # diffuse horizontal     [kWh/m²/month]

    # Wind
    WS_avg: list[float]        # monthly mean wind speed [m/s]

    # Humidity
    RH_avg: list[float]        # monthly mean RH         [%]


@dataclass
class PVGISLocation:
    """Metadata about the queried location returned by PVGIS."""
    latitude: float
    longitude: float
    elevation_m: float
    country: str
    dataset: str          # e.g. "PVGIS-SARAH3"
    horizon_profile: list[float] = field(default_factory=list)  # azimuth-dependent horizon [°]


@dataclass
class TMYResult:
    """Complete result of a TMY query."""
    location: PVGISLocation
    hourly: list[HourlyRecord]
    monthly: MonthlyClimate

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict (matches `_tmy_from_dict`)."""
        return {
            "location": {
                "latitude": self.location.latitude,
                "longitude": self.location.longitude,
                "elevation_m": self.location.elevation_m,
                "country": self.location.country,
                "dataset": self.location.dataset,
                "horizon_profile": list(self.location.horizon_profile),
            },
            "hourly": [
                {
                    "month": h.month, "day": h.day, "hour": h.hour,
                    "T2m": h.T2m, "RH": h.RH, "G_h": h.G_h,
                    "Gb_n": h.Gb_n, "Gd_h": h.Gd_h, "IR_h": h.IR_h,
                    "WS10m": h.WS10m, "WD10m": h.WD10m, "SP": h.SP,
                }
                for h in self.hourly
            ],
            "monthly": {
                "T_avg":       list(self.monthly.T_avg),
                "T_min":       list(self.monthly.T_min),
                "T_max":       list(self.monthly.T_max),
                "GHI_monthly": list(self.monthly.GHI_monthly),
                "DNI_monthly": list(self.monthly.DNI_monthly),
                "DHI_monthly": list(self.monthly.DHI_monthly),
                "WS_avg":      list(self.monthly.WS_avg),
                "RH_avg":      list(self.monthly.RH_avg),
            },
        }


def _tmy_from_dict(data: dict) -> TMYResult:
    """Inverse of `TMYResult.to_dict` — used by the API-layer disk cache."""
    loc = data["location"]
    location = PVGISLocation(
        latitude=float(loc["latitude"]),
        longitude=float(loc["longitude"]),
        elevation_m=float(loc.get("elevation_m", 0.0)),
        country=str(loc.get("country", "unknown")),
        dataset=str(loc.get("dataset", "unknown")),
        horizon_profile=[float(v) for v in loc.get("horizon_profile", [])],
    )
    hourly = [
        HourlyRecord(
            month=int(h["month"]), day=int(h["day"]), hour=int(h["hour"]),
            T2m=float(h["T2m"]), RH=float(h["RH"]),
            G_h=float(h["G_h"]), Gb_n=float(h["Gb_n"]), Gd_h=float(h["Gd_h"]),
            IR_h=float(h["IR_h"]), WS10m=float(h["WS10m"]),
            WD10m=float(h["WD10m"]), SP=float(h["SP"]),
        )
        for h in data["hourly"]
    ]
    m = data["monthly"]
    monthly = MonthlyClimate(
        T_avg=[float(v) for v in m["T_avg"]],
        T_min=[float(v) for v in m["T_min"]],
        T_max=[float(v) for v in m["T_max"]],
        GHI_monthly=[float(v) for v in m["GHI_monthly"]],
        DNI_monthly=[float(v) for v in m["DNI_monthly"]],
        DHI_monthly=[float(v) for v in m["DHI_monthly"]],
        WS_avg=[float(v) for v in m["WS_avg"]],
        RH_avg=[float(v) for v in m["RH_avg"]],
    )
    return TMYResult(location=location, hourly=hourly, monthly=monthly)


# ══════════════════════════════════════════════════════════════════════════════
# CACHE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _cache_key(lat: float, lon: float) -> str:
    """Deterministic cache filename from coordinates (rounded to 4 decimals)."""
    key = f"tmy_{lat:.4f}_{lon:.4f}"
    return hashlib.md5(key.encode()).hexdigest()[:12] + f"_{lat:.4f}_{lon:.4f}.json"


def _cache_path(lat: float, lon: float) -> Path:
    return CACHE_DIR / _cache_key(lat, lon)


def _load_cache(lat: float, lon: float) -> Optional[dict]:
    path = _cache_path(lat, lon)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_cache(lat: float, lon: float, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(lat, lon)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))


# ══════════════════════════════════════════════════════════════════════════════
# API REQUEST
# ══════════════════════════════════════════════════════════════════════════════

def _build_tmy_url(lat: float, lon: float) -> str:
    params = urllib.parse.urlencode({
        "lat":          f"{lat:.6f}",
        "lon":          f"{lon:.6f}",
        "usehorizon":   1,
        "outputformat": "json",
        "browser":      0,
    })
    return f"{PVGIS_BASE_URL}/tmy?{params}"


def _fetch_raw(url: str) -> dict:
    """
    HTTP GET with retry logic.  Raises PVGISError on failure.
    """
    last_error: Exception = RuntimeError("No attempt made")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "pv-feasibility-engine/1.0"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_error = PVGISError(
                f"PVGIS HTTP {e.code}: {e.reason}. "
                f"Check coordinates are within PVGIS coverage area."
            )
            break   # HTTP errors are not retryable
        except urllib.error.URLError as e:
            last_error = PVGISError(
                f"Network error reaching PVGIS (attempt {attempt}/{MAX_RETRIES}): {e.reason}"
            )
        except json.JSONDecodeError as e:
            last_error = PVGISError(f"PVGIS returned invalid JSON: {e}")
            break

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_S)

    raise last_error


# ══════════════════════════════════════════════════════════════════════════════
# TMY PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _parse_tmy(raw: dict) -> TMYResult:
    """
    Parse the PVGIS JSON response into typed Python objects.

    PVGIS TMY JSON structure:
    {
      "inputs":  { "location": {...}, "meteo_data": {...} },
      "outputs": {
        "tmy_hourly": [
          { "time(UTC)": "0001-01-01 00:10",
            "T2m": ..., "RH": ..., "G(h)": ..., "Gb(n)": ...,
            "Gd(h)": ..., "IR(h)": ..., "WS10m": ..., "WD10m": ..., "SP": ... },
          ...   (8760 records)
        ],
        "months_selected": [...],
        "horizon": [...]   (optional)
      },
      "meta": { ... }
    }
    """
    try:
        inputs  = raw["inputs"]
        outputs = raw["outputs"]
    except KeyError as e:
        raise PVGISError(f"Unexpected PVGIS response structure: missing key {e}") from e

    # ── Location metadata ──
    loc_raw  = inputs.get("location", {})
    meta_raw = inputs.get("meteo_data", {})
    horizon_raw = outputs.get("horizon", [])

    location = PVGISLocation(
        latitude    = float(loc_raw.get("latitude",  0)),
        longitude   = float(loc_raw.get("longitude", 0)),
        elevation_m = float(loc_raw.get("elevation", 0)),
        country     = str(loc_raw.get("country", "unknown")),
        dataset     = str(meta_raw.get("radiation_db", "unknown")),
        horizon_profile = [float(h.get("elevation", 0)) for h in horizon_raw],
    )

    # ── Hourly records ──
    hourly_raw = outputs.get("tmy_hourly", [])
    if len(hourly_raw) not in (8760, 8784):   # 8784 = leap-year padding
        raise PVGISError(
            f"Expected 8760 hourly records, got {len(hourly_raw)}. "
            f"PVGIS response may be malformed."
        )

    hourly: list[HourlyRecord] = []
    for row in hourly_raw:
        time_str = row.get("time(UTC)", "")
        try:
            # PVGIS restituisce due formati diversi a seconda della versione API:
            #   Vecchio: "0001-MM-DD HH:MM"   (spazio come separatore)
            #   Nuovo:   "YYYYMMDD:HHMM"      (come il CSV, introdotto ~2024)
            if ":" in time_str and " " not in time_str:
                # Formato nuovo: "20140101:0000"
                month = int(time_str[4:6])
                day   = int(time_str[6:8])
                hour  = int(time_str[9:11])
            else:
                # Formato vecchio: "0001-01-15 06:10"
                date_part, time_part = time_str.split(" ")
                _, mm, dd = date_part.split("-")
                month = int(mm)
                day   = int(dd)
                hour  = int(time_part.split(":")[0])
        except (ValueError, IndexError):
            raise PVGISError(f"Cannot parse PVGIS time string: '{time_str}'")

        hourly.append(HourlyRecord(
            month  = month,
            day    = day,
            hour   = hour,
            T2m    = float(row.get("T2m",    0)),
            RH     = float(row.get("RH",     50)),
            G_h    = float(row.get("G(h)",   0)),
            Gb_n   = float(row.get("Gb(n)",  0)),
            Gd_h   = float(row.get("Gd(h)",  0)),
            IR_h   = float(row.get("IR(h)",  0)),
            WS10m  = float(row.get("WS10m",  0)),
            WD10m  = float(row.get("WD10m",  0)),
            SP     = float(row.get("SP",     101325)),
        ))

    # ── Monthly aggregation ──
    monthly = _aggregate_monthly(hourly)

    return TMYResult(location=location, hourly=hourly, monthly=monthly)


# ══════════════════════════════════════════════════════════════════════════════
# MONTHLY AGGREGATION
# ══════════════════════════════════════════════════════════════════════════════

def _aggregate_monthly(hourly: list[HourlyRecord]) -> MonthlyClimate:
    """
    Aggregate 8 760 hourly records into 12 monthly values.

    Irradiance: W/m² × 1 h = Wh/m²  →  sum / 1000 = kWh/m²/month
    Temperature: simple arithmetic mean of all hours in the month.
    """
    # Buckets for each month (index 0 = January)
    T_sum   = [0.0] * 12;  T_vals  = [[] for _ in range(12)]
    GHI_sum = [0.0] * 12
    DNI_sum = [0.0] * 12
    DHI_sum = [0.0] * 12
    WS_sum  = [0.0] * 12
    RH_sum  = [0.0] * 12
    count   = [0]   * 12

    for rec in hourly:
        m = rec.month - 1   # 0-indexed
        T_vals[m].append(rec.T2m)
        T_sum[m]   += rec.T2m
        GHI_sum[m] += rec.G_h    # Wh/m²  (W/m² × 1 h)
        DNI_sum[m] += rec.Gb_n
        DHI_sum[m] += rec.Gd_h
        WS_sum[m]  += rec.WS10m
        RH_sum[m]  += rec.RH
        count[m]   += 1

    def safe_avg(total: float, n: int) -> float:
        return round(total / n, 2) if n > 0 else 0.0

    return MonthlyClimate(
        T_avg        = [safe_avg(T_sum[m],  count[m]) for m in range(12)],
        T_min        = [round(min(T_vals[m]), 2) if T_vals[m] else 0.0 for m in range(12)],
        T_max        = [round(max(T_vals[m]), 2) if T_vals[m] else 0.0 for m in range(12)],
        GHI_monthly  = [round(GHI_sum[m] / 1000, 2) for m in range(12)],
        DNI_monthly  = [round(DNI_sum[m] / 1000, 2) for m in range(12)],
        DHI_monthly  = [round(DHI_sum[m] / 1000, 2) for m in range(12)],
        WS_avg       = [safe_avg(WS_sum[m], count[m]) for m in range(12)],
        RH_avg       = [safe_avg(RH_sum[m], count[m]) for m in range(12)],
    )


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC INTERFACE
# ══════════════════════════════════════════════════════════════════════════════

class PVGISError(Exception):
    """Raised when the PVGIS API returns an error or unexpected data."""


def fetch_tmy(
    lat: float,
    lon: float,
    use_cache: bool = True,
) -> TMYResult:
    """
    Fetch the Typical Meteorological Year for any location via PVGIS.

    Parameters
    ----------
    lat : float
        Latitude  [°]  –90 to +90.  Positive = North.
    lon : float
        Longitude [°]  –180 to +180.  Positive = East.
    use_cache : bool
        If True (default), saves the raw API response to disk and
        reuses it on subsequent calls with the same coordinates.
        Set to False to always fetch fresh data.

    Returns
    -------
    TMYResult
        Parsed and aggregated TMY data ready to pass to calc_thermal_demand()
        and the solar module.

    Raises
    ------
    PVGISError
        On any API or parsing error.
    ValueError
        If coordinates are out of range.

    Examples
    --------
    >>> tmy = fetch_tmy(lat=44.5, lon=11.3)
    >>> print(tmy.monthly.T_avg)
    [1.8, 4.1, 8.3, ...]
    >>> print(tmy.monthly.GHI_monthly)
    [38.2, 58.7, 102.4, ...]
    """
    if not (-90 <= lat <= 90):
        raise ValueError(f"Latitude {lat} out of range [-90, 90].")
    if not (-180 <= lon <= 180):
        raise ValueError(f"Longitude {lon} out of range [-180, 180].")

    # Try cache first
    if use_cache and CACHE_ENABLED:
        cached = _load_cache(lat, lon)
        if cached is not None:
            return _parse_tmy(cached)

    # Live API call
    url = _build_tmy_url(lat, lon)
    raw = _fetch_raw(url)

    # Persist to cache
    if use_cache and CACHE_ENABLED:
        _save_cache(lat, lon, raw)

    return _parse_tmy(raw)


# ══════════════════════════════════════════════════════════════════════════════
# CSV PARSER  (PVGIS TMY download format)
# ══════════════════════════════════════════════════════════════════════════════

def parse_pvgis_csv(csv_text: str) -> TMYResult:
    """
    Parse a PVGIS TMY CSV file (downloaded from the PVGIS web interface)
    into a TMYResult identical to the one produced by fetch_tmy().

    PVGIS CSV format
    ----------------
    The file has three sections separated by blank lines:

    1. Header metadata (variable number of lines):
         Latitude (decimal degrees): 44.601
         Longitude (decimal degrees): 10.636
         Elevation (m): 173.0
         Irradiance Time Offset (h): 0.1756
         month,year          ← months_selected table (12 rows)
         1,2011
         ...
         12,2015

    2. Data section — starts with the column header row:
         time(UTC),T2m,RH,G(h),Gb(n),Gd(h),IR(h),WS10m,WD10m,SP
       followed by exactly 8 760 data rows:
         20110101:0000,0.49,92.81,0.0,-0.0,0.0,276.42,0.96,203.0,99220.0

       Time format: YYYYMMDD:HHMM  (e.g. 20110101:0000 = Jan 1, 00:00)

    3. Footer — variable legend lines (ignored).

    Parameters
    ----------
    csv_text : str
        Full text content of the PVGIS TMY CSV file.

    Returns
    -------
    TMYResult
        Same structure as fetch_tmy() — fully interchangeable.

    Raises
    ------
    PVGISError
        If the file format is unrecognised or the data section has
        fewer than 8 760 rows.
    """
    lines = csv_text.splitlines()

    # ── 1. Extract header metadata ──────────────────────────────────────────
    lat = lon = elev = 0.0
    for line in lines[:10]:
        stripped = line.strip()
        if stripped.lower().startswith("latitude"):
            try:
                lat = float(stripped.split(":")[-1].strip())
            except ValueError:
                pass
        elif stripped.lower().startswith("longitude"):
            try:
                lon = float(stripped.split(":")[-1].strip())
            except ValueError:
                pass
        elif stripped.lower().startswith("elevation"):
            try:
                elev = float(stripped.split(":")[-1].strip())
            except ValueError:
                pass

    # ── 2. Find the data header row ─────────────────────────────────────────
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("time(UTC)"):
            header_idx = i
            break
    if header_idx is None:
        raise PVGISError(
            "Could not find 'time(UTC)' column header in PVGIS CSV. "
            "Make sure you downloaded the TMY file from PVGIS."
        )

    # ── 3. Parse 8 760 hourly rows ──────────────────────────────────────────
    hourly: list[HourlyRecord] = []
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        # Stop at footer (non-data lines don't match YYYYMMDD:HHMM pattern)
        if not stripped or ":" not in stripped[:13]:
            continue
        parts = stripped.split(",")
        if len(parts) < 9:
            continue

        # Parse timestamp: YYYYMMDD:HHMM
        ts = parts[0]   # e.g. "20110101:0000"
        try:
            month = int(ts[4:6])
            day   = int(ts[6:8])
            hour  = int(ts[9:11])
        except (ValueError, IndexError):
            continue    # skip malformed rows silently

        try:
            hourly.append(HourlyRecord(
                month = month,
                day   = day,
                hour  = hour,
                T2m   = float(parts[1]),
                RH    = float(parts[2]),
                G_h   = max(float(parts[3]), 0.0),   # clamp −0.0 artefacts
                Gb_n  = max(float(parts[4]), 0.0),
                Gd_h  = max(float(parts[5]), 0.0),
                IR_h  = float(parts[6]),
                WS10m = float(parts[7]),
                WD10m = float(parts[8]),
                SP    = float(parts[9]) if len(parts) > 9 else 101325.0,
            ))
        except (ValueError, IndexError):
            continue    # skip rows with missing/invalid values

    if len(hourly) != 8760:
        raise PVGISError(
            f"Expected 8 760 hourly records in PVGIS CSV, found {len(hourly)}. "
            f"The file may be incomplete or in an unexpected format."
        )

    # ── 4. Build location and aggregate monthly ─────────────────────────────
    location = PVGISLocation(
        latitude        = lat,
        longitude       = lon,
        elevation_m     = elev,
        country         = "",           # not present in CSV format
        dataset         = "PVGIS-SARAH3",
        horizon_profile = [],           # not present in CSV format
    )
    monthly = _aggregate_monthly(hourly)

    return TMYResult(location=location, hourly=hourly, monthly=monthly)


def tmy_from_csv_file(path: str | Path) -> TMYResult:
    """
    Load a PVGIS TMY CSV file from disk and return a TMYResult.

    Convenience wrapper around parse_pvgis_csv() for local files.

    Parameters
    ----------
    path : str or Path
        Path to the downloaded PVGIS TMY CSV file.

    Returns
    -------
    TMYResult

    Example
    -------
    >>> tmy = tmy_from_csv_file("tmy_44_601_10_636_2005_2023.csv")
    >>> print(tmy.monthly.GHI_monthly)
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PVGIS CSV file not found: {path}")
    if path.stat().st_size < 1000:
        raise PVGISError(f"File too small to be a valid PVGIS TMY CSV: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_pvgis_csv(text)


def print_tmy_summary(tmy: TMYResult) -> None:
    """Pretty-print a TMY result to stdout."""
    MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]
    SEP = "─" * 72
    loc = tmy.location
    m   = tmy.monthly

    print(f"\n{SEP}")
    print(f"  PVGIS TMY — {loc.country}  |  "
          f"lat {loc.latitude:.4f}°  lon {loc.longitude:.4f}°  "
          f"elev {loc.elevation_m:.0f} m")
    print(f"  Dataset: {loc.dataset}  |  {len(tmy.hourly)} hourly records")
    print(SEP)
    print(f"  {'Month':<5} {'T_avg':>6} {'T_min':>6} {'T_max':>6} "
          f"{'GHI':>8} {'DNI':>8} {'DHI':>8} {'WS':>6} {'RH':>6}")
    print(f"  {'':5} {'°C':>6} {'°C':>6} {'°C':>6} "
          f"{'kWh/m²':>8} {'kWh/m²':>8} {'kWh/m²':>8} {'m/s':>6} {'%':>6}")
    print(f"  {SEP}")
    for i in range(12):
        print(
            f"  {MONTHS[i]:<5}"
            f" {m.T_avg[i]:>6.1f}"
            f" {m.T_min[i]:>6.1f}"
            f" {m.T_max[i]:>6.1f}"
            f" {m.GHI_monthly[i]:>8.1f}"
            f" {m.DNI_monthly[i]:>8.1f}"
            f" {m.DHI_monthly[i]:>8.1f}"
            f" {m.WS_avg[i]:>6.1f}"
            f" {m.RH_avg[i]:>6.1f}"
        )
    annual_ghi = sum(m.GHI_monthly)
    annual_dni = sum(m.DNI_monthly)
    print(f"  {SEP}")
    print(f"  {'YEAR':<5} {sum(m.T_avg)/12:>6.1f}"
          f" {'':>6} {'':>6}"
          f" {annual_ghi:>8.1f}"
          f" {annual_dni:>8.1f}")
    print(f"{SEP}\n")
