"""Geometry + geocoding.

A Region is anything we can test "is this aircraft inside?" against:
a GeoJSON (Multi)Polygon, a bounding box, or a circle (point + radius).
Places (countries, cities, postcodes/pincodes) are resolved to Regions via
OpenStreetMap Nominatim, with results cached in SQLite and a small built-in
gazetteer used as an offline fallback.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Iterable

import httpx

from . import config
from .store import Store

EARTH_R_KM = 6371.0


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def destination(lat, lon, bearing_deg, dist_km):
    """Great-circle destination point."""
    d = dist_km / EARTH_R_KM
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def _ring_contains(ring, lon, lat) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


@dataclass
class Region:
    name: str
    kind: str  # country | city | postcode | area | custom
    bbox: tuple[float, float, float, float]  # south, west, north, east
    polygons: list = field(default_factory=list)  # list of polygons; polygon = [outer, hole, ...] in [lon,lat]
    center: tuple[float, float] | None = None  # lat, lon
    radius_km: float | None = None
    source: str = "nominatim"

    @property
    def key(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", f"{self.kind}-{self.name}".lower()).strip("-")

    def contains(self, lat: float, lon: float) -> bool:
        if self.radius_km is not None and self.center:
            return haversine_km(lat, lon, *self.center) <= self.radius_km
        s, w, n, e = self.bbox
        if not (s <= lat <= n):
            return False
        if w <= e:
            if not (w <= lon <= e):
                return False
        elif not (lon >= w or lon <= e):  # bbox crosses the antimeridian
            return False
        if not self.polygons:
            return True
        for poly in self.polygons:
            if _ring_contains(poly[0], lon, lat) and not any(_ring_contains(h, lon, lat) for h in poly[1:]):
                return True
        return False

    def centroid(self) -> tuple[float, float]:
        if self.center:
            return self.center
        s, w, n, e = self.bbox
        if w > e:
            e += 360
        return (s + n) / 2, ((w + e) / 2 + 180) % 360 - 180

    def to_geojson(self) -> dict:
        if self.radius_km is not None and self.center:
            ring = [list(reversed(destination(*self.center, b, self.radius_km))) for b in range(0, 361, 10)]
            geom = {"type": "Polygon", "coordinates": [ring]}
        elif self.polygons:
            geom = {"type": "MultiPolygon", "coordinates": self.polygons}
        else:
            s, w, n, e = self.bbox
            geom = {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
        return {"type": "Feature", "properties": {"name": self.name, "kind": self.kind}, "geometry": geom}

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "bbox": list(self.bbox), "polygons": self.polygons,
                "center": list(self.center) if self.center else None, "radius_km": self.radius_km,
                "source": self.source}

    @classmethod
    def from_dict(cls, d: dict) -> "Region":
        return cls(name=d["name"], kind=d["kind"], bbox=tuple(d["bbox"]), polygons=d.get("polygons") or [],
                   center=tuple(d["center"]) if d.get("center") else None, radius_km=d.get("radius_km"),
                   source=d.get("source", "cache"))


def _simplify(ring: list, max_pts: int = 400) -> list:
    if len(ring) <= max_pts:
        return ring
    step = math.ceil(len(ring) / max_pts)
    out = ring[::step]
    if out[-1] != ring[-1]:
        out.append(ring[-1])
    return out


def _geojson_to_polygons(geom: dict) -> list:
    t = geom.get("type")
    if t == "Polygon":
        polys = [geom["coordinates"]]
    elif t == "MultiPolygon":
        polys = geom["coordinates"]
    else:
        return []
    return [[_simplify(r) for r in p] for p in polys]


# Offline fallback gazetteer (coarse bounding boxes: south, west, north, east).
# Used when Nominatim is unreachable; a polygon from Nominatim is always preferred.
GAZETTEER: dict[str, tuple[str, tuple[float, float, float, float]]] = {
    "new zealand": ("country", (-47.4, 166.3, -34.3, 178.6)),
    "england": ("country", (49.9, -5.8, 55.9, 1.8)),
    "united kingdom": ("country", (49.8, -8.7, 60.9, 1.8)),
    "scotland": ("country", (54.6, -7.7, 60.9, -0.7)),
    "ireland": ("country", (51.4, -10.5, 55.4, -6.0)),
    "france": ("country", (42.3, -4.8, 51.1, 8.2)),
    "germany": ("country", (47.3, 5.9, 55.1, 15.0)),
    "spain": ("country", (36.0, -9.4, 43.8, 3.3)),
    "italy": ("country", (36.6, 6.6, 47.1, 18.5)),
    "netherlands": ("country", (50.8, 3.3, 53.6, 7.2)),
    "india": ("country", (6.7, 68.1, 35.5, 97.4)),
    "australia": ("country", (-43.7, 113.3, -10.6, 153.6)),
    "united states": ("country", (24.5, -125.0, 49.4, -66.9)),
    "canada": ("country", (41.7, -141.0, 70.0, -52.6)),
    "japan": ("country", (30.0, 129.4, 45.6, 145.9)),
    "china": ("country", (18.2, 73.5, 53.6, 134.8)),
    "singapore": ("country", (1.16, 103.6, 1.48, 104.1)),
    "united arab emirates": ("country", (22.6, 51.5, 26.1, 56.4)),
    "brazil": ("country", (-33.8, -73.99, 5.3, -34.8)),
    "south africa": ("country", (-34.9, 16.4, -22.1, 32.9)),
    "london": ("city", (51.28, -0.51, 51.69, 0.33)),
    "auckland": ("city", (-37.1, 174.5, -36.6, 175.0)),
    "bengaluru": ("city", (12.83, 77.46, 13.14, 77.78)),
    "mumbai": ("city", (18.89, 72.77, 19.27, 72.99)),
    "delhi": ("city", (28.40, 76.84, 28.88, 77.35)),
    "new york": ("city", (40.48, -74.26, 40.92, -73.70)),
    "paris": ("city", (48.81, 2.22, 48.90, 2.47)),
    "dubai": ("city", (24.79, 54.89, 25.36, 55.56)),
    "sydney": ("city", (-34.12, 150.52, -33.58, 151.34)),
    "tokyo": ("city", (35.52, 139.56, 35.82, 139.92)),
}

ALIASES = {
    "newzealand": "new zealand", "newzeland": "new zealand", "new zeland": "new zealand", "nz": "new zealand",
    "uk": "united kingdom", "u.k.": "united kingdom", "britain": "united kingdom", "great britain": "united kingdom",
    "usa": "united states", "us": "united states", "america": "united states", "u.s.": "united states",
    "uae": "united arab emirates", "bangalore": "bengaluru", "new delhi": "delhi", "nyc": "new york",
    "holland": "netherlands",
}


def normalize_place(name: str) -> str:
    n = re.sub(r"\s+", " ", name.strip().lower()).strip(" ?.!,")
    n = re.sub(r"^the ", "", n)
    return ALIASES.get(n, n)


class Geocoder:
    def __init__(self, store: Store):
        self.store = store
        self._last_call = 0.0

    def _nominatim(self, params: dict) -> list:
        # Nominatim usage policy: max 1 request/second and an identifying User-Agent.
        wait = 1.1 - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()
        params = {"format": "jsonv2", "polygon_geojson": 1, "polygon_threshold": 0.01, "limit": 1, **params}
        r = httpx.get(f"{config.NOMINATIM_URL}/search", params=params, timeout=20,
                      headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        return r.json()

    def resolve(self, name: str, kind: str | None = None, country_code: str | None = None,
                radius_km: float | None = None) -> Region:
        """Resolve a free-text place (country, city, area, postcode) into a Region."""
        norm = normalize_place(name)
        is_postcode = kind == "postcode" or bool(re.fullmatch(r"[0-9]{4,6}|[a-z]{1,2}[0-9][a-z0-9]? ?[0-9][a-z]{2}", norm))
        cache_key = f"{norm}|{kind or ''}|{country_code or ''}|{radius_km or ''}"
        cached = self.store.geocache_get(cache_key)
        if cached:
            return Region.from_dict(cached)

        region = None
        try:
            if is_postcode:
                params = {"postalcode": norm.upper()}
                if country_code:
                    params["countrycodes"] = country_code.lower()
                hits = self._nominatim(params)
            else:
                params = {"q": norm}
                if country_code:
                    params["countrycodes"] = country_code.lower()
                if kind == "country":
                    params["featureType"] = "country"
                hits = self._nominatim(params)
            if hits:
                region = self._from_hit(hits[0], name, "postcode" if is_postcode else kind, radius_km)
        except Exception as exc:  # network blocked/offline: fall back below
            print(f"[geo] nominatim failed for {name!r}: {exc}")

        if region is None:
            if norm in GAZETTEER:
                k, bbox = GAZETTEER[norm]
                region = Region(name=norm.title(), kind=kind or k, bbox=bbox, source="builtin")
                if radius_km:
                    region.center, region.radius_km = region.centroid(), radius_km
            else:
                raise LookupError(f"Couldn't find a place called '{name}'.")

        self.store.geocache_put(cache_key, region.to_dict())
        return region

    def _from_hit(self, hit: dict, query: str, kind: str | None, radius_km: float | None) -> Region:
        s, n, w, e = (float(x) for x in hit["boundingbox"])
        lat, lon = float(hit["lat"]), float(hit["lon"])
        addrtype = hit.get("addresstype") or hit.get("type") or ""
        if not kind:
            kind = "country" if addrtype == "country" else "city" if addrtype in ("city", "town", "village", "municipality") else "area"
        display = hit.get("name") or hit.get("display_name", query).split(",")[0]
        polys = _geojson_to_polygons(hit.get("geojson") or {})
        region = Region(name=display, kind=kind, bbox=(s, w, n, e), polygons=polys, center=(lat, lon))
        # Postcodes (and Indian PIN codes) usually come back as a point or a tiny box.
        # Treat them as a circle so "how many flights over 560001" has a sensible area.
        tiny = haversine_km(s, w, n, e) < 2
        if radius_km or (kind == "postcode" and (not polys or tiny)):
            region.polygons = []
            region.radius_km = radius_km or config.POSTCODE_DEFAULT_RADIUS_KM
            region.center = (lat, lon)
            r = region.radius_km / 111.0
            region.bbox = (lat - r, lon - r / max(math.cos(math.radians(lat)), 0.1),
                           lat + r, lon + r / max(math.cos(math.radians(lat)), 0.1))
        elif not polys:
            region.center = None  # bbox-only region
        return region


def region_from_bbox(name: str, s: float, w: float, n: float, e: float) -> Region:
    return Region(name=name, kind="custom", bbox=(s, w, n, e), source="custom")


def filter_inside(region: Region, states: Iterable[dict]) -> list[dict]:
    return [a for a in states if a.get("lat") is not None and region.contains(a["lat"], a["lon"])]


def is_inbound(region: Region, a: dict, horizon_min: int = 120, step_min: int = 10) -> bool:
    """Aircraft is outside the region but its current track enters it within `horizon_min`.

    State vectors don't carry a destination, so we dead-reckon along the current
    heading at current ground speed. This is a heuristic proxy for "flights to X".
    """
    if a.get("on_ground") or a.get("lat") is None or a.get("heading") is None or not a.get("velocity"):
        return False
    if region.contains(a["lat"], a["lon"]):
        return False
    km_per_step = a["velocity"] * 60 * step_min / 1000.0
    # Cheap reject: too far away to arrive within the horizon.
    clat, clon = region.centroid()
    s, w, n, e = region.bbox
    span = haversine_km(s, w, n, e) / 2
    if haversine_km(a["lat"], a["lon"], clat, clon) - span > km_per_step * (horizon_min / step_min):
        return False
    lat, lon = a["lat"], a["lon"]
    for _ in range(horizon_min // step_min):
        lat, lon = destination(lat, lon, a["heading"], km_per_step)
        if region.contains(lat, lon):
            return True
    return False


def region_to_json(region: Region) -> str:
    return json.dumps(region.to_dict())
