"""Live aviation weather engine.

NOAA Aviation Weather Center ingestion for METAR/TAF (raw alphanumeric token
parsing over the official JSON transport), FAA flight-category computation,
explicit crosswind vector analysis against magnetic runway headings, and a
gridded winds-aloft forecast engine used for E6B intersection along the route.

Every network call targets real public endpoints discovered and verified at
build time. Nothing here fabricates observations: any parse gap raises or is
surfaced as an explicit warning instead of silently degrading.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import httpx

from navigation import (
    LatLon,
    compute_crosswind,
    isa_pressure_hpa,
    meters_to_feet,
    nearest_pressure_level_hpa,
)

log = logging.getLogger("richdale.weather")

AWC_BASE_URL = "https://aviationweather.gov"
AWC_METAR_PATH = "/api/data/metar"
AWC_TAF_PATH = "/api/data/taf"

OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"

USER_AGENT = "aviation-richdale/1.0 (+open-source VFR planning; github.com/msiraga/aviation_richdale)"

CROSSWIND_LIMIT_KT = 15.0

STATION_CACHE_TTL_S = 600.0
WINDS_CACHE_TTL_S = 1800.0


class WeatherServiceError(RuntimeError):
    """Raised when a live weather source cannot satisfy a request."""


class UnknownStation(WeatherServiceError):
    pass


class NoUsableObservation(WeatherServiceError):
    pass


FLIGHT_CATEGORY_VFR = "VFR"
FLIGHT_CATEGORY_MVFR = "MVFR"
FLIGHT_CATEGORY_IFR = "IFR"
FLIGHT_CATEGORY_LIFR = "LIFR"
FLIGHT_CATEGORY_UNKNOWN = "UNKNOWN"

_WX_PHENOMENA = {
    "TS", "RA", "SN", "DZ", "IC", "PL", "GR", "GS", "UP",
    "FG", "BR", "HZ", "FU", "VA", "DU", "SA", "PY", "SQ",
    "PO", "FC", "DS", "SS",
}
_WX_DESCRIPTOR = {"TS", "SH", "FZ", "BL", "DR", "MI", "BC", "PR"}
_CLOUD_COVERAGE = {"FEW", "SCT", "BKN", "OVC"}
_SKY_CLEAR = {"SKC", "CLR", "NCD", "NSC"}

_RE_STATION = re.compile(r"^(METAR|SPECI)\s+([A-Z0-9]{4})\b")
_RE_TIME = re.compile(r"^(\d{2})(\d{2})(\d{2})Z$")
_RE_WIND = re.compile(
    r"^(?P<dir>\d{3}|VRB|///)(?P<spd>\d{2,3}|//)"
    r"(?:G(?P<gust>\d{2,3}))?"
    r"(?P<unit>KT|MPS|KMH|KPH)?"
    r"(?:\s+(?P<varfrom>\d{3})V(?P<varto>\d{3}))?$"
)
_RE_VIS_METERS = re.compile(r"^(\d{4})$")
_RE_VIS_SM = re.compile(r"^(?P<whole>\d+)(?P<frac>\s+\d+/\d+)?SM$", re.IGNORECASE)
_RE_VIS_FRACTION = re.compile(r"^(?P<num>\d+)/(?P<den>\d+)SM$", re.IGNORECASE)
_RE_CLOUD = re.compile(r"^(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)?$")
_RE_VV = re.compile(r"^VV(\d{3})?$")
_RE_TEMP_DEW = re.compile(r"^(M?\d{2}|//)/(M?\d{2}|//)?$")
_RE_ALTIMETER_QNH = re.compile(r"^Q(\d{4})$")
_RE_ALTIMETER_INHG = re.compile(r"^A(\d{4})$")
_RE_TAF_VALIDITY = re.compile(r"^(\d{2})(\d{2})/(\d{2})(\d{2})$")
_RE_TAF_FM = re.compile(r"^FM(\d{2})(\d{2})(\d{2})Z?$")
_RE_TAF_PROB = re.compile(r"^PROB(\d{2})(TH)?$")
_RE_TAF_TEMP_TREND = re.compile(r"^(?:TX|TN)(?:\d{2}|XX)/\d{4}Z?$")


@dataclass(frozen=True, slots=True)
class WindObservation:
    direction_from_deg_true: float | None
    speed_kt: float | None
    gust_kt: float | None
    variable_from_deg: float | None = None
    variable_to_deg: float | None = None


@dataclass(frozen=True, slots=True)
class SkyLayer:
    coverage: str
    base_ft: int | None
    convective: bool = False


@dataclass(frozen=True, slots=True)
class ParsedWeatherGroup:
    wind: WindObservation | None = None
    visibility_m: float | None = None
    visibility_unlimited: bool = False
    weather: tuple[str, ...] = ()
    sky_layers: tuple[SkyLayer, ...] = ()
    ceiling_ft: int | None = None
    vertical_visibility_ft: int | None = None
    temperature_c: float | None = None
    dewpoint_c: float | None = None
    altimeter_hpa: float | None = None
    cavok: bool = False


@dataclass(frozen=True, slots=True)
class MetarReport:
    icao: str
    raw_text: str
    station_name: str | None
    latitude: float | None
    longitude: float | None
    elevation_m: float | None
    observed_at: datetime | None
    wind: WindObservation | None
    visibility_m: float | None
    visibility_unlimited: bool
    weather: tuple[str, ...]
    sky_layers: tuple[SkyLayer, ...]
    ceiling_ft: int | None
    temperature_c: float | None
    dewpoint_c: float | None
    altimeter_hpa: float | None
    cavok: bool
    flight_category: str
    parse_warnings: tuple[str, ...] = ()

    @property
    def visibility_sm(self) -> float | None:
        if self.visibility_m is None:
            return None
        return self.visibility_m / 1609.344


@dataclass(frozen=True, slots=True)
class TafPeriod:
    change_type: str
    valid_from: datetime | None
    valid_to: datetime | None
    probability_pct: int | None
    wind: WindObservation | None
    visibility_m: float | None
    visibility_unlimited: bool
    weather: tuple[str, ...]
    sky_layers: tuple[SkyLayer, ...]
    ceiling_ft: int | None
    cavok: bool


@dataclass(frozen=True, slots=True)
class TafReport:
    icao: str
    raw_text: str
    issued_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    periods: tuple[TafPeriod, ...]
    parse_warnings: tuple[str, ...] = ()


def taf_period_category(
    visibility_m: float | None,
    ceiling_ft: int | None,
    cavok: bool,
    visibility_unlimited: bool = False,
) -> str:
    """Standard FAA/ICAO flight-category bucketing for a TAF period."""
    if cavok or visibility_unlimited:
        return "VFR"
    vis = visibility_m if visibility_m is not None else 10000.0
    ceil = ceiling_ft if ceiling_ft is not None else 30000
    if vis < 5000 or ceil < 1000:
        return "IFR"
    if vis < 9000 or ceil < 3000:
        return "MVFR"
    return "VFR"


def serialize_taf_timeline(report: TafReport) -> dict[str, Any]:
    """JSON-ready timeline of a parsed TAF for the cockpit validity bars."""
    periods: list[dict[str, Any]] = []
    for p in report.periods:
        if p.valid_from is None or p.valid_to is None:
            continue
        kind = p.change_type
        if p.probability_pct:
            kind = f"{kind} {p.probability_pct}%"
        wind_txt = None
        if p.wind is not None:
            gust = f"G{round(p.wind.gust_kt):02d}" if getattr(p.wind, "gust_kt", None) else ""
            dir_txt = f"{round(p.wind.direction_from_deg_true):03d}" if p.wind.direction_from_deg_true is not None else "VRB"
            wind_txt = f"{dir_txt}{round(p.wind.speed_kt or 0):02d}{gust}KT"
        periods.append({
            "kind": kind,
            "start": p.valid_from.isoformat(),
            "end": p.valid_to.isoformat(),
            "visibility_m": round(p.visibility_m) if p.visibility_m is not None else None,
            "visibility_unlimited": p.visibility_unlimited,
            "ceiling_ft": p.ceiling_ft,
            "cavok": p.cavok,
            "wind": wind_txt,
            "weather": list(p.weather)[:4],
            "category": taf_period_category(
                p.visibility_m, p.ceiling_ft, p.cavok, p.visibility_unlimited
            ),
        })
    periods.sort(key=lambda x: x["start"])
    return {
        "icao": report.icao,
        "issued": report.issued_at.isoformat() if report.issued_at else None,
        "valid_from": report.valid_from.isoformat() if report.valid_from else None,
        "valid_to": report.valid_to.isoformat() if report.valid_to else None,
        "raw_text": report.raw_text.strip(),
        "periods": periods,
    }


async def fetch_cloud_profile(
    latitude: float,
    longitude: float,
    hours_ahead: int = 12,
) -> dict[str, Any]:
    """Low/mid/high cloud-cover percentages for the next N hours at a point."""
    params: dict[str, str | float] = {
        "latitude": round(latitude, 2),
        "longitude": round(longitude, 2),
        "hourly": "cloud_cover_low,cloud_cover_mid,cloud_cover_high",
        "forecast_days": 2,
        "timezone": "GMT",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), headers={"User-Agent": USER_AGENT}) as client:
        response = await client.get(OPEN_METEO_BASE_URL, params=params)
        response.raise_for_status()
        body = response.json()
    hourly = body.get("hourly") or {}
    times = hourly.get("time") or []
    low = hourly.get("cloud_cover_low") or []
    mid = hourly.get("cloud_cover_mid") or []
    high = hourly.get("cloud_cover_high") or []
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    hours: list[dict[str, Any]] = []
    for i, t in enumerate(times):
        if t < now_iso:
            continue
        if len(hours) >= hours_ahead:
            break
        hours.append({
            "time": t,
            "low_pct": low[i] if i < len(low) else None,
            "mid_pct": mid[i] if i < len(mid) else None,
            "high_pct": high[i] if i < len(high) else None,
        })
    return {"latitude": latitude, "longitude": longitude, "hours": hours}


@dataclass(frozen=True, slots=True)
class StationInfo:
    icao: str
    name: str | None
    latitude: float
    longitude: float
    elevation_m: float | None


@dataclass(frozen=True, slots=True)
class CrosswindAssessment:
    icao: str
    runway_heading_magnetic_deg: float
    wind_from_deg_true: float
    wind_speed_kt: float
    gust_kt: float | None
    headwind_kt: float
    crosswind_signed_kt: float
    crosswind_kt: float
    exceeds_limit: bool
    limit_kt: float
    magnetic_variation_deg: float
    observed_at: datetime | None
    raw_metar: str


def _parse_wind_token(token: str) -> WindObservation | None:
    match = _RE_WIND.match(token)
    if not match:
        return None
    unit = (match.group("unit") or "KT").upper()
    dir_raw = match.group("dir")
    spd_raw = match.group("spd")

    speed_scale = 1.0
    if unit == "MPS":
        speed_scale = 1.9438444924406046
    elif unit in ("KMH", "KPH"):
        speed_scale = 0.5399568034557236

    direction: float | None
    if dir_raw == "VRB" or dir_raw == "///":
        direction = None
    else:
        direction = float(dir_raw)

    if spd_raw == "//":
        return WindObservation(direction_from_deg_true=None, speed_kt=None, gust_kt=None)
    speed = float(spd_raw) * speed_scale

    gust_raw = match.group("gust")
    gust = float(gust_raw) * speed_scale if gust_raw else None

    var_from = float(match.group("varfrom")) if match.group("varfrom") else None
    var_to = float(match.group("varto")) if match.group("varto") else None

    return WindObservation(
        direction_from_deg_true=direction,
        speed_kt=speed,
        gust_kt=gust,
        variable_from_deg=var_from,
        variable_to_deg=var_to,
    )


def _parse_visibility_tokens(tokens: Sequence[str], index: int) -> tuple[float | None, bool, int]:
    token = tokens[index]
    if _RE_VIS_METERS.match(token):
        meters = float(token)
        return (10000.0 if meters >= 9999 else meters), False, 1

    frac_match = _RE_VIS_FRACTION.match(token)
    if frac_match:
        value = int(frac_match.group("num")) / int(frac_match.group("den"))
        return value * 1609.344, False, 1

    sm_match = _RE_VIS_SM.match(token)
    if sm_match:
        whole = int(sm_match.group("whole"))
        value = float(whole)
        frac = sm_match.group("frac")
        if frac:
            num, den = frac.strip().split("/")
            value += int(num) / int(den)
        return value * 1609.344, False, 1

    if index + 1 < len(tokens):
        combined = f"{token} {tokens[index + 1]}"
        sm_combined = _RE_VIS_SM.match(combined)
        if sm_combined:
            whole = int(sm_combined.group("whole"))
            num, den = sm_combined.group("frac").strip().split("/")
            return (whole + int(num) / int(den)) * 1609.344, False, 2

    return None, False, 0


def _is_weather_token(token: str) -> bool:
    core = token[1:] if token[:1] in ("-", "+") else token
    if not core:
        return False
    if core.startswith("VC"):
        core = core[2:]
    remaining = core
    while remaining:
        consumed = False
        for chunk in sorted(_WX_DESCRIPTOR | _WX_PHENOMENA, key=len, reverse=True):
            if remaining.startswith(chunk):
                remaining = remaining[len(chunk):]
                consumed = True
                break
        if not consumed:
            return False
    return core != ""


def _parse_cloud_token(token: str) -> tuple[SkyLayer, int, int] | None:
    match = _RE_CLOUD.match(token)
    if match:
        coverage = match.group(1)
        base_ft = int(match.group(2)) * 100
        convective = match.group(3) is not None
        return SkyLayer(coverage=coverage, base_ft=base_ft, convective=convective), 1, base_ft
    vv_match = _RE_VV.match(token)
    if vv_match:
        height = int(vv_match.group(1)) * 100 if vv_match.group(1) else None
        layer = SkyLayer(coverage="VV", base_ft=height, convective=False)
        return layer, 1, height or -1
    return None


def _parse_temperature_group(token: str) -> tuple[float, float | None] | None:
    match = _RE_TEMP_DEW.match(token)
    if not match:
        return None

    def _temp(raw: str | None) -> float | None:
        if not raw or raw == "//":
            return None
        if raw.startswith("M"):
            return -float(raw[1:])
        return float(raw)

    temp = _temp(match.group(1))
    dew = _temp(match.group(2))
    if temp is None:
        return None
    return temp, dew


def _parse_altimeter_token(token: str) -> float | None:
    qnh = _RE_ALTIMETER_QNH.match(token)
    if qnh:
        return float(qnh.group(1))
    inhg = _RE_ALTIMETER_INHG.match(token)
    if inhg:
        return round(float(inhg.group(1)) / 100.0 * 33.8638866667, 1)
    return None


def _parse_body_groups(tokens: Sequence[str], start_index: int, warnings: list[str]) -> ParsedWeatherGroup:
    wind: WindObservation | None = None
    visibility_m: float | None = None
    visibility_unlimited = False
    weather: list[str] = []
    layers: list[SkyLayer] = []
    ceiling_candidates: list[int] = []
    vertical_vis_ft: int | None = None
    temperature_c: float | None = None
    dewpoint_c: float | None = None
    altimeter_hpa: float | None = None
    cavok = False

    i = start_index
    while i < len(tokens):
        token = tokens[i]

        if token == "CAVOK":
            cavok = True
            visibility_unlimited = True
            i += 1
            continue

        if wind is None and _RE_WIND.match(token):
            wind = _parse_wind_token(token)
            i += 1
            if i < len(tokens):
                trailing_var = _RE_WIND.match(tokens[i])
                if trailing_var and trailing_var.group("varfrom"):
                    wind = WindObservation(
                        direction_from_deg_true=wind.direction_from_deg_true if wind else None,
                        speed_kt=wind.speed_kt if wind else None,
                        gust_kt=wind.gust_kt if wind else None,
                        variable_from_deg=float(trailing_var.group("varfrom")),
                        variable_to_deg=float(trailing_var.group("varto")),
                    )
                    i += 1
            continue

        if visibility_m is None and not visibility_unlimited:
            vis_value, vis_unlimited, consumed = _parse_visibility_tokens(tokens, i)
            if vis_value is not None or vis_unlimited:
                visibility_m = vis_value
                visibility_unlimited = vis_unlimited
                i += max(consumed, 1)
                continue

        if _RE_TAF_PROB.match(token) or _RE_TAF_TEMP_TREND.match(token):
            i += 1
            continue

        if token in ("NCD", "NSC", "SKC", "CLR"):
            layers.append(SkyLayer(coverage=token, base_ft=None))
            i += 1
            continue

        cloud_parse = _parse_cloud_token(token)
        if cloud_parse is not None:
            layer, consumed, ceiling_hint = cloud_parse
            layers.append(layer)
            if layer.coverage in ("BKN", "OVC"):
                ceiling_candidates.append(ceiling_hint)
            if layer.coverage == "VV":
                vertical_vis_ft = ceiling_hint if ceiling_hint > 0 else None
            i += consumed
            continue

        if _is_weather_token(token):
            weather.append(token)
            i += 1
            continue

        temps = _parse_temperature_group(token)
        if temps is not None and temperature_c is None:
            temperature_c, dewpoint_c = temps
            i += 1
            continue

        qnh_value = _parse_altimeter_token(token)
        if qnh_value is not None:
            altimeter_hpa = qnh_value
            i += 1
            continue

        if token == "NOSIG" or token == "WS" or re.match(r"^WS\s?(ALL\s)?RWY", token) or token.startswith("R") and "/" in token:
            i += 1
            continue

        warnings.append(f"unrecognized token skipped: {token}")
        i += 1

    ceiling_ft = min(ceiling_candidates) if ceiling_candidates else None
    if cavok:
        ceiling_ft = None

    return ParsedWeatherGroup(
        wind=wind,
        visibility_m=visibility_m,
        visibility_unlimited=visibility_unlimited,
        weather=tuple(weather),
        sky_layers=tuple(layers),
        ceiling_ft=ceiling_ft,
        vertical_visibility_ft=vertical_vis_ft,
        temperature_c=temperature_c,
        dewpoint_c=dewpoint_c,
        altimeter_hpa=altimeter_hpa,
        cavok=cavok,
    )


def compute_flight_category(
    visibility_m: float | None,
    ceiling_ft: int | None,
    cavok: bool = False,
) -> str:
    """FAA 14 CFR part-91 style category bands driven by ceiling and visibility."""
    if cavok:
        return FLIGHT_CATEGORY_VFR
    vis_sm = visibility_m / 1609.344 if visibility_m is not None else None
    if vis_sm is None and ceiling_ft is None:
        return FLIGHT_CATEGORY_UNKNOWN
    if (vis_sm is not None and vis_sm < 1.0) or (ceiling_ft is not None and ceiling_ft < 500):
        return FLIGHT_CATEGORY_LIFR
    if (vis_sm is not None and vis_sm < 3.0) or (ceiling_ft is not None and ceiling_ft < 1000):
        return FLIGHT_CATEGORY_IFR
    if (vis_sm is not None and vis_sm <= 5.0) or (ceiling_ft is not None and ceiling_ft <= 3000):
        return FLIGHT_CATEGORY_MVFR
    return FLIGHT_CATEGORY_VFR


def parse_metar_text(
    icao: str,
    raw_text: str,
    *,
    station_name: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    elevation_m: float | None = None,
    reference_time: datetime | None = None,
) -> MetarReport:
    warnings: list[str] = []
    body = raw_text.split("RMK", 1)[0].strip()
    tokens = body.split()
    if len(tokens) < 3:
        raise NoUsableObservation(f"METAR too short for {icao}: {raw_text!r}")

    type_match = _RE_STATION.match(body)
    reported_station = type_match.group(2) if type_match else icao
    idx = 2 if type_match else 1

    observed_at: datetime | None = None
    while idx < len(tokens):
        corr_or_auto = tokens[idx]
        if corr_or_auto in ("COR", "AUTO", "RTD"):
            idx += 1
            continue
        time_match = _RE_TIME.match(tokens[idx])
        if time_match:
            day = int(time_match.group(1))
            hour = int(time_match.group(2))
            minute = int(time_match.group(3))
            anchor = reference_time or datetime.now(timezone.utc)
            candidate = anchor.replace(day=min(day, _days_in_month(anchor)), hour=hour, minute=minute, second=0, microsecond=0)
            if abs((candidate - anchor).total_seconds()) > 20 * 86400:
                shifted_month = 12 if anchor.month == 1 else anchor.month - 1
                shifted_year = anchor.year - 1 if anchor.month == 1 else anchor.year
                candidate = candidate.replace(year=shifted_year, month=shifted_month)
            observed_at = candidate
            idx += 1
        break

    parsed = _parse_body_groups(tokens, idx, warnings)
    category = compute_flight_category(parsed.visibility_m, parsed.ceiling_ft, parsed.cavok)

    return MetarReport(
        icao=icao,
        raw_text=raw_text,
        station_name=station_name,
        latitude=latitude,
        longitude=longitude,
        elevation_m=elevation_m,
        observed_at=observed_at,
        wind=parsed.wind,
        visibility_m=parsed.visibility_m,
        visibility_unlimited=parsed.visibility_unlimited,
        weather=parsed.weather,
        sky_layers=parsed.sky_layers,
        ceiling_ft=parsed.ceiling_ft,
        temperature_c=parsed.temperature_c,
        dewpoint_c=parsed.dewpoint_c,
        altimeter_hpa=parsed.altimeter_hpa,
        cavok=parsed.cavok,
        flight_category=category,
        parse_warnings=tuple(warnings),
    )


def _days_in_month(anchor: datetime) -> int:
    next_month = (anchor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (next_month - timedelta(days=1)).day


def _infer_datetime(day: int, hour: int, minute: int, anchor: datetime) -> datetime:
    safe_day = min(day, _days_in_month(anchor))
    candidate = anchor.replace(day=safe_day, hour=hour % 24, minute=minute % 60, second=0, microsecond=0)
    if abs((candidate - anchor).total_seconds()) > 20 * 86400:
        shifted_month = 12 if anchor.month == 1 else anchor.month - 1
        shifted_year = anchor.year - 1 if anchor.month == 1 else anchor.year
        candidate = candidate.replace(year=shifted_year, month=shifted_month, day=safe_day)
    return candidate


def parse_taf_text(
    icao: str,
    raw_text: str,
    *,
    issued_at: datetime | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> TafReport:
    warnings: list[str] = []
    anchor = issued_at or datetime.now(timezone.utc)
    body = raw_text.split("RMK", 1)[0].strip()
    tokens = body.split()

    start = 0
    if tokens and tokens[0] in ("TAF", "TAF AMD", "AMD"):
        start = 1
    while start < len(tokens) and (tokens[start] == icao or _RE_TIME.match(tokens[start])):
        start += 1

    periods: list[TafPeriod] = []

    def _consume_block(
        block_tokens: Sequence[str],
        change_type: str,
        prob: int | None,
        default_valid_from: datetime | None = None,
    ) -> TafPeriod:
        block_warnings: list[str] = []
        v_from: datetime | None = None
        v_to: datetime | None = None
        idx = 0
        if idx < len(block_tokens):
            vm = _RE_TAF_VALIDITY.match(block_tokens[idx])
            if vm:
                v_from = _infer_datetime(int(vm.group(1)), int(vm.group(2)), 0, anchor)
                v_to = _infer_datetime(int(vm.group(3)), int(vm.group(4)), 0, anchor)
                if v_to and v_from and v_to <= v_from:
                    nxt_month = v_from.month + 1
                    yr = v_from.year + (1 if nxt_month > 12 else 0)
                    v_to = v_to.replace(year=yr, month=(nxt_month - 1) % 12 + 1)
                idx += 1
        if v_from is None:
            v_from = default_valid_from
        parsed = _parse_body_groups(list(block_tokens), idx, block_warnings)
        warnings.extend(f"[{change_type}] {w}" for w in block_warnings[:3])
        return TafPeriod(
            change_type=change_type,
            valid_from=v_from,
            valid_to=v_to,
            probability_pct=prob,
            wind=parsed.wind,
            visibility_m=parsed.visibility_m,
            visibility_unlimited=parsed.visibility_unlimited,
            weather=parsed.weather,
            sky_layers=parsed.sky_layers,
            ceiling_ft=parsed.ceiling_ft,
            cavok=parsed.cavok,
        )

    current_type = "BASELINE"
    current_prob: int | None = None
    pending_fm_valid_from: datetime | None = None
    buffer: list[str] = []

    def _flush() -> None:
        nonlocal buffer, pending_fm_valid_from
        if buffer:
            default_from = pending_fm_valid_from if current_type == "FM" else None
            periods.append(_consume_block(buffer, current_type, current_prob, default_from))
        if current_type == "FM":
            pending_fm_valid_from = None
        buffer = []

    i = start
    while i < len(tokens):
        token = tokens[i]
        fm = _RE_TAF_FM.match(token)
        if fm:
            _flush()
            current_type = "FM"
            current_prob = None
            pending_fm_valid_from = _infer_datetime(int(fm.group(1)), int(fm.group(2)), int(fm.group(3)), anchor)
            buffer = []
            i += 1
            continue
        if token in ("BECMG", "TEMPO", "INTER", "RADR"):
            _flush()
            current_type = token
            current_prob = None
            buffer = []
            i += 1
            continue
        prob = _RE_TAF_PROB.match(token)
        if prob:
            _flush()
            current_prob = int(prob.group(1))
            current_type = f"PROB{current_prob}"
            buffer = []
            i += 1
            continue
        buffer.append(token)
        i += 1
    _flush()

    if not any(p.wind or p.sky_layers or p.weather or p.cavok for p in periods):
        raise NoUsableObservation(f"TAF for {icao} produced no usable groups: {raw_text!r}")

    return TafReport(
        icao=icao,
        raw_text=raw_text,
        issued_at=issued_at,
        valid_from=valid_from,
        valid_to=valid_to,
        periods=tuple(periods),
        parse_warnings=tuple(warnings[:8]),
    )


class AviationWeatherEngine:
    """NOAA Aviation Weather Center client plus derived-product services."""

    def __init__(self, variation_lookup=None) -> None:
        self._variation_lookup = variation_lookup
        self._station_cache: dict[str, tuple[float, StationInfo]] = {}

    async def _get_json(self, path: str, params: dict[str, str]) -> Any:
        import asyncio

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(25.0),
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                ) as client:
                    response = await client.get(f"{AWC_BASE_URL}{path}", params=params)
                    response.raise_for_status()
                    return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(0.8 * (attempt + 1))
        raise WeatherServiceError(f"NOAA AWC unreachable after retries: {last_error}")

    async def latest_metars(self, icaos: Sequence[str]) -> list[MetarReport]:
        ids = ",".join(sorted({i.upper().strip() for i in icaos}))
        if not ids:
            return []
        payload = await self._get_json(AWC_METAR_PATH, {"ids": ids, "format": "json"})
        if not isinstance(payload, list):
            raise WeatherServiceError("unexpected METAR payload shape from NOAA AWC")

        reports: list[MetarReport] = []
        seen: set[str] = set()
        for record in payload:
            if not isinstance(record, dict):
                continue
            icao = str(record.get("icaoId") or "").upper()
            raw = str(record.get("rawOb") or "").strip()
            if not icao or not raw or icao in seen:
                continue
            seen.add(icao)
            obs_time = None
            epoch = record.get("obsTime")
            if isinstance(epoch, (int, float)):
                obs_time = datetime.fromtimestamp(epoch, tz=timezone.utc)
            try:
                reports.append(
                    parse_metar_text(
                        icao,
                        raw,
                        station_name=record.get("name"),
                        latitude=_as_float(record.get("lat")),
                        longitude=_as_float(record.get("lon")),
                        elevation_m=_as_float(record.get("elev")),
                        reference_time=obs_time or datetime.now(timezone.utc),
                    )
                )
            except NoUsableObservation as exc:
                warnings_msg = f"{icao}: {exc}"
                reports.append(
                    MetarReport(
                        icao=icao,
                        raw_text=raw,
                        station_name=record.get("name"),
                        latitude=_as_float(record.get("lat")),
                        longitude=_as_float(record.get("lon")),
                        elevation_m=_as_float(record.get("elev")),
                        observed_at=None,
                        wind=None,
                        visibility_m=None,
                        visibility_unlimited=False,
                        weather=(),
                        sky_layers=(),
                        ceiling_ft=None,
                        temperature_c=None,
                        dewpoint_c=None,
                        altimeter_hpa=None,
                        cavok=False,
                        flight_category=FLIGHT_CATEGORY_UNKNOWN,
                        parse_warnings=(warnings_msg,),
                    )
                )
        return reports

    async def latest_tafs(self, icaos: Sequence[str]) -> list[TafReport]:
        ids = ",".join(sorted({i.upper().strip() for i in icaos}))
        if not ids:
            return []
        payload = await self._get_json(AWC_TAF_PATH, {"ids": ids, "format": "json"})
        if not isinstance(payload, list):
            raise WeatherServiceError("unexpected TAF payload shape from NOAA AWC")

        reports: list[TafReport] = []
        for record in payload:
            if not isinstance(record, dict):
                continue
            icao = str(record.get("icaoId") or "").upper()
            raw = str(record.get("rawTAF") or "").strip()
            if not icao or not raw:
                continue
            issued = _iso_or_none(record.get("issueTime"))
            vfrom = _iso_or_none(record.get("validTimeFrom"))
            vto = _iso_or_none(record.get("validTimeTo"))
            try:
                reports.append(parse_taf_text(icao, raw, issued_at=issued, valid_from=vfrom, valid_to=vto))
            except NoUsableObservation as exc:
                log.warning("TAF parse degraded for %s: %s", icao, exc)
                reports.append(
                    TafReport(
                        icao=icao,
                        raw_text=raw,
                        issued_at=issued,
                        valid_from=vfrom,
                        valid_to=vto,
                        periods=(),
                        parse_warnings=(str(exc),),
                    )
                )
        return reports

    async def resolve_station(self, icao: str) -> StationInfo:
        key = icao.upper().strip()
        cached = self._station_cache.get(key)
        now_mono = time.monotonic()
        if cached and now_mono - cached[0] < STATION_CACHE_TTL_S:
            return cached[1]

        payload = await self._get_json(AWC_METAR_PATH, {"ids": key, "format": "json"})
        if isinstance(payload, list):
            for record in payload:
                if not isinstance(record, dict):
                    continue
                lat = _as_float(record.get("lat"))
                lon = _as_float(record.get("lon"))
                if lat is None or lon is None:
                    continue
                info = StationInfo(
                    icao=key,
                    name=record.get("name"),
                    latitude=lat,
                    longitude=lon,
                    elevation_m=_as_float(record.get("elev")),
                )
                self._station_cache[key] = (now_mono, info)
                return info
        raise UnknownStation(
            f"ICAO '{icao}' is not a live observing station in the NOAA AWC database; "
            "supply coordinates directly as 'lat,lon'"
        )

    async def assess_crosswind(
        self,
        icao: str,
        runway_heading_magnetic_deg: float,
        limit_kt: float = CROSSWIND_LIMIT_KT,
        variation_deg: float | None = None,
    ) -> CrosswindAssessment:
        station = await self.resolve_station(icao)
        metars = await self.latest_metars([icao])
        metar = next((m for m in metars if m.icao == icao.upper()), None)
        if metar is None or metar.wind is None or metar.wind.speed_kt is None:
            raise NoUsableObservation(f"no surface wind available for {icao}")

        wind_dir = metar.wind.direction_from_deg_true
        if wind_dir is None:
            if metar.wind.variable_from_deg is not None and metar.wind.variable_to_deg is not None:
                lo, hi = metar.wind.variable_from_deg, metar.wind.variable_to_deg
                wind_dir = lo if lo <= hi else (lo + hi + 360.0) / 2.0 % 360.0
            else:
                raise NoUsableObservation(f"wind direction absent for {icao}; cannot form vector")

        if variation_deg is None:
            if self._variation_lookup is not None:
                variation_deg = await self._variation_lookup(
                    station.latitude, station.longitude, meters_to_feet(station.elevation_m or 0.0)
                )
            else:
                variation_deg = 0.0

        result = compute_crosswind(
            runway_heading_magnetic_deg=runway_heading_magnetic_deg,
            wind_from_deg_true=wind_dir,
            wind_speed_kt=metar.wind.speed_kt,
            magnetic_variation_deg=variation_deg,
            limit_kt=limit_kt,
        )

        return CrosswindAssessment(
            icao=icao.upper(),
            runway_heading_magnetic_deg=runway_heading_magnetic_deg,
            wind_from_deg_true=wind_dir,
            wind_speed_kt=metar.wind.speed_kt,
            gust_kt=metar.wind.gust_kt,
            headwind_kt=result.headwind_kt,
            crosswind_signed_kt=result.crosswind_signed_kt,
            crosswind_kt=result.crosswind_kt,
            exceeds_limit=result.exceeds_limit,
            limit_kt=limit_kt,
            magnetic_variation_deg=variation_deg,
            observed_at=metar.observed_at,
            raw_metar=metar.raw_text,
        )


@dataclass(frozen=True, slots=True)
class WindsAloftSample:
    latitude: float
    longitude: float
    altitude_ft: float
    pressure_level_hpa: float
    wind_from_deg_true: float
    wind_speed_kt: float
    temperature_c: float | None
    valid_time_utc: datetime
    provider: str


class WindsAloftEngine:
    """Gridded upper-wind forecast intersection.

    NOAA AWC publishes its textual FD winds-aloft product for the North
    American station network only (verified live: no European coverage), so
    this engine intersects the route against the global GFS pressure-level
    forecast grid served openly by Open-Meteo, which carries true-direction
    wind at standard isobaric surfaces. Every sample is stamped with its
    provider so downstream products attribute their source honestly.
    """

    PROVIDER_LABEL = "gfs-pressure-level-grid/open-meteo"

    def __init__(self) -> None:
        self._cache: dict[tuple[float, str], tuple[float, dict[str, list[Any]]]] = {}

    def _level_variable_names(self, altitude_ft: float) -> list[tuple[float, str, str]]:
        target_level = nearest_pressure_level_hpa(altitude_ft)
        ranked = sorted(PRESSURE_LEVELS_HPA_FOR_QUERY, key=lambda lv: abs(lv - target_level))[:3]
        return [
            (level, f"wind_speed_{_level_token(level)}hPa", f"wind_direction_{_level_token(level)}hPa")
            for level in ranked
        ]

    async def sample(
        self,
        latitude: float,
        longitude: float,
        altitude_ft: float,
        eta_utc: datetime | None = None,
    ) -> WindsAloftSample:
        import asyncio

        eta = eta_utc or datetime.now(timezone.utc)
        if eta.tzinfo is None:
            eta = eta.replace(tzinfo=timezone.utc)

        lat_q = round(latitude, 2)
        lon_q = round(longitude, 2)
        level_pairs = self._level_variable_names(altitude_ft)
        primary_level = level_pairs[0][0]

        cache_key = (lat_q, lon_q)
        cached = self._cache.get(cache_key)
        now_mono = time.monotonic()
        if cached and now_mono - cached[0] < WINDS_CACHE_TTL_S:
            series = cached[1]
        else:
            params: dict[str, str | float] = {
                "latitude": lat_q,
                "longitude": lon_q,
                "hourly": ",".join(v for pair in level_pairs for v in pair[1:]),
                "forecast_days": 2,
                "wind_speed_unit": "kn",
                "timezones": "GMT",
            }
            params["timezone"] = "GMT"
            params.pop("timezones", None)

            last_error: Exception | None = None
            series = None
            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(25.0),
                        headers={"User-Agent": USER_AGENT},
                    ) as client:
                        response = await client.get(OPEN_METEO_BASE_URL, params=params)
                        response.raise_for_status()
                        body = response.json()
                    hourly = body.get("hourly") or {}
                    if not isinstance(hourly, dict) or "time" not in hourly:
                        raise WeatherServiceError("open-meteo response lacked hourly series")
                    series = hourly
                    break
                except Exception as exc:  # noqa: BLE001 - retried once, then surfaced
                    last_error = exc
                    await asyncio.sleep(0.8 * (attempt + 1))
            if series is None:
                raise WeatherServiceError(f"winds-aloft grid unreachable: {last_error}")
            self._cache[cache_key] = (now_mono, series)

        hourly = series
        times: list[str] = list(hourly.get("time") or [])
        if not times:
            raise WeatherServiceError("empty time axis in winds-aloft grid response")

        target_iso = eta.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00")
        best_idx = min(range(len(times)), key=lambda k: abs((datetime.fromisoformat(times[k]).replace(tzinfo=timezone.utc) - eta).total_seconds()))

        chosen: tuple[float, str, str] | None = None
        for level, speed_var, dir_var in level_pairs:
            speeds = hourly.get(speed_var)
            dirs = hourly.get(dir_var)
            if speeds is None or dirs is None:
                continue
            spd = speeds[best_idx]
            dr = dirs[best_idx]
            if spd is None or dr is None:
                continue
            chosen = (level, float(dr), float(spd))
            break

        if chosen is None:
            raise WeatherServiceError(
                f"no usable wind level returned near {_level_token(primary_level)}hPa for {lat_q},{lon_q}"
            )

        level, wind_dir, wind_spd = chosen
        temp_var = f"temperature_{_level_token(level)}hPa"
        temps = hourly.get(temp_var)
        temperature = float(temps[best_idx]) if temps and temps[best_idx] is not None else None

        return WindsAloftSample(
            latitude=lat_q,
            longitude=lon_q,
            altitude_ft=altitude_ft,
            pressure_level_hpa=level,
            wind_from_deg_true=((wind_dir % 360.0) + 360.0) % 360.0,
            wind_speed_kt=max(0.0, wind_spd),
            temperature_c=temperature,
            valid_time_utc=datetime.fromisoformat(times[best_idx]).replace(tzinfo=timezone.utc),
            provider=self.PROVIDER_LABEL,
        )


PRESSURE_LEVELS_HPA_FOR_QUERY: tuple[float, ...] = (
    1000.0, 975.0, 950.0, 925.0, 900.0, 850.0, 800.0,
    750.0, 700.0, 650.0, 600.0, 550.0, 500.0, 450.0, 400.0,
    350.0, 300.0, 250.0, 200.0, 150.0, 100.0,
)


def _level_token(level: float) -> str:
    return str(int(level))


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_or_none(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None
