"""Spherical navigation mathematics, the classic E6B wind triangle, and the
World Magnetic Model declination engine.

Every routine here is pure math over real geodetic coordinates. Nothing in this
module performs I/O and nothing depends on network state, which keeps it
unit-testable and safe to reuse across services.
"""

from __future__ import annotations

import io
import logging
import math
import os
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

log = logging.getLogger("richdale.navigation")

EARTH_MEAN_RADIUS_NM = 3440.0647416288884  # 6371.0088 km expressed in nautical miles (1 NM = 1852 m)
DEG2RAD = math.pi / 180.0
RAD2DEG = 180.0 / math.pi

WMM_REMOTE_ZIP_URL = "https://www.ngdc.noaa.gov/geomag/WMM/data/WMM2020/WMM2020COF.zip"
WMM_ZIP_MEMBER_HINTS = ("WMM.COF", "WMM2020.COF", "WMM2025.COF")

CROSSWIND_LIMIT_KT = 15.0


def normalize_bearing(angle_deg: float) -> float:
    return angle_deg % 360.0


def signed_angular_difference(target_deg: float, source_deg: float) -> float:
    return (target_deg - source_deg + 540.0) % 360.0 - 180.0


@dataclass(frozen=True, slots=True)
class LatLon:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not (-90.0 <= self.latitude <= 90.0):
            raise ValueError(f"latitude out of range: {self.latitude}")
        if not (-180.0 <= self.longitude <= 180.0):
            raise ValueError(f"longitude out of range: {self.longitude}")


def meters_to_nm(meters: float) -> float:
    return meters / 1852.0


def nm_to_meters(nm: float) -> float:
    return nm * 1852.0


def meters_to_feet(meters: float) -> float:
    return meters * 3.280839895013123


def feet_to_meters(feet: float) -> float:
    return feet * 0.3048


def knots_to_kmh(knots: float) -> float:
    return knots * 1.852


def liters_per_hour_from_gph(gph: float, fuel_density_lb_gal: float = 6.7) -> float:
    return gph * 3.785411784 * (fuel_density_lb_gal * 0.45359237) / 0.72


def haversine_distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = lat1 * DEG2RAD
    phi2 = lat2 * DEG2RAD
    dphi = (lat2 - lat1) * DEG2RAD
    dlmb = (lon2 - lon1) * DEG2RAD
    sin_dphi_half = math.sin(dphi / 2.0)
    sin_dlmb_half = math.sin(dlmb / 2.0)
    a = sin_dphi_half * sin_dphi_half + math.cos(phi1) * math.cos(phi2) * sin_dlmb_half * sin_dlmb_half
    a = min(1.0, max(0.0, a))
    central_angle = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_MEAN_RADIUS_NM * central_angle


def initial_true_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = lat1 * DEG2RAD
    phi2 = lat2 * DEG2RAD
    dlmb = (lon2 - lon1) * DEG2RAD
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return normalize_bearing(math.atan2(y, x) * RAD2DEG)


def final_true_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    reverse = initial_true_bearing_deg(lat2, lon2, lat1, lon1)
    return normalize_bearing(reverse + 180.0)


def great_circle_interpolate(
    lat1: float, lon1: float, lat2: float, lon2: float, fraction: float
) -> tuple[float, float]:
    if fraction <= 0.0:
        return lat1, lon1
    if fraction >= 1.0:
        return lat2, lon2

    phi1 = lat1 * DEG2RAD
    lmb1 = lon1 * DEG2RAD
    phi2 = lat2 * DEG2RAD
    lmb2 = lon2 * DEG2RAD

    v1x = math.cos(phi1) * math.cos(lmb1)
    v1y = math.cos(phi1) * math.sin(lmb1)
    v1z = math.sin(phi1)

    v2x = math.cos(phi2) * math.cos(lmb2)
    v2y = math.cos(phi2) * math.sin(lmb2)
    v2z = math.sin(phi2)

    dot = min(1.0, max(-1.0, v1x * v2x + v1y * v2y + v1z * v2z))
    omega = math.acos(dot)
    if omega < 1e-12:
        return lat1, lon1

    sin_omega = math.sin(omega)
    a = math.sin((1.0 - fraction) * omega) / sin_omega
    b = math.sin(fraction * omega) / sin_omega

    vx = a * v1x + b * v2x
    vy = a * v1y + b * v2y
    vz = a * v1z + b * v2z

    hyp_xy = math.hypot(vx, vy)
    lat = math.atan2(vz, hyp_xy) * RAD2DEG
    lon = math.atan2(vy, vx) * RAD2DEG
    return lat, lon


def great_circle_points(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    samples: int = 64,
) -> list[LatLon]:
    if samples < 2:
        raise ValueError("great_circle_points requires at least two samples")
    pts: list[LatLon] = []
    denom = samples - 1
    for i in range(samples):
        f = i / denom
        la, lo = great_circle_interpolate(lat1, lon1, lat2, lon2, f)
        pts.append(LatLon(latitude=la, longitude=lo))
    return pts


def perpendicular_offset_point(
    lat: float, lon: float, course_true_deg: float, offset_nm: float
) -> tuple[float, float]:
    right_course = normalize_bearing(course_true_deg + 90.0)
    rad = right_course * DEG2RAD
    angular = offset_nm / EARTH_MEAN_RADIUS_NM
    phi1 = lat * DEG2RAD
    lmb1 = lon * DEG2RAD
    phi2 = math.asin(
        min(1.0, max(-1.0, math.sin(phi1) * math.cos(angular) + math.cos(phi1) * math.sin(angular) * math.cos(rad)))
    )
    lmb2 = lmb1 + math.atan2(
        math.sin(rad) * math.sin(angular) * math.cos(phi1),
        math.cos(angular) - math.sin(phi1) * math.sin(phi2),
    )
    return phi2 * RAD2DEG, ((lmb2 * RAD2DEG + 540.0) % 360.0) - 180.0


def isa_pressure_hpa(altitude_ft: float) -> float:
    t0 = 288.15
    lapse = 0.0019812  # K per ft below the tropopause
    tropopause_ft = 36089.2388
    if altitude_ft <= tropopause_ft:
        t_ratio = (t0 - lapse * altitude_ft) / t0
        return 1013.25 * math.pow(t_ratio, 5.25612219)
    p_trop = isa_pressure_hpa(tropopause_ft)
    return p_trop * math.exp(-(altitude_ft - tropopause_ft) * 0.0000480697)


PRESSURE_LEVELS_HPA: tuple[float, ...] = (
    1013.0, 1000.0, 975.0, 950.0, 925.0, 900.0, 850.0, 800.0,
    750.0, 700.0, 650.0, 600.0, 550.0, 500.0, 450.0, 400.0,
    350.0, 300.0, 250.0, 200.0, 150.0, 100.0,
)


def nearest_pressure_level_hpa(altitude_ft: float) -> float:
    target = isa_pressure_hpa(max(0.0, min(altitude_ft, 65000.0)))
    best = PRESSURE_LEVELS_HPA[0]
    best_delta = abs(best - target)
    for level in PRESSURE_LEVELS_HPA[1:]:
        delta = abs(level - target)
        if delta < best_delta:
            best = level
            best_delta = delta
    return best


@dataclass(frozen=True, slots=True)
class WindTriangleSolution:
    true_course_deg: float
    wind_correction_angle_deg: float
    magnetic_variation_deg: float
    magnetic_heading_deg: float
    true_heading_deg: float
    ground_speed_kt: float
    headwind_component_kt: float
    crosswind_component_kt: float
    wind_from_deg_true: float | None
    wind_speed_kt: float


def solve_wind_triangle(
    true_course_deg: float,
    true_airspeed_kt: float,
    wind_from_deg_true: float | None,
    wind_speed_kt: float,
) -> WindTriangleSolution:
    """Classic E6B triangle of velocities.

    The wind angle is measured from the nose: relative bearing of the wind's
    FROM direction against the course. Its sine component pushes the aircraft
    laterally and demands the wind correction angle; its cosine component is
    the headwind that must be subtracted from the heading-speed resultant.
    """
    if true_airspeed_kt <= 0.0:
        raise ValueError("true airspeed must be positive")
    if wind_speed_kt < 0.0:
        raise ValueError("wind speed cannot be negative")

    tc = normalize_bearing(true_course_deg)
    if wind_from_deg_true is None or wind_speed_kt == 0.0:
        wca = 0.0
        gs = true_airspeed_kt
        headwind = 0.0
        crosswind = 0.0
    else:
        rel = (wind_from_deg_true - tc) * DEG2RAD
        crosswind = wind_speed_kt * math.sin(rel)
        headwind = wind_speed_kt * math.cos(rel)
        swc = crosswind / true_airspeed_kt
        if abs(swc) > 1.0:
            raise ValueError(
                "wind exceeds airspeed envelope: no closed-form drift solution "
                f"(crosswind {abs(crosswind):.1f} kt vs TAS {true_airspeed_kt:.1f} kt)"
            )
        wca = math.asin(swc) * RAD2DEG
        gs = max(0.0, true_airspeed_kt * math.cos(wca * DEG2RAD) - headwind)

    return WindTriangleSolution(
        true_course_deg=tc,
        wind_correction_angle_deg=wca,
        magnetic_variation_deg=0.0,
        magnetic_heading_deg=normalize_bearing(tc + wca),
        true_heading_deg=normalize_bearing(tc + wca),
        ground_speed_kt=gs,
        headwind_component_kt=headwind,
        crosswind_component_kt=crosswind,
        wind_from_deg_true=wind_from_deg_true,
        wind_speed_kt=wind_speed_kt,
    )


def apply_magnetic_variation(solution: WindTriangleSolution, variation_deg_east_positive: float) -> WindTriangleSolution:
    """East is least: magnetic heading = true heading minus easterly variation."""
    mh = normalize_bearing(solution.true_heading_deg - variation_deg_east_positive)
    return WindTriangleSolution(
        true_course_deg=solution.true_course_deg,
        wind_correction_angle_deg=solution.wind_correction_angle_deg,
        magnetic_variation_deg=variation_deg_east_positive,
        magnetic_heading_deg=mh,
        true_heading_deg=solution.true_heading_deg,
        ground_speed_kt=solution.ground_speed_kt,
        headwind_component_kt=solution.headwind_component_kt,
        crosswind_component_kt=solution.crosswind_component_kt,
        wind_from_deg_true=solution.wind_from_deg_true,
        wind_speed_kt=solution.wind_speed_kt,
    )


@dataclass(frozen=True, slots=True)
class CrosswindResult:
    runway_heading_magnetic_deg: float
    wind_from_deg_true: float
    wind_speed_kt: float
    magnetic_variation_deg: float
    headwind_kt: float
    crosswind_signed_kt: float
    crosswind_kt: float
    exceeds_limit: bool


def compute_crosswind(
    runway_heading_magnetic_deg: float,
    wind_from_deg_true: float,
    wind_speed_kt: float,
    magnetic_variation_deg: float,
    limit_kt: float = CROSSWIND_LIMIT_KT,
) -> CrosswindResult:
    """Crosswind components against a magnetic runway heading.

    METAR surface wind directions are true; runway designators are magnetic.
    The localized magnetic variation bridges the two reference frames before
    any trigonometry runs.
    """
    runway_true = runway_heading_magnetic_deg + magnetic_variation_deg
    rel = (wind_from_deg_true - runway_true) * DEG2RAD
    headwind = wind_speed_kt * math.cos(rel)
    crosswind_signed = wind_speed_kt * math.sin(rel)
    magnitude = abs(crosswind_signed)
    return CrosswindResult(
        runway_heading_magnetic_deg=runway_heading_magnetic_deg,
        wind_from_deg_true=wind_from_deg_true,
        wind_speed_kt=wind_speed_kt,
        magnetic_variation_deg=magnetic_variation_deg,
        headwind_kt=headwind,
        crosswind_signed_kt=crosswind_signed,
        crosswind_kt=magnitude,
        exceeds_limit=magnitude > limit_kt,
    )


class MagneticModelUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GeomagneticField:
    declination_deg: float
    inclination_deg: float
    total_intensity_nT: float
    horizontal_intensity_nT: float
    north_component_nT: float
    east_component_nT: float
    vertical_component_nT: float


class WorldMagneticModel:
    """NOAA World Magnetic Model main-field evaluator.

    Faithful implementation of the official WMM evaluation: the COF Schmidt
    coefficients are converted once into the working basis used by the
    reference algorithm (snorm-scaled, transposed storage), then the field is
    synthesized with unnormalized associated Legendre functions built by the
    K-recursion, and rotated from spherical to geodetic components.
    """

    _SIZE = 16

    def __init__(self, cof_text: str, source_label: str = "memory") -> None:
        lines = [ln.rstrip() for ln in cof_text.splitlines() if ln.strip()]
        if len(lines) < 3:
            raise MagneticModelUnavailable("coefficient file truncated")

        header = lines[0].split()
        try:
            self.epoch: float = float(header[0])
        except (ValueError, IndexError) as exc:
            raise MagneticModelUnavailable(f"bad epoch line: {lines[0]!r}") from exc

        size = self._SIZE
        maxord = 12
        c_mat = [0.0] * (size * size)
        cd_mat = [0.0] * (size * size)
        k_mat = [0.0] * (size * size)
        snorm = [0.0] * (size * size)
        fn = [0.0] * size
        fm = [0.0] * size

        seen_rows = 0
        for raw in lines[1:]:
            fields = raw.split()
            if not fields:
                continue
            if fields[0].startswith("9999"):
                break
            if len(fields) < 5:
                continue
            try:
                n = int(float(fields[0]))
                m = int(float(fields[1]))
                gnm = float(fields[2])
                hnm = float(fields[3])
                dgnm = float(fields[4]) if len(fields) > 4 else 0.0
                dhnm = float(fields[5]) if len(fields) > 5 else 0.0
            except ValueError as exc:
                raise MagneticModelUnavailable(f"unparseable coefficient row: {raw!r}") from exc
            if m > n or m > maxord or n > maxord:
                continue
            seen_rows += 1
            c_mat[m * size + n] = gnm
            cd_mat[m * size + n] = dgnm
            if m != 0:
                c_mat[n * size + (m - 1)] = hnm
                cd_mat[n * size + (m - 1)] = dhnm

        if seen_rows == 0:
            raise MagneticModelUnavailable("no Gauss coefficients found in file")
        self.max_degree = maxord
        self.reference_radius_km = 6371.2
        self.source_label = source_label

        snorm[0] = 1.0
        for n in range(1, maxord + 1):
            snorm[n] = snorm[n - 1] * (2.0 * n - 1.0) / n
            j = 2
            m = 0
            while m <= n:
                k_mat[m * size + n] = (
                    ((n - 1) * (n - 1) - m * m) / ((2.0 * n - 1.0) * (2.0 * n - 3.0))
                )
                if m > 0:
                    flnmj = ((n - m + 1) * j) / (n + m)
                    snorm[n + m * size] = snorm[n + (m - 1) * size] * math.sqrt(flnmj)
                    j = 1
                    c_mat[n * size + (m - 1)] *= snorm[n + m * size]
                    cd_mat[n * size + (m - 1)] *= snorm[n + m * size]
                c_mat[m * size + n] *= snorm[n + m * size]
                cd_mat[m * size + n] *= snorm[n + m * size]
                m += 1
            fn[n] = float(n + 1)
            fm[n] = float(n)
        k_mat[1 * size + 1] = 0.0

        self._c = c_mat
        self._cd = cd_mat
        self._k = k_mat
        self._fn = fn
        self._fm = fm

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "WorldMagneticModel":
        text = Path(path).read_text(encoding="utf-8")
        return cls(text, source_label=str(path))

    def evaluate(
        self,
        latitude_deg: float,
        longitude_deg: float,
        altitude_ft: float,
        when: datetime | None = None,
    ) -> GeomagneticField:
        when = when or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        doy = when.timetuple().tm_yday
        hour_fraction = (
            when.hour * 3600.0 + when.minute * 60.0 + when.second + when.microsecond / 1e6
        ) / 86400.0
        days_in_year = 366.0 if (when.year % 4 == 0 and (when.year % 100 != 0 or when.year % 400 == 0)) else 365.0
        decimal_year = when.year + ((doy - 1) + hour_fraction) / days_in_year
        dt_years = decimal_year - self.epoch

        size = self._SIZE
        maxord = self.max_degree

        a = 6378.137
        b = 6356.7523142
        re = 6371.2
        a2 = a * a
        b2 = b * b
        c2 = a2 - b2
        a4 = a2 * a2
        b4 = b2 * b2
        c4 = a4 - b4

        alt_km = feet_to_meters(altitude_ft) / 1000.0

        sp = [0.0] * (maxord + 1)
        cp = [1.0] * (maxord + 1)
        pp = [1.0] * (maxord + 1)
        p = [0.0] * (size * size)
        dp = [0.0] * (size * size)
        p[0] = 1.0

        rlon = math.radians(longitude_deg)
        rlat = math.radians(latitude_deg)
        srlon = math.sin(rlon)
        srlat = math.sin(rlat)
        crlon = math.cos(rlon)
        crlat = math.cos(rlat)
        srlat2 = srlat * srlat
        crlat2 = crlat * crlat
        sp_1 = srlon
        cp_1 = crlon

        q = math.sqrt(a2 - c2 * srlat2)
        q1 = alt_km * q
        q2 = ((q1 + a2) / (q1 + b2)) * ((q1 + a2) / (q1 + b2))
        ct = srlat / math.sqrt(q2 * crlat2 + srlat2)
        st = math.sqrt(1.0 - (ct * ct))
        r2 = (alt_km * alt_km) + 2.0 * q1 + (a4 - c4 * srlat2) / (q * q)
        r = math.sqrt(r2)
        d = math.sqrt(a2 * crlat2 + b2 * srlat2)
        ca = (alt_km + d) / r
        sa = c2 * crlat * srlat / (r * d)

        sp_m1 = sp_1
        cp_m1 = cp_1
        cos_mlmb_cache = [1.0] * (maxord + 1)
        sin_mlmb_cache = [0.0] * (maxord + 1)
        if maxord >= 1:
            cos_mlmb_cache[1] = cp_1
            sin_mlmb_cache[1] = sp_1
        for m in range(2, maxord + 1):
            new_sp = sp_1 * cp_m1 + cp_1 * sp_m1
            new_cp = cp_1 * cp_m1 - sp_1 * sp_m1
            cos_mlmb_cache[m] = new_cp
            sin_mlmb_cache[m] = new_sp
            sp_m1, cp_m1 = new_sp, new_cp

        aor = re / r
        ar = aor * aor
        br = bt = bp = bpp = 0.0

        for n in range(1, maxord + 1):
            ar = ar * aor
            for m in range(0, n + 1):
                idx_p = n + m * size
                if n == m:
                    p[idx_p] = st * p[(n - 1) + (m - 1) * size]
                    dp[m * size + n] = (
                        st * dp[(m - 1) * size + (n - 1)]
                        + ct * p[(n - 1) + (m - 1) * size]
                    )
                elif n == 1 and m == 0:
                    p[idx_p] = ct * p[0]
                    dp[m * size + n] = ct * dp[0] - st * p[0]
                else:
                    idx_nm2 = (n - 2) + m * size
                    if m > n - 2:
                        p[idx_nm2] = 0.0
                        dp[m * size + (n - 2)] = 0.0
                    p[idx_p] = (
                        ct * p[(n - 1) + m * size]
                        - self._k[m * size + n] * p[idx_nm2]
                    )
                    dp[m * size + n] = (
                        ct * dp[m * size + (n - 1)]
                        - st * p[(n - 1) + m * size]
                        - self._k[m * size + n] * dp[m * size + (n - 2)]
                    )

                tc_g = self._c[m * size + n] + dt_years * self._cd[m * size + n]
                if m != 0:
                    tc_h = self._c[n * size + (m - 1)] + dt_years * self._cd[n * size + (m - 1)]
                    temp1 = tc_g * cos_mlmb_cache[m] + tc_h * sin_mlmb_cache[m]
                    temp2 = tc_g * sin_mlmb_cache[m] - tc_h * cos_mlmb_cache[m]
                else:
                    temp1 = tc_g * cos_mlmb_cache[0]
                    temp2 = tc_g * sin_mlmb_cache[0]

                par = ar * p[idx_p]
                bt -= ar * temp1 * dp[m * size + n]
                bp += self._fm[m] * temp2 * par
                br += self._fn[n] * temp1 * par

                if st == 0.0 and m == 1:
                    if n == 1:
                        pp[n] = pp[n - 1]
                    else:
                        pp[n] = ct * pp[n - 1] - self._k[1 * size + n] * pp[n - 2]
                    bpp += self._fm[m] * temp2 * ar * pp[n]

        if st == 0.0:
            bp = bpp
        else:
            bp /= st

        bx = -bt * ca - br * sa
        by = bp
        bz = bt * sa - br * ca

        bh = math.hypot(bx, by)
        f_field = math.sqrt(bh * bh + bz * bz)
        declination = math.degrees(math.atan2(by, bx))
        inclination = math.degrees(math.atan2(bz, bh))

        return GeomagneticField(
            declination_deg=declination,
            inclination_deg=inclination,
            total_intensity_nT=f_field,
            horizontal_intensity_nT=bh,
            north_component_nT=bx,
            east_component_nT=by,
            vertical_component_nT=bz,
        )

    def declination_deg(
        self,
        latitude_deg: float,
        longitude_deg: float,
        altitude_ft: float = 0.0,
        when: datetime | None = None,
    ) -> float:
        return self.evaluate(latitude_deg, longitude_deg, altitude_ft, when).declination_deg

_model_cache: dict[str, tuple[float, WorldMagneticModel]] = {}
_CACHE_TTL_SECONDS = 86400.0


async def load_world_magnetic_model(cache_dir: Path | None = None) -> WorldMagneticModel:
    """Resolve the best available coefficient set without ever inventing data.

    Resolution order: explicit env override, bundled magmodel/*.COF, previously
    cached remote fetch, live NOAA archive download. Every failure raises so
    callers can surface an actionable configuration error.
    """
    import httpx

    now_mono = time.monotonic()
    cache_key = "singleton"
    cached = _model_cache.get(cache_key)
    if cached and now_mono - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    candidates: list[Path] = []
    env_path = os.environ.get("MAGMODEL_COF_PATH")
    if env_path:
        candidates.append(Path(env_path))

    package_dir = Path(__file__).resolve().parent / "magmodel"
    if package_dir.is_dir():
        cof_files = sorted(package_dir.glob("*.COF")) + sorted(package_dir.glob("*.cof"))
        candidates.extend(cof_files)

    if cache_dir is not None:
        cached_file = cache_dir / "magmodel" / "WMM.COF"
        if cached_file.exists():
            candidates.append(cached_file)

    for candidate in candidates:
        try:
            model = WorldMagneticModel.from_file(candidate)
            _model_cache[cache_key] = (now_mono, model)
            return model
        except (OSError, MagneticModelUnavailable) as exc:
            log.warning("magnetic model candidate failed (%s): %s", candidate.name, exc)

    if cache_dir is None:
        raise MagneticModelUnavailable(
            "no WMM coefficients available: bundle magmodel/WMM.COF or set MAGMODEL_COF_PATH"
        )

    cache_dir = Path(cache_dir) / "magmodel"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "WMM2020COF.zip"

    async def _fetch_zip() -> bytes:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0), follow_redirects=True) as client:
            response = await client.get(WMM_REMOTE_ZIP_URL)
            response.raise_for_status()
            return response.content

    zip_bytes = await _fetch_zip()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        member_name = None
        for name in archive.namelist():
            base = Path(name).name.upper()
            if base in WMM_ZIP_MEMBER_HINTS or base.endswith(".COF"):
                member_name = name
                break
        if member_name is None:
            raise MagneticModelUnavailable("NOAA archive contained no .COF member")
        cof_text = archive.read(member_name).decode("utf-8")

    (cache_dir / "WMM.COF").write_text(cof_text, encoding="utf-8")
    if target.exists():
        target.unlink()

    model = WorldMagneticModel(cof_text, source_label=f"remote:{WMM_REMOTE_ZIP_URL}")
    _model_cache[cache_key] = (time.monotonic(), model)
    return model


def route_leg_midpoint(points: Sequence[LatLon]) -> tuple[float, float]:
    if not points:
        raise ValueError("empty point sequence")
    middle = points[len(points) // 2]
    return middle.latitude, middle.longitude


def cumulative_leg_distances_nm(points: Iterable[LatLon]) -> list[float]:
    result: list[float] = []
    running = 0.0
    previous: LatLon | None = None
    for pt in points:
        if previous is not None:
            running += haversine_distance_nm(previous.latitude, previous.longitude, pt.latitude, pt.longitude)
        result.append(running)
        previous = pt
    return result
