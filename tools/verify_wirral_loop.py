"""Verify inferred Liverpool loop calls against a separately obtained passenger GTFS.

Network Rail's public conversion omits two low-level calls. Merseytravel's
Wirral Line timetable (20 September 2026, pp. 3, 6, 11) gives the running
times between Moorfields and James Street via Lime Street and Central.
Only apply those few timing facts to an actual, dated Network Rail service
when a separate timetable confirms the precise calls and boarding rights.
"""
from collections import defaultdict
import csv
from datetime import timedelta
import io
from zipfile import ZipFile

from build_city_timetables import seconds

JAMES = '9100JAMESST'
MOOR = '9100MORFLDS'
LIME = '9100LVRPLSL'
CENTRAL = '9100LVRPLCH'
BOOKLET = 'https://merseytravel.adidocdn.dev/assets/uploads/Wirral_Line_Booklet_Sep_26_compressed.pdf'


def rows(z, table):
    with io.TextIOWrapper(z.open(table + '.txt'), encoding='utf-8-sig', newline='') as stream:
        yield from csv.DictReader(stream)


def passenger_calendars(z, start, end):
    weekdays = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
    window = (start + timedelta(days=i) for i in range((end-start).days+1))
    dates = [(int(day.strftime('%Y%m%d')), weekdays[day.weekday()]) for day in window]
    calendars = defaultdict(set)
    for r in rows(z, 'calendar'):
        calendars[r['service_id']] = {d for d, w in dates if r[w] == '1'
                                      and int(r['start_date']) <= d <= int(r['end_date'])}
    for r in rows(z, 'calendar_dates'):
        day = int(r['date'])
        if dates[0][0] <= day <= dates[-1][0]:
            active = calendars[r['service_id']]
            if r['exception_type'] == '1':
                active.add(day)
            else:
                active.discard(day)
    return calendars


def passenger_checks(feed, start, end):
    """Return corroborated service dates for the exact loop, profile and destination."""
    eligible = defaultdict(set)
    with ZipFile(feed) as z:
        trips = {r['trip_id']: (r['trip_headsign'], r['service_id']) for r in rows(z, 'trips')
                 if r['route_id'] == 'ME_Wirral'}
        calendars = passenger_calendars(z, start, end)
        calls = defaultdict(list)
        stop_codes = (JAMES, MOOR, LIME, '9100LVRPLCL')
        for r in rows(z, 'stop_times'):
            trip = r['trip_id']
            if trip in trips:
                code = next((c for c in stop_codes if r['stop_id'].startswith(c)), None)
                # Keep all calls in sequence, so a diversion cannot match the loop.
                calls[trip].append((int(r['stop_sequence']), code, r['arrival_time'], r['departure_time'],
                                    r['pickup_type'] in ('', '0') and r['drop_off_type'] in ('', '0')))
    for trip, items in calls.items():
        items.sort()
        dest, service_id = trips[trip]
        for i in range(len(items)-4):
            block = items[i:i+5]
            if tuple(b[1] for b in block) != (JAMES, MOOR, LIME, '9100LVRPLCL', JAMES):
                continue
            if not all(b[4] for b in block[2:4]) or any(b[2] != b[3] for b in block[2:4]):
                continue
            key = (dest, *(seconds(b[3]) for b in block[:4]), seconds(block[-1][2]))
            eligible[key].update(calendars[service_id])
    return eligible


def add_verified_wirral_calls(groups, verification, start, end):
    eligible = passenger_checks(verification, start, end)
    for key, timings in list(groups.items()):
        line, stops, dest, beyond, nop, nod, approximate = key
        if line != 'Wirral':
            continue
        i = next((i for i in range(1, len(stops)-1) if stops[i-1:i+2] == (JAMES, MOOR, JAMES)), None)
        if i is None:
            continue
        new_stops = stops[:i+1] + (LIME, CENTRAL) + stops[i+1:]
        def shifted(markers):
            return tuple(idx + (2 if idx > i else 0) for idx in markers)
        new_key = (line, new_stops, dest, beyond, shifted(nop), shifted(nod),
                   tuple(sorted((*shifted(approximate), i+1, i+2))))
        del groups[key]
        for (days, deps, arrs), starts in timings.items():
            for clock in starts:
                mrf = clock + deps[i]
                # The last Hamilton Square train has a shorter Central section.
                late = dest == 'Birkenhead Hamilton Square' and mrf == 23*3600+56*60
                extra = (deps[i]+120, deps[i]+(180 if late else 240))
                signature = (dest, clock+deps[i-1], mrf, clock+extra[0], clock+extra[1], clock+arrs[i+1])
                confirmed = eligible.get(signature, ())
                exact = tuple(day for day in days if day in confirmed)
                other = tuple(day for day in days if day not in confirmed)
                if exact:
                    ds = deps[:i+1] + extra + deps[i+1:]
                    ars = arrs[:i+1] + extra + arrs[i+1:]
                    groups[new_key][exact, ds, ars].add(clock)
                if other:
                    groups[key][other, deps, arrs].add(clock)
