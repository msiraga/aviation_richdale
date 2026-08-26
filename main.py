"""VFR flight-planning platform service.

FastAPI application wiring the live weather engine, the E6B navigation core,
the terrain awareness pipeline, and the ENAIRE AIP compliance layer behind a
single HTTP surface consumed by the glassmorphic cockpit UI.

Error gateway contract: every failure leaves this API as a structured JSON
envelope ({ok: false, error: {...}}) with a client-safe message; internals,
stack traces, and filesystem paths never cross the boundary.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

import navigation
from airdata import (
    AirDataError,
    AirReferenceService,
    EnaireChartTileProxy,
    GroundReferenceService,
    initial_bearing as _ground_initial_bearing,
    wind_components as _ground_wind_components,
)
from enaire import EnaireAipRepository, EnaireAd2Service, EnaireError, EnaireNotamService
from navigation import (
    LatLon,
    apply_magnetic_variation,
    great_circle_points,
    haversine_distance_nm,
    initial_true_bearing_deg,
    load_world_magnetic_model,
    normalize_bearing,
    solve_wind_triangle,
)
from terrain import SAFETY_CEILING_FT, TerrainEngine, TerrainServiceError
from weather import (
    CROSSWIND_LIMIT_KT,
    AviationWeatherEngine,
    NoUsableObservation,
    UnknownStation,
    WeatherServiceError,
    WindsAloftEngine,
    fetch_cloud_profile,
    sample_winds_batch,
    serialize_taf_timeline,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("richdale.main")

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file() -> None:
    """Tiny .env loader (.env.txt or .env): KEY=VALUE lines, '#' comments.

    Real environment variables always win over file values. Values are never
    logged anywhere.
    """
    for name in (".env", ".env.txt"):
        p = BASE_DIR / name
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    os.environ.setdefault(k, v)
            log.info("loaded env overrides from %s", name)
        except OSError as exc:
            log.warning("could not read %s: %s", name, exc)


_load_env_file()
DATA_DIR = Path(os.environ.get("RICHDALE_DATA_DIR", BASE_DIR / "data"))
TEMPLATES_DIR = BASE_DIR / "templates"

DEFAULT_MAP_BBOX_SW = [38.62, -0.62]
DEFAULT_MAP_BBOX_NE = [39.78, 1.62]
DEFAULT_MAP_CENTER = [
    (DEFAULT_MAP_BBOX_SW[0] + DEFAULT_MAP_BBOX_NE[0]) / 2.0,
    (DEFAULT_MAP_BBOX_SW[1] + DEFAULT_MAP_BBOX_NE[1]) / 2.0,
]
DEFAULT_MAP_ZOOM = 8

MANUAL_WIND_MATCH_RADIUS_NM = 60.0
LEG_GEOMETRY_SAMPLES = 48
TERRARIUM_PROBE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/9/258/226.png"

COORD_ENTRY_RE = re.compile(r"^(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)$")


class ApiError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, detail: Any | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code


class BadRequest(ApiError):
    status_code = 400
    code = "bad_request"


class UpstreamUnavailable(ApiError):
    status_code = 502
    code = "upstream_unavailable"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetarRequest(StrictModel):
    icaos: list[str] = Field(min_length=1, max_length=12)


class TafRequest(StrictModel):
    icaos: list[str] = Field(min_length=1, max_length=6)


class CrosswindRequest(StrictModel):
    icao: str = Field(pattern=r"^[A-Za-z]{4}$")
    runway_heading_deg: float = Field(ge=0, lt=360)
    limit_kt: float | None = Field(default=None, ge=1, le=50)


class ManualWindEntry(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    wind_from_deg: float = Field(ge=0, lt=360)
    wind_speed_kt: float = Field(ge=0, le=250)


class NavLogRequest(StrictModel):
    departure: str
    arrival: str
    waypoints: list[str] = Field(default_factory=list, max_length=8)
    cruise_altitude_ft: int = Field(ge=500, le=18000)
    tas_kt: float = Field(gt=20, le=400)
    fuel_rate: float = Field(gt=0.1, le=200)
    fuel_unit: Literal["GPH", "LPH"] = "GPH"
    destination_runway_heading_deg: float | None = Field(default=None, ge=0, lt=360)
    departure_runway_heading_deg: float | None = Field(default=None, ge=0, lt=360)
    departure_time_utc: datetime | None = None
    manual_winds: list[ManualWindEntry] = Field(default_factory=list, max_length=16)


class TerrainProfileRequest(StrictModel):
    waypoints: list[list[float]] = Field(min_length=2, max_length=32)
    cruise_altitude_ft: int = Field(ge=500, le=18000)

    def waypoint_pairs(self) -> list[tuple[float, float]]:
        pairs: list[tuple[float, float]] = []
        for point in self.waypoints:
            if len(point) != 2:
                raise BadRequest("each waypoint must be exactly [latitude, longitude]")
            lat, lon = float(point[0]), float(point[1])
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                raise BadRequest(f"waypoint out of geographic range: {point}")
            pairs.append((lat, lon))
        return pairs


class GpsIngestRequest(StrictModel):
    positions: list[dict[str, Any]] = Field(default_factory=list, max_length=512)


def jsonify(value: Any) -> Any:
    """Recursively convert dataclasses/datetimes into JSON-safe structures."""
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: jsonify(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonify(v) for v in value]
    return value


def error_response(status_code: int, code: str, message: str, detail: Any | None = None) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if detail is not None:
        body["error"]["detail"] = jsonify(detail)
    return JSONResponse(status_code=status_code, content=body)


def ok_payload(payload: Any) -> dict[str, Any]:
    return {"ok": True, **jsonify(payload)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def variation_lookup(lat: float, lon: float, altitude_ft: float) -> float:
        model = await load_world_magnetic_model(cache_dir=DATA_DIR)
        return model.declination_deg(lat, lon, altitude_ft)

    app.state.variation_lookup = variation_lookup
    app.state.weather = AviationWeatherEngine(variation_lookup=variation_lookup)
    app.state.winds = WindsAloftEngine()
    app.state.terrain = TerrainEngine(cache_dir=DATA_DIR / "cache" / "terrain")
    app.state.enaire = EnaireAd2Service(
        repository=EnaireAipRepository(db_path=DATA_DIR / "enaire.sqlite"),
    )
    app.state.airref = AirReferenceService(cache_dir=DATA_DIR / "cache" / "ourairports")
    app.state.chart_proxy = EnaireChartTileProxy(cache_dir=DATA_DIR / "cache")
    app.state.notam = EnaireNotamService()
    app.state.ground = GroundReferenceService(cache_dir=DATA_DIR / "cache" / "ourairports")
    app.state.wmm_model = None
    log.info("engines initialized; data directory ready")
    yield


app = FastAPI(
    title="aviation_richdale VFR Platform",
    version="1.0.0",
    description=(
        "Open-source VFR flight planning: NOAA weather ingestion, E6B wind-triangle "
        "navigation log, SRTM terrain awareness with a 1,000 ft safety ceiling, "
        "and ENAIRE AIP Spain compliance structures."
    ),
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#04070f'/>"
    "<path d='M6 23 L13 15 L18.5 18 L26 9' stroke='#22d3ee' stroke-width='2.6' "
    "fill='none' stroke-linecap='round' stroke-linejoin='round'/>"
    "<circle cx='26' cy='9' r='2.6' fill='#a78bfa'/>"
    "</svg>"
)


@app.get("/favicon.ico")
async def favicon():
    from fastapi.responses import Response
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    log.warning("api error %s: %s", exc.code, exc.message)
    return error_response(exc.status_code, exc.code, exc.message, exc.detail)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    simplified = [
        {
            "field": ".".join(str(loc) for loc in err.get("loc", []) if loc != "body"),
            "message": err.get("msg", "invalid value"),
        }
        for err in exc.errors()
    ]
    return error_response(422, "validation_failed", "request payload rejected", simplified)


@app.exception_handler(WeatherServiceError)
async def weather_error_handler(_: Request, exc: WeatherServiceError) -> JSONResponse:
    log.warning("weather engine failure: %s", exc)
    return error_response(502, "weather_source_error", str(exc))


@app.exception_handler(TerrainServiceError)
async def terrain_error_handler(_: Request, exc: TerrainServiceError) -> JSONResponse:
    log.warning("terrain engine failure: %s", exc)
    return error_response(502, "terrain_source_error", str(exc))


@app.exception_handler(EnaireError)
async def enaire_error_handler(_: Request, exc: EnaireError) -> JSONResponse:
    log.warning("ENAIRE engine failure: %s", exc)
    return error_response(502, "enaire_source_error", str(exc))


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled server fault")
    return error_response(500, "internal_error", "unexpected server fault; see server logs")


async def resolve_waypoints(
    entries: Sequence[str],
    engine: AviationWeatherEngine,
    air_reference: Any = None,
    enaire_service: Any = None,
) -> list[tuple[LatLon, str]]:
    """Resolve route entries: 'lat,lon', airport ICAO, navaid ident
    (optionally typed: 'VOR:VLC', 'NDB:SKA'), or ENAIRE reporting points
    ('RP:FOIOS' from already-indexed charts, 'LEVC:FOIOS' to load one)."""
    resolved: list[tuple[LatLon, str]] = []
    seen_labels: set[str] = set()
    for entry in entries:
        label = entry.strip()
        if not label:
            continue
        key = label.upper()
        if key in seen_labels:
            raise BadRequest(f"duplicate route point: {label}")
        seen_labels.add(key)

        coord_match = COORD_ENTRY_RE.match(label.replace(" ", ""))
        if coord_match:
            lat = float(coord_match.group(1))
            lon = float(coord_match.group(2))
            if abs(lat) > 90 or abs(lon) > 180:
                raise BadRequest(f"coordinates out of range: {label}")
            resolved.append((LatLon(latitude=lat, longitude=lon), f"{lat:.4f},{lon:.4f}"))
            continue

        # ---- prefixed forms -------------------------------------------------
        if ":" in key and not coord_match:
            prefix, _, rest = key.partition(":")
            rest = rest.strip()
            if not rest:
                raise BadRequest(f"empty value after ':' in: {label}")
            if prefix in {"VOR", "NDB", "DME", "TACAN", "VORDME"}:
                if air_reference is None:
                    raise BadRequest("navaid lookup unavailable (dataset not loaded)")
                hit = await air_reference.navaid_by_ident(rest, "VOR" if prefix == "VORDME" else prefix)
                if hit is None:
                    raise BadRequest(
                        f"no {prefix} navaid with ident '{rest}' in OurAirports dataset"
                    )
                note = f" ({hit['candidates']} worldwide share this ident; ES preferred)" if hit["candidates"] > 1 else ""
                log.info("navaid waypoint %s -> %s%s", label, hit["ident"], note)
                resolved.append((LatLon(latitude=hit["latitude"], longitude=hit["longitude"]),
                                 f"{hit['ident']} {hit['type']}"))
                continue
            if prefix == "RP":
                hit = _rp_lookup(rest)
                if hit is None:
                    raise BadRequest(
                        f"reporting point '{rest}' is not indexed yet â€” open that airport's "
                        "VAC once, or use the ICAO:NAME form (e.g. LEVC:FOIOS)"
                    )
                resolved.append((LatLon(latitude=hit["latitude"], longitude=hit["longitude"]),
                                 f"RP {rest}"))
                continue
            if len(prefix) == 4:  # ICAO:NAME â€” load this field's chart on demand
                try:
                    snap = await enaire_service.get_ad2(prefix)
                except Exception as exc:  # noqa: BLE001 - explicit degradation
                    raise BadRequest(f"could not load AD-2 for {prefix}: {exc}") from exc
                _index_reporting_points(snap, prefix)
                target = rest.upper()
                for p in getattr(snap, "reporting_points", []) or []:
                    pname = str(getattr(p, "name", "")).strip().upper()
                    pbase = pname.split("(", 1)[0].strip()
                    if pname == target or (pbase and pbase == target):
                        resolved.append((LatLon(latitude=p.latitude_deg, longitude=p.longitude_deg),
                                         f"RP {target}"))
                        break
                else:
                    known = ", ".join(sorted(str(getattr(p, "name", "")) for p in snap.reporting_points)[:8]) or "none parsed"
                    raise BadRequest(f"'{rest}' not among {prefix} reporting points ({known})")
                continue
            raise BadRequest(
                f"unknown prefix '{prefix}:' â€” use VOR:, NDB:, DME:, RP: or ICAO:NAME"
            )

        # ---- bare idents: local airports first, then NOAA station, then navaid
        airport = None
        if air_reference is not None:
            airport = await air_reference.airport_by_ident(key)
        if airport is not None:
            resolved.append((LatLon(latitude=airport["latitude"], longitude=airport["longitude"]),
                             airport["ident"]))
            continue
        try:
            station = await engine.resolve_station(key)
        except (UnknownStation, WeatherServiceError):
            # no live METAR station by that ident â€” fall through to navaids
            station = None
        if station is not None:
            resolved.append(
                (LatLon(latitude=station.latitude, longitude=station.longitude), station.icao)
            )
            continue
        if air_reference is not None:
            hit = await air_reference.navaid_by_ident(key)
            if hit is not None:
                resolved.append((LatLon(latitude=hit["latitude"], longitude=hit["longitude"]),
                                 f"{hit['ident']} {hit['type']}"))
                continue
        raise BadRequest(
            f"unresolved waypoint '{label}' â€” use an airport ICAO, navaid ident "
            "(VOR:/NDB:), RP:NAME / ICAO:NAME, or 'lat,lon'"
        )

    if len(resolved) < 2:
        raise BadRequest("a route needs at least two distinct points")
    return resolved


# Reporting points seen so far this process: NAME -> {icao, latitude, longitude}
# Populated opportunistically whenever an AD-2 snapshot is loaded â€” never by
# mass-downloading charts. Honest gap: RP:NAME fails until a chart is opened
# or the ICAO:NAME form loads it explicitly.
_reporting_point_index: dict[str, dict[str, Any]] = {}


def _index_reporting_points(snapshot: Any, icao: str) -> None:
    for p in getattr(snapshot, "reporting_points", []) or []:
        name = str(getattr(p, "name", "")).strip()
        if not name:
            continue
        entry = {"icao": icao, "latitude": p.latitude_deg, "longitude": p.longitude_deg}
        # index the printed name AND its base form ("N-1 (FOIOS)" -> also "N-1")
        aliases = {name.upper()}
        base = name.split("(", 1)[0].strip().upper()
        if base:
            aliases.add(base)
        for alias in aliases:
            _reporting_point_index.setdefault(alias, entry)


def _rp_lookup(name: str) -> dict[str, Any] | None:
    """Reporting-point lookup tolerant of the '(place)' suffix."""
    key = name.strip().upper()
    hit = _reporting_point_index.get(key)
    if hit is None:
        base = key.split("(", 1)[0].strip()
        hit = _reporting_point_index.get(base)
    return hit


async def get_variation(engine_state: Any, lat: float, lon: float, alt_ft: float) -> tuple[float, bool]:
    try:
        lookup = engine_state.variation_lookup
        value = await lookup(lat, lon, alt_ft)
        return value, True
    except Exception as exc:  # noqa: BLE001 - degradation must be explicit, never silent
        log.warning("magnetic variation unavailable at %.3f,%.3f: %s", lat, lon, exc)
        return 0.0, False


@app.get("/")
async def index(request: Request):
    openaip_key = os.environ.get("OPENAIP_API_KEY")
    ofm_template = os.environ.get("OPENFLIGHTMAPS_TILE_URL")
    build_candidates = [Path(__file__), TEMPLATES_DIR / "index.html"]
    build_stamp = max(int(p.stat().st_mtime) for p in build_candidates)
    context = {
        "request": request,
        "openaip_api_key": openaip_key or "",
        "aemet_api_key": os.environ.get("AEMET_API_KEY", ""),
        "ofm_tile_url": ofm_template or "",
        "map_center": DEFAULT_MAP_CENTER,
        "map_bbox_sw": DEFAULT_MAP_BBOX_SW,
        "map_bbox_ne": DEFAULT_MAP_BBOX_NE,
        "crosswind_limit_kt": CROSSWIND_LIMIT_KT,
        "safety_ceiling_ft": SAFETY_CEILING_FT,
        "build_id": f"b{datetime.fromtimestamp(build_stamp, tz=timezone.utc).strftime('%y%m%d.%H%M')}",
    }
    response = templates.TemplateResponse(request, "index.html", context)
    # the UI evolves fast â€” never let a browser serve yesterday's cockpit
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/api/config")
async def get_config():
    return ok_payload(
        {
            "map": {
                "center": DEFAULT_MAP_CENTER,
                "bbox": [DEFAULT_MAP_BBOX_SW, DEFAULT_MAP_BBOX_NE],
                "zoom": DEFAULT_MAP_ZOOM,
            },
            "limits": {"crosswind_kt": CROSSWIND_LIMIT_KT, "safety_ceiling_ft": SAFETY_CEILING_FT},
        }
    )


@app.get("/api/health")
async def health():
    return ok_payload(
        {
            "status": "ok",
            "python": sys.version.split()[0],
            "providers": {
                "noaa_awc": True,
                "winds_grid": True,
                "terrain_opentopography": bool(os.environ.get("OT_API_KEY")),
                "terrain_srtm_mirror": bool(os.environ.get("SRTM_BASE_URL")),
                "terrain_terrarium_fallback": True,
                "openaip_overlay": bool(os.environ.get("OPENAIP_API_KEY")),
                "openflightmaps_overlay": bool(os.environ.get("OPENFLIGHTMAPS_TILE_URL")),
                "enaire_ad2": True,
            },
        }
    )


@app.post("/api/weather/metar")
async def post_metar(body: MetarRequest):
    reports = await app.state.weather.latest_metars(body.icaos)
    missing = sorted(set(i.upper() for i in body.icaos) - {r.icao for r in reports})
    return ok_payload({"reports": reports, "missing": missing})


@app.post("/api/weather/taf")
async def post_taf(body: TafRequest):
    reports = await app.state.weather.latest_tafs(body.icaos)
    missing = sorted(set(i.upper() for i in body.icaos) - {r.icao for r in reports})
    return ok_payload({"reports": reports, "missing": missing})


@app.post("/api/weather/crosswind")
async def post_crosswind(body: CrosswindRequest):
    assessment = await app.state.weather.assess_crosswind(
        body.icao,
        runway_heading_magnetic_deg=body.runway_heading_deg,
        limit_kt=body.limit_kt if body.limit_kt is not None else CROSSWIND_LIMIT_KT,
    )
    return ok_payload({"assessment": assessment})


@app.post("/api/navigation/log")
async def post_navlog(body: NavLogRequest):
    engine: AviationWeatherEngine = app.state.weather
    winds_engine: WindsAloftEngine = app.state.winds

    entries = [body.departure, *body.waypoints, body.arrival]
    points = await resolve_waypoints(
        entries, engine,
        air_reference=getattr(app.state, "airref", None),
        enaire_service=app.state.enaire,
    )
    fuel_rate_gph = body.fuel_rate / 3.785411784 if body.fuel_unit == "LPH" else body.fuel_rate

    start_time = (body.departure_time_utc or datetime.now(timezone.utc))
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)

    legs: list[dict[str, Any]] = []
    warnings: list[str] = []
    cumulative_seconds = 0.0
    cumulative_fuel_gal = 0.0
    total_distance_nm = 0.0

    for idx in range(len(points) - 1):
        origin, origin_label = points[idx]
        dest, dest_label = points[idx + 1]

        distance_nm = haversine_distance_nm(origin.latitude, origin.longitude, dest.latitude, dest.longitude)
        true_course = initial_true_bearing_deg(origin.latitude, origin.longitude, dest.latitude, dest.longitude)
        mid_lat = (origin.latitude + dest.latitude) / 2.0
        mid_lon = (origin.longitude + dest.longitude) / 2.0

        variation_deg, variation_ok = await get_variation(app.state, mid_lat, mid_lon, body.cruise_altitude_ft)
        if not variation_ok:
            warnings.append(
                f"leg {idx + 1}: WMM declination unavailable; magnetic values assume zero variation"
            )

        manual_wind = None
        best_dist = MANUAL_WIND_MATCH_RADIUS_NM
        for entry in body.manual_winds:
            d = haversine_distance_nm(mid_lat, mid_lon, entry.latitude, entry.longitude)
            if d < best_dist:
                best_dist = d
                manual_wind = (entry.wind_from_deg, entry.wind_speed_kt)

        wind_sample = None
        wind_note = None
        estimated_eta = start_time + timedelta(seconds=cumulative_seconds + (distance_nm / body.tas_kt) * 3600.0)
        for attempt in range(2):
            try:
                if manual_wind is not None:
                    wind_sample = {
                        "wind_from_deg_true": manual_wind[0],
                        "wind_speed_kt": manual_wind[1],
                        "pressure_level_hpa": None,
                        "valid_time_utc": estimated_eta.isoformat(),
                        "provider": "pilot-supplied forecast",
                        "temperature_c": None,
                    }
                else:
                    sample = await winds_engine.sample(mid_lat, mid_lon, body.cruise_altitude_ft, eta_utc=estimated_eta)
                    wind_sample = {
                        "wind_from_deg_true": sample.wind_from_deg_true,
                        "wind_speed_kt": sample.wind_speed_kt,
                        "pressure_level_hpa": sample.pressure_level_hpa,
                        "valid_time_utc": sample.valid_time_utc,
                        "provider": sample.provider,
                        "temperature_c": sample.temperature_c,
                    }
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    wind_note = f"winds aloft unavailable ({exc}); solved with zero-wind assumptions"
                    warnings.append(f"leg {idx + 1}: {wind_note}")
                    wind_sample = {
                        "wind_from_deg_true": None,
                        "wind_speed_kt": 0.0,
                        "pressure_level_hpa": None,
                        "valid_time_utc": estimated_eta.isoformat(),
                        "provider": "none",
                        "temperature_c": None,
                    }
                else:
                    estimated_eta = estimated_eta + timedelta(minutes=2)

        solution = solve_wind_triangle(
            true_course_deg=true_course,
            true_airspeed_kt=body.tas_kt,
            wind_from_deg_true=(wind_sample or {}).get("wind_from_deg_true"),
            wind_speed_kt=float((wind_sample or {}).get("wind_speed_kt") or 0.0),
        )
        solution = apply_magnetic_variation(solution, variation_deg)

        ete_seconds = (distance_nm / solution.ground_speed_kt) * 3600.0 if solution.ground_speed_kt > 0 else float("inf")
        if ete_seconds == float("inf"):
            raise BadRequest(
                f"leg {idx + 1}: ground speed collapsed to zero; adjust TAS, altitude, or winds"
            )

        refined_solution = solution
        if wind_sample and wind_sample["wind_from_deg_true"] is not None and not manual_wind:
            refined_eta = start_time + timedelta(seconds=cumulative_seconds + ete_seconds)
            try:
                sample2 = await winds_engine.sample(mid_lat, mid_lon, body.cruise_altitude_ft, eta_utc=refined_eta)
                wind_sample = {
                    "wind_from_deg_true": sample2.wind_from_deg_true,
                    "wind_speed_kt": sample2.wind_speed_kt,
                    "pressure_level_hpa": sample2.pressure_level_hpa,
                    "valid_time_utc": sample2.valid_time_utc,
                    "provider": sample2.provider,
                    "temperature_c": sample2.temperature_c,
                }
                refined_solution = apply_magnetic_variation(
                    solve_wind_triangle(true_course, body.tas_kt, sample2.wind_from_deg_true, sample2.wind_speed_kt),
                    variation_deg,
                )
                ete_seconds = (distance_nm / refined_solution.ground_speed_kt) * 3600.0
            except Exception:  # noqa: BLE001 - keep first-pass figures
                pass
        solution = refined_solution

        fuel_burn_gal = (ete_seconds / 3600.0) * fuel_rate_gph
        cumulative_seconds += ete_seconds
        cumulative_fuel_gal += fuel_burn_gal
        total_distance_nm += distance_nm

        geometry = great_circle_points(
            origin.latitude, origin.longitude, dest.latitude, dest.longitude, LEG_GEOMETRY_SAMPLES
        )

        legs.append(
            {
                "index": idx + 1,
                "from": origin_label,
                "to": dest_label,
                "from_coords": [origin.latitude, origin.longitude],
                "to_coords": [dest.latitude, dest.longitude],
                "distance_nm": round(distance_nm, 2),
                "true_course_deg": round(solution.true_course_deg, 1),
                "magnetic_variation_deg": round(variation_deg, 1),
                "variation_authoritative": variation_ok,
                "wind_correction_angle_deg": round(solution.wind_correction_angle_deg, 1),
                "true_heading_deg": round(solution.true_heading_deg, 1),
                "magnetic_heading_deg": round(solution.magnetic_heading_deg, 1),
                "ground_speed_kt": round(solution.ground_speed_kt, 1),
                "headwind_component_kt": round(solution.headwind_component_kt, 1),
                "ete_min": round(ete_seconds / 60.0, 1),
                "fuel_gal": round(fuel_burn_gal, 2),
                "cumulative_min": round(cumulative_seconds / 60.0, 1),
                "cumulative_fuel_gal": round(cumulative_fuel_gal, 2),
                "eta_utc": (start_time + timedelta(seconds=cumulative_seconds)).isoformat(),
                "wind": wind_sample,
                "note": wind_note,
                "geometry": [[p.latitude, p.longitude] for p in geometry],
            }
        )

    total_ete_min = cumulative_seconds / 60.0
    average_gs = (total_distance_nm / (cumulative_seconds / 3600.0)) if cumulative_seconds > 0 else 0.0

    crosswind_alert = None
    if body.destination_runway_heading_deg is not None:
        arrival_icao = body.arrival.strip().upper()
        if COORD_ENTRY_RE.match(arrival_icao):
            warnings.append("arrival is a coordinate fix; runway crosswind needs an observing-station ICAO")
        else:
            try:
                assessment = await app.state.weather.assess_crosswind(
                    arrival_icao,
                    runway_heading_magnetic_deg=body.destination_runway_heading_deg,
                )
                crosswind_alert = assessment
            except (NoUsableObservation, UnknownStation, WeatherServiceError) as exc:
                warnings.append(f"destination crosswind check failed: {exc}")

    return ok_payload(
        {
            "route": [label for _, label in points],
            "cruise_altitude_ft": body.cruise_altitude_ft,
            "tas_kt": body.tas_kt,
            "fuel_unit": body.fuel_unit,
            "legs": legs,
            "totals": {
                "distance_nm": round(total_distance_nm, 2),
                "ete_min": round(total_ete_min, 1),
                "ete_display": _fmt_hmm(cumulative_seconds),
                "fuel": round(cumulative_fuel_gal * (3.785411784 if body.fuel_unit == "LPH" else 1.0), 1),
                "average_groundspeed_kt": round(average_gs, 1),
                "block_off_utc": start_time.isoformat(),
                "on_block_utc": (start_time + timedelta(seconds=cumulative_seconds)).isoformat(),
            },
            "crosswind": crosswind_alert,
            "warnings": warnings,
        }
    )


def _fmt_hmm(seconds: float) -> str:
    total_minutes = int(round(seconds / 60.0))
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


class WindGridRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: list[dict[str, float]] = Field(min_length=2, max_length=14)
    altitudes_ft: list[int] = Field(
        default_factory=lambda: [2500, 4500, 6500, 8500, 10500, 12500],
        min_length=1,
        max_length=8,
    )
    tas_kt: float = Field(gt=20, le=400)


@app.post("/api/navigation/windgrid")
async def post_wind_grid(body: WindGridRequest):
    """Sample winds at every waypoint for each candidate altitude and rank levels."""
    pts = []
    for p in body.points:
        lat = p.get("latitude")
        lon = p.get("longitude")
        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            raise BadRequest("each point needs valid latitude/longitude")
        pts.append((float(lat), float(lon)))

    def leg_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
        return haversine_distance_nm(a[0], a[1], b[0], b[1])

    def leg_course(a: tuple[float, float], b: tuple[float, float]) -> float:
        return initial_true_bearing_deg(a[0], a[1], b[0], b[1])

    levels_out = []
    for alt in sorted(set(body.altitudes_ft)):
        try:
            batch = await sample_winds_batch(pts, float(alt))
        except Exception as exc:  # noqa: BLE001 â€” a dead grid level must not kill the ranking
            log.info("batched wind sampling failed at %dft: %s", alt, exc)
            continue
        samples = [
            {
                "latitude": lat,
                "longitude": lon,
                "wind_from_deg": w.wind_from_deg_true,
                "wind_speed_kt": w.wind_speed_kt,
            }
            for (lat, lon), w in batch.items()
        ]

        if len(samples) < 2:
            continue

        total_headwind = 0.0
        weighted_ete_s = 0.0
        total_dist_nm = 0.0
        for i in range(len(pts) - 1):
            dist_nm = leg_distance(pts[i], pts[i + 1])
            if dist_nm <= 0:
                continue
            course = leg_course(pts[i], pts[i + 1])
            # average the two endpoint samples for the leg
            w_from = [
                s["wind_from_deg"] for s in samples
                if (s["latitude"], s["longitude"]) in (pts[i], pts[i + 1])
            ]
            w_speed = [
                s["wind_speed_kt"] for s in samples
                if (s["latitude"], s["longitude"]) in (pts[i], pts[i + 1])
            ]
            if len(w_from) >= 2:
                from_deg = sum(w_from[:2]) / 2.0
                speed_kt = sum(w_speed[:2]) / 2.0
                head = speed_kt * math.cos(math.radians(from_deg - course))
                gs = max(20.0, body.tas_kt - head)
                total_headwind += head * dist_nm
                weighted_ete_s += (dist_nm / gs) * 3600.0
                total_dist_nm += dist_nm

        if total_dist_nm <= 0:
            continue

        levels_out.append({
            "altitude_ft": alt,
            "samples": samples,
            "total_distance_nm": round(total_dist_nm, 1),
            "distance_weighted_headwind_kt": round(total_headwind / total_dist_nm, 1),
            "average_gs_kt": round(body.tas_kt - total_headwind / total_dist_nm, 1),
            "ete_min": round(weighted_ete_s / 60.0, 1),
        })

    levels_out.sort(key=lambda lv: lv["ete_min"])
    recommended = levels_out[0]["altitude_ft"] if levels_out else None
    return ok_payload({
        "levels": levels_out,
        "recommended_altitude_ft": recommended,
        "tas_kt": body.tas_kt,
    })


class TafTimelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icaos: list[str] = Field(min_length=1, max_length=8)


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    waypoints: list[list[float]] = Field(min_length=2, max_length=14)
    cruise_altitude_ft: int = Field(ge=500, le=20000)
    tas_kt: float = Field(gt=20, le=400)
    fuel_rate_gph: float | None = Field(default=None, gt=0, le=100)


@app.post("/api/simulation/replay")
async def post_simulation_replay(body: ReplayRequest):
    """Ghost-flyer: integrate the route through TODAY'S forecast winds.

    Point-mass kinematics only â€” position advances along each leg with the
    wind triangle solved at every step from GFS samples interpolated along
    the route. No aircraft dynamics are pretended; this answers 'when/fuel/
    drift if I fly this plan through this sky', nothing more.
    """
    pts_in = [(float(lat), float(lon)) for lat, lon in body.waypoints]
    for lat, lon in pts_in:
        if abs(lat) > 90 or abs(lon) > 180:
            raise BadRequest("each waypoint needs valid latitude/longitude")

    def hav(a: tuple[float, float], b: tuple[float, float]) -> float:
        return haversine_distance_nm(a[0], a[1], b[0], b[1])

    def course(a: tuple[float, float], b: tuple[float, float]) -> float:
        return initial_true_bearing_deg(a[0], a[1], b[0], b[1])

    total_nm = sum(hav(pts_in[i], pts_in[i + 1]) for i in range(len(pts_in) - 1))
    if total_nm <= 0:
        raise BadRequest("route has zero length")

    # ---- coarse wind field along the route (one batched Open-Meteo call) ----
    k = min(12, max(4, int(total_nm / 40)))
    samples: list[tuple[float, float]] = []
    for i in range(k):
        f = i / (k - 1) if k > 1 else 0.0
        target = f * total_nm
        acc = 0.0
        for j in range(len(pts_in) - 1):
            seg = hav(pts_in[j], pts_in[j + 1])
            if acc + seg >= target or j == len(pts_in) - 2:
                t = (target - acc) / seg if seg > 0 else 0.0
                t = max(0.0, min(1.0, t))
                samples.append((
                    pts_in[j][0] + (pts_in[j + 1][0] - pts_in[j][0]) * t,
                    pts_in[j][1] + (pts_in[j + 1][1] - pts_in[j][1]) * t,
                ))
                break
            acc += seg
    try:
        batch = await sample_winds_batch(samples, float(body.cruise_altitude_ft))
    except Exception as exc:  # noqa: BLE001 â€” explicit degradation, never fake calm
        raise BadRequest(
            f"winds unavailable for replay ({exc}); simulation needs Open-Meteo"
        ) from exc
    wind_list = [
        (lat, lon, w.wind_from_deg_true, w.wind_speed_kt)
        for (lat, lon), w in batch.items()
    ]
    if not wind_list:
        raise BadRequest("wind grid returned no usable samples")

    def wind_at(lat: float, lon: float) -> tuple[float, float]:
        best = min(wind_list, key=lambda s: (s[0] - lat) ** 2 + ((s[1] - lon) * math.cos(math.radians(lat))) ** 2)
        return best[2], best[3]

    def advance(p: tuple[float, float], crs_deg: float, dist_nm: float) -> tuple[float, float]:
        rad = math.radians(crs_deg)
        dlat = dist_nm * math.cos(rad) / 60.0
        dlon = dist_nm * math.sin(rad) / (60.0 * max(0.2, math.cos(math.radians(p[0]))))
        return (p[0] + dlat, p[1] + dlon)

    # ---- integration -------------------------------------------------------
    timeline: list[dict[str, Any]] = []
    fuel_gal = 0.0
    elapsed_s = 0.0
    done_nm = 0.0
    still_air_s = total_nm / body.tas_kt * 3600.0

    for li in range(len(pts_in) - 1):
        a, b = pts_in[li], pts_in[li + 1]
        leg_crs = course(a, b)
        p = a
        remaining = hav(a, b)
        guard = 0
        while remaining > 0.05 and guard < 600:
            guard += 1
            crs_to_go = course(p, b)
            w_from, w_spd = wind_at(*p)
            rel = math.radians((w_from - crs_to_go) % 360.0)
            xw = w_spd * math.sin(rel)          # from the right -> positive
            hw = w_spd * math.cos(rel)          # headwind positive
            tas = body.tas_kt
            root = max(1.0, tas * tas - xw * xw)
            gs = math.sqrt(root) - hw           # groundspeed along course
            gs = max(15.0, gs)                  # physics floor, never negative-time
            dt_s = min(60.0, remaining / gs * 3600.0)
            step_nm = gs * dt_s / 3600.0
            p = advance(p, crs_to_go, step_nm)
            remaining = hav(p, b)
            elapsed_s += dt_s
            done_nm += step_nm
            if body.fuel_rate_gph:
                fuel_gal += body.fuel_rate_gph * dt_s / 3600.0
            if len(timeline) == 0 or elapsed_s - timeline[-1]["t"] >= 60.0 or remaining <= 0.05:
                timeline.append({
                    "t": round(elapsed_s, 1),
                    "latitude": round(p[0], 5),
                    "longitude": round(p[1], 5),
                    "leg": li,
                    "gs_kt": round(gs, 1),
                    "track_deg": round(crs_to_go, 1),
                    "wind_from_deg": round(w_from),
                    "wind_kt": round(w_spd, 1),
                    "fuel_gal": round(fuel_gal, 2) if body.fuel_rate_gph else None,
                    "distance_nm": round(done_nm, 2),
                })

    drift_s = elapsed_s - still_air_s
    return ok_payload({
        "timeline": timeline,
        "totals": {
            "distance_nm": round(total_nm, 2),
            "sim_time_s": round(elapsed_s),
            "still_air_time_s": round(still_air_s),
            "drift_s": round(drift_s),
            "fuel_gal": round(fuel_gal, 2) if body.fuel_rate_gph else None,
            "avg_gs_kt": round(total_nm / (elapsed_s / 3600.0), 1) if elapsed_s > 0 else None,
        },
        "wind_samples_used": len(wind_list),
        "method_note": (
            "point-mass kinematics through GFS winds sampled every "
            f"~{total_nm / k:.0f} NM; no aircraft dynamics, no turbulence model"
        ),
    })


class BriefRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icaos: list[str] = Field(min_length=1, max_length=8)


@app.post("/api/weather/briefing")
async def post_weather_briefing(body: BriefRequest):
    """Real RAW METAR history + full raw TAF per airport (NOAA AWC, no keys)."""
    out: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as client:
        for icao in body.icaos:
            key = icao.strip().upper()[:4]
            if not re.fullmatch(r"[A-Z]{4}", key):
                out[icao] = {"error": "not an ICAO ident"}
                continue
            entry: dict[str, Any] = {}
            try:
                r = await client.get(
                    "https://aviationweather.gov/api/data/metar",
                    params={"ids": key, "format": "raw", "hours": 4},
                )
                lines = [ln for ln in (r.text or "").splitlines() if ln.startswith("METAR")]
                entry["metar_raw"] = lines[:4]
            except httpx.HTTPError as exc:
                entry["metar_raw_error"] = f"NOAA unreachable: {exc.__class__.__name__}"
            try:
                t = await client.get(
                    "https://aviationweather.gov/api/data/taf",
                    params={"ids": key, "format": "raw"},
                )
                entry["taf_raw"] = (t.text or "").strip()
            except httpx.HTTPError as exc:
                entry["taf_raw_error"] = f"NOAA unreachable: {exc.__class__.__name__}"
            out[key] = entry
    return ok_payload({"briefings": out})


@app.get("/api/sigmet/spain")
async def get_sigmet_spain():
    """Active SIGMETs for Spanish FIRs (Madrid/Barcelona/Canariasâ€¦) from NOAA AWC."""
    import asyncio as _aio

    def _fetch() -> Any:
        r = httpx.get(
            "https://aviationweather.gov/api/data/sigmet",
            params={"format": "json", "hours": 6},
            timeout=25.0,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        return r.json()

    try:
        data = await _aio.to_thread(_fetch)
    except Exception as exc:  # noqa: BLE001 â€” explicit degradation
        return ok_payload({"sigmets": [], "note": f"SIGMET feed unavailable: {exc.__class__.__name__}"})
    items = []
    for s in data if isinstance(data, list) else []:
        fir = str(s.get("fir", ""))
        raw = str(s.get("rawText", ""))
        if re.match(r"^(LE|GE|GC)", fir) or re.search(r"MADRID|BARCELONA|CANARIAS|CANARY", raw.upper()):
            items.append({
                "fir": fir,
                "raw": raw,
                "valid_from": s.get("validTimeFrom"),
                "valid_to": s.get("validTimeTo"),
                "hazard": s.get("hazard"),
            })
    return ok_payload({
        "sigmets": items,
        "checked_worldwide": len(data) if isinstance(data, list) else 0,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "empty list means no active SIGMET over Spain right now",
    })


@app.get("/api/aemet/diag")
async def get_aemet_diag():
    """Honest probe of AEMET OpenData: per-endpoint HTTP status + byte count.

    AEMET's gateway has been observed returning 200 with EMPTY bodies for all
    endpoints from some networks; this endpoint exists so the UI can show that
    fact instead of a silent empty chart.
    """
    key = os.environ.get("AEMET_API_KEY", "")
    if not key:
        return ok_payload({"configured": False,
                           "note": "set AEMET_API_KEY in .env.txt (free key at aemet.es/opendata)"})
    probes: list[dict[str, Any]] = []
    f = datetime.now(timezone.utc).date().isoformat()
    targets = {
        "sigwx_espana_d0": f"/opendata/api/mapasygraficos/mapassignificativos/fecha/{f}/espana/0",
        "analisis": "/opendata/api/mapasygraficos/analisis",
        "avisos_cap": "/opendata/api/avisos_cap/ultimoelaborado/area/espen",
    }
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for label, path in targets.items():
            try:
                r = await client.get(f"https://opendata.aemet.es{path}", params={"api_key": key})
                probes.append({"probe": label, "status": r.status_code, "bytes": len(r.content)})
            except httpx.HTTPError as exc:
                probes.append({"probe": label, "error": exc.__class__.__name__})
    alive = any(p.get("bytes", 0) > 0 for p in probes)
    return ok_payload({
        "configured": True, "probes": probes,
        "gateway_serving_data": alive,
        "note": ("AEMET responded normally" if alive
                 else "AEMET gateway returned EMPTY bodies for every endpoint â€” "
                      "their service is not delivering data to this network right now"),
    })


async def _aemet_json(path: str) -> dict[str, Any]:
    """Call an AEMET OpenData endpoint; raise BadRequest with honest detail."""
    key = os.environ.get("AEMET_API_KEY", "")
    if not key:
        raise BadRequest("AEMET_API_KEY is not configured (.env.txt)")
    url = f"https://opendata.aemet.es/opendata/api{path}"
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        r = await client.get(url, params={"api_key": key})
        if len(r.content) == 0:
            raise BadRequest(
                "AEMET returned an empty response (their gateway currently serves "
                "no data on this route/network â€” see /api/aemet/diag)")
        try:
            body = r.json()
        except ValueError as exc:
            raise BadRequest(f"AEMET returned non-JSON ({r.status_code})") from exc
        datos = body.get("datos")
        if not datos:
            raise BadRequest(
                f"AEMET: {body.get('descripcion') or 'no data URL returned'} "
                f"(estado {body.get('estado')})")
        m = await client.get(datos)
        if len(m.content) == 0:
            raise BadRequest("AEMET data URL returned empty content")
        return {"meta": body, "payload_text": m.text, "content_type": m.headers.get("content-type", "")}


@app.get("/api/aemet/sigwx")
async def get_aemet_sigwx(ambito: str = "espana", dia: int = 0):
    """Significant-weather chart metadata+image for ambito/espana|peninsula|baleares|canarias."""
    f = datetime.now(timezone.utc).date().isoformat()
    res = await _aemet_json(f"/mapasygraficos/mapassignificativos/fecha/{f}/{ambito}/{max(0, min(2, dia))}")
    # payload may be JSON list of images or a single image URL â€” normalise
    text = res["payload_text"]
    img_urls: list[str] = []
    cap = ""
    try:
        parsed = json.loads(text)
        items = parsed if isinstance(parsed, list) else [parsed]
        for it in items:
            u = it.get("url") or it.get("imagen") or ""
            if u:
                img_urls.append(u)
            if not cap:
                cap = it.get("titulo") or it.get("descripcion") or ""
    except ValueError:
        m = re.search(r'https?://[^\s"\']+\.(?:gif|png|jpe?g)', text)
        if m:
            img_urls.append(m.group(0))
    if not img_urls:
        # payload itself might be the image bytes
        if res["content_type"].startswith("image/"):
            import hashlib as _h
            digest = _h.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]
            cache = DATA_DIR / "cache" / "aemet"
            cache.mkdir(parents=True, exist_ok=True)
            fp = cache / f"sigwx_{digest}"
            fp.write_bytes(text.encode("latin-1"))
            return ok_payload({"images": [{"src": f"/api/aemet/image?f={fp.name}"}], "caption": "", "source": "AEMET OpenData"})
        raise BadRequest("AEMET SIGWX payload had no image reference")
    import hashlib as _h
    out = []
    cache = DATA_DIR / "cache" / "aemet"
    cache.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for u in img_urls[:4]:
            r = await client.get(u)
            ext = ".png" if "png" in r.headers.get("content-type", "") else ".gif"
            name = f"sigwx_{_h.sha256(u.encode()).hexdigest()[:16]}{ext}"
            (cache / name).write_bytes(r.content)
            out.append({"src": f"/api/aemet/image?f={name}"})
    return ok_payload({"images": out, "caption": str(cap), "source": "AEMET OpenData"})


_FRENTES_STEP_CODES = {
    "000": "gpx0a000",
    "024": "g1x0a2d1",
    "036": "g1x0a2c1",
    "048": "g1x0a2d2",
    "060": "g1x0a2c2",
    "072": "g1x0a2d3",
}
_frentes_cache: dict[str, Any] = {"at": 0.0, "data": None}


@app.get("/api/aemet/frentes")
async def get_aemet_frentes():
    """Latest significant-weather ('mapa de frentes') chart set from aemet.es public pages."""
    import time as _time
    now_mono = _time.monotonic()
    if _frentes_cache["data"] and now_mono - _frentes_cache["at"] < 1800:
        return ok_payload(_frentes_cache["data"])
    now = datetime.now(timezone.utc)
    day = timedelta(days=1)
    runs = []
    for cand in (
        now.replace(hour=0, minute=0, second=0, microsecond=0),
        (now - day).replace(hour=12, minute=0, second=0, microsecond=0),
        (now - day).replace(hour=0, minute=0, second=0, microsecond=0),
        (now - 2 * day).replace(hour=12, minute=0, second=0, microsecond=0),
    ):
        if cand <= now:
            runs.append(cand)
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for run in runs:
            base = run.strftime("%Y%m%d%H")
            found = []
            for step, code in _FRENTES_STEP_CODES.items():
                url = (f"https://www.aemet.es/imagenes_d/eltiempo/prediccion/"
                       f"mapa_frentes/{base}%2B{step}_ww_{code}.gif")
                try:
                    r = await client.head(url)
                    if r.status_code == 200:
                        found.append({"step": step, "url": url})
                except httpx.HTTPError:
                    continue
            if len(found) >= 3:
                data = {"run_utc": base, "images": found,
                        "source": "aemet.es mapa de frentes (public)"}
                _frentes_cache.update(at=now_mono, data=data)
                return ok_payload(data)
    raise BadRequest("no significant-weather chart run found on aemet.es")


@app.get("/api/aemet/image")
async def get_aemet_image(f: str):
    """Serve cached AEMET image by filename only (no paths, no traversal)."""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_.-]+", f):
        raise BadRequest("bad filename")
    fp = DATA_DIR / "cache" / "aemet" / f
    if not fp.is_file():
        raise BadRequest("not cached")
    ext = fp.suffix.lower()
    media = "image/png" if ext == ".png" else "image/gif"
    return Response(content=fp.read_bytes(), media_type=media)


@app.get("/api/aemet/analisis")
async def get_aemet_analisis():
    """AEMET surface analysis chart (fronts, pressure) â€” same pipeline as SIGWX."""
    res = await _aemet_json("/mapasygraficos/analisis")
    text = res["payload_text"]
    import hashlib as _h
    cache = DATA_DIR / "cache" / "aemet"
    cache.mkdir(parents=True, exist_ok=True)
    if res["content_type"].startswith("image/"):
        name = f"analisis_{_h.sha256(text.encode('utf-8', 'ignore')).hexdigest()[:16]}.png"
        (cache / name).write_bytes(text.encode("latin-1"))
        return ok_payload({"images": [{"src": f"/api/aemet/image?f={name}"}], "source": "AEMET OpenData"})
    m = re.search(r'https?://[^\s"\']+\.(?:gif|png|jpe?g)', text)
    if not m:
        raise BadRequest("analysis payload had no image reference")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(m.group(0))
        ext = ".png" if "png" in r.headers.get("content-type", "") else ".gif"
        name = f"analisis_{_h.sha256(m.group(0).encode()).hexdigest()[:16]}{ext}"
        (cache / name).write_bytes(r.content)
    return ok_payload({"images": [{"src": f"/api/aemet/image?f={name}"}], "source": "AEMET OpenData"})


@app.post("/api/weather/taf/timeline")
async def post_taf_timeline(body: TafTimelineRequest):
    """TAF periods serialized as validity-bar segments per station."""
    icaos = [i.strip().upper() for i in body.icaos]
    reports = await app.state.weather.latest_tafs(icaos)
    timelines = {rep.icao: serialize_taf_timeline(rep) for rep in reports}
    return ok_payload({"timelines": timelines})


class CloudProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: list[dict[str, float]] = Field(min_length=1, max_length=4)


@app.post("/api/weather/clouds")
async def post_cloud_profile(body: CloudProfileRequest):
    profiles = []
    for p in body.points:
        lat, lon = p.get("latitude"), p.get("longitude")
        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            raise BadRequest("each point needs valid latitude/longitude")
        try:
            profiles.append(await fetch_cloud_profile(float(lat), float(lon)))
        except httpx.HTTPError as exc:
            log.info("cloud profile failed at %.2f,%.2f: %s", lat, lon, exc)
            profiles.append({"latitude": lat, "longitude": lon, "hours": [], "error": "source unavailable"})
    return ok_payload({"profiles": profiles})


@app.get("/api/aip/reporting-points/{icao}")
async def get_reporting_points(icao: str):
    """VFR entry/exit reporting points parsed from the ENAIRE AD-2/VAC chart."""
    try:
        snap = await app.state.enaire.get_ad2(icao)
        _index_reporting_points(snap, icao.upper())
    except Exception as exc:  # noqa: BLE001 â€” points are an enhancement
        return ok_payload({"points": [], "note": f"AD2 unavailable: {exc}"})
    pts = [
        {"name": p.name, "latitude": p.latitude_deg, "longitude": p.longitude_deg}
        for p in snap.reporting_points
    ]
    note = None if pts else "No VFR reporting points parsed for this aerodrome."
    return ok_payload({"points": pts, "count": len(pts), "note": note, "vac_url": snap.vac_url})


@app.get("/api/weather/metar/history")
async def get_metar_history(icao: str, hours: int = 3):
    reports = await app.state.weather.metar_history(icao, hours)
    return ok_payload({
        "observations": [
            {
                "observed_at": r.observed_at.isoformat() if r.observed_at else None,
                "wind_from_deg": r.wind.direction_from_deg_true if r.wind else None,
                "speed_kt": r.wind.speed_kt if r.wind else None,
                "gust_kt": r.wind.gust_kt if r.wind else None,
                "temperature_c": r.temperature_c,
                "altimeter_hpa": r.altimeter_hpa,
                "raw": r.raw_text,
            }
            for r in reports
        ],
        "count": len(reports),
    })


class TrafficRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_nm: float = Field(default=10, ge=1, le=25)


OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) aviation-richdale/1.0"


@app.post("/api/traffic/nearby")
async def post_traffic_nearby(body: TrafficRequest):
    """Live ADS-B positions (OpenSky, anonymous). Advisory only â€” see MANUAL."""
    import math as _m
    dlat = body.radius_nm / 60.0
    dlon = dlat / max(0.2, _m.cos(_m.radians(body.latitude)))
    params = {
        "lamin": round(body.latitude - dlat, 4),
        "lamax": round(body.latitude + dlat, 4),
        "lomin": round(body.longitude - dlon, 4),
        "lomax": round(body.longitude + dlon, 4),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), headers={"User-Agent": USER_AGENT}) as client:
        try:
            response = await client.get(OPENSKY_STATES_URL, params=params)
            if response.status_code == 429:
                return ok_payload({"aircraft": [], "note": "Rate limited by OpenSky â€” retrying later."})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.info("traffic fetch failed: %s", exc)
            return ok_payload({"aircraft": [], "note": f"Traffic feed unavailable: {exc}"})

    states = payload.get("states") or []
    aircraft = []
    for s in states[:60]:
        # OpenSky state vector: icao24, callsign, origin_country, time_position,
        # last_contact, lon, lat, baro_alt(m), on_ground, velocity(m/s), true_track, ...
        try:
            lon, lat = float(s[5]), float(s[6])
        except (TypeError, ValueError):
            continue
        if s[8]:   # on_ground
            continue
        alt_m = s[7] if isinstance(s[7], (int, float)) else (s[13] if len(s) > 13 and isinstance(s[13], (int, float)) else None)
        vel_ms = s[9] if isinstance(s[9], (int, float)) else None
        track = s[10] if isinstance(s[10], (int, float)) else None
        vrate = s[14] if len(s) > 14 and isinstance(s[14], (int, float)) else None
        aircraft.append({
            "icao24": s[0],
            "callsign": (s[1] or "").strip() or s[0],
            "latitude": lat,
            "longitude": lon,
            "altitude_ft": round(alt_m * 3.28084) if alt_m is not None else None,
            "ground_speed_kt": round(vel_ms * 1.94384) if vel_ms is not None else None,
            "track_deg": track,
            "vertical_rate_fpm": round(vrate * 196.85) if vrate is not None else None,
            "distance_nm": round(_m.hypot(
                (lat - body.latitude) * 60.0,
                (lon - body.longitude) * 60.0 * _m.cos(_m.radians(body.latitude)),
            ), 1),
        })
    aircraft.sort(key=lambda a: a["distance_nm"])
    return ok_payload({
        "aircraft": aircraft,
        "count": len(aircraft),
        "note": "ADS-B coverage only â€” aircraft without transponders or below coverage are INVISIBLE.",
    })


@app.get("/api/notam")
async def get_notams(bbox: str):
    south, west, north, east = _bbox_or_bad(bbox)
    notams = await app.state.notam.query(south, west, north, east)
    active = [n for n in notams if not n["expired"]]
    return ok_payload({"notams": active, "count": len(active),
                       "expired_hidden": len(notams) - len(active)})


class GroundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    heading_deg: float | None = Field(default=None, ge=0, lt=360)


class GuardianRequest(BaseModel):
    """Engine-out reachability ask: what can this glide actually reach?"""
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_ft: float = Field(ge=0, le=25000)
    best_glide_kt: float = Field(default=75, ge=35, le=140)
    glide_ratio: float = Field(default=9.0, ge=3, le=25)


@app.post("/api/emergency/guardian")
async def post_guardian(body: GuardianRequest):
    """Wind-corrected glide envelope + ranked runway options from this spot."""
    import asyncio as _aio

    agl_ft = max(200.0, body.altitude_ft - 800.0)          # conservative field-elevation pad
    still_range_nm = body.glide_ratio * agl_ft / 6076.0
    time_hr = still_range_nm / body.best_glide_kt

    # nearest paved-runway fields within plausible glide range
    nearby = await app.state.ground.nearby_far(body.latitude, body.longitude,
                                               nm=max(30.0, still_range_nm * 1.6))
    if nearby is None:
        return ok_payload({"options": [], "ring": [], "assumptions": {
            "agl_ft": round(agl_ft), "still_air_range_nm": round(still_range_nm, 1),
            "wind": None, "note": "No paved-runway airport within glide-plus-margin.",
        }})

    # surface wind from the closest field with a report (veered +30 deg, x0.7 aloft proxy)
    wind = None
    try:
        idents = [n["airport"]["ident"] for n in nearby[:6]]
        reports = await app.state.weather.latest_metars(idents)
        for rep in reports:
            if rep.wind and rep.wind.speed_kt is not None and rep.wind.direction_from_deg_true:
                wind = {
                    "from_deg": (float(rep.wind.direction_from_deg_true) + 30.0) % 360.0,
                    "speed_kt": round(float(rep.wind.speed_kt) * 0.7, 1),
                    "source": f"{rep.icao} METAR",
                }
                break
    except Exception as exc:  # noqa: BLE001 â€” ring must work offline too
        log.info("guardian METAR unavailable: %s", exc)

    def range_nm(bearing_deg: float) -> float:
        if not wind:
            return still_range_nm
        import math as _m
        hw = wind["speed_kt"] * _m.cos(_m.radians(bearing_deg - wind["from_deg"]))
        gs = max(20.0, body.best_glide_kt - hw)
        return min(gs * time_hr, still_range_nm * 1.45)

    ring = []
    for k in range(73):
        brg = k * 5.0
        r = range_nm(brg)
        import math as _m
        ring.append([
            round(body.latitude + (_m.cos(_m.radians(brg)) * r) / 60.0, 4),
            round(body.longitude + (_m.sin(_m.radians(brg)) * r)
                  / (60.0 * max(0.2, _m.cos(_m.radians(body.latitude)))), 4),
        ])

    options = []
    import math as _m
    for entry in nearby:
        apt = entry["airport"]
        brg = _ground_initial_bearing(body.latitude, body.longitude,
                                      apt["latitude"], apt["longitude"])
        reach = range_nm(brg)
        dist = entry["distance_nm"]
        margin_nm = reach - dist
        if margin_nm < -1.0:
            continue
        for rwy in entry["runways"]:
            for side in ("le", "he"):
                ident = rwy.get(f"{side}_ident")
                head = rwy.get(f"{side}_heading_t")
                if not ident or head is None:
                    continue
                hw, xw_signed = _ground_wind_components(float(head),
                                                        wind["from_deg"] if wind else 0.0,
                                                        wind["speed_kt"] if wind else 0.0)
                xw = abs(xw_signed) if wind else 0.0
                score = (margin_nm / max(reach, 0.1)) * 100.0 \
                    - max(0.0, xw - 8.0) * 2.5 \
                    - (0.0 if (rwy.get("surface") or "").upper().startswith(("ASP", "CON", "BIT")) else 3.0)
                options.append({
                    "airport": apt["ident"],
                    "name": apt.get("name", ""),
                    "runway": str(ident).lstrip("0") if str(ident).endswith("0") else str(ident),
                    "runway_heading_t": float(head),
                    "surface": rwy.get("surface") or "",
                    "length_m": rwy["length_m"],
                    "distance_nm": round(dist, 1),
                    "bearing_deg": round(brg, 0),
                    "margin_nm": round(margin_nm, 1),
                    "headwind_kt": round(hw, 1) if wind else None,
                    "crosswind_kt": round(xw, 1) if wind else None,
                    "score": round(score, 1),
                })

    options.sort(key=lambda o: -o["score"])
    seen = set()
    top = []
    for o in options:
        key = (o["airport"], o["runway"])
        if key in seen:
            continue
        seen.add(key)
        top.append(o)
        if len(top) >= 5:
            break

    return ok_payload({
        "options": top,
        "ring": ring,
        "assumptions": {
            "agl_ft": round(agl_ft),
            "still_air_range_nm": round(still_range_nm, 1),
            "wind": wind,
            "note": "" if wind else "No METAR within range â€” still-air envelope shown.",
        },
    })


@app.post("/api/ground/nearby")
async def post_ground_nearby(body: GroundRequest):
    """Taxi-phase picture: nearest airport, its runways, live wind components."""
    nearby = await app.state.ground.nearby(body.latitude, body.longitude)
    if nearby is None:
        return ok_payload({"nearby": None})

    wind = None
    ident = nearby["airport"]["ident"]
    try:
        reports = await app.state.weather.latest_metars([ident])
        if reports and reports[0].wind and reports[0].wind.speed_kt is not None:
            wind = {
                "from_deg_true": reports[0].wind.direction_from_deg_true,
                "speed_kt": reports[0].wind.speed_kt,
                "gust_kt": reports[0].wind.gust_kt,
            }
    except Exception as exc:  # noqa: BLE001 â€” wind is an enhancement here
        log.info("ground-view METAR failed for %s: %s", ident, exc)

    runways_out = []
    for r in nearby["runways"]:
        entry = dict(r)
        # per-runway components against the published threshold heading
        for tag, head_key in (("le", "le_heading_t"), ("he", "he_heading_t")):
            heading = r[head_key]
            if heading is None or wind is None or wind.get("from_deg_true") is None:
                continue
            head_xw, cross_xw = _ground_wind_components(
                float(heading), float(wind["from_deg_true"]), float(wind["speed_kt"])
            )
            entry[f"{tag}_headwind_kt"] = round(head_xw, 1)
            entry[f"{tag}_crosswind_kt"] = round(abs(cross_xw), 1)
            entry[f"{tag}_crosswind_right"] = cross_xw >= 0
        # bearing from aircraft to each threshold helps orientation on the ramp
        if r["le_lat"] is not None:
            entry["bearing_to_le"] = round(_ground_initial_bearing(
                body.latitude, body.longitude, r["le_lat"], r["le_lon"]), 1)
        if r["he_lat"] is not None:
            entry["bearing_to_he"] = round(_ground_initial_bearing(
                body.latitude, body.longitude, r["he_lat"], r["he_lon"]), 1)
        runways_out.append(entry)

    return ok_payload({
        "nearby": {
            "airport": nearby["airport"],
            "distance_nm": nearby["distance_nm"],
            "runways": runways_out,
            "wind": wind,
        },
    })


@app.post("/api/terrain/profile")
async def post_terrain_profile(body: TerrainProfileRequest):
    result = await app.state.terrain.build_profile(body.waypoint_pairs(), body.cruise_altitude_ft)
    collision = len(result.breach_indices) > 0
    return ok_payload(
        {
            "profile": result,
            "collision_alert": collision,
            "alert_message": (
                "TERRAIN COLLISION RISK: planned cruise altitude intersects the 1,000 ft "
                "VFR safety ceiling over the marked sectors."
                if collision
                else None
            ),
        }
    )


@app.get("/api/aip/ad2/{icao}")
async def get_aip_ad2(icao: str, force_refresh: bool = False):
    snapshot = await app.state.enaire.get_ad2(icao, force_refresh=force_refresh)
    _index_reporting_points(snapshot, icao.upper())
    return ok_payload({"ad2": snapshot})


@app.post("/api/gps/ingest")
async def post_gps_ingest(body: GpsIngestRequest):
    accepted = sum(
        1
        for pos in body.positions
        if isinstance(pos, dict)
        and isinstance(pos.get("latitude"), (int, float))
        and isinstance(pos.get("longitude"), (int, float))
        and abs(float(pos["latitude"])) <= 90
        and abs(float(pos["longitude"])) <= 180
    )
    return ok_payload({"accepted": accepted, "stored": 0})


@app.get("/api/diagnostics/live")
async def diagnostics_live():
    results = {
        "noaa_metar": False,
        "winds_grid": False,
        "elevation_tiles": False,
        "magnetic_model": False,
    }

    async def probe(coro_factory, key: str) -> None:
        try:
            await coro_factory()
            results[key] = True
        except Exception as exc:  # noqa: BLE001
            log.info("diagnostic %s failed: %s", key, exc)

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:

        async def awc_probe():
            resp = await client.get(
                "https://aviationweather.gov/api/data/metar",
                params={"ids": "LEVC", "format": "json"},
            )
            resp.raise_for_status()

        async def dem_probe():
            resp = await client.head(
                TERRARIUM_PROBE_URL,
            )
            resp.raise_for_status()

        await probe(awc_probe, "noaa_metar")

        async def winds_probe():
            await app.state.winds.sample(39.49, -0.48, 5500)

        await probe(winds_probe, "winds_grid")
        await probe(dem_probe, "elevation_tiles")

    try:
        model = await load_world_magnetic_model(cache_dir=DATA_DIR)
        declination = model.declination_deg(39.489, -0.482, 0)
        results["magnetic_model"] = True
        results["declination_valencia_deg"] = round(declination, 3)
    except Exception as exc:  # noqa: BLE001
        log.info("diagnostic magnetic_model failed: %s", exc)

    return ok_payload({"diagnostics": results})


def _bbox_or_bad(value: str) -> tuple[float, float, float, float]:
    parts = value.split(",")
    if len(parts) != 4:
        raise BadRequest("bbox must be 'south,west,north,east' in decimal degrees")
    try:
        south, west, north, east = (float(p) for p in parts)
    except ValueError as exc:
        raise BadRequest("bbox values must be numeric") from exc
    if not (-90 <= south < north <= 90) or not (-180 <= west <= east <= 180):
        raise BadRequest("bbox out of geographic range")
    if north - south > 40 or east - west > 40:
        raise BadRequest("bbox too large; request a smaller region")
    return south, west, north, east


@app.get("/api/airports")
async def get_airports(bbox: str):
    south, west, north, east = _bbox_or_bad(bbox)
    airports = await app.state.airref.airports(south, west, north, east)
    return ok_payload({"airports": airports, "count": len(airports)})


@app.get("/api/navaids")
async def get_navaids(bbox: str):
    south, west, north, east = _bbox_or_bad(bbox)
    navaids = await app.state.airref.navaids(south, west, north, east)
    return ok_payload({"navaids": navaids, "count": len(navaids)})


@app.get("/api/charts/vfr/tile/{zoom}/{x}/{y}.png")
async def get_vfr_chart_tile(zoom: int, x: int, y: int):
    if zoom < 5 or zoom > 12:
        raise BadRequest("chart tiles are served for zoom 5..12")
    n = 2 ** zoom
    if not (0 <= x < n and 0 <= y < n):
        raise BadRequest("tile indices outside the pyramid")
    payload = await app.state.chart_proxy.tile_png(zoom, x, y)
    if payload is None:
        raise ApiError(status_code=502, code="chart_source_error",
                       message="ENAIRE chart service returned no image for this tile")
    from fastapi.responses import Response
    return Response(content=payload, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")),
    )
