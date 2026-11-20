#!/usr/bin/env python3
"""Build city bus packages from BODS regional GTFS or the NTA Northern Ireland GTFS.

Install duckdb==1.4.3. National GTFS is parsed once for all selected cities.
Inputs are public operator schedules, official boarding coordinates and, when
published, NaPTAN compass bearings. The SQLite calls view expands compressed
stop-specific clock lists losslessly. No timetable is extended beyond its calendar.
"""
import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import urllib.request
import zipfile
import duckdb

ROOT = Path(__file__).resolve().parents[1]
NAPTAN = "https://naptan.api.dft.gov.uk/v1/access-nodes?dataFormat=csv"
COMPASS = {d: i * 22.5 for i, d in enumerate("N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split())}
WEEK = "monday tuesday wednesday thursday friday saturday sunday".split()


def fetch(url, dest):
    if dest.is_file():
        return dest
    temp = dest.with_suffix(".partial")
    with urllib.request.urlopen(url, timeout=240) as src, temp.open("wb") as out:
        shutil.copyfileobj(src, out, 1024 * 1024)
    os.replace(temp, dest)
    return dest


def iterate(archive, table):
    with archive.open(table + ".txt") as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def datekey(day):
    return day.year * 10000 + day.month * 100 + day.day


def dates(archive, today):
    start = today - dt.timedelta(days=8)
    end = today + dt.timedelta(days=90)
    span = {
        datekey(start + dt.timedelta(days=i)): (start + dt.timedelta(days=i)).weekday()
        for i in range((end - start).days + 1)
    }
    active = {}
    if "calendar.txt" in archive.namelist():
        for row in iterate(archive, "calendar"):
            first, last = int(row["start_date"]), int(row["end_date"])
            active[row["service_id"]] = {
                day for day, weekday in span.items() if first <= day <= last and row[WEEK[weekday]] == "1"
            }
    if "calendar_dates.txt" in archive.namelist():
        for row in iterate(archive, "calendar_dates"):
            day = int(row["date"])
            if day in span:
                days = active.setdefault(row["service_id"], set())
                if row["exception_type"] == "1":
                    days.add(day)
                elif row["exception_type"] == "2":
                    days.discard(day)
    return [(service, day) for service, ids in active.items() for day in sorted(ids)]


def csv_tables(cx, archive, work):
    for table in ("stops", "routes", "trips", "stop_times", "agency"):
        member = table + ".txt"
        file = work / member
        if archive.getinfo(member).file_size > 1_500_000_000:
            file = file.with_suffix(".csv.gz")
            open_out = lambda: gzip.open(file, "wb", compresslevel=1)
        else:
            open_out = lambda: file.open("wb")
        with archive.open(member) as src, open_out() as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        # Views cannot have prepared parameters. DuckDB's relation API escapes paths.
        cx.read_csv(
            str(file),
            all_varchar=True,
            header=True,
            delimiter=",",
            quotechar='"',
            escapechar='"',
            sample_size=1000,
        ).create_view("src_" + table)
    cx.execute(
        "CREATE TABLE frequencies(trip_id VARCHAR,start_time VARCHAR,end_time VARCHAR,headway_secs INT,exact_times INT)"
    )
    if "frequencies.txt" in archive.namelist():
        data = [
            (r["trip_id"], r["start_time"], r["end_time"], int(r["headway_secs"]), int(r.get("exact_times") or 0))
            for r in iterate(archive, "frequencies")
        ]
        if data:
            cx.executemany("INSERT INTO frequencies VALUES(?,?,?,?,?)", data)


def regional_tables(cx, archive, work, today, bounds, naptan):
    csv_tables(cx, archive, work)
    cx.execute("CREATE TABLE days(service_id VARCHAR,day INTEGER)")
    days = dates(archive, today)
    if not days:
        raise ValueError("No active GTFS service dates in this feed")
    dayfile = work / "days.csv"
    with dayfile.open("w", newline="") as dst:
        w = csv.writer(dst)
        w.writerow(["service_id", "day"])
        w.writerows(days)
    cx.execute("INSERT INTO days SELECT service_id,day FROM read_csv(?, all_varchar=true)", [str(dayfile)])
    cx.execute("CREATE TABLE areas(city VARCHAR,south DOUBLE,west DOUBLE,north DOUBLE,east DOUBLE)")
    cx.executemany("INSERT INTO areas VALUES(?,?,?,?,?)", [(c, *box) for c, box in bounds.items()])
    cx.execute(
        """
        CREATE TABLE stops AS SELECT stop_id, COALESCE(NULLIF(stop_name,''),stop_id) AS name,
          try_cast(stop_lat as double) lat,try_cast(stop_lon as double) lon FROM src_stops
          WHERE try_cast(stop_lat AS double) BETWEEN -90 AND 90 AND try_cast(stop_lon AS double) BETWEEN -180 AND 180;
        CREATE TABLE routes AS SELECT r.route_id,r.agency_id,
          coalesce(NULLIF(trim(r.route_short_name),''),NULLIF(trim(r.route_long_name),''),r.route_id) AS name,
          concat_ws(' · ',a.agency_name,nullif(nullif(trim(r.route_long_name),''),trim(r.route_short_name))) AS detail
          FROM src_routes r LEFT JOIN src_agency a ON r.agency_id=a.agency_id
          WHERE try_cast(r.route_type AS INT) IN (3,11) OR try_cast(r.route_type AS INT) BETWEEN 200 AND 209
              OR try_cast(r.route_type AS INT) BETWEEN 700 AND 899;
        CREATE TABLE trips AS SELECT row_number() OVER() AS tid,t.trip_id,t.route_id,t.service_id,
          nullif(trim(t.trip_headsign),'') AS headsign,coalesce(t.direction_id,'') direction_id
          FROM src_trips t JOIN routes r USING(route_id)
          SEMI JOIN days d USING(service_id);
        CREATE MACRO clock_s(clk) AS (try_cast(split_part(clk,':',1) AS INT)*3600
          +try_cast(split_part(clk,':',2) AS INT)*60+try_cast(split_part(clk,':',3) AS INT));
        """
    )
    if cx.execute(
        "SELECT COUNT(*) FROM frequencies WHERE headway_secs<=0 OR clock_s(end_time)<=clock_s(start_time)"
    ).fetchone()[0]:
        raise ValueError("Invalid frequency window in GTFS")
    cols = {r[0] for r in cx.sql("DESCRIBE src_stop_times").fetchall()}
    head = "NULLIF(trim(st.stop_headsign),'')" if "stop_headsign" in cols else "NULL::VARCHAR"
    pickup = "coalesce(st.pickup_type,'0') <> '1'" if "pickup_type" in cols else "true"
    approximate = "coalesce(st.timepoint,'1')='0'" if "timepoint" in cols else "false"
    cx.execute(
        f"""
        CREATE TABLE raw AS SELECT t.tid,st.stop_id,try_cast(st.stop_sequence as INT) AS seq,
          clock_s(coalesce(nullif(st.departure_time,''),st.arrival_time)) sec,
          {head} AS headsign,{pickup} AS board,{approximate} AS approx
          FROM src_stop_times st JOIN trips t USING(trip_id);
        CREATE TABLE termini AS SELECT tid,arg_max(stop_id,seq) stop_id FROM raw GROUP BY tid;
        CREATE TABLE area_stops AS SELECT a.city,s.stop_id FROM areas a JOIN stops s
          ON s.lat BETWEEN a.south AND a.north AND s.lon BETWEEN a.west AND a.east;
        CREATE TABLE area_routes AS SELECT DISTINCT a.city,t.route_id FROM area_stops a JOIN raw x USING(stop_id)
          JOIN trips t USING(tid) WHERE x.board;
        """
    )
    # Use the official NaPTAN road direction first. For other stops, only use an
    # observed next-stop direction when the observed bearings agree.
    cx.execute("CREATE TABLE official(stop_id VARCHAR,bearing DOUBLE)")
    if naptan:
        cx.read_csv(
            str(naptan),
            all_varchar=True,
            header=True,
            delimiter=",",
            quotechar='"',
            escapechar='"',
            sample_size=1000,
        ).create_view("naptan")
        cm = " ".join(f"WHEN '{key}' THEN {value}" for key, value in COMPASS.items())
        cx.execute(
            f"""INSERT INTO official SELECT ATCOCode,max(CASE upper(trim(Bearing)) {cm} END)
                       FROM naptan SEMI JOIN stops ON ATCOCode=stop_id WHERE lower(Status)='active' GROUP BY ATCOCode"""
        )

    # All national GTFS data is read once. Partition the further timetable
    # compression by number of routes so it also runs within hosted CI memory.
    for table in ("stop_times", "trips", "routes", "stops", "agency"):
        cx.execute("DROP VIEW src_" + table)
        for filename in (table + ".txt", table + ".csv.gz"):
            (work / filename).unlink(missing_ok=True)


def city_groups(cx):
    groups = []
    running = 0
    for city, n in cx.sql("SELECT city,count(*) n FROM area_routes GROUP BY city ORDER BY n DESC,city").fetchall():
        if not groups or running + n > 900:
            groups.append([])
            running = 0
        groups[-1].append(city)
        running += n
    return groups


def compress_group(cx, cities):
    cx.execute("CREATE TEMP TABLE group_cities(city VARCHAR)")
    cx.executemany("INSERT INTO group_cities VALUES(?)", [(city,) for city in cities])
    cx.execute(
        """
        CREATE TEMP TABLE wanted AS SELECT t.* FROM trips t SEMI JOIN
            (SELECT route_id FROM area_routes SEMI JOIN group_cities USING(city)) ar USING(route_id);
        CREATE TEMP TABLE slice AS SELECT raw.* FROM raw SEMI JOIN wanted USING(tid);
        CREATE TEMP TABLE frequency_starts AS SELECT t.tid,unnest(range(clock_s(f.start_time),clock_s(f.end_time),f.headway_secs)) AS sec,
          (f.exact_times<>1) AS approximate FROM frequencies f JOIN wanted t USING(trip_id);
        CREATE TEMP TABLE anchors AS SELECT x.tid,arg_min(x.sec,x.seq) origin FROM slice x SEMI JOIN frequency_starts f USING(tid) GROUP BY x.tid;
        CREATE TEMP VIEW effective AS SELECT x.* FROM slice x ANTI JOIN frequency_starts f USING(tid)
          UNION ALL SELECT x.* REPLACE(CAST(x.sec-a.origin+f.sec AS INT) AS sec,(x.approx OR f.approximate) AS approx)
          FROM slice x JOIN frequency_starts f USING(tid) JOIN anchors a USING(tid);
        CREATE TEMP TABLE timed AS SELECT t.route_id,x.stop_id,t.service_id,coalesce(x.headsign,t.headsign,s.name,'') AS headsign,
          CAST(x.approx AS INTEGER) AS approximate,list(distinct x.sec order by x.sec) AS times
          FROM effective x JOIN wanted t USING(tid) JOIN termini last USING(tid) LEFT JOIN stops s ON s.stop_id=last.stop_id
          WHERE x.board AND x.sec BETWEEN 0 AND 604799 GROUP BY ALL;
        CREATE TEMP TABLE groups_ AS SELECT * EXCLUDE(times), times[1] AS base,
          list_transform(times, v -> v-times[1]) AS deltas FROM timed;
        DROP TABLE timed;
        CREATE TEMP TABLE profiles AS SELECT row_number() OVER(ORDER BY deltas) AS pid,deltas
          FROM (SELECT DISTINCT deltas FROM groups_);
        CREATE TEMP TABLE schedules AS SELECT g.* EXCLUDE(deltas),p.pid FROM groups_ g JOIN profiles p USING(deltas);
        DROP TABLE groups_;
        """
    )
    cx.execute(
        """
        CREATE TEMP TABLE bearing AS WITH missing AS
          (SELECT DISTINCT s.stop_id FROM schedules s LEFT JOIN official n USING(stop_id) WHERE n.bearing IS NULL),
          path AS (SELECT x.stop_id,lead(x.stop_id) OVER(PARTITION BY x.tid ORDER BY x.seq) AS dst
            FROM slice x SEMI JOIN (SELECT DISTINCT y.tid FROM slice y SEMI JOIN missing m USING(stop_id)) t USING(tid)),
          edges AS (SELECT p.stop_id,p.dst,count(*) AS n FROM path p SEMI JOIN missing m USING(stop_id)
            WHERE dst IS NOT NULL AND p.stop_id<>p.dst GROUP BY 1,2),
          heading AS (SELECT e.stop_id,n,atan2((b.lon-a.lon)*cos(radians(a.lat)),b.lat-a.lat) ang
            FROM edges e JOIN stops a ON a.stop_id=e.stop_id JOIN stops b ON b.stop_id=e.dst
            WHERE abs(b.lon-a.lon)+abs(b.lat-a.lat)>0.000015),
          vectors AS (SELECT stop_id, sum(n*sin(ang))/sum(n) x,sum(n*cos(ang))/sum(n) y FROM heading GROUP BY 1)
          SELECT stop_id,(degrees(atan2(x,y))+360)%360 AS value FROM vectors WHERE x*x+y*y>0.40;
        """
    )


SCHEMA = """
PRAGMA user_version=1;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE routes(id TEXT PRIMARY KEY,name TEXT NOT NULL,detail TEXT) WITHOUT ROWID;
CREATE TABLE stops(id TEXT PRIMARY KEY,name TEXT NOT NULL,lat REAL NOT NULL,lon REAL NOT NULL,bearing REAL) WITHOUT ROWID;
CREATE TABLE stop_routes(stop_id TEXT,route_id TEXT,PRIMARY KEY(stop_id,route_id)) WITHOUT ROWID;
CREATE INDEX routes_stops ON stop_routes(route_id,stop_id);
CREATE INDEX map_bounds ON stops(lat,lon);
CREATE TABLE service_days(service_id TEXT,day INTEGER,PRIMARY KEY(service_id,day)) WITHOUT ROWID;
CREATE TABLE offsets(profile INTEGER,delta INTEGER,PRIMARY KEY(profile,delta)) WITHOUT ROWID;
CREATE TABLE signs(id INTEGER PRIMARY KEY,label TEXT NOT NULL);
CREATE TABLE windows(stop_id TEXT,route_id TEXT,service_id TEXT,sign INTEGER,approximate INTEGER,base INTEGER,profile INTEGER);
CREATE VIEW calls AS SELECT w.stop_id,w.route_id,w.service_id,h.label AS headsign,w.base+o.delta AS seconds,w.approximate
FROM windows w JOIN signs h ON h.id=w.sign JOIN offsets o ON o.profile=w.profile;
"""


def insert(db, cx, table, sql, parameters=()):
    cursor = cx.execute(sql, parameters)
    n = len(cursor.description)
    statement = "INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in range(n)) + ")"
    while True:
        batch = cursor.fetchmany(32_768)
        if not batch:
            return
        db.executemany(statement, batch)


def build_city(cx, city, feed, stage, work):
    cx.execute("CREATE OR REPLACE TEMP TABLE chosen AS SELECT route_id FROM area_routes WHERE city=?", [city])
    cx.execute("CREATE OR REPLACE TEMP TABLE sc AS SELECT * FROM schedules SEMI JOIN chosen USING(route_id)")
    cx.execute(
        "CREATE OR REPLACE TEMP TABLE head AS SELECT row_number() OVER(ORDER BY headsign) id,headsign FROM (SELECT DISTINCT headsign FROM sc)"
    )
    until = cx.execute("SELECT MAX(day) FROM days SEMI JOIN sc USING(service_id)").fetchone()[0]
    if until is None:
        raise ValueError(f"{city} has no timetabled bus routes in its area")
    # One operator can stop publishing dates earlier than the city-wide horizon.
    refresh = cx.execute(
        """
        SELECT min(last_day) FROM (
          SELECT r.agency_id,max(d.day) last_day FROM (SELECT DISTINCT route_id,service_id FROM sc) pairs
          JOIN routes r USING(route_id) JOIN days d USING(service_id) GROUP BY r.agency_id
          HAVING max(d.day)>=?)
        """,
        [datekey(feed.get("asOf", dt.date.today()))],
    ).fetchone()[0]
    dbfile = work / ("bus-" + city + ".sqlite")
    with sqlite3.connect(dbfile) as db:
        db.executescript("PRAGMA page_size=8192;PRAGMA synchronous=OFF;PRAGMA journal_mode=OFF;" + SCHEMA)
        db.executemany(
            "INSERT INTO metadata VALUES (?,?)",
            [
                ("city", city),
                ("timeZone", "Europe/London"),
                ("validThrough", str(until)),
                ("sources", feed["operator"]),
                (
                    "sourceDetails",
                    json.dumps(
                        [dict((k, feed[k]) for k in ("id", "operator", "source", "license"))], ensure_ascii=False
                    ),
                ),
            ],
        )
        insert(
            db,
            cx,
            "routes",
            "SELECT route_id,name,detail FROM routes SEMI JOIN sc USING(route_id) ORDER BY route_id",
        )
        insert(
            db,
            cx,
            "stops",
            """SELECT s.stop_id,s.name,s.lat,s.lon,coalesce(n.bearing,b.value) FROM stops s
              SEMI JOIN sc USING(stop_id) LEFT JOIN official n USING(stop_id) LEFT JOIN bearing b USING(stop_id) ORDER BY s.stop_id""",
        )
        insert(db, cx, "stop_routes", "SELECT DISTINCT stop_id,route_id FROM sc ORDER BY 1,2")
        insert(
            db, cx, "service_days", "SELECT service_id,day FROM days SEMI JOIN sc USING(service_id) ORDER BY 1,2"
        )
        insert(db, cx, "signs", "SELECT id,headsign FROM head ORDER BY id")
        insert(
            db, cx, "offsets", "SELECT pid,unnest(deltas) delta FROM profiles SEMI JOIN sc USING(pid) ORDER BY 1,2"
        )
        insert(
            db,
            cx,
            "windows",
            "SELECT stop_id,route_id,service_id,h.id,approximate,base,pid FROM sc JOIN head h USING(headsign) ORDER BY stop_id,route_id,service_id,base",
        )
        db.execute("CREATE INDEX stop_clocks ON windows(stop_id,route_id)")
        stops = db.execute("SELECT count(*) FROM stops").fetchone()[0]
        routes = db.execute("SELECT count(*) FROM routes").fetchone()[0]
        if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok" or db.execute(
            "SELECT 1 FROM calls LIMIT 1"
        ).fetchone() != (1,):
            raise ValueError("Incomplete database " + city)
    temp = stage / (city + ".new")
    with dbfile.open("rb") as src, temp.open("wb") as compressed:
        with gzip.GzipFile(fileobj=compressed, filename="", mtime=0, mode="wb", compresslevel=6) as stream:
            shutil.copyfileobj(src, stream, 1024 * 1024)
    sha = hashlib.sha256()
    with temp.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    name = f"{city}-{digest[:16]}.sqlite.gz"
    temp.replace(stage / name)
    entry = {
        "path": "packages/bus/data/" + name,
        "sha256": digest,
        "bytes": (stage / name).stat().st_size,
        "uncompressedBytes": dbfile.stat().st_size,
        "validThrough": str(until),
        "refreshThrough": str(refresh or until),
        "routes": routes,
        "stops": stops,
    }
    (stage / (city + ".entry.json")).write_text(json.dumps(entry, sort_keys=True) + "\n")
    dbfile.unlink()
    print(city, routes, "routes", stops, "stops", round(entry["bytes"] / 1e6, 1), "MB", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "inputs" / "bus-gb.json")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--city", action="append")
    parser.add_argument("--region", action="append")
    parser.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--memory", default="2300MB")
    parser.add_argument("--threads", type=int, default=3)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(args.catalog.read_text())
    selected = {}
    for city, feeds in catalog.items():
        feed = feeds[0]
        if not args.city or city in args.city:
            if not args.region or feed["id"] in args.region:
                selected.setdefault(feed["id"], {})[city] = feed
    if not selected:
        parser.error("No matching city or region")
    for feed_id, cities in selected.items():
        feed = next(iter(cities.values()))
        print("Reading", feed_id, flush=True)
        archive_path = fetch(feed["url"], args.cache / (feed_id + ".zip"))
        naptan = fetch(NAPTAN, args.cache / "naptan.csv") if feed_id.startswith("bods-") else None
        with tempfile.TemporaryDirectory(dir=args.cache, prefix="british-bus-") as temp, zipfile.ZipFile(
            archive_path
        ) as archive:
            work = Path(temp)
            with duckdb.connect(str(work / "regional.duckdb")) as cx:
                cx.execute("SET memory_limit=?", [args.memory])
                cx.execute("SET threads=?", [args.threads])
                cx.execute("SET preserve_insertion_order=false")
                regional_tables(cx, archive, work, args.as_of, {c: f["bounds"] for c, f in cities.items()}, naptan)
                groups = city_groups(cx)
                if {city for group in groups for city in group} != set(cities):
                    raise ValueError("One or more cities have no dated bus routes")
                for group in groups:
                    print("Compressing", ", ".join(group), flush=True)
                    compress_group(cx, group)
                    for city in group:
                        build_city(cx, city, {**cities[city], "asOf": args.as_of}, args.output, work)
                    cx.execute("DROP VIEW effective")
                    for table in (
                        "sc",
                        "chosen",
                        "head",
                        "wanted",
                        "slice",
                        "bearing",
                        "profiles",
                        "schedules",
                        "group_cities",
                        "frequency_starts",
                        "anchors",
                    ):
                        cx.execute("DROP TABLE " + table)


if __name__ == "__main__":
    main()
