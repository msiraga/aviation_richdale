# NOTICE — data sources, attribution, and licensing

This repository aggregates publicly available aeronautical and geospatial
data. Each source keeps its own terms; the table below is the operator's
attribution map.

| Source | What it provides here | Terms / attribution required |
|---|---|---|
| NOAA Aviation Weather Center (aviationweather.gov) | METAR, TAF raw observations via the public data API | U.S. government work, public domain. Attribution appreciated: "NOAA Aviation Weather Center". |
| NOAA NCEI World Magnetic Model (`magmodel/WMM2020.COF`, test values) | magnetic declination engine coefficients | Public domain (NASA/NGA/NOAA). Cite WMM2020 report when publishing derived products. |
| NASA Shuttle Radar Topography Mission (SRTM) | terrain elevation (via OpenTopography API, .hgt mirrors, or AWS Open Data terrarium tiles whose landmass layer is SRTM-derived) | Public domain (NASA). Courtesy credit "NASA SRTM" requested by OpenTopography for derived maps. |
| AWS Open Data – Mapzen/AWS Terrain Tiles | keyless terrarium-encoded elevation tiles fallback | Open Data Commons terms of the dataset; attribution "Terrain tiles © Mapzen/AWS" recommended. |
| Open-Meteo (api.open-meteo.com) | gridded pressure-level winds-aloft forecasts | Free tier requires attribution: "Weather data by Open-Meteo.com (CC BY 4.0)". |
| OpenStreetMap + CARTO | dark basemap raster tiles | © OpenStreetMap contributors (ODbL); tiles © CARTO. Must remain visible in map attribution. |
| openAIP (openaip.net) | airspace boundary overlay raster tiles | Requires your own free API key and acceptance of openAIP license terms; attribution "&copy; openAIP" kept in-layer. |
| OpenFlightMaps | optional VFR chart layer if an operator provisions a tile endpoint | Their content terms apply; the platform ships no OFM imagery itself. |
| ENAIRE / AIS España (enaire.es, aip.enaire.es) | AD 2 aerodrome documents and VAC charts parsed on demand (frequencies, transitions, VFR reporting points) | AIP content is protected aeronautical information; extracts stored locally are for flight preparation reference only, not republication. |
| ENAIRE Insignia VFR (servais.enaire.es ArcGIS services) | official Spanish VFR airspace chart layer via the local tile proxy (`/api/charts/vfr/tile/...`) | Same ENAIRE aeronautical-information terms; rendered tiles stay on your device for personal reference. |
| OurAirports Data (davidmegginson.github.io/ourairports-data) | worldwide airport and navaid (VOR/DME/NDB) reference points shown on the map | Public domain; downloaded on demand and cached under `data/cache/ourairports/`. |
| Esri World Imagery | optional satellite basemap tiles | Attribution "Imagery © Esri, Maxar, Earthstar Geographics" kept in map attribution. |
| Three.js, MapLibre GL JS, Tailwind CSS, FastAPI ecosystem | frontend/backend libraries under their OSS licenses (MIT/BSD-family) | License headers live in each distribution package. |

## Privacy posture

- The GPS ingestion endpoint (`POST /api/gps/ingest`) is **stateless**: it
  validates and counts positions, then forgets them. Cockpit position history
  lives only in the browser (`localStorage`) until you clear it.
- No telemetry, analytics, crash reporting, or third-party beacons exist in
  this codebase.
- The bundled WMM coefficient file and NOAA test vectors are verbatim U.S.
  federal datasets with no added content.
