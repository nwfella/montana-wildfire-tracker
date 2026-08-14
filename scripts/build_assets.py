#!/usr/bin/env python3
"""Build Montana map assets (run once, results are cached in assets/).

1. Fetch Census 2020 Montana counties from the keyless ArcGIS FeatureService
   (https://services.arcgis.com/iTQUx5ZpNUh47Geb/.../CENSUS_2020_PL94171_MONTANA_COUNTY).
2. Simplify each county (Douglas-Peucker) -> assets/counties.json
3. Derive the state outline from the RAW rings via edge-union -> assets/montana_outline.json

Usage:  python scripts/build_assets.py
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from geo import build_counties, derive_outline, rings_from_geom  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
COUNTIES_URL = ("https://services.arcgis.com/iTQUx5ZpNUh47Geb/arcgis/rest/services/"
                "CENSUS_2020_PL94171_MONTANA_COUNTY/FeatureServer/0/query"
                "?where=1%3D1&outFields=NAME,GEOID&f=geojson&resultRecordCount=2000&outSR=4326")


def fetch(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def main():
    print("== Montana map assets ==")
    raw = json.loads(fetch(COUNTIES_URL).decode("utf-8", "replace"))
    feats = raw.get("features", [])
    print(f"  census features: {len(feats)}")
    if len(feats) < 50:
        print("FATAL: expected ~56 Montana counties")
        sys.exit(1)

    os.makedirs(os.path.join(ROOT, "scratch"), exist_ok=True)
    with open(os.path.join(ROOT, "scratch", "mt_counties_raw.geojson"), "w", encoding="utf-8") as f:
        json.dump(raw, f, separators=(",", ":"))

    # outline from RAW rings (topologically exact shared edges)
    all_rings = []
    for feat in feats:
        all_rings.extend(rings_from_geom(feat.get("geometry")))
    print(f"  raw rings: {len(all_rings)}, raw pts: {sum(len(r) for r in all_rings)}")
    outline = derive_outline(all_rings)
    with open(os.path.join(ROOT, "assets", "montana_outline.json"), "w", encoding="utf-8") as f:
        json.dump({"rings": outline}, f, separators=(",", ":"))
    kb = os.path.getsize(os.path.join(ROOT, "assets", "montana_outline.json")) / 1024
    # verify: closed + bbox
    for r in outline:
        xs = [p[0] for p in r]
        ys = [p[1] for p in r]
        print(f"  outline ring: pts={len(r)} closed={r[0] == r[-1]} "
              f"bbox=({min(xs):.2f},{min(ys):.2f})-({max(xs):.2f},{max(ys):.2f})")
    print(f"  outline file: {kb:.0f} KB")

    # simplified counties
    raw_path = os.path.join(ROOT, "scratch", "mt_counties_raw.geojson")
    build_counties(raw_path, os.path.join(ROOT, "assets", "counties.json"))
    print("OK")


if __name__ == "__main__":
    main()
