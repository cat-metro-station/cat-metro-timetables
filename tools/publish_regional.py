"""Build the public offline timetable index from validated per-city candidates."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import sys

ROOT = Path(__file__).resolve().parents[1]


def require(ok, message):
    if not ok:
        raise ValueError(message)


def day(value):
    require(type(value) is int and 20000101 <= value <= 20991231, f"invalid service date {value}")
    return date(value // 10000, value // 100 % 100, value % 100)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def web_url(value):
    if not isinstance(value, str):
        return False
    url = urlsplit(value)
    return url.scheme in ("https", "http") and bool(url.hostname)


def validate_credits(credits):
    require(isinstance(credits["source"], str) and credits["source"].strip()
            and web_url(credits["url"]) and credits["url"].startswith("https://"),
            "missing timetable credits")
    if credits.get("notice") is not None:
        require(isinstance(credits["notice"], str), "invalid attribution notice")
    if credits.get("terms") is not None:
        require(web_url(credits["terms"]), "invalid attribution terms")
    for key in ("estimateSources", "feeds"):
        require(credits.get(key) is None or isinstance(credits[key], list), "invalid attribution " + key)
    for source in credits.get("estimateSources") or []:
        require(isinstance(source["title"], str) and web_url(source["url"]), "invalid estimate source")
    for feed in credits.get("feeds") or []:
        require(web_url(feed["url"]), "missing feed URL")
        if feed.get("serviceThrough") is not None:
            day(feed["serviceThrough"])


def metadata(city, data, lines):
    require(len(data) <= 30_000_000, "package exceeds app size limit")
    x = json.loads(data)
    require(type(x["version"]) is int and x["version"] == 1 and x["city"] == city, "wrong city or schema")
    source = date.fromisoformat(x["sourceDate"])
    require(source.isoformat() == x["sourceDate"], "invalid source date")
    validate_credits(x["attribution"])
    if x.get("allowsReferenceWeek") is not None:
        require(type(x["allowsReferenceWeek"]) is bool, "invalid reference-week setting")
    calendars = x["calendars"]
    ends = []
    for c in calendars.values():
        require(integer(c["days"], 0, 127), "invalid weekday mask")
        require(type(c["start"]) is int and type(c["end"]) is int and c["start"] <= c["end"], "inverted calendar")
        if c["days"]:
            day(c["start"])
            day(c["end"])
            ends.append(c["end"])
        for key in ("added", "removed"):
            require(isinstance(c[key], list), "invalid exception dates")
            for exception in c[key]:
                day(exception)
                ends.append(exception)
    require(ends, "no service dates")
    through = x.get("validThrough", max(ends))
    day(through)
    require(through <= max(ends), "expiry exceeds service coverage")
    require(isinstance(x["patterns"], list) and x["patterns"], "no train patterns")
    covered = set()
    can_board = False
    for p in x["patterns"]:
        stops = p["stops"]
        require(isinstance(stops, list) and stops and set(stops) <= set(lines[p["line"]]), "incompatible station or line")
        covered.add(p["line"])
        for key in ("approximate", "noPickup", "noDropOff"):
            require(isinstance(p[key], list) and all(integer(i, 0, len(stops)-1) for i in p[key]), "bad stop indices")
        for key in ("boundary", "destinationName"):
            require(p.get(key) is None or isinstance(p[key], str), "invalid " + key)
        if p.get("headwayMinutes") is not None:
            require(isinstance(p["headwayMinutes"], list) and all(type(i) is int for i in p["headwayMinutes"]),
                    "invalid headway minutes")
        continuing = p.get("continuesBeyondMap")
        require(continuing is None or type(continuing) is bool, "invalid through train flag")
        require(not continuing or p.get("destinationName"), "missing through destination")
        board_count = len(stops) if continuing else len(stops) - 1
        require(board_count > 0, "pattern has no boarding stop")
        can_board = can_board or any(i not in p["noPickup"] for i in range(board_count))
        require(isinstance(p["timings"], list) and p["timings"], "no train times")
        for t in p["timings"]:
            dep = t["offsets"]
            arr = dep if t.get("arrivalOffsets") is None else t["arrivalOffsets"]
            require(isinstance(dep, list) and isinstance(arr, list)
                    and t["service"] in calendars and len(dep) == len(arr) == len(stops), "invalid timing")
            require(all(integer(v, 0, 86399) for v in dep + arr), "invalid offset")
            require(dep[0] == arr[0] == 0 and all(a <= d for a, d in zip(arr, dep))
                    and all(d <= a for d, a in zip(dep, arr[1:])), "nonchronological train")
            starts = t["starts"]
            require(isinstance(starts, list) and starts and all(integer(v, 0, 172799) for v in starts)
                    and starts == sorted(starts), "invalid or unsorted departures")
    require(can_board and set(lines) <= covered, "missing boarding stop or mapped line")
    return {"sha256": hashlib.sha256(data).hexdigest(), "validThrough": through, "sourceDate": source.isoformat()}


def validate_service_window(package, end, current_end, today):
    services = {t["service"] for p in package["patterns"] for t in p["timings"]}
    calendars = [package["calendars"][s] for s in services]
    require(any((c["days"] and c["end"] >= today and c["start"] <= end)
                or any(today <= d <= end and d not in c["removed"] for d in c["added"])
                for c in calendars), "no remaining service in the published period")
    if current_end >= today:
        require(any((c["days"] and c["start"] <= today) or any(d <= today for d in c["added"]) for c in calendars),
                "replacement service period has not begun")


def check_previous_feeds(old, new):
    previous = {f["url"]: f.get("serviceThrough") for f in old.get("feeds") or []}
    feeds = new.get("feeds") or []
    require(not previous or feeds, "missing previous feed attribution")
    for feed in feeds:
        last = previous.get(feed["url"])
        require(last is None or (feed.get("serviceThrough") or 0) >= last, "earlier operator expiry: " + feed["url"])


def publish(candidates, packages, contracts, today):
    index_file = packages / "regional-index.json"
    index = json.loads(index_file.read_bytes()) if index_file.exists() else {"version": 1, "cities": {}}
    entries = index["cities"]
    require(index["version"] == 1, "unsupported index")
    assets = packages / "regional"
    assets.mkdir(parents=True, exist_ok=True)
    errors = []
    updates = {}
    for city, lines in sorted(contracts.items()):
        candidate = candidates / f"offline-{city}.json"
        if not candidate.exists():
            continue
        try:
            require(candidate.stat().st_size <= 30_000_000, "package exceeds app size limit")
            data = candidate.read_bytes()
            entry = metadata(city, data, lines)
            prev = entries.get(city)
            if prev and entry == prev:
                continue
            new = json.loads(data)
            if prev:
                require(entry["validThrough"] >= prev["validThrough"], "earlier upstream expiry")
                old = json.loads((assets / (prev["sha256"] + ".json")).read_bytes())
                check_previous_feeds(old["attribution"], new["attribution"])
                # A rebuild date or JSON formatting alone does not change any departures.
                old.pop("generatedOn", None)
                new.pop("generatedOn", None)
                if entry["sourceDate"] < prev["sourceDate"]:
                    # A slower job may have observed identical operator files on an earlier day.
                    for snapshot in (old, new):
                        snapshot.pop("sourceDate")
                        for feed in snapshot["attribution"].get("feeds") or []:
                            feed.pop("archiveDate", None)
                    require(entry["validThrough"] == prev["validThrough"] and
                            json.dumps(old, sort_keys=True) == json.dumps(new, sort_keys=True), "older upstream timetable")
                    continue
                if old == new:
                    continue
            require(day(entry["validThrough"]) >= today, "upstream timetable expired")
            today_key = today.year * 10000 + today.month * 100 + today.day
            validate_service_window(new, entry["validThrough"], prev["validThrough"] if prev else 0, today_key)
            obj = assets / (entry["sha256"] + ".json")
            if obj.exists():
                require(obj.read_bytes() == data, "corrupted immutable object")
            else:
                obj.write_bytes(data)
            updates[city] = entry
        except (AttributeError, KeyError, ValueError, TypeError, OSError) as exc:
            errors.append(f"{city}: {exc}")
    entries.update(updates)
    require(set(entries) == set(contracts), "index incomplete: " + ", ".join(sorted(set(contracts)-set(entries))))
    if updates:
        staging = index_file.with_suffix(".tmp")
        staging.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
        staging.replace(index_file)
    print(f"{len(entries)} cities indexed, {len(updates)} updated")
    for error in errors:
        print(error, file=sys.stderr)
    return not errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--packages", type=Path, default=ROOT / "packages")
    args = parser.parse_args()
    contracts = json.loads((ROOT / "inputs/regional-lines.json").read_bytes())
    return publish(args.candidates, args.packages, contracts, date.today())


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
