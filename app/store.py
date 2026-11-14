"""SQLite persistence: region counts per snapshot (for baselines) and a geocode cache."""
from __future__ import annotations

import json
import sqlite3
import threading
import time


class Store:
    persistent = True

    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS region_counts (
                    ts INTEGER NOT NULL, region_key TEXT NOT NULL,
                    inside INTEGER NOT NULL, inbound INTEGER NOT NULL,
                    PRIMARY KEY (region_key, ts));
                CREATE TABLE IF NOT EXISTS geocache (key TEXT PRIMARY KEY, region TEXT NOT NULL, ts INTEGER);
                CREATE TABLE IF NOT EXISTS watchlist (region_key TEXT PRIMARY KEY, region TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (ts INTEGER PRIMARY KEY, total INTEGER, airborne INTEGER);
                CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
                """
            )

    def add_counts(self, ts: int, rows: list[tuple[str, int, int]]):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO region_counts VALUES (?,?,?,?)",
                                [(ts, k, i, b) for k, i, b in rows])
            self.db.commit()

    def add_snapshot(self, ts: int, total: int, airborne: int):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?)", (ts, total, airborne))
            self.db.commit()

    def counts(self, region_key: str, since: int) -> list[tuple[int, int, int]]:
        with self.lock:
            return self.db.execute(
                "SELECT ts, inside, inbound FROM region_counts WHERE region_key=? AND ts>=? ORDER BY ts",
                (region_key, since)).fetchall()

    def prune(self, keep_days: int):
        with self.lock:
            cutoff = int(time.time()) - keep_days * 86400
            self.db.execute("DELETE FROM region_counts WHERE ts<?", (cutoff,))
            self.db.execute("DELETE FROM snapshots WHERE ts<?", (cutoff,))
            self.db.commit()

    def geocache_get(self, key: str):
        with self.lock:
            row = self.db.execute("SELECT region FROM geocache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def geocache_put(self, key: str, region: dict):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO geocache VALUES (?,?,?)",
                            (key, json.dumps(region), int(time.time())))
            self.db.commit()

    def watchlist(self) -> list[dict]:
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT region FROM watchlist").fetchall()]

    def meta(self, key: str, value=None):
        with self.lock:
            if value is None:
                row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
                return row[0] if row else None
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
            self.db.commit()

    def watch(self, key: str, region: dict):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO watchlist VALUES (?,?)", (key, json.dumps(region)))
            self.db.commit()


class RedisStore:
    """Same interface as Store, backed by Upstash Redis over its REST API (no extra deps)."""

    persistent = True

    def __init__(self, url: str, token: str, prefix: str = "am:"):
        import httpx

        self.http = httpx.Client(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=15)
        self.p = prefix

    def _pipe(self, cmds: list[list]) -> list:
        if not cmds:
            return []
        r = self.http.post("/pipeline", json=[[str(x) for x in c] for c in cmds])
        r.raise_for_status()
        out = r.json()
        for item in out:
            if "error" in item:
                raise RuntimeError(f"Redis error: {item['error']}")
        return [item.get("result") for item in out]

    def add_counts(self, ts: int, rows: list[tuple[str, int, int]]):
        self._pipe([["ZADD", f"{self.p}counts:{k}", ts, f"{ts}:{i}:{b}"] for k, i, b in rows])

    def add_snapshot(self, ts: int, total: int, airborne: int):
        self._pipe([["ZADD", f"{self.p}snapshots", ts, f"{ts}:{total}:{airborne}"]])

    def counts(self, region_key: str, since: int) -> list[tuple[int, int, int]]:
        (res,) = self._pipe([["ZRANGEBYSCORE", f"{self.p}counts:{region_key}", since, "+inf"]])
        out = []
        for m in res or []:
            t, i, b = m.split(":")
            out.append((int(t), int(i), int(b)))
        return out

    def prune(self, keep_days: int):
        cutoff = int(time.time()) - keep_days * 86400
        keys = self._pipe([["HKEYS", f"{self.p}watchlist"]])[0] or []
        self._pipe([["ZREMRANGEBYSCORE", f"{self.p}counts:{k}", "-inf", cutoff] for k in keys]
                   + [["ZREMRANGEBYSCORE", f"{self.p}snapshots", "-inf", cutoff]])

    def geocache_get(self, key: str):
        (v,) = self._pipe([["GET", f"{self.p}geo:{key}"]])
        return json.loads(v) if v else None

    def geocache_put(self, key: str, region: dict):
        self._pipe([["SET", f"{self.p}geo:{key}", json.dumps(region), "EX", 90 * 86400]])

    def watchlist(self) -> list[dict]:
        (res,) = self._pipe([["HVALS", f"{self.p}watchlist"]])
        return [json.loads(v) for v in res or []]

    def watch(self, key: str, region: dict):
        self._pipe([["HSET", f"{self.p}watchlist", key, json.dumps(region)]])

    def meta(self, key, value=None):
        if value is None:
            return self._pipe([["GET", f"{self.p}meta:{key}"]])[0]
        self._pipe([["SET", f"{self.p}meta:{key}", value]])


def make_store():
    from . import config

    if config.REDIS_URL and config.REDIS_TOKEN:
        return RedisStore(config.REDIS_URL, config.REDIS_TOKEN)
    s = Store(config.DB_PATH)
    s.persistent = not config.SERVERLESS  # /tmp on serverless is wiped between cold starts
    return s
