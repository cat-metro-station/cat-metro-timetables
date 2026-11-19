"""Build estimated Las Vegas Monorail boards from operator hours, ride times and notices."""
import calendar
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from itertools import accumulate
import re
import urllib.request

SOURCE = 'https://www.lvmonorail.com/route-map/'
MAP = 'https://www.lvmonorail.com/wp-content/uploads/2026/06/Horizontal_LV_Strip_Map_0426-scaled.webp'
STOPS = ['mgm-grand', 'horseshoe-paris', 'flamingo-caesars', 'harrahs-linq', 'lvcc', 'westgate', 'sahara']
STATION_NAMES = ['MGM Grand Station', 'Horseshoe/Paris Station', 'Flamingo/Caesars Palace Station',
                 'Harrah’s/The LINQ Station', 'Boingo Station at the Las Vegas Convention Center',
                 'Westgate Station', 'SAHARA Las Vegas Station']
NUMBERS = 'zero one two three four five six seven eight nine ten eleven twelve'.split()
NUMBER = r'(?:\d+|' + '|'.join(NUMBERS[1:]) + ')'
CLOCK = r'(?:midnight|noon|\d{1,2}(?::\d{2})?\s*[ap]\.?\s*m\.?)'
MONTH = '(?:' + '|'.join(list(calendar.month_name)[1:]) + ')'
WEEKDAY = '(?:' + '|'.join(calendar.day_name) + ')'
DAY = timedelta(days=1)
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
BLOCKS = {'p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}


def number(text):
    return int(text) if text.isdigit() else NUMBERS.index(text.lower())


def minute(text):
    text = re.sub(r'[.\s]', '', text.lower())
    if text in {'midnight', 'noon'}:
        return 0 if text == 'midnight' else 720
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?([ap])m', text)
    if not match or not 1 <= int(match[1]) <= 12 or int(match[2] or 0) >= 60:
        raise ValueError(f'Invalid published operating time: {text}')
    return (int(match[1]) % 12 + 12 * (match[3] == 'p')) * 60 + int(match[2] or 0)


class RoutePage(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.blocks = []
        self.links = set()
        self.popups = []
        self.feed(html)
        self.blocks = [(tag, ' '.join(' '.join(parts).split()).strip(), popup) for tag, parts, popup in self.blocks]

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        classes = set(attr.get('class', '').split())
        if tag == 'a':
            self.links.add(attr.get('href', ''))
        popup = self.stack[-1][2] if self.stack else None
        if 'kadence-conversion-wrap' in classes:
            popup = len(self.popups)
            self.popups.append(popup)
        block = None
        if tag in BLOCKS or 'sc-tl-label' in classes:
            block = []
            self.blocks.append((tag, block, popup))
        if tag not in VOID:
            self.stack.append((tag, block, popup))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        for _, block, _ in self.stack:
            if block is not None:
                block.append(data)


def line_times(page, texts):
    rides = [int(m[1]) for t in texts if (m := re.fullmatch(r'(\d+) Minute Ride', t, re.I))]
    if MAP in page.links and rides == [2, 1, 1, 4, 2, 1]:
        return [rides, list(reversed(rides))]
    result = []
    for direction, stations in [('Northbound', STATION_NAMES), ('Southbound', STATION_NAMES[::-1])]:
        at = texts.index(direction + ' travel times between stations are:')
        segments = []
        for i, row in enumerate(texts[at+1:at+7]):
            pattern = re.escape(stations[i]) + r':\s*(' + NUMBER + r') minutes? to ' + re.escape(stations[i+1]) + r'\.?'
            match = re.fullmatch(pattern, row, re.I)
            if not match:
                raise ValueError(f'Unrecognised {direction} monorail ride time: {row}')
            segments.append(number(match[1]))
        if len(segments) != 6 or min(segments) <= 0:
            raise ValueError('Incomplete monorail ride times')
        result.append(segments)
    return result


def regular_hours(texts):
    start = None
    week = {}
    for i, t in enumerate(texts):
        match = re.fullmatch(r'The Las Vegas Monorail operates daily beginning at (' + CLOCK + r') until these times:', t, re.I)
        if not match:
            continue
        start = minute(match[1])
        for row in texts[i+1:]:
            m = re.fullmatch('(' + WEEKDAY + ')(?: to (' + WEEKDAY + '))?: (' + CLOCK + ')', row, re.I)
            if not m:
                break
            first = list(calendar.day_name).index(m[1].title())
            last = list(calendar.day_name).index((m[2] or m[1]).title())
            close = minute(m[3])
            for delta in range((last-first) % 7 + 1):
                w = (first+delta) % 7
                if w in week:
                    raise ValueError('Overlapping regular Las Vegas service days')
                week[w] = (start, close + (1440 if close <= start else 0), False)
        break
    if len(week) != 7:
        raise ValueError('Incomplete Las Vegas Monorail regular hours')
    return week


def half_date(part, year, month):
    m = re.fullmatch(r'(?:(' + MONTH + r')\s+)?(\d{1,2})(?:,?\s+(\d{4}))?', part.strip(), re.I)
    if not m:
        raise ValueError(f'Unrecognised monorail service date: {part}')
    month = list(calendar.month_name).index(m[1].title()) if m[1] else month
    return date(int(m[3] or year), month, int(m[2]))


def closure(row):
    m = re.fullmatch(r'(.+?):\s*(' + CLOCK + r')\s*[-–—]\s*(' + CLOCK + r')', row, re.I)
    if not m or not (years := re.findall(r'\b\d{4}\b', m[1])):
        raise ValueError(f'Unrecognised dated monorail operating hours: {row}')
    ends = re.split(r'\s*[-–—]\s*', m[1])
    if len(ends) > 2:
        raise ValueError(f'Ambiguous monorail service dates: {m[1]}')
    first = half_date(ends[0], int(years[0]), 0)
    last = half_date(ends[-1], int(years[-1]), first.month)
    if last < first and len(years) == 1 and not re.search(r'\b\d{4}\b', ends[0]):
        first = first.replace(year=first.year - 1)
    if not 0 <= (last-first).days < 367:
        raise ValueError(f'Invalid monorail service date range: {m[1]}')
    start, close = minute(m[2]), minute(m[3])
    return first, last, (start, close + (1440 if close <= start else 0), False)


def moment(part, reference):
    m = re.fullmatch(r'(' + WEEKDAY + r'),?\s+(' + MONTH + r'\s+\d{1,2}(?:,?\s+\d{4})?) at (' + CLOCK + r')', part, re.I)
    if not m:
        raise ValueError(f'Unrecognised monorail nonstop date: {part}')
    explicit_year = re.search(r'\d{4}\b', m[2])
    years = [int(explicit_year[0])] if explicit_year else range(reference.year-1, reference.year+2)
    days = [half_date(m[2], y, 0) for y in years]
    matches = [d for d in days if calendar.day_name[d.weekday()].lower() == m[1].lower()]
    if len(matches) != 1:
        raise ValueError('Monorail notice weekday and date disagree')
    return datetime.combine(matches[0], time.min) + timedelta(minutes=minute(m[3]))


def service_notices(page, week, reference):
    special = {}

    def override(day, hours):
        if day in special and special[day] != hours:
            raise ValueError(f'Overlapping monorail service notices on {day}')
        special[day] = hours

    for tag, text, popup in page.blocks:
        if popup is None and re.search(r'^(?:System Notice|Service (?:Alert|Notice)|Temporary (?:Closure|Suspension))', text, re.I):
            raise ValueError(f'Unexpected monorail service notice outside the notices panel: {text}')
    for popup in page.popups:
        rows = [t for tag, t, loc in page.blocks if loc == popup and t]
        if not rows:
            continue
        if 'uses cookies' in rows[0].lower() and 'privacy policy' in ' '.join(rows).lower():
            continue
        heading, body = rows[0], rows[1:]
        if re.fullmatch(r'System Notice\s*[-–—]\s*Early Closure', heading, re.I):
            # All meaningful content must be recognised; a new exception appended
            # to an existing alert should stop publication for review.
            rules = [r for r in body if not re.fullmatch(r'Thank you for your interest in the Las Vegas Monorail\. Please note that the system will have modified hours of operation on the following dates:|We appreciate your understanding and apologize for the inconvenience\. We look forward to welcoming you on board soon\.', r, re.I)]
            if not rules:
                raise ValueError('Empty monorail closure notice')
            for row in rules:
                first, last, hours = closure(row)
                for i in range((last-first).days+1):
                    override(first+DAY*i, hours)
        elif re.fullmatch(r'System Notice\s*[-–—]\s*Extended Hours For F1 Las Vegas', heading, re.I):
            rules = [r for r in body if not re.fullmatch(r'We look forward to welcoming you on board during race week!', r, re.I)]
            if len(rules) != 1 or not (m := re.fullmatch(r'The Monorail will be operational nonstop from (.+?)\s*[-–—]\s*(.+?)\.', rules[0], re.I)):
                raise ValueError('Unrecognised monorail nonstop service notice')
            start, end = moment(m[1], reference), moment(m[2], reference)
            if not 0 < (end-start).total_seconds() <= 32 * 86400:
                raise ValueError('Invalid monorail nonstop service window')
            day = start.date()
            while day < end.date() or (day == end.date() and end.time() > time.min):
                midnight = datetime.combine(day, time.min)
                # Keep each service day anchored at its normal opening. The final
                # overnight portion belongs to the previous service day.
                opening = start if day == start.date() else midnight + timedelta(minutes=week[day.weekday()][0])
                if opening >= end:
                    break
                next_open = midnight + DAY + timedelta(minutes=week[(day.weekday()+1) % 7][0])
                until = min(next_open, end)
                override(day, (int((opening-midnight).total_seconds()//60),
                               int((until-midnight).total_seconds()//60), until < end))
                day += DAY
        else:
            raise ValueError(f'Unrecognised Las Vegas Monorail service notice: {heading}')
    return special


def parse(html, reference):
    page = RoutePage(html)
    texts = [t for _, t, _ in page.blocks if t]
    bands = {tuple(number(v) for v in m.groups()) for t in texts
             for m in re.finditer(r'Monorail trains arrive continuously every (' + NUMBER + ') to (' + NUMBER + ') minutes', t, re.I)}
    if len(bands) != 1:
        raise ValueError('Unverified Las Vegas Monorail headway')
    band = bands.pop()
    if not 0 < band[0] <= band[1] <= 60:
        raise ValueError('Invalid Las Vegas Monorail headway')
    weekly = regular_hours(texts)
    special = service_notices(page, weekly, reference)
    durations = line_times(page, texts)
    periods = defaultdict(list)
    # Estimates are periodically re-checked. Omit New Year's Eve unless the
    # operator publishes dated hours for this service day.
    for i in range(-1, 43):
        day = reference+DAY*i
        if (day.month, day.day) == (12, 31) and day not in special:
            continue
        periods[special.get(day, weekly[day.weekday()])].append(int(day.strftime('%Y%m%d')))
    calendars = {}
    patterns = []
    for stops, rides in [(STOPS, durations[0]), (STOPS[::-1], durations[1])]:
        offsets = [0] + [60*m for m in accumulate(rides)]
        timings = []
        for (opening, close, continuous), dates in sorted(periods.items()):
            key = f'{opening}-{close}-' + ('through' if continuous else 'close')
            calendars[key] = dict(days=0, start=dates[0], end=dates[-1], added=dates, removed=[])
            # Complete the journey by closing. During nonstop service the
            # following service day continues at its normal opening minute.
            until = close*60 if continuous else close*60-offsets[-1]+1
            starts = list(range(opening*60, until, band[1]*60))
            if not starts:
                raise ValueError('Published service window is shorter than the journey')
            timings.append(dict(service=key, offsets=offsets, starts=starts))
        patterns.append(dict(line='LV', stops=stops, noPickup=[], noDropOff=[],
                             approximate=list(range(len(stops))), boundary='headway', headwayMinutes=list(band),
                             timings=timings))
    return dict(version=1, city='lasVegas', sourceDate=reference.isoformat(), validThrough=max(d[-1] for d in periods.values()),
                allowsReferenceWeek=False, calendars=calendars, patterns=patterns,
                attribution=dict(source='Las Vegas Monorail', url=SOURCE,
                                 notice=f'Planning estimates use the published {band[0]}–{band[1]} minute headway, ride times, operating hours and service notices. Source checked {reference.isoformat()}.'))


def build(reference: date):
    request = urllib.request.Request(SOURCE, headers={'User-Agent': 'cat-metro-transit-schedule'})
    with urllib.request.urlopen(request, timeout=45) as response:
        return parse(response.read().decode('utf-8'), reference)
