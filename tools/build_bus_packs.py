#!/usr/bin/env python3
"""Build a compact, independently downloadable SQLite bus timetable from licensed GTFS feeds."""
import argparse
import collections
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

WEEK = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
BUS_TYPES = {3, 11, 200, 201, 202, 203, 204, 205, 206, 207, 208, 209, *range(700, 717), 800}
SCHEMA = """
PRAGMA user_version=1;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE stops(id INTEGER PRIMARY KEY,name TEXT NOT NULL,lat REAL NOT NULL,lon REAL NOT NULL,bearing REAL);
CREATE TABLE routes(id INTEGER PRIMARY KEY,name TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE heads(id INTEGER PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE times(stop_id INTEGER,seconds INTEGER,route_id INTEGER,service_id INTEGER,head INTEGER,approximate INTEGER,
 PRIMARY KEY(stop_id,seconds,route_id,service_id,head,approximate)) WITHOUT ROWID;
CREATE VIEW calls AS SELECT t.stop_id,t.route_id,t.service_id,h.name AS headsign,t.seconds,t.approximate
 FROM times t JOIN heads h ON t.head=h.id;
CREATE TABLE service_days(service_id INTEGER,day INTEGER,PRIMARY KEY(service_id,day)) WITHOUT ROWID;
CREATE TABLE stop_routes(stop_id INTEGER,route_id INTEGER,PRIMARY KEY(stop_id,route_id)) WITHOUT ROWID;
"""
csv.field_size_limit(4_000_000)

def datecode(date):
    return int(date.strftime('%Y%m%d'))

def clock(value):
    try:
        h, m, s = map(int, value.split(':'))
        return h * 3600 + m * 60 + s if 0 <= h < 72 and 0 <= m < 60 and 0 <= s < 60 else None
    except ValueError:
        return None

class Tables:
    def __init__(self, archive):
        self.zip = archive
        self.names = {n.rsplit('/', 1)[-1].lower(): n for n in archive.namelist()
                      if not n.startswith('__MACOSX') and n.lower().endswith('.txt')}

    def rows(self, name, columns):
        if name + '.txt' not in self.names:
            return
        with self.zip.open(self.names[name + '.txt']) as raw:
            with io.TextIOWrapper(raw, encoding='utf-8-sig', newline='', errors='replace') as text:
                reader = csv.reader(text)
                header = [x.strip() for x in next(reader, ())]
                indices = [header.index(c) if c in header else -1 for c in columns.split()]
                for row in reader:
                    yield tuple(row[i].strip() if 0 <= i < len(row) else '' for i in indices)

def service_dates(tables, days):
    service = collections.defaultdict(set)
    for row in tables.rows('calendar', 'service_id start_date end_date ' + ' '.join(WEEK)):
        sid, start, end = row[:3]
        if not (start.isdigit() and end.isdigit()):
            continue
        service[sid].update(n for n, w in days if int(start) <= n <= int(end) and row[3 + w] == '1')
    for sid, date, kind in tables.rows('calendar_dates', 'service_id date exception_type'):
        if date.isdigit() and days[0][0] <= int(date) <= days[-1][0]:
            if kind == '1':
                service[sid].add(int(date))
            elif kind == '2':
                service[sid].discard(int(date))
    return {k: sorted(v) for k, v in service.items() if v}

def feed_file(cache, source):
    key = source['id']
    if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
        raise ValueError('invalid feed identifier')
    path = cache / (key + '.zip')
    if path.is_file():
        return path
    # Independent city jobs can share a feed. Rename only after checking the ZIP.
    handle, temp = tempfile.mkstemp(dir=cache, suffix='.part')
    os.close(handle)
    try:
        for url in (source['url'], source.get('mirror')):
            if not url:
                continue
            proc = subprocess.run(['curl', '-fsSL', '--retry', '2', '--max-time', '180', '--max-filesize',
                                   '1200000000', '--output', temp, url], capture_output=True)
            if proc.returncode == 0 and zipfile.is_zipfile(temp):
                os.replace(temp, path)
                return path
        raise ValueError('no valid GTFS ZIP at registered source')
    finally:
        Path(temp).unlink(missing_ok=True)

def add_feed(db, source, path, days, seq):
    with zipfile.ZipFile(path) as archive:
        t = Tables(archive)
        if not {'routes.txt','stops.txt','trips.txt','stop_times.txt'} <= t.names.keys():
            raise ValueError('not a static GTFS feed')
        routes = {i: (short or long or i, ' · '.join(dict.fromkeys(s for s in (source['operator'], long if short and long != short else None) if s))) for i, short, long, kind in t.rows('routes','route_id route_short_name route_long_name route_type')
                  if kind.isdigit() and int(kind) in BUS_TYPES}
        if not routes:
            return None
        active = service_dates(t, days)
        if not active:
            return None
        trips = {trip: (route, sid, head) for trip, route, sid, head in
                 t.rows('trips', 'trip_id route_id service_id trip_headsign') if route in routes and sid in active}
        if not trips:
            return None
        stops = {}
        area = set()
        bounds = source.get('bounds', [-90,-180,90,180])
        for key, name, lat, lon, typ in t.rows('stops','stop_id stop_name stop_lat stop_lon location_type'):
            if typ not in ('','0'):
                continue
            try:
                y, x = float(lat), float(lon)
            except ValueError:
                continue
            if not (-90 <= y <= 90 and -180 <= x <= 180) or (not x and not y):
                continue
            stops[key] = (name or key, y, x)
            if bounds[0] <= y <= bounds[2] and bounds[1] <= x <= bounds[3]:
                area.add(key)
        if not area:
            return None
        covered = set()
        # Keep whole routes which enter the city. Never clip a cross-boundary route at an arbitrary rectangle.
        if len(area) == len(stops):
            covered = {r for r, _, _ in trips.values()}
        else:
            for trip, stop in t.rows('stop_times','trip_id stop_id'):
                if stop in area and trip in trips:
                    covered.add(trips[trip][0])
        if not covered:
            return None
        def alloc(typ):
            seq[typ] += 1
            return seq[typ]
        rids = {r: alloc('route') for r in sorted(covered)}
        sids = {s: alloc('service') for s in sorted({sid for r,sid,_ in trips.values() if r in covered})}
        trips = {i: (rids[r],sids[s],h) for i,(r,s,h) in trips.items() if r in covered}
        freq = collections.defaultdict(list)
        for trip, start, end, step, exact in t.rows('frequencies','trip_id start_time end_time headway_secs exact_times'):
            a, b = clock(start), clock(end)
            if trip in trips and a is not None and b is not None and step.isdigit() and int(step) > 0 and b > a:
                freq[trip].append((a,b,int(step),int(exact != '1')))
        first_freq = {}
        terminal = {}
        # Headsigns are optional. The last named stop provides a meaningful destination when absent.
        if freq or any(not h for _,_,h in trips.values()):
            for trip, rawseq, stop, departure, arrival in t.rows('stop_times','trip_id stop_sequence stop_id departure_time arrival_time'):
                if trip not in trips or not rawseq.isdigit() or stop not in stops:
                    continue
                n = int(rawseq)
                if trip in freq:
                    v = clock(departure or arrival)
                    if v is not None and (trip not in first_freq or first_freq[trip][0] > n):
                        first_freq[trip] = (n,v)
                if not trips[trip][2] and n > terminal.get(trip,(-1,''))[0]:
                    terminal[trip] = (n, stops[stop][0])
        headids, physical, headings, last, served = {}, {}, {}, {}, set()
        used_routes, used_services = set(), set()
        links = set()
        writes = []
        def flush():
            db.executemany('INSERT OR IGNORE INTO times VALUES(?,?,?,?,?,?)',writes)
            writes.clear()
        for trip, stop, rawseq, departure, arrival, pickup, stop_head, point in t.rows(
                'stop_times','trip_id stop_id stop_sequence departure_time arrival_time pickup_type stop_headsign timepoint'):
            if trip not in trips or stop not in stops or not rawseq.isdigit():
                continue
            n = int(rawseq)
            if stop not in physical:
                physical[stop] = alloc('stop')
            pid = physical[stop]
            route, sid, head = trips[trip]
            before = last.get(trip)
            if before and before[0] < n and before[1] != stop:
                _, prev = before
                y0, x0 = stops[prev][1:]
                y1, x1 = stops[stop][1:]
                east = (x1-x0)*math.cos(math.radians((y0+y1)*0.5)); north = y1-y0
                mag = math.hypot(east,north)
                if mag:
                    for target in (prev, stop):
                        total = headings.setdefault(target,[0.,0.,0])
                        total[0] += east/mag; total[1] += north/mag; total[2] += 1
            if before is None or n > before[0]:
                last[trip] = (n,stop)
            links.add((pid,route))
            v = clock(departure or arrival)
            if v is None or pickup not in ('','0'):
                continue
            dest = stop_head or head or terminal.get(trip,(0,''))[1]
            if dest not in headids:
                headids[dest] = alloc('head')
            hid = headids[dest]
            if trip in freq:
                if trip not in first_freq:
                    continue
                offset = v - first_freq[trip][1]
                for a,b,step,approx in freq[trip]:
                    writes.extend((pid, start+offset, route,sid,hid, int(approx or point == '0')) for start in range(a,b,step)
                                  if 0 <= start+offset < 72 * 3600)
            else:
                writes.append((pid,v,route,sid,hid,int(point == '0')))
            served.add(pid)
            used_routes.add(route)
            used_services.add(sid)
            if len(writes) >= 12000:
                flush()
        flush()
        if not used_services:
            return None
        db.executemany('INSERT INTO routes VALUES(?,?,?)',[(idx,*routes[r]) for r,idx in rids.items() if idx in used_routes])
        db.executemany('INSERT INTO heads VALUES(?,?)',[(idx,h) for h,idx in headids.items()])
        included_stops = {pid for pid,r in links if r in used_routes}
        coords = []
        for stop, pid in physical.items():
            if pid not in included_stops:
                continue
            east,north,count = headings.get(stop,(0,0,0))
            # Opposing directions can share one station-level feed coordinate. Leave it unmarked if ambiguous.
            bearing = (math.degrees(math.atan2(east,north)) % 360) if count and math.hypot(east,north)/count >= 0.55 else None
            coords.append((pid,*stops[stop],bearing))
        db.executemany('INSERT INTO stops VALUES(?,?,?,?,?)',coords)
        db.executemany('INSERT INTO stop_routes VALUES(?,?)',[(pid,r) for pid,r in sorted(links) if r in used_routes])
        end = 0
        for sid,idx in sids.items():
            if idx in used_services:
                db.executemany('INSERT INTO service_days VALUES(?,?)',((idx,date) for date in active[sid]))
                end = max(end, active[sid][-1])
        return end

def build(city, feeds, cache, out, today, tolerate_unavailable=False):
    if not re.fullmatch('[a-zA-Z0-9]+',city):
        raise ValueError('invalid city')
    zone = next((f['timeZone'] for f in feeds if f.get('timeZone')),None)
    if not zone:
        if all(f['id'].startswith('jp-') for f in feeds):
            zone = 'Asia/Tokyo'
        else:
            raise ValueError('timeZone missing for '+city)
    dates = [today + dt.timedelta(days=delta) for delta in range(-3,92)]
    days = [(datecode(d),d.weekday()) for d in dates]
    seq = collections.Counter()
    sources = []
    with tempfile.TemporaryDirectory(dir=out, prefix=city+'-build-') as temp:
        raw = Path(temp)/'raw.sqlite'
        db = sqlite3.connect(raw)
        db.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE; PRAGMA cache_size=-96000;'+SCHEMA)
        valid = []
        unavailable = []
        for source in feeds:
            # An unavailable feed cannot contribute a timetable or extend its own validity.
            # Still refresh independent operators, and record the loss for the coverage report.
            try:
                path = feed_file(cache,source)
            except (OSError, ValueError):
                if not tolerate_unavailable:
                    raise
                unavailable.append(source['operator'])
                print(city, 'could not retrieve', source['id'], file=sys.stderr,flush=True)
                continue
            end = add_feed(db,source,path,days,seq)
            if end:
                valid.append(end)
                sources.append({k:source[k] for k in ('id','operator','source','license','notice','terms') if source.get(k)})
        if not valid or max(valid) < datecode(today):
            db.close()
            raise ValueError('no current, dated bus trips in '+city)
        through = str(max(valid))
        # Individual services retain their own calendars; refresh when the first agency's coverage is near its end.
        first_end = str(min(valid))
        credits = "GTFS adapted for offline timetables and stop directions.\n" + "\n".join(
            " · ".join(dict.fromkeys(str(r[k]) for k in ("operator","notice","source","license","terms") if r.get(k))) for r in sources)
        meta = {'city':city,'timeZone':zone,'validThrough':through,'refreshThrough':first_end,
                'sources':credits,'sourceDetails':json.dumps(sources,ensure_ascii=False),'adaptation':'GTFS converted to offline stops, direction bearings and timetable estimates.'}
        db.executemany('INSERT INTO metadata VALUES(?,?)',meta.items())
        db.executescript('CREATE INDEX stops_latlon ON stops(lat,lon); CREATE INDEX routes_stops ON stop_routes(route_id,stop_id);')
        counts = {k:db.execute('SELECT COUNT(*) FROM '+k).fetchone()[0] for k in ('routes','stops')}
        db.commit()
        if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise ValueError('invalid output database')
        db.close()
        packed = Path(temp)/'compressed'
        with raw.open('rb') as r,packed.open('wb') as f,gzip.GzipFile(filename='',fileobj=f,mode='wb',compresslevel=4,mtime=0) as z:
            shutil.copyfileobj(r,z,1024*1024)
        with packed.open('rb') as f:
            h=hashlib.sha256()
            for part in iter(lambda:f.read(1024*1024),b''): h.update(part)
        sha=h.hexdigest()
        name = city+'-'+sha[:16]+'.sqlite.gz'
        entry = {**counts,'path':'packages/bus/data/'+name,
                 'url':'https://github.com/cat-metro-station/cat-metro-timetables/releases/download/bus-packages-v1/'+name,
                 'sha256':sha,'bytes':packed.stat().st_size,'uncompressedBytes':raw.stat().st_size,
                 'validThrough':through,'refreshThrough':first_end,'operatorFeeds':len(sources)}
        if unavailable:
            entry['unavailableOperators'] = sorted(set(unavailable))
        os.replace(packed,out/name)
        (Path(temp)/'entry').write_text(json.dumps(entry,indent=2)+'\n')
        os.replace(Path(temp)/'entry',out/(city+'.entry.json'))
        print(city,json.dumps(entry),flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,required=True)
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--city',action='append')
    parser.add_argument('--allow-missing-feeds',action='store_true',help='Refresh accessible operators and report any temporarily unavailable feeds')
    parser.add_argument('--date',type=dt.date.fromisoformat,default=dt.date.today())
    a=parser.parse_args()
    entries=json.loads(a.catalog.read_text())
    a.cache.mkdir(parents=True,exist_ok=True)
    a.output.mkdir(parents=True,exist_ok=True)
    failed=[]
    for city in a.city or entries:
        try: build(city,entries[city],a.cache,a.output,a.date,a.allow_missing_feeds)
        except (ValueError,KeyError,zipfile.BadZipFile,sqlite3.Error,OSError) as e:
            failed.append(city)
            print(city,'failed:',e,file=sys.stderr,flush=True)
    if failed:
        raise SystemExit('Failed cities: '+', '.join(failed))
if __name__ == '__main__':
    main()
