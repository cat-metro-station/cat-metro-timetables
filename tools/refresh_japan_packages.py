"""Refresh public Japan packages. Run with --packages packages; see --help for replay inputs."""
import argparse
import csv
import datetime as dt
import io
import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.request import Request, urlopen

TOEI = {"A": "asakusa", "I": "mita", "S": "shinjuku", "E": "oedo"}
UPSTREAM = "nagix/mini-tokyo-3d"
KYOTO_CSV = "https://www2.city.kyoto.lg.jp/kotsu/tikadia/diadata/tn{}_dia_utf8.csv"
HOLIDAYS = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"
KYOTO_STOPS = {
    "K": "国際会館 松ヶ崎 北山 北大路 鞍馬口 今出川 丸太町 烏丸御池 四条 五条 京都 九条 十条 くいな橋 竹田",
    "T": "六地蔵 石田 醍醐 小野 椥辻 東野 山科 御陵 蹴上 東山 三条京阪 京都市役所前 烏丸御池 二条城前 二条 西大路御池 太秦天神川",
}
KYOTO_TERMINALS = {
    "国際会館": "kokusaikaikan", "竹田": "takeda", "新田辺": "shinTanabe", "近鉄奈良": "kintetsuNara",
    "太秦天神川": "uzumasaTenjingawa", "京都市役所前": "kyotoShiyakushoMae", "六地蔵": "rokujizo", "びわ湖浜大津": "biwakoHamaotsu",
}


def fetch(url, filename, inputs):
    if inputs:
        return (inputs / filename).read_bytes()
    request = Request(url, headers={"User-Agent": "cat-metro-timetable-updater", "Accept": "*/*"})
    with urlopen(request, timeout=35) as response:
        raw = response.read(15_000_001)
    if len(raw) > 15_000_000:
        raise ValueError(f"Unexpected source size for {filename}")
    return raw


def holidays(inputs):
    raw = fetch(HOLIDAYS, "holidays.csv", inputs)
    by_year = defaultdict(set)
    for row in csv.reader(io.StringIO(raw.decode("cp932"))):
        if row and re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", row[0]):
            day = dt.datetime.strptime(row[0], "%Y/%m/%d").date()
            if day.year >= 2026:
                by_year[day.year].add(day.month * 100 + day.day)
    # Publish complete lists only; do not infer future holidays.
    years = {year: sorted(days) for year, days in by_year.items() if 101 in days and 1123 in days and len(days) >= 16}
    if dt.datetime.now(dt.timezone.utc).year not in years:
        raise ValueError("Cabinet Office holiday calendar missing for the current year")
    return years


def service_minute(text):
    hour, minute = map(int, text.split(":"))
    if not (0 <= hour <= 26 and 0 <= minute < 60):
        raise ValueError(f"Invalid service time {text}")
    return (hour + 24 if hour < 3 else hour) * 60 + minute


def rebuild_kyoto(seed, inputs):
    result = defaultdict(lambda: defaultdict(dict))
    # The regular CSVs do not contain the city's special autumn timetable.
    for code, groups in seed["stations"].items():
        for group in groups:
            if "autumn" in group:
                result[code][group["direction"], group["terminal"]]["autumn"] = group["autumn"]
    for line, prefix in [("K", "02"), ("T", "01")]:
        stations = {name: f"{line}{i:02d}" for i, name in enumerate(KYOTO_STOPS[line].split(), 1)}
        for calendar, day in [("01", "weekday"), ("02", "holiday")]:
            for digit, direction in [("00", "down"), ("01", "up")]:
                digits = prefix + calendar + digit
                raw = fetch(KYOTO_CSV.format(digits), digits + ".csv", inputs)
                rows = iter(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
                head = next(row for row in rows if row and row[0] == "行先（終着）")
                if not set(stations).issubset(head):
                    raise ValueError(f"Incomplete Kyoto station columns in {digits}")
                for row in rows:
                    if not row:
                        continue
                    if len(row) != len(head):
                        raise ValueError(f"Invalid Kyoto row size for {digits}")
                    terminal = KYOTO_TERMINALS[row[0]]
                    for name, time in zip(head[2:], row[2:]):
                        if name in stations and time and name != row[0]:
                            days = result[stations[name]][direction, terminal]
                            days.setdefault(day, []).append(service_minute(time))
    rebuilt = {}
    for code, groups in sorted(result.items()):
        rebuilt[code] = [
            {"direction": direction, "terminal": terminal, "weekday": sorted(days.get("weekday", [])),
             "holiday": sorted(days.get("holiday", [])), **({"autumn": days["autumn"]} if "autumn" in days else {})}
            for (direction, terminal), days in sorted(groups.items())
        ]
    if len(rebuilt) != 32:
        raise ValueError("Incomplete Kyoto coverage")
    for code, groups in rebuilt.items():
        expected_directions = {g["direction"] for g in seed["stations"][code]}
        if {g["direction"] for g in groups} != expected_directions:
            raise ValueError(f"Missing Kyoto direction at {code}")
        for direction in expected_directions:
            for day in (["weekday", "holiday", "autumn"] if code[0] == "K" else ["weekday", "holiday"]):
                times = [v for g in groups if g["direction"] == direction for v in g.get(day, [])]
                if not times or len(times) != len(set(times)):
                    raise ValueError(f"Invalid {day} departures at {code} {direction}")
    revisions = dict(seed.get("revision", {"K": "2026-03-14", "T": "2025-02-22"}))
    for line in KYOTO_STOPS:
        if any(rebuilt[code] != seed["stations"][code] for code in rebuilt if code.startswith(line)):
            revisions[line] = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return {**seed, "stations": rebuilt, "revision": revisions}


def rebuild_tokyo(seed, inputs):
    all_trips = {}
    revisions = {}
    for symbol, name in TOEI.items():
        path = f"data/train-timetables/toei-{name}.json"
        history = f"https://api.github.com/repos/{UPSTREAM}/commits?path={path}&per_page=1"
        commit = json.loads(fetch(history, f"toei-{name}-commit.json", inputs))[0]
        revision = commit["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("Invalid upstream git revision")
        revisions[symbol] = dt.date.fromisoformat(commit["commit"]["committer"]["date"][:10]).isoformat()
        url = f"https://raw.githubusercontent.com/{UPSTREAM}/{revision}/{path}"
        for trip in json.loads(fetch(url, f"toei-{name}.json", inputs)):
            if trip["r"].lower() != "toei." + name or trip["id"] in all_trips:
                raise ValueError(f"Unexpected or duplicate trip in {path}")
            all_trips[trip["id"]] = trip
    network = {s for line in seed["toei"].values() for p in line["patterns"] for s in p["stops"] if s}
    def station(event):
        name = event["s"].split(".")[2]
        slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"([a-z])([A-Z])", r"\1-\2", name).lower()).strip("-")
        return slug if slug in network else ""
    output = {}
    for symbol, name in TOEI.items():
        shapes, indexes, runs = [], {}, []
        for trip in all_trips.values():
            if trip["r"].lower() != "toei." + name:
                continue
            events = trip["tt"]
            if len(events) < 2:
                continue
            calls = tuple(station(event) for event in events)
            day = {"Weekday": 0, "SaturdayHoliday": 1}[trip["id"].split(".")[-1]]
            minutes = [service_minute(event["d"]) if event.get("d") else -1 for event in events]
            issued = [minute for minute in minutes if minute >= 0]
            if issued != sorted(issued):
                raise ValueError(f"Non-monotonic departure times for {trip['id']}")
            # Follow verified one-to-one continuations within the four licensed Toei feeds.
            onwards, seen, current = [], {trip["id"]}, trip
            while len(current.get("nt", [])) == 1:
                next_id = current["nt"][0]
                next_trip = all_trips.get(next_id)
                if next_id in seen or not next_trip or current["tt"][-1]["s"].split(".")[-1] != next_trip["tt"][0]["s"].split(".")[-1]:
                    break
                seen.add(next_id)
                onwards.extend(filter(None, (station(event) for event in next_trip["tt"][1:])))
                current = next_trip
            terminal = seed["terminalNames"][trip["ds"][0]]
            kind = trip["y"].split(".")[-1]
            key = (calls, tuple(onwards), tuple(terminal), kind)
            if key not in indexes:
                indexes[key] = len(shapes)
                shapes.append({"stops": calls, "onward": onwards, "terminal": terminal, "kind": kind})
            runs.append([day, indexes[key], *minutes])
        prior_stops = {s for p in seed["toei"][symbol]["patterns"] for s in p["stops"] if s}
        new_stops = {s for p in shapes for s in p["stops"] if s}
        if not prior_stops.issubset(new_stops) or {r[0] for r in runs} != {0, 1}:
            raise ValueError(f"Incomplete Toei timetable for {symbol}")
        output[symbol] = {"revision": revisions[symbol], "patterns": shapes, "runs": runs}
    return {**seed, "toei": output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", required=True, type=Path, help="Directory containing both existing Japan city packages")
    parser.add_argument("--inputs", type=Path, help="Replay downloaded public feeds: 8 Kyoto NNNNNN.csv files, four toei-<line>.json plus GitHub toei-<line>-commit.json, and holidays.csv")
    args = parser.parse_args()
    public_holidays = holidays(args.inputs)
    updates = {}
    for filename, rebuild in [("japan-tokyo.json", rebuild_tokyo), ("kyoto-station-departures.json", rebuild_kyoto)]:
        path = args.packages / filename
        package = rebuild(json.loads(path.read_text()), args.inputs)
        package["holidays"] = public_holidays
        package.setdefault("sources", {})["holidays"] = HOLIDAYS
        updates[path] = json.dumps(package, ensure_ascii=False, separators=(",", ":")) + "\n"
    for path, contents in updates.items():
        temporary = path.with_suffix(".tmp")
        temporary.write_text(contents)
        temporary.replace(path)
        print(f"Validated {path.name}: {len(contents.encode())} bytes")


if __name__ == "__main__":
    main()
