"""
Build script for ``school/data/comuni_italiani.csv``.

Merges three official sources into a single per-comune table with centroid
coordinates and the official DPR 412/93 climate zone:

1. ISTAT — Elenco dei comuni italiani (codici e denominazioni)
   https://www.istat.it/storage/codici-unita-amministrative/Elenco-comuni-italiani.csv
   License: CC BY 3.0 IT (ISTAT standard reuse terms).

2. ISTAT — Confini delle unità amministrative a fini statistici,
   versione generalizzata, 1 gennaio 2025 (shapefile, WGS84 / UTM 32N)
   https://www.istat.it/storage/cartografia/confini_amministrativi/generalizzati/2025/Limiti01012025_g.zip
   License: CC BY 3.0 IT. Used only to compute polygon centroids.

3. ENEA SolarItaly — Gradi-giorno e zone climatiche dei comuni italiani
   (DPR 412/93 Allegato A, aggiornato ai decreti successivi; file datato
   5 luglio 2002, l'ultima revisione pubblicata in formato tabellare)
   http://www.solaritaly.enea.it/clisun/Dati/FilesCSV/GraGioWb.txt
   The table reproduces the content of a normative act (DPR 412/93 All. A),
   not subject to copyright per art. 5 L. 633/1941; ENEA cited as compiler.

Join strategy
-------------
Primary key is the 6-digit ISTAT code (PRO_COM_T). The ENEA file predates
the 2016+ province reshuffles (Sud Sardegna, Monza-Brianza, Fermo, BAT) and
~150 municipal mergers, so unmatched comuni fall back to:
  a. normalized-name match (unique nationwide, or unique within region);
  b. climate zone of the geographically nearest comune already matched
     (flagged ``zona_fonte=nearest_neighbor``, gradi_giorno left empty).

Run with a Python that has ``pyshp`` and ``pyproj`` installed (NOT required
by the engine at runtime — this script is build-time only):

    python -m venv /tmp/buildenv && /tmp/buildenv/bin/pip install pyshp pyproj
    /tmp/buildenv/bin/python school/data/build_comuni_dataset.py
"""

from __future__ import annotations

import csv
import io
import math
import sys
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

ISTAT_ELENCO_URL = (
    "https://www.istat.it/storage/codici-unita-amministrative/"
    "Elenco-comuni-italiani.csv"
)
ISTAT_LIMITI_URL = (
    "https://www.istat.it/storage/cartografia/confini_amministrativi/"
    "generalizzati/2025/Limiti01012025_g.zip"
)
ENEA_GG_URL = "http://www.solaritaly.enea.it/clisun/Dati/FilesCSV/GraGioWb.txt"

OUT_PATH = Path(__file__).parent / "comuni_italiani.csv"
WORK_DIR = Path("/tmp/comuni_build")


def normalize_name(name: str) -> str:
    """lowercase, strip accents/apostrophes/hyphens, collapse whitespace."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("'", " ").replace("’", " ").replace("-", " ")
    return " ".join(s.split())


def _download(url: str, dest: Path) -> Path:
    if not dest.exists():
        print(f"downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        dest.write_bytes(urllib.request.urlopen(req, timeout=120).read())
    return dest


def load_istat_elenco(path: Path) -> dict[str, dict]:
    """PRO_COM_T → {nome, sigla, regione}."""
    out: dict[str, dict] = {}
    with open(path, encoding="latin-1", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        idx = {h.strip(): i for i, h in enumerate(header)}
        i_code = idx["Codice Comune formato alfanumerico"]
        i_name = idx["Denominazione in italiano"]
        i_reg = idx["Denominazione Regione"]
        i_sigla = idx["Sigla automobilistica"]
        for row in reader:
            if not row or not row[i_code].strip():
                continue
            out[row[i_code].strip()] = {
                "nome": row[i_name].strip(),
                "sigla_provincia": row[i_sigla].strip(),
                "regione": row[i_reg].strip(),
            }
    return out


def load_centroids(shp_path: Path) -> dict[str, tuple[float, float]]:
    """PRO_COM_T → (lat, lon) from polygon centroid (UTM32N → WGS84)."""
    import shapefile  # pyshp
    from pyproj import Transformer
    from shapely.geometry import shape

    tr = Transformer.from_crs("EPSG:32632", "EPSG:4326", always_xy=True)
    sf = shapefile.Reader(str(shp_path))
    fields = [f[0] for f in sf.fields[1:]]
    i_code = fields.index("PRO_COM_T")
    out: dict[str, tuple[float, float]] = {}
    for i in range(len(sf)):
        rec = sf.record(i)
        geom = shape(sf.shape(i).__geo_interface__)
        c = geom.centroid
        lon, lat = tr.transform(c.x, c.y)
        out[rec[i_code]] = (round(lat, 6), round(lon, 6))
    return out


def load_enea(path: Path) -> tuple[dict[str, tuple[str, int]], dict[str, list]]:
    """Returns (code → (zona, gg), normalized name → [(code, zona, gg), ...])."""
    by_code: dict[str, tuple[str, int]] = {}
    by_name: dict[str, list] = {}
    with open(path, encoding="latin-1", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) != 8 or row[0].strip().lower() in ("", "cod reg"):
                continue
            try:
                prov, com = int(row[1]), int(row[2])
                gg = int(row[6])
            except ValueError:
                continue
            zona = row[7].strip().upper()
            if zona not in "ABCDEF" or len(zona) != 1:
                continue
            code = f"{prov:03d}{com:03d}"
            by_code[code] = (zona, gg)
            by_name.setdefault(normalize_name(row[4]), []).append((code, zona, gg))
    return by_code, by_name


def main() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    elenco_csv = _download(ISTAT_ELENCO_URL, WORK_DIR / "Elenco-comuni-italiani.csv")
    enea_txt = _download(ENEA_GG_URL, WORK_DIR / "GraGioWb.txt")
    limiti_zip = _download(ISTAT_LIMITI_URL, WORK_DIR / "Limiti01012025_g.zip")
    shp_dir = WORK_DIR / "limiti"
    if not shp_dir.exists():
        with zipfile.ZipFile(limiti_zip) as z:
            z.extractall(shp_dir)
    shp_path = next(shp_dir.glob("**/Com*_g_WGS84.shp"))

    elenco = load_istat_elenco(elenco_csv)
    centroids = load_centroids(shp_path)
    enea_by_code, enea_by_name = load_enea(enea_txt)

    # region of each ENEA code (first 3 digits = historical province) is not
    # directly known; we resolve name-fallback ambiguity via current region
    # of the *candidate* comuni sharing the name in the 2025 elenco.
    rows = []
    unmatched = []
    for code, info in sorted(elenco.items()):
        if code not in centroids:
            print(f"WARN: no centroid for {code} {info['nome']} — skipped")
            continue
        lat, lon = centroids[code]
        zona = gg = None
        fonte = "enea_dpr412"
        if code in enea_by_code:
            zona, gg = enea_by_code[code]
        else:
            cands = enea_by_name.get(normalize_name(info["nome"]), [])
            if len(cands) == 1:
                zona, gg = cands[0][1], cands[0][2]
            elif len(cands) > 1:
                # same name in several old provinces: pick the one whose old
                # province code matches the current code's first digits, else
                # give up to the nearest-neighbour pass
                same_prov = [c for c in cands if c[0][:3] == code[:3]]
                if len(same_prov) == 1:
                    zona, gg = same_prov[0][1], same_prov[0][2]
        if zona is None:
            unmatched.append(code)
            rows.append([info["nome"], code, info["sigla_provincia"],
                         info["regione"], lat, lon, None, None, None])
        else:
            rows.append([info["nome"], code, info["sigla_provincia"],
                         info["regione"], lat, lon, zona, gg, fonte])

    # nearest-neighbour fallback for the residue
    matched = [r for r in rows if r[6] is not None]
    for r in rows:
        if r[6] is not None:
            continue
        best, best_d = None, math.inf
        for m in matched:
            d = (m[4] - r[4]) ** 2 + ((m[5] - r[5]) * math.cos(math.radians(r[4]))) ** 2
            if d < best_d:
                best, best_d = m, d
        r[6] = best[6]
        r[7] = ""  # gradi_giorno of a neighbour would be misleading
        r[8] = "nearest_neighbor"
        print(f"NN fallback: {r[0]} ({r[2]}) → zona {r[6]} "
              f"(da {best[0]}, {math.sqrt(best_d) * 111:.1f} km)")

    print(f"\ncomuni: {len(rows)}  |  ENEA code/name match: {len(matched)}  "
          f"|  nearest-neighbour: {len(unmatched)}")

    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["nome", "codice_istat", "sigla_provincia", "regione",
                    "lat", "lon", "zona_climatica", "gradi_giorno", "zona_fonte"])
        w.writerows(rows)
    print(f"written {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
