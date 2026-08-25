"""ENAIRE AIP Spain integration engine.

Async crawler/downloader that locates Spanish aerodrome AD 2 documents,
extracts their text, and parses the regulator-defined structures this
platform consumes: radio frequency assignments (TWR / APP / GND / ATIS),
transition altitude and transition level declarations, runway designators,
aerodrome reference points, and visual-reporting-point coordinates such as
the Point W / N / S entries published for Valencia (LEVC).

Parsed snapshots persist to a local SQLite database so cockpit deployments
keep operating through connectivity gaps. Discovery targets stay fully
operator-configurable because the AISP publishes documents under rotating
AIRAC release paths; nothing is invented when a structure cannot be parsed —
the failure surfaces explicitly.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import httpx

log = logging.getLogger("richdale.enaire")

DEFAULT_AIP_BASES: tuple[str, ...] = (
    os.environ.get("ENAIRE_AIP_BASE_URL", "https://aip.enaire.es/AIP/"),
    "https://ais.enaire.es",
)

DISCOVERY_CANDIDATE_PATHS: tuple[str, ...] = (
    "/AIP-en.html",
    "/AIP-es.html",
    "/",
    "/servicios/ais",
    "/AIP",
    "/aip",
)

INDEX_AD2_RE = re.compile(
    r"contenido_AIP/AD/AD2/([A-Z]{4})/LE_AD_2_[A-Z]{4}_(en|es)\.pdf",
    re.IGNORECASE,
)

DOCUMENT_HINT_RE = re.compile(
    r"href=[\"']([^\"']*(?:ad[_\- ]?2|AD2)[^\"']*(?:LE[A-Z]{2}|GC[A-Z]{2}|GE[A-Z]{2})[^\"']*)[\"']",
    re.IGNORECASE,
)
DIRECT_DOC_RE = re.compile(r"(https?://[^\s\"']+)(AD2?[_\- ]?(?:LE|GC|GE)[A-Z]{2}[^\s\"']*\.pdf)", re.IGNORECASE)

_FREQUENCY_RE = re.compile(r"\b(\d{3}\.\d{1,3})\s*(?:MHz|MHZ|mhz)?")
_ROLE_KEYWORDS: tuple[str, ...] = (
    "TWR", "APP", "GND", "ATIS", "AFIS", "DEL", "FIS", "ACC", "RADIO", "INFO",
)

_TRANSITION_ALT_RE = re.compile(
    r"TRANSITION\s+(?:ALTITUDE|ALT\.?)([^\n]{0,80})", re.IGNORECASE
)
_TRANSITION_LEVEL_RE = re.compile(
    r"TRANSITION\s+LEVEL([^\n]{0,80})", re.IGNORECASE
)


def _extract_ft_value(tail: str | None) -> int | None:
    """Pick the feet-denominated value from a declaration tail like '1850 m / 6000 ft'."""
    if not tail:
        return None
    pairs = re.findall(r"(\d{3,6})\s*(m\b|ft\b)?", tail, re.IGNORECASE)
    for value, unit in pairs:
        if unit and unit.lower() == "ft":
            return int(value)
    for value, unit in pairs:
        if not unit:
            return int(value)
    return None

_DMS_COORD_RE = re.compile(
    r"([0-9]{1,2})\s*[º°\*]\s*([0-9]{1,2})\s*['′m\.]?\s*"
    r"([0-9]{1,2}(?:\.[0-9]{1,3})?)?\s*[\"″s]?\s*([NS])\s*[\s,;/]{1,3}"
    r"([0-1]?[0-9]{1,2})\s*[º°\*]\s*([0-5]?[0-9])\s*['′m\.]?\s*"
    r"([0-9]{1,2}(?:\.[0-9]{1,3})?)?\s*[\"″s]?\s*([EW])",
    re.IGNORECASE,
)
_DECIMAL_COORD_RE = re.compile(r"(-?\d{1,2}\.\d{3,})[,\s]+(-?\d{1,3}\.\d{3,})")

_COMPACT_COORD_RE = re.compile(
    r"(\d{2})(\d{2})(\d{2})\s*([NS])[\s\n]+(\d{3})(\d{2})(\d{2})\s*([EW])",
    re.IGNORECASE,
)

_VRP_LINE_RE = re.compile(
    r"(?<![A-Za-z])([NSEW]{1,2}(?:\s?[-–]\s?\d{1,2})?)"
    r"(?:\s*\(([^)]{2,48})\))?"
    r"\s*:?\s*(\d{2})(\d{2})(\d{2})\s*([NS])\s+(\d{3})(\d{2})(\d{2})\s*([EW])"
)

_RWY_RE = re.compile(r"\bRWY\s?(\d{2}[LRC]?)\b", re.IGNORECASE)

_NAME_BEFORE_COORD_RE = re.compile(
    r"(?:PUNTO|POINT|WP|WPT|VRP)?\s*[-–:]?\s*((?:[A-ZÁÉÍÓÚÜÑ0-9]{1,10}\s){0,2}[A-ZÁÉÍÓÚÜÑ0-9]{1,10})\s*$",
    re.IGNORECASE,
)


class EnaireError(RuntimeError):
    pass


class Ad2DocumentUnavailable(EnaireError):
    pass


@dataclass(frozen=True, slots=True)
class FrequencyAssignment:
    service: str
    frequency_mhz: float


@dataclass(frozen=True, slots=True)
class ReportingPoint:
    name: str
    latitude_deg: float
    longitude_deg: float


@dataclass(frozen=True, slots=True)
class RunwayDesignator:
    designation: str
    heading_reference: str = "magnetic"


@dataclass(frozen=True, slots=True)
class Ad2Snapshot:
    icao: str
    source_url: str
    fetched_at_utc: datetime
    aerodrome_name: str | None
    arp_latitude: float | None
    arp_longitude: float | None
    transition_altitude_ft: int | None
    transition_level_ft: int | None
    frequencies: tuple[FrequencyAssignment, ...]
    reporting_points: tuple[ReportingPoint, ...]
    runways: tuple[RunwayDesignator, ...]
    parse_notes: tuple[str, ...]

    def to_cache_dict(self) -> dict:
        return {
            "icao": self.icao,
            "source_url": self.source_url,
            "fetched_at_utc": self.fetched_at_utc.isoformat(),
            "aerodrome_name": self.aerodrome_name,
            "arp_latitude": self.arp_latitude,
            "arp_longitude": self.arp_longitude,
            "transition_altitude_ft": self.transition_altitude_ft,
            "transition_level_ft": self.transition_level_ft,
            "frequencies": [{"service": f.service, "frequency_mhz": f.frequency_mhz} for f in self.frequencies],
            "reporting_points": [
                {"name": p.name, "latitude_deg": p.latitude_deg, "longitude_deg": p.longitude_deg}
                for p in self.reporting_points
            ],
            "runways": [{"designation": r.designation} for r in self.runways],
            "parse_notes": list(self.parse_notes),
        }

    @classmethod
    def from_cache_dict(cls, raw: dict) -> "Ad2Snapshot":
        return cls(
            icao=raw["icao"],
            source_url=raw["source_url"],
            fetched_at_utc=datetime.fromisoformat(raw["fetched_at_utc"]),
            aerodrome_name=raw.get("aerodrome_name"),
            arp_latitude=raw.get("arp_latitude"),
            arp_longitude=raw.get("arp_longitude"),
            transition_altitude_ft=raw.get("transition_altitude_ft"),
            transition_level_ft=raw.get("transition_level_ft"),
            frequencies=tuple(
                FrequencyAssignment(service=f["service"], frequency_mhz=f["frequency_mhz"])
                for f in raw.get("frequencies", [])
            ),
            reporting_points=tuple(
                ReportingPoint(name=p["name"], latitude_deg=p["latitude_deg"], longitude_deg=p["longitude_deg"])
                for p in raw.get("reporting_points", [])
            ),
            runways=tuple(RunwayDesignator(designation=r["designation"]) for r in raw.get("runways", [])),
            parse_notes=tuple(raw.get("parse_notes", [])),
        )


def dms_to_decimal(
    lat_deg: int, lat_min: int, lat_sec: float | None, ns: str,
    lon_deg: int, lon_min: int, lon_sec: float | None, ew: str,
) -> tuple[float, float]:
    lat = float(lat_deg) + float(lat_min) / 60.0 + ((lat_sec or 0.0) / 3600.0)
    lon = float(lon_deg) + float(lon_min) / 60.0 + ((lon_sec or 0.0) / 3600.0)
    if ns.upper() == "S":
        lat = -lat
    if ew.upper() == "W":
        lon = -lon
    return round(lat, 6), round(lon, 6)


def compact_to_decimal(match: "re.Match[str]") -> tuple[float, float]:
    """Convert a DDDMMSSN DDDMMSSW ICAO-style coordinate match to decimal degrees."""
    return dms_to_decimal(
        int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4),
        int(match.group(5)), int(match.group(6)), int(match.group(7)), match.group(8),
    )


def _parse_frequencies(text: str) -> tuple[FrequencyAssignment, ...]:
    found: dict[str, float] = {}
    for line in text.splitlines():
        roles = [kw for kw in _ROLE_KEYWORDS if re.search(rf"\b{kw}\b", line, re.IGNORECASE)]
        if not roles:
            continue
        for freq_match in _FREQUENCY_RE.finditer(line):
            value = float(freq_match.group(1))
            if not (100.0 <= value <= 140.0):
                continue
            service = roles[0].upper()
            found.setdefault(f"{service}-{value}", value)
    return tuple(
        FrequencyAssignment(service=key.rsplit("-", 1)[0], frequency_mhz=value)
        for key, value in found.items()
    )


def _parse_reporting_points(text: str) -> tuple[ReportingPoint, ...]:
    points: dict[str, ReportingPoint] = {}
    for line in text.splitlines():
        for match in _DMS_COORD_RE.finditer(line):
            lat, lon = dms_to_decimal(
                int(match.group(1)), int(match.group(2)),
                float(match.group(3)) if match.group(3) else None, match.group(4),
                int(match.group(5)), int(match.group(6)),
                float(match.group(7)) if match.group(7) else None, match.group(8),
            )
            prefix = line[: match.start()].strip()
            name_match = _NAME_BEFORE_COORD_RE.search(prefix)
            name = name_match.group(1).strip().rstrip(",;:") if name_match else ""
            name = re.sub(r"^(?:PUNTO|POINT|WP|WPT|VRP)\s+", "", name, flags=re.IGNORECASE).strip()
            if not name or len(name) > 16 or not re.search(r"[A-Z0-9]", name, re.IGNORECASE):
                continue
            key = f"{name.upper()}-{round(lat, 3)}-{round(lon, 3)}"
            points.setdefault(key, ReportingPoint(name=name.upper(), latitude_deg=lat, longitude_deg=lon))
    return tuple(points.values())[:64]


def parse_reporting_points_from_vac(text: str) -> tuple[ReportingPoint, ...]:
    """Parse VFR reporting points from a Visual Approach Chart text layer.

    ENAIRE VAC charts list points as `W-1 (Calicanto): 392710N 0003414W`.
    """
    points: dict[str, ReportingPoint] = {}
    for match in _VRP_LINE_RE.finditer(text):
        designator = re.sub(r"\s+", "", match.group(1)).upper()
        place = (match.group(2) or "").strip().upper()
        name = f"{designator} ({place})" if place else designator
        lat, lon = dms_to_decimal(
            int(match.group(3)), int(match.group(4)), int(match.group(5)), match.group(6),
            int(match.group(7)), int(match.group(8)), int(match.group(9)), match.group(10),
        )
        key = f"{name}-{round(lat, 3)}-{round(lon, 3)}"
        points.setdefault(key, ReportingPoint(name=name, latitude_deg=lat, longitude_deg=lon))
    return tuple(points.values())[:64]


def merge_reporting_points(
    base: tuple[ReportingPoint, ...],
    extra: tuple[ReportingPoint, ...],
) -> tuple[ReportingPoint, ...]:
    merged: dict[str, ReportingPoint] = {p.name.upper(): p for p in base}
    for point in extra:
        merged.setdefault(point.name.upper(), point)
    return tuple(merged.values())[:96]


def parse_ad2_text(icao: str, text: str, source_url: str) -> Ad2Snapshot:
    notes: list[str] = []

    arp_lat: float | None = None
    arp_lon: float | None = None
    arp_zone = _find_section(text, ("ARP", "AERODROME REFERENCE POINT"), window=400)
    if arp_zone:
        dms = _DMS_COORD_RE.search(arp_zone)
        if dms:
            arp_lat, arp_lon = dms_to_decimal(
                int(dms.group(1)), int(dms.group(2)),
                float(dms.group(3)) if dms.group(3) else None, dms.group(4),
                int(dms.group(5)), int(dms.group(6)),
                float(dms.group(7)) if dms.group(7) else None, dms.group(8),
            )
        else:
            compact = _COMPACT_COORD_RE.search(arp_zone)
            if compact:
                arp_lat, arp_lon = compact_to_decimal(compact)
            else:
                dec = _DECIMAL_COORD_RE.search(arp_zone)
                if dec:
                    la = float(dec.group(1))
                    lo = float(dec.group(2))
                    if abs(la) <= 90 and abs(lo) <= 180:
                        arp_lat, arp_lon = la, lo

    ta_match = _TRANSITION_ALT_RE.search(text)
    tl_match = _TRANSITION_LEVEL_RE.search(text)
    transition_altitude = _extract_ft_value(ta_match.group(1)) if ta_match else None
    transition_level = _extract_ft_value(tl_match.group(1)) if tl_match else None
    if transition_altitude is None:
        notes.append("transition altitude declaration not found in extracted text")

    frequencies = _parse_frequencies(text)
    if not frequencies:
        notes.append("no frequency assignments recognized")

    reporting_points = _parse_reporting_points(text)

    runways: list[RunwayDesignator] = []
    seen_runways: set[str] = set()
    for m in _RWY_RE.finditer(text):
        designation = m.group(1).upper()
        if designation in seen_runways:
            continue
        seen_runways.add(designation)
        runways.append(RunwayDesignator(designation=designation))

    name_match = re.search(
        rf"{re.escape(icao)}\s+[–—-]?\s*([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑ\s\-]{{4,48}})",
        text[:6000],
    )
    aerodrome_name = name_match.group(1).strip() if name_match else None

    return Ad2Snapshot(
        icao=icao.upper(),
        source_url=source_url,
        fetched_at_utc=datetime.now(timezone.utc),
        aerodrome_name=aerodrome_name,
        arp_latitude=arp_lat,
        arp_longitude=arp_lon,
        transition_altitude_ft=transition_altitude,
        transition_level_ft=transition_level,
        frequencies=frequencies,
        reporting_points=reporting_points,
        runways=runways,
        parse_notes=tuple(notes),
    )


def _find_section(text: str, keywords: Sequence[str], window: int = 300) -> str | None:
    lowered = text.lower()
    for keyword in keywords:
        idx = lowered.find(keyword.lower())
        if idx >= 0:
            return text[idx: idx + window]
    return None


def extract_pdf_text(payload: bytes, max_pages: int = 80) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise EnaireError("pypdf is required to read ENAIRE AD 2 documents") from exc

    reader = PdfReader(io.BytesIO(payload))
    chunks: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - individual page failures tolerated
            log.debug("page text extraction failed: %s", exc)
    return "\n".join(chunks)


class EnaireAipRepository:
    """Persistence-backed accessor for parsed AD 2 snapshots."""

    def __init__(self, db_path: Path, cache_ttl_hours: float = 84.0) -> None:
        self.db_path = db_path
        self.cache_ttl_hours = cache_ttl_hours
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ad2_snapshots (
                    icao TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (icao, content_hash)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ad2_icao_time ON ad2_snapshots (icao, fetched_at)"
            )

    def _save_sync(self, snapshot: Ad2Snapshot) -> None:
        digest = hashlib.sha256(snapshot.source_url.encode()).hexdigest()[:16]
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ad2_snapshots VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.icao,
                    snapshot.source_url,
                    snapshot.fetched_at_utc.isoformat(),
                    digest,
                    json.dumps(snapshot.to_cache_dict()),
                ),
            )

    def _load_latest_sync(self, icao: str, max_age_hours: float | None = None) -> Ad2Snapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload FROM ad2_snapshots
                WHERE icao = ?
                ORDER BY fetched_at DESC
                LIMIT 1
                """,
                (icao.upper(),),
            ).fetchone()
        if row is None:
            return None
        raw = json.loads(row[0])
        if max_age_hours is not None:
            fetched = datetime.fromisoformat(raw["fetched_at_utc"])
            age_hours = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600.0
            if age_hours > max_age_hours:
                return None
        return Ad2Snapshot.from_cache_dict(raw)

    async def cached_snapshot(self, icao: str) -> Ad2Snapshot | None:
        return await asyncio.to_thread(self._load_latest_sync, icao)

    async def any_snapshot(self, icao: str) -> Ad2Snapshot | None:
        """Latest snapshot regardless of age — used when the network is down."""
        return await asyncio.to_thread(self._load_latest_sync, icao, None)

    async def store_snapshot(self, snapshot: Ad2Snapshot) -> None:
        await asyncio.to_thread(self._save_sync, snapshot)


class EnaireAd2Service:
    """Live discovery + parsing pipeline for Spanish aerodrome AD 2 charts."""

    def __init__(
        self,
        repository: EnaireAipRepository,
        bases: Sequence[str] = DEFAULT_AIP_BASES,
        document_url_template: str | None = None,
    ) -> None:
        self.repository = repository
        self.bases = tuple(bases)
        self.document_url_template = (
            document_url_template
            or os.environ.get("ENAIRE_AD2_URL_TEMPLATE")
        )

    async def get_ad2(self, icao: str, force_refresh: bool = False) -> Ad2Snapshot:
        target = icao.strip().upper()
        if not re.fullmatch(r"[A-Z]{4}", target):
            raise EnaireError(f"'{icao}' is not a four-letter ICAO designator")

        if not force_refresh:
            cached = await self.repository.cached_snapshot(target)
            if cached is not None:
                return cached

        try:
            document_url = await self._discover_document(target)
            payload = await self._download(document_url)
            text = await asyncio.to_thread(extract_pdf_text, payload)
            snapshot = parse_ad2_text(target, text, source_url=document_url)
        except (EnaireError, httpx.HTTPError) as exc:
            stale = await self.repository.any_snapshot(target)
            if stale is None:
                raise
            age_hours = (
                datetime.now(timezone.utc) - stale.fetched_at_utc
            ).total_seconds() / 3600.0
            log.warning("serving stale AD2 for %s (%.1fh old): %s", target, age_hours, exc)
            return Ad2Snapshot(
                icao=stale.icao,
                source_url=stale.source_url,
                fetched_at_utc=stale.fetched_at_utc,
                aerodrome_name=stale.aerodrome_name,
                arp_latitude=stale.arp_latitude,
                arp_longitude=stale.arp_longitude,
                transition_altitude_ft=stale.transition_altitude_ft,
                transition_level_ft=stale.transition_level_ft,
                frequencies=stale.frequencies,
                reporting_points=stale.reporting_points,
                runways=stale.runways,
                parse_notes=stale.parse_notes
                + (f"Network unavailable — showing data cached {age_hours:.0f} h ago",),
            )

        try:
            vac_text = await self._load_vac_text(target)
        except (EnaireError, httpx.HTTPError) as exc:
            log.info("VAC chart unavailable for %s: %s", target, exc)
        else:
            if vac_text:
                extra_points = parse_reporting_points_from_vac(vac_text)
                if extra_points:
                    snapshot = Ad2Snapshot(
                        icao=snapshot.icao,
                        source_url=snapshot.source_url,
                        fetched_at_utc=snapshot.fetched_at_utc,
                        aerodrome_name=snapshot.aerodrome_name,
                        arp_latitude=snapshot.arp_latitude,
                        arp_longitude=snapshot.arp_longitude,
                        transition_altitude_ft=snapshot.transition_altitude_ft,
                        transition_level_ft=snapshot.transition_level_ft,
                        frequencies=snapshot.frequencies,
                        reporting_points=merge_reporting_points(
                            snapshot.reporting_points, extra_points
                        ),
                        runways=snapshot.runways,
                        parse_notes=snapshot.parse_notes
                        + ("VFR reporting points sourced from the ENAIRE VAC chart",),
                    )

        await self.repository.store_snapshot(snapshot)
        return snapshot

    async def _load_vac_text(self, icao: str) -> str | None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) aviation-richdale/1.0",
            "Accept": "application/pdf,*/*",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0), headers=headers, follow_redirects=True) as client:
            for base in self.bases:
                for suffix in ("en", "es"):
                    url = (
                        f"{base.rstrip('/')}/contenido_AIP/AD/AD2/{icao}/"
                        f"LE_AD_2_{icao}_VAC_1_{suffix}.pdf"
                    )
                    try:
                        response = await client.get(url)
                    except httpx.HTTPError:
                        continue
                    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
                        continue
                    return await asyncio.to_thread(extract_pdf_text, response.content)
        return None

    async def _discover_document(self, icao: str) -> str:
        if self.document_url_template:
            url = self.document_url_template.format(icao=icao)
            probe = await self._head_ok(url)
            if probe:
                return url
            raise Ad2DocumentUnavailable(
                f"configured ENAIRE_AD2_URL_TEMPLATE produced no document for {icao}: {url}"
            )

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) aviation-richdale/1.0",
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0), headers=headers, follow_redirects=True) as client:
            for base in self.bases:
                for path in DISCOVERY_CANDIDATE_PATHS:
                    page_url = f"{base.rstrip('/')}{path}"
                    try:
                        response = await client.get(page_url)
                        if response.status_code != 200:
                            continue
                        body = response.text
                    except httpx.HTTPError:
                        continue

                    candidates: dict[str, str] = {}
                    for index_match in INDEX_AD2_RE.finditer(body):
                        found_icao, lang = index_match.group(1).upper(), index_match.group(2).lower()
                        if found_icao != icao:
                            continue
                        relative = index_match.group(0)
                        if lang == "en" or found_icao not in candidates:
                            candidates[found_icao] = (
                                relative if relative.startswith("http")
                                else f"{base.rstrip('/')}/{relative.lstrip('/')}"
                            )
                    if icao in candidates:
                        return candidates[icao]

                    direct = DIRECT_DOC_RE.search(body)
                    if direct:
                        candidate = direct.group(1) + direct.group(2)
                        if icao in candidate.upper():
                            return candidate

                    for hint_match in DOCUMENT_HINT_RE.finditer(body):
                        href = hint_match.group(1)
                        if icao not in href.upper():
                            continue
                        absolute = href if href.startswith("http") else f"{base.rstrip('/')}/{href.lstrip('/')}"
                        return absolute

                for suffix in ("en", "es"):
                    fallback_url = (
                        f"{base.rstrip('/')}/contenido_AIP/AD/AD2/{icao}/LE_AD_2_{icao}_{suffix}.pdf"
                    )
                    if await self._head_ok(fallback_url):
                        return fallback_url

        raise Ad2DocumentUnavailable(
            f"could not locate an ENAIRE AD 2 document for {icao} on {', '.join(self.bases)}; "
            "set ENAIRE_AIP_BASE_URL / ENAIRE_AD2_URL_TEMPLATE to the current AISP publication path "
            "(AIRAC releases rotate their directory names)"
        )

    async def _head_ok(self, url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True) as client:
                response = await client.head(url)
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def _download(self, url: str) -> bytes:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) aviation-richdale/1.0",
            "Accept": "application/pdf,*/*",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0), headers=headers, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "pdf" not in content_type.lower() and not response.content[:5] == b"%PDF-":
                raise Ad2DocumentUnavailable(f"document at {url} is not a PDF ({content_type or 'unknown type'})")
            return response.content


class EnaireNotamService:
    """Live NOTAMs from ENAIRE's public Insignia ArcGIS FeatureServer.

    The service exposes geo-located Spanish NOTAMs with full item E text and
    effective/expire windows, keyless. Responses are cached briefly because
    NOTAM data updates continuously but preflight use tolerates minutes.
    """

    QUERY_URL = (
        "https://servais.enaire.es/insignias/rest/services/NOTAM/"
        "NOTAM_APP_V3/FeatureServer/0/query"
    )
    CACHE_TTL_S = 300.0
    MAX_RECORDS = 120

    def __init__(self) -> None:
        self._cache: dict[tuple[float, float, float, float], tuple[float, list[dict[str, Any]]]] = {}

    async def query(self, south: float, west: float, north: float, east: float) -> list[dict[str, Any]]:
        key = (round(south, 1), round(west, 1), round(north, 1), round(east, 1))
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self.CACHE_TTL_S:
            return cached[1]

        params = {
            "f": "json",
            "where": "1=1",
            "outFields": "notamId,itemB,itemC,itemE",
            "geometry": f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outSR": "4326",
            "orderByFields": "itemB DESC",
            "resultRecordCount": str(self.MAX_RECORDS),
        }
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) aviation-richdale/1.0"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0), headers=headers) as client:
            response = await client.get(self.QUERY_URL, params=params)
            response.raise_for_status()
            body = response.json()

        now_utc = datetime.now(timezone.utc)
        notams: list[dict[str, Any]] = []
        for feature in body.get("features", []):
            attrs = feature.get("attributes") or {}
            geom = feature.get("geometry") or {}
            expires_raw = attrs.get("itemC")
            try:
                # ArcGIS epoch dates arrive as milliseconds since 1970.
                expires = datetime.fromtimestamp(expires_raw / 1000.0, tz=timezone.utc) if expires_raw else None
            except (ValueError, OSError, OverflowError):
                expires = None
            notams.append({
                "id": attrs.get("notamId"),
                "text": (attrs.get("itemE") or "").strip(),
                "effective": attrs.get("itemBstr"),
                "expires": attrs.get("itemCstr"),
                "expires_epoch_ms": expires_raw,
                "expired": bool(expires and expires < now_utc),
                "latitude": geom.get("y"),
                "longitude": geom.get("x"),
            })

        self._cache[key] = (now, notams)
        return notams
