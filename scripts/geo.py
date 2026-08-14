"""Geometry utilities: Douglas-Peucker simplification + Montana asset builder.

Counties are built once (Census 2020 PL94-171 Montana county service) and cached
to assets/counties.json. The state outline is derived from the SAME raw county
rings via edge-union (segments seen exactly once = boundary), then simplified —
no shapely needed. Perimeter polygons are simplified at every bake inside
collect.py using dp_simplify().
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def dp_simplify(ring, eps):
    """Iterative Douglas-Peucker on a closed ring [(lon, lat), ...].
    eps in degrees. Returns simplified ring (not closed)."""
    if len(ring) < 4:
        return list(ring)
    # treat ring as open polyline for simplification (drop closing point)
    pts = ring[:-1] if ring[0] == ring[-1] else list(ring)
    n = len(pts)
    if n < 3:
        return pts
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    eps2 = eps * eps
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        maxd = -1.0
        maxi = -1
        for i in range(a + 1, b):
            x, y = pts[i]
            if length2 > 1e-12:
                t = ((x - ax) * dx + (y - ay) * dy) / length2
                t = max(0.0, min(1.0, t))
                px, py = ax + t * dx, ay + t * dy
            else:
                px, py = ax, ay
            ddx, ddy = x - px, y - py
            d2 = ddx * ddx + ddy * ddy
            if d2 > maxd:
                maxd = d2
                maxi = i
        if maxd > eps2:
            keep[maxi] = True
            stack.append((a, maxi))
            stack.append((maxi, b))
    out = [p for p, k in zip(pts, keep) if k]
    # re-close
    out.append(out[0])
    return out


def simplify_polygon(coords, eps):
    """coords: list of rings (each [(lon,lat),...]). Returns simplified rings, drops tiny ones."""
    out = []
    for ring in coords:
        if len(ring) < 4:
            continue
        s = dp_simplify(ring, eps)
        if len(s) >= 4:
            # drop degenerate rings with tiny bbox
            xs = [p[0] for p in s]
            ys = [p[1] for p in s]
            if (max(xs) - min(xs)) > eps * 0.5 and (max(ys) - min(ys)) > eps * 0.5:
                out.append(s)
    return out


def area_of_ring(ring):
    """Shoelace area in deg^2 (sign-agnostic)."""
    s = 0.0
    n = len(ring)
    for i in range(n - 1):
        s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(s) / 2.0


def rings_from_geom(geom):
    """Extract list of rings [(lon,lat),...] from a GeoJSON Polygon/MultiPolygon."""
    if not geom:
        return []
    t = geom.get('type')
    coords = geom.get('coordinates') or []
    if t == 'Polygon':
        return [r for r in coords if len(r) >= 4]
    if t == 'MultiPolygon':
        return [r for poly in coords for r in poly if len(r) >= 4]
    return []


def nice_title(s):
    """Title-case a county name, preserving Mc/St prefixes and small words."""
    words = s.strip().split()
    out = []
    for w in words:
        low = w.lower()
        if low in ("and", "of", "the"):
            out.append(low)
        elif len(w) > 2 and low.startswith("mc"):
            out.append("Mc" + w[2:].capitalize())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def build_counties(src_geojson, out_json, eps=0.0032, name_field='NAME'):
    """Merge Montana county polygons by NAME, simplify, write compact JSON.

    Handles the Census 2020 Montana county service (property name = NAME,
    no state-code prefix to filter — the layer is Montana-only).
    """
    g = json.load(open(src_geojson, encoding='utf-8'))
    by_name = {}
    for f in g['features']:
        p = f['properties']
        name = nice_title(p.get(name_field) or '')
        if not name:
            continue
        rings = rings_from_geom(f.get('geometry'))
        if rings:
            by_name.setdefault(name, []).extend(rings)

    counties = []
    total_raw = total_sim = 0
    for name in sorted(by_name):
        rings = by_name[name]
        total_raw += sum(len(r) for r in rings)
        simp = []
        for ring in rings:
            s = dp_simplify(ring, eps)
            if len(s) >= 4:
                ar = area_of_ring(s)
                if ar > (eps * 8) ** 2:  # drop specks
                    simp.append(s)
        total_sim += sum(len(r) for r in simp)
        counties.append({'name': name, 'polys': simp})
        print(f"  {name:14s} rings={len(simp):3d} pts={sum(len(r) for r in simp):5d} (raw {sum(len(r) for r in rings)})")

    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump({'counties': counties}, f, separators=(',', ':'))
    kb = os.path.getsize(out_json) / 1024
    print(f"-> {out_json}  {kb:.0f} KB  ({total_raw} raw pts -> {total_sim} sim pts)")


def derive_outline(county_rings, eps=0.0035, min_ring_area=0.05):
    """Edge-union boundary of all county rings -> outermost state boundary rings.

    county_rings: iterable of RAW rings [(lon,lat),...] from all counties.
    Segments appearing exactly once across all rings are boundary; twice are
    interior shared edges. Chain boundary segments into rings via an endpoint
    index (O(n), not O(n^2)), keep rings above a size threshold, simplify.

    Returns list of simplified closed rings (largest first).
    """
    from collections import defaultdict

    seg_count = defaultdict(int)
    seg_points = {}  # normalized key -> (a, b) raw points

    def q(v):
        return round(v, 6)

    for ring in county_rings:
        n = len(ring)
        if n < 4:
            continue
        for i in range(n - 1):
            a = (q(ring[i][0]), q(ring[i][1]))
            b = (q(ring[i + 1][0]), q(ring[i + 1][1]))
            key = (a, b) if a <= b else (b, a)
            seg_count[key] += 1
            seg_points[key] = (a, b)

    # boundary segments: seen exactly once
    index = defaultdict(list)  # point -> [segment keys]
    boundary_keys = []
    for key, cnt in seg_count.items():
        if cnt == 1:
            a, b = seg_points[key]
            index[a].append(key)
            index[b].append(key)
            boundary_keys.append(key)

    # chain segments into rings: walk segment-to-segment, each used once,
    # close when we return to the ring's start point
    used_segs = set()
    rings = []
    for first_key in boundary_keys:
        if first_key in used_segs:
            continue
        a, b = seg_points[first_key]
        ring = [a, b]
        used_segs.add(first_key)
        cur = b
        while True:
            nxt_key = None
            for k in index[cur]:
                if k not in used_segs:
                    nxt_key = k
                    break
            if nxt_key is None:
                break
            p, q = seg_points[nxt_key]
            used_segs.add(nxt_key)
            cur = q if p == cur else p
            ring.append(cur)
            if cur == ring[0]:
                break
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)

    # keep rings above a size threshold (drops lake-island holes etc.)
    big = [r for r in rings if area_of_ring(r) > min_ring_area]
    if not big:
        big = [max(rings, key=area_of_ring)] if rings else []
    big.sort(key=area_of_ring, reverse=True)

    out = []
    for r in big:
        s = dp_simplify(r, eps)
        if len(s) >= 4:
            out.append(s)
    return out


if __name__ == '__main__':
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'scratch', 'mt_counties.geojson')
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, 'assets', 'counties.json')
    eps = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0032
    build_counties(src, out, eps)
