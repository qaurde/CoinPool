#!/usr/bin/env python3
"""
dealhunter.py: find cheap flights to countries you have not visited yet and
lay them out as a travel schedule.

Free, no-paywall data sources only:
  * Travelpayouts / Aviasales Data API (free token): cached fares that real
    travellers found in the last ~48h. Used for discovery ("anywhere" search).
  * Duffel Flights API (free sign-up, optional): live airline offers. Used to
    re-price the legs that made it into the schedule.
  * passport-index-dataset (GitHub, free CSV): visa requirement per passport.
  * Travelpayouts reference data (cities.json / countries.json, no token).

Python 3.9+ standard library only. No pip install needed.

Usage:
  python dealhunter.py --config config.json            # live discovery
  python dealhunter.py --config config.json --verify   # + Duffel live re-price
  python dealhunter.py --config config.example.json --demo   # offline sample data
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / ".cache"
UA = "dealhunter/1.0 (+personal travel planner)"

TP_BASE = "https://api.travelpayouts.com"
TP_CITIES = f"{TP_BASE}/data/en/cities.json"
TP_COUNTRIES = f"{TP_BASE}/data/en/countries.json"
PASSPORT_CSV = ("https://raw.githubusercontent.com/ilyankou/passport-index-dataset/"
                "master/passport-index-tidy-iso2.csv")
DUFFEL_BASE = "https://api.duffel.com"


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def http(url: str, *, headers: dict | None = None, body: dict | None = None,
         timeout: int = 40, retries: int = 3) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs,
                                         method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # 4xx other than 429 will not fix itself; surface it.
            if e.code != 429 and e.code < 500:
                detail = e.read()[:300].decode(errors="replace")
                raise RuntimeError(f"HTTP {e.code} for {url.split('?')[0]}: {detail}") from e
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"request failed after {retries} tries: {url.split('?')[0]}: {last}")


def cached_get(url: str, max_age_h: float, *, headers: dict | None = None) -> bytes:
    """GET with an on-disk cache. Tokens are never part of the cache key file name."""
    CACHE_DIR.mkdir(exist_ok=True)
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    path = CACHE_DIR / key
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_h * 3600:
        return path.read_bytes()
    raw = http(url, headers=headers)
    path.write_bytes(raw)
    return raw


# --------------------------------------------------------------------------- #
# Reference data: city -> country, visa rules
# --------------------------------------------------------------------------- #
class Geo:
    def __init__(self, cities: list[dict], countries: list[dict]):
        self.city_country = {c["code"]: c.get("country_code") for c in cities if c.get("code")}
        self.city_name = {c["code"]: c.get("name") or c["code"] for c in cities if c.get("code")}
        self.country_name = {c["code"]: c.get("name") or c["code"] for c in countries if c.get("code")}

    @classmethod
    def load(cls) -> "Geo":
        cities = json.loads(cached_get(TP_CITIES, 24 * 7))
        countries = json.loads(cached_get(TP_COUNTRIES, 24 * 7))
        return cls(cities, countries)

    def country_of(self, city: str) -> str | None:
        return self.city_country.get(city)

    def label(self, city: str) -> str:
        cc = self.country_of(city) or "??"
        return f"{self.city_name.get(city, city)} ({city}), {self.country_name.get(cc, cc)}"


class Visa:
    """Requirement strings: 'visa free', 'visa on arrival', 'e-visa', 'eta',
    'visa required', 'no admission', or a number of visa-free days."""

    def __init__(self, rows: list[dict], passport: str):
        p = passport.upper()
        self.req: dict[str, str] = {}
        for r in rows:
            if r["Passport"] == p:
                v = r["Requirement"].strip().lower()
                self.req[r["Destination"]] = "visa free" if v.isdigit() else v
        self.loaded = bool(self.req)

    @classmethod
    def load(cls, passport: str) -> "Visa":
        try:
            raw = cached_get(PASSPORT_CSV, 24 * 7).decode()
            return cls(list(csv.DictReader(raw.splitlines())), passport)
        except Exception as e:  # visa data is advisory; keep going without it
            print(f"[warn] visa dataset unavailable ({e}); visa filter disabled", file=sys.stderr)
            return cls([], passport)

    def get(self, cc: str) -> str:
        r = self.req.get(cc, "unknown") if self.loaded else "unknown"
        return "home" if r == "-1" else r

    def ok(self, cc: str, allowed: list[str]) -> bool:
        if not self.loaded:
            return True
        r = self.get(cc)
        return r in allowed or r == "home"


# --------------------------------------------------------------------------- #
# Fare sources
# --------------------------------------------------------------------------- #
@dataclass
class Fare:
    origin: str
    destination: str
    depart: str                 # YYYY-MM-DD
    ret: str | None             # YYYY-MM-DD for round trips
    price: float
    currency: str
    stops: int | None = None
    source: str = "travelpayouts"
    found_at: str | None = None
    link: str | None = None
    country: str | None = None

    @property
    def nights(self) -> int | None:
        if not self.ret:
            return None
        return (d(self.ret) - d(self.depart)).days


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def months_between(start: dt.date, end: dt.date) -> list[dt.date]:
    out, cur = [], start.replace(day=1)
    while cur <= end:
        out.append(cur)
        cur = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


class Travelpayouts:
    def __init__(self, token: str, currency: str):
        self.token, self.cur = token, currency.lower()

    def _get(self, path: str, params: dict) -> dict:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{TP_BASE}{path}?{q}"
        # Fares change fast; cache only briefly to stay under rate limits on re-runs.
        raw = cached_get(url, 3, headers={"X-Access-Token": self.token})
        return json.loads(raw)

    def anywhere(self, origin: str, month: dt.date, one_way: bool) -> list[Fare]:
        """Cheapest cached fares from `origin` to every destination in a month."""
        res = self._get("/v2/prices/latest", {
            "origin": origin, "currency": self.cur, "period_type": "month",
            "beginning_of_period": month.isoformat(), "one_way": str(one_way).lower(),
            "show_to_affiliates": "false", "sorting": "price", "limit": 1000,
        })
        fares = []
        for x in res.get("data") or []:
            fares.append(Fare(
                origin=x["origin"], destination=x["destination"],
                depart=x["depart_date"], ret=None if one_way else x.get("return_date"),
                price=float(x["value"]), currency=self.cur.upper(),
                stops=x.get("number_of_changes"), found_at=x.get("found_at")))
        return fares

    def dated(self, origin: str, dest: str, depart: str, ret: str | None) -> list[Fare]:
        """Cheapest cached fares for a specific route and date (or month)."""
        res = self._get("/aviasales/v3/prices_for_dates", {
            "origin": origin, "destination": dest, "departure_at": depart,
            "return_at": ret, "one_way": "false" if ret else "true",
            "currency": self.cur, "sorting": "price", "limit": 10, "unique": "false",
        })
        out = []
        for x in res.get("data") or []:
            out.append(Fare(
                origin=x["origin"], destination=x["destination"],
                depart=x["departure_at"][:10],
                ret=(x.get("return_at") or "")[:10] or None,
                price=float(x["price"]), currency=self.cur.upper(),
                stops=x.get("transfers"),
                link=("https://www.aviasales.com" + x["link"]) if x.get("link") else None))
        return out


class Duffel:
    """Live offers. Free to sign up; searches are free up to 1500 per confirmed
    order, then $0.005 each. A test token (duffel_test_...) returns sandbox data."""

    def __init__(self, token: str):
        self.token = token

    def cheapest(self, origin: str, dest: str, depart: str, ret: str | None,
                 max_connections: int = 1) -> Fare | None:
        slices = [{"origin": origin, "destination": dest, "departure_date": depart}]
        if ret:
            slices.append({"origin": dest, "destination": origin, "departure_date": ret})
        body = {"data": {"slices": slices, "passengers": [{"type": "adult"}],
                         "cabin_class": "economy", "max_connections": max_connections}}
        raw = http(f"{DUFFEL_BASE}/air/offer_requests?return_offers=true&supplier_timeout=15000",
                   headers={"Authorization": f"Bearer {self.token}", "Duffel-Version": "v2"},
                   body=body, timeout=60)
        offers = json.loads(raw)["data"].get("offers") or []
        if not offers:
            return None
        o = min(offers, key=lambda x: float(x["total_amount"]))
        stops = max(len(s["segments"]) - 1 for s in o["slices"])
        return Fare(origin, dest, depart, ret, float(o["total_amount"]), o["total_currency"],
                    stops=stops, source=f"duffel:{o['owner']['name']}")


# --------------------------------------------------------------------------- #
# Demo data (synthetic, for trying the tool without tokens)
# --------------------------------------------------------------------------- #
DEMO_CITIES = [
    ("LON", "London", "GB"), ("LIS", "Lisbon", "PT"), ("TIA", "Tirana", "AL"),
    ("RAK", "Marrakech", "MA"), ("TBS", "Tbilisi", "GE"), ("KRK", "Krakow", "PL"),
    ("SOF", "Sofia", "BG"), ("TLL", "Tallinn", "EE"), ("OPO", "Porto", "PT"),
    ("BUD", "Budapest", "HU"), ("AMM", "Amman", "JO"), ("CAI", "Cairo", "EG"),
    ("IST", "Istanbul", "TR"), ("RIX", "Riga", "LV"), ("SKP", "Skopje", "MK"),
    ("BEG", "Belgrade", "RS"), ("KEF", "Reykjavik", "IS"), ("MLA", "Malta", "MT"),
    ("TNG", "Tangier", "MA"), ("VNO", "Vilnius", "LT"), ("ACC", "Accra", "GH"),
    ("PAR", "Paris", "FR"), ("BCN", "Barcelona", "ES"),
]
DEMO_COUNTRIES = {"GB": "United Kingdom", "PT": "Portugal", "AL": "Albania", "MA": "Morocco",
                  "GE": "Georgia", "PL": "Poland", "BG": "Bulgaria", "EE": "Estonia",
                  "HU": "Hungary", "JO": "Jordan", "EG": "Egypt", "TR": "Turkey",
                  "LV": "Latvia", "MK": "North Macedonia", "RS": "Serbia", "IS": "Iceland",
                  "MT": "Malta", "LT": "Lithuania", "GH": "Ghana", "FR": "France", "ES": "Spain"}


class DemoFares:
    """Deterministic pseudo-fares so the scheduler can be exercised offline."""

    def __init__(self, currency: str):
        self.cur = currency.upper()

    def _price(self, a: str, b: str, day: dt.date, base: float) -> float:
        h = int(hashlib.md5(f"{a}{b}{day}".encode()).hexdigest()[:6], 16)
        return round(base * (0.55 + (h % 900) / 1000), 0)

    def anywhere(self, origin: str, month: dt.date, one_way: bool) -> list[Fare]:
        out = []
        for i, (code, _, _) in enumerate(DEMO_CITIES):
            if code == origin:
                continue
            base = 60 + (i * 37) % 260
            for wk in range(4):
                dep = month + dt.timedelta(days=3 + wk * 7 + (i % 5))
                ret = None if one_way else (dep + dt.timedelta(days=4 + (i + wk) % 6)).isoformat()
                mult = 1 if one_way else 1.8
                out.append(Fare(origin, code, dep.isoformat(), ret,
                                self._price(origin, code, dep, base * mult), self.cur,
                                stops=(i + wk) % 2, source="demo"))
        return out

    def dated(self, origin: str, dest: str, depart: str, ret: str | None) -> list[Fare]:
        day = d(depart) if len(depart) == 10 else d(depart + "-01")
        base = 120 if ret else 70
        return [Fare(origin, dest, day.isoformat(), ret, self._price(origin, dest, day, base),
                     self.cur, stops=1, source="demo")]


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #
@dataclass
class Trip:
    legs: list[Fare]
    countries: list[str]
    start: str
    end: str
    total: float
    notes: list[str] = field(default_factory=list)


def gflights_url(f: Fare) -> str:
    q = f"Flights from {f.origin} to {f.destination} on {f.depart}"
    if f.ret:
        q += f" through {f.ret}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


class Planner:
    def __init__(self, cfg: dict, src, geo: Geo, visa: Visa):
        self.cfg, self.src, self.geo, self.visa = cfg, src, geo, visa
        self.start = d(cfg["window"]["start"])
        self.end = d(cfg["window"]["end"])
        self.visited = {c.upper() for c in cfg.get("visited_countries", [])}
        self.excluded = {c.upper() for c in cfg.get("exclude_countries", [])}
        self.visa_ok = [v.lower() for v in cfg.get("visa_ok",
                        ["visa free", "visa on arrival", "e-visa", "eta"])]
        self.homes = [h.upper() for h in cfg["home_cities"]]
        self.visited |= {cc for h in self.homes if (cc := geo.country_of(h))}
        self.rejections: dict[str, int] = {}

    def _reject(self, why: str) -> bool:
        self.rejections[why] = self.rejections.get(why, 0) + 1
        return False

    def eligible(self, f: Fare, also_exclude: set[str] = frozenset()) -> bool:
        cc = self.geo.country_of(f.destination)
        f.country = cc
        if not cc:
            return self._reject("unknown city code")
        if cc in self.visited or cc in self.excluded or cc in also_exclude:
            return self._reject("visited / excluded")
        if not self.visa.ok(cc, self.visa_ok):
            return self._reject(f"visa: {self.visa.get(cc)}")
        return True

    # ---- discovery ------------------------------------------------------- #
    def deal_board(self) -> list[Fare]:
        """Cheapest qualifying round trip per new country, across the window."""
        tn = self.cfg.get("trip_nights", {"min": 3, "max": 14})
        cap = self.cfg.get("max_price_per_trip", 10 ** 9)
        best: dict[str, Fare] = {}
        for home in self.homes:
            for m in months_between(self.start, self.end):
                try:
                    fares = self.src.anywhere(home, m, one_way=False)
                except RuntimeError as e:
                    print(f"[warn] {home} {m:%Y-%m}: {e}", file=sys.stderr)
                    continue
                for f in fares:
                    if not f.ret or not (self.start <= d(f.depart) and d(f.ret) <= self.end):
                        self._reject("outside window")
                        continue
                    if not (tn["min"] <= (f.nights or 0) <= tn["max"]):
                        self._reject("trip length")
                        continue
                    if f.price > cap:
                        self._reject("over price cap")
                        continue
                    if not self.eligible(f):
                        continue
                    cur = best.get(f.country)
                    if cur is None or f.price < cur.price:
                        best[f.country] = f
        return sorted(best.values(), key=lambda f: f.price)

    # ---- scheduling: separate round trips -------------------------------- #
    def schedule_roundtrips(self, board: list[Fare]) -> list[Trip]:
        gap = dt.timedelta(days=self.cfg.get("min_days_between_trips", 14))
        budget = self.cfg.get("total_budget", 10 ** 9)
        max_trips = self.cfg.get("max_trips", 6)
        chosen: list[Fare] = []
        spent = 0.0
        for f in board:  # cheapest new country first
            if len(chosen) >= max_trips:
                break
            if spent + f.price > budget:
                continue
            s, e = d(f.depart), d(f.ret)
            if any(not (e + gap <= d(c.depart) or d(c.ret) + gap <= s) for c in chosen):
                continue
            chosen.append(f)
            spent += f.price
        chosen.sort(key=lambda f: f.depart)
        return [Trip([f], [f.country], f.depart, f.ret, f.price) for f in chosen]

    # ---- scheduling: one multi-country loop ------------------------------ #
    def schedule_chain(self) -> list[Trip]:
        c = self.cfg.get("chain", {})
        hops = c.get("max_hops", 4)
        nights = c.get("nights_per_stop", 3)
        flex = c.get("flex_days", 4)
        leg_cap = c.get("max_leg_price", 10 ** 9)
        home = self.homes[0]
        here, date = home, self.start
        legs: list[Fare] = []
        seen: set[str] = set()
        notes: list[str] = []
        for _ in range(hops):
            pool: list[Fare] = []
            for m in {date.replace(day=1), (date + dt.timedelta(days=flex)).replace(day=1)}:
                try:
                    pool += self.src.anywhere(here, m, one_way=True)
                except RuntimeError as e:
                    print(f"[warn] {here} {m:%Y-%m}: {e}", file=sys.stderr)
            pool = [f for f in pool
                    if date <= d(f.depart) <= date + dt.timedelta(days=flex)
                    and f.price <= leg_cap and self.eligible(f, seen)]
            if not pool:
                notes.append(f"no cached one-way deal out of {here} around {date}; chain stopped")
                break
            f = min(pool, key=lambda x: x.price)
            legs.append(f)
            seen.add(f.country)
            here, date = f.destination, d(f.depart) + dt.timedelta(days=nights)
            if date > self.end:
                break
        if not legs:
            return []
        back = self._cheapest_dated(here, home, date, flex)
        if back:
            legs.append(back)
        else:
            notes.append(f"no cached fare {here} -> {home} near {date}: price it live")
        total = sum(x.price for x in legs)
        return [Trip(legs, [x.country for x in legs if x.destination != home],
                     legs[0].depart, legs[-1].depart, total, notes)]

    def _cheapest_dated(self, a: str, b: str, day: dt.date, flex: int) -> Fare | None:
        found: list[Fare] = []
        for off in range(flex + 1):
            try:
                found += self.src.dated(a, b, (day + dt.timedelta(days=off)).isoformat(), None)
            except RuntimeError:
                pass
            if found:
                break
        return min(found, key=lambda x: x.price) if found else None

    # ---- live re-pricing -------------------------------------------------- #
    def verify(self, trips: list[Trip], duffel: Duffel) -> None:
        for t in trips:
            for i, f in enumerate(t.legs):
                try:
                    live = duffel.cheapest(f.origin, f.destination, f.depart, f.ret)
                except RuntimeError as e:
                    t.notes.append(f"live check failed {f.origin}->{f.destination}: {e}")
                    continue
                if live is None:
                    t.notes.append(f"no live offer {f.origin}->{f.destination} on {f.depart}")
                    continue
                delta = live.price - f.price
                t.notes.append(f"live {f.origin}->{f.destination}: {live.price:.0f} "
                               f"{live.currency} via {live.source} ({delta:+.0f} vs cached)")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_outputs(out: Path, cfg: dict, board: list[Fare], trips: list[Trip],
                  geo: Geo, visa: Visa, planner: Planner, demo: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = cfg.get("currency", "usd").upper()

    # deals.csv: the whole board, so you can sort/filter yourself
    with open(out / "deals.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["country", "country_name", "visa", "origin", "destination", "depart",
                    "return", "nights", "price", "currency", "stops", "found_at", "search_url"])
        for f in board:
            w.writerow([f.country, geo.country_name.get(f.country, f.country), visa.get(f.country),
                        f.origin, f.destination, f.depart, f.ret, f.nights, f"{f.price:.0f}",
                        f.currency, f.stops, f.found_at, gflights_url(f)])

    # itinerary.md
    L = [f"# Travel schedule: new countries only", "",
         f"Generated {stamp}. Window {cfg['window']['start']} to {cfg['window']['end']}. "
         f"Home: {', '.join(planner.homes)}. Passport: {cfg.get('passport', '?')}.", ""]
    if demo:
        L += ["> **DEMO DATA.** Prices below are synthetic. Run without `--demo` and with a "
              "`TRAVELPAYOUTS_TOKEN` for real fares.", ""]
    L += ["> Cached fares are what other travellers saw in the last ~48h. They move. "
          "Re-price before booking (`--verify` or the search link).", ""]
    total = sum(t.total for t in trips)
    L += [f"## Schedule: {len(trips)} trip(s), {sum(len(t.countries) for t in trips)} new "
          f"countr{'y' if sum(len(t.countries) for t in trips) == 1 else 'ies'}, "
          f"≈ {total:.0f} {cur} in flights", ""]
    for n, t in enumerate(trips, 1):
        names = ", ".join(geo.country_name.get(c, c) for c in t.countries)
        L.append(f"### {n}. {names}  ({t.start} → {t.end}, {t.total:.0f} {cur})")
        L.append("")
        L.append("| Leg | Depart | Return | Price | Stops | Visa | Search |")
        L.append("|---|---|---|---|---|---|---|")
        for f in t.legs:
            cc = geo.country_of(f.destination)
            L.append(f"| {geo.label(f.origin)} → {geo.label(f.destination)} | {f.depart} | "
                     f"{f.ret or '-'} | {f.price:.0f} {f.currency} | "
                     f"{'?' if f.stops is None else f.stops} | {visa.get(cc) if cc else '-'} | "
                     f"[Google Flights]({gflights_url(f)})"
                     + (f" · [Aviasales]({f.link})" if f.link else "") + " |")
        for note in t.notes:
            L.append(f"- {note}")
        L.append("")
    L += ["## Runner-up deals (not scheduled)", "", "| Country | Route | Dates | Price | Visa |",
          "|---|---|---|---|---|"]
    scheduled = {c for t in trips for c in t.countries}
    for f in [f for f in board if f.country not in scheduled][:15]:
        L.append(f"| {geo.country_name.get(f.country, f.country)} | {f.origin}→{f.destination} | "
                 f"{f.depart} → {f.ret} | {f.price:.0f} {f.currency} | {visa.get(f.country)} |")
    if planner.rejections:
        L += ["", "## Filtered out", ""]
        L += [f"- {k}: {v}" for k, v in sorted(planner.rejections.items(), key=lambda x: -x[1])]
    L += ["", "## Before you book", "",
          "- Check the official government travel advisory for each country.",
          "- Confirm visa/entry rules on the destination's official immigration site "
          "(the dataset is community maintained).",
          "- Check passport validity (many countries require 6 months beyond departure).",
          "- Book directly with the airline when the price matches; it is simpler to fix problems."]
    (out / "itinerary.md").write_text("\n".join(L) + "\n")

    # trips.ics: one all-day event per trip, importable into any calendar
    ics = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//dealhunter//EN", "CALSCALE:GREGORIAN"]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for t in trips:
        names = ", ".join(geo.country_name.get(c, c) for c in t.countries)
        uid = hashlib.sha1(f"{t.start}{names}".encode()).hexdigest()[:16]
        desc = "\\n".join(f"{f.origin}->{f.destination} {f.depart} {f.price:.0f} {f.currency} "
                          f"{gflights_url(f)}" for f in t.legs)
        ics += ["BEGIN:VEVENT", f"UID:{uid}@dealhunter", f"DTSTAMP:{now}",
                f"DTSTART;VALUE=DATE:{t.start.replace('-', '')}",
                f"DTEND;VALUE=DATE:{(d(t.end) + dt.timedelta(days=1)).strftime('%Y%m%d')}",
                f"SUMMARY:Trip: {names} (~{t.total:.0f} {cur})",
                f"DESCRIPTION:{desc}", "END:VEVENT"]
    ics.append("END:VCALENDAR")
    (out / "trips.ics").write_text("\r\n".join(ics) + "\r\n")

    # snapshot for price-drop tracking
    snap = {f.country: f.price for f in board}
    prev_path = out / "snapshot.json"
    drops = []
    if prev_path.exists():
        prev = json.loads(prev_path.read_text())
        drops = [(c, prev[c], p) for c, p in snap.items() if c in prev and p < prev[c]]
    prev_path.write_text(json.dumps(snap, indent=1))
    if drops:
        print("\nPrice drops since last run:")
        for c, old, new in sorted(drops, key=lambda x: x[2] - x[1]):
            print(f"  {geo.country_name.get(c, c):<20} {old:.0f} -> {new:.0f} {cur}")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--demo", action="store_true", help="offline synthetic fares, no tokens needed")
    ap.add_argument("--verify", action="store_true", help="re-price scheduled legs live via Duffel")
    ap.add_argument("--mode", choices=["roundtrip", "chain"], help="override config mode")
    ap.add_argument("--out", default=str(HERE / "out"))
    a = ap.parse_args()

    cfg = json.loads(Path(a.config).read_text())
    mode = a.mode or cfg.get("mode", "roundtrip")

    if a.demo:
        geo = Geo([{"code": c, "name": n, "country_code": cc} for c, n, cc in DEMO_CITIES],
                  [{"code": k, "name": v} for k, v in DEMO_COUNTRIES.items()])
        src = DemoFares(cfg.get("currency", "usd"))
        cfg = {**cfg, "home_cities": ["LON"]}
    else:
        token = os.environ.get("TRAVELPAYOUTS_TOKEN")
        if not token:
            print("Set TRAVELPAYOUTS_TOKEN (free at travelpayouts.com > Profile > API token), "
                  "or try --demo.", file=sys.stderr)
            return 2
        geo = Geo.load()
        src = Travelpayouts(token, cfg.get("currency", "usd"))
    visa = Visa.load(cfg.get("passport", ""))

    p = Planner(cfg, src, geo, visa)
    board = p.deal_board()
    trips = p.schedule_roundtrips(board) if mode == "roundtrip" else p.schedule_chain()

    if a.verify:
        tok = os.environ.get("DUFFEL_TOKEN")
        if not tok:
            print("[warn] --verify needs DUFFEL_TOKEN; skipping live re-price", file=sys.stderr)
        elif a.demo:
            print("[warn] --verify ignored in --demo mode", file=sys.stderr)
        else:
            p.verify(trips, Duffel(tok))

    out = Path(a.out)
    write_outputs(out, cfg, board, trips, geo, visa, p, a.demo)
    cur = cfg.get("currency", "usd").upper()
    print(f"\n{len(board)} new-country deals found; {len(trips)} trip(s) scheduled ({mode}).")
    for t in trips:
        names = ", ".join(geo.country_name.get(c, c) for c in t.countries)
        print(f"  {t.start} → {t.end}  {names:<40} {t.total:>6.0f} {cur}")
    print(f"\nWrote {out/'itinerary.md'}, {out/'deals.csv'}, {out/'trips.ics'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
