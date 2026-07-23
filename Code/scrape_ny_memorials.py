"""Scrape New York 9/11 memorials from the VOICES Living Memorial directory.

Walks https://voicescenter.org/living-memorial/memorials/by-state, collects every
memorial listed under the NY heading, then visits each detail page to pull the
"Dedicated to", "Victims Listed", "About" and address fields.

Outputs two CSVs:
  data/ny_memorials.csv        every NY memorial
  data/nyc_memorials.csv       the subset located in NYC proper (the five boroughs)

Usage:
    python Code/scrape_ny_memorials.py [--state NY] [--concurrency 6] [--no-cache]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BASE = "https://voicescenter.org"
LIST_URL = f"{BASE}/living-memorial/memorials/by-state"
LIST_PAGES = 18  # pager on the by-state view runs page=0..17

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache" / "memorial_html"

# Field-group headers we care about, mapped to output column names.
FIELD_MAP = {
    "dedicated to": "dedicated_to",
    "victims listed": "victims_listed",
    "about": "about",
    "website": "website",
}

# "City, ST  12345" or "City, ST 12345-6789"
CITY_STATE_ZIP = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\.?\s+(?P<zip>\d{5})(?:-\d{4})?$")
# Plenty of records omit the ZIP entirely: "Merrick, NY"
CITY_STATE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\.?$")


@dataclass
class Memorial:
    name: str = ""
    state: str = ""
    url: str = ""
    dedicated_to: str = ""
    victims_listed: list[str] = field(default_factory=list)
    about: str = ""
    website: str = ""
    street: str = ""
    city: str = ""
    address_state: str = ""
    zip: str = ""
    country: str = ""
    address_full: str = ""
    map_query: str = ""
    region: str = ""
    in_nyc: bool = False
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# NYC metro classification
# --------------------------------------------------------------------------- #

# Downstate NY ZIPs are 10xxx (NYC/Westchester/Rockland/Putnam) and 11xxx
# (Brooklyn/Queens/Nassau/Suffolk). Everything upstate is 12xxx-14xxx.
BOROUGH_ZIP_RANGES = [
    ("Manhattan", 10001, 10282),
    ("Staten Island", 10301, 10314),
    ("Bronx", 10451, 10475),
    ("Queens", 11004, 11005),
    ("Queens", 11101, 11120),
    ("Queens", 11351, 11436),
    ("Queens", 11691, 11697),
    ("Brooklyn", 11201, 11256),
]
SUBURB_ZIP_RANGES = [
    ("Westchester County", 10501, 10598),
    ("Westchester County", 10601, 10610),
    ("Westchester County", 10701, 10710),
    ("Westchester County", 10801, 10805),
    ("Rockland County", 10900, 10998),
    ("Nassau County", 11001, 11003),
    ("Nassau County", 11010, 11099),
    ("Nassau County", 11501, 11599),
    ("Long Island (Nassau/Suffolk)", 11701, 11980),
]

BOROUGH_CITY_NAMES = {
    "new york": "Manhattan",
    "new york city": "Manhattan",
    "manhattan": "Manhattan",
    "nyc": "Manhattan",
    "brooklyn": "Brooklyn",
    "bronx": "Bronx",
    "the bronx": "Bronx",
    "staten island": "Staten Island",
    "queens": "Queens",
    "astoria": "Queens",
    "flushing": "Queens",
    "jamaica": "Queens",
    "long island city": "Queens",
    "forest hills": "Queens",
    "elmhurst": "Queens",
    "jackson heights": "Queens",
    "rockaway park": "Queens",
    "far rockaway": "Queens",
    "breezy point": "Queens",
    "belle harbor": "Queens",
    "ridgewood": "Queens",
    "middle village": "Queens",
    "bayside": "Queens",
    "whitestone": "Queens",
    "college point": "Queens",
    "howard beach": "Queens",
    "ozone park": "Queens",
    "richmond hill": "Queens",
    "woodhaven": "Queens",
    "rego park": "Queens",
    "corona": "Queens",
    "maspeth": "Queens",
    "sunnyside": "Queens",
    "woodside": "Queens",
    "glendale": "Queens",
    "fresh meadows": "Queens",
    "little neck": "Queens",
    "douglaston": "Queens",
    "springfield gardens": "Queens",
    "saint albans": "Queens",
    "st. albans": "Queens",
    "rosedale": "Queens",
    "laurelton": "Queens",
    "cambria heights": "Queens",
    "hollis": "Queens",
    "queens village": "Queens",
    "bellerose": "Queens",
    "kew gardens": "Queens",
}

SUBURB_COUNTY_HINTS = {
    "westchester": "Westchester County",
    "rockland": "Rockland County",
    "putnam": "Putnam County",
    "nassau": "Nassau County",
    "suffolk": "Suffolk County",
}

# Gazetteer of suburban metro municipalities. Needed because many records carry no
# ZIP at all, and a handful carry ZIPs that are simply wrong on the source site
# (e.g. Yonkers listed as 01701, Bardonia as 12533).
_SUBURB_CITIES = {
    "Nassau County": """albertson, atlantic beach, baldwin, baldwin harbor, bayville, bellerose village,
        bellmore, bethpage, carle place, cedarhurst, east meadow, east norwich, east rockaway,
        east williston, elmont, farmingdale, floral park, franklin square, freeport, garden city,
        glen cove, glen head, great neck, greenvale, hempstead, hewlett, hicksville, inwood,
        island park, jericho, lawrence, levittown, lido beach, locust valley, long beach, lynbrook,
        malverne, manhasset, massapequa, massapequa park, merrick, mill neck, mineola, new cassel,
        new hyde park, north bellmore, north merrick, oceanside, old bethpage, old westbury,
        oyster bay, plainview, plandome, point lookout, port washington, roosevelt, roslyn,
        roslyn heights, rockville centre, sands point, sea cliff, seaford, south farmingdale,
        stewart manor, syosset, uniondale, valley stream, wantagh, west hempstead, westbury,
        williston park, woodbury, woodmere""",
    "Suffolk County": """amityville, babylon, bay shore, bayport, bellport, blue point, bohemia,
        brentwood, bridgehampton, brookhaven, center moriches, centereach, central islip, commack,
        copiague, coram, deer park, east hampton, east islip, east northport, east setauket,
        farmingville, greenlawn, hampton bays, hauppauge, holbrook, holtsville, huntington,
        huntington station, islip, islip terrace, kings park, lake grove, lindenhurst, manorville,
        mastic, mastic beach, mattituck, medford, melville, miller place, mount sinai, nesconset,
        north babylon, northport, oakdale, ocean beach, patchogue, port jefferson,
        port jefferson station, quogue, riverhead, ronkonkoma, sag harbor, sayville, selden,
        setauket, shirley, shoreham, smithtown, sound beach, southampton, southold, stony brook,
        wading river, west babylon, west islip, west sayville, westhampton, westhampton beach,
        wyandanch, yaphank""",
    "Westchester County": """ardsley, armonk, bedford, briarcliff manor, bronxville, buchanan,
        chappaqua, cortlandt manor, croton-on-hudson, dobbs ferry, eastchester, elmsford, harrison,
        hartsdale, hastings-on-hudson, hawthorne, irvington, katonah, larchmont, mamaroneck,
        mohegan lake, montrose, mount kisco, mount vernon, new rochelle, north salem, ossining,
        peekskill, pelham, pleasantville, port chester, purchase, rye, scarsdale, shrub oak,
        sleepy hollow, somers, tarrytown, thornwood, tuckahoe, valhalla, verplanck, white plains,
        yonkers, yorktown heights""",
    "Rockland County": """airmont, bardonia, blauvelt, congers, garnerville, haverstraw, hillburn,
        monsey, nanuet, new city, nyack, orangeburg, palisades, pearl river, piermont, pomona,
        sloatsburg, spring valley, stony point, suffern, tappan, thiells, tomkins cove,
        valley cottage, west haverstraw, west nyack""",
    "Putnam County": "brewster, carmel, cold spring, garrison, mahopac, patterson, putnam valley",
}
SUBURB_CITY_NAMES: dict[str, str] = {}
for _county, _blob in _SUBURB_CITIES.items():
    for _city in _blob.split(","):
        _city = " ".join(_city.split())
        if _city:
            SUBURB_CITY_NAMES[_city] = _county

# Misspellings seen in the source data.
CITY_ALIASES = {"rockvile centre": "rockville centre", "ithica": "ithaca"}


def _norm_city(city: str) -> str:
    city = re.sub(r"\s+", " ", city.strip().lower())
    return CITY_ALIASES.get(city, city)


def classify(m: Memorial, scope: str) -> tuple[str, bool]:
    """Return (region label, in-scope flag) for a memorial's address.

    A named suburban municipality outranks the ZIP, because the site carries more
    bad ZIPs than bad city names ("New City, NY 11226" is Rockland, not Brooklyn).
    Borough names are checked after the ZIP instead, since "New York" is written on
    plenty of records that a ZIP places in an outer borough.
    """
    zip5 = m.zip[:5]
    z = int(zip5) if zip5.isdigit() else None
    city = _norm_city(m.city)

    if city in SUBURB_CITY_NAMES:
        return SUBURB_CITY_NAMES[city], scope == "metro"

    if z is not None:
        for label, lo, hi in BOROUGH_ZIP_RANGES:
            if lo <= z <= hi:
                return label, True

    if city in BOROUGH_CITY_NAMES:
        return BOROUGH_CITY_NAMES[city], True

    if z is not None:
        for label, lo, hi in SUBURB_ZIP_RANGES:
            if lo <= z <= hi:
                return label, scope == "metro"

    haystack = f"{m.city} {m.street} {m.address_full}".lower()
    for hint, label in SUBURB_COUNTY_HINTS.items():
        if hint in haystack:
            return label, scope == "metro"

    return "", False


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def parse_state_listing(html: str, want_state: str) -> list[tuple[str, str]]:
    """Pull (name, url) pairs sitting under the wanted <h3>STATE</h3> heading."""
    soup = BeautifulSoup(html, "lxml")
    found: list[tuple[str, str]] = []
    current = None
    # The view renders a flat sequence: <h3>ST</h3> then a .view-content-wrap of items.
    for node in soup.select("h3, div.view-content-wrap"):
        if node.name == "h3":
            text = node.get_text(strip=True)
            current = text if re.fullmatch(r"[A-Z]{2}", text) else current
            continue
        if current != want_state:
            continue
        for a in node.select(".views-field-title a[href]"):
            href = a["href"]
            if "/living-memorial/memorials/" not in href:
                continue
            found.append((a.get_text(" ", strip=True), BASE + href if href.startswith("/") else href))
    return found


def _block_text(node) -> str:
    """Text of a field-group minus its header label, paragraphs kept separate."""
    parts = []
    for child in node.children:
        if getattr(child, "get", None) and "field-group-header" in (child.get("class") or []):
            continue
        text = child.get_text("\n", strip=True) if hasattr(child, "get_text") else str(child).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def split_victims(text: str) -> list[str]:
    """One victim per source paragraph. Commas inside nickname quotes
    ('Michael Massaroli  "Mike,Maz"') become slashes so the joined CSV cell stays
    unambiguous under a plain comma split."""
    names = []
    for chunk in re.split(r"\n{2,}", text):
        name = " ".join(chunk.split())
        if name:
            names.append(name.replace(",", "/"))
    return names


def parse_detail(html: str, m: Memorial) -> Memorial:
    soup = BeautifulSoup(html, "lxml")

    title = soup.find("title")
    if title and not m.name:
        m.name = title.get_text(strip=True).split("|")[0].strip()

    article = soup.select_one("article.node--type-memorial") or soup

    for group in article.select(".field-group"):
        header = group.select_one(".field-group-header")
        if not header:
            continue
        key = header.get_text(strip=True).rstrip(":").strip().lower()
        col = FIELD_MAP.get(key)
        if not col:
            continue
        if col == "website":
            # The field renders as one or more bare anchors; the href is the payload.
            urls = [a["href"].strip() for a in group.select("a[href]") if a["href"].strip()]
            m.website = ", ".join(dict.fromkeys(urls))
            continue
        text = _block_text(group)
        if col == "victims_listed":
            m.victims_listed = split_victims(text)
        else:
            setattr(m, col, text)

    lines = [d.get_text(" ", strip=True) for d in article.select(".map-address .address .address_line")]
    lines = [ln for ln in lines if ln]
    m.address_full = ", ".join(lines)

    street_parts: list[str] = []
    for i, line in enumerate(lines):
        match = CITY_STATE_ZIP.match(line) or CITY_STATE.match(line)
        if match:
            # Some records cram the street into the city line
            # ("Hewlett Avenue, Merrick, NY"); the city is the last comma segment.
            head = match.group("city").split(",")
            m.city = head[-1].strip()
            if len(head) > 1:
                street_parts.extend(p.strip() for p in head[:-1] if p.strip())
            m.address_state = match.group("state")
            m.zip = match.groupdict().get("zip") or ""
            m.country = lines[i + 1] if i + 1 < len(lines) else ""
            break
        street_parts.append(line)
    else:
        # No "City, ST ZIP" line: keep everything as street, guess country/state loosely.
        if lines and lines[-1].lower() in {"united states", "usa", "us"}:
            m.country = lines[-1]
            street_parts = lines[:-1]
        else:
            street_parts = lines
    m.street = ", ".join(street_parts)

    iframe = article.select_one(".map-address iframe[src]")
    if iframe:
        query = re.search(r"[?&]q=([^&]*)", iframe["src"])
        if query:
            m.map_query = query.group(1).replace("+", " ")

    return m


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

async def fetch(page, url: str, cache_key: str, use_cache: bool) -> str:
    cache_file = CACHE_DIR / f"{cache_key}.html"
    if use_cache and cache_file.exists():
        return cache_file.read_text(encoding="utf-8")

    last_error = None
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            html = await page.content()
            if use_cache:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(html, encoding="utf-8")
            return html
        except Exception as exc:  # noqa: BLE001 - retry any nav failure
            last_error = exc
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to load {url}: {last_error}")


async def collect_links(browser, state: str, use_cache: bool) -> list[tuple[str, str]]:
    page = await browser.new_page()
    links: dict[str, str] = {}
    try:
        for i in range(LIST_PAGES):
            url = f"{LIST_URL}?page={i}"
            html = await fetch(page, url, f"_list_page_{i}", use_cache)
            hits = parse_state_listing(html, state)
            for name, link in hits:
                links.setdefault(link, name)
            print(f"  list page {i:>2}: {len(hits):>3} {state} memorials", file=sys.stderr)
    finally:
        await page.close()
    return [(name, link) for link, name in links.items()]


async def scrape_details(browser, entries: list[tuple[str, str]], state: str,
                         concurrency: int, use_cache: bool) -> list[Memorial]:
    queue: asyncio.Queue = asyncio.Queue()
    for item in entries:
        queue.put_nowait(item)
    results: list[Memorial] = []
    done = 0
    total = len(entries)

    async def worker():
        nonlocal done
        page = await browser.new_page()
        try:
            while True:
                try:
                    name, url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                m = Memorial(name=name, state=state, url=url)
                slug = url.rstrip("/").rsplit("/", 1)[-1]
                try:
                    html = await fetch(page, url, slug, use_cache)
                    parse_detail(html, m)
                except Exception as exc:  # noqa: BLE001
                    m.errors.append(str(exc))
                results.append(m)
                done += 1
                if done % 25 == 0 or done == total:
                    print(f"  detail {done}/{total}", file=sys.stderr)
        finally:
            await page.close()

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return results


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

COLUMNS = ["name", "state", "url", "dedicated_to", "victims_count", "victims_listed",
           "about", "website", "street", "city", "address_state", "zip", "country",
           "address_full", "map_query", "region", "in_nyc"]


def write_csv(path: Path, rows: list[Memorial], victims_format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for m in rows:
            row = asdict(m)
            row["victims_count"] = len(m.victims_listed)
            row["victims_listed"] = (json.dumps(m.victims_listed) if victims_format == "json"
                                     else ", ".join(m.victims_listed))
            writer.writerow(row)


async def main_async(args) -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            print(f"Collecting {args.state} memorial links...", file=sys.stderr)
            entries = await collect_links(browser, args.state, not args.no_cache)
            entries.sort(key=lambda e: e[0].lower())
            print(f"Found {len(entries)} {args.state} memorials.", file=sys.stderr)

            memorials = await scrape_details(browser, entries, args.state,
                                             args.concurrency, not args.no_cache)
        finally:
            await browser.close()

    memorials.sort(key=lambda m: m.name.lower())
    for m in memorials:
        m.region, m.in_nyc = classify(m, args.scope)

    all_path = DATA_DIR / f"{args.state.lower()}_memorials.csv"
    nyc_path = DATA_DIR / ("nyc_memorials.csv" if args.scope == "boroughs"
                           else "nyc_metro_memorials.csv")
    in_scope = [m for m in memorials if m.in_nyc]
    write_csv(all_path, memorials, args.victims_format)
    write_csv(nyc_path, in_scope, args.victims_format)

    failed = [m for m in memorials if m.errors]
    if failed:
        (DATA_DIR / "errors.json").write_text(
            json.dumps([{"url": m.url, "errors": m.errors} for m in failed], indent=2))

    print(f"\n{len(memorials)} memorials -> {all_path}", file=sys.stderr)
    print(f"{len(in_scope)} in scope '{args.scope}' -> {nyc_path}", file=sys.stderr)
    if failed:
        print(f"{len(failed)} pages failed, see data/errors.json", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="NY", help="two-letter state heading to scrape")
    parser.add_argument("--concurrency", type=int, default=6, help="parallel browser tabs")
    parser.add_argument("--scope", choices=["boroughs", "metro"], default="boroughs",
                        help="'boroughs' = NYC proper, the five boroughs (default); "
                             "'metro' = also Westchester/Rockland/Putnam/Nassau/Suffolk")
    parser.add_argument("--victims-format", choices=["comma", "json"], default="comma",
                        help="'comma' = one cell of comma-separated names; "
                             "'json' = a JSON array, parseable straight back into a list")
    parser.add_argument("--no-cache", action="store_true", help="ignore/skip the on-disk HTML cache")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
