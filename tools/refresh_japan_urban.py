"""Convert the three public Japanese tram/monorail GTFS feeds to mapped train runs."""
import argparse
from collections import defaultdict
import csv
from datetime import datetime
import io
import json
import math
from pathlib import Path
import re
import unicodedata
from urllib.request import urlopen
from zipfile import ZipFile

from build_city_timetables import seconds, service

ROOT = Path(__file__).resolve().parents[1]


def normalized(value):
    text = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"[\s・･（）()‐-]", "", text).replace("ヶ", "ケ").replace("ヵ", "カ")


SOURCES = {
    "kumamoto": ("Kumamoto City Transportation Bureau", "https://api.gtfs-data.jp/v2/organizations/kumamoto-shiden/feeds/kumamotoshiden",
                 "https://creativecommons.org/licenses/by/2.1/jp/", {"A系統": "A", "B系統": "B"}),
    "okayama": ("Okayama Electric Tramway", "https://okdweb.jcld.jp/gtfs/gtfs.zip",
                "https://creativecommons.org/licenses/by/4.0/", {"東山線": "1", "清輝橋線": "2"}),
    "naha": ("OTTOP · Okinawa Urban Monorail", "https://www.ottop.org/gtfs-opendata",
             "https://creativecommons.org/licenses/by/4.0/", {"沖縄都市モノレール線": "Yui"}),
}


def stop_id(row, candidates):
    name = normalized(row["stop_name"].removesuffix("駅"))
    lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
    ranked = []
    for stop in candidates:
        names = {normalized(s.removesuffix("駅")) for s in [stop["localName"], *stop.get("aliases", [])]}
        distance = math.hypot((stop["lat"] - lat) * 111200,
                              (stop["lon"] - lon) * math.cos(math.radians(lat)) * 111200)
        ranked.append((name not in names, distance, stop["id"]))
    ranked.sort()
    direct = [r for r in ranked if not r[0] and r[1] < 350]
    if len(direct) == 1:
        return direct[0][2]
    # Some platform names omit a new commemorative subtitle. Only accept an
    # unambiguous colocated station on this exact route.
    nearby = [r for r in ranked if r[1] < 110]
    if len(nearby) == 1:
        return nearby[0][2]
    raise ValueError(f"Unmatched GTFS stop {row['stop_name']}: {ranked[:2]}")


def build(city, archive, network):
    provider, source, terms, symbols = SOURCES[city]

    def rows(name):
        with archive.open(name + ".txt") as raw:
            yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))

    if "frequencies.txt" in archive.namelist() and any(rows("frequencies")):
        raise ValueError("Review new frequency-based services before publishing")
    info = next(rows("feed_info"))
    first, last = int(info["feed_start_date"]), int(info["feed_end_date"])
    source_day = datetime.strptime(str(first), "%Y%m%d").date().isoformat()
    services = {}
    for row in rows("calendar"):
        weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        mask = sum(int(row[k]) << n for n, k in enumerate(weekdays))
        start, end = max(first, int(row["start_date"])), min(last, int(row["end_date"]))
        services[row["service_id"]] = service(mask, start, end) if start <= end else service(0)
    for row in rows("calendar_dates"):
        d = int(row["date"])
        if first <= d <= last:
            key = {"1": "added", "2": "removed"}[row["exception_type"]]
            services.setdefault(row["service_id"], service(0))[key].append(d)

    stops = {s["id"]: s for s in network["stations"]}
    by_line = {l["symbol"]: [stops[i] for i in l["stops"]] for l in network["lines"]}
    route = {r["route_id"]: symbols[r["route_long_name"]] for r in rows("routes")}
    trip = {t["trip_id"]: t for t in rows("trips")}
    upstream = {s["stop_id"]: s for s in rows("stops")}
    calls = defaultdict(list)
    mapped = {}
    for r in rows("stop_times"):
        symbol = route[trip[r["trip_id"]]["route_id"]]
        key = (symbol, r["stop_id"])
        if key not in mapped:
            mapped[key] = stop_id(upstream[key[1]], by_line[symbol])
        calls[r["trip_id"]].append((int(r["stop_sequence"]), mapped[key], seconds(r["arrival_time"]),
                                    seconds(r["departure_time"]), r.get("pickup_type") or "0",
                                    r.get("drop_off_type") or "0", r.get("timepoint") or "1"))
    patterns = defaultdict(lambda: defaultdict(set))
    used = set()
    for tid, run in calls.items():
        run.sort()
        sid = trip[tid]["service_id"]
        cal = services[sid]
        if not cal["days"] and not cal["added"]:
            continue
        ids = tuple(row[1] for row in run)
        if len(set(ids)) < 2:
            raise ValueError(f"Train {tid} has no mapped journey")
        start = run[0][3]
        departures = tuple(row[3] - start for row in run)
        arrivals = (0,) + tuple(row[2] - start for row in run[1:])
        if any(a > d for a, d in zip(arrivals, departures)) or any(d > a for d, a in zip(departures, arrivals[1:])):
            raise ValueError(f"Non-chronological stop times on {tid}")
        nop = tuple(i for i,r in enumerate(run) if r[4] != "0" or i == len(run)-1)
        nod = tuple(i for i,r in enumerate(run) if r[5] != "0" or i == 0)
        estimated = tuple(i for i,r in enumerate(run) if r[6] == "0")
        patterns[(route[trip[tid]["route_id"]], ids, nop, nod, estimated)][sid, departures, arrivals].add(start)
        used.add(sid)
    compiled = []
    for (line, ids, nop, nod, est), timings in sorted(patterns.items()):
        items = []
        for (sid, dep, arr), starts in sorted(timings.items()):
            t = dict(service=sid, offsets=dep, starts=sorted(starts))
            if arr != dep:
                t["arrivalOffsets"] = arr
            items.append(t)
        compiled.append(dict(line=line, stops=ids, noPickup=nop, noDropOff=nod, approximate=est, timings=items))
    if not compiled or {p["line"] for p in compiled} != set(symbols.values()):
        raise ValueError("Incomplete line coverage")
    return dict(version=1, city=city, sourceDate=source_day, validThrough=last, allowsReferenceWeek=False,
                calendars={s: services[s] for s in sorted(used)}, patterns=compiled,
                attribution=dict(source=provider, url=source, terms=terms,
                                 notice=f"Adapted from {provider} GTFS. Service period starts {source_day}.",
                                 feedVersion=info.get("feed_version", "")))


DOWNLOADS = {
    "kumamoto": "https://api.gtfs-data.jp/v2/organizations/kumamoto-shiden/feeds/kumamotoshiden/files/feed.zip?rid=current",
    "okayama": "https://okdweb.jcld.jp/gtfs/gtfs.zip",
    "naha": "https://api3.ottop.org/download/gtfs/ooXuXei4op7y/4360001000447",
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("city", choices=sorted(SOURCES))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, help="Replay a previously downloaded public GTFS ZIP")
    args = parser.parse_args()
    if args.archive:
        data = args.archive.read_bytes()
    else:
        with urlopen(DOWNLOADS[args.city], timeout=60) as response:
            data = response.read(25_000_001)
        if len(data) > 25_000_000:
            raise ValueError("Unexpected GTFS archive size")
    network = json.loads((ROOT / "inputs/japan-rail-stations.json").read_bytes())[args.city]
    with ZipFile(io.BytesIO(data)) as archive:
        package = build(args.city, archive, network)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / f"offline-{args.city}.json").write_text(
        json.dumps(package, ensure_ascii=False, separators=(",", ":")) + "\n")
