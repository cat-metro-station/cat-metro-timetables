"""Check every regional package against its index, city map and checksum."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from urllib.request import urlopen

from publish_regional import ROOT, metadata, require


def verify(base=None):
    def read(name):
        if base:
            with urlopen(base.rstrip("/") + "/" + name, timeout=40) as response:
                return response.read()
        return (ROOT / "packages" / name).read_bytes()

    contracts = json.loads((ROOT / "inputs/regional-lines.json").read_bytes())
    index = json.loads(read("regional-index.json"))
    require(index["version"] == 1 and len(contracts) == 78 and set(index["cities"]) == set(contracts),
            "index must match all 78 mapped regional cities")

    def check(city):
        entry = index["cities"][city]
        data = read("regional/" + entry["sha256"] + ".json")
        require(metadata(city, data, contracts[city]) == entry, city + ": manifest mismatch")
        return len(data)

    with ThreadPoolExecutor(max_workers=6) as pool:
        size = sum(pool.map(check, contracts))
    print(f"Verified {len(contracts)} city timetables ({size:,} bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="verify HTTP assets in place of local packages")
    verify(parser.parse_args().base_url)
