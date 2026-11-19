#!/usr/bin/env python3
"""Build US regional rail timetable updates from current operator feeds."""
import argparse
import concurrent.futures
import collections
import csv
import datetime
import html
import io
import json
import math
from pathlib import Path
import re
import tempfile
import zipfile
import shutil
import subprocess
import sys

from build_city_timetables import seconds

ROOT = Path(__file__).resolve().parents[1]
DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
NTM = 'https://www.bts.gov/national-transit-map'
RAIL_TYPES = {0, 1, 2, 5, 7, 12}


def write_json(path, value):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=path.parent, delete=False) as out:
        json.dump(value, out, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        out.write('\n')
    Path(out.name).replace(path)


def rows(archive, table):
    try:
        file = archive.open(table + '.txt')
    except KeyError:
        return
    with file, io.TextIOWrapper(file, encoding='utf-8-sig', newline='') as stream:
        yield from csv.DictReader(stream)


def clean(value):
    return ' '.join(html.unescape(value or '').split())


def calendars(archive, prefix):
    result = {}
    for row in rows(archive, 'calendar'):
        mask = sum(int(row[day]) << i for i, day in enumerate(DAYS))
        result[prefix + row['service_id']] = dict(days=mask, start=int(row['start_date']),
                                                   end=int(row['end_date']), added=[], removed=[])
    for row in rows(archive, 'calendar_dates'):
        key = prefix + row['service_id']
        cal = result.setdefault(key, dict(days=0, start=0, end=0, added=[], removed=[]))
        cal['added' if row['exception_type'] == '1' else 'removed'].append(int(row['date']))
    for cal in result.values():
        if not cal['days'] and cal['added']:
            cal['start'], cal['end'] = min(cal['added']), max(cal['added'])
        cal['added'].sort()
        cal['removed'].sort()
    return result


def is_rail(route, config):
    eligible_ids = config.get('includeRoutes')
    if eligible_ids and route['route_id'] not in eligible_ids:
        return False
    kind = int(route['route_type'])
    name = (clean(route.get('route_short_name')) + ' ' + clean(route.get('route_long_name'))).upper()
    if any(s.upper() in name for s in config.get('excludeNames', [])):
        return False
    if kind in config.get('excludeTypes', []):
        return False
    return (kind in RAIL_TYPES or 100 <= kind <= 117 or 400 <= kind <= 405 or 900 <= kind <= 906
            or kind == 3 and any(s.upper() in name for s in config.get('busCodedRailNames', [])))


def strip_direction(name):
    return re.sub(r'\s+\(?(?:northbound|southbound|eastbound|westbound|NB|SB|EB|WB)\)?(?:\s+platform)?$',
                  '', name, flags=re.I)


def station_key(name):
    # Opposite platforms often have separate IDs and a direction suffix.
    value = strip_direction(name)
    return re.sub(r'[^\w]', '', value.casefold())


def meters(a, b):
    x = math.radians(a['lon'] - b['lon']) * math.cos(math.radians((a['lat'] + b['lat']) / 2))
    return 6371000 * math.hypot(x, math.radians(a['lat'] - b['lat']))


class CityResources:
    """Use the station IDs and line symbols present in released applications."""

    def __init__(self, city, reference, catalog):
        self.city = city
        self.reference = reference
        self.stations = {sid: dict(id=sid, en=name, lat=lat, lon=lon)
                         for sid, name, lat, lon in catalog['stops']}
        self.stations_by_name = collections.defaultdict(list)
        for stop in self.stations.values():
            self.stations_by_name[station_key(stop['en'])].append(stop)
        self.lines = {lid: (symbol, name) for lid, symbol, name in catalog['lines']}
        self.timing = collections.defaultdict(lambda: collections.defaultdict(set))
        self.calendars = {}
        self.providers = []
        self.city_horizon = None

    def station(self, row, prefix):
        exact = prefix + row['stop_id']
        if exact in self.stations:
            return exact
        position = dict(lat=float(row['stop_lat']), lon=float(row['stop_lon']))
        matches = [s['id'] for s in self.stations_by_name[station_key(row['stop_name'])]
                   if meters(s, position) <= 180]
        return matches[0] if len(matches) == 1 else None

    def line(self, row, prefix):
        lid = prefix + row['route_id']
        if lid in self.lines:
            return self.lines[lid][0]
        name, short = clean(row.get('route_long_name')), clean(row.get('route_short_name'))
        if prefix == '53-':  # BART gives each direction its own GTFS route ID.
            short = re.sub(r'-[NSEW]$', '', short)
        matched = {symbol for key, (symbol, en) in self.lines.items()
                   if key.startswith(prefix) and (symbol in (short, name) or en == name)}
        return matched.pop() if len(matched) == 1 else None

    def read(self, archive, feed, through=None):
        prefix = feed['id'] + '-'
        routes = {r['route_id']: self.line(r, prefix) for r in rows(archive, 'routes') if is_rail(r, feed)}
        routes = {route: symbol for route, symbol in routes.items() if symbol is not None}
        if not routes:
            raise ValueError('No rail routes for ' + prefix)
        service_prefix = prefix if through is None else prefix + str(through) + ':'
        services = calendars(archive, service_prefix)
        if through is not None:
            for cal in services.values():
                cal['end'] = min(cal['end'], through)
                cal['added'] = [d for d in cal['added'] if d <= through]
                cal['removed'] = [d for d in cal['removed'] if d <= through]
            services = {k:c for k,c in services.items() if c['start'] <= c['end']}
        trips = {r['trip_id']: (routes[r['route_id']], service_prefix + r['service_id'])
                 for r in rows(archive, 'trips') if r['route_id'] in routes}
        # Exclude retired route variants if this feed contains a newer service calendar.
        def final_date(cal):
            return max([cal['end']] + cal['added'])
        used = {s for _, s in trips.values() if s in services}
        last_service = max(final_date(services[s]) for s in used)
        if last_service < self.reference:
            raise ValueError('Expired operator calendars for ' + prefix)
        cutoff = self.reference
        trips = {t: (line, s) for t, (line, s) in trips.items()
                 if s in services and final_date(services[s]) >= cutoff}
        self.calendars.update({s: services[s] for _, s in trips.values()})

        stops = {r['stop_id']: r for r in rows(archive, 'stops')}
        station_ids = {}
        calls = collections.defaultdict(list)
        for row in rows(archive, 'stop_times'):
            trip = row['trip_id']
            if trip not in trips:
                continue
            raw_id = row['stop_id']
            if raw_id not in station_ids:
                raw = stops[raw_id]
                parent = stops.get(raw.get('parent_station')) or raw
                station_ids[raw_id] = self.station(parent, prefix)
            clock = clean(row.get('departure_time')) or clean(row.get('arrival_time'))
            calls[trip].append((int(row['stop_sequence']), station_ids[raw_id],
                                seconds(clock) if clock else None,
                                clean(row.get('pickup_type')) not in ('', '0'),
                                clean(row.get('drop_off_type')) not in ('', '0'),
                                clean(row.get('timepoint')) == '0',
                                seconds(row['arrival_time']) if clean(row.get('arrival_time')) else None))
        freq = collections.defaultdict(list)
        for r in rows(archive, 'frequencies'):
            if r['trip_id'] in trips:
                freq[r['trip_id']].append((seconds(r['start_time']), seconds(r['end_time']),
                                           int(r['headway_secs']), r.get('exact_times') != '1'))
        for trip, unordered in calls.items():
            line, service = trips[trip]
            ordered = sorted(unordered)
            stations = tuple(call[1] for call in ordered)
            if None in stations or len(set(stations)) < 2:
                continue
            # Do not invent departure times for stops with no published time.
            timed = [call for call in ordered if call[2] is not None]
            if len(timed) < 2:
                continue
            first = timed[0][2]
            offsets = tuple(call[2] - first for call in timed)
            if sorted(offsets) != list(offsets) or offsets[-1] >= 86400:
                continue
            arrivals = (0,) + tuple(offsets[i] if call[6] is None else call[6] - first
                                    for i, call in enumerate(timed[1:], start=1))
            if any(not offsets[i - 1] <= arrivals[i] <= offsets[i] for i in range(1, len(timed))):
                continue
            arrival_offsets = () if arrivals == offsets else arrivals
            stations = tuple(call[1] for call in timed)
            no_pickup = tuple(i for i,c in enumerate(timed) if c[3])
            no_dropoff = tuple(i for i,c in enumerate(timed) if c[4])
            estimated = tuple(i for i,c in enumerate(timed) if c[5])
            if trip in freq:
                # A frequency is measured at the first stop of the trip.
                if timed[0] != ordered[0]:
                    continue
                for start, end, headway, approximate in freq[trip]:
                    if headway <= 0 or end <= start:
                        continue
                    key = (line, stations, no_pickup, no_dropoff,
                           tuple(range(len(timed))) if approximate else estimated, approximate)
                    self.timing[key][service, offsets, arrival_offsets].update(range(start, end, headway))
            else:
                self.timing[line, stations, no_pickup, no_dropoff, estimated, False][service, offsets, arrival_offsets].add(first)
        if through is not None:
            return
        timed_services = {service for times in self.timing.values() for service, _, _ in times
                          if service in services}
        if not timed_services:
            raise ValueError('No usable rail timings for ' + prefix)
        last_service = max(final_date(services[s]) for s in timed_services)
        if feed.get('mode') == 'timetableOnly':
            meta = next(rows(archive, 'feed_info'))
            if not int(meta['feed_start_date']) <= self.reference <= int(meta['feed_end_date']):
                raise ValueError('WMATA feed_info does not cover this date')
            self.city_horizon = min(last_service, int(meta['feed_end_date']))
            for service in timed_services:
                cal = self.calendars[service]
                cal['end'] = min(cal['end'], self.city_horizon)
                cal['added'] = [date for date in cal['added'] if date <= self.city_horizon]
        fields = ('provider', 'url', 'downloadURL', 'ntdID', 'ntdLicense', 'ntdWeblink', 'terms', 'notice')
        source = {key: feed[key] for key in fields if key in feed}
        if 'supplement' in feed and self.reference <= feed['supplement']['through']:
            source['supplement'] = feed['supplement']
        source['serviceThrough'] = last_service
        archive_date = datetime.date(*archive.getinfo('stop_times.txt').date_time[:3])
        reference_date = datetime.datetime.strptime(str(self.reference), '%Y%m%d').date()
        # ZIP timestamps have no time zone. A freshly generated ZIP can already
        # be on tomorrow's UTC date while the local operator is on today's date.
        if 2000 <= archive_date.year and archive_date <= reference_date + datetime.timedelta(days=1):
            source['archiveDate'] = min(archive_date, reference_date).isoformat()
        else:
            start = max((c['start'] for s,c in self.calendars.items()
                         if s in used and 20000101 <= c['start'] <= min(last_service, self.reference)), default=0)
            if start:
                source['calendarStartDate'] = str(datetime.datetime.strptime(str(start), '%Y%m%d').date())
        self.providers.append(source)

    def package(self):
        patterns = []
        for (line, stops, no_pickup, no_dropoff, estimated, headways), times in self.timing.items():
            t = []
            for (service, offsets, arrivals), starts in sorted(times.items()):
                timing = dict(service=service, offsets=offsets, starts=sorted(starts))
                if arrivals:
                    timing['arrivalOffsets'] = arrivals
                t.append(timing)
            pattern = dict(line=line, stops=stops, noPickup=no_pickup, noDropOff=no_dropoff,
                           approximate=estimated, timings=t)
            if headways:
                pattern['boundary'] = 'headway'
            patterns.append(pattern)
        used = {t['service'] for p in patterns for t in p['timings']}
        dates = [f.get('archiveDate') or f.get('calendarStartDate') for f in self.providers]
        return dict(version=1, city=self.city, sourceDate=min(d for d in dates if d),
                    validThrough=self.city_horizon or max(f['serviceThrough'] for f in self.providers),
                    attribution=dict(source=' / '.join(dict.fromkeys(f['provider'] for f in self.providers)),
                                     url=NTM, feeds=self.providers),
                    calendars={k:c for k,c in self.calendars.items() if k in used}, patterns=patterns)


def validate_current(archive, feed, reference):
    routes = {r['route_id'] for r in rows(archive, 'routes') if is_rail(r, feed)}
    services = {t['service_id'] for t in rows(archive, 'trips') if t['route_id'] in routes}
    dates = [max([c['end'], *c['added']]) for k, c in calendars(archive, '').items()
             if k in services and (c['days'] or c['added'])]
    if not dates or max(dates) < reference:
        raise ValueError("expired or missing rail service in " + feed['provider'])
    if feed.get('mode') == 'timetableOnly':
        info = next(rows(archive, 'feed_info'), {})
        if not int(info.get('feed_start_date') or 0) <= reference <= int(info.get('feed_end_date') or 0):
            raise ValueError("published feed_info window does not cover the service date")


def download(url, path, member=None, validate=None):
    """Download to a temporary path; a failed refresh never overwrites the cache."""
    with tempfile.TemporaryDirectory(dir=path.parent) as folder:
        candidate = Path(folder)/'download.zip'
        subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location',
                        '--proto', '=https', '--proto-redir', '=https',
                        '--connect-timeout', '12', '--max-time', '100',
                        '--retry', '1', '--retry-delay', '2',
                        '--user-agent', 'CatMetroStation/1.4 (public schedule updater)',
                        '--output', str(candidate), url], check=True)
        if member:
            with zipfile.ZipFile(candidate) as outer:
                inner = Path(folder)/'feed.zip'
                with outer.open(member) as src, inner.open('wb') as dest:
                    shutil.copyfileobj(src, dest)
            candidate = inner
        with zipfile.ZipFile(candidate) as archive:
            if not {'routes.txt','stops.txt','trips.txt','stop_times.txt'} <= set(archive.namelist()):
                raise ValueError('Download does not contain the required GTFS tables')
            if validate:
                validate(archive)
        candidate.replace(path)


def fetch_feed(feed, cache, supplement, reference):
    if supplement:
        entry = feed['supplement']
        path = cache/(feed['id'] + '-' + str(entry['through']) + '.zip')
        if not path.exists():  # A dated and bounded historical interval, never a renewal source.
            download(entry['url'], path, entry.get('member'))
        return path
    path = cache/(feed.get('cacheID', feed['id']) + '.zip')
    urls = [feed.get('downloadURL', feed['url'])]
    if feed['id'].isdecimal():
        urls.append(f"https://files.mobilitydatabase.org/mdb-{feed['id']}/latest.zip")
    if feed.get('downloadURL') and feed['url'] != feed['downloadURL'] and feed.get('mode') != 'timetableOnly':
        urls.append(feed['url'])
    errors = []
    for url in dict.fromkeys(urls):
        try:
            download(url, path, validate=lambda archive: validate_current(archive, feed, reference))
            return path
        except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
            errors.append(str(error))
    raise ValueError(f"{feed['provider']}: no valid current feed; {' / '.join(errors)}")


def generate(city, catalog, date, fetch, previous=None):
    reference = int(date.strftime('%Y%m%d'))
    resource = CityResources(city, reference, catalog)
    for feed in catalog['feeds']:
        if feed.get('ntdLicense') != 'Public Domain':
            raise ValueError('Review NTD distribution terms for ' + feed['provider'])
        with zipfile.ZipFile(fetch(feed, False)) as archive:
            resource.read(archive, feed)
        if feed.get('supplement', {}).get('through', 0) >= reference:
            with zipfile.ZipFile(fetch(feed, True)) as archive:
                resource.read(archive, feed, through=feed['supplement']['through'])
    package = resource.package()
    if previous and previous['patterns'] == package['patterns'] and previous['calendars'] == package['calendars']:
        # Sources such as Milwaukee regenerate ZIP timestamps for identical
        # timetable data on every HTTP request. Keep its existing provenance.
        old_feeds = {f['url']: f for f in previous['attribution']['feeds']}
        for feed in package['attribution']['feeds']:
            old = old_feeds.get(feed['url'])
            if old and old.get('serviceThrough') == feed['serviceThrough']:
                for field in ('archiveDate', 'calendarStartDate'):
                    feed.pop(field, None)
                    if field in old:
                        feed[field] = old[field]
        dates = [f.get('archiveDate') or f.get('calendarStartDate') for f in package['attribution']['feeds']]
        package['sourceDate'] = min(d for d in dates if d)
    # An agency can switch to a future feed before current service ends. If no
    # current supplement exists, keep the working city and retry the new feed
    # when its published service window begins.
    for feed in catalog['feeds']:
        prefix = feed['id'] + '-'
        begins = [c['start'] for s,c in package['calendars'].items() if s.startswith(prefix)]
        if not begins or min(begins) > reference:
            raise ValueError('Future service only for ' + feed['provider'])
    return package


def previous_package(city):
    path = ROOT/'packages'/'regional-index.json'
    if path.is_file():
        entry = json.loads(path.read_bytes())['cities'].get(city)
        if entry:
            return json.loads((path.parent/'regional'/(entry['sha256'] + '.json')).read_bytes())
    return None


def main():
    from zoneinfo import ZoneInfo

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--city', nargs='+')
    args = parser.parse_args()
    # Use US service dates when newer UTC dates have already begun.
    date = datetime.datetime.now(ZoneInfo('America/Los_Angeles')).date()
    args.output.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    catalog = json.loads((ROOT/'inputs/us-regional-rail.json').read_bytes())
    cities = args.city or [*catalog, 'lasVegas']
    failures = []
    reference = int(date.strftime('%Y%m%d'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        downloads = {}
        for city in cities:
            if city == 'lasVegas':
                continue
            for feed in catalog[city]['feeds']:
                variants = [False]
                if feed.get('supplement', {}).get('through', 0) >= reference:
                    variants.append(True)
                for supplement in variants:
                    key = (feed['id'], supplement)
                    if key not in downloads:
                        downloads[key] = pool.submit(fetch_feed, feed, args.cache, supplement, reference)
        for city in cities:
            try:
                if city == 'lasVegas':
                    from refresh_us_monorail import build
                    package = build(date)
                else:
                    package = generate(city, catalog[city], date,
                                       lambda feed, old: downloads[(feed['id'], old)].result(),
                                       previous_package(city))
                write_json(args.output/f'offline-{city}.json', package)
                print(city, len(package['patterns']), 'patterns through', package['validThrough'], flush=True)
            except Exception as error:
                failures.append(city)
                print(f'{city}: {error}', file=sys.stderr, flush=True)
    if failures:
        raise SystemExit('Failed to refresh: ' + ', '.join(failures))


if __name__ == '__main__':
    main()
