"""Terrain awareness engine.

Extracts real digital-elevation samples along the flight track from public
NASA SRTM-derived sources and evaluates them against the pilot's planned
cruise altitude with a 1,000 ft VFR safety ceiling.

Provider chain (first configured provider wins):
  1. OpenTopography global DEM API  -> genuine NASA SRTM GL3 (requires free
     operator API key via OT_API_KEY).
  2. Raw SRTM .hgt mirror           -> binary 16-bit big-endian height grids
     (enable via SRTM_BASE_URL pointing at any .hgt repository).
  3. AWS Open Data terrain tiles    -> terrarium-encoded raster tiles whose
     landmass layer is derived from NASA SRTM; keyless and always available,
     so this doubles as the default provider.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import struct
import zlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import httpx

from navigation import (
    LatLon,
    great_circle_points,
    haversine_distance_nm,
    initial_true_bearing_deg,
    meters_to_feet,
    perpendicular_offset_point,
)

log = logging.getLogger("richdale.terrain")

TERRARIUM_TILE_TEMPLATE = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
OPENTOPOGRAPHY_GLOBALDEM_URL = "https://portal.opentopography.org/API/globaldem"

SAFETY_CEILING_FT = 1000.0
CORRIDOR_LANE_OFFSET_NM = 0.5
HTTP_USER_AGENT = "aviation-richdale/1.0 (+open-source VFR planning)"


class TerrainServiceError(RuntimeError):
    pass


def _floor_lat(lat: float) -> int:
    return math.floor(lat)


def _hgt_tile_name(lat: float, lon: float) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(_floor_lat(lat)):02d}{ew}{abs(_floor_lon(lon)):03d}.hgt"


def _floor_lon(lon: float) -> int:
    return int(math.floor(lon))


def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(-85.05112878, min(85.05112878, lat)))
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def choose_terrarium_zoom(total_distance_nm: float) -> int:
    """Pick a zoom level that keeps tile fetches proportional to route length."""
    if total_distance_nm <= 25:
        return 13
    if total_distance_nm <= 60:
        return 12
    if total_distance_nm <= 150:
        return 11
    return 10


class DemProvider:
    name: str = "abstract"

    async def elevation_m(self, client: httpx.AsyncClient, lat: float, lon: float) -> float | None:
        raise NotImplementedError


class OpenTopographySrtmProvider(DemProvider):
    """Genuine NASA SRTM GL3 via the OpenTopography global DEM API (ASCII grid)."""

    name = "nasa-srtmgl3/opentopography"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def elevation_m(self, client: httpx.AsyncClient, lat: float, lon: float) -> float | None:
        params = {
            "demtype": "SRTMGL3",
            "south": f"{lat - 0.0005:.6f}",
            "north": f"{lat + 0.0005:.6f}",
            "west": f"{lon - 0.0005:.6f}",
            "east": f"{lon + 0.0005:.6f}",
            "outputFormat": "AAIGrid",
            "API_Key": self.api_key,
        }
        response = await client.get(OPENTOPOGRAPHY_GLOBALDEM_URL, params=params)
        response.raise_for_status()
        text = response.text.splitlines()
        header_size = 0
        for line in text:
            parts = line.split()
            if len(parts) == 6 and not _is_number(parts[0]):
                break
            header_size += 1
        grid_rows = [line.split() for line in text[header_size:] if line.strip()]
        if len(grid_rows) < 2 or len(grid_rows[1]) < 1:
            raise TerrainServiceError("OpenTopography returned an unreadable ASCII grid")
        value = grid_rows[1][0]
        return None if _is_void(value) else float(value)


class SrtmHgtProvider(DemProvider):
    """Raw Shuttle Radar Topography Mission height files (.hgt)."""

    name = "nasa-srtm-hgt-mirror"

    def __init__(self, base_url: str, cache_dir: Path) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._grid_cache: dict[str, tuple[int, list[int]]] = {}

    def _grid_path(self, tile: str) -> Path:
        return self.cache_dir / tile

    async def _load_grid(self, client: httpx.AsyncClient, tile: str) -> tuple[int, list[int]]:
        cached = self._grid_cache.get(tile)
        if cached:
            return cached
        local = self._grid_path(tile)
        if not local.exists() or local.stat().st_size < 4:
            url = f"{self.base_url}{tile}"
            response = await client.get(url)
            response.raise_for_status()
            payload = response.content
            local.write_bytes(payload)
        else:
            payload = local.read_bytes()

        side = int(round(math.sqrt(len(payload) / 2)))
        if side * side * 2 != len(payload) or side not in (1201, 1801, 3601, 7201):
            raise TerrainServiceError(f"unexpected .hgt geometry for {tile}: {len(payload)} bytes")
        values = list(struct.unpack(f">{side * side}h", payload))
        result = (side, values)
        self._grid_cache[tile] = result
        return result

    async def elevation_m(self, client: httpx.AsyncClient, lat: float, lon: float) -> float | None:
        tile = _hgt_tile_name(lat, lon)
        try:
            side, values = await self._load_grid(client, tile)
        except httpx.HTTPStatusError as exc:
            log.warning("hgt mirror miss for %s: %s", tile, exc.response.status_code)
            return None

        origin_lat = _tile_origin_lat(tile)
        origin_lon = _tile_origin_lon(tile)
        fx = (lon - origin_lon) * (side - 1)
        fy = (origin_lat + 1.0 - lat) * (side - 1)
        return _bilinear(values, side, fx, fy)


class TerrariumTileProvider(DemProvider):
    """AWS Open Data terrain tiles in terrarium encoding (SRTM-derived landmass)."""

    name = "terrarium-terrain-tiles/aws-open-data"

    def __init__(self, template: str = TERRARIUM_TILE_TEMPLATE, cache_dir: Path | None = None) -> None:
        self.template = template
        self.cache_dir = cache_dir
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self._png_cache: dict[tuple[int, int, int], bytes] = {}
        self._decoded_cache: dict[tuple[int, int, int], tuple[int, int, bytearray]] = {}

    def _cache_path(self, z: int, x: int, y: int) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"terrarium_{z}_{x}_{y}.png"

    async def _fetch_png(self, client: httpx.AsyncClient, z: int, x: int, y: int) -> bytes:
        cached_png = self._png_cache.get((z, x, y))
        if cached_png:
            return cached_png
        path = self._cache_path(z, x, y)
        if path is not None and path.exists():
            payload = path.read_bytes()
        else:
            response = await client.get(self.template.format(z=z, x=x, y=y))
            response.raise_for_status()
            payload = response.content
            if path is not None:
                path.write_bytes(payload)
        self._png_cache[(z, x, y)] = payload
        return payload

    def _decoded(self, z: int, x: int, y: int, png_bytes: bytes) -> tuple[int, int, bytearray]:
        cached = self._decoded_cache.get((z, x, y))
        if cached is not None:
            return cached
        decoded = _decode_png_rgb(png_bytes)
        self._decoded_cache[(z, x, y)] = decoded
        return decoded

    async def sample(
        self,
        client: httpx.AsyncClient,
        lat: float,
        lon: float,
        zoom: int,
    ) -> float | None:
        n = 2 ** zoom
        world_x = (lon + 180.0) / 360.0 * n
        lat_rad = math.radians(lat)
        world_y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n

        base_tile_x = min(n - 1, max(0, int(world_x)))
        base_tile_y = min(n - 1, max(0, int(world_y)))
        try:
            base_png = await self._fetch_png(client, zoom, base_tile_x, base_tile_y)
            tile_w, tile_h, _data = self._decoded(zoom, base_tile_x, base_tile_y, base_png)
        except (httpx.HTTPError, TerrainServiceError):
            return None
        if tile_w <= 0 or tile_h <= 0:
            return None

        px = world_x * tile_w
        py = world_y * tile_h
        gx = int(math.floor(px))
        gy = int(math.floor(py))
        fx = px - gx
        fy = py - gy

        corners: dict[tuple[int, int], float | None] = {}
        for dx in (0, 1):
            for dy in (0, 1):
                tx = gx + dx
                ty = gy + dy
                tile_x = ((tx // tile_w) % n)
                tile_y = ty // tile_h
                if tile_y < 0 or tile_y >= n:
                    corners[(dx, dy)] = None
                    continue
                try:
                    png = await self._fetch_png(client, zoom, tile_x, tile_y)
                    w, h, data = self._decoded(zoom, tile_x, tile_y, png)
                except httpx.HTTPError:
                    corners[(dx, dy)] = None
                    continue
                inner_x = tx % tile_w
                inner_y = ty % tile_h
                corners[(dx, dy)] = _terrarium_pixel(w, h, data, inner_x, inner_y)

        tl = corners[(0, 0)]
        tr = corners[(1, 0)]
        bl = corners[(0, 1)]
        br = corners[(1, 1)]
        known = [v for v in (tl, tr, bl, br) if v is not None]
        if not known:
            return None
        top = _lerp_nullable(tl, tr, fx)
        bottom = _lerp_nullable(bl, br, fx)
        if top is None:
            top = bottom
        if bottom is None:
            bottom = top
        if top is None or bottom is None:
            return known[0]
        return top * (1.0 - fy) + bottom * fy

    async def elevation_m(self, client: httpx.AsyncClient, lat: float, lon: float) -> float | None:
        return await self.sample(client, lat, lon, 11)


def _lerp_nullable(a: float | None, b: float | None, t: float) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return a * (1.0 - t) + b * t


def _terrarium_pixel(width: int, height: int, data: bytearray, x: int, y: int) -> float | None:
    if x < 0 or y < 0 or x >= width or y >= height:
        return None
    offset = (y * width + x) * 3
    r = data[offset]
    g = data[offset + 1]
    b = data[offset + 2]
    return (r * 256 + g + b / 256.0) - 32768.0


def _decode_png_rgb(data: bytes) -> tuple[int, int, bytearray]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise TerrainServiceError("not a PNG stream")

    pos = 8
    width = height = 0
    bit_depth = color_type = None
    idat = bytearray()
    palette: bytes | None = None

    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + length]
        pos += 12 + length

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk_data[:10])
        elif chunk_type == b"PLTE":
            palette = chunk_data
        elif chunk_type == b"IDAT":
            idat.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    if width == 0 or height == 0:
        raise TerrainServiceError("PNG header missing dimensions")
    if bit_depth != 8 or color_type not in (2, 3, 6):
        raise TerrainServiceError(
            f"unsupported PNG layout: depth={bit_depth} color={color_type}"
        )
    channels = {2: 3, 3: 1, 6: 4}[color_type]

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray()
    prev_row = bytearray(stride)
    src_pos = 0
    for _ in range(height):
        filter_byte = raw[src_pos]
        src_pos += 1
        row = bytearray(raw[src_pos:src_pos + stride])
        src_pos += stride

        if filter_byte == 0:
            pass
        elif filter_byte == 1:
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 0xFF
        elif filter_byte == 2:
            for i in range(stride):
                row[i] = (row[i] + prev_row[i]) & 0xFF
        elif filter_byte == 3:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + ((left + prev_row[i]) >> 1)) & 0xFF
        elif filter_byte == 4:
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                b = prev_row[i]
                c = prev_row[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + predictor) & 0xFF
        else:
            raise TerrainServiceError(f"unknown PNG filter {filter_byte}")

        out.extend(row)
        prev_row = row

    if color_type == 3:
        if palette is None:
            raise TerrainServiceError("palette PNG without PLTE")
        rgb = bytearray()
        for idx in out:
            base = idx * 3
            rgb.extend(palette[base:base + 3])
        return width, height, list(rgb)

    if color_type == 6:
        rgb = bytearray()
        for base in range(0, len(out), 4):
            rgb.extend(out[base:base + 3])
        return width, height, list(rgb)

    return width, height, list(out)


def _is_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def _is_void(token: str) -> bool:
    try:
        return float(token) <= -32000.0
    except ValueError:
        return True


def _tile_origin_lat(tile: str) -> float:
    lat_part = int(tile[1:3])
    return float(lat_part) if tile[0] == "N" else -float(lat_part)


def _tile_origin_lon(tile: str) -> float:
    lon_part = int(tile[4:7])
    return float(lon_part) if tile[3] == "E" else -float(lon_part)


def _bilinear(values: list[int], side: int, fx: float, fy: float) -> float | None:
    x0 = int(math.floor(fx))
    y0 = int(math.floor(fy))
    tx = fx - x0
    ty = fy - y0

    def at(x: int, y: int) -> float | None:
        cx = max(0, min(side - 1, x))
        cy = max(0, min(side - 1, y))
        v = values[cy * side + cx]
        return None if v <= -32768 else float(v)

    tl = at(x0, y0)
    tr = at(x0 + 1, y0)
    bl = at(x0, y0 + 1)
    br = at(x0 + 1, y0 + 1)
    top = _lerp_nullable(tl, tr, tx)
    bottom = _lerp_nullable(bl, br, tx)
    if top is None:
        top = bottom
    if bottom is None:
        bottom = top
    if top is None or bottom is None:
        vals = [v for v in (tl, tr, bl, br) if v is not None]
        return vals[0] if vals else None
    return top * (1.0 - ty) + bottom * ty


@dataclass(frozen=True, slots=True)
class TerrainProfileRequest:
    waypoints_deg: tuple[tuple[float, float], ...]
    cruise_altitude_ft: float


@dataclass(frozen=True, slots=True)
class TerrainSample:
    distance_nm: float
    latitude: float
    longitude: float
    terrain_ft: float | None


@dataclass(frozen=True, slots=True)
class TerrainProfileResult:
    cruise_altitude_ft: float
    safety_ceiling_ft: float
    total_distance_nm: float
    samples: tuple[TerrainSample, ...]
    ceiling_profile_ft: tuple[float | None, ...]
    breach_indices: tuple[int, ...]
    min_clearance_ft: float | None
    highest_terrain_ft: float | None
    provider_chain: tuple[str, ...]


class TerrainEngine:
    """Async DEM sampling plus VFR safety-ceiling collision analysis."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.http_headers = {"User-Agent": HTTP_USER_AGENT}
        self.disk_cache_dir = cache_dir
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)

        providers: list[DemProvider] = []
        ot_key = os.environ.get("OT_API_KEY")
        if ot_key:
            providers.append(OpenTopographySrtmProvider(ot_key))
        srtm_base = os.environ.get("SRTM_BASE_URL")
        if srtm_base and cache_dir is not None:
            providers.append(SrtmHgtProvider(srtm_base, cache_dir / "hgt"))
        providers.append(TerrariumTileProvider(cache_dir=cache_dir / "tiles" if cache_dir else None))
        self.providers = providers

    async def _sample_elevation(
        self,
        client: httpx.AsyncClient,
        lat: float,
        lon: float,
        zoom: int,
    ) -> tuple[float | None, str]:
        for provider in self.providers:
            try:
                if isinstance(provider, TerrariumTileProvider):
                    value = await provider.sample(client, lat, lon, zoom)
                else:
                    value = await provider.elevation_m(client, lat, lon)
            except (httpx.HTTPError, TerrainServiceError) as exc:
                log.warning("provider %s failed at %.4f,%.4f: %s", provider.name, lat, lon, exc)
                continue
            if value is not None:
                return value, provider.name
        return None, "unavailable"

    async def build_profile(
        self,
        waypoint_pairs: Sequence[tuple[float, float]],
        cruise_altitude_ft: float,
        samples_per_leg: int = 48,
    ) -> TerrainProfileResult:
        import asyncio

        if len(waypoint_pairs) < 2:
            raise TerrainServiceError("profile needs at least two waypoints")
        if cruise_altitude_ft <= 0:
            raise TerrainServiceError("cruise altitude must be positive")

        cache_key_payload = json.dumps(
            {
                "wpts": [list(p) for p in waypoint_pairs],
                "alt": cruise_altitude_ft,
                "spl": samples_per_leg,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(cache_key_payload.encode()).hexdigest()[:24]

        cached_result = self._read_disk_cache(digest)
        if cached_result is not None:
            return cached_result

        centerline: list[LatLon] = []
        leg_courses: list[float] = []
        leg_lengths: list[float] = []
        cumulative: list[float] = [0.0]
        for i in range(len(waypoint_pairs) - 1):
            lat1, lon1 = waypoint_pairs[i]
            lat2, lon2 = waypoint_pairs[i + 1]
            leg_pts = great_circle_points(lat1, lon1, lat2, lon2, samples_per_leg)
            if i > 0:
                leg_pts = leg_pts[1:]
            course = initial_true_bearing_deg(lat1, lon1, lat2, lon2)
            length = haversine_distance_nm(lat1, lon1, lat2, lon2)
            leg_courses.append(course)
            leg_lengths.append(length)
            for pt in leg_pts:
                centerline.append(pt)
                cumulative.append(0.0)
        cumulative = [0.0]
        running = 0.0
        leg_index_of_sample: list[int] = []
        cursor = 0.0
        for i in range(len(waypoint_pairs) - 1):
            pts_in_leg = samples_per_leg if i == 0 else samples_per_leg - 1
            for k in range(pts_in_leg):
                step = leg_lengths[i] / (samples_per_leg - 1)
                cursor += step if not (i > 0 and k == 0) else 0.0
                cumulative.append(cursor)
                leg_index_of_sample.append(i)
        cumulative = cumulative[1:]

        lanes: dict[str, list[tuple[float, float]]] = {"center": [(p.latitude, p.longitude) for p in centerline]}
        for lane_label, sign in (("left", -1.0), ("right", 1.0)):
            lane_pts: list[tuple[float, float]] = []
            for idx, pt in enumerate(centerline):
                course = leg_courses[leg_index_of_sample[idx]]
                la, lo = perpendicular_offset_point(pt.latitude, pt.longitude, course, sign * CORRIDOR_LANE_OFFSET_NM)
                lane_pts.append((la, lo))
            lanes[lane_label] = lane_pts

        total_nm = sum(leg_lengths)
        zoom = choose_terrarium_zoom(total_nm)

        elevations_ft: list[float | None] = []
        used_providers: set[str] = set()
        sem = asyncio.Semaphore(6)
        connector = httpx.AsyncClient(timeout=httpx.Timeout(30.0), headers=self.http_headers, follow_redirects=True)
        try:
            async def sample_point(lat: float, lon: float) -> tuple[float | None, str]:
                async with sem:
                    return await self._sample_elevation(connector, lat, lon, zoom)

            tasks = [sample_point(pt.latitude, pt.longitude) for pt in centerline]
            results = await asyncio.gather(*tasks)

            async def sample_lane(la: float, lo: float) -> tuple[float | None, str]:
                async with sem:
                    return await self._sample_elevation(connector, la, lo, zoom)

            lateral_tasks = []
            for lane_label in ("left", "right"):
                for idx in range(0, len(centerline), 4):
                    la, lo = lanes[lane_label][idx]
                    lateral_tasks.append((idx, sample_lane(la, lo)))
            lateral_results = await asyncio.gather(*(t[1] for t in lateral_tasks))
        finally:
            await connector.aclose()

        for value_m, provider_name in results:
            used_providers.add(provider_name)
            elevations_ft.append(None if value_m is None else round(meters_to_feet(value_m), 1))

        for (idx, _), (value_m, provider_name) in zip(lateral_tasks, lateral_results):
            used_providers.add(provider_name)
            if value_m is None:
                continue
            ft = meters_to_feet(value_m)
            if elevations_ft[idx] is None or ft > (elevations_ft[idx] or -1e9):
                elevations_ft[idx] = round(ft, 1)

        ceiling_profile: list[float | None] = []
        breach_indices: list[int] = []
        min_clearance: float | None = None
        highest: float | None = None

        for idx, elev in enumerate(elevations_ft):
            if elev is None:
                ceiling_profile.append(None)
                continue
            highest = elev if highest is None else max(highest, elev)
            ceiling = elev + SAFETY_CEILING_FT
            ceiling_profile.append(ceiling)
            clearance = cruise_altitude_ft - ceiling
            if clearance < 0:
                breach_indices.append(idx)
            if min_clearance is None or clearance < min_clearance:
                min_clearance = clearance

        result = TerrainProfileResult(
            cruise_altitude_ft=cruise_altitude_ft,
            safety_ceiling_ft=SAFETY_CEILING_FT,
            total_distance_nm=round(total_nm, 2),
            samples=tuple(
                TerrainSample(
                    distance_nm=round(cumulative[idx], 3),
                    latitude=centerline[idx].latitude,
                    longitude=centerline[idx].longitude,
                    terrain_ft=elevations_ft[idx],
                )
                for idx in range(len(centerline))
            ),
            ceiling_profile_ft=tuple(ceiling_profile),
            breach_indices=tuple(breach_indices),
            min_clearance_ft=None if min_clearance is None else round(min_clearance, 1),
            highest_terrain_ft=highest,
            provider_chain=tuple(sorted(used_providers)),
        )

        self._write_disk_cache(digest, result)
        return result

    def _disk_cache_path(self, digest: str) -> Path | None:
        if self.disk_cache_dir is None:
            return None
        return self.disk_cache_dir / "profiles" / f"{digest}.json"

    def _read_disk_cache(self, digest: str) -> TerrainProfileResult | None:
        path = self._disk_cache_path(digest)
        if path is None or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > 86400.0 * 7:
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            samples = tuple(
                TerrainSample(
                    distance_nm=s["distance_nm"],
                    latitude=s["latitude"],
                    longitude=s["longitude"],
                    terrain_ft=s["terrain_ft"],
                )
                for s in raw["samples"]
            )
            return TerrainProfileResult(
                cruise_altitude_ft=raw["cruise_altitude_ft"],
                safety_ceiling_ft=raw["safety_ceiling_ft"],
                total_distance_nm=raw["total_distance_nm"],
                samples=samples,
                ceiling_profile_ft=tuple(raw["ceiling_profile_ft"]),
                breach_indices=tuple(raw["breach_indices"]),
                min_clearance_ft=raw["min_clearance_ft"],
                highest_terrain_ft=raw["highest_terrain_ft"],
                provider_chain=tuple(raw["provider_chain"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def _write_disk_cache(self, digest: str, result: TerrainProfileResult) -> None:
        path = self._disk_cache_path(digest)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cruise_altitude_ft": result.cruise_altitude_ft,
            "safety_ceiling_ft": result.safety_ceiling_ft,
            "total_distance_nm": result.total_distance_nm,
            "samples": [
                {
                    "distance_nm": s.distance_nm,
                    "latitude": s.latitude,
                    "longitude": s.longitude,
                    "terrain_ft": s.terrain_ft,
                }
                for s in result.samples
            ],
            "ceiling_profile_ft": list(result.ceiling_profile_ft),
            "breach_indices": list(result.breach_indices),
            "min_clearance_ft": result.min_clearance_ft,
            "highest_terrain_ft": result.highest_terrain_ft,
            "provider_chain": list(result.provider_chain),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
