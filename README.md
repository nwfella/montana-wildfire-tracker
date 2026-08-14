# Montana Wildfire Tracker 🔥

Live wildfire tracker for the state of Montana — active fires, daily perimeters, containment,
air quality, red-flag warnings, and open-burning restrictions. **Fully static**: every data
point is baked into `index.html` server-side, so the page works even where corporate IT
blocks `fetch`/XHR.

**Live:** https://nwfella.github.io/montana-wildfire-tracker/

## Features

- **Hero map, top and center** — full-width canvas map of Montana on page load; **⛶ maximizes it to full screen** (native Fullscreen API + CSS fallback, Esc/✕ to exit)
- **5 color themes** — Ember (default), Forest, Ocean, Magma, Daybreak; theme picker in the header, saved to localStorage, the canvas map recolorizes to match
- **Legend = layer filters** — tap any legend row (active fires, perimeters, county burn heat, AQI monitors) to hide/show that layer on the map; multi-select; "show all" reset; choices remembered
- **Collapsible legend** — the **−** button minimizes it to a small "Legend" pill (tap to bring it back; auto-collapsed on phones)
- **Mobile-first** — touch map, pinch-zoom + drag pan, swipeable alert strip, responsive grid lists
- **County burn-heat choropleth** — all 56 Montana counties shaded by active fire acreage
- **Active fires** — live from the NIFC/WFIGS mirror: size, containment, cause, personnel, structures lost, complexity, discovery & last-report times
- **Anchored popup details** — tap a fire on the map and a detail bubble appears right next to the marker; the same card opens inline from the list — full details from either surface
- **Daily perimeters** — orange fire boundaries, pulse-highlighted when a fire is selected
- **Air quality** — official Montana DEQ monitors (PM2.5), EPA AQI + category colors, worst-first ranking
- **NWS alerts** — red flag warnings, fire weather watches, evacuations, air quality alerts (color-coded swipe strip + list)
- **Burn status** — Montana DEQ open-burning restrictions by area (no restrictions / fully restricted / regulated by county), with the agency's contact info
- **No-JS fallback** — static table of the top fires renders with JavaScript disabled
- **Zero runtime network calls** — a cron refreshes the snapshot 3×/day

## Data sources (all public, no API keys)

| Data | Source |
|---|---|
| Incidents | Esri Live Feeds `USA_Wildfires_v1` (NIFC/WFIGS mirror) |
| Perimeters | Esri `Wildfire_aggregated_v1` (daily fire perimeters) |
| Air quality | Montana DEQ `Montana_Air_Quality_Monitoring_Data_REV24` |
| Alerts | NWS `api.weather.gov` (area=MT) |
| Burn status | Montana DEQ `Montana_Open_Burning_Restrictions` |
| Counties | Census 2020 PL94-171 Montana county service (ArcGIS) |
| State outline | Derived from county rings via edge-union |

## How it works

```
scripts/collect.py (cron, 3×/day)
  ├─ fetch incidents / perimeters / AQI / alerts / burn status (keyless)
  ├─ normalize + simplify geometry (Douglas-Peucker: 215K county pts → 6K)
  ├─ compute stats + county burn heat + EPA AQI
  └─ bake inline JSON into index.html via template.html markers
        → git commit + push → GitHub Pages serves the static snapshot
```

- `template.html` — editable source with `/*__MT_FIRE_START__*/`…`/*__MT_FIRE_END__*/` markers
- `index.html` — generated, fully self-contained, committed
- `scripts/geo.py` — Douglas-Peucker + edge-union outline derivation
- `scripts/build_assets.py` — one-time fetch of Census counties → `assets/counties.json` + `assets/montana_outline.json`
- `scripts/collect.py` — the baker (also run manually for local refresh)
- `scripts/publish.py` — cron wrapper: bake → commit only if changed → push (silent when nothing new)

### Local refresh

```bash
python scripts/collect.py    # fetches + bakes index.html
```

## License

MIT — see [LICENSE](LICENSE).
