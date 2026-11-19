"""Convert Network Rail CIF service days and calls to the configured regional corridors."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache
import hashlib
from itertools import groupby
import json
import xml.etree.ElementTree as ET

from build_city_timetables import service
from verify_merseyrail_northern import BOOKLET as NORTHERN_BOOKLET, add_verified_northern_calls
from verify_wirral_loop import BOOKLET as WIRRAL_BOOKLET, add_verified_wirral_calls

SCHEDULE_URL = 'https://www.networkrail.co.uk/who-we-are/transparency-and-ethics/transparency/open-data-feeds/'
LICENCE = 'https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/'
NOTICE = ('Contains Network Rail data © {year}. Contains public sector information licensed under the '
          'Open Government Licence v3.0. Passenger timetable from Network Rail Infrastructure Limited, '
          'via DirectConnectionFinder. Station codes and names from DfT NaPTAN. '
          'Applicable bank holidays are omitted. Some stops are served on request.')


def date_number(day):
    return int(day.strftime('%Y%m%d'))


def station_reference(xml, mapped):
    """Keep official CRS → mapped station IDs and NaPTAN names, including off-map destinations."""
    ns = '{http://www.naptan.org.uk/}'
    candidates = defaultdict(set)
    names = {}
    regions = {}
    for point in ET.parse(xml).iter(ns + 'StopPoint'):
        if point.get('Status') != 'active':
            continue
        station = point.findtext(ns + 'AtcoCode')
        crs = point.findtext('.//' + ns + 'CrsRef')
        if not crs:
            continue
        name = point.findtext(ns + 'Descriptor/' + ns + 'CommonName')
        name = name.removesuffix(' Rail Station').removesuffix(' Railway Station').strip()
        # NaPTAN has several access areas for certain stations. Prefer an actual
        # mapped station's name or the shortest label over a platform/level name.
        score = (station not in mapped, len(name), name)
        if crs not in names or score < names[crs]:
            names[crs] = score
        locality = point.findtext(ns + 'Place/' + ns + 'NptgLocalityRef', '')
        if locality[0:1] in {'S','W','E'}:
            regions[crs] = 'scotland' if locality.startswith('S') else 'england-and-wales'
        if station in mapped:
            candidates[crs].add(station)
    mapped_ids = {crs: next(iter(ids)) for crs, ids in candidates.items() if len(ids) == 1}
    return mapped_ids, {code: label[2] for code, label in names.items()}, regions


class Corridors:
    def __init__(self, networks):
        self.cities = networks
        self.stations = defaultdict(set)
        self.edges = defaultdict(lambda: defaultdict(dict))
        for city, network in networks.items():
            for symbol, patterns in network['lines'].items():
                for stops in patterns:
                    for i, a in enumerate(stops):
                        self.stations[a].add(city)
                        for j, b in enumerate(stops[i+1:], 1):
                            if a != b:
                                by_line = self.edges[city][a, b]
                                by_line[symbol] = min(by_line.get(symbol, j), j)

    @lru_cache(maxsize=16384)
    def segments(self, city, stops):
        """Match consecutive observed calls. Shared track takes the train's best matching corridor."""
        edges = [self.edges[city].get((a, b), {}) for a, b in zip(stops, stops[1:])]
        support = defaultdict(int)
        for edge in edges:
            for line in edge:
                support[line] += 1
        labels = [min(edge, key=lambda line: (-support[line], edge[line], line)) if edge else None for edge in edges]
        segments = []
        for line, group in groupby(enumerate(labels), key=lambda x: x[1]):
            indices = [i for i, _ in group]
            if line:
                segments.append((line, indices[0], indices[-1]+1))
        return segments


def compressed_calendar(active):
    weekdays = [datetime.strptime(str(n), '%Y%m%d').weekday() for n in active]
    mask = sum(1 << w for w in set(weekdays))
    start, end = (datetime.strptime(str(n), '%Y%m%d').date() for n in (active[0], active[-1]))
    days = [start + timedelta(days=i) for i in range((end-start).days+1)]
    actual = set(active)
    removed = [date_number(d) for d in days if mask & (1 << d.weekday()) and date_number(d) not in actual]
    result = service(mask, active[0], active[-1])
    result['removed'] = removed
    return result


def package(city, groups, source_date, end, attribution, input_hash):
    patterns = []
    unique_calendars = {days for group in groups.values() for days, _, _ in group}
    calendar_ids = {days: str(i+1) for i, days in enumerate(sorted(unique_calendars))}
    for (line, stops, headsign, beyond, nop, nod, approx), group in sorted(groups.items()):
        timings = []
        for (days, deps, arrs), starts in sorted(group.items()):
            t = dict(service=calendar_ids[days], offsets=deps, starts=sorted(starts))
            if arrs != deps:
                t['arrivalOffsets'] = arrs
            timings.append(t)
        pattern = dict(line=line, stops=stops, destinationName=headsign, noPickup=nop,
                       noDropOff=nod, approximate=approx, timings=timings)
        if beyond:
            pattern['continuesBeyondMap'] = True
        patterns.append(pattern)
    if not patterns:
        raise ValueError(f'{city} has no matched trips')
    data = dict(version=1, city=city, sourceDate=source_date, validThrough=end,
                calendars={i: compressed_calendar(days) for days, i in calendar_ids.items()},
                allowsReferenceWeek=False, attribution=attribution, sourceSHA256=input_hash, patterns=patterns)
    return data


def schedule_patterns(data, corridors, mapped, names, regions):
    result = []
    codes = [s[1] for s in data['stations']]
    physical = {p for i, p in enumerate(data['trips']['pattern']) if '+' not in data['trips']['id'][i]}
    for pattern_id, (station_indices, deltas, flags) in enumerate(data['patterns']):
        if pattern_id not in physical:
            result.append(([], (), 0))
            continue
        stops = [codes[i] for i in station_indices]
        if len(deltas) != 2 * (len(stops)-1) or len(flags) != len(stops):
            raise ValueError('Invalid CIF pattern')
        # Copy only the public train timings, passenger flags and CRS identifiers.
        # Station display names (including off-map termini) come from NaPTAN.
        arr, dep = [0], [0]
        for travel, dwell in zip(deltas[::2], deltas[1::2]):
            if min(travel, dwell) < 0:
                raise ValueError('Invalid stop time')
            arr.append(dep[-1] + 60 * travel)
            dep.append(arr[-1] + 60 * dwell)
        mapped_calls = defaultdict(list)
        for i, crs in enumerate(stops):
            if crs in mapped:
                station = mapped[crs]
                for city in corridors.stations[station]:
                    mapped_calls[city].append((i, station))
        pieces = []
        destination = names.get(stops[-1], stops[-1])
        for city, local in mapped_calls.items():
            stations = tuple(s for _, s in local)
            for line, first, last in corridors.segments(city, stations):
                selected = local[first:last+1]
                indices = [i for i, _ in selected]
                base = dep[indices[0]]
                beyond = last == len(local)-1 and indices[-1] < len(stops)-1
                no_pickup = tuple(k for k, i in enumerate(indices)
                                  if int(flags[i]) & 2 == 0 or (k == len(indices)-1 and not beyond))
                no_drop = tuple(k for k, i in enumerate(indices) if int(flags[i]) & 1 == 0)
                if len(no_pickup) == len(indices):
                    continue
                key = (line, stations[first:last+1], destination, beyond, no_pickup, no_drop, ())
                departure_offsets = tuple(dep[i] - base for i in indices)
                arrival_offsets = (0,) + tuple(arr[i] - base for i in indices[1:])
                pieces.append((city, key, departure_offsets, arrival_offsets, base))
        # CIF does not publish dates for some bank holiday overrides in this
        # converted view. Exclude holidays in every country the train serves.
        countries = {regions[c] for c in stops if c in regions}
        result.append((pieces, tuple(sorted(countries)), dep[-1]))
    return result


def calendars(data, holidays):
    start = date.fromisoformat(data['window']['start'])
    length = data['window']['days']
    days = [start + timedelta(days=i) for i in range(length)]
    vectors = []
    for record in data['services']:
        if len(record) != (length + 3) // 4:
            raise ValueError('Invalid running day mask')
        vectors.append([d for i, d in enumerate(days) if int(record[i//4], 16) & (1 << (i%4))])
    bank_days = {country: {date.fromisoformat(x['date']) for x in holidays[country]['events']}
                 for country in ('england-and-wales', 'scotland')}

    @lru_cache(maxsize=None)
    def active(service_id, countries, extra_days):
        excluded = set().union(*(bank_days[c] for c in countries))
        return tuple(date_number(d) for d in vectors[service_id]
                     if all(d + timedelta(days=i) not in excluded for i in range(extra_days+1)))
    return active


def build(data_file, xml, holiday_file, networks, publish, fail, wirral_verification=None):
    raw = data_file.read_bytes()
    data = json.loads(raw)
    if data['format'] != 1 or data['source']['kind'] != 'cif' or data['info']['publisher'] != 'Network Rail CIF':
        raise ValueError('A production Network Rail passenger CIF bundle is required')
    source_date = datetime.strptime(data['source']['extracted'], '%d%m%y%H%M').date()
    window_end = date.fromisoformat(data['searchable']['end'])
    active = calendars(data, json.loads(holiday_file.read_text()))
    corridors = Corridors(networks)
    ids, names, regions = station_reference(xml, set(corridors.stations))
    unresolved = corridors.stations.keys() - set(ids.values())
    blocked = {city for station in unresolved for city in corridors.stations[station]}
    for city in sorted(blocked):
        fail(city, ValueError('NaPTAN station mapping changed: ' +
                              ', '.join(sorted(s for s in unresolved if city in corridors.stations[s]))))
    patterns = schedule_patterns(data, corridors, ids, names, regions)
    groups = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    trips = data['trips']
    for i, pat in enumerate(trips['pattern']):
        # The feed also contains composite split/join possibilities. Original
        # physical schedules preserve boarding rules without inventing extra trains.
        if '+' in trips['id'][i]:
            continue
        pieces, countries, duration = patterns[pat]
        if not pieces:
            continue
        start = trips['start'][i] * 60
        days = active(trips['service'][i], countries, (start + duration) // 86400)
        if not days:
            continue
        for city, key, dep, arr, offset in pieces:
            groups[city][key][days, dep, arr].add(start + offset)
    if 'liverpool' in networks and 'liverpool' not in blocked and wirral_verification:
        window_start = date.fromisoformat(data['window']['start'])
        try:
            add_verified_wirral_calls(groups['liverpool'], wirral_verification, window_start, window_end)
            add_verified_northern_calls(groups['liverpool'], wirral_verification, window_start, window_end)
        except Exception as error:
            fail('liverpool', error)
            blocked.add('liverpool')
    input_hash = hashlib.sha256(raw).hexdigest()
    attribution = dict(source='Network Rail Infrastructure Limited · DirectConnectionFinder · DfT NaPTAN',
                       url=SCHEDULE_URL, terms=LICENCE, notice=NOTICE.format(year=source_date.year))
    for city in sorted(corridors.cities.keys() - blocked):
        credit = attribution
        if city == 'liverpool' and wirral_verification:
            sources = [dict(title='Merseytravel · Wirral Line', url=WIRRAL_BOOKLET),
                       dict(title='Merseytravel · Northern Line', url=NORTHERN_BOOKLET)]
            credit = dict(attribution, estimateSources=sources,
                          notice=attribution['notice'] + ' Wirral Line times at Lime Street Low Level and '
                          'Liverpool Central, and Northern Line times at Moorfields, are estimated from '
                          'published Merseytravel running times. Each estimated call is cross-checked '
                          'against the passenger timetable for that service date.')
        try:
            publish(package(city, groups[city], source_date.isoformat(), date_number(window_end), credit, input_hash))
        except Exception as error:
            fail(city, error)
