"""Attach lat/lon to scraped memorials using the NYC Geoclient v2 API.

Only ~40% of the scraped rows carry a house number, so this tries three endpoints
in order of precision:

  /address?houseNumber=&street=&borough=&zip=   when the street starts with a number
  /intersection?crossStreetOne=&crossStreetTwo=&borough=   for "X and Y" corners
  /search?input=                                free-form, everything else

Needs a NYC API Portal subscription key (https://api-portal.nyc.gov) in the
GEOCLIENT_KEY environment variable, or passed with --key.

Usage:
    export GEOCLIENT_KEY=...
    python Code/geocode_memorials.py                       # data/nyc_memorials.csv
    python Code/geocode_memorials.py --in data/ny_memorials.csv --address-only
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.nyc.gov/geoclient/v2"
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache" / "geoclient"
KEY_FILE = ROOT / "geoclient_key.txt"  # gitignored


def load_key(cli_key: str) -> str:
    """Key precedence: --key, then GEOCLIENT_KEY, then the gitignored key file."""
    if cli_key:
        return cli_key.strip()
    if os.environ.get("GEOCLIENT_KEY"):
        return os.environ["GEOCLIENT_KEY"].strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    return ""

GEO_COLUMNS = ["latitude", "longitude", "geo_method", "geo_status", "geo_bbl"]

# Geoclient wants a borough name it recognises; "Staten Island" and the rest match.
VALID_BOROUGHS = {"Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"}

HOUSE_NUMBER = re.compile(r"^\s*(\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?)\s+(\S.*)$")
INTERSECTION = re.compile(r"^(?P<a>.+?)\s+(?:and|&|at|/)\s+(?P<b>.+)$", re.I)

# Noise that stops Geoclient from resolving an otherwise fine street name.
STRIP_PREFIXES = re.compile(r"^(?:traffic island at|corner of|located at|intersection (?:of|between))\s+", re.I)
STRIP_SUFFIXES = re.compile(r"\s*(?:,?\s*(?:between|near|across from|opposite|outside|inside|in front of)\b.*"
                            r"|\bentrance\b.*)$", re.I)


class Geoclient:
    def __init__(self, key: str, use_cache: bool = True, pause: float = 0.05):
        self.key = key
        self.use_cache = use_cache
        self.pause = pause
        self.calls = 0

    def get(self, endpoint: str, params: dict[str, str]) -> dict:
        params = {k: v for k, v in params.items() if v}
        url = f"{API_BASE}/{endpoint}?" + urllib.parse.urlencode(params)

        digest = hashlib.sha1(url.encode()).hexdigest()[:20]
        cache_file = CACHE_DIR / f"{endpoint}_{digest}.json"
        if self.use_cache and cache_file.exists():
            return json.loads(cache_file.read_text())

        request = urllib.request.Request(url, headers={"Ocp-Apim-Subscription-Key": self.key})
        payload: dict = {}
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode())
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")[:200]
                if exc.code in (401, 403):
                    raise SystemExit(f"Geoclient rejected the key ({exc.code}): {body}")
                if exc.code == 429 and attempt < 2:  # throttled
                    time.sleep(2 * (attempt + 1))
                    continue
                payload = {"_error": f"HTTP {exc.code}: {body}"}
                break
            except Exception as exc:  # noqa: BLE001 - network flake
                if attempt == 2:
                    payload = {"_error": str(exc)}
                else:
                    time.sleep(1.5 * (attempt + 1))

        self.calls += 1
        if self.use_cache:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(payload))
        time.sleep(self.pause)
        return payload


def clean_street(street: str) -> str:
    street = STRIP_PREFIXES.sub("", street.strip())
    street = STRIP_SUFFIXES.sub("", street)
    return " ".join(street.split()).strip(" ,.")


def unwrap(payload: dict) -> dict:
    """Geoclient nests the useful bit under 'address'/'intersection'/'results'."""
    if not isinstance(payload, dict):
        return {}
    for key in ("address", "intersection", "blockface", "place"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    results = payload.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            return first.get("response", first)
    return payload


def read_latlon(payload: dict) -> tuple[str, str, str, str]:
    """Return (lat, lon, status, bbl) from any Geoclient response shape."""
    body = unwrap(payload)
    if payload.get("_error"):
        return "", "", payload["_error"], ""

    # Address responses carry latitude/longitude; some shapes only have the
    # internal-label point, which is the lot centroid rather than the curb.
    lat = body.get("latitude") or body.get("latitudeInternalLabel")
    lon = body.get("longitude") or body.get("longitudeInternalLabel")

    code = str(body.get("geosupportReturnCode") or body.get("geosupportReturnCode2") or "")
    message = body.get("message") or body.get("geosupportReturnCodeMessage") or ""
    status = "ok" if lat and lon else (message or f"no match (rc={code})" if code else "no match")
    bbl = str(body.get("bbl") or "")
    return (str(lat) if lat else "", str(lon) if lon else "", status, bbl)


def geocode_row(row: dict, client: Geoclient, address_only: bool) -> dict:
    borough = row.get("region", "")
    borough = borough if borough in VALID_BOROUGHS else ""
    zipcode = row.get("zip", "")
    street = clean_street(row.get("street", ""))
    if not borough and not zipcode:
        return {"latitude": "", "longitude": "", "geo_method": "",
                "geo_status": "no borough or zip", "geo_bbl": ""}

    attempts: list[tuple[str, str, dict]] = []
    house = HOUSE_NUMBER.match(street)
    if house:
        attempts.append(("address", "address", {
            "houseNumber": house.group(1), "street": house.group(2),
            "borough": borough, "zip": zipcode}))
    if not address_only:
        corner = INTERSECTION.match(street) if street else None
        if corner:
            attempts.append(("intersection", "intersection", {
                "crossStreetOne": corner.group("a"), "crossStreetTwo": corner.group("b"),
                "borough": borough, "zip": zipcode}))
        if street:  # a bare "Manhattan, 10009" would resolve to nothing useful
            free_form = ", ".join(p for p in [street, borough or row.get("city", ""), zipcode] if p)
            attempts.append(("search", "search", {"input": free_form}))

    if not attempts:
        return {"latitude": "", "longitude": "", "geo_method": "",
                "geo_status": "no street to geocode", "geo_bbl": ""}

    last = ("", "", "no match", "")
    for method, endpoint, params in attempts:
        lat, lon, status, bbl = read_latlon(client.get(endpoint, params))
        if lat and lon:
            return {"latitude": lat, "longitude": lon, "geo_method": method,
                    "geo_status": "ok", "geo_bbl": bbl}
        last = (lat, lon, status, bbl)
    return {"latitude": "", "longitude": "", "geo_method": "",
            "geo_status": last[2], "geo_bbl": ""}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="src", type=Path, default=DATA_DIR / "nyc_memorials.csv")
    parser.add_argument("--out", dest="dst", type=Path, default=None,
                        help="defaults to <input>_geocoded.csv")
    parser.add_argument("--key", default="",
                        help="NYC API Portal subscription key; else GEOCLIENT_KEY env "
                             "or the gitignored geoclient_key.txt")
    parser.add_argument("--address-only", action="store_true",
                        help="only use /address; skip the intersection and search fallbacks")
    parser.add_argument("--no-cache", action="store_true", help="ignore the on-disk response cache")
    args = parser.parse_args()

    key = load_key(args.key)
    if not key:
        print("No API key. Add geoclient_key.txt, set GEOCLIENT_KEY, or pass --key.",
              file=sys.stderr)
        return 2
    if not args.src.exists():
        print(f"Input not found: {args.src}", file=sys.stderr)
        return 2

    dst = args.dst or args.src.with_name(args.src.stem + "_geocoded.csv")
    rows = list(csv.DictReader(args.src.open(encoding="utf-8")))
    client = Geoclient(key, use_cache=not args.no_cache)

    hits = 0
    for i, row in enumerate(rows, 1):
        row.update(geocode_row(row, client, args.address_only))
        hits += row["geo_status"] == "ok"
        if i % 25 == 0 or i == len(rows):
            print(f"  {i}/{len(rows)} rows, {hits} located", file=sys.stderr)

    fieldnames = [c for c in rows[0] if c not in GEO_COLUMNS] + GEO_COLUMNS if rows else GEO_COLUMNS
    with dst.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    by_method: dict[str, int] = {}
    for row in rows:
        if row["geo_status"] == "ok":
            by_method[row["geo_method"]] = by_method.get(row["geo_method"], 0) + 1
    print(f"\n{hits}/{len(rows)} geocoded ({client.calls} API calls) -> {dst}", file=sys.stderr)
    for method, count in sorted(by_method.items(), key=lambda kv: -kv[1]):
        print(f"  {method}: {count}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
