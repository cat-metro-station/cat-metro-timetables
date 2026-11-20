import csv
import datetime as dt
import gzip
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
if importlib.util.find_spec("duckdb"):
    import build_british_bus_packs as bus
else:
    bus = None


@unittest.skipIf(bus is None, "Install duckdb==1.4.3 for British GTFS checks")
class BritishBusPacks(unittest.TestCase):
    def test_complete_routes_within_city_calendar_exceptions_and_headways(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            path = work / "fixture.zip"
            with zipfile.ZipFile(path, "w") as z:

                def table(name, columns, rows):
                    text = io.StringIO(newline="")
                    writer = csv.writer(text)
                    writer.writerow(columns.split())
                    writer.writerows(rows)
                    z.writestr(name + ".txt", text.getvalue())

                table("agency", "agency_id agency_name", [("o", "City Bus")])
                table(
                    "stops",
                    "stop_id stop_name stop_lat stop_lon",
                    [
                        ("a", "Market east", 51, -1),
                        ("b", "Market west", 51, -1.001),
                        ("c", "Terminus", 51.8, -1),
                        ("d", "Other", 52, -1),
                    ],
                )
                table(
                    "routes",
                    "route_id agency_id route_type route_short_name route_long_name",
                    [
                        ("r", "o", 3, "A", "Market service"),
                        ("coach", "o", 200, "X", "Airport"),
                        ("rail", "o", 2, "T", "Rail"),
                        ("away", "o", 3, "B", "Outside"),
                    ],
                )
                table(
                    "trips",
                    "trip_id route_id service_id trip_headsign direction_id",
                    [
                        ("actual", "r", "S", "Town", 0),
                        ("freq", "coach", "S", "Airport", 1),
                        ("other", "away", "S", "Other", 0),
                        ("train", "rail", "S", "Rail", 0),
                    ],
                )
                table(
                    "stop_times",
                    "trip_id stop_id stop_sequence departure_time arrival_time pickup_type stop_headsign timepoint",
                    [
                        ("actual", "a", 1, "23:50:00", "23:50:00", 0, "Via bridge", 1),
                        ("actual", "c", 2, "25:15:00", "25:15:00", 0, "", 1),
                        ("freq", "b", 1, "00:00:00", "00:00:00", 0, "", 1),
                        ("freq", "c", 2, "00:05:00", "00:05:00", 0, "", 1),
                        ("other", "d", 1, "06:00:00", "06:00:00", 0, "", 1),
                        ("other", "a", 2, "06:15:00", "06:15:00", 1, "", 1),
                        ("train", "a", 1, "07:00:00", "07:00:00", 0, "", 1),
                    ],
                )
                table(
                    "calendar",
                    "service_id monday tuesday wednesday thursday friday saturday sunday start_date end_date",
                    [("S", 1, 1, 1, 1, 1, 0, 0, 20261116, 20261122)],
                )
                table("calendar_dates", "service_id date exception_type", [("S", 20261120, 2), ("S", 20261122, 1)])
                table(
                    "frequencies",
                    "trip_id start_time end_time headway_secs exact_times",
                    [("freq", "08:00:00", "08:21:00", 600, 0)],
                )
            feed = {
                "id": "fixture",
                "source": "https://example.invalid/source",
                "license": "https://example.invalid/licence",
                "operator": "City Bus",
            }
            with zipfile.ZipFile(path) as archive, bus.duckdb.connect() as cx:
                bus.regional_tables(
                    cx, archive, work, dt.date(2026, 11, 19), {"sample": [50.99, -1.005, 51.01, -0.99]}, None
                )
                bus.compress_group(cx, ["sample"])
                bus.build_city(cx, "sample", feed, work, work)
            entry = json.loads((work / "sample.entry.json").read_text())
            self.assertEqual((entry["routes"], entry["stops"]), (2, 3))
            out = work / "read.sqlite"
            with gzip.open(work / Path(entry["path"]).name, "rb") as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            with sqlite3.connect(out) as db:
                self.assertEqual(
                    db.execute("SELECT seconds,headsign FROM calls WHERE route_id='r' AND stop_id='c'").fetchone(),
                    (90900, "Town"),
                )
                self.assertEqual(
                    db.execute("SELECT headsign FROM calls WHERE route_id='r' AND stop_id='a'").fetchone(),
                    ("Via bridge",),
                )
                self.assertEqual(
                    db.execute(
                        "SELECT seconds,approximate FROM calls WHERE route_id='coach' AND stop_id='c' ORDER BY seconds"
                    ).fetchall(),
                    [(8 * 3600 + 300, 1), (8 * 3600 + 900, 1), (8 * 3600 + 1500, 1)],
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM service_days WHERE day=20261120").fetchone(), (0,)
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM service_days WHERE day=20261122").fetchone(), (1,)
                )
                self.assertEqual(
                    db.execute("SELECT detail FROM routes WHERE id='coach'").fetchone(), ("City Bus · Airport",)
                )
