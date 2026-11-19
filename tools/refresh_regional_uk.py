#!/usr/bin/env python3
"""Refresh UK regional boards. --offline replays source files in --cache for validation."""
import argparse
from datetime import date
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile

import regional_uk_local as local
import regional_uk_rail as rail
import regional_uk_tram as tram

NETWORKS = Path(__file__).resolve().parents[1] / 'inputs/regional-uk.json'
SOURCES = {
    'schedule.json': 'https://jameswardvp.github.io/DirectConnectionFinder/data/timetable.json',
    'naptan.xml': 'https://naptan.api.dft.gov.uk/v1/access-nodes?dataFormat=xml&atcoAreaCodes=910',
    'holidays.json': 'https://www.gov.uk/bank-holidays.json',
    'merseyrail-check.zip': 'https://github.com/planarnetwork/gb-transit/releases/latest/download/gtfs-national-rail-only.zip',
    'belfast.zip': local.ATTRIBUTION['belfast']['url'],
    'nottingham.zip': local.ATTRIBUTION['nottingham']['url'],
    'nottingham-profiles.html': 'https://bustimes.org/services/tram-clifton-toton-nottingham-phoenix-park-hucknal/debug',
}
REGIONS = dict(tram.OPERATORS.values())
for region in set(REGIONS.values()):
    SOURCES[region + '.zip'] = 'https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/' + region + '/'


def refresh(output, cache, cities, offline=False):
    networks = json.loads(NETWORKS.read_bytes())
    selected = set(cities or networks)
    if unknown := selected - networks.keys():
        raise ValueError(f'Unknown regional UK cities: {sorted(unknown)}')
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    failed = set()
    completed = set()

    def fail(city, error):
        failed.add(city)
        print(f'{city}: {error}', file=sys.stderr, flush=True)

    @lru_cache(maxsize=None)
    def fetch(key):
        path = cache / key
        if offline:
            if not path.is_file():
                raise FileNotFoundError(path)
            return path
        # Always revalidate the upstream data on a networked run. In-progress
        # or interrupted downloads never replace the last complete input file.
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error', '--retry', '3',
                            '--connect-timeout', '20', '--max-time', '480', '--user-agent',
                            'cat-metro-station/1.4 timetable refresh', '--output', str(temporary_path),
                            SOURCES[key]], check=True)
            if key.endswith('.zip'):
                with ZipFile(temporary_path) as archive:
                    if 'trips.txt' not in archive.namelist():
                        raise ValueError(f'{key} is not a GTFS train schedule')
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return path

    def publish(package):
        city = package['city']
        network = networks[city]['lines']
        ids = {line: {s for route in stops for s in route} if isinstance(stops[0], list) else set(stops)
               for line, stops in network.items()}
        boardable = set()
        if package['validThrough'] < int(date.today().strftime('%Y%m%d')):
            raise ValueError(f'Upstream timetable expired on {package["validThrough"]}')
        for pattern in package['patterns']:
            if pattern['line'] not in ids or set(pattern['stops']) - ids[pattern['line']]:
                raise ValueError(f'Station or line has changed on {pattern["line"]}. Review the network map.')
            for i, station in enumerate(pattern['stops']):
                if i not in pattern['noPickup'] and (i < len(pattern['stops']) - 1 or pattern.get('continuesBeyondMap')):
                    boardable.add(station)
        omitted = set.union(*ids.values()) - boardable
        if omitted:
            raise ValueError(f'No boardable departures at {sorted(omitted)}')
        package['allowsReferenceWeek'] = False
        path = output / f'offline-{city}.json'
        with tempfile.NamedTemporaryFile(mode='w', dir=output, delete=False, encoding='utf-8') as target:
            json.dump(package, target, ensure_ascii=False, separators=(',', ':'))
            target.write('\n')
        Path(target.name).replace(path)
        completed.add(city)
        print(f'{city}: {len(boardable)} stations, valid through {package["validThrough"]}', flush=True)

    national = {i: networks[i] for i in selected if i not in REGIONS and i not in local.ATTRIBUTION}
    check = None
    if 'liverpool' in national:
        try:
            check = fetch('merseyrail-check.zip')
        except Exception as error:
            fail('liverpool', error)
            del national['liverpool']
    if national:
        try:
            rail.build(fetch('schedule.json'), fetch('naptan.xml'), fetch('holidays.json'), national, publish, fail, check)
        except Exception as error:
            for city in national.keys() - completed:
                fail(city, error)
    for city in sorted(selected & (REGIONS.keys() | local.ATTRIBUTION.keys())):
        try:
            if city in REGIONS:
                with ZipFile(fetch(REGIONS[city] + '.zip')) as feed:
                    publish(tram.build(feed, city))
            else:
                with ZipFile(fetch(city + '.zip')) as feed:
                    if city == 'nottingham':
                        package = local.build(city, feed, networks[city], fetch('nottingham-profiles.html'), fetch('holidays.json'))
                    else:
                        package = local.build(city, feed, networks[city])
                    publish(package)
        except Exception as error:
            fail(city, error)
    return not (failed or selected - completed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--city', action='append', help='Restrict to a city ID (repeatable). Default: all 32.')
    parser.add_argument('--offline', action='store_true', help='Use already downloaded files for a reproducible test.')
    args = parser.parse_args()
    sys.exit(0 if refresh(args.output, args.cache, args.city, args.offline) else 1)
