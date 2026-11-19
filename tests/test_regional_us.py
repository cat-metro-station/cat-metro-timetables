import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import refresh_regional_us as us


DATE = datetime.date(2026, 11, 19)
FEED = {"id": "123", "provider": "Test operator", "url": "https://example.test/rail.zip",
        "ntdLicense": "Public Domain", "ntdID": "1"}
STOPS = [["123-A", "Station A", 10, 20], ["123-B", "Station B", 10.1, 20]]
CATALOG = {"feeds": [FEED], "lines": [["123-1", "L", "Line"]], "stops": STOPS}


def make_feed(start, end, generated=(2026, 11, 19), info=None):
    tables = {
        "routes": "route_id,route_short_name,route_long_name,route_type\n1,L,Line,1\n",
        "calendar": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                     f"S,1,1,1,1,1,1,1,{start},{end}\n"),
        "trips": "route_id,service_id,trip_id\n1,S,T\n",
        "stops": "stop_id,stop_name,stop_lat,stop_lon\nA,Station A,10,20\nB,Station B,10.1,20\n",
        "stop_times": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T,23:59:00,23:59:00,A,1\nT,24:13:00,24:15:00,B,2\n"),
    }
    if info:
        tables["feed_info"] = "feed_start_date,feed_end_date\n" + info + "\n"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, rows in tables.items():
            archive.writestr(zipfile.ZipInfo(name + ".txt", (*generated, 0, 0, 0)), rows)
    return data.getvalue()


class USRefreshTests(unittest.TestCase):
    def test_expired_valid_zip_uses_live_mirror_and_never_replaces_good_cache_on_failure(self):
        expired = make_feed(20261001, 20261101)
        current = make_feed(20261101, 20261201)
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            def respond(args, check):
                Path(args[args.index('--output') + 1]).write_bytes(
                    current if args[-1].endswith('/latest.zip') else expired)
            with mock.patch.object(us.subprocess, "run", side_effect=respond) as requests:
                path = us.fetch_feed(FEED, cache, False, 20261119)
                self.assertEqual(requests.call_count, 2)
            self.assertEqual(path.read_bytes(), current)
            def stale(args, check):
                Path(args[args.index('--output') + 1]).write_bytes(expired)
            with mock.patch.object(us.subprocess, "run", side_effect=stale):
                with self.assertRaisesRegex(ValueError, "expired"):
                    us.fetch_feed(FEED, cache, False, 20261119)
            self.assertEqual(path.read_bytes(), current)

    def test_effective_city_window_and_arrivals_are_preserved_across_midnight(self):
        raw = make_feed(20261101, 20270102, info="20261117,20261127")
        data = dict(CATALOG, feeds=[dict(FEED, mode="timetableOnly")])
        package = us.generate("test", data, DATE, lambda *_: io.BytesIO(raw))
        self.assertEqual(package["validThrough"], 20261127)
        self.assertEqual(package["attribution"]["feeds"][0]["serviceThrough"], 20270102)
        self.assertEqual(package["calendars"]["123-S"]["end"], 20261127)
        trip = package["patterns"][0]["timings"][0]
        self.assertEqual(trip["arrivalOffsets"], [0, 14 * 60])
        self.assertEqual(trip["offsets"], [0, 16 * 60])

    def test_repackaged_unchanged_data_keeps_source_date_and_future_service_waits(self):
        initial = make_feed(20261101, 20261201, (2026, 11, 15))
        later = make_feed(20261101, 20261201)
        previous = us.generate("test", CATALOG, DATE, lambda *_: io.BytesIO(initial))
        package = us.generate("test", CATALOG, DATE, lambda *_: io.BytesIO(later),
                              previous=json.loads(json.dumps(previous)))
        self.assertEqual(package, previous)
        future = make_feed(20261123, 20261201)
        with self.assertRaisesRegex(ValueError, "Future service only"):
            us.generate("test", CATALOG, DATE, lambda *_: io.BytesIO(future), previous)

    def test_release_catalog_uses_public_feed_terms_and_has_all_us_cities(self):
        catalog = json.loads((us.ROOT / "inputs/us-regional-rail.json").read_text())
        uk = json.loads((us.ROOT / "inputs/regional-uk.json").read_text())
        all_cities = json.loads((us.ROOT / "inputs/regional-lines.json").read_text())
        self.assertEqual(set(catalog) | {"lasVegas"}, set(all_cities) - set(uk))
        for row in catalog.values():
            for feed in row["feeds"]:
                self.assertEqual(feed["ntdLicense"], "Public Domain")
                self.assertTrue(feed["ntdID"] and feed["ntdWeblink"].startswith("https://"))
