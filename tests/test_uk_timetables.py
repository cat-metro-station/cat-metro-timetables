import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from regional_uk_net_calendar import reconcile


class NottinghamCalendarTests(unittest.TestCase):
    def test_separate_bank_holiday_and_sunday_only_journeys(self):
        header = ['', 'Block', 'Code', 'Direction', 'Start', 'End', 'Dates', 'Days', 'Bank Holidays', 'Dates', 'Garage']
        html = '<table><tr>' + ''.join(f'<th>{title}</th>' for title in header) + '</tr>'
        for ref, rules in [('1', 'DisplacementHolidays<br>not BoxingDay'), ('2', 'not AllBankHolidays')]:
            row = ['', '', ref, 'out', '10:00', '10:15', '1 Dec 2026–31 Dec 2026', 'Sundays', rules, '', '']
            html += '<tr>' + ''.join(f'<td>{value}</td>' for value in row) + '</tr>'
        html += '</table>'
        trips = {k: dict(txc_vehicle_journey_ref=k, realtime_trip_id='NET:1000', service_id='s') for k in ['1', '2']}
        # Both GTFS trips have one service_id. Only the first runs on the
        # substitute Boxing Day bank holiday according to its original profile.
        calendars = {'s': dict(days=64, start=20261201, end=20261231, added=[], removed=[])}
        events = [
            ('2026-08-31', 'Summer bank holiday', ''), ('2026-12-28', 'Boxing Day', 'Substitute day'),
            ('2027-01-01', 'New Year’s Day', ''),
        ]
        with TemporaryDirectory() as tmp:
            profile_file, holidays = Path(tmp) / 'net.html', Path(tmp) / 'days.json'
            profile_file.write_text(html)
            holidays.write_text(json.dumps({'england-and-wales': {'events': [
                dict(date=d, title=t, notes=n) for d, t, n in events]}}))
            services, normalized = reconcile(trips, calendars, profile_file, holidays)
            self.assertEqual(normalized[services['1']]['added'], [20261228])
            self.assertEqual(normalized[services['2']]['added'], [])
            profile_file.write_text(html.replace('DisplacementHolidays', 'UnrecognisedHoliday'))
            with self.assertRaisesRegex(ValueError, 'Unknown NETTRAM'):
                reconcile(trips, calendars, profile_file, holidays)
            profile_file.write_text(html.replace('10:00', '10:05'))
            with self.assertRaisesRegex(ValueError, 'disagree about the start'):
                reconcile(trips, calendars, profile_file, holidays)
