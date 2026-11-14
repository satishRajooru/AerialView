"""Flight data sources. Each returns a list of normalized aircraft dicts:

{icao24, callsign, origin_country, lat, lon, alt_m, on_ground, velocity (m/s),
 heading (deg), vertical_rate (m/s), squawk, last_contact}
"""
from __future__ import annotations

import math
import random
import time

import httpx

from . import config
from .geo import destination, haversine_km


class OpenSkySource:
    """OpenSky Network REST API, /states/all (global snapshot)."""

    name = "opensky"

    def __init__(self):
        self._token = None
        self._token_exp = 0.0

    def _auth_headers(self) -> dict:
        if not (config.OPENSKY_CLIENT_ID and config.OPENSKY_CLIENT_SECRET):
            return {}
        if not self._token or time.time() > self._token_exp - 60:
            r = httpx.post(config.OPENSKY_TOKEN_URL, data={
                "grant_type": "client_credentials",
                "client_id": config.OPENSKY_CLIENT_ID,
                "client_secret": config.OPENSKY_CLIENT_SECRET}, timeout=20)
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_exp = time.time() + body.get("expires_in", 1800)
        return {"Authorization": f"Bearer {self._token}"}

    def fetch(self, bbox: tuple[float, float, float, float] | None = None) -> tuple[int, list[dict]]:
        params = {}
        if bbox:
            s, w, n, e = bbox
            params = {"lamin": s, "lomin": w, "lamax": n, "lomax": e}
        r = httpx.get(f"{config.OPENSKY_URL}/states/all", params=params,
                      headers={"User-Agent": config.USER_AGENT, **self._auth_headers()}, timeout=60)
        if r.status_code == 429:
            raise RuntimeError("OpenSky rate limit hit (429). Add OPENSKY_CLIENT_ID/SECRET or raise AM_POLL_SECONDS.")
        r.raise_for_status()
        body = r.json()
        out = []
        for s in body.get("states") or []:
            if s[5] is None or s[6] is None:
                continue
            out.append({
                "icao24": s[0], "callsign": (s[1] or "").strip(), "origin_country": s[2],
                "lon": s[5], "lat": s[6], "alt_m": s[7] if s[7] is not None else s[13],
                "on_ground": bool(s[8]), "velocity": s[9], "heading": s[10], "vertical_rate": s[11],
                "squawk": s[14], "last_contact": s[4],
            })
        return int(body.get("time") or time.time()), out


# ---------------------------------------------------------------------------
# adsb.lol: free, keyless, community ADS-B network (data under ODbL). It has no
# "everything" endpoint, but /v2/type/{icao_type} is global, so the union over the
# common airliner types gives a worldwide picture of commercial traffic. Point
# queries (<=250 nm) return *all* aircraft near a spot and are used for small areas.
AIRLINER_TYPES = [
    "A20N", "A21N", "A319", "A320", "A321", "A332", "A333", "A339", "A359", "A35K", "A388", "A220",
    "BCS1", "BCS3", "B737", "B738", "B739", "B38M", "B39M", "B3XM", "B744", "B748", "B752", "B753", "B763",
    "B764", "B772", "B77L", "B77W", "B788", "B789", "B78X", "E170", "E175", "E75L", "E75S", "E190", "E195",
    "E290", "E295", "CRJ2", "CRJ7", "CRJ9", "CRJX", "AT72", "AT75", "AT76", "DH8D", "MD11", "A306", "C919",
]
KT_TO_MS = 0.514444
FT_TO_M = 0.3048


def _norm_readsb(a: dict) -> dict | None:
    if a.get("lat") is None or a.get("lon") is None:
        return None
    alt = a.get("alt_baro")
    on_ground = alt == "ground"
    alt_m = 0 if on_ground else (alt * FT_TO_M if isinstance(alt, (int, float)) else
                                 (a["alt_geom"] * FT_TO_M if isinstance(a.get("alt_geom"), (int, float)) else None))
    rate = a.get("baro_rate", a.get("geom_rate"))
    desc = " · ".join(x for x in (a.get("t"), a.get("r")) if x)
    return {"icao24": a.get("hex", "").lstrip("~"), "callsign": (a.get("flight") or "").strip(),
            "origin_country": None, "desc": desc or None, "lat": a["lat"], "lon": a["lon"],
            "alt_m": round(alt_m) if alt_m is not None else None, "on_ground": on_ground,
            "velocity": a["gs"] * KT_TO_MS if a.get("gs") is not None else None, "heading": a.get("track"),
            "vertical_rate": rate * FT_TO_M / 60 if isinstance(rate, (int, float)) else None,
            "squawk": a.get("squawk"), "last_contact": None}


class RateLimited(Exception):
    pass


class AdsbLolSource:
    """Global airliner snapshot from adsb.lol, gentle on its rate limit.

    One request per aircraft type, a few at a time. The first 429 stops further
    requests for this round; any type that couldn't be fetched is filled from its
    last good result (kept up to TYPE_CACHE_SECONDS), so the map never loses whole
    aircraft families because of throttling."""

    name = "adsb.lol"
    base = "https://api.adsb.lol/v2"
    TYPE_CACHE_SECONDS = 900

    def __init__(self):
        self.http = httpx.Client(timeout=12, headers={"User-Agent": config.USER_AGENT})
        self.last_errors: list[str] = []
        self.type_cache: dict[str, tuple[float, list[dict]]] = {}
        self.coverage = {"fresh": 0, "cached": 0, "missing": 0}

    def _get(self, path: str) -> list[dict]:
        r = self.http.get(f"{self.base}{path}")
        if r.status_code == 429:
            raise RateLimited(path)
        r.raise_for_status()
        return r.json().get("ac") or []

    def fetch(self, bbox=None) -> tuple[int, list[dict]]:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        throttled = threading.Event()
        errors: list[str] = []
        now = time.time()

        def one(t):
            if throttled.is_set():
                return t, None
            try:
                return t, self._get(f"/type/{t}")
            except RateLimited:
                throttled.set()
                errors.append(f"{t}: rate limited")
            except Exception as exc:
                errors.append(f"{t}: {type(exc).__name__}")
            return t, None

        with ThreadPoolExecutor(max_workers=config.ADSBLOL_CONCURRENCY) as pool:
            results = list(pool.map(one, AIRLINER_TYPES))

        seen: dict[str, dict] = {}
        cov = {"fresh": 0, "cached": 0, "missing": 0}
        for t, raw in results:
            if raw is not None:
                states = [a for x in raw if (a := _norm_readsb(x)) and a["icao24"]]
                self.type_cache[t] = (now, states)
                cov["fresh"] += 1
            elif t in self.type_cache and now - self.type_cache[t][0] < self.TYPE_CACHE_SECONDS:
                states = self.type_cache[t][1]
                cov["cached"] += 1
            else:
                cov["missing"] += 1
                continue
            for a in states:
                seen[a["icao24"]] = a
        self.last_errors, self.coverage = errors, cov
        if not seen:
            raise RuntimeError(f"adsb.lol unavailable ({errors[0] if errors else 'no data'})")
        out = list(seen.values())
        if bbox:
            s, w, n, e = bbox
            out = [a for a in out if s <= a["lat"] <= n and w <= a["lon"] <= e]
        return int(now), out

    def point(self, lat: float, lon: float, radius_nm: float) -> list[dict]:
        """All aircraft (incl. GA/helicopters) within radius_nm (max 250) of a point."""
        raw = self._get(f"/point/{lat:.4f}/{lon:.4f}/{min(int(math.ceil(radius_nm)), 250)}")
        return [a for x in raw if (a := _norm_readsb(x))]


# ---------------------------------------------------------------------------
# Demo source: deterministic synthetic traffic between real airports, so the
# whole stack (map, queries, spike detection) can be exercised offline.
AIRPORTS = {
    "LHR": (51.47, -0.45, "United Kingdom"), "LGW": (51.15, -0.19, "United Kingdom"),
    "MAN": (53.35, -2.27, "United Kingdom"), "EDI": (55.95, -3.37, "United Kingdom"),
    "CDG": (49.01, 2.55, "France"), "FRA": (50.03, 8.57, "Germany"), "MUC": (48.35, 11.79, "Germany"),
    "AMS": (52.31, 4.76, "Netherlands"), "MAD": (40.47, -3.56, "Spain"), "FCO": (41.80, 12.25, "Italy"),
    "DXB": (25.25, 55.36, "United Arab Emirates"), "DOH": (25.27, 51.61, "Qatar"),
    "DEL": (28.56, 77.10, "India"), "BOM": (19.09, 72.87, "India"), "BLR": (13.20, 77.71, "India"),
    "SIN": (1.36, 103.99, "Singapore"), "HKG": (22.31, 113.91, "China"), "PEK": (40.08, 116.58, "China"),
    "HND": (35.55, 139.78, "Japan"), "NRT": (35.77, 140.39, "Japan"), "ICN": (37.46, 126.44, "South Korea"),
    "SYD": (-33.95, 151.18, "Australia"), "MEL": (-37.67, 144.84, "Australia"), "BNE": (-27.38, 153.12, "Australia"),
    "AKL": (-37.01, 174.79, "New Zealand"), "WLG": (-41.33, 174.81, "New Zealand"), "CHC": (-43.49, 172.53, "New Zealand"),
    "JFK": (40.64, -73.78, "United States"), "LAX": (33.94, -118.41, "United States"), "ORD": (41.97, -87.91, "United States"),
    "ATL": (33.64, -84.43, "United States"), "SFO": (37.62, -122.38, "United States"), "DFW": (32.90, -97.04, "United States"),
    "YYZ": (43.68, -79.63, "Canada"), "YVR": (49.19, -123.18, "Canada"), "GRU": (-23.43, -46.47, "Brazil"),
    "JNB": (-26.14, 28.25, "South Africa"), "NBO": (-1.32, 36.93, "Kenya"), "IST": (41.26, 28.74, "Turkey"),
    "MEX": (19.44, -99.07, "Mexico"),
}
DAY = 86400


class DemoSource:
    name = "demo"

    def __init__(self, n_flights: int = config.DEMO_FLIGHTS, seed: int = 7, spike_airport: str = config.DEMO_SPIKE):
        rng = random.Random(seed)
        codes = list(AIRPORTS)
        # Hubs get more traffic.
        weights = [3 if c in ("LHR", "DXB", "ATL", "FRA", "SIN", "JFK", "HND", "DEL", "CDG", "LAX") else 1 for c in codes]
        self.flights = []
        for i in range(n_flights):
            o, d = rng.choices(codes, weights)[0], rng.choices(codes, weights)[0]
            while d == o:
                d = rng.choice(codes)
            self.flights.append(self._mk(i, o, d, rng, 0))
        self.spike_flights = []
        self.spike_start = time.time() - 1800  # spike begins 30 min before the server started
        if spike_airport and spike_airport in AIRPORTS:
            for j in range(int(n_flights * 0.02)):
                o = rng.choice([c for c in codes if c != spike_airport])
                self.spike_flights.append(self._mk(n_flights + j, o, spike_airport, rng, 0))

    @staticmethod
    def _mk(i, o, d, rng, _):
        dist = haversine_km(*AIRPORTS[o][:2], *AIRPORTS[d][:2])
        speed_kmh = rng.uniform(780, 900)
        return {"icao24": f"{i:06x}", "callsign": f"DMO{i:04d}", "o": o, "d": d, "dist": dist,
                "dur": dist / speed_kmh * 3600, "speed": speed_kmh / 3.6,
                "offset": rng.uniform(0, DAY), "cruise": rng.choice([10058, 10668, 11278, 11887])}

    @staticmethod
    def _position(f, frac):
        lat1, lon1 = AIRPORTS[f["o"]][:2]
        lat2, lon2 = AIRPORTS[f["d"]][:2]
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dl = math.radians(lon2 - lon1)
        brg = math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                      math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360
        lat, lon = destination(lat1, lon1, brg, f["dist"] * frac)
        # heading at the current point = initial bearing from current point to destination
        p1, dl = math.radians(lat), math.radians(lon2 - lon)
        hdg = math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                      math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360
        return lat, lon, hdg

    def _state(self, f, t):
        # Each route flies once a day; diurnal shaping: fewer departures 01:00-05:00 local-ish.
        phase = (t - f["offset"]) % DAY
        if phase > f["dur"]:
            return None
        frac = phase / f["dur"]
        lat, lon, hdg = self._position(f, frac)
        climb = min(frac * f["dur"] / 1200, 1, (1 - frac) * f["dur"] / 1500)
        return {"icao24": f["icao24"], "callsign": f["callsign"], "origin_country": AIRPORTS[f["o"]][2],
                "lat": lat, "lon": lon, "alt_m": round(f["cruise"] * climb), "on_ground": False,
                "velocity": f["speed"], "heading": hdg, "vertical_rate": 0.0, "squawk": None,
                "last_contact": int(t), "route": f"{f['o']}-{f['d']}"}

    def states_at(self, t: float) -> list[dict]:
        out = [s for f in self.flights if (s := self._state(f, t))]
        if t >= self.spike_start:
            for f in self.spike_flights:
                # Spike flights all depart within the spike window so they're airborne now.
                g = dict(f, offset=self.spike_start - f["dur"] * (int(f["icao24"], 16) % 97) / 97)
                if (s := self._state(g, t)):
                    out.append(s)
        return out

    def fetch(self, bbox=None) -> tuple[int, list[dict]]:
        t = int(time.time())
        states = self.states_at(t)
        if bbox:
            s, w, n, e = bbox
            states = [a for a in states if s <= a["lat"] <= n and w <= a["lon"] <= e]
        return t, states


def make_source():
    if config.SOURCE == "demo":
        return DemoSource()
    if config.SOURCE == "opensky":
        return OpenSkySource()
    return AdsbLolSource()
