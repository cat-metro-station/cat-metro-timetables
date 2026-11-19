"""Build the public offline timetable index from validated per-city candidates."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
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


def metadata(city, data, lines):
    x = json.loads(data)
    require(x["version"] == 1 and x["city"] == city, "wrong city or schema")
    source = date.fromisoformat(x["sourceDate"])
    require(source.isoformat() == x["sourceDate"], "invalid source date")
    require(x["attribution"]["source"] and x["attribution"]["url"].startswith("https://"), "missing credits")
    calendars = x["calendars"]
    ends = []
    for c in calendars.values():
        require(integer(c["days"], 0, 127), "invalid weekday mask")
        if c["days"]:
            require(day(c["start"]) <= day(c["end"]), "inverted calendar")
            ends.append(c["end"])
        for key in ("added", "removed"):
            for exception in c[key]:
                day(exception)
                ends.append(exception)
    require(ends, "no service dates")
    through = x.get("validThrough", max(ends))
    day(through)
    require(through <= max(ends), "expiry exceeds service coverage")
    require(x["patterns"], "no train patterns")
    covered = set()
    for p in x["patterns"]:
        stops = p["stops"]
        require(len(stops) >= 1 and set(stops) <= set(lines[p["line"]]), "incompatible station or line")
        covered.add(p["line"])
        for key in ("approximate", "noPickup", "noDropOff"):
            require(all(integer(i, 0, len(stops)-1) for i in p.get(key, [])), "bad stop index")
        require(p["timings"], "no train times")
        for t in p["timings"]:
            dep = t["offsets"]
            arr = t.get("arrivalOffsets", dep)
            require(t["service"] in calendars and len(dep) == len(arr) == len(stops), "invalid timing")
            require(all(integer(v, 0, 259200) for v in dep + arr), "invalid offset")
            require(dep[0] == 0 and all(a <= d for a, d in zip(arr, dep))
                    and all(d <= a for d, a in zip(dep, arr[1:])), "nonchronological train")
            require(t["starts"] and all(integer(v, 0, 259200) for v in t["starts"]), "invalid departure")
    require(set(lines) <= covered, "missing mapped line")
    return {"sha256": hashlib.sha256(data).hexdigest(), "validThrough": through, "sourceDate": source.isoformat()}


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
            data = candidate.read_bytes()
            entry = metadata(city, data, lines)
            prev = entries.get(city)
            if prev and entry == prev:
                continue
            require(day(entry["validThrough"]) >= today, "upstream timetable expired")
            require(prev is None or (entry["validThrough"] >= prev["validThrough"] and
                                     entry["sourceDate"] >= prev["sourceDate"]), "older upstream timetable")
            obj = assets / (entry["sha256"] + ".json")
            if obj.exists():
                require(obj.read_bytes() == data, "corrupted immutable object")
            else:
                obj.write_bytes(data)
            updates[city] = entry
        except (KeyError, ValueError, TypeError, OSError) as exc:
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
