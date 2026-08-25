# Richdale — VFR Flight Planning & Cockpit Awareness

**The complete field manual.** Every feature, what it does, where the data comes
from, how it fails honestly, and how to use it in the cockpit.

> **What this is.** A self-hosted, open-source VFR planning and in-flight
> awareness tool built for real flying in Spanish airspace (and anywhere
> Open-Meteo / NOAA / OurAirports cover). It runs on your own PC, serves your
> phone over your local Wi-Fi with HTTPS, uses **no API keys for core data**,
> and **never invents numbers**: when data is missing it says so.

---

## Table of contents

1. [Getting started](#1-getting-started)
2. [The layout at 10,000 ft](#2-the-layout-at-10000-ft)
3. [Planning a route](#3-planning-a-route)
4. [The Nav Log panel — four tabs](#4-the-nav-log-panel--four-tabs)
   - BRIEF · WEATHER · NOTAMS · LOG
5. [Map layers](#5-map-layers)
6. [Navigation aids](#6-navigation-aids)
   - VOR tuner & DIRECT mode · Reporting Point Copilot · Ground view
7. [Safety systems](#7-safety-systems)
   - Guardian · Glare Compass · Night check · Terrain collision banner ·
     Dead-reckon banner · SOS mode
8. [3D views](#8-3d-views)
   - Corridor (terrain) view · Earth view
9. [GPS on iPhone/iPad](#9-gps-on-iphoneipad)
10. [Where every number comes from](#10-where-every-number-comes-from)
11. [Honest limitations](#11-honest-limitations)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Getting started

```powershell
# from the project folder
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8443 `
  --ssl-certfile data\certs\server.crt.pem --ssl-keyfile data\certs\server.key.pem
```

* On your PC: `https://localhost:8443`
* On your phone (same Wi-Fi): `https://<your-PC-LAN-IP>:8443`
  — find the IP with `ipconfig` (look for *IPv4 Address*, e.g. `192.168.1.71`).
* The first time on each device you must trust the local certificate authority.
  See [§9](#9-gps-on-iphoneipad) — this is also required for GPS to work at all,
  because browsers only expose geolocation on trusted secure origins.
* Plain HTTP works too (`--port 8000`) but iOS will refuse geolocation on it.

The tiny build stamp beside the logo (`bYYMMDD.HHMM`) tells you which version
the server is running; after an update, hard-refresh and check it changed.

---

## 2. The layout at 10,000 ft

```
┌───────────────────────────────────────────────────────────────┐
│ PLAN │ MAP: DARK CHART VOR/DME RAIN RP 🌐 │ SKY ☀ │ FLIGHT: GPS FLY SOS │
├───────────────┬───────────────────────────────────────────────┤
│ Route composer│                LIVE MAP                       │
│ (sidebar)     │   wx legend (bottom-left)                     │
├───────────────┤   zoom controls (bottom-right)                │
│ NAV LOG       │                                               │
│ ┌───────────┐ │                                               │
│ │BRIEF WX NTM LOG│                                            │
│ └───────────┘ │                                               │
├───────────────┴───────────────────────────────────────────────┤
│ [FLY MODE hides everything above and shows the cockpit HUD]    │
└───────────────────────────────────────────────────────────────┘
```

* **PLAN** opens/closes the route composer sidebar.
* The header chips are grouped by purpose: map layers, sun info, flight state.
* Everything is touch-sized; the whole UI works on a phone held portrait.

---

## 3. Planning a route

Open **PLAN** and enter waypoints, one per line:

* **ICAO identifiers** (`LEVC`, `LEIB`) resolve via OurAirports and get named.
* **Navaid idents** (`VLC`, `MAD`) resolve against the loaded navaid dataset.
* **Free coordinates** (`38.87,-1.37`) work anywhere.

Then set cruise altitude, TAS and fuel burn, and press compute. You get:

* Great-circle legs with true course, magnetic course (WMM2020 declination),
  distance, wind-corrected ground speed, ETE and fuel per leg.
* A polyline on the map with a soft glow underlay.
* The **altitude advisor** (see below).

**Altitude advisor.** For every altitude you listed (up to six), the server
samples the GFS wind grid along your whole route in one batched call per level
(≈4 s total), computes the distance-weighted headwind and average ground
speed, and ranks the levels. It recommends one and shows the table — e.g.
"FLY 6,500 ft → GS 126 kt vs 109 kt at 2,500". This answers *"how much am I
losing by staying low?"* before you fly.

---

## 4. The Nav Log panel — four tabs

### BRIEF
Everything you check before engine start, top to bottom:

* **Departure/arrival crosswind card** — live METAR wind resolved onto every
  runway of both fields; runway ends are colour-coded (green calm → red >15 kt
  crosswind).
* **Glare Compass** ☀ — forecasts when the sun will sit low (2°–22° elevation)
  within ±22° of each leg's course, using NOAA solar position vs your ETA.
  Example: *"LEG 2 → LEIB: sun dead-ahead ~18:42Z"*. Put sunglasses on before
  those minutes instead of discovering them en route.
* **Night check** 🌙 — compares your destination ETA against actual sunset at
  that airport's location. Warns if you land after sunset so night-VFR
  requirements don't surprise you.
* **Stats tiles** — total distance, ETE, fuel.
* **Warnings** — terrain-collision banner appears here too when your planned
  cruise would punch through the 1,000 ft VFR safety ceiling over SRTM terrain.
* **FILE FPL / EXPORT GPX** buttons (FPL copies an ICAO-format plan to your
  clipboard; GPX downloads your track).

### WEATHER
* **Wind advisor** — the ranked altitude table from §3.
* **Fly-the-Future** ⏩ — a slider from NOW to +12 h. Departure and arrival
  flight categories step through their TAF periods at the selected hour, and a
  marker sweeps the cloud strip. This is *the weather at your ETA*, clearly
  labelled as stepwise-TAF logic rather than invented interpolation.
* **TAF validity windows** — colour-coded timeline bars per airport (green
  VFR, blue MVFR, red IFR), with TEMPO/PROB shown as thin sub-bars. Hover or
  long-press any bar for decoded details.
* **Cloud strip** — next 12 h of low/mid/high cloud percentage at the route
  midpoint, stacked columns.

### NOTAMS
Automatic NOTAM fetch for a bounding box around your full route, active-only
(expired ones counted, not shown), with a badge on the tab showing how many
are live right now.

### LOG
The classic leg table: TC, WCA, MH, wind, GS, ETE, fuel — computed with your
real wind grid sample per leg, not book averages.

---

## 5. Map layers

| Chip | Layer | Source | Notes |
|---|---|---|---|
| DARK/LIGHT/SAT | Base style + satellite imagery | Esri World Imagery | Tap MAP label cycles too |
| CHART | Official ENAIRE Insignia VFR chart | ENAIRE servais export proxy | ~2–3 s/tile first load; button shows CHART… while loading |
| VOR/DME | Navaids + airports coloured by live METAR category | OurAirports + NOAA | Airport dots green/blue/red = VFR/MVFR/IFR |
| RAIN | Precipitation radar animation frames | RainViewer (keyless) | Refreshes every 5 min |
| RP | VFR entry/exit reporting points | ENAIRE AD-2/VAC PDFs (parsed) | Violet pins with names |
| 🌐 | 3D Earth view | see §8 | |

Layer honesty: if a tile source errors past its supported zoom, MapLibre now
scales the last valid tiles instead of surfacing error images. If ENAIRE is
down, CHART simply stays blank — no fake chart is ever drawn.

---

## 6. Navigation aids

### 6.1 VOR tuner — without the confusion

Tap any VOR symbol (VOR/DME layer on) and choose:

* **DIRECT** *(recommended)* — GPS-style guidance with zero VOR theory needed:
  a big amber **"214°"** in the HUD is literally the heading to fly to the
  station right now, wind-corrected, with distance counting down. If you have
  ever been confused by OBS knobs, use this button and never think about
  radials again.
* **TUNE VOR** — classic behaviour for training/traditional cockpits: sets the
  OBS to your current radial, shows the CDI needle (10° full scale),
  TO/FROM flag, and still prints the big fly-heading with wind-drift
  correction applied ("TRACK R085 · FROM · FLY 092° · WCA +4°").

Both modes show ident, frequency, distance, and refresh every second. Wind
correction comes from the last live wind the app saw (nearest-field METAR via
Guardian/Ground), marked as WCA in the mode line so you know it moved.

### 6.2 Reporting Point Copilot

Spanish VFR transit lives on reporting points, which until now only existed as
text in VAC PDFs. With the RP chip on, all parsed entry/exit points around
your departure and arrival airports appear as violet pins. In FLY mode the HUD
shows the next point ahead within 20 NM of track:
`RP N-1 (FOIOS) · 6.4 NM · 11°R`.

Honest scope: points exist for airports whose AD-2/VAC has been fetched and
whose chart text parses cleanly; the endpoint reports `"No VFR reporting
points parsed"` otherwise rather than guessing.

### 6.3 Ground view (taxi phase)

Below ~80 kt within 6 NM of a paved-runway airport, a panel slides out with a
to-scale diagram of its runways: threshold identifiers, dashed **extended
centrelines with approach chevrons**, your aircraft arrow with live heading,
and a 1 NM scale bar. Beside it, every runway end lists **live headwind /
crosswind** from the current METAR (green ≤8 kt, amber ≤15, red above). This
answers "which end do I ask for?" while taxiing.

Taxiway letters are deliberately absent — they only exist in ENAIRE's printed
ADG diagram, and we won't invent geometry. The panel links to the AD-2.

Airport popups add three buttons: **METAR** (decoded latest), **AD-2 doc**
(full ENAIRE document), **VAC chart** (the VFR arrival/departure chart itself).

---

## 7. Safety systems

### 7.1 GUARDIAN — "where can I put this down right now?"

Arm it from FLY mode (GUARD button). Every 10 s it computes, from your live
GPS altitude, your aircraft's glide ratio and best-glide speed (editable,
remembered):

* A **wind-distorted glide envelope** drawn on the map — stretched downwind,
  compressed into wind — not a naive circle.
* Ranked **specific runways**: reachability margin in NM, crosswind and
  headwind per runway *end*, surface penalty. Tap one to fly the map there.

It refuses to flatter you: at 3,000 ft over Ibiza town it correctly reported
*"BELOW GLIDE of every listed runway"* (LEIB needed 4.2 NM; the glide had
3.1). Climb, or turn toward the widest arc of the envelope. Wind aloft is
proxied from the nearest METAR (+30° veer, ×0.7) — labelled as such in the
panel.

### 7.2 GLARE COMPASS
See §4 BRIEF. Nobody else warns you that the sun will be exactly in your eyes
on heading 264 at 18:42.

### 7.3 NIGHT CHECK
See §4 BRIEF. ETA vs sunset at the destination, with a note pointing at the
night-VFR requirements you need to verify.

### 7.4 Terrain collision banner
Route profile sampling (SRTM) flags sectors below the 1,000 ft VFR safety
ceiling and shows worst clearance margin before you commit.

### 7.5 Dead-reckon banner
If GPS permission breaks or fixes stop while a route is active, a persistent
banner shows your last known HDG and GS so you can dead-reckon consciously
instead of staring at a frozen position dot. It clears itself on the next fix.

### 7.6 SOS MODE
The red SOS button (header and FLY HUD): full-screen emergency card with

* giant **SQUAWK 7700**,
* live position / altitude / ground speed,
* COPY POSITION and SMS hand-off links,
* Guardian's best three runways recomputed on demand.

It is a decision aid, not a transmitter — nothing leaves your device except
what you send yourself.

---

## 8. 3D views

### Corridor (terrain) view
CORRIDOR moves the terrain canvas fullscreen: SRTM terrain strip along your
route, cyan plane at cruise altitude, translucent 1,000 ft safety ceiling,
rings at each waypoint, sun-accurate lighting for your departure time, and a
live progress marker while flying.

### Earth view 🌐
Full-screen globe (NASA Blue Marble day imagery blended into city-lights
night imagery along the **true terminator**, driven by the real subsolar
point). Your itinerary renders as an amber tube following genuine great
circles — spherical interpolation, not a projected straight line. Controls:
drag rotate, pinch/wheel zoom, ＋/－ buttons, LOCATE re-flies the framing to
your route, SPIN toggles idle drift (auto-drift disables whenever a route is
loaded so your route never drifts away), X-RAY fades the planet translucent
and draws the route through it. Live GPS dot included. Texture loads keyless
from jsDelivr; offline it degrades to a graticule instrument globe and says
so.

*Status: usable but still rough around the edges — flagged for another pass.*

---

## 9. GPS on iPhone/iPad

1. Serve over HTTPS (§1) — geolocation requires a secure origin.
2. Install the CA profile once: open `http://<PC-IP>:8000/ca` on the phone →
   allow profile → Settings → install → **Settings › General › About ›
   Certificate Trust Settings → enable full trust** for the Richdale CA.
   The CA key persists across restarts, so you redo this only once per device,
   even if your PC's LAN IP changes (server cert regenerates, CA stays).
3. Load the site **without any certificate warning**. If you ever tapped
   through a warning, iOS silently blocks geolocation — reinstall/refresh the
   profile first.
4. Settings › Privacy › Location Services → Safari Websites → Allow; and
   **Precise Location ON**.
5. Tap GPS OFF → it becomes ACQ… then LIVE outdoors in seconds. Errors now
   name the exact setting to fix, and a timeout auto-restarts the watch once.

While GPS is live you also get: breadcrumb trail, track logging (GPX export),
sun chip updates for present position, and the FLY suggestion toast once a
route exists.

---

## 10. Where every number comes from

| Data | Source | Cache | Failure behaviour |
|---|---|---|---|
| METAR/TAF | NOAA AWC JSON | none (always fresh) | loud error, stale UI values labelled |
| Winds aloft | Open-Meteo GFS pressure grids | 20 min/cell | level skipped, ranking continues |
| Cloud forecast | Open-Meteo hourly cloud_low/mid/high | — | hidden with message |
| Terrain | AWS terrarium tiles (SRTM) | disk | profile skipped, banner suppressed with note |
| Airports/navaids/runways | OurAirports CSVs (public domain) | 30 days | served stale |
| VFR chart tiles | ENAIRE ArcGIS export proxy | disk, PNG-verified | blank layer, honest toast |
| AD-2/VAC docs + reporting points | ENAIRE AIP PDFs | SQLite, 84 h TTL, stale-fallback | *"showing data cached N h ago"* note |
| NOTAMs | ENAIRE FeatureServer | 300 s bbox cache | error toast |
| Radar | RainViewer public API | 5 min frames | toast |
| Magnetic variation | WMM2020 (transliterated, NOAA test vectors pass 100 %) | n/a | n/a |
| Solar position | NOAA solar algorithm | n/a | n/a |

No API keys are required for any of the above. The optional AIRSPACE overlay
uses your personal openAIP key supplied at runtime via environment variable;
it is never stored in the repo.

Privacy posture: GPS positions stay in the browser session and optional local
track file; there is no telemetry, no accounts, no third-party analytics.

---

## 11. Honest limitations

* **Taxiway letters** are not drawn (only ENAIRE's printed ADG has them);
  ground view covers runways/thresholds/centreline extensions only.
* **Fly-the-Future** categories step per TAF change period; inside a period
  conditions are assumed constant (that is what TAFs mean).
* **Guardian wind** is a METAR-derived aloft proxy, labelled in the panel;
  real winds-aloft integration is future work.
* **Reporting points** appear only for airports whose VAC text parsed cleanly;
  the API states the gap explicitly.
* **Earth view** is functional but still being polished (postponed by choice).
* The app is decision support. It is not certified equipment; cross-check
  against official sources per your regulator's rules.

---

## 12. Troubleshooting

| Symptom | Cause & fix |
|---|---|
| Phone can't connect | PC IP changed (Wi-Fi switch). `ipconfig`, reload `https://NEWIP:8443`; server cert SANs regenerate automatically, CA unchanged. |
| Certificate warning on phone | Re-do the CA profile install + trust toggle (§9 step 2). Never tap through the warning. |
| GPS stuck ACQ indoors/on laptop | Most PCs have no GNSS. Phones need sky view + Precise Location (§9 step 4). |
| GPS worked, now ERR | See banner: it dead-reckons. Usually permission reset or cert warning appeared. |
| DNS errors on tiles (`ERR_NAME_NOT_RESOLVED`) | Transient resolver flake — flush DNS (`ipconfig /flushdns`) or set 1.1.1.1/8.8.8.8. |
| "zoom level not supported" tiles | Fixed by max-zoom caps; if seen, a layer source lacks its cap — report it. |
| CHART slow first time | ENAIRE exports tiles on demand (~2–3 s each); button shows CHART… until done. |
| Stale build stamp after update | Templates hot-reload but Python doesn't — restart uvicorn. |
| Firewall blocks phone | Rule `aviation_richdale 8443` exists for all profiles; check you launched with the same port. |

---

## Credits

ENAIRE (charts, AIP, NOTAMs) · OurAirports (David Megginson, public domain) ·
NOAA AWC (METAR/TAF) · Open-Meteo (GFS winds/clouds) · AWS Terrain Tiles
(SRTM) · RainViewer · Esri World Imagery · MapLibre GL · Three.js.
Full attributions in `NOTICE.md`. Licensed per repository root.
