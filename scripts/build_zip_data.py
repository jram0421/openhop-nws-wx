"""Generate src/stormhop/zipdata.json from the Census ZCTA gazetteer (public domain).

Usage: build_zip_data.py 2020_Gaz_zcta_national.txt src/stormhop/zipdata.json
  Source: https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2020_Gazetteer/2020_Gaz_zcta_national.zip
"""

import csv
import json
import sys


def main(src: str, out: str) -> None:
    zips = {}
    with open(src, encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = [h.strip() for h in next(reader)]
        gi, la, lo = header.index("GEOID"), header.index("INTPTLAT"), header.index("INTPTLONG")
        for row in reader:
            zips[row[gi].strip()] = [round(float(row[la]), 3), round(float(row[lo]), 3)]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(zips.items())), f, separators=(",", ":"))


if __name__ == "__main__":
    main(*sys.argv[1:3])
