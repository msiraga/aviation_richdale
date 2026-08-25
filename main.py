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
from enaire import EnaireAipRepository, EnaireAd2Service, EnaireError
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


async def resolve_waypoints(entries: Sequence[str], engine: AviationWeatherEngine) -> list[tuple[LatLon, str]]:
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

        station = await engine.resolve_station(key)
        resolved.append(
            (LatLon(latitude=station.latitude, longitude=station.longitude), station.icao)
        )

    if len(resolved) < 2:
        raise BadRequest("a route needs at least two distinct points (ICAO or 'lat,lon')")
    return resolved


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
    context = {
        "request": request,
        "openaip_api_key": openaip_key or "",
        "ofm_tile_url": ofm_template or "",
        "map_center": DEFAULT_MAP_CENTER,
        "map_bbox_sw": DEFAULT_MAP_BBOX_SW,
        "map_bbox_ne": DEFAULT_MAP_BBOX_NE,
        "crosswind_limit_kt": CROSSWIND_LIMIT_KT,
        "safety_ceiling_ft": SAFETY_CEILING_FT,
    }
    return templates.TemplateResponse(request, "index.html", context)


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
    points = await resolve_waypoints(entries, engine)
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


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")),
    )
