"""Open aviation reference data: airports, navigation aids, ENAIRE chart tiles.

Airports and navaids come from the OurAirports project database (public
domain), downloaded once and cached on disk with a refresh window. The ENAIRE
Insignia VFR chart is served as an ArcGIS dynamic MapServer; a tile proxy
converts slippy-map tiles into bbox export requests so any XYZ client can
display the official Spanish VFR chart without a key.
"""

import asyncio
import csv
import io
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

OURAIRPORTS_BASE = "https://davidmegginson.github.io/ourairports-data"
ENAIRE_VFR_EXPORT_URL = (
    "https://servais.enaire.es/insignias/rest/services/INSIGNIA_VFR/"
    "InsigniaVFR_VIGOR_Aispace_v2/MapServer/export"
)
USER_AGENT = "aviation-richdale/1.0 (open-source VFR planning)"
CACHE_REFRESH_SECONDS = 30 * 24 * 3600.0


class AirDataError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AirportRecord:
    ident: str
    name: str
    kind: str
    latitude_deg: float
    longitude_deg: float
    elevation_ft: int
    country: str
    municipality: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ident": self.ident,
            "name": self.name,
            "type": self.kind,
            "latitude": self.latitude_deg,
            "longitude": self.longitude_deg,
            "elevation_ft": self.elevation_ft,
            "country": self.country,
            "municipality": self.municipality,
        }


@dataclass(frozen=True, slots=True)
class NavaidRecord:
    ident: str
    name: str
    kind: str
    frequency_khz: float | None
    latitude_deg: float
    longitude_deg: float
    elevation_ft: int
    country: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ident": self.ident,
            "name": self.name,
            "type": self.kind,
            "frequency_khz": self.frequency_khz,
            "latitude": self.latitude_deg,
            "longitude": self.longitude_deg,
            "elevation_ft": self.elevation_ft,
            "country": self.country,
        }


def _csv_cache_path(cache_dir: Path, name: str) -> Path:
    return cache_dir / name


async def _ensure_csv(cache_dir: Path, filename: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = _csv_cache_path(cache_dir, filename)
    if target.exists() and time.time() - target.stat().st_mtime < CACHE_REFRESH_SECONDS:
        return target

    def _download() -> None:
        tmp = target.with_suffix(".part")
        with httpx.Client(timeout=httpx.Timeout(120.0), headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            with client.stream("GET", f"{OURAIRPORTS_BASE}/{filename}") as response:
                response.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in response.iter_bytes(65536):
                        fh.write(chunk)
        tmp.replace(target)

    await asyncio.to_thread(_download)
    return target


def _parse_float(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def _load_airports(cache_dir: Path) -> list[AirportRecord]:
    path = await _ensure_csv(cache_dir, "airports.csv")

    def _parse() -> list[AirportRecord]:
        records: list[AirportRecord] = []
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                lat = _parse_float(row.get("latitude_deg", ""))
                lon = _parse_float(row.get("longitude_deg", ""))
                if lat is None or lon is None:
                    continue
                kind = row.get("type", "")
                if kind in {"balloonport", "closed"}:
                    continue
                elev = _parse_float(row.get("elevation_ft", ""))
                records.append(AirportRecord(
                    ident=row.get("ident", ""),
                    name=row.get("name", ""),
                    kind=kind,
                    latitude_deg=lat,
                    longitude_deg=lon,
                    elevation_ft=int(elev) if elev is not None else 0,
                    country=row.get("iso_country", ""),
                    municipality=row.get("municipality", ""),
                ))
        return records

    return await asyncio.to_thread(_parse)


async def _load_navaids(cache_dir: Path) -> list[NavaidRecord]:
    path = await _ensure_csv(cache_dir, "navaids.csv")

    def _parse() -> list[NavaidRecord]:
        records: list[NavaidRecord] = []
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                lat = _parse_float(row.get("latitude_deg", ""))
                lon = _parse_float(row.get("longitude_deg", ""))
                if lat is None or lon is None:
                    continue
                kind = row.get("type", "")
                if kind not in {"VOR", "VORTAC", "VOR-DME", "DME", "NDB", "NDB-DME", "TACAN"}:
                    continue
                freq = _parse_float(row.get("frequency_khz", ""))
                records.append(NavaidRecord(
                    ident=row.get("ident", ""),
                    name=row.get("name", ""),
                    kind=kind,
                    frequency_khz=freq,
                    latitude_deg=lat,
                    longitude_deg=lon,
                    elevation_ft=int(_parse_float(row.get("elevation_ft", "")) or 0),
                    country=row.get("iso_country", ""),
                ))
        return records

    return await asyncio.to_thread(_parse)


async def _load_runways(cache_dir: Path) -> list[dict[str, Any]]:
    """Runway geometry (thresholds, headings, dimensions) from OurAirports."""
    path = await _ensure_csv(cache_dir, "runways.csv")

    def _parse() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("closed") == "1":
                    continue
                length_ft = _parse_float(row.get("length_ft", "")) or 0.0
                if length_ft < 500:
                    continue
                rows.append({
                    "airport_ident": row.get("airport_ident", ""),
                    "le_ident": row.get("le_ident") or "",
                    "he_ident": row.get("he_ident") or "",
                    "length_m": round(length_ft * 0.3048),
                    "width_m": round((_parse_float(row.get("width_ft", "")) or 0.0) * 0.3048),
                    "surface": row.get("surface") or "",
                    "lighted": row.get("lighted") == "1",
                    "le_lat": _parse_float(row.get("le_latitude_deg", "")),
                    "le_lon": _parse_float(row.get("le_longitude_deg", "")),
                    "he_lat": _parse_float(row.get("he_latitude_deg", "")),
                    "he_lon": _parse_float(row.get("he_longitude_deg", "")),
                    "le_heading_t": _parse_float(row.get("le_heading_degT", "")),
                    "he_heading_t": _parse_float(row.get("he_heading_degT", "")),
                })
        return rows

    return await asyncio.to_thread(_parse)


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math as _m
    p1, p2 = _m.radians(lat1), _m.radians(lat2)
    dp = p2 - p1
    dl = _m.radians(lon2 - lon1)
    a = _m.sin(dp / 2) ** 2 + _m.cos(p1) * _m.cos(p2) * _m.sin(dl / 2) ** 2
    return 3440.065 * 2 * _m.atan2(_m.sqrt(a), _m.sqrt(1 - a))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math as _m
    p1, p2 = _m.radians(lat1), _m.radians(lat2)
    dl = _m.radians(lon2 - lon1)
    y = _m.sin(dl) * _m.cos(p2)
    x = _m.cos(p1) * _m.sin(p2) - _m.sin(p1) * _m.cos(p2) * _m.cos(dl)
    return (_m.degrees(_m.atan2(y, x)) + 360.0) % 360.0


def wind_components(aircraft_heading_deg: float, wind_from_deg: float, wind_speed_kt: float) -> tuple[float, float]:
    """Return (headwind_kt, crosswind_kt_signed_right)."""
    import math as _m
    angle = _m.radians(wind_from_deg - aircraft_heading_deg)
    return (
        wind_speed_kt * _m.cos(angle),
        wind_speed_kt * _m.sin(angle),
    )


class GroundReferenceService:
    """Nearest-airport runway picture with live wind components for taxi phase."""

    NEAR_NM = 6.0

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._lock = asyncio.Lock()

    async def nearby(self, lat: float, lon: float) -> dict[str, Any] | None:
        async with self._lock:
            airports = await _load_airports(self.cache_dir)
            runways = await _load_runways(self.cache_dir)

        by_ident: dict[str, list[dict[str, Any]]] = {}
        for r in runways:
            by_ident.setdefault(r["airport_ident"], []).append(r)
        usable = {i for i, rwys in by_ident.items() if any(r["length_m"] >= 600 for r in rwys)}

        best: AirportRecord | None = None
        best_d = self.NEAR_NM
        for a in airports:
            if a.ident not in usable:
                continue
            d = haversine_nm(lat, lon, a.latitude_deg, a.longitude_deg)
            if d < best_d:
                best_d, best = d, a

        if best is None:
            return None

        rwys = [r for r in by_ident.get(best.ident, []) if r["le_lat"] is not None or r["le_heading_t"] is not None]
        rwys.sort(key=lambda r: -(r["length_m"]))
        return {
            "airport": best.to_dict(),
            "distance_nm": round(best_d, 2),
            "runways": rwys[:8],
        }

    async def nearby_far(self, lat: float, lon: float, nm: float) -> list[dict[str, Any]] | None:
        """All paved-runway fields within nm, nearest first (Guardian search set)."""
        async with self._lock:
            airports = await _load_airports(self.cache_dir)
            runways = await _load_runways(self.cache_dir)

        by_ident: dict[str, list[dict[str, Any]]] = {}
        for r in runways:
            by_ident.setdefault(r["airport_ident"], []).append(r)
        usable = {i for i, rwys in by_ident.items() if any(r["length_m"] >= 600 for r in rwys)}

        out: list[tuple[float, AirportRecord]] = []
        for a in airports:
            if a.ident not in usable:
                continue
            d = haversine_nm(lat, lon, a.latitude_deg, a.longitude_deg)
            if d <= nm:
                out.append((d, a))
        out.sort(key=lambda t: t[0])

        entries: list[dict[str, Any]] = []
        for d, a in out[:12]:
            rwys = [r for r in by_ident.get(a.ident, []) if r["le_lat"] is not None or r["le_heading_t"] is not None]
            rwys.sort(key=lambda r: -(r["length_m"]))
            entries.append({"airport": a.to_dict(), "distance_nm": round(d, 2), "runways": rwys[:6]})
        return entries or None


class AirReferenceService:
    """Bbox-filtered airport and navaid lookups over the cached datasets."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._lock = asyncio.Lock()

    async def airports(self, south: float, west: float, north: float, east: float) -> list[dict[str, Any]]:
        async with self._lock:
            records = await _load_airports(self.cache_dir)
        return [
            r.to_dict() for r in records
            if south <= r.latitude_deg <= north and west <= r.longitude_deg <= east
        ]

    async def navaids(self, south: float, west: float, north: float, east: float) -> list[dict[str, Any]]:
        async with self._lock:
            records = await _load_navaids(self.cache_dir)
        return [
            r.to_dict() for r in records
            if south <= r.latitude_deg <= north and west <= r.longitude_deg <= east
        ]

    async def airport_by_ident(self, ident: str) -> dict[str, Any] | None:
        """Local OurAirports lookup so airport waypoints never need NOAA."""
        async with self._lock:
            records = await _load_airports(self.cache_dir)
        key = ident.strip().upper()
        for r in records:
            if r.ident.upper() == key:
                d = r.to_dict()
                d["candidates"] = 1
                return d
        return None

    async def navaid_by_ident(
        self, ident: str, kind_prefix: str | None = None
    ) -> dict[str, Any] | None:
        """Exact-ident navaid lookup (VOR/NDB/DME...).

        Spanish stations win ties deterministically; the returned dict carries
        ``candidates`` so callers can disclose ambiguity instead of hiding it.
        """
        async with self._lock:
            records = await _load_navaids(self.cache_dir)
        key = ident.strip().upper()
        matches = [r.to_dict() for r in records if r.ident.strip().upper() == key]
        if kind_prefix:
            prefix = kind_prefix.strip().upper()
            matches = [r for r in matches if str(r.get("type", "")).upper().startswith(prefix)]
        if not matches:
            return None
        matches.sort(key=lambda r: (str(r.get("country", "")) != "ES", str(r.get("name", ""))))
        best = dict(matches[0])
        best["candidates"] = len(matches)
        return best


# --------------------------- ENAIRE VFR tile proxy ---------------------------

TILE_SIZE = 512  # request 512px exports so labels stay legible


def tile_bbox(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Web-Mercator bbox [xmin, ymin, xmax, ymax] (EPSG:3857 meters) of a tile."""
    n = 2 ** zoom
    left = x / n * 360.0 - 180.0
    right = (x + 1) / n * 360.0 - 180.0

    def lat_of(row: float) -> float:
        nn = math.pi - 2.0 * math.pi * row / n
        return math.degrees(math.atan(0.5 * (math.exp(nn) - math.exp(-nn))))

    top = lat_of(y)
    bottom = lat_of(y + 1)

    def merc_x(lon_deg: float) -> float:
        return lon_deg * 20037508.342789244 / 180.0

    def merc_y(lat_deg: float) -> float:
        lat = max(-85.05112878, min(85.05112878, lat_deg))
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 6378137.0

    xmin = merc_x(left)
    xmax = merc_x(right)
    ymax = merc_y(top)
    ymin = merc_y(bottom)
    return xmin, ymin, xmax, ymax


class EnaireChartTileProxy:
    """Serve the official Insignia VFR chart layer as plain XYZ PNG tiles."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir / "enaire_chart_tiles"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._semaphore = __import__("asyncio").Semaphore(4)

    def _cache_path(self, zoom: int, x: int, y: int) -> Path:
        return self.cache_dir / f"{zoom}_{x}_{y}.png"

    async def tile_png(self, zoom: int, x: int, y: int) -> bytes | None:
        cached = self._cache_path(zoom, x, y)
        if cached.exists():
            return cached.read_bytes()

        xmin, ymin, xmax, ymax = tile_bbox(zoom, x, y)
        params = {
            "bbox": f"{xmin:.1f},{ymin:.1f},{xmax:.1f},{ymax:.1f}",
            "bboxSR": "3857",
            "imageSR": "3857",
            "size": f"{TILE_SIZE},{TILE_SIZE}",
            "format": "png32",
            "transparent": "true",
            "dpi": "96",
            "f": "image",
        }
        headers = {"User-Agent": USER_AGENT}
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45.0), headers=headers, follow_redirects=True) as client:
                try:
                    response = await client.get(ENAIRE_VFR_EXPORT_URL, params=params)
                    response.raise_for_status()
                except httpx.HTTPError:
                    return None

        payload = response.content
        if len(payload) < 100 or not payload.startswith(b"\x89PNG"):
            return None
        await asyncio.to_thread(cached.write_bytes, payload)
        return payload


def summarize_datasets(cache_dir: Path) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for name in ("airports.csv", "navaids.csv"):
        p = _csv_cache_path(cache_dir, name)
        info[name.replace(".csv", "")] = (
            {"cached": True, "bytes": p.stat().st_size, "age_hours": round((time.time() - p.stat().st_mtime) / 3600.0, 1)}
            if p.exists() else {"cached": False}
        )
    return info
