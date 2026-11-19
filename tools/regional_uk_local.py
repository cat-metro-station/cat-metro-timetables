"""Convert Northern Ireland Railways and Nottingham Express Transit GTFS releases."""
from collections import defaultdict
import csv
from datetime import date
import io
import math
import re

from build_city_timetables import seconds, service
from regional_uk_net_calendar import reconcile

CC_BY = 'https://creativecommons.org/licenses/by/4.0/'
OGL = 'https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/'
ATTRIBUTION = {
    'belfast': dict(
        source='National Transport Authority · Northern Ireland Railways',
        url='https://www.transportforireland.ie/transitData/Data/GTFS_NI.zip',
        notice='Contains GTFS data from the National Transport Authority, licensed under CC BY 4.0. '
               'NI Railways services are matched to the mapped stations and converted to offline timetables.',
        terms=CC_BY),
    'nottingham': dict(
        source='Traveline National Dataset · TravelWhiz · Nottingham Express Transit',
        url='https://storage.travelwhiz.app/generated-gtfs/uk-busmetro-EM.gtfs.zip',
        notice='Contains public sector information licensed under the Open Government Licence v3.0 (' + OGL + '). '
               'GTFS conversion by TravelWhiz Ltd, CC BY 4.0. Tram services are matched to the mapped stations '
               'and converted to offline timetables. Bank holidays follow the NETTRAM operating profiles. Source notices: '
               'https://github.com/travelwhiz-ltd/GB-Bus-Train-Metro-GTFS/blob/main/DATA_SOURCES.md',
        terms=CC_BY),
}
BELFAST_ROUTES = {'Bangor Line': 'Bangor', 'Portadown Line': 'Portadown',
                  'Larne Line': 'Larne', 'Derry/Londonderry Line': 'Antrim', '1': 'Larne'}
WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']


def rows(archive, table):
    with archive.open(table + '.txt') as fp:
        yield from csv.DictReader(io.TextIOWrapper(fp, encoding='utf-8-sig', newline=''))


def date_key(text):
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def load_calendars(archive, used):
    calendars = {}
    for c in rows(archive, 'calendar'):
        if c['service_id'] in used:
            mask = sum(int(c[name]) << i for i, name in enumerate(WEEKDAYS))
            calendars[c['service_id']] = service(mask, int(c['start_date']), int(c['end_date']))
    for ex in rows(archive, 'calendar_dates'):
        if ex['service_id'] in used:
            c = calendars.setdefault(ex['service_id'], service(0, 0, 0))
            key = {'1': 'added', '2': 'removed'}[ex['exception_type']]
            c[key].append(int(ex['date']))
    if calendars.keys() != used:
        raise ValueError('Selected trips have missing service dates: ' + str(used - calendars.keys()))
    for c in calendars.values():
        c['added'].sort()
        c['removed'].sort()
    return dict(sorted(calendars.items()))


def belfast_station_map(network, stops, used):
    # Join platforms to the existing Wikidata station IDs. Require BOTH the station
    # name and proximity, including the two operator-specific names below.
    aliases = {'Grand Central': 'Belfast Grand Central', 'Larne': 'Larne Town'}
    by_name = {name.casefold(): (sid, y, x) for sid, (name, y, x) in network['stations'].items()}
    found = {}
    for sid in used:
        row = stops[sid]
        name = re.sub(r' (?:Train |Rail )?Station(?: \(Platform \d+\))?$', '', row['stop_name'])
        mapped = by_name.get(aliases.get(name, name).casefold())
        if mapped:
            lat, lon = float(row['stop_lat']), float(row['stop_lon'])
            km = 111 * math.hypot(lat - mapped[1], (lon - mapped[2]) * math.cos(math.radians(lat)))
            if km > 0.5:
                raise ValueError(f"Belfast station name matched at unexpected distance: {sid}, {km:.1f} km")
            found[sid] = mapped[0]
    return found


def select_routes(archive, city):
    selected = {}
    for row in rows(archive, 'routes'):
        if city == 'belfast' and row['agency_id'] == 'RailNI' and row['route_type'] == '2':
            selected[row['route_id']] = BELFAST_ROUTES[row['route_short_name']]
        # The TNDS conversion labels the named NET tram service as GTFS type 3.
        elif city == 'nottingham' and row['agency_id'] == 'nottinghamexpresstransit' and row['route_short_name'] == 'TRAM':
            selected[row['route_id']] = 'NET'
    if not selected:
        raise ValueError('Selected rail operator not present in the feed')
    return selected


def build(city, archive, network, profiles=None, holidays=None):
    mapped = {s for stops in network['lines'].values() for s in stops}
    tram_lines = {symbol: set(stops) for symbol, stops in network['lines'].items() if symbol in ('T1', 'T2')}
    routes = select_routes(archive, city)
    trips = {t['trip_id']: t for t in rows(archive, 'trips') if t['route_id'] in routes}
    if city == 'nottingham':
        regular = load_calendars(archive, {t['service_id'] for t in trips.values()})
        service_ids, net_calendar = reconcile(trips, regular, profiles, holidays)
        trips = {tid: t for tid, t in trips.items() if tid in service_ids}
    else:
        service_ids = {tid: t['service_id'] for tid, t in trips.items()}
    if 'frequencies.txt' in archive.namelist() and any(f['trip_id'] in trips for f in rows(archive, 'frequencies')):
        raise ValueError('Frequency service requires separate processing')
    calls = defaultdict(list)
    for c in rows(archive, 'stop_times'):
        if c['trip_id'] in trips:
            calls[c['trip_id']].append(c)
    used = {c['stop_id'] for trip in calls.values() for c in trip}
    feed_stops = {s['stop_id']: s for s in rows(archive, 'stops') if s['stop_id'] in used}
    if city == 'belfast':
        stop_map = belfast_station_map(network, feed_stops, used)
        snapshot = date_key(next(rows(archive, 'feed_info'))['feed_start_date'])
    else:
        stop_map = {p: p[:-1] for p in used if p[:-1] in mapped and p[-1].isdigit()}
        if used != stop_map.keys():
            raise ValueError('Unmapped NET platform: ' + str(used - stop_map.keys()))
        snapshot = date(*archive.getinfo('trips.txt').date_time[:3])

    groups = defaultdict(lambda: defaultdict(set))
    services = set()
    for trip_id, trip in trips.items():
        ordered = sorted(calls[trip_id], key=lambda c: int(c['stop_sequence']))
        selected = [c for c in ordered if c['stop_id'] in stop_map]
        if not selected:
            continue
        continues = selected[-1] is not ordered[-1]
        if len(selected) == 1 and not continues:
            continue
        stops = tuple(stop_map[c['stop_id']] for c in selected)
        line = routes[trip['route_id']]
        if city == 'nottingham':
            candidates = [symbol for symbol, ids in tram_lines.items() if set(stops) <= ids]
            if not candidates:
                raise ValueError('Unexpected NET service pattern: ' + str(stops))
            line = candidates[0] if len(candidates) == 1 else 'NET'
        departures = [seconds(c['departure_time']) for c in selected]
        arrivals = [seconds(c['arrival_time']) for c in selected]
        if any(a > d for a, d in zip(arrivals, departures)) or any(d > a for d, a in zip(departures, arrivals[1:])):
            raise ValueError('Invalid stop times: ' + trip_id)
        first = departures[0]
        offsets = tuple(d - first for d in departures)
        arrival_offsets = (0, *(a - first for a in arrivals[1:]))
        restricted = tuple(tuple(i for i, c in enumerate(selected) if c.get(k) not in ('', '0', None))
                           for k in ['pickup_type', 'drop_off_type'])
        approx = tuple(i for i, c in enumerate(selected) if c.get('timepoint') == '0')
        destination = trip['trip_headsign'] or feed_stops[ordered[-1]['stop_id']]['stop_name']
        pattern = (line, stops, *restricted, approx, destination if continues else '', continues)
        groups[pattern][(service_ids[trip_id], offsets, arrival_offsets)].add(first)
        services.add(service_ids[trip_id])

    patterns = []
    covered = set()
    for (line, stops, nop, nod, approx, destination, continues), timing_set in sorted(groups.items()):
        timings = []
        for (cal, offsets, arrivals), starts in sorted(timing_set.items()):
            timing = dict(service=cal, offsets=offsets, starts=sorted(starts))
            if arrivals != offsets:
                timing['arrivalOffsets'] = arrivals
            timings.append(timing)
        p = dict(line=line, stops=stops, noPickup=nop, noDropOff=nod, approximate=approx, timings=timings)
        if continues:
            p.update(destinationName=destination, continuesBeyondMap=True)
        patterns.append(p)
        covered.update(stop for idx, stop in enumerate(stops) if idx not in nop and (idx < len(stops)-1 or continues))
    if mapped != covered:
        raise ValueError('Missing boardable stations: ' + str(mapped - covered))
    calendars = net_calendar if city == 'nottingham' else load_calendars(archive, services)
    end = max(max(c['end'] if c['days'] else 0, max(c['added'], default=0)) for c in calendars.values())
    if date_key(str(end)) < snapshot:
        raise ValueError('Rail service expired before the snapshot')
    package = dict(version=1, city=city, sourceDate=snapshot.isoformat(), validThrough=end,
                   calendars=calendars, attribution=ATTRIBUTION[city], patterns=patterns)
    return package
