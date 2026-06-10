# school/data — dataset

## comuni_italiani.csv

Tabella dei comuni italiani (7.896 al 1° gennaio 2025) con centroide e zona
climatica ufficiale DPR 412/93. Generata da `build_comuni_dataset.py`
(build-time only, richiede `pyshp` + `pyproj` + `shapely`, NON dipendenze
runtime del motore).

**Colonne**: `nome` (denominazione ISTAT in italiano), `codice_istat`
(PRO_COM_T, 6 cifre), `sigla_provincia`, `regione` (denominazione ISTAT),
`lat`, `lon` (centroide del poligono comunale, WGS84), `zona_climatica`
(A–F), `gradi_giorno`, `zona_fonte` (provenienza della zona).

**Fonti** (scaricate il 10 giugno 2026):

| Dato | Fonte | URL | Licenza |
|---|---|---|---|
| Codici, denominazioni, province, regioni | ISTAT — Elenco dei comuni italiani | https://www.istat.it/storage/codici-unita-amministrative/Elenco-comuni-italiani.csv | CC BY 3.0 IT |
| Centroidi | ISTAT — Confini delle unità amministrative a fini statistici, versione generalizzata, 1/1/2025 (UTM 32N → WGS84) | https://www.istat.it/storage/cartografia/confini_amministrativi/generalizzati/2025/Limiti01012025_g.zip | CC BY 3.0 IT |
| Zona climatica + gradi-giorno | ENEA SolarItaly — tabella DPR 412/93 Allegato A aggiornata (file datato 5/7/2002, ultima revisione tabellare pubblicata) | http://www.solaritaly.enea.it/clisun/Dati/FilesCSV/GraGioWb.txt | Contenuto di atto normativo (art. 5 L. 633/1941); ENEA citata come compilatore |

**Copertura zona climatica**: 7.768 comuni (98,4 %) per match diretto
codice/nome con la tabella ENEA (`zona_fonte=enea_dpr412`); 128 comuni
(1,6 %, tutte fusioni amministrative post-2002) ereditano la zona del
comune più vicino già classificato (`zona_fonte=nearest_neighbor`,
`gradi_giorno` lasciato vuoto). Distribuzione: E 4.181, D 1.595, C 981,
F 978, B 159, A 2.

## regioni_istat.geojson

Poligoni amministrativi regionali ISTAT semplificati (~2 km, ~131 KB),
usati da `school.calendar_it.region_from_coordinates`. Vedi commit
2026-05-17 (fix voce #12).
