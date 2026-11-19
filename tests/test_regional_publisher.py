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
            "patterns": [{"line": "R", "stops": ["A", "B"], "timings": [
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
        self.data["calendars"]["s"].update(days=0, end=99991231, added=[20261202])
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

    def test_no_service_beyond_upstream_calendar(self):
        self.data["validThrough"] = 20300101
        with self.assertRaises(ValueError):
            metadata("metro", json.dumps(self.data), self.rules["metro"])
