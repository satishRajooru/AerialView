"""Settings, all overridable with environment variables."""
import os

# Data source: "opensky" (live, global), or "demo" (synthetic flights, works offline).
SOURCE = os.getenv("AM_SOURCE", "opensky")

# OpenSky Network. Anonymous access works but is heavily rate limited
# (~400 credits/day; a global /states/all call costs 4). Create a free account and an
# API client at https://opensky-network.org to get OAuth2 client credentials (4000+ credits/day).
OPENSKY_URL = os.getenv("OPENSKY_URL", "https://opensky-network.org/api")
OPENSKY_TOKEN_URL = os.getenv(
    "OPENSKY_TOKEN_URL",
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
)
OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET")

# Seconds between global snapshots. Default respects anonymous OpenSky limits;
# drop to 60-120 with an authenticated client.
POLL_SECONDS = int(os.getenv("AM_POLL_SECONDS", "900" if not OPENSKY_CLIENT_ID else "120"))

NOMINATIM_URL = os.getenv("AM_NOMINATIM_URL", "https://nominatim.openstreetmap.org")
USER_AGENT = os.getenv("AM_USER_AGENT", "aerial-monitor/0.1 (self-hosted)")
POSTCODE_DEFAULT_RADIUS_KM = float(os.getenv("AM_POSTCODE_RADIUS_KM", "10"))

DB_PATH = os.getenv("AM_DB", "aerial_monitor.sqlite3")
HISTORY_DAYS = int(os.getenv("AM_HISTORY_DAYS", "30"))

# Regions tracked on every snapshot so spike detection has a baseline.
# Any place you ask about is added to this list automatically.
DEFAULT_WATCHLIST = [
    s.strip() for s in os.getenv(
        "AM_WATCHLIST",
        "New Zealand,England,United Kingdom,India,United States,Australia,Germany,France,Japan,"
        "United Arab Emirates,Singapore,Canada",
    ).split(",") if s.strip()
]

# Spike thresholds: robust z-score on the baseline AND a minimum ratio over the median.
SPIKE_Z = float(os.getenv("AM_SPIKE_Z", "3.0"))
SPIKE_RATIO = float(os.getenv("AM_SPIKE_RATIO", "1.25"))
MIN_BASELINE_SAMPLES = int(os.getenv("AM_MIN_BASELINE", "6"))

# Optional: use Claude to parse free-form questions (falls back to the built-in parser).
# Set ANTHROPIC_API_KEY (or log in with `ant auth login`) and AM_LLM=1.
USE_LLM = os.getenv("AM_LLM", "1" if os.getenv("ANTHROPIC_API_KEY") else "0") == "1"
LLM_MODEL = os.getenv("AM_LLM_MODEL", "claude-opus-5-5")

# Demo source knobs.
DEMO_FLIGHTS = int(os.getenv("AM_DEMO_FLIGHTS", "6000"))
DEMO_SPIKE = os.getenv("AM_DEMO_SPIKE", "AKL")  # inject extra arrivals into this airport ("" to disable)
DEMO_BACKFILL_DAYS = int(os.getenv("AM_DEMO_BACKFILL_DAYS", "7"))

# --- Serverless (Vercel) -----------------------------------------------------
# On Vercel there is no long-running process: snapshots are fetched on demand
# (cached for SNAPSHOT_TTL seconds per warm instance) and region counts are
# recorded at most every RECORD_INTERVAL seconds, triggered by traffic or by the
# scheduled GitHub Action hitting /api/collect.
SERVERLESS = os.getenv("AM_SERVERLESS", "1" if os.getenv("VERCEL") else "0") == "1"
SNAPSHOT_TTL = int(os.getenv("AM_SNAPSHOT_TTL", "60" if OPENSKY_CLIENT_ID else "300"))
RECORD_INTERVAL = int(os.getenv("AM_RECORD_INTERVAL", "600"))
COLLECT_TOKEN = os.getenv("AM_COLLECT_TOKEN")  # optional shared secret for /api/collect
# If the live source fails and we have nothing to show, serve demo data (flagged in the UI).
FALLBACK_TO_DEMO = os.getenv("AM_FALLBACK_DEMO", "1" if SERVERLESS else "0") == "1"

# Persistent history on serverless: Upstash Redis (Vercel Marketplace adds these env vars).
REDIS_URL = os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")
if SERVERLESS and "AM_DB" not in os.environ:
    DB_PATH = "/tmp/aerial_monitor.sqlite3"
