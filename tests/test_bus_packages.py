import contextlib
import csv
import datetime as dt
import gzip
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from build_bus_packs import build

class BusPackageTests(unittest.TestCase):
    def test_real_service_dates_separate_directional_poles_after_midnight_and_frequency(self):
        today=dt.date(2026,11,19)
        feed={
            'stops':[['stop_id','stop_name','stop_lat','stop_lon'],['E','Main east side','34','-118.002'],['X','Market','34','-117.999'],
                     ['W','Main west side','34.0002','-118.002'],['Y','Elm','34.0002','-118.004'],['OTHER','Remote','35','-110']],
            'routes':[['route_id','route_short_name','route_type'],['B','101','3'],['F','X1','3'],['U','R','1'],['O','remote','3']],
            'calendar':[['service_id','monday','tuesday','wednesday','thursday','friday','saturday','sunday','start_date','end_date'],
                        ['week','0','0','0','1','0','0','0','20261119','20261126']],
            'calendar_dates':[['service_id','date','exception_type'],['week','20261126','2'],['week','20261120','1']],
            'trips':[['route_id','service_id','trip_id','trip_headsign'],['B','week','T1','Market'],['B','week','T2','Elm'],
                     ['F','week','T3','Market'],['U','week','T4','Subway'],['O','week','T5','Remote']],
            'stop_times':[['trip_id','stop_id','stop_sequence','departure_time','pickup_type'],
                          ['T1','E','1','25:05:00','0'],['T1','X','2','25:10:00','1'],['T2','W','1','10:00:00','0'],['T2','Y','2','10:03:00','1'],
                          ['T3','E','1','08:00:00','0'],['T3','X','2','08:05:00','1'],['T4','W','1','12:00:00','0'],['T5','OTHER','1','12:00:00','0']],
            'frequencies':[['trip_id','start_time','end_time','headway_secs','exact_times'],['T3','09:00:00','09:30:00','600','0']]}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with zipfile.ZipFile(root/'licensed.zip','w') as z:
                for name,rows in feed.items():
                    out=io.StringIO(newline='')
                    csv.writer(out).writerows(rows)
                    z.writestr(name+'.txt',out.getvalue())
            source={'id':'licensed','operator':'Local Transit','source':'https://agency.example/open','license':'https://agency.example/license',
                    'timeZone':'America/Los_Angeles','bounds':[33.99,-118.005,34.01,-118.001]}
            with contextlib.redirect_stdout(io.StringIO()):
                build('losAngeles',[source],root,root,today)
            e=json.loads((root/'losAngeles.entry.json').read_text())
            self.assertEqual((e['stops'],e['routes'],e['validThrough']),(4,2,'20261120'))
            dbfile=root/'test.sqlite'
            dbfile.write_bytes(gzip.decompress((root/Path(e['path']).name).read_bytes()))
            db=sqlite3.connect(dbfile)
            try:
                self.assertEqual(db.execute('SELECT DISTINCT day FROM service_days ORDER BY day').fetchall(),[(20261119,),(20261120,)])
                self.assertEqual(db.execute('SELECT name,ROUND(bearing) FROM stops WHERE name LIKE "Main%" ORDER BY name').fetchall(),
                                 [('Main east side',90.),('Main west side',270.)])
                self.assertEqual(db.execute('SELECT seconds,approximate FROM calls WHERE stop_id=(SELECT id FROM stops WHERE name="Main east side") ORDER BY seconds').fetchall(),
                                 [(32400,1),(33000,1),(33600,1),(90300,0)])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM calls WHERE stop_id=(SELECT id FROM stops WHERE name="Market")').fetchone()[0],0)
            finally:
                db.close()

if __name__=='__main__':
    unittest.main()
