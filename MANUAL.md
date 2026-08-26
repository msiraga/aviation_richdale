# Richdale — VFR Flight Planning & Cockpit Awareness

**The complete field manual.** Every feature documented as four answers:
**Where** it lives in the UI · **When** to use it · **How** to use it step by
step · and what's **behind it** (data source, physics, honest limits).

> **What this is.** A self-hosted, open-source VFR planning and in-flight
> awareness tool built for real flying in Spanish airspace (and anywhere
> Open-Meteo / NOAA / OurAirports cover). It runs on your own PC, serves your
> phone over local Wi-Fi with HTTPS, uses **no API keys for core data**, and
> **never invents numbers**: when data is missing it says so.
>
> It is decision support, not certified equipment. Cross-check against
> official sources per your regulator's rules.

---

## Table of contents

1. [Getting started](#1-getting-started)
2. [Screen layout](#2-screen-layout)
3. [Cheat sheet — everything at a glance](#3-cheat-sheet--everything-at-a-glance)
4. [Before flight — planning & briefing](#4-before-flight--planning--briefing)
5. [On the ground](#5-on-the-ground)
6. [En route — navigation](#6-en-route--navigation)
7. [Approach & landing](#7-approach--landing)
8. [Emergency systems](#8-emergency-systems)
9. [Map layers reference](#9-map-layers-reference)
10. [3D views](#10-3d-views)
11. [GPS on iPhone/iPad](#11-gps-on-iphoneipad)
12. [Where every number comes from](#12-where-every-number-comes-from)
13. [Honest limitations](#13-honest-limitations)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. Getting started

```powershell
# from the project folder
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8443 `
  --ssl-certfile data\certs\server.crt.pem --ssl-keyfile data\certs\server.key.pem
```

* PC: `https://localhost:8443` · Phone (same Wi-Fi): `https://<PC-LAN-IP>:8443`
  (find the IP with `ipconfig`, look for *IPv4 Address*).
* First visit per device requires trusting the local CA — see [§11](#11-gps-on-iphoneipad).
  GPS also requires this trusted secure origin.
* The build stamp beside the logo (`bYYMMDD.HHMM`) identifies the running
  version. The page is served `no-cache`, so a normal refresh always brings
  the current UI.

---

## 2. Screen layout

```
┌──────────────────────────────────────────────────────────────────┐
│ PLAN │ MAP: DARK CHART VOR/DME RAIN RP TRF 🌐 │ SKY ☀ │          │
│      │ FLIGHT: GPS·FLY·SOS   ?   LINK                            │ ← control bar
├──────────────┬───────────────────────────────────────────────────┤
│ NAV LOG      │                 LIVE MAP                          │
│ BRIEF/WX/    │   FINAL panel (right, when armed)                 │
│ NTM/LOG tabs │   Guardian panel (top-left, FLY)                  │
│              │   Ground view (bottom-left, slow near field)      │
│              │   wx legend ▮VFR ▮MVFR ▮IFR (bottom-centre)       │
└──────────────┴───────────────────────────────────────────────────┘
```

* **Control bar** wraps into rows on laptops (≥900 px) so every tool is on
  screen; on phones it is one swipe row — if ‹ › pips appear at its edges,
  there is more to scroll.
* **?** (far right) opens the **Tool Directory**: the in-app summary of every
  feature on this page.
* **FLY mode** (orange button) declutters: composer, chart chips, legend and
  side panels hide; the cockpit HUD appears at the bottom.

---

## 3. Cheat sheet — everything at a glance

| I want to… | Where | When |
|---|---|---|
| Plan a route | **PLAN** sidebar | Pre-flight |
| Waypoints: VOR · NDB · reporting points | PLAN — `VLC` · `NDB:AA` · `RP:N-1` / `LEVC:N-1` | Pre-flight |
| Adjust route by dragging on the map | **✎ Edit trip on map** | Pre-flight |
| Fly the route on today's winds | **▶ Simulate flight** → corridor view | Pre-flight |
| Rehearse engine failure decisions | Scenario dropdown → Simulate → decision card | Pre-flight |
| Pick best cruise altitude | WEATHER tab → wind advisor | Pre-flight |
| See weather at my ETA | WEATHER tab → Fly-the-Future slider | Pre-flight |
| Know when sun blinds each leg | BRIEF tab → ☀ Glare Compass | Pre-flight |
| Check night-legal arrival | BRIEF tab → 🌙 Night check | Pre-flight |
| Terrain clash warning | BRIEF tab banner (auto) | Pre-flight |
| Active NOTAMs on route | NOTAMS tab | Pre-flight |
| Classic leg table (TC/WCA/MH…) | LOG tab | Pre-flight |
| Official VFR chart | MAP → CHART | Any time |
| Radar rain | MAP → RAIN | Any time |
| VFR entry/exit points | MAP → RP | CTR/TMA transit |
| See other traffic | MAP → TRF | Awareness only (ADS-B limits!) |
| Airspace overlay | MAP chip (needs your openAIP key env) | Optional |
| 3D globe of itinerary | MAP → 🌐 | Briefing / debrief |
| Steer to a VOR, no theory | Tap VOR → **DIRECT** | En route |
| Classic CDI to a VOR | Tap VOR → **TUNE** | Training / traditional |
| Next reporting point callout | automatic in FLY HUD (RP armed) | En route |
| Taxi diagram + runway winds | automatic when slow near a field | Taxi |
| Guided visual approach | popup / Ground view / **FINAL** button | Final |
| Runway silhouette on camera | FINAL panel → CAMERA VIEW | Final (experimental) |
| Engine-out options live | FLY → **GUARD** | Any airborne moment |
| Emergency card | **SOS** | Emergency |
| Dead-reckon after GPS loss | automatic banner | GPS failure |
| Terrain profile in 3D | Terrain panel → **CORRIDOR** | Pre-flight |

---

## 4. Before flight — planning & briefing

### 4.1 Route composer
* **Where:** **PLAN** chip, left sidebar.
* **When:** before every flight.
* **How:** build the route from **any mix** of these entry forms — departure
  and arrival fields accept the same grammar as en-route waypoints:

| You type | It resolves as |
|---|---|
| `LEVC` | airport ICAO (local OurAirports lookup — works offline) |
| `VLC` | navaid ident if no station matches (VOR/DME/NDB) |
| `VOR:VLC` | forces the VOR even when an ident collides |
| `NDB:AA` | forces the NDB type explicitly |
| `RP:N-1` | ENAIRE reporting point already indexed this session |
| `LEVC:N-1` | loads that field's VAC chart on demand and uses its point |
| `38.87,-1.37` | raw coordinates |

  Set cruise altitude, TAS, fuel burn → compute. Legs appear on map and in
  LOG; FPL/GPX export lives at the bottom of BRIEF.
* **Behind it:** great-circle legs and courses with WMM2020 magnetic variation
  (validated against NOAA's official 100-vector test set). Navaid idents are
  matched exactly against the worldwide OurAirports dataset with Spanish
  stations preferred on collisions — the server logs disclose how many
  stations share a given ident.

### 4.2 Altitude advisor (winds-aloft ranking)
* **Where:** WEATHER tab, first card.
* **When:** choosing your cruise level.
* **How:** nothing to press — after computing a route the advisor ranks your
  listed altitudes by distance-weighted headwind and average ground speed:
  *"fly 6,500 ft → 126 kt vs 109 kt low"*.
* **Behind it:** one batched Open-Meteo GFS call per altitude (~4 s total);
  a level that fails to sample drops out of the ranking rather than faking.

### 4.3 BRIEF tab — the pre-flight card deck
* **Where:** results panel → BRIEF tab. **When:** during planning, read top to bottom.
* **Crosswind card** — every runway end of departure & arrival vs live METAR,
  colour-coded (green ≤8 kt, amber ≤15, red >15).
* **☀ Glare Compass** — samples solar azimuth along each leg's course and ETA;
  warns e.g. *"LEG 2 → LEIB: sun dead-ahead ~18:42Z"* when the sun sits low
  (2–22° high) within ±22° of heading. Sunglasses on *before* those minutes.
* **🌙 Night check** — ETA vs actual sunset at the destination's coordinates;
  warns if you arrive after sunset so night-VFR rules don't surprise you.
* **Terrain collision banner** — appears automatically if sampled SRTM terrain
  breaches the 1,000 ft VFR safety ceiling under your planned cruise, showing
  how many sectors breach and the worst margin.

### 4.4 WEATHER tab
* **Airport WX brief** *(top card)* — a chip per airport on your route;
  tapping one shows its **real raw METAR** (latest plus the last few for
  trend) with a decoded one-liner beneath (wind/visibility/temp/QNH/wx), and
  the **full raw TAF** in an expandable block. Straight from NOAA AWC's raw
  feeds — no keys, no reformatting lies.
* **SIGMET · Spain (live)** — active SIGMETs for Madrid/Barcelona/Canarias
  FIRs as issued by AEMET and relayed through NOAA AWC, refreshed on demand.
  An empty list genuinely means *no active significant weather over Spain*.
* **Wind advisor** — see 4.2.
* **⏩ Fly-the-Future** — slider NOW→+12 h: departure/arrival categories step
  through their TAF periods at that hour and a marker sweeps the cloud strip.
  Stepwise by design — that is what TAF periods mean.
* **TAF windows** — colour-coded validity bars (green/blue/red =
  VFR/MVFR/IFR), TEMPO/PROB as thin sub-bars; long-press for decoded values.
* **Cloud strip** — 12 h of low/mid/high cloud columns at the route midpoint.

> **AEMET SIGWX charts** live behind their OpenData API. Put your free key
> in `.env.txt` as `AEMET_API_KEY=…` (the file is git-ignored; values are
> never logged). The **SIGWX · AEMET** card then offers España D0/D+1,
> Baleares, Canarias and the surface-analysis chart. If their gateway serves
> empty responses — which it demonstrably does on some networks even with a
> valid key — press **DIAGNOSE** on the card: it prints per-endpoint HTTP
> status and byte counts straight from `/api/aemet/diag`, so you see exactly
> where it breaks instead of staring at a blank panel.

### 4.5 NOTAMS tab
Automatic bounding-box fetch around the whole route, active items only
(expired counted, hidden). The tab badge shows the live count.

### 4.6 LOG tab
Classic leg table: TC, WCA, MH, wind used, GS, ETE, fuel — computed per leg
from the real wind grid sample, not book averages.

### 4.7 FILE FPL / EXPORT GPX
Bottom of BRIEF: copies an ICAO-format plan to the clipboard, and downloads
your recorded track as GPX after a flight.

### 4.8 Editing the trip on the map
* **Where:** **✎ Edit trip on map** button under the composer.
* **When:** adjusting an existing route visually instead of retyping entries.
* **How:** compute once first. Toggle the button:
  * numbered cyan handles appear at every waypoint (amber = departure/arrival);
  * **drag** any handle → that point moves, the plan recomputes automatically;
  * **click a mid-route handle** → removes it (departure/arrival stay fixed);
  * **click anywhere within 30 NM of the route** → inserts a new waypoint at
    that spot, spliced into the nearest leg.
  Every change reuses the full compute pipeline (winds, terrain, NOTAMs), so
  the nav log, corridor view and banners stay truthful. Toggle off to return
  the map to normal interaction.

### 4.9 SIMULATE FLIGHT — ghost flyer (Tier A)
* **Where:** **▶ Simulate flight (today's winds)** button under the composer.
* **When:** after computing a route — before you commit to a departure time,
  or to sanity-check an altitude choice against the actual sky.
* **How:** one press. The aircraft dot flies your route in the CORRIDOR view
  at compressed speed while a readout chip tracks `T+ · GS · FUEL · DRIFT`
  versus still-air. At the end: total time, average groundspeed and drift.
* **Behind it & honesty:** point-mass kinematics through GFS winds sampled
  every ~20–25 NM along the route (wind triangle solved each minute of sim
  time). It answers *"when / how much fuel / how much drift if I fly this
  plan through this sky"* — it does NOT model aircraft dynamics, climb
  profiles or turbulence, and says so in every API response.

### 4.10 Scenario trainer — decision injects during the ghost flight (Tier B)
* **Where:** the **Scenario:** dropdown next to ▶ Simulate flight.
* **When:** practising the failure loop — recognise, decide, commit — on your
  own route before you ever need it for real.
* **How:** pick a scenario, press Simulate:
  * **Engine failure mid-route / late** — at half or three-quarters of the
    way the replay freezes with a full decision card: your simulated position,
    the aloft wind proxy, and **live Guardian output** computed for that exact
    point and altitude (top runways with distance, glide margin and crosswind).
  * You answer with one of three calls: *commit to best option*, *continue to
    destination*, or *own call*. Each is timestamped and logged.
  * The replay resumes; at the end a **debrief** appends verdicts — ✓ where a
    reachable runway existed and you took it, ✗ where you continued past one,
    · for own-calls. Sometimes Guardian's honest answer is *"nothing within
    glide-plus-margin"* (mid-Castellón at 6,500 ft genuinely is); then fast
    commitment itself is the graded skill.
* **Behind it & honesty:** the judge is the same deterministic Guardian physics
  that flies with you — no separate scoring model, no LLM in the loop. Inject
  timing is fixed per scenario so runs are repeatable.

---

## 5. On the ground

### 5.1 Ground view (automatic taxi aid)
* **Where:** bottom-left card titled `Ground · ICAO · N NM`.
* **When:** automatically while GPS is live, you are slower than 80 kt and
  within 6 NM of a paved-runway airport. Hides itself when you speed up or leave.
* **How:** read the canvas — runways to scale, cyan threshold labels, dashed
  yellow **approach gates** (extended centrelines with chevrons), your orange
  aircraft arrow, 1 NM scale bar. Each end lists live **HW/XW** against the
  current METAR. Buttons: **SET FINAL** arms approach mode (§7.1); **HIDE**
  dismisses.
* **Behind it & honesty:** OurAirports geometry + NOAA METAR. **Taxiway
  letters do NOT exist here** — they are only printed in ENAIRE's ADG chart;
  the panel says so and links the AD-2 instead of inventing geometry.

### 5.2 Airport popups
Tap any airport dot (VOR/DME layer on): **METAR** decoded latest · **AD-2
doc** full ENAIRE document · **VAC chart** the VFR chart proper · **SET
FINAL** best-wind-end approach guidance.

---

## 6. En route — navigation

### 6.1 VOR navigation — two modes, zero confusion
* **Where:** tap any VOR symbol (VOR/DME layer on).
* **When:** navigating by radials — or simply getting somewhere.
* **DIRECT** *(recommended)* — a big amber heading in the HUD that is
  literally the heading to fly to the station right now, wind-corrected,
  distance counting down. If OBS knobs have ever confused you, this is yours.
* **TUNE VOR** — classic CDI semantics: OBS preset to your current radial,
  needle = 10° full scale, TO/FROM flag… **and still prints the plain-language
  answer** (`TRACK R085 · FROM · FLY 092° · WCA +4°`) so both worlds agree.
* **Behind it:** refreshes every second from live GPS; drift correction uses
  the nearest-field METAR the app last saw, shown as WCA.

### 6.2 Reporting Point Copilot (RP)
* **Where:** MAP zone chip **RP** + violet pins; callout line inside the FLY HUD.
* **When:** transiting Spanish CTR/TMA via published VFR points.
* **How:** arm once; points parsed from ENAIRE AD-2/VAC documents appear near
  your route airports. In FLY mode the HUD calls the next point ahead within
  20 NM of track: `RP N-1 (FOIOS) · 6.4 NM · 11°R`.
* **Honesty:** points exist only for fields whose VAC text parsed cleanly —
  the API reports gaps explicitly instead of guessing.

### 6.3 Traffic advisory (TRF)
* **Where:** MAP zone chip **TRF**. **When:** awareness in busy airspace.
* **How:** polls OpenSky ADS-B every 30 s within 10 NM; rose dots carry
  callsign/altitude; vibration + red toast if any aircraft is predicted within
  ¾ NM inside 60 s at similar altitude to yours.
* **Read this twice:** sees ONLY transponding aircraft within ground-station
  coverage. Low-level VFR traffic and gliders are frequently invisible. A
  second pair of eyes — never a replacement for look-out. Feed outages show no
  dots rather than false comfort.

### 6.4 FLY mode & the HUD
* **Where:** orange **FLY** chip in FLIGHT zone. **When:** actually flying.
* **How:** hides planning chrome; shows the bottom HUD: next waypoint, BRG,
  distance, ETE, live GS, XTK with a ±1 NM CDI bar. Second row adds a mini
  six-pack: attitude + heading gauges (**ENABLE SENSORS** grants iPhone motion
  access once; without it HDG falls back to GPS course), GPS ALT and V/S.
  **EXIT** returns the full UI. A translucent corridor paints along your track
  while flying; an aircraft icon replaces the breadcrumb dot when moving.

---

## 7. Approach & landing

### 7.1 FINAL mode — virtual PAPI & guided visual approach
* **Where:** right-hand panel. Arm it three ways: airport popup **SET FINAL**
  (best-wind end chosen for you) · Ground view **SET FINAL** · FLY HUD
  **FINAL** (nearest field, best end).
* **When:** from ~8 NM out to touchdown, on any visual approach.

What you get, refreshed twice a second:

* **Virtual PAPI** — four lights behaving exactly like the physical bar
  (white above path, red below, ~one light per 0.12°) keyed to the published
  angle, beside your live glidepath angle and HAT.
* **Glidepath strip** — ideal slope, threshold at the right edge, your dot
  sliding down it.
* **Steering line** — `TRACK 242° · FLY 236° · XW 8L` (drift-corrected; same
  language as VOR DIRECT and Guardian).
* **Voice callouts** — tap ENABLE VOICE CALLOUTS once (this gesture unlocks
  iOS speech): *five/three/two/one miles*, *one thousand*, *five hundred*,
  *two hundred*, *short final, check runway*. Low calls **auto-suppress while
  GPS vertical accuracy is worse than ±12 m** — the panel shows live accuracy,
  so silence is informative, not mysterious.
* **Landing performance card** — enter POH landing distance once (stored on
  device): density altitude from METAR temperature (+10 % per 1,000 ft DA),
  head/tailwind adjustment, your safety multiplier, compared to available
  length with ✓ / ✗ TOO SHORT. Labelled rules-of-thumb, not POH tables.
* Dashed green centreline on the map from 8 NM out.

**What it is NOT:** an ILS. Geometry from GPS against published data with no
integrity channel — PAPI semantics you already know how to read, nothing more.

### 7.2 Camera view — experimental
* **Where:** CAMERA VIEW button inside the FINAL panel.
* **When:** visually searching for the runway environment by day.
* **How:** rear camera fullscreen with the computed runway silhouette, dashed
  centreline and aim ring overlaid; EXIT stops the camera stream.
* **Honesty:** stamped EXPERIMENTAL · NON-CONFORMAL. The overlay assumes
  roughly level flight aligned with the runway — it helps you *find* the
  runway; it is not registered synthetic vision.

---

## 8. Emergency systems

### 8.1 GUARDIAN — "where can I put this down right now?"
* **Where:** FLY HUD → **GUARD**; panel opens top-left.
* **When:** any moment you want options ready — especially after any rough
  engine sound.
* **How:** arms itself for the whole flight; recomputes every 10 s from live
  altitude, your glide ratio / best-glide speed (editable inputs, saved on
  device), and the nearest field's METAR sheared +30° × 0.7 as an aloft proxy.
  You get a dashed **wind-distorted reachability envelope** on the map plus
  ranked runways: margin in NM, crosswind/headwind per end, surface penalty;
  tapping one flies the map there.
* **Honesty:** it refuses to flatter — at 3,000 ft over Ibiza town it reported
  *"BELOW GLIDE of every listed runway"* because LEIB needed 4.2 NM of glide
  and only 3.1 existed. Turn toward the widest arc and climb. The wind proxy
  is labelled in the panel itself.

### 8.2 SOS mode
* **Where:** red **SOS** chip (header + FLY HUD). **When:** emergency or
  genuine uncertainty.
* **How:** fullscreen card: giant SQUAWK 7700, live position/ALT/GS, COPY
  POSITION, SMS hand-off link, Guardian's best three runways recomputed on
  demand. Nothing leaves the device except what you send yourself.

### 8.3 Dead-reckon banner
* **Where:** automatic, top-centre. **When:** GPS permission lost or fixes
  stop while a route is active.
* **How:** shows last known HDG/GS for conscious dead-reckoning; clears on the
  next fix. Error messages name the exact iOS setting to fix, and one timeout
  restarts the watch automatically.

---

## 9. Map layers reference

| Chip | Layer | When to use | Notes |
|---|---|---|---|
| DARK / LIGHT / SAT | Base styles + Esri satellite | Always on | Tap the **MAP** label to cycle too |
| CHART | ENAIRE Insignia VFR chart | Navigation truth | ~2–3 s/tile first load; label reads CHART… until ready |
| VOR/DME | Navaids + METAR-coloured airports | Navigation | Legend colours bottom-centre |
| RAIN | RainViewer radar | Weather avoidance | 5-min refresh |
| RP | VFR reporting points | CTR/TMA transit | §6.2 |
| TRF | OpenSky ADS-B traffic | Awareness only | §6.3 limits apply |
| AIRSPACE | openAIP overlay (optional key) | Controlled-airspace picture | Gated behind your own env var |
| 🌐 | Earth view | Briefing/debrief | §10.2 |

Legend: **green VFR · blue MVFR · red IFR** (bottom-centre pill).

---

## 10. 3D views

### 10.1 CORRIDOR — terrain profile theatre
* **Where:** Terrain panel header → CORRIDOR.
* **When:** pre-flight terrain review; situational awareness en route.
* **How:** fullscreen SRTM terrain strip along the route with the cyan
  aircraft at cruise, translucent 1,000 ft safety ceiling, rings at waypoints,
  sun-accurate lighting for your departure time, live progress marker in
  flight. Drag orbit / right-drag pan / wheel zoom. EXIT restores the panel.

### 10.2 EARTH — great-circle globe
* **Where:** 🌐 chip (header) or EARTH button (Terrain panel).
* **When:** briefing the shape of the journey; admiring the planet.
* **How:** drag rotate · pinch/wheel/＋− zoom · SPIN toggles idle drift (drift
  disables whenever a route exists so your itinerary holds still) · **LOCATE**
  re-flies framing to the route from anywhere · **X-RAY** fades the planet
  translucent and draws the amber great-circle tube, pins and GPS dot *through*
  it. Day/night terminator is real (subsolar-point shader with city lights).
* **Behind it:** textures load keyless from jsDelivr; offline degrades to a
  graticule globe with a notice. Status: usable, still being polished by choice.

---

## 11. GPS on iPhone/iPad

1. Serve HTTPS (§1). Geolocation needs a trusted secure origin.
2. Install the CA once: on the phone open `http://<PC-IP>:8000/ca` → allow →
   Settings install → **General › About › Certificate Trust Settings → full
   trust** for the Richdale CA. The CA key persists, so IP changes never
   require redoing this.
3. Load the page with **no certificate warning** — tapping through one makes
   iOS silently block geolocation.
4. Settings › Privacy › Location Services → Safari Websites → Allow, and
   **Precise Location ON**.
5. Tap GPS OFF → ACQ… → LIVE outdoors within seconds. Errors name the exact
   setting to change; timeouts restart the watch once automatically.

While live: breadcrumb trail, GPX track recording, accuracy ring, and real
altitude feeding Night check, Guardian and FINAL callouts.

---

## 12. Where every number comes from

| Data | Source | Cache | Failure behaviour |
|---|---|---|---|
| METAR/TAF/history | NOAA AWC JSON | none (fresh) | loud error; stale values labelled |
| Raw METAR/TAF briefing | NOAA AWC `format=raw` feeds | none (fresh) | per-airport visible error |
| Spain SIGMETs (AEMET-issued) | NOAA AWC SIGMET feed, FIR-filtered | none (fresh) | visible "feed unavailable" note |
| AEMET SIGWX charts / analysis | AEMET OpenData API (`AEMET_API_KEY` in .env.txt) | disk, per-image | DIAGNOSE button shows per-endpoint HTTP+bytes |
| Winds aloft | Open-Meteo GFS pressure grids | 20 min/cell | level skipped, ranking continues |
| Cloud forecast | Open-Meteo hourly low/mid/high | — | hidden with message |
| Terrain | AWS terrarium tiles (SRTM) | disk | profile skipped with note |
| Airports/navaids/runways | OurAirports CSVs (public domain) | 30 days | served stale |
| VFR chart tiles | ENAIRE ArcGIS export proxy | disk, PNG-verified | blank layer + honest toast |
| AD-2/VAC docs, reporting points | ENAIRE AIP PDFs | SQLite 84 h, stale-fallback | *“showing data cached N h ago”* note |
| NOTAMs | ENAIRE FeatureServer | 300 s bbox | error toast |
| Radar | RainViewer public API | 5-min frames | toast |
| Traffic | OpenSky public ADS-B | none (30 s poll) | silent — no dots, no false comfort |
| Magnetic variation | WMM2020 transliteration | n/a | n/a (100 % of NOAA test vectors) |
| Solar position | NOAA algorithm | n/a | n/a |

No API keys required for core data. The optional AIRSPACE overlay uses your
own openAIP key via environment variable, never stored in the repo.
Privacy: GPS positions stay in your browser session/local track — no
telemetry, no accounts.

---

## 13. Honest limitations

* **Taxiway letters** absent (only ENAIRE's printed ADG has them) — Ground
  view covers runways, thresholds, centrelines, gates.
* **Fly-the-Future** steps TAF periods; conditions assumed constant within one.
* **Guardian wind** is a METAR-derived aloft proxy, labelled in-panel.
* **Reporting points** only where VAC parsing succeeded; API states gaps.
* **FINAL is visual guidance, not precision nav** — no integrity channel, and
  GPS vertical error dwarfs decision heights near touchdown (hence suppressed
  low callouts).
* **Camera view** non-conformal; not certified synthetic vision.
* **Traffic** sees only ADS-B participants within coverage.
* Windshear alerting and radar-altimeter flare cueing are physically out of
  reach for a browser device (no air data / radio altimeter) — not pretended.
* **Earth view** functional but still being polished (postponed by choice).
* Decision support only — not certified equipment.

---

## 14. Troubleshooting

| Symptom | Cause & fix |
|---|---|
| Phone can't connect | PC IP changed (Wi-Fi switch). `ipconfig`, load `https://NEWIP:8443`; cert SANs regenerate, CA unchanged. |
| Certificate warning | Redo CA install + trust toggle (§11). Never tap through. |
| Menu/tools look old or missing | Page sends `Cache-Control: no-cache`; one normal reload fixes legacy caches. Verify stamp near logo ≥ your last update. Not stuck in FLY mode? (**EXIT** restores chrome.) |
| Header chips overflow on phone | Swipe; ‹ › pips mark hidden edges. Laptops ≥900 px wrap instead. |
| GPS stuck ACQ | Most PCs have no GNSS. Phones need sky view + Precise Location (§11.4). |
| GPS ERR mid-flight | DR banner shows last HDG/GS; fix permission/cert per the message shown. |
| DNS errors on tiles | Resolver flake: `ipconfig /flushdns` or switch DNS to 1.1.1.1 / 8.8.8.8. |
| CHART slow first time | ENAIRE exports on demand (~2–3 s/tile); wait for the label flip back from CHART…. |
| Stale build stamp after update | Templates hot-reload but Python doesn't — restart uvicorn. |
| Voice callouts silent | Arm once via ENABLE VOICE CALLOUTS (iOS gesture rule); low calls suppress when vertical accuracy >±12 m — shown live in-panel. |
| Traffic shows nothing | Coverage/rate-limit reality; advisory layer only. |

---

## Credits

ENAIRE (charts, AIP, NOTAMs) · OurAirports (David Megginson, public domain) ·
NOAA AWC (METAR/TAF) · Open-Meteo (GFS winds/clouds) · AWS Terrain Tiles
(SRTM) · RainViewer · Esri World Imagery · OpenSky Network (ADS-B) ·
MapLibre GL · Three.js. Full attributions in `NOTICE.md`.
Licensed per repository root.
