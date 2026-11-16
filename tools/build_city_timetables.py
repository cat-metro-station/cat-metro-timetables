#!/usr/bin/env python3
"""Generate offline city packages from public timetables (Python 3.9+, standard library)."""
import argparse
import collections
import csv
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request
import zipfile

CITIES = ('newYork', 'losAngeles', 'beijing', 'shanghai')
GTFS = {
    'newYork': 'https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip',
    'losAngeles': 'https://gitlab.com/LACMTA/gtfs_rail/-/raw/master/gtfs_rail.zip',
}
MIN_STATIONS = {'newYork': 492, 'losAngeles': 109, 'beijing': 410, 'shanghai': 412}
BJ_REPO = 'BoyInTheSun/beijing-subway-schedule'
SH_REPO = 'Naptie/openmetro'
BJ_LINES = {'房山线': 'FS', '昌平线': '27', 'S1线': 'S1', '大兴机场线': 'PKX', '首都机场线': 'PEK',
            '亦庄线': 'YZ', '燕房线': 'YF', '西郊线': 'XJ'}
BJ_ALIASES = {'北宫门': '北宫门站', '2号航站楼': '首都机场2号航站楼', '3号航站楼': '首都机场3号航站楼'}


def fetch(url):
    headers = {'User-Agent': 'Cat-Metro-offline-timetables/1.1'}
    if url.startswith('https://api.github.com/') and os.getenv('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as response:
        return response.read()


def revision(repo, path):
    latest = json.loads(fetch(f'https://api.github.com/repos/{repo}/commits?path={path}&per_page=1'))[0]
    return latest['sha'], latest['commit']['committer']['date'][:10]


def upstream(repo, commit, path):
    return json.loads(fetch(f'https://raw.githubusercontent.com/{repo}/{commit}/{path}'))


def seconds(clock):
    parts = list(map(int, clock.split(':')))
    if len(parts) == 2:
        parts.append(0)
    h, m, s = parts
    if not (0 <= h < 48 and 0 <= m < 60 and 0 <= s < 60):
        raise ValueError('Invalid service-day time ' + clock)
    return h * 3600 + m * 60 + s


def service(days, start=0, end=99991231):
    return dict(days=days, start=start, end=end, added=[], removed=[])


class Builder:
    def __init__(self, city, network):
        self.city = city
        self.station_ids = {s['id'] for s in network['stations']}
        self.lookup = {v.replace('・', '·'): s['id'] for s in network['stations']
                       for v in [s['id'], s['zh']] + s.get('codes', [])}
        self.symbols = {line['symbol'] for line in network['lines']}
        self.groups = collections.defaultdict(lambda: collections.defaultdict(set))
        self.calendars = {}
        self.source_date = ''
        self.valid_through = None
        self.attribution = {}

    def trip(self, line, calendar, stops, times, no_pickup=(), no_drop=(), approximate=(), boundary=None):
        if len(stops) < 2 or len(stops) != len(times):
            return
        if times != sorted(times) or times[-1] - times[0] >= 86400:
            raise ValueError(f'Invalid trip times {line}: {times}')
        pattern = (line, tuple(stops), tuple(no_pickup), tuple(no_drop), tuple(approximate), boundary)
        timing = (calendar, tuple(t - times[0] for t in times))
        self.groups[pattern][timing].add(times[0])

    def package(self):
        patterns, covered = [], set()
        for (line, stops, nop, nod, approx, boundary), timings in self.groups.items():
            covered.update(stop for i, stop in enumerate(stops[:-1]) if i not in nop and stop in self.station_ids)
            p = dict(line=line, stops=stops, noPickup=nop, noDropOff=nod, approximate=approx,
                     timings=[dict(service=c, offsets=offsets, starts=sorted(starts))
                              for (c, offsets), starts in timings.items()])
            if boundary:
                p['boundary'] = boundary
            patterns.append(p)
        if len(covered) < MIN_STATIONS[self.city]:
            raise ValueError(f'{self.city}: only {len(covered)} mapped stations, retaining previous package')
        print(f'{self.city}: {len(covered)}/{len(self.station_ids)} mapped stations, {len(patterns)} service patterns')
        package = dict(version=1, city=self.city, sourceDate=self.source_date, attribution=self.attribution,
                       calendars=self.calendars, patterns=patterns)
        if self.valid_through:
            package['validThrough'] = self.valid_through
        return package


def gtfs(b, local):
    raw = local.read_bytes() if local else fetch(GTFS[b.city])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        def rows(name):
            with archive.open(name + '.txt') as f:
                yield from csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig', newline=''))
        if 'frequencies.txt' in archive.namelist() and any(rows('frequencies')):
            raise ValueError('Frequency-based trips require a separate adapter')
        for r in rows('calendar'):
            mask = sum(int(r[k]) << i for i,k in enumerate(('monday','tuesday','wednesday','thursday','friday','saturday','sunday')))
            b.calendars[r['service_id']] = service(mask, int(r['start_date']), int(r['end_date']))
        for r in rows('calendar_dates'):
            cal = b.calendars.setdefault(r['service_id'], service(0))
            cal['added' if r['exception_type'] == '1' else 'removed'].append(int(r['date']))
        info = next(rows('feed_info'))
        if info.get('feed_end_date'):
            b.valid_through = int(info['feed_end_date'])
        version = re.match(r'(\d{8})', info.get('feed_version') or '')
        b.source_date = (dt.datetime.strptime(version[1], '%Y%m%d').date() if version
                         else dt.date(*archive.getinfo('stop_times.txt').date_time[:3])).isoformat()
        b.attribution = {'source': 'MTA New York City Transit' if b.city == 'newYork' else 'Los Angeles County Metropolitan Transportation Authority',
                         'url': GTFS[b.city], 'type': 'Published GTFS schedule. Service calendar and dated exceptions apply.'}
        parent = {r['stop_id']: r['parent_station'] or r['stop_id'] for r in rows('stops')}
        routes = {}
        for r in rows('routes'):
            symbol = r['route_id'] if b.city == 'newYork' else r['route_long_name'].split()[1]
            if symbol in b.symbols:
                routes[r['route_id']] = symbol
        trips = {r['trip_id']: (routes[r['route_id']], r['service_id']) for r in rows('trips') if r['route_id'] in routes}
        times = collections.defaultdict(list)
        for r in rows('stop_times'):
            code = r['stop_id']
            station = b.lookup.get(code) or b.lookup.get(parent[code])
            if not station and b.city == 'losAngeles':
                station = b.lookup.get(parent[code].removesuffix('S'))
            # Keep an unmapped call in the sequence; never present an earlier mapped stop as the terminal.
            station = station or parent[code]
            times[r['trip_id']].append((int(r['stop_sequence']), station, seconds(r['departure_time']),
                r.get('pickup_type','0') not in ('','0'), r.get('drop_off_type','0') not in ('','0'), r.get('timepoint') == '0'))
        for trip, calls in times.items():
            if trip not in trips:
                continue
            calls.sort()
            _, stops, clocks, nop, nod, approx = zip(*calls)
            indices = lambda flags: [i for i, flag in enumerate(flags) if flag]
            b.trip(*trips[trip], stops, list(clocks), indices(nop), indices(nod), indices(approx))


def apply_workday_swaps(calendars):
    # Official State Council holiday/workday swaps, distributed by the holiday-cn project.
    year = dt.date.today().year
    for y in (year, year + 1):
        try:
            data = json.loads(fetch(f'https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{y}.json'))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and y == year + 1:
                continue
            raise
        for d in data['days']:
            day = int(d['date'].replace('-',''))
            off = d['isOffDay']
            calendars['weekday']['removed' if off else 'added'].append(day)
            calendars['weekend']['added' if off else 'removed'].append(day)


def beijing(b):
    commit, b.source_date = revision(BJ_REPO,'train_schedule_all')
    b.attribution = {'author': 'BoyInTheSun; source data by Mick235711 / Beijing-Subway-Tools',
                     'source': f'https://github.com/{BJ_REPO}/tree/{commit}/train_schedule_all',
                     'upstream': 'https://github.com/Mick235711/Beijing-Subway-Tools',
                     'license': 'https://creativecommons.org/licenses/by-sa/4.0/',
                     'changes': 'Adapted station IDs and grouped reconstructed train calls by stopping pattern and calendar. Weekday/weekend estimates; special operations may differ.'}
    b.calendars = {'weekday':service(31), 'weekend':service(96)}
    apply_workday_swaps(b.calendars)
    for day in b.calendars:
        dataset = upstream(BJ_REPO,commit,f'train_schedule_all/schedule_{day}.json')
        for name, directions in dataset.items():
            match = re.match(r'^(\d+)号线', name)
            symbol = match[1] if match else BJ_LINES.get(name)
            if symbol not in b.symbols:
                continue
            for trains in directions.values():
                for calls in trains.values():
                    stops, clocks = [], []
                    for station, clock in calls:
                        if clock.startswith('(') or clock.endswith('-'):  # pass-through or arrival before a hold
                            continue
                        t = seconds(clock)
                        if clocks and t < clocks[-1]:
                            t += 86400
                        stops.append(b.lookup.get(BJ_ALIASES.get(station, station), station))
                        clocks.append(t)
                    b.trip(symbol,day,stops,clocks)


def shanghai(b):
    commit, _ = revision(SH_REPO,'data/cn-shanghai')
    pull = lambda name: upstream(SH_REPO,commit,'data/cn-shanghai/'+name+'.json')
    stations = {s['id']: b.lookup.get(s['name'].replace('・','·'), s['name']) for s in pull('stations')['records']}
    stops = {s['id']:stations[s['station_id']] for s in pull('stops')['records']}
    table = pull('timetables')
    patterns = {r['id']: r for r in pull('patterns')['records']}
    b.source_date = table['generated_at'][:10]
    b.attribution = {'source':'Shanghai Metro published first and last trains, normalized by Naptie / OpenMetro',
                     'openMetroLicense': 'https://github.com/Naptie/openmetro/blob/main/LICENSE',
                     'headway':'https://jtw.sh.gov.cn/xwfb2/20251027/14557ac7aeaa4e95a61cce023ce01a55.html',
                     'timeBands':'https://service.shmetro.com/skin/timetable/2-flt.png',
                     'hongqiaoNightService':'https://www.shanghai.gov.cn/nw17239/20260701/17d579b4885f44b1bb486d93930d3bbd.html',
                     'nonstopService':'https://www.shanghai.gov.cn/nw17239/20260922/dbe1fb96861243e4af33317c0d4b03c4.html',
                     'weekendExtensions':'https://www.meet-in-shanghai.net/en/news/shanghai-extends-metro-service-to-midnight-streamlines-transfers-at-major-hubs-381597/',
                     'url':'https://m.shmetro.com/workspace/shmetrotest/view_lnfltime.aspx',
                     'snapshot':f'https://github.com/{SH_REPO}/tree/{commit}/data/cn-shanghai',
                     'changes':'Extracted first/last facts and terminal names. Estimated Line 2 slots use reported full-length off-peak/regular weekend daytime headways, limited to 11:00–15:00 local time. Also includes the published final regular Line 2 Hongqiao night service and Line 16 weekday nonstops.'}
    b.calendars = {'all':service(127), 'weekday':service(31), 'weekend':service(96)}
    apply_workday_swaps(b.calendars)
    for i in range(7):
        b.calendars[str(i)] = service(1 << i)
    for r in table['records']:
        if r.get('is_arrival'):
            continue
        if r['service'] != 'all_days':
            raise ValueError('Unknown Shanghai operating calendar')
        number = r['line_id'].rsplit('-',1)[-1]
        symbol = 'PJ' if number == '41' else number
        if symbol not in b.symbols:
            continue
        stop = stops[r['stop_id']]
        terminal = stops[r['destination_stop_id']] if r.get('destination_stop_id') else r['direction_label']
        if stop == terminal:
            continue
        # Only the published destination is confirmed for these boundary trains. Do not infer intermediate calls.
        for boundary, values in [('first',r['first_train']),('last',r['last_train'])]:
            if len(values) not in (1,7):
                raise ValueError('Invalid Shanghai weekday array')
            for i, clock in enumerate(values):
                t = seconds(clock)
                if boundary == 'last' and t < 3*3600:
                    t += 86400
                # Shanghai Metro extends Friday/Saturday service. The raw weekday array starts on Sunday.
                day = 'all' if len(values) == 1 else str((i + 6) % 7)
                b.trip(symbol, day, [stop,terminal], [t,t], boundary=boundary)
        if symbol == '2' and r.get('destination_stop_id'):
            main = patterns[r['pattern_id']]['stop_ids']
            if r['destination_stop_id'] not in (main[0], main[-1]) or r['stop_id'] not in main:
                continue
            route = main if r['destination_stop_id'] == main[-1] else list(reversed(main))
            onward = [stops[x] for x in route[route.index(r['stop_id']):]]
            if len(onward) < 2:
                continue
            # The operator's full-route daytime service has no short turns in these bands.
            # The first train anchors the phase only for estimating; individual trips are unpublished.
            phase = seconds(r['first_train'][0])
            for calendar, interval in [('weekday', 300), ('weekend', 240)]:
                begin = 11 * 3600 + (phase - 11 * 3600) % interval
                for departure in range(begin, 15 * 3600, interval):
                    b.trip(symbol, calendar, onward, [departure]*len(onward),
                           no_pickup=range(1,len(onward)), approximate=[0], boundary='headway')

    # The published regular night service names all calls and boarding restrictions.
    # Only the origin has a published departure time. Never suggest boarding downstream
    # from zero placeholder offsets, including at Hongqiao T2 where boarding is allowed.
    late = [b.lookup[name] for name in ('虹桥火车站','虹桥2号航站楼','淞虹路','威宁路','中山公园',
                                       '静安寺','人民广场','世纪大道','龙阳路')]
    for weekday in range(7):
        t = seconds('24:00' if weekday == 6 else '23:30')
        b.trip('2', str(weekday), late, [t]*len(late), no_pickup=range(1,len(late)))

    express_day = dict(b.calendars['weekday'], start=20260928)
    express_day['added'] = [date for date in express_day['added'] if date >= express_day['start']]
    b.calendars['nonstopWeekday'] = express_day
    for start, end, clocks in [('龙阳路','滴水湖',('07:30','07:45','12:30')),
                               ('滴水湖','龙阳路',('12:30','17:20','18:00'))]:
        for clock in clocks:
            t = seconds(clock)
            b.trip('16','nonstopWeekday',[b.lookup[start],b.lookup[end]],[t,t])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--city',required=True,choices=CITIES)
    ap.add_argument('--network-dir',type=Path,required=True)
    ap.add_argument('--output-dir',type=Path,required=True)
    ap.add_argument('--gtfs-file',type=Path,help='Use a locally downloaded operator ZIP')
    args = ap.parse_args()
    net = json.loads((args.network_dir/f'network-{args.city}.json').read_bytes())
    builder = Builder(args.city,net)
    if args.city in GTFS:
        gtfs(builder,args.gtfs_file)
    elif args.city == 'beijing':
        beijing(builder)
    else:
        shanghai(builder)
    package = builder.package()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir/f'offline-{args.city}.json'
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=args.output_dir,delete=False) as f:
        json.dump(package,f,ensure_ascii=False,separators=(',',':'))
        f.write('\n')
    os.replace(f.name,output)


if __name__ == '__main__':
    main()
