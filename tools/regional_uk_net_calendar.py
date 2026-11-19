"""Reconcile UK NET trip calendars with the TNDS operating profiles and statutory holidays."""
from datetime import date, datetime, timedelta
from functools import lru_cache
from html.parser import HTMLParser
import json
import re

from build_city_timetables import service


class JourneyTable(HTMLParser):
    """Read the public NETTRAM service table, expanding its row-spanning profiles."""
    def __init__(self):
        super().__init__()
        self.table = None
        self.row = None
        self.cell = None
        self.tables = []

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.table = []
        elif self.table is not None:
            if tag == 'tr':
                self.row = []
            elif tag in {'td', 'th'} and self.row is not None:
                self.cell = []
                self.span = int(dict(attrs).get('rowspan', '1'))
            elif tag == 'br' and self.cell is not None:
                self.cell.append(' ')

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {'td', 'th'} and self.cell is not None:
            self.row.append((' '.join(''.join(self.cell).split()), self.span))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table)
            self.table = None


DAYS = {'Monday to Friday': 31, 'Saturdays': 32, 'Sundays': 64, '': 0}
HOLIDAY_TITLES = {'new year’s day': 'NewYearsDay', 'christmas day': 'ChristmasDay', 'boxing day': 'BoxingDay',
                  'early may bank holiday': 'MayDay', 'spring bank holiday': 'SpringBank',
                  'summer bank holiday': 'LateSummerBankHolidayNotScotland', 'easter monday': 'EasterMonday',
                  'good friday': 'GoodFriday'}
HOLIDAY_RULES = set(HOLIDAY_TITLES.values()) | {'AllBankHolidays', 'HolidayMondays', 'DisplacementHolidays'}
MONDAY_HOLIDAYS = {'MayDay', 'SpringBank', 'LateSummerBankHolidayNotScotland', 'EasterMonday'}


def rules(text, date_rule=False):
    items = []
    tokens = iter(text.split())
    for token in tokens:
        include = token != 'not'
        name = token if include else next(tokens)
        if date_rule:
            name = date.fromisoformat(name)
        elif name not in HOLIDAY_RULES:
            raise ValueError(f'Unknown NETTRAM holiday rule {name}')
        items.append((include, name))
    return tuple(items)


def profiles(document):
    parser = JourneyTable()
    parser.feed(document)
    result = {}
    for table in parser.tables:
        if not table or [text for text, _ in table[0]][2:9] != ['Code', 'Direction', 'Start', 'End', 'Dates', 'Days', 'Bank Holidays']:
            continue
        spans = {}
        for original in table[1:]:
            cells = iter(original)
            row = []
            for column in range(len(table[0])):
                if column in spans:
                    value, left = spans.pop(column)
                else:
                    value, left = next(cells)
                if left > 1:
                    spans[column] = (value, left - 1)
                row.append(value)
            ref, departure, period, days, holidays, exceptions = [row[n] for n in (2, 4, 6, 7, 8, 9)]
            start, end = (datetime.strptime(s.strip(), '%d %b %Y').date() for s in re.split('[–—]', period))
            if ref in result:
                raise ValueError(f'Duplicate NETTRAM journey ref {ref}')
            result[ref] = (departure, (start, end, DAYS[days], rules(holidays), rules(exceptions, True)))
    if not result:
        raise ValueError('NETTRAM operating profiles unavailable')
    return result


def holiday_days(file):
    events = json.loads(file.read_bytes())['england-and-wales']['events']
    # Actual Christmas, Boxing Day and New Year's Day still affect weekend
    # services on their calendar dates when GOV.UK lists a substitute weekday.
    days = {}
    for event in events:
        day = date.fromisoformat(event['date'])
        kind = HOLIDAY_TITLES.get(event['title'].lower())
        tags = {'AllBankHolidays'}
        if kind:
            tags.add(kind)
        if kind in MONDAY_HOLIDAYS:
            tags.add('HolidayMondays')
        if 'substitute' in event['notes'].lower():
            tags.add('DisplacementHolidays')
            actual = {'ChristmasDay': (12, 25), 'BoxingDay': (12, 26), 'NewYearsDay': (1, 1)}.get(kind)
            if actual:
                days[date(day.year, *actual)] = {'AllBankHolidays', kind}
        days[day] = tags
    return days


def date_key(day):
    return day.year * 10000 + day.month * 100 + day.day


def reconcile(trips, calendars, document, holiday_file):
    board = profiles(document.read_text(encoding='utf-8'))
    holidays = holiday_days(holiday_file)
    result = {}
    selected = {}

    @lru_cache(maxsize=None)
    def calendar(profile, feed_start, feed_end):
        start, end, weekdays, bank_rules, day_rules = profile
        as_date = lambda s: datetime.strptime(str(s), '%Y%m%d').date()
        start, end = max(start, as_date(feed_start)), min(end, as_date(feed_end))
        if end < start:
            return None
        if min(holidays) > start or max(holidays) < end:
            raise ValueError('GOV.UK holiday coverage ends before this NETTRAM schedule')
        cal = service(weekdays, date_key(start), date_key(end))
        active_days = 0
        for offset in range((end-start).days+1):
            day = start + timedelta(days=offset)
            base = bool(weekdays & (1 << day.weekday()))
            active = base
            tags = holidays.get(day, set())
            included = any(include and name in tags for include, name in bank_rules)
            excluded = any(not include and name in tags for include, name in bank_rules)
            # A date-specific displacement rule schedules holiday service on
            # the observed weekday even when the actual Boxing Day is excluded.
            allow_substitute = 'DisplacementHolidays' in tags and (True, 'DisplacementHolidays') in bank_rules
            if included:
                active = True
            if excluded and not allow_substitute:
                active = False
            for include, special_day in day_rules:
                if special_day == day:
                    active = include
            if active != base:
                cal['added' if active else 'removed'].append(date_key(day))
            active_days += active
        if active_days == 0:
            return None
        key = f'net-{len(result)+1}'
        result[key] = cal
        return key

    for trip_id, trip in sorted(trips.items()):
        ref = trip['txc_vehicle_journey_ref']
        departure, profile = board[ref]
        token = trip['realtime_trip_id'].split(':')[-1]
        if not re.fullmatch(r'\d{4}', token) or f'{int(token[:2]) % 24:02}:{token[2:]}' != departure:
            raise ValueError(f'GTFS and NETTRAM source disagree about the start of journey {ref}')
        c = calendars[trip['service_id']]
        if c['days'] != profile[2]:
            raise ValueError(f'GTFS and NETTRAM disagree about the weekday profile of journey {ref}')
        if not c['start'] and not c['added']:
            continue
        start = c['start'] or min(c['added'])
        end = c['end'] or max(c['added'])
        key = calendar(profile, start, end)
        if key:
            selected[trip_id] = key
    return selected, result
