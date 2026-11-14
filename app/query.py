"""Natural-language query engine.

Question -> QueryIntent (by Claude when configured, otherwise the rule parser)
-> executed against the live snapshot and stored history -> answer text + data.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import config
from .geo import Region, normalize_place
from .monitor import Monitor

M_PER_FT = 0.3048


class Place(BaseModel):
    name: str = Field(description="Place name or postal/PIN code exactly as the user means it, e.g. 'New Zealand', 'London', '560001'")
    kind: Optional[Literal["country", "city", "area", "postcode"]] = None
    country_code: Optional[str] = Field(None, description="ISO 3166-1 alpha-2 hint, mainly for postcodes, e.g. 'in' for an Indian PIN code")
    radius_km: Optional[float] = Field(None, description="Only if the user asks for 'within N km of'")


class QueryIntent(BaseModel):
    intent: Literal["count", "list", "spike", "spike_scan", "busiest", "overview"] = Field(
        description="count: how many aircraft over a place; list: which aircraft; spike: is traffic to/over the "
                    "place(s) unusual; spike_scan: any unusual spikes anywhere being watched; busiest: rank places; "
                    "overview: global totals")
    places: list[Place] = Field(default_factory=list)
    min_alt_ft: Optional[float] = None
    max_alt_ft: Optional[float] = None
    airborne_only: bool = True
    origin_country: Optional[str] = Field(None, description="Filter by aircraft registration country if asked")


# ---------------------------------------------------------------------------
# Rule-based parser (no API key needed)

POSTCODE_RE = re.compile(r"\b(?:pin\s*code|pincode|post\s*code|postcode|zip(?:\s*code)?|postal\s*code)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9 -]{2,8}[A-Za-z0-9])", re.I)
BARE_PIN_RE = re.compile(r"\b(\d{6})\b")
RADIUS_RE = re.compile(r"within\s+(\d+(?:\.\d+)?)\s*(km|kilomet(?:er|re)s?|mi|miles?|nm|nautical miles?)\s+(?:of|from|around)\s+", re.I)
ALT_ABOVE_RE = re.compile(r"(?:above|over|higher than)\s+(\d[\d,]*)\s*(?:ft|feet)", re.I)
ALT_BELOW_RE = re.compile(r"(?:below|under|lower than)\s+(\d[\d,]*)\s*(?:ft|feet)", re.I)
PLACE_LEAD = r"(?:over|above|in|into|to|towards|near|around|across|inbound to|heading to|flying to|from|of|for)"
STOP_TAIL = r"(?=\s+(?:right now|now|currently|today|at the moment|this hour|compared|vs\.?|versus|above|below|under|within)\b|[?.!]|$)"


def _split_places(text: str) -> list[str]:
    parts = re.split(r"\s*(?:,|\bor\b|\band\b|/|&)\s*", text)
    return [p.strip() for p in parts if p.strip() and normalize_place(p) not in ("", "the", "a", "any")]


def rule_parse(q: str) -> QueryIntent:
    ql = q.lower().strip()
    if re.search(r"\b(spike|surge|unusual|anomal|abnormal|more than usual|higher than usual|busier than usual|jump)\b", ql):
        intent = "spike"
    elif re.search(r"\b(busiest|top \d*|most (?:traffic|flights|aircraft)|rank)\b", ql):
        intent = "busiest"
    elif re.search(r"\b(list|which|show|what (?:flights|aircraft|planes))\b", ql) and not re.search(r"how many", ql):
        intent = "list"
    elif re.search(r"\b(how many|count|number of)\b", ql):
        intent = "count"
    else:
        intent = "count"

    places: list[Place] = []
    radius = None
    if (m := RADIUS_RE.search(q)):
        val, unit = float(m.group(1)), m.group(2).lower()
        radius = val * (1.609 if unit.startswith("mi") else 1.852 if unit.startswith("n") else 1)
    if (m := POSTCODE_RE.search(q)):
        cc = "in" if re.fullmatch(r"\d{6}", m.group(1).strip()) else None
        places.append(Place(name=m.group(1).strip(), kind="postcode", country_code=cc, radius_km=radius))
    elif (m := BARE_PIN_RE.search(q)):
        places.append(Place(name=m.group(1), kind="postcode", country_code="in", radius_km=radius))
    else:
        cleaned = RADIUS_RE.sub(" over ", q)
        cleaned = ALT_ABOVE_RE.sub(" ", ALT_BELOW_RE.sub(" ", cleaned))
        m = None
        for m in re.finditer(PLACE_LEAD + r"\s+(?:the\s+)?([A-Za-z][A-Za-z .'\-,/&]*?)" + STOP_TAIL, cleaned, re.I):
            pass  # keep the last match: "flights over the sea near Auckland" -> Auckland
        if m:
            raw = m.group(1)
            # drop generic nouns like "the city of", "the area"
            raw = re.sub(r"^(?:city|country|area|region|state)\s+of\s+", "", raw, flags=re.I)
            for p in _split_places(raw):
                if normalize_place(p) in ("flights", "aircraft", "planes", "traffic", "us now"):
                    continue
                places.append(Place(name=p, radius_km=radius))

    if intent == "spike" and not places:
        intent = "spike_scan"
    if intent in ("count", "list") and not places:
        intent = "overview"
    to_ft = lambda s: float(s.replace(",", ""))
    return QueryIntent(
        intent=intent, places=places,
        min_alt_ft=to_ft(m.group(1)) if (m := ALT_ABOVE_RE.search(q)) else None,
        max_alt_ft=to_ft(m.group(1)) if (m := ALT_BELOW_RE.search(q)) else None,
        airborne_only=not re.search(r"on (?:the )?ground|parked|taxi", ql),
    )


# ---------------------------------------------------------------------------
# Claude parser (optional)

LLM_SYSTEM = """You turn questions about live air traffic into a structured query.
Fix obvious spelling mistakes in place names (e.g. "newzeland" -> "New Zealand").
"England" is a country distinct from "United Kingdom"; keep what the user said.
A bare 6-digit number is almost always an Indian PIN code (country_code "in").
"Flights to X" or "traffic to X" with words like spike/unusual/surge means intent "spike" with X as a place.
If the user asks about unusual traffic without naming any place, use "spike_scan"."""


def llm_parse(q: str) -> QueryIntent | None:
    if not config.USE_LLM:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic()
        resp = client.beta.messages.parse(
            model=config.LLM_MODEL,
            max_tokens=2000,
            system=LLM_SYSTEM,
            messages=[{"role": "user", "content": q}],
            output_format=QueryIntent,
            output_config={"effort": "low"},
            # On a safety decline, let the API retry on a fallback model automatically.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            return None
        return resp.parsed_output
    except Exception as exc:
        print(f"[query] LLM parse failed, using rule parser: {exc}")
        return None


# ---------------------------------------------------------------------------
# Execution

def _apply_filters(states: list[dict], qi: QueryIntent) -> list[dict]:
    out = states
    if qi.airborne_only:
        out = [a for a in out if not a["on_ground"]]
    if qi.min_alt_ft is not None:
        out = [a for a in out if (a.get("alt_m") or 0) >= qi.min_alt_ft * M_PER_FT]
    if qi.max_alt_ft is not None:
        out = [a for a in out if a.get("alt_m") is not None and a["alt_m"] <= qi.max_alt_ft * M_PER_FT]
    if qi.origin_country:
        oc = qi.origin_country.lower()
        out = [a for a in out if (a.get("origin_country") or "").lower() == oc]
    return out


def _filters_text(qi: QueryIntent) -> str:
    bits = []
    if qi.min_alt_ft is not None:
        bits.append(f"above {qi.min_alt_ft:,.0f} ft")
    if qi.max_alt_ft is not None:
        bits.append(f"below {qi.max_alt_ft:,.0f} ft")
    if qi.origin_country:
        bits.append(f"registered in {qi.origin_country}")
    return (" " + ", ".join(bits)) if bits else ""


class QueryEngine:
    def __init__(self, monitor: Monitor):
        self.m = monitor

    def parse(self, q: str) -> tuple[QueryIntent, str]:
        qi = llm_parse(q)
        return (qi, "claude") if qi else (rule_parse(q), "rules")

    def _resolve(self, p: Place) -> Region:
        return self.m.geo.resolve(p.name, kind=p.kind, country_code=p.country_code, radius_km=p.radius_km)

    def ask(self, q: str) -> dict:
        qi, parser = self.parse(q)
        res = {"question": q, "parser": parser, "intent": qi.model_dump(), "snapshot_ts": self.m.snapshot_ts,
               "regions": [], "aircraft": [], "results": []}
        if not self.m.states:
            res["answer"] = "No flight snapshot yet. " + (f"Last error: {self.m.last_error}" if self.m.last_error else "The first poll is in progress.")
            return res
        try:
            regions = [self._resolve(p) for p in qi.places]
        except LookupError as exc:
            res["answer"] = str(exc)
            return res
        res["regions"] = [r.to_geojson() for r in regions]
        states = _apply_filters(self.m.states, qi)
        lines = []

        if qi.intent in ("count", "list"):
            for r in regions:
                inside = [a for a in states if r.contains(a["lat"], a["lon"])]
                self.m.watch(r)
                desc = f"within {r.radius_km:g} km of {r.name}" if r.radius_km else f"over {r.name}"
                lines.append(f"{len(inside):,} aircraft{_filters_text(qi)} {'is' if len(inside) == 1 else 'are'} {desc} right now.")
                res["results"].append({"region": r.name, "count": len(inside)})
                res["aircraft"].extend(inside[:2000])
                if qi.intent == "list" and inside:
                    top = sorted(inside, key=lambda a: -(a.get("alt_m") or 0))[:15]
                    lines.append("  " + ", ".join(a["callsign"] or a["icao24"] for a in top) + (" ..." if len(inside) > 15 else ""))

        elif qi.intent in ("spike", "spike_scan"):
            if not regions:
                self.m.load_watchlist()
            targets = regions or list(self.m.regions.values())
            spikes = []
            for r in targets:
                s = self.m.spike(r)
                res["results"].append(s)
                if s["status"] == "insufficient_history":
                    if regions:
                        lines.append(f"{r.name}: {s['current']} aircraft over or inbound now. {s['note']}")
                elif s["status"] == "spike":
                    spikes.append(s)
                    lines.append(f"{r.name}: YES, unusual spike. {s['current']} aircraft over or inbound "
                                 f"({s['inside']} over, {s['inbound']} inbound) vs a typical {s['baseline']:g} "
                                 f"for {s['basis']} ({s['ratio']}x, z={s['z']}).")
                elif regions:
                    word = "lower than usual" if s["status"] == "dip" else "normal"
                    lines.append(f"{r.name}: {word}. {s['current']} aircraft over or inbound vs a typical "
                                 f"{s['baseline']:g} for {s['basis']} ({s['ratio']}x).")
                if regions:
                    inside, inbound = self.m.count(r)
                    res["aircraft"].extend(inside[:1000] + inbound[:1000])
            if not regions:
                lines.insert(0, f"Checked {len(targets)} watched regions: "
                             + (f"{len(spikes)} with an unusual spike." if spikes else "no unusual spikes right now."))
                res["regions"] = [self.m.regions[s_key].to_geojson() for s_key in self.m.regions
                                  if any(x["region"] == self.m.regions[s_key].name for x in spikes)]

        elif qi.intent == "busiest":
            if not regions:
                self.m.load_watchlist()
            targets = regions or list(self.m.regions.values())
            ranked = sorted(((r.name, sum(1 for a in states if r.contains(a["lat"], a["lon"]))) for r in targets),
                            key=lambda x: -x[1])
            res["results"] = [{"region": n, "count": c} for n, c in ranked]
            lines.append("Busiest right now: " + "; ".join(f"{n} {c:,}" for n, c in ranked[:10]))

        else:  # overview
            airborne = sum(1 for a in self.m.states if not a["on_ground"])
            lines.append(f"Tracking {len(self.m.states):,} aircraft worldwide, {airborne:,} airborne"
                         + (f"; {len(states):,} match{_filters_text(qi)}." if _filters_text(qi) else "."))
            res["results"].append({"total": len(self.m.states), "airborne": airborne, "matching": len(states)})

        if self.m.using_fallback:
            lines.insert(0, f"(Live feed unavailable, so these are DEMO numbers. {self.m.last_error})")
        res["answer"] = "\n".join(lines) or "No results."
        return res
