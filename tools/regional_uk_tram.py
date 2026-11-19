"""Extract the configured light-rail station boards from DfT regional GTFS."""
import csv
from collections import defaultdict
from datetime import date
import io
from math import cos, radians, hypot
import re

from build_city_timetables import seconds, service

# Operator matching is scoped to rail route types, excluding rail-replacement buses.
OPERATORS = {
    'Metrolink': ('manchester', 'north_west'), 'Blackpool Transport': ('blackpool', 'north_west'),
    'West Midlands Metro': ('birmingham', 'west_midlands'), 'Tyne and Wear Metro': ('newcastle', 'north_east'),
    'South Yorkshire Future Tram': ('sheffield', 'yorkshire'), 'Edinburgh Trams': ('edinburgh', 'scotland'),
    'SPT Subway': ('glasgow', 'scotland'),
}
OGL = 'https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/'
SYMBOLS = {'GRN': 'Green', 'YEL': 'Yellow', 'BLUE': 'Blue', 'YELL': 'Yellow', 'PURP': 'Purple', 'T50': 'Tram'}


def station_name(name):
    return re.sub(r' \((?:Manchester Metrolink|West Midlands Metro|Tyne and Wear Metro Station|'
                  r'South Yorkshire Supertram|S Yorkshire Supertram|Edinburgh Trams|Blackpool Tramway)\)$|'
                  r' SPT Subway Station$', '', name, flags=re.I).strip()


def build(archive, city):
    def rows(table):
        with archive.open(table + '.txt') as src:
            yield from csv.DictReader(io.TextIOWrapper(src, encoding='utf-8-sig', newline=''))

    agencies = {a['agency_id']: a['agency_name'] for a in rows('agency')}
    routes = {}
    for row in rows('routes'):
        agency = agencies[row['agency_id']]
        if agency in OPERATORS and OPERATORS[agency][0] == city and row['route_type'] in ('0', '1'):
            routes[row['route_id']] = row['route_short_name']
    if not routes:
        raise ValueError('Feed contains no configured rail operator')
    trips = {t['trip_id']: (routes[t['route_id']], t['service_id'])
             for t in rows('trips') if t['route_id'] in routes}
    if 'frequencies.txt' in archive.namelist() and any(t['trip_id'] in trips for t in rows('frequencies')):
        raise ValueError('Frequency-based trips require a dedicated importer')
    info = next(rows('feed_info'))
    source_date = date.fromisoformat('-'.join((info['feed_version'][:4], info['feed_version'][4:6], info['feed_version'][6:8])))
    calendars = {}
    weekdays = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    for c in rows('calendar'):
        mask = sum(int(c[name]) << i for i, name in enumerate(weekdays))
        calendars[c['service_id']] = service(mask, int(c['start_date']), int(c['end_date']))
    for ex in rows('calendar_dates'):
        cal = calendars.setdefault(ex['service_id'], service(0, 0, 0))
        cal['added' if ex['exception_type'] == '1' else 'removed'].append(int(ex['date']))
    stations = {s['stop_id']: s for s in rows('stops')}
    calls = defaultdict(list)
    for s in rows('stop_times'):
        if s['trip_id'] in trips:
            sid = stations[s['stop_id']]['parent_station'] or s['stop_id']
            calls[s['trip_id']].append((int(s['stop_sequence']), sid, seconds(s['arrival_time']), seconds(s['departure_time']),
                s['pickup_type'] not in ('', '0'), s['drop_off_type'] not in ('', '0'), s['timepoint'] == '0'))

    # Some operators publish each platform without parent_station. Join opposite
    # platforms only for the same local station name and a short walking distance.
    used_ids = {call[1] for trip in trips for call in calls[trip]}
    by_name = defaultdict(list)
    canonical = {}
    for sid in sorted(used_ids, key=lambda i: (stations[i]['location_type'] != '1', i)):
        row = stations[sid]
        lat, lon = float(row['stop_lat']), float(row['stop_lon'])
        group = by_name[station_name(row['stop_name'])]
        nearby = next((other for other, y, x in group if hypot(lat-y, (lon-x)*cos(radians(lat))) < 0.003), None)
        canonical[sid] = nearby or sid
        if nearby is None:
            group.append((sid, lat, lon))
    schedules = defaultdict(lambda: defaultdict(set))
    used_services = set()
    for trip, (name, calendar) in trips.items():
        ordered = sorted(calls[trip])
        if len(ordered) < 2:
            continue
        _, stops, arrivals, departures, no_pickup, no_drop, approximate = zip(*ordered)
        stops = tuple(canonical[sid] for sid in stops)
        if any(a > d for a, d in zip(arrivals, departures)) or any(d > a for d, a in zip(departures, arrivals[1:])):
            raise ValueError('Unordered stop times: ' + trip)
        restricted = tuple(tuple(i for i, flag in enumerate(flags) if flag) for flags in (no_pickup, no_drop, approximate))
        pattern = (SYMBOLS.get(name, name.removesuffix(' Line')), stops, *restricted)
        first = departures[0]
        departure_offsets = tuple(d - first for d in departures)
        arrival_offsets = (0, *(a - first for a in arrivals[1:]))
        schedules[pattern][(calendar, departure_offsets, arrival_offsets)].add(first)
        used_services.add(calendar)
    region = next(region for area, region in OPERATORS.values() if area == city)
    source_url = 'https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/' + region + '/'
    source = 'Department for Transport · Bus Open Data Service / Traveline / NaPTAN'
    package_patterns = []
    for (line, stops, nop, nod, approx), group in sorted(schedules.items()):
        timings = []
        for (cal, offsets, arrivals), starts in sorted(group.items()):
            timing = dict(service=cal, offsets=offsets, starts=sorted(starts))
            if offsets != arrivals:
                timing['arrivalOffsets'] = arrivals
            timings.append(timing)
        package_patterns.append(dict(line=line, stops=stops, noPickup=nop, noDropOff=nod, approximate=approx,
                                     timings=timings))
    local_calendar = {c: calendars[c] for c in sorted(used_services)}
    # The region-wide feed end may belong to an unrelated bus operator.
    end = max(max(c['end'] if c['days'] else 0, max(c['added'], default=0)) for c in local_calendar.values())
    package = dict(version=1, city=city, sourceDate=str(source_date), validThrough=end, calendars=local_calendar,
        attribution=dict(source=source, url=source_url, terms=OGL, notice='Contains public sector information licensed under the Open Government Licence v3.0. Regional GTFS rail schedules adapted for mapped station boards.'),
        patterns=package_patterns)
    return package
