"""Build per-station schedules from TfL's published, directed Underground timetables."""
import argparse
import collections
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
NEXT_REQUEST = 0
LOCK = threading.Lock()


def fetch(url, paced=False):
    global NEXT_REQUEST
    if paced:
        with LOCK:
            delay = max(0, NEXT_REQUEST - time.monotonic())
            NEXT_REQUEST = time.monotonic() + delay + 1.26  # Below TfL's anonymous rate limit.
        time.sleep(delay)
    raw = subprocess.check_output(["curl", "--fail", "--silent", "--show-error",
                                   "--retry", "3", "--max-time", "35", url])
    return json.loads(raw)


def board(query, cache):
    line, origin, neighbour = query
    saved = cache / ("-".join(query) + ".json")
    if saved.exists():
        return json.loads(saved.read_bytes())
    url = f"https://api.tfl.gov.uk/Line/{line}/Timetable/{origin}/to/{neighbour}"
    # A timetable may have no journeys for a published route, e.g. an exhibition shuttle.
    data = fetch(url, paced=True)
    if data.get("lineId") != line or data.get("timetable", {}).get("departureStopId") != origin:
        if data.get("lineId") == line and not data.get("timetable"):
            return []
        raise ValueError(f"Unexpected TfL response for {query}")
    groups = collections.defaultdict(set)
    for route in data["timetable"]["routes"]:
        calls = {str(row["id"]): tuple(item["stopId"] for item in row["intervals"])
                 for row in route["stationIntervals"]}
        for service in route["schedules"]:
            for train in service.get("knownJourneys", []):
                stops = calls.get(str(train["intervalId"]), ())
                if not stops:
                    continue
                minute = int(train["hour"]) * 60 + int(train["minute"])
                if not 0 <= minute < 48 * 60:
                    raise ValueError("Invalid TfL day minute")
                groups[service["name"], stops].add(minute)
    result = [[day, stops, sorted(minutes)] for (day, stops), minutes in sorted(groups.items())]
    saved.write_text(json.dumps(result, separators=(",", ":")))
    return result


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--network", type=Path, default=ROOT / "FirstMile/NetworkMaps/network-london.json")
    parser.add_argument("--output", type=Path, default=ROOT / "FirstMile/OfflineTimetables/offline-london.json")
    parser.add_argument("--cache", type=Path, help="Reuse results from an interrupted run on the same UTC date")
    args = parser.parse_args()
    stamp = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    net = json.loads(args.network.read_bytes())
    lines = {line["id"]: line["symbol"] for line in net["lines"]}
    queries = sorted({(line["id"], a, b) for line in net["lines"] for path in line["patterns"]
                      for a, b in zip(path["stops"], path["stops"][1:]) if a != b})
    groups = collections.defaultdict(set)
    holiday_data = fetch("https://www.gov.uk/bank-holidays.json")["england-and-wales"]["events"]
    holidays = {e["date"]: e["title"] for e in holiday_data}
    with tempfile.TemporaryDirectory() as tmp:
        cache = (args.cache or Path(tmp)) / stamp
        cache.mkdir(parents=True, exist_ok=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            jobs = {executor.submit(board, q, cache): q for q in queries}
            errors = []
            for n, job in enumerate(concurrent.futures.as_completed(jobs), 1):
                line, origin, neighbour = jobs[job]
                try:
                    for day, stops, minutes in job.result():
                        groups[origin, lines[line], day, tuple(stops)].update(minutes)
                except Exception as error:
                    errors.append((line, origin, neighbour, str(error)))
                if n % 100 == 0:
                    print(f"TfL boards {n}/{len(jobs)}, errors {len(errors)}", flush=True)
    if errors:
        print(errors[:15], flush=True)
        raise ValueError(f"{len(errors)} TfL responses failed; retaining previous package")
    stations = collections.defaultdict(list)
    for (origin, symbol, day, stops), minutes in sorted(groups.items()):
        stations[origin].append({"line": symbol, "days": day, "calls": stops, "minutes": sorted(minutes)})
    if len(stations) < 260 or len({g["line"] for s in stations.values() for g in s}) != 11:
        raise ValueError("Incomplete London network")
    package = {"version": 1, "checked": stamp, "holidays": holidays, "stations": stations}
    temp = args.output.with_suffix(".tmp")
    temp.write_text(json.dumps(package, ensure_ascii=False, separators=(",", ":")))
    os.replace(temp, args.output)
    print(f"TfL: {len(stations)} stations, {len(groups)} groups, {args.output.stat().st_size} bytes")


if __name__ == "__main__":
    main()
