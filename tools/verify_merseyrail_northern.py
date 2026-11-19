"""Restore independently corroborated Moorfields calls on Merseyrail's Northern Line.

The station's northern platforms have a different source code from its Wirral
platform. The regional map uses one passenger-facing Moorfields interchange.
The 7 Nov 2026 operator booklet gives the usual two directional running times.
"""
from collections import defaultdict
from zipfile import ZipFile

from build_city_timetables import seconds
from verify_wirral_loop import rows, passenger_calendars

BOOKLET = 'https://merseytravel.adidocdn.dev/assets/uploads/Northern_Line_Booklet_compressed_7Nov26.pdf'
SANDHILLS = '9100SANDH'
CENTRAL = '9100LVRPLCH'
MOORFIELDS = '9100MORFLDS'
NORTHERN_PLATFORMS = '9100MORFNL'
# Source: timetable pp. 3, 7 and 17. Durations are used only if the exact trip
# on that date independently confirms the inferred Moorfields minute and rights.
TO_MOORFIELDS = {(SANDHILLS, CENTRAL): 4 * 60, (CENTRAL, SANDHILLS): 3 * 60}


def confirmed_calls(feed, start, end):
    checks = defaultdict(set)
    with ZipFile(feed) as z:
        dates = passenger_calendars(z, start, end)
        trips = {r['trip_id']: (r['trip_headsign'], r['service_id']) for r in rows(z, 'trips')
                 if r['route_id'] == 'ME_Northern' and dates[r['service_id']]}
        stations = {r['stop_id']: code for r in rows(z, 'stops')
                    for code in (SANDHILLS, CENTRAL, NORTHERN_PLATFORMS)
                    if r['stop_id'].startswith(code)}
        calls = defaultdict(list)
        for r in rows(z, 'stop_times'):
            if r['trip_id'] in trips:
                calls[r['trip_id']].append((int(r['stop_sequence']), stations.get(r['stop_id']),
                                            r['arrival_time'], r['departure_time'],
                                            r['pickup_type'] in ('', '0') and r['drop_off_type'] in ('', '0')))
        for trip, calls_on_trip in calls.items():
            dest, service = trips[trip]
            ordered = sorted(calls_on_trip)
            for before, middle, after in zip(ordered, ordered[1:], ordered[2:]):
                interval = TO_MOORFIELDS.get((before[1], after[1]))
                if not interval or middle[1] != NORTHERN_PLATFORMS or not middle[4]:
                    continue
                if not before[3] or not after[2] or not middle[2] or middle[2] != middle[3]:
                    continue
                depart = seconds(before[3])
                if seconds(middle[3]) != depart + interval:
                    continue
                checks[dest, before[1], after[1], depart, seconds(after[2])].update(dates[service])
    return checks


def add_verified_northern_calls(groups, verification, start, end):
    checks = confirmed_calls(verification, start, end)
    for key, timing in list(groups.items()):
        line, stops, dest, beyond, nop, nod, approx = key
        if line != 'Northern':
            continue
        anchors = [(i, TO_MOORFIELDS[pair]) for i, pair in enumerate(zip(stops, stops[1:]))
                   if pair in TO_MOORFIELDS]
        if len(anchors) != 1:
            continue
        i, interval = anchors[0]
        at = i + 1

        def shift(indices):
            return tuple(index + (index >= at) for index in indices)

        extended = (line, stops[:at] + (MOORFIELDS,) + stops[at:], dest, beyond,
                    shift(nop), shift(nod), tuple(sorted((*shift(approx), at))))
        del groups[key]
        for (dates, deps, arrs), clocks in timing.items():
            middle = deps[i] + interval
            for first in clocks:
                signature = (dest, stops[i], stops[at], first + deps[i], first + arrs[at])
                verified = checks.get(signature, ())
                with_call = tuple(day for day in dates if day in verified)
                without_call = tuple(day for day in dates if day not in verified)
                if with_call:
                    ds = deps[:at] + (middle,) + deps[at:]
                    ars = arrs[:at] + (middle,) + arrs[at:]
                    groups[extended][with_call, ds, ars].add(first)
                if without_call:
                    groups[key][without_call, deps, arrs].add(first)
