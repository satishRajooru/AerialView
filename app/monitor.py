"""Collector: polls the source, keeps the latest global snapshot in memory, and
records per-region counts (inside + inbound) so spikes can be judged against history."""
from __future__ import annotations

import statistics
import threading
import time
import traceback

from . import config
from .geo import Geocoder, Region, filter_inside, is_inbound
from .sources import DemoSource, make_source
from .store import Store


class Monitor:
    def __init__(self, store: Store, geocoder: Geocoder, source=None):
        self.store = store
        self.geo = geocoder
        self.source = source or make_source()
        self.snapshot_ts = 0
        self.states: list[dict] = []
        self.last_error: str | None = None
        self.regions: dict[str, Region] = {}
        self.live_source = self.source
        self.using_fallback = False
        self._watchlist_loaded = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._started = False

    # -- watchlist ---------------------------------------------------------
    def load_watchlist(self, resolve_defaults: bool = True):
        """Load watched regions from the store; geocode the default list if it's empty."""
        if self._watchlist_loaded:
            return
        for d in self.store.watchlist():
            r = Region.from_dict(d)
            self.regions.setdefault(r.key, r)
        if not resolve_defaults:
            return
        self._watchlist_loaded = True
        if not self.regions:
            for name in config.DEFAULT_WATCHLIST:
                try:
                    self.watch(self.geo.resolve(name))
                except Exception as exc:
                    print(f"[monitor] could not resolve watchlist place {name!r}: {exc}")

    def watch(self, region: Region) -> Region:
        if region.key not in self.regions:
            self.regions[region.key] = region
            self.store.watch(region.key, region.to_dict())
            # Record a point immediately so history starts now.
            if self.states:
                self._record(self.snapshot_ts, self.states, [region])
        return self.regions[region.key]

    # -- collection --------------------------------------------------------
    def count(self, region: Region, states: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
        states = self.states if states is None else states
        inside = filter_inside(region, states)
        inbound = [a for a in states if is_inbound(region, a)]
        return inside, inbound

    def _record(self, ts: int, states: list[dict], regions: list[Region]):
        rows = []
        for r in regions:
            inside, inbound = self.count(r, states)
            rows.append((r.key, len(inside), len(inbound)))
        self.store.add_counts(ts, rows)

    def poll_once(self, record: bool = True):
        try:
            ts, states = self.live_source.fetch()
            self.source, self.using_fallback = self.live_source, False
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            if not (config.FALLBACK_TO_DEMO and not isinstance(self.live_source, DemoSource)):
                raise
            # Live feed unreachable (rate limit, blocked IP...): show demo data rather than an empty map.
            if not isinstance(self.source, DemoSource):
                self.source = DemoSource()
            self.using_fallback = True
            ts, states = self.source.fetch()
        with self._lock:
            self.snapshot_ts, self.states = ts, states
        if record and not self.using_fallback:  # never mix synthetic counts into real history
            self.store.add_snapshot(ts, len(states), sum(1 for a in states if not a["on_ground"]))
            self._record(ts, states, list(self.regions.values()))
            self.store.meta("last_record", ts)

    # -- serverless --------------------------------------------------------
    def ensure_fresh(self, force_record: bool = False):
        """Serverless: fetch a snapshot if ours is stale; record history at most every RECORD_INTERVAL."""
        if not config.SERVERLESS:
            return
        now = time.time()
        if self.states and now - self.snapshot_ts < config.SNAPSHOT_TTL and not force_record:
            return
        with self._fetch_lock:
            if self.states and time.time() - self.snapshot_ts < config.SNAPSHOT_TTL and not force_record:
                return
            last = float(self.store.meta("last_record") or 0)
            record = force_record or (now - last >= config.RECORD_INTERVAL)
            if record:
                # Page loads only use regions already stored (cheap); /api/collect and
                # spike scans resolve the default watchlist, which may need geocoding.
                self.load_watchlist(resolve_defaults=force_record)
            self.poll_once(record=record)
            if record:
                self.store.prune(config.HISTORY_DAYS)

    def backfill_demo(self):
        """Demo mode only: synthesize history so baselines exist on first run."""
        if not isinstance(self.source, DemoSource) or config.DEMO_BACKFILL_DAYS <= 0:
            return
        now = int(time.time())
        start = now - config.DEMO_BACKFILL_DAYS * 86400
        if any(self.store.counts(r.key, start) for r in self.regions.values()):
            return
        step = 3 * 3600 if config.SERVERLESS else 3600  # keep cold starts fast on serverless
        print(f"[monitor] demo: backfilling {config.DEMO_BACKFILL_DAYS} days of history...")
        for t in range(start, now - 3600, step):
            self._record(t, self.source.states_at(t), list(self.regions.values()))
        self.store.meta("last_record", now - 3600)

    def run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
                self.store.prune(config.HISTORY_DAYS)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            self._stop.wait(config.POLL_SECONDS if self.source.name != "demo" else 30)

    def start(self):
        if self._started:
            return
        self._started = True
        if config.SERVERLESS:
            if isinstance(self.source, DemoSource):
                self.load_watchlist()
                self.backfill_demo()
            return  # no background thread; see ensure_fresh()
        self.load_watchlist()
        self.backfill_demo()
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self._stop.set()

    # -- analysis ----------------------------------------------------------
    def spike(self, region: Region) -> dict:
        """Compare current inside+inbound traffic to a baseline from history.

        Baseline preference: same hour-of-day (±1h) over previous days; else the
        trailing 24h. Uses median and MAD (robust to past spikes)."""
        region = self.watch(region)
        inside, inbound = self.count(region)
        current = len(inside) + len(inbound)
        now = self.snapshot_ts or int(time.time())
        hist = [(ts, i + b) for ts, i, b in self.store.counts(region.key, now - config.HISTORY_DAYS * 86400)
                if ts < now - 1800]
        same_hour = [v for ts, v in hist if abs(((ts - now) % 86400 + 43200) % 86400 - 43200) <= 3600]
        basis, sample = "same time of day on previous days", same_hour
        if len(sample) < config.MIN_BASELINE_SAMPLES:
            basis, sample = "the last 24 hours", [v for ts, v in hist if ts >= now - 86400]
        result = {"region": region.name, "current": current, "inside": len(inside), "inbound": len(inbound),
                  "samples": len(sample), "basis": basis, "snapshot_ts": now}
        if len(sample) < config.MIN_BASELINE_SAMPLES:
            hint = ("History builds up as snapshots are recorded." if getattr(self.store, "persistent", True) else
                    "This deployment has no persistent storage, so history resets on every cold start; "
                    "add Upstash Redis (KV_REST_API_URL/KV_REST_API_TOKEN) to keep it.")
            result.update(status="insufficient_history", spike=False,
                          note=f"Only {len(sample)} past snapshots for {region.name}; need "
                               f"{config.MIN_BASELINE_SAMPLES}. {hint}")
            return result
        med = statistics.median(sample)
        mad = statistics.median(abs(v - med) for v in sample) * 1.4826 or max(1.0, med * 0.05)
        z = (current - med) / mad
        ratio = current / med if med else float("inf") if current else 1.0
        is_spike = z >= config.SPIKE_Z and ratio >= config.SPIKE_RATIO
        is_dip = z <= -config.SPIKE_Z and ratio <= 1 / config.SPIKE_RATIO
        result.update(baseline=round(med, 1), z=round(z, 2), ratio=round(ratio, 2), spike=is_spike,
                      status="spike" if is_spike else "dip" if is_dip else "normal")
        return result
