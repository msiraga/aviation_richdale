# aviation_richdale

Open-source VFR flight-planning and 3D situational-awareness engine: live NOAA
weather ingestion, a true E6B wind-triangle navigation log, NASA SRTM terrain
awareness with a 1,000 ft safety-ceiling collision analyzer, ENAIRE AIP Spain
compliance structures, and an interactive glassmorphic cockpit UI built on
MapLibre GL + Three.js.

**This is engineering software, not a certified navigation aid.** Always plan
and fly with official AIP publications, NOTAMs, and avionics. See
[DISCLAIMER](#disclaimer).

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000   # 0.0.0.0 exposes the cockpit to your local network (iPad)
```

Open `http://<your-ip>:8000` on the cockpit device. The default viewport is the
Balearic Sea corridor between **Valencia (LEVC)** and **Ibiza (LEIB)**.

### HTTPS for cockpit GNSS (iOS Safari)

iOS only exposes `navigator.geolocation` to secure origins, so live GPS on an
iPhone needs TLS. Generate a local CA + server certificate covering your LAN
address:

```bash
python make_https_cert.py <your-lan-ip>     # writes under data/certs/ (git-ignored)
```

Then serve over TLS:

```bash
uvicorn main:app --host 0.0.0.0 --port 8443 \
  --ssl-certfile data/certs/server.crt.pem --ssl-keyfile data/certs/server.key.pem
```

On the iPhone: AirDrop `data/certs/ca.crt`, install the profile
(Settings → Profile Downloaded), then enable full trust for it
(Settings → General → About → Certificate Trust Settings). Open
`https://<your-lan-ip>:8443` — the padlock appears and the GPS layer can ask
for location permission. Remove the profile any time you're done; the CA signs
nothing but this cockpit.

## What it does

### Weather engine (`weather.py`)
- Pulls **live METAR and TAF** from the NOAA Aviation Weather Center data API
  (`aviationweather.gov/api/data/metar|taf`) for any pilot-supplied ICAOs.
- Parses the raw alphanumeric METAR/TAF token streams into immutable frozen
  dataclasses: surface wind vectors, gusts, visibility, sky coverage, ceilings,
  temperature/dewpoint, QNH altimeter.
- Computes the FAA flight category (VFR / MVFR / IFR / LIFR) from ceiling and
  visibility bands.
- Explicit crosswind vector analysis: given a destination runway magnetic
  heading and the live METAR wind, it resolves headwind / crosswind components
  after reconciling the true-vs-magnetic reference frames with WMM declination.
  A glowing warning flag fires when crosswind exceeds **15 kt**.
- Winds-aloft intersection: NOAA's textual FD winds-aloft product covers North
  America only (verified), so upper winds are intersected against the global
  GFS pressure-level forecast grid served openly by Open-Meteo, sampled at each
  leg midpoint *and* ETA, with per-sample provider attribution. Pilot-supplied
  forecast winds can override any leg.

### Navigation core (`navigation.py`)
- Haversine distances, initial/final true bearings, great-circle interpolation.
- The full E6B triangle of velocities: true course → wind correction angle →
  magnetic heading (with localized magnetic variation) → ground speed → ETE →
  fuel burn, iterated to converge ETA-dependent upper winds.
- A complete **NOAA World Magnetic Model** evaluator: the official WMM
  spherical-harmonic expansion over the coefficient file bundled in
  `magmodel/WMM2020.COF` (public-domain NOAA data, also auto-downloadable).
  Passes **100/100** of NOAA's published test vectors (`python validate_wmm.py`).

### Terrain awareness (`terrain.py`)
- Real DEM sampling along the route centerline plus lateral corridor lanes from
  NASA SRTM-derived sources:
  1. OpenTopography SRTM GL3 API (set `OT_API_KEY`), or
  2. any raw SRTM `.hgt` mirror (set `SRTM_BASE_URL`), or by default
  3. AWS Open Data terrarium terrain tiles (keyless; SRTM-derived landmass,
     decoded in-process with zero heavy dependencies).
- Builds a 1,000 ft **VFR safety ceiling** profile above terrain; any sector
  where cruise altitude fails to clear it turns bright red in the 3D mesh and
  raises the collision alert banner.

### ENAIRE AIP compliance (`enaire.py`)
- Async crawler/downloader against ENAIRE's live AIP Spain publication
  (`aip.enaire.es`, index-driven discovery), PDF text extraction via `pypdf`,
  and parsers for AD 2 structures: TWR/APP/GND/ATIS frequency assignments,
  transition altitude / transition level declarations (unit-aware, m vs ft),
  ARP coordinates (symbol-DMS and ICAO `DDDMMSSN DDDMMSSW` formats), and runway
  designators.
- VFR reporting points (e.g. Valencia's N/S/W points — Foios, Torrent,
  Calicanto…) are parsed from the aerodrome's **VAC chart** text layer with
  DMS→decimal conversion.
- Parsed snapshots persist to SQLite (`data/enaire.sqlite`) for offline cockpit
  use. Publication paths rotate across AIRAC releases; discovery endpoints are
  operator-configurable via `ENAIRE_AIP_BASE_URL` / `ENAIRE_AD2_URL_TEMPLATE`.
  When a structure cannot be parsed the API says so explicitly instead of
  guessing.

### Cockpit UI (`templates/index.html`)
- Glassmorphic floating dashboard (Tailwind CSS): blurred panels, neon telemetry,
  animated count-up counters, sliding sidebar on route submission.
- **MapLibre GL** multi-layer composition: dark CARTO base raster, openAIP
  airspace overlay (needs free `OPENAIP_API_KEY`), optional OpenFlightMaps chart
  layer (see config note below), great-circle route geometry, waypoint halos,
  GPS breadcrumb trail.
- **Three.js WebGL terrain panel**: draggable, rotatable, zoomable side-view mesh
  of the landscape along the flight path with the translucent safety-ceiling
  plane hovering at cruise+1,000 ft; left-drag rotates, wheel zooms, right-drag
  pans.
- Native `navigator.geolocation` streaming from iPad/iPhone GNSS with accuracy
  ring, breadcrumb trail, offline position buffering in `localStorage`, and
  automatic re-sync when connectivity returns.

## Configuration (environment variables)

| Variable | Purpose |
|---|---|
| `OPENAIP_API_KEY` | free key from openaip.net enabling the airspace tile overlay |
| `OPENFLIGHTMAPS_TILE_URL` | XYZ template for an OpenFlightMaps-compatible chart endpoint |
| `OT_API_KEY` | free OpenTopography key → native NASA SRTM GL3 sampling |
| `SRTM_BASE_URL` | base URL of any `.hgt` mirror |
| `MAGMODEL_COF_PATH` | explicit path to a WMM `.COF` file (bundled file used otherwise) |
| `ENAIRE_AIP_BASE_URL` / `ENAIRE_AD2_URL_TEMPLATE` | AISP publication endpoints |
| `HOST`, `PORT`, `LOG_LEVEL`, `RICHDALE_DATA_DIR` | server basics |

No keys are required to boot: the platform runs fully on keyless sources and
degrades loudly (never silently) where a provider is unconfigured.

**OpenFlightMaps note:** OFM retired its public XYZ tile servers (verified at
build time); charts are now distributed as regional downloads. Point
`OPENFLIGHTMAPS_TILE_URL` at their current endpoint or your own tiled cache to
light that layer up — the platform will not fake one.

## Data source credits & licenses

See [NOTICE.md](NOTICE.md). Summary: NOAA/AWC and NASA SRTM/WMM are U.S.
government public-domain sources; Open-Meteo forecasts are CC-BY 4.0;
basemap © OpenStreetMap contributors © CARTO; openAIP requires its own terms
acceptance via API key; ENAIRE AIP content remains under state/aeronautical
terms — parsed snapshots are reference extracts for flight preparation.

## Repository hygiene

- `.gitignore` excludes all runtime caches (`data/`), virtualenvs, sqlite files,
  env files, and OS artifacts.
- No secrets, tokens, absolute filesystem paths, or personal identifiers are
  embedded anywhere; the git identity for this repository is intentionally the
  GitHub noreply address.

## DISCLAIMER

This project is for flight-simulation, training, research, and educational use.
It is **not FAA/EASA-certified**, must not be used as a primary means of
navigation or operational decision-making, and carries no warranty of
accuracy or fitness for any purpose. Aviation data can be delayed, incomplete,
or wrong. Fly the official paperwork.
