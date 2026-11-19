from datetime import date
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from publish_regional import metadata, publish


class RegionalPublisherTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.out = self.base / "packages"
        self.rules = {"metro": {"R": ["A", "B"]}}
        self.data = {
            "version": 1, "city": "metro", "sourceDate": "2026-11-10",
            "attribution": {"source": "Test operator", "url": "https://example.com"},
            "calendars": {"s": {"days": 127, "start": 20261101, "end": 20261201, "added": [], "removed": []}},
            "patterns": [{"line": "R", "stops": ["A", "B"], "approximate": [],
                          "noPickup": [], "noDropOff": [], "timings": [
                {"service": "s", "starts": [3600], "offsets": [0, 300]}]}]
        }
        self.assertTrue(self.save())

    def save(self):
        (self.base / "offline-metro.json").write_text(json.dumps(self.data))
        return publish(self.base, self.out, self.rules, date(2026, 11, 18))

    def entry(self):
        return json.loads((self.out / "regional-index.json").read_bytes())["cities"]["metro"]

    def test_exception_only_calendar_and_immutable_versions(self):
        before = self.entry()
        self.data["calendars"]["s"].update(days=0, end=99991231, added=[20261118, 20261202])
        self.assertTrue(self.save())
        self.assertEqual(self.entry()["validThrough"], 20261202)
        self.assertNotEqual(self.entry()["sha256"], before["sha256"])
        self.assertTrue((self.out / "regional" / (before["sha256"] + ".json")).is_file())

    def test_failed_or_older_snapshot_leaves_index_and_object_intact(self):
        before = self.entry()
        self.data["patterns"][0]["stops"][1] = "unknown"
        self.assertFalse(self.save())
        self.data["patterns"][0]["stops"][1] = "B"
        self.data["sourceDate"] = "2026-11-09"
        self.data["patterns"][0]["timings"][0]["starts"] = [7200]
        self.assertFalse(self.save())
        self.data["sourceDate"] = "2026-11-11"
        self.data["calendars"]["s"]["end"] = 20261129
        self.assertFalse(self.save())
        self.data["calendars"]["s"]["end"] = 20261110
        self.assertFalse(self.save())
        self.assertEqual(self.entry(), before)
        self.assertEqual(len(list((self.out / "regional").glob("*.json"))), 1)

    def test_valid_city_can_publish_when_another_fails(self):
        self.rules["local"] = self.rules["metro"]
        record = dict(self.data, city="local")
        other = self.base / "offline-local.json"
        other.write_text(json.dumps(record))
        self.assertTrue(self.save())
        previous = self.entry()
        other.write_text("{")
        self.data["sourceDate"] = "2026-11-11"
        self.assertFalse(self.save())
        self.assertNotEqual(self.entry(), previous)

    def test_rebuild_timestamp_alone_does_not_trigger_download(self):
        original = self.entry()
        self.data["generatedOn"] = "2026-11-18"
        self.assertTrue(self.save())
        self.assertEqual(self.entry(), original)

    def test_older_observation_of_identical_feed_keeps_newer_object(self):
        feed = {"url": "https://example.com/feed", "serviceThrough": 20261201, "archiveDate": "2026-11-10"}
        self.data["attribution"]["feeds"] = [feed]
        self.data["generatedOn"] = "2026-11-18"
        self.assertTrue(self.save())
        original = self.entry()
        objects = set((self.out / "regional").iterdir())

        self.data["sourceDate"] = feed["archiveDate"] = "2026-11-09"
        self.data["generatedOn"] = "2026-11-17"
        self.assertTrue(self.save())
        self.assertEqual(self.entry(), original)
        self.assertEqual(set((self.out / "regional").iterdir()), objects)
        for key, changed in [("url", "https://example.com/other-feed"), ("serviceThrough", 20261130),
                             ("notice", "Different attribution")]:
            with self.subTest(key=key):
                self.data["attribution"]["feeds"] = [dict(feed, **{key: changed})]
                self.assertFalse(self.save())
                self.assertEqual(self.entry(), original)

    def test_calendar_start_substitutes_for_missing_zip_timestamp_only_with_matching_service_date(self):
        feed = {"url": "https://example.com/feed", "archiveDate": self.data["sourceDate"]}
        self.data["attribution"]["feeds"] = [feed]
        self.assertTrue(self.save())
        current = self.entry()
        del feed["archiveDate"]
        self.data["sourceDate"] = feed["calendarStartDate"] = "2026-11-01"
        self.assertTrue(self.save())
        self.assertEqual(self.entry(), current)
        self.data["sourceDate"] = feed["calendarStartDate"] = "2026-11-02"
        self.assertFalse(self.save())
        self.data["calendars"]["s"]["start"] = 20261102
        self.assertFalse(self.save())
        self.assertEqual(self.entry(), current)

    def test_app_decoder_timing_and_single_stop_contract(self):
        original = self.entry()
        pattern = self.data["patterns"][0]
        timing = pattern["timings"][0]
        for starts in ([3600, 3500], [172800], []):
            with self.subTest(starts=starts):
                timing["starts"] = starts
                self.assertFalse(self.save())
        timing["starts"] = [3600]
        timing["offsets"] = [0, 86400]
        self.assertFalse(self.save())
        timing["offsets"] = [0, 300]
        del pattern["noPickup"]
        self.assertFalse(self.save())
        pattern["noPickup"] = [0]
        self.assertFalse(self.save())
        self.assertEqual(self.entry(), original)
        pattern.update(noPickup=[], stops=["A"])
        timing["offsets"] = [0]
        self.assertFalse(self.save())
        pattern.update(continuesBeyondMap=True, destinationName="Outside mapped area")
        self.assertTrue(self.save())

    def test_one_operator_cannot_regress_behind_another_operators_later_end(self):
        feeds = [{"url": "https://example.com/qline", "serviceThrough": 20261121},
                 {"url": "https://example.com/people-mover", "serviceThrough": 20261201}]
        self.data["attribution"]["feeds"] = feeds
        self.assertTrue(self.save())
        original = self.entry()
        feeds[0]["serviceThrough"] = 20261120
        self.assertFalse(self.save())
        self.assertEqual(self.entry(), original)
        feeds[0]["serviceThrough"] = 20261130
        self.assertTrue(self.save())
        self.assertNotEqual(self.entry(), original)

    def test_future_only_or_unusable_replacement_preserves_working_timetable(self):
        before = self.entry()
        calendar = self.data["calendars"]["s"]
        calendar.update(start=20261119)
        self.assertFalse(self.save())
        calendar.update(days=0, start=0, end=0, added=[20261201], removed=[20261201])
        self.assertFalse(self.save())
        self.assertEqual(self.entry(), before)

    def test_no_service_beyond_upstream_calendar(self):
        self.data["validThrough"] = 20300101
        with self.assertRaises(ValueError):
            metadata("metro", json.dumps(self.data), self.rules["metro"])
