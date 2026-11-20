"""Publish reviewed Japanese subway, tram and monorail timetable revisions."""
import argparse
from datetime import date
import json
from pathlib import Path
from publish_regional import ROOT, publish


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--packages", type=Path, default=ROOT / "packages")
    args = parser.parse_args()
    stations = json.loads((ROOT / "inputs/japan-rail-stations.json").read_bytes())
    lines = {city: {line["symbol"]: line["stops"] for line in info["lines"]} for city, info in stations.items()}
    ok = publish(args.candidates, args.packages, lines, date.today(), collection="japan-regional")
    raise SystemExit(0 if ok else 1)
