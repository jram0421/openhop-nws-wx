"""Generate ui/data.js from the Census county list and the NWS alert type list.

Usage: build_ui_data.py counties.txt types.json ui/data.js
  counties.txt: https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt
  types.json:   https://api.weather.gov/alerts/types
"""

import csv
import json
import sys

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "AS": "American Samoa", "GU": "Guam",
    "MP": "Northern Mariana Islands", "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
}


def main(counties_path: str, types_path: str, out_path: str) -> None:
    counties: dict[str, list[list[str]]] = {}
    with open(counties_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="|"):
            if row["STATE"] in STATES:
                counties.setdefault(row["STATE"], []).append(["0" + row["STATEFP"] + row["COUNTYFP"], row["COUNTYNAME"]])
    for rows in counties.values():
        rows.sort(key=lambda r: r[1])
    with open(types_path, encoding="utf-8") as f:
        events = sorted(json.load(f)["eventTypes"])
    data = {"states": STATES, "counties": counties, "events": events}
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("window.NWS_DATA = " + json.dumps(data, separators=(",", ":")) + ";\n")


if __name__ == "__main__":
    main(*sys.argv[1:4])
