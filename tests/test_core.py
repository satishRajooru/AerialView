import time

import httpx

from app import geo, sources
from app.geo import Geocoder, Region, is_inbound
from app.monitor import Monitor
from app.query import QueryEngine, rule_parse
from app.store import Store


class Resp:
    def __init__(self, body, code=200):
        self._b, self.status_code = body, code

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def test_rule_parser():
    qi = rule_parse("Do we see any unusual spike to newzeland or england?")
    assert qi.intent == "spike" and [p.name for p in qi.places] == ["newzeland", "england"]
    qi = rule_parse("how many aircraft over pincode 560001")
    assert qi.intent == "count" and qi.places[0].kind == "postcode" and qi.places[0].country_code == "in"
    qi = rule_parse("How many planes within 100 km of Dubai above 30000 ft?")
    assert qi.places[0].radius_km == 100 and qi.min_alt_ft == 30000
    assert rule_parse("any unusual spikes?").intent == "spike_scan"


def test_opensky_parse(monkeypatch):
    row = ["abc123", "ANZ1  ", "New Zealand", 1, 2, 174.8, -37.0, 10000.0, False, 250.0, 90.0, 0.0, None, 10100.0, "1234", False, 0]
    monkeypatch.setattr(sources.httpx, "get", lambda *a, **k: Resp({"time": 100, "states": [row, row[:5] + [None, None] + row[7:]]}))
    ts, st = sources.OpenSkySource().fetch()
    assert ts == 100 and len(st) == 1
    a = st[0]
    assert a["callsign"] == "ANZ1" and a["lat"] == -37.0 and a["lon"] == 174.8 and a["heading"] == 90.0


def test_postcode_becomes_circle(monkeypatch, tmp_path):
    hit = {"lat": "12.97", "lon": "77.59", "boundingbox": ["12.969", "12.971", "77.589", "77.591"],
           "addresstype": "postcode", "name": "560001", "geojson": {"type": "Point", "coordinates": [77.59, 12.97]}}
    monkeypatch.setattr(geo.httpx, "get", lambda *a, **k: Resp([hit]))
    r = Geocoder(Store(str(tmp_path / "db"))).resolve("560001", kind="postcode", country_code="in")
    assert r.radius_km == 10 and r.contains(12.97, 77.65) and not r.contains(13.2, 77.59)


def test_polygon_and_inbound():
    sq = [[[170, -40], [175, -40], [175, -35], [170, -35], [170, -40]]]
    r = Region("Box", "area", (-40, 170, -35, 175), polygons=[sq])
    assert r.contains(-37, 172) and not r.contains(-37, 176)
    plane = {"lat": -37, "lon": 165, "heading": 90, "velocity": 250, "on_ground": False}
    assert is_inbound(r, plane)
    assert not is_inbound(r, dict(plane, heading=270))


def test_spike_detection(tmp_path):
    store = Store(str(tmp_path / "db"))
    region = Region("Box", "area", (-40, 170, -35, 175))
    now = int(time.time())

    class Src:
        name = "test"
        def fetch(self, bbox=None):
            return now, [{"icao24": str(i), "callsign": "", "lat": -37, "lon": 172, "on_ground": False,
                          "heading": 0, "velocity": 200, "alt_m": 10000} for i in range(40)]

    m = Monitor(store, Geocoder(store), Src())
    m.regions[region.key] = region
    store.add_counts(0, [])
    for d in range(1, 8):  # same time of day on past 7 days: ~20 aircraft
        store.add_counts(now - d * 86400, [(region.key, 20 + d % 3, 0)])
    m.poll_once()
    s = m.spike(region)
    assert s["status"] == "spike" and s["current"] == 40

    eng = QueryEngine(m)
    m.geo.resolve = lambda *a, **k: region
    assert "40 aircraft" in eng.ask("how many flights over box")["answer"]


def test_serverless_fallback_to_demo(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setattr(config, "SERVERLESS", True)
    monkeypatch.setattr(config, "FALLBACK_TO_DEMO", True)

    class Broken:
        name = "opensky"
        def fetch(self, bbox=None):
            raise RuntimeError("429")

    store = Store(str(tmp_path / "db"))
    m = Monitor(store, Geocoder(store), Broken())
    m.ensure_fresh()
    assert m.using_fallback and m.states and "429" in m.last_error
    assert store.meta("last_record") is None  # synthetic data never enters real history


def test_vercel_rewrite_fix_restores_path():
    import asyncio

    from app.main import VercelRewriteFix

    seen = {}

    async def fake(scope, receive, send):
        seen.update(path=scope["path"], qs=scope["query_string"])

    mw = VercelRewriteFix(fake)
    asyncio.run(mw({"type": "http", "path": "/api/index", "query_string": b"s=1&__path=%2Fapi%2Fstates&w=2"}, None, None))
    assert seen == {"path": "/api/states", "qs": b"s=1&w=2"}
    asyncio.run(mw({"type": "http", "path": "/api/index", "query_string": b"__path=%2F"}, None, None))
    assert seen["path"] == "/"
    asyncio.run(mw({"type": "http", "path": "/api/status", "query_string": b""}, None, None))
    assert seen["path"] == "/api/status"


def test_adsblol_source_merges_types(monkeypatch):
    calls = []

    def fake_get(self, path):
        calls.append(path)
        if path == "/type/B738":
            return [{"hex": "abc", "flight": "ASA1 ", "t": "B738", "r": "N1", "lat": 40, "lon": -74,
                     "alt_baro": 36000, "gs": 450, "track": 90, "baro_rate": -600}]
        if path == "/type/A320":
            return [{"hex": "def", "lat": 51, "lon": 0, "alt_baro": "ground", "gs": 5},
                    {"hex": "nopos"}]
        return []

    monkeypatch.setattr(sources.AdsbLolSource, "_get", fake_get)
    _, st = sources.AdsbLolSource().fetch()
    by = {a["icao24"]: a for a in st}
    assert set(by) == {"abc", "def"} and len(calls) == len(sources.AIRLINER_TYPES)
    assert by["abc"]["callsign"] == "ASA1" and round(by["abc"]["alt_m"]) == 10973 and by["abc"]["desc"] == "B738 · N1"
    assert by["def"]["on_ground"] and by["def"]["alt_m"] == 0


def test_adsblol_rate_limit_uses_cache(monkeypatch):
    src = sources.AdsbLolSource()
    good = [{"hex": "a1", "lat": 1, "lon": 2, "alt_baro": 30000}]
    monkeypatch.setattr(sources.config, "ADSBLOL_CONCURRENCY", 1)
    monkeypatch.setattr(sources.AdsbLolSource, "_get", lambda self, p: good)
    src.fetch()
    calls = []

    def throttled(self, path):
        calls.append(path)
        raise sources.RateLimited(path)

    monkeypatch.setattr(sources.AdsbLolSource, "_get", throttled)
    _, st = src.fetch()
    assert len(calls) == 1  # stops after the first 429
    assert [a["icao24"] for a in st] == ["a1"] and src.coverage["cached"] == len(sources.AIRLINER_TYPES)
