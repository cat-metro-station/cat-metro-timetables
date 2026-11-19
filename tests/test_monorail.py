from datetime import date
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import refresh_us_monorail as rail


def page():
    # Reduced fixture of the actual 18 Nov service facts, excluding the map art.
    return f"""
        <a href="{rail.MAP}">Map</a>
        {''.join(f'<span class="sc-tl-label">{n} Minute Ride</span>' for n in [2, 1, 1, 4, 2, 1])}
        <p>Monorail trains arrive continuously every four to eight minutes.</p>
        <p>The Las Vegas Monorail operates daily beginning at 7 am until these times:</p>
        <ul><li>Monday: Midnight</li><li>Tuesday to Thursday: 2 am</li><li>Friday to Sunday: 3 am</li></ul>
        <div class="kadence-conversion-wrap"><h3>System Notice – Early Closure</h3>
          <ul><li>November 29 – December 2, 2026: 7am – Midnight</li><li>December 3, 2026: 7a m – 1am</li></ul>
        </div>
        <div class="kadence-conversion-wrap"><h3>System Notice – Extended Hours For F1 Las Vegas</h3>
          <p>The Monorail will be operational nonstop from <mark>Tuesday, November 17 at 7am – Monday, November 23 at 3am.</mark></p>
        </div>"""


class MonorailTests(unittest.TestCase):
    def test_special_dates_and_overnight_runs(self):
        p = rail.parse(page(), date(2026, 11, 18))
        self.assertEqual(p['validThrough'], 20261230)
        self.assertFalse(p['allowsReferenceWeek'])
        self.assertEqual(p['patterns'][0]['headwayMinutes'], [4, 8])
        self.assertEqual(p['patterns'][0]['timings'][0]['offsets'], [0, 120, 180, 240, 480, 600, 660])
        by_day = {d: k for k, cal in p['calendars'].items() for d in cal['added']}
        self.assertEqual(by_day[20261117], '420-1860-through')
        self.assertEqual(by_day[20261121], '420-1860-through')
        self.assertEqual(by_day[20261122], '420-1620-close')
        self.assertEqual(by_day[20261123], '420-1440-close')
        self.assertEqual(by_day[20261203], '420-1500-close')
        for direction in p['patterns']:
            self.assertEqual(direction['approximate'], list(range(7)))
            by_key = {t['service']: t for t in direction['timings']}
            self.assertEqual(by_key['420-1860-through']['starts'][-1], 31*3600-8*60)
            last = by_key['420-1500-close']
            self.assertLessEqual(last['starts'][-1]+last['offsets'][-1], 25*3600)

    def test_year_rollover_and_unknown_notices(self):
        p = rail.parse(page(), date(2026, 12, 30))
        dates = [d for cal in p['calendars'].values() for d in cal['added']]
        self.assertNotIn(20261231, dates)
        self.assertIn(20261229, dates)
        self.assertIn(20270101, dates)
        self.assertEqual(max(dates), 20270210)
        self.assertEqual(rail.closure('December 30 – January 1, 2027: 7am – Midnight')[:2],
                         (date(2026, 12, 30), date(2027, 1, 1)))
        with self.assertRaisesRegex(ValueError, 'Unrecognised Las Vegas Monorail service notice'):
            rail.parse(page()+'<div class="kadence-conversion-wrap"><h3>New timetable announcement</h3></div>', date(2026, 11, 18))
        with self.assertRaisesRegex(ValueError, 'Unrecognised dated'):
            rail.parse(page().replace('December 3, 2026: 7a m – 1am','Some stations close earlier'), date(2026, 11, 18))

    def test_map_change_requires_a_valid_directional_reference(self):
        changed = page().replace(rail.MAP, 'https://www.lvmonorail.com/new-map.webp')
        with self.assertRaises(ValueError):
            rail.parse(changed, date(2026, 11, 18))
        for name, stations in [('Northbound', rail.STATION_NAMES), ('Southbound', rail.STATION_NAMES[::-1])]:
            changed += f'<p>{name} travel times between stations are:</p>'
            for a, b in zip(stations, stations[1:]):
                changed += f'<li>{a}: Two minutes to {b}.</li>'
        for pattern in rail.parse(changed, date(2026, 11, 18))['patterns']:
            self.assertEqual(pattern['timings'][0]['offsets'], [0, 120, 240, 360, 480, 600, 720])
