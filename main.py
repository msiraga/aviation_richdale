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
from fastapi.responses import JSONResponse
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
                        f"reporting point '{rest}' is not indexed yet — open that airport's "
                        "VAC once, or use the ICAO:NAME form (e.g. LEVC:FOIOS)"
                    )
                resolved.append((LatLon(latitude=hit["latitude"], longitude=hit["longitude"]),
                                 f"RP {rest}"))
                continue
            if len(prefix) == 4:  # ICAO:NAME — load this field's chart on demand
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
                f"unknown prefix '{prefix}:' — use VOR:, NDB:, DME:, RP: or ICAO:NAME"
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
            # no live METAR station by that ident — fall through to navaids
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
            f"unresolved waypoint '{label}' — use an airport ICAO, navaid ident "
            "(VOR:/NDB:), RP:NAME / ICAO:NAME, or 'lat,lon'"
        )

    if len(resolved) < 2:
        raise BadRequest("a route needs at least two distinct points")
    return resolved


# Reporting points seen so far this process: NAME -> {icao, latitude, longitude}
# Populated opportunistically whenever an AD-2 snapshot is loaded — never by
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
        "ofm_tile_url": ofm_template or "",
        "map_center": DEFAULT_MAP_CENTER,
        "map_bbox_sw": DEFAULT_MAP_BBOX_SW,
        "map_bbox_ne": DEFAULT_MAP_BBOX_NE,
        "crosswind_limit_kt": CROSSWIND_LIMIT_KT,
        "safety_ceiling_ft": SAFETY_CEILING_FT,
        "build_id": f"b{datetime.fromtimestamp(build_stamp, tz=timezone.utc).strftime('%y%m%d.%H%M')}",
    }
    response = templates.TemplateResponse(request, "index.html", context)
    # the UI evolves fast — never let a browser serve yesterday's cockpit
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
        except Exception as exc:  # noqa: BLE001 — a dead grid level must not kill the ranking
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
    except Exception as exc:  # noqa: BLE001 — points are an enhancement
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
    """Live ADS-B positions (OpenSky, anonymous). Advisory only — see MANUAL."""
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
                return ok_payload({"aircraft": [], "note": "Rate limited by OpenSky — retrying later."})
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
        "note": "ADS-B coverage only — aircraft without transponders or below coverage are INVISIBLE.",
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
    except Exception as exc:  # noqa: BLE001 — ring must work offline too
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
            "note": "" if wind else "No METAR within range — still-air envelope shown.",
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
    except Exception as exc:  # noqa: BLE001 — wind is an enhancement here
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
