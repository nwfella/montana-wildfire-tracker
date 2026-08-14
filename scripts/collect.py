#!/usr/bin/env python3
"""Montana Wildfire Tracker — data collector & static baker.

Fetches live wildfire data (keyless public APIs), normalizes it, and bakes a
fully static snapshot into index.html (zero runtime network calls — the IT-safe
pattern: the page must render even where fetch/XHR is blocked).

Sources:
  - Incidents:  Esri Live Feeds USA_Wildfires_v1 (NIFC/WFIGS mirror), MT bbox
  - Perimeters: Esri Wildfire_aggregated_v1 layer 1 (daily fire perimeters)
  - Air quality: Montana DEQ official monitoring (Montana_Air_Quality_Monitoring_Data_REV24)
  - Alerts:     NWS api.weather.gov (area=MT), fire-relevant events only
  - Burn status: Montana DEQ Open Burning Restrictions (state burn areas, status codes)
  - Counties:   cached simplified GeoJSON (assets/counties.json, see geo.py)
  - Outline:    cached state boundary (assets/montana_outline.json)

Usage:  python scripts/collect.py
"""
import json
import os
import re
import sys
import time
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from geo import dp_simplify, rings_from_geom  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
MT_BBOX = "-116.1,44.3,-103.9,49.1"
FIRES_URL = ("https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/"
             "USA_Wildfires_v1/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson"
             "&geometry=-116.1%2C44.3%2C-103.9%2C49.1&geometryType=esriGeometryEnvelope&inSR=4326&outSR=4326"
             "&resultRecordCount=600")
PERIM_URL = ("https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/"
             "Wildfire_aggregated_v1/FeatureServer/1/query?where=1%3D1&outFields=*&f=geojson"
             "&geometry=-116.1%2C44.3%2C-103.9%2C49.1&geometryType=esriGeometryEnvelope&inSR=4326&outSR=4326"
             "&resultRecordCount=600")
# Montana DEQ official monitoring — PM2.5 hourly rows, newest first
AQI_URL = ("https://gis.mtdeq.us/hosting/rest/services/Hosted/"
           "Montana_Air_Quality_Monitoring_Data_REV24/FeatureServer/0/query"
           "?where=parameter%3D%27PM25%27"
           "&outFields=sitename,latitude,longitude,parameter,nowcast,rawvalue,aqi_value,healthcategory,datetime"
           "&f=geojson&resultRecordCount=600&orderByFields=datetime%20DESC")
NWS_URL = "https://api.weather.gov/alerts/active?area=MT"
BURN_URL = ("https://gis.mtdeq.us/hosting/rest/services/Hosted/"
            "Montana_Open_Burning_Restrictions/FeatureServer/0/query"
            "?where=1%3D1&outFields=*&f=geojson&resultRecordCount=100")
BURN_META_URL = ("https://gis.mtdeq.us/hosting/rest/services/Hosted/"
                 "Montana_Open_Burning_Restrictions/FeatureServer/0?f=json")

# Montana bounds (strict post-filter for AQI; bbox queries leak neighbors)
MT_LON = (-116.1, -103.9)
MT_LAT = (44.3, 49.1)

ALERT_EVENTS = {
    "Red Flag Warning": 1, "Fire Weather Watch": 2, "Evacuation": 3, "Evacuation Order": 3,
    "Evacuation Warning": 3, "Air Quality Alert": 4, "Excessive Heat Warning": 5,
    "Heat Advisory": 6, "High Wind Warning": 7, "Wind Advisory": 8, "Fire Warning": 1,
    "Flash Flood Warning": 7, "Flood Warning": 8, "Flood Watch": 9, "Severe Thunderstorm Warning": 9,
}
ALERT_CATEGORIES = {
    "Fire Warning": "Fire Warning", "Red Flag Warning": "Red Flag Warning", "Fire Weather Watch": "Fire Weather Watch",
    "Evacuation Order": "Evacuation", "Evacuation Warning": "Evacuation", "Evacuation": "Evacuation",
    "Air Quality Alert": "Air Quality Alert",
}
KEEP_UNKNOWN = True  # keep unexpected events but rank them lowest


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, timeout=30):
    return json.loads(fetch(url, timeout).decode("utf-8", "replace"))


def epoch_to_iso(ms):
    if not ms:
        return None
    try:
        return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def pm25_to_aqi(pm):
    """EPA PM2.5 breakpoints → AQI + category + color."""
    bp = [(0.0, 12.0, 0, 50), (12.1, 35.4, 51, 100), (35.5, 55.4, 101, 150),
          (55.5, 150.4, 151, 200), (150.5, 250.4, 201, 300), (250.5, 500.4, 301, 500)]
    for lo, hi, alo, ahi in bp:
        if lo <= pm <= hi:
            aqi = int(round((ahi - alo) / (hi - lo) * (pm - lo) + alo))
            break
    else:
        aqi = min(999, int(pm * 2))
    if aqi <= 50:
        return aqi, "Good", "#00e400"
    if aqi <= 100:
        return aqi, "Moderate", "#ffff00"
    if aqi <= 150:
        return aqi, "USG", "#ff7e00"
    if aqi <= 200:
        return aqi, "Unhealthy", "#ff0000"
    if aqi <= 300:
        return aqi, "Very Unhealthy", "#8f3f97"
    return aqi, "Hazardous", "#7e0023"


# ---------------------------------------------------------------- incidents
def get_incidents():
    g = fetch_json(FIRES_URL)
    feats = g.get("features", [])
    out = []
    seen = set()
    for f in feats:
        a = f.get("properties") or f.get("attributes") or {}
        if a.get("POOState") != "US-MT":
            continue
        uid = a.get("UniqueFireIdentifier") or a.get("IrwinID") or str(a.get("OBJECTID"))
        if uid in seen:
            continue
        seen.add(uid)
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        acres = a.get("CalculatedAcres") or a.get("DailyAcres") or 0
        struc = (a.get("ResidencesDestroyed") or 0) + (a.get("OtherStructuresDestroyed") or 0)
        out.append({
            "id": uid,
            "n": a.get("IncidentName") or "Unnamed fire",
            "cnty": (a.get("POOCounty") or "").strip() or None,
            "acres": round(acres, 1),
            "cont": a.get("PercentContained"),
            "cause": a.get("FireCause") or None,
            "kind": a.get("IncidentTypeKind") or a.get("IncidentTypeCategory") or None,
            "per": a.get("TotalIncidentPersonnel"),
            "struc": struc or None,
            "inj": a.get("Injuries") or None,
            "fat": a.get("Fatalities") or None,
            "comp": a.get("FireMgmtComplexity") or None,
            "mgmt": a.get("IncidentManagementOrganization") or None,
            "fuel": a.get("PredominantFuelGroup") or None,
            "disp": epoch_to_iso(a.get("FireDiscoveryDateTime")),
            "rep": epoch_to_iso(a.get("ICS209ReportDateTime")),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "irwin": a.get("IrwinID"),
        })
    out.sort(key=lambda i: -(i["acres"] or 0))
    return out


# ---------------------------------------------------------------- perimeters
def get_perimeters(incidents):
    irwin_ids = set(i.get("irwin") for i in incidents if i.get("irwin"))
    g = fetch_json(PERIM_URL)
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    used_names = set()
    for f in g.get("features", []):
        a = f.get("properties") or {}
        dcur = a.get("DateCurrent")
        keep = False
        if a.get("IRWINID") in irwin_ids:
            keep = True
        elif dcur:
            try:
                dt = datetime.datetime.fromtimestamp(dcur / 1000, tz=datetime.timezone.utc)
                if (now - dt).days <= 30:
                    keep = True
            except (ValueError, OSError, OverflowError):
                keep = False
        if not keep:
            continue
        rings = rings_from_geom(f.get("geometry"))
        simp = []
        for r in rings:
            s = dp_simplify(r, 0.004)
            if len(s) >= 8:
                # cap per-ring points
                if len(s) > 2500:
                    step = len(s) // 2500
                    s = s[::step] + [s[-1]]
                simp.append(s)
        if not simp:
            continue
        name = (a.get("IncidentName") or "").strip()
        if not name:
            continue
        # dedupe by name: keep the most recent
        date_s = epoch_to_iso(dcur)
        if name in used_names:
            continue
        used_names.add(name)
        out.append({"n": name, "d": date_s, "acres": round(a.get("GISAcres") or 0, 0), "p": simp})
    # cap total points to keep the HTML lean
    total = sum(len(r) for o in out for r in o["p"])
    if total > 40000:
        factor = 40000 / total
        for o in out:
            o["p"] = [r[::max(1, int(1 / factor))] + [r[-1]] for r in o["p"]]
    out.sort(key=lambda o: -o["acres"])
    return out


# ---------------------------------------------------------------- air quality (MT DEQ)
def get_aqi():
    """Montana DEQ official PM2.5 rows (parameter='PM25', newest first).

    Prefers the agency's own nowcast + aqi_value; falls back to EPA breakpoints
    from the raw reading. Dedupes by site keeping the newest row.
    """
    g = fetch_json(AQI_URL)
    out = []
    seen_site = {}
    for f in g.get("features", []):
        a = f.get("properties") or f.get("attributes") or {}
        par = (a.get("parameter") or "").upper().replace(" ", "")
        if "PM2" not in par:
            continue
        lon = a.get("longitude")
        lat = a.get("latitude")
        if lon is None or lat is None:
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) >= 2:
                lon, lat = coords[0], coords[1]
        if lon is None or lat is None:
            continue
        if not (MT_LON[0] <= lon <= MT_LON[1] and MT_LAT[0] <= lat <= MT_LAT[1]):
            continue  # strict Montana bounds
        v = a.get("nowcast")
        if v is None:
            v = a.get("rawvalue")
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v < 0 or v > 2000:
            continue
        # official AQI if the agency published one, else EPA breakpoints
        aqi = None
        try:
            av = float(a.get("aqi_value")) if a.get("aqi_value") is not None else None
            if av is not None and 0 <= av <= 600:
                aqi = int(round(av))
        except (TypeError, ValueError):
            aqi = None
        if aqi is None:
            aqi, cat, col = pm25_to_aqi(v)
        else:
            cat = (a.get("healthcategory") or "").strip() or "—"
            col = {"Good": "#00e400", "Moderate": "#ffff00", "Unhealthy for Sensitive Groups": "#ff7e00",
                   "USG": "#ff7e00", "Unhealthy": "#ff0000", "Very Unhealthy": "#8f3f97",
                   "Hazardous": "#7e0023"}.get(cat, "#888")
        dt = a.get("datetime")
        item = {
            "c": (a.get("sitename") or "Unknown").strip(),
            "l": (a.get("sitename") or "").strip(),
            "v": round(v, 1),
            "aqi": aqi,
            "cat": cat,
            "col": col,
            "t": epoch_to_iso(dt),
            "lat": round(lat, 4),
            "lon": round(lon, 4),
        }
        # dedupe by site, keep the newest row
        cur = seen_site.get(item["l"])
        if cur is None or (item.get("t") or "") > (cur.get("t") or ""):
            seen_site[item["l"]] = item
    out = list(seen_site.values())
    out.sort(key=lambda o: -o["v"])
    return out


# ---------------------------------------------------------------- alerts
def get_alerts():
    d = fetch_json(NWS_URL)
    out = []
    for f in d.get("features", []):
        p = f.get("properties") or {}
        event = p.get("event") or ""
        rank = ALERT_EVENTS.get(event)
        if rank is None and not KEEP_UNKNOWN:
            continue
        if rank is None:
            rank = 99
        out.append({
            "e": ALERT_CATEGORIES.get(event, event),
            "raw": event,
            "h": p.get("headline") or event,
            "a": p.get("areaDesc") or "",
            "sev": p.get("severity") or "",
            "on": p.get("onset"),
            "ex": p.get("expires"),
            "d": (p.get("description") or "")[:400],
            "url": "https://api.weather.gov/alerts/" + (f.get("id", "").rsplit("/", 1)[-1]),
            "rank": rank,
        })
    out.sort(key=lambda o: o["rank"])
    return out


# ---------------------------------------------------------------- burn restrictions
def get_burn_status():
    """Montana DEQ open burning restrictions (Fall layer) — area + status + contact.

    Status codes are decoded from the layer's own coded-value domain at bake
    time (0 = no restrictions, 1 = fully restricted, 2 = regulated by county, ...).
    """
    g = fetch_json(BURN_URL)
    # decode the restrictions domain from the layer metadata
    code_map = {}
    try:
        meta = fetch_json(BURN_META_URL)
        for fld in meta.get("fields", []):
            if fld.get("name") == "restrictions":
                for cv in (fld.get("domain") or {}).get("codedValues", []):
                    code_map[cv.get("code")] = cv.get("name")
    except Exception:
        code_map = {}
    out = []
    for f in g.get("features", []):
        a = f.get("properties") or f.get("attributes") or {}
        area = (a.get("area_name") or "").strip()
        if not area:
            continue
        code = a.get("restrictions")
        out.append({
            "area": area,
            "code": code if code is not None else -1,
            "status": code_map.get(code, "Status unknown") if code is not None else "Status unknown",
            "notes": (a.get("area_notes") or "").strip(),
            "edited": epoch_to_iso(a.get("last_edited_date")),
        })
    # most restricted first, then area name
    out.sort(key=lambda o: (-(o["code"] if o["code"] is not None else -1), o["area"]))
    return out


# ---------------------------------------------------------------- cities
CITIES = [
    ("Billings", 45.783, -108.501, 1), ("Missoula", 46.872, -113.994, 1), ("Great Falls", 47.505, -111.301, 1),
    ("Bozeman", 45.677, -111.043, 1), ("Butte", 46.004, -112.535, 1), ("Helena", 46.589, -112.039, 1),
    ("Kalispell", 48.196, -114.313, 1), ("Havre", 48.550, -109.684, 1), ("Miles City", 46.408, -105.846, 1),
    ("Lewistown", 47.065, -109.428, 1), ("Sidney", 47.717, -104.156, 1),
    ("Hamilton", 46.247, -114.160, 0), ("Livingston", 45.662, -110.561, 0), ("Red Lodge", 45.186, -109.246, 0),
    ("Dillon", 45.216, -112.638, 0), ("Anaconda", 46.129, -112.942, 0), ("Deer Lodge", 46.396, -112.730, 0),
    ("Glasgow", 48.197, -106.637, 0), ("Wolf Point", 48.091, -105.644, 0), ("Glendive", 47.105, -104.713, 0),
    ("Baker", 46.367, -104.284, 0), ("Malta", 48.360, -107.871, 0), ("Chinook", 48.590, -109.231, 0),
    ("Shelby", 48.507, -111.857, 0), ("Cut Bank", 48.633, -112.331, 0), ("Conrad", 48.170, -111.946, 0),
    ("Choteau", 47.812, -112.183, 0), ("Stanford", 47.153, -110.218, 0), ("Harlowton", 46.435, -109.834, 0),
    ("Big Timber", 45.834, -109.955, 0), ("White Sulphur Springs", 46.548, -110.902, 0),
    ("Townsend", 46.319, -111.520, 0), ("Three Forks", 45.892, -111.552, 0), ("Ennis", 45.349, -111.732, 0),
    ("Thompson Falls", 47.595, -115.338, 0), ("Libby", 48.388, -115.556, 0), ("Columbia Falls", 48.372, -114.181, 0),
    ("Whitefish", 48.411, -114.338, 0), ("Polson", 47.694, -114.157, 0), ("Stevensville", 46.510, -114.093, 0),
    ("West Yellowstone", 44.662, -111.104, 0), ("Gardiner", 45.032, -110.714, 0), ("Forsyth", 46.266, -106.678, 0),
    ("Hardin", 45.732, -107.612, 0), ("Fort Benton", 47.819, -110.667, 0), ("Seeley Lake", 47.176, -113.485, 0),
    ("Broadus", 45.443, -105.408, 0), ("Ekalaka", 45.889, -104.553, 0), ("Scobey", 48.790, -105.420, 0),
    ("Plentywood", 48.774, -104.562, 0),
]


def build_counties(incidents):
    with open(os.path.join(ROOT, "assets", "counties.json"), encoding="utf-8") as f:
        data = json.load(f)
    burn = {}
    for i in incidents:
        if i.get("cnty"):
            key = i["cnty"].strip().lower()
            burn[key] = burn.get(key, 0) + (i["acres"] or 0)
    out = []
    for c in data["counties"]:
        out.append({"n": c["name"], "b": round(burn.get(c["name"].lower(), 0), 0), "p": c["polys"]})
    return out


def build_static_fires_table(incidents):
    rows = []
    for i in incidents[:12]:
        cont = i["cont"] if i["cont"] is not None else "—"
        rows.append(
            f'<tr><td style="padding:6px;border:1px solid #242a38;">{i["n"]}</td>'
            f'<td style="padding:6px;border:1px solid #242a38;">{i["cnty"] or "—"}</td>'
            f'<td style="padding:6px;border:1px solid #242a38;">{int(i["acres"]):,}</td>'
            f'<td style="padding:6px;border:1px solid #242a38;">{cont}%</td>'
            f'<td style="padding:6px;border:1px solid #242a38;">{i["cause"] or "—"}</td></tr>')
    return "\n".join(rows)


def main():
    t0 = time.time()
    print("== Montana Wildfire Tracker bake ==")
    warnings = []

    try:
        incidents = get_incidents()
        print(f"  incidents: {len(incidents)}")
        if not incidents:
            print("FATAL: zero Montana incidents — refusing to bake; keeping previous index.html")
            sys.exit(1)
    except Exception as e:
        print(f"FATAL: incidents fetch failed: {e}")
        sys.exit(1)

    try:
        perimeters = get_perimeters(incidents)
        print(f"  perimeters: {len(perimeters)}")
    except Exception as e:
        warnings.append(f"perimeters: {e}")
        perimeters = []

    try:
        aqi = get_aqi()
        print(f"  aqi monitors: {len(aqi)}")
    except Exception as e:
        warnings.append(f"aqi: {e}")
        aqi = []

    try:
        alerts = get_alerts()
        red_flags = sum(1 for a in alerts if "Red Flag" in a["raw"] or "Fire Warning" in a["raw"])
        print(f"  alerts: {len(alerts)} ({red_flags} red flag/fire warnings)")
    except Exception as e:
        warnings.append(f"alerts: {e}")
        alerts = []
        red_flags = 0

    try:
        burn = get_burn_status()
        restricted = sum(1 for b in burn if b["code"] in (1, 2))
        print(f"  burn areas: {len(burn)} ({restricted} with restrictions)")
    except Exception as e:
        warnings.append(f"burn: {e}")
        burn = []

    counties = build_counties(incidents)
    print(f"  counties: {len(counties)}")

    with open(os.path.join(ROOT, "assets", "montana_outline.json"), encoding="utf-8") as f:
        outline = json.load(f)["rings"]
    print(f"  state outline rings: {len(outline)}")

    # ---- stats
    total_acres = sum(i["acres"] or 0 for i in incidents)
    conts = [i["cont"] for i in incidents if i["cont"] is not None]
    avg_cont = round(sum(conts) / len(conts)) if conts else None
    total_per = sum(i["per"] or 0 for i in incidents)
    now = datetime.datetime.now(datetime.timezone.utc)
    new24 = sum(1 for i in incidents if i["disp"] and
                (now - datetime.datetime.fromisoformat(i["disp"])).total_seconds() < 86400)
    worst = aqi[0] if aqi else None
    stats = {
        "fires": len(incidents), "acres": round(total_acres), "avgCont": avg_cont,
        "per": total_per, "new24": new24, "redFlags": red_flags,
        "worstAqi": {"city": worst["c"], "aqi": worst["aqi"], "cat": worst["cat"]} if worst else None,
        "burnAreas": len(burn),
        "burnRestricted": restricted,
    }

    data = {
        "updated": now_iso(),
        "stats": stats,
        "incidents": incidents,
        "perimeters": perimeters,
        "aqi": aqi,
        "alerts": [{"e": a["e"], "h": a["h"], "a": a["a"], "sev": a["sev"],
                    "on": a["on"], "ex": a["ex"], "d": a["d"], "url": a["url"]} for a in alerts],
        "burn": burn,
        "counties": counties,
        "outline": outline,
        "cities": [{"n": n, "lat": la, "lon": lo, "major": m} for n, la, lo, m in CITIES],
    }

    # ---- bake
    with open(os.path.join(ROOT, "template.html"), "rb") as f:
        html = f.read()

    payload = ("\nconst MT_FIRE_DATA = " +
               json.dumps(data, separators=(",", ":")).replace("<", "\\u003c") + ";\n").encode("utf-8")

    START = b"/*__MT_FIRE_START__*/"
    END = b"/*__MT_FIRE_END__*/"
    s = html.find(START)
    e = html.find(END)
    if s == -1 or e == -1 or e <= s:
        print("FATAL: bake markers not found in template.html")
        sys.exit(1)
    baked = html[:s + len(START)] + payload + html[e:]

    static_rows = build_static_fires_table(incidents).encode("utf-8")
    m = b"<!--__STATIC_FIRES__-->"
    mi = baked.find(m)
    if mi != -1:
        baked = baked[:mi] + static_rows + baked[mi + len(m):]

    out_path = os.path.join(ROOT, "index.html")
    with open(out_path, "wb") as f:
        f.write(baked)

    # ---- data artifacts
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "timestamp.json"), "w") as f:
        json.dump({"updated": data["updated"]}, f, separators=(",", ":"))
    with open(os.path.join(ROOT, "data", "snapshot.json"), "w") as f:
        json.dump(data, f, separators=(",", ":"))

    kb = os.path.getsize(out_path) / 1024
    print(f"  baked index.html: {kb:.0f} KB in {time.time() - t0:.1f}s")
    if warnings:
        print("  WARN:", "; ".join(warnings))
    print(f"  top fires: {', '.join(i['n'] for i in incidents[:5])}")
    if worst:
        print(f"  worst AQI: {worst['c']} — {worst['aqi']} ({worst['cat']})")
    print("OK")


if __name__ == "__main__":
    main()
