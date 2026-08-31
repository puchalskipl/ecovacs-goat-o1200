# Ecovacs GOAT O1200 for Home Assistant

A mower-only Home Assistant custom integration for ECOVACS GOAT lawn mowers,
**tuned for the GOAT O1200 LiDAR Pro** (O-series): a live map built from the
mower's own stored geometry (lawn outline, obstacles, the lanes still to be
cut, dock and mower position), cutting height control, battery telemetry, and a dashboard
card with mower controls. The integration domain is **`ecovacs_goat`**.

It gives you a lawn mower entity, useful sensors, mower settings, and an optional dashboard card with the map and start / stop / dock / edge-trim controls.

## Important

This is an unofficial community project. It is not affiliated with, endorsed by, certified by, or supported by ECOVACS.

Use it at your own risk. A robotic mower has moving blades and can cause damage or injury if used unsafely. Always keep the mower in sight when testing new commands, and stop using the integration if either Home Assistant or the official ECOVACS app loses reliable control.

## Tested Mowers

Developed and tested with an **ECOVACS GOAT O1200 LiDAR Pro** (firmware
2.13.10). GOAT G1-line mowers are also supported.

No promise is made that it will work with any other mower model. ECOVACS vacuums are not supported.

### Mower families

The integration detects the **mower family** and adapts the protocol dialect it uses:

- **GOAT G1 line** (G1, G1-800 / G-800, G1-2000, G1-1600) — supported. Uses the UWB beacon + `*_V2` map dialect.
- **GOAT O-series** (O800 RTK, O1200, O1200 LiDAR Pro, ...) — supported, validated against a live-mowing O1200 LiDAR Pro capture. These models speak a different dialect: the `clean` command (not `clean_V2`), `getCleanInfo` (not `getCleanInfo_V2`), RTK reference points (`rtkPos`) instead of UWB, and the `getMapState` / `getMapTrack` / `getMI` / `getAreaSet` map commands.

For O-series mowers the integration:

- Detects the model and reports it on the **GOAT model line** diagnostic sensor (with `family`, `map_dialect`, and an `experimental` flag).
- Drives the lawn mower entity, start/pause/resume/stop/dock controls, status, battery, error, and the **live position map** (mower marker + path) from the shared position stream.
- Decodes **what is still to be cut** (`onMapTrack` compact-LZMA pushes: the mower's numbered lanes plus the border lap) and hatches it on the card the way the app does, clearing it when a new mowing task starts.
- Decodes the mower's own **lawn outline** (`onMI` chain-coded geometry) and the **obstacle shapes** it has learned (`onArI`, layer 3), and draws them the way the app does: a filled lawn with obstacle holes. The grid scale is derived from each map's own payload, and the decoded geometry is persisted so it survives restarts.
- Exposes the **cutting height** (`AreaParameters.mowHeightLevel`, level x 10 mm on the O1200) as a number entity, plus battery temperature / current / voltage telemetry sensors from the `onFwBuryPoint-bd_*` stream.
- Keeps the app-style live map session alive automatically while mowing (the `auto_live_map` option, on by default) so all of the above streams without the official app being open.
- Shows the **RTK base station** on the map (from `getRTK`) where the G1 line shows UWB beacons.
- Treats the map id `"0"` reported by O-series position pushes as a placeholder, not a map switch, so live geometry survives map replies.

## Why This Integration Exists

This project is separate from Home Assistant's regular ECOVACS integration. It was created because the regular Ecovacs/Home Assistant path is built around a broader vacuum-oriented command stack, while this project only targets GOAT mowers and uses behavior observed from the official ECOVACS app.

The goal is to keep communication with the mower conservative: use pushed updates where possible, refresh state only when needed, and avoid broad background polling.

## Features

- Start or resume mowing, stop mowing, and return to dock.
- Battery, error, Wi-Fi, current mow, total mow, and consumable sensors.
- Settings for cutting height, rain delay, animal protection, AI recognition, edge mowing, warning switches, cut direction, mowing efficiency, obstacle avoidance, and speaker volumes.
- Edge trimming (the app's border-cut job) as its own button. Note the mower's
  own semantics: **stop ends a job for good** (its progress is abandoned and
  the play button then starts a *new* mow), while **pause** is the resumable
  interruption — resume picks the same job back up, edge trim included.
- Timestamps and summaries of the last mowing and the last edge trim, including how long the job took from start to finish (mid-job recharges included) — the mower keeps no dated history of its own, so the integration tracks jobs itself and persists one in progress so a Home Assistant restart does not reset the clock.
- Why the mower is doing what it is doing: `pause_reason` and `resumes_automatically` on the lawn mower entity tell a mid-job recharge (which carries on by itself) apart from a pause somebody pressed, and `charging` comes straight from the mower rather than being guessed.
- Optional Lovelace card that draws the mower's own map the way the app does: lawn outline, obstacles, the lanes still to be cut, dock, and mower.
- Opt-in debug capture tools for troubleshooting.

## Installation With HACS

Add the repository as a custom repository:

1. In Home Assistant, open **HACS**.
2. Open the three-dot menu and choose **Custom repositories**.
3. Add `https://github.com/puchalskipl/ecovacs-goat-o1200`.
4. Select category **Integration**.
5. Install **Ecovacs GOAT O1200**.
6. Restart Home Assistant.
7. Add the integration from **Settings -> Devices & services -> Add integration**.

### Testing pre-release (beta) builds

Experimental builds — for example new model support being validated with a tester — are **automatically published as pre-releases** from the development branch (each push produces an incrementing beta such as `0.2.0b1`, `0.2.0b2`; only the latest beta is kept, and the line auto-advances after each stable release). See [CONTRIBUTING.md](CONTRIBUTING.md#releases--versioning) for the full release flow. To install one in HACS:

1. Open **Ecovacs GOAT O1200** in HACS.
2. Three-dot menu -> **Redownload**.
3. Enable **Show beta versions**.
4. Pick the beta (e.g. `0.2.0b1`) and download, then **restart Home Assistant**.

To return to a stable build later, redownload and pick the latest non-beta version.

## Setup

You need your ECOVACS account username, password, and country. The integration uses the ECOVACS cloud, just like the official app.

During setup, choose a Home Assistant device name. A generated default such as `Ecovacs-GOAT-1` is provided.

ECOVACS now requires a one-time **email device verification** for Home Assistant. After you submit your account details, check the email inbox for that ECOVACS account, enter the code, and keep using the same Home Assistant instance. The integration stores a stable client device id and keeps the verified account session in a **private Home Assistant store** (not in the config entry). Later startups reuse and rotate that session with `checkLogin`, so they do not password-login again. If the integration later asks you to reauthenticate, confirm the password and enter a fresh email code. You can also start that login yourself from **Settings → Devices & services → Ecovacs GOAT O1200 → Configure → Re-authenticate account**.

## Optional Dashboard Card

The custom card is optional, but recommended. It exposes a clear stop button and a mower-focused map layout.

**No manual install is needed.** The integration bundles the card and registers it with Home Assistant automatically: it serves `ecovacs-goat-card.js` from the integration and loads it as a frontend module, versioned to the integration release so browsers pick up updates after each upgrade (a hard refresh may be needed once).

Just add **Ecovacs GOAT Card** from the custom card picker (you may need to reload the dashboard once after first install).

> **Upgrading from a manual install (do this):** if you previously added a `/local/ecovacs_goat/ecovacs-goat-card.js` Lovelace resource, **remove it** in **Settings -> Dashboards -> Resources**, then hard-refresh your browser (Ctrl+Shift+R / Cmd+Shift+R). This is required: a dashboard card can only be registered once, and the old manual resource has no version in its URL, so your browser keeps loading the **cached old card** and never picks up the bundled one. You can also delete the copied `config/www/ecovacs_goat/ecovacs-goat-card.js` file.

While the card is visible it periodically asks the integration for an app-style live session, so the map animates like the official app; a hidden or closed card sends nothing.

Example YAML:

```yaml
type: custom:ecovacs-goat-card
entity: lawn_mower.mower
battery_entity: sensor.mower_battery_level
error_entity: sensor.mower_error
area_entity: sensor.mower_mowing_area
progress_entity: sensor.mower_mowing_progress
stop_button: button.mower_end_mowing
trim_button: button.mower_start_edge_trimming
map_entity: sensor.mower_live_map
name: Mower
```

Layout options: `show_header`, `show_summary` and `show_buttons` (all `true` by
default) turn off the card's own title, metric tiles and button row — useful
when the dashboard already shows those and the card should only draw the map.
`map_max_height` (CSS pixels, default `380`) caps the map height so a wide
dashboard column does not push the controls below the fold.

The card falls back to `sensor.mower_live_map` when `map_entity` is not set, so
set it explicitly if your mower's entities use a different prefix — otherwise
the map shows "Waiting for live map data" forever. 

## How It Behaves

The integration tries to be conservative with the mower and cloud connection:

- It prefers live updates pushed by ECOVACS over MQTT.
- It refreshes grouped state at startup and after meaningful MQTT changes (with a short debounced readback).
- It avoids broad background polling loops.
- Commands name the job they act on. The mower matches a `clean` act against
  the job currently open and **silently ignores a mismatch** (answering `ok`),
  so stop/pause/resume carry the running job's type — otherwise an edge trim
  cannot be stopped from Home Assistant.
- Map geometry and the remaining-work lanes only ever change on the mower's own pushes. A grouped refresh assembles its result from a snapshot taken seconds earlier, so publishing it verbatim would make those layers flicker between the new picture and the old one.
- Decoded geometry is persisted and versioned: after a decoder change the stored shapes are dropped and refetched rather than drawn wrong.

### Live map: what is drawn and where it comes from

The mower stores its map as vector geometry, so the card draws the same
picture the official app does, from four layers:

1. **Lawn outline** — the mower's own stored map (`onMI`), an anchor point
   plus an 8-direction chain code. The card fills it green. The mower sends
   it reliably during a job and only occasionally while docked (often just an
   empty placeholder), so the integration persists the last real outline: it
   survives restarts and new jobs, and an empty reply never clears it. Stored
   geometry is versioned — after a decoder change it is dropped and refetched
   rather than drawn wrong.
2. **Obstacles** — chain-coded shapes the mower has learned (`onArI`,
   layer 3), punched out of the lawn as holes with `fill-rule="evenodd"`.
3. **What is still to be cut** — the mower plans a job as numbered lanes and
   reports what is left on each of them (`onMapTrack`), shrinking them as it
   works. The card hatches those over the lawn and they disappear lane by
   lane, exactly as the app does. The lanes are separate segments, never
   joined into one path. The full plan arrives as the answer to `getMapTrack`
   and, for a mow, is **split across two messages** that must be joined before
   they decode — an integration that ignores multi-part pushes shows a plan
   for edge trims but never for a mow.

   The same layer carries the **border lap** — the edge finishing pass. The
   mower announces the lap closed, then reports only the arc from the loop's
   fixed origin to its own front; the stretch it cuts last is never sent, so
   the integration keeps the announcement as a template and completes the
   remainder from it. The card does not draw that chain directly (it drifts a
   cell or two from the outline): like the app, it recolours the lawn boundary
   by progress — green for still to edge, white for done.
4. **Mower and dock** — the mower marker from `onPos`, the dock at the origin
   `(0, 0)`; the dock *is* the coordinate frame's origin, so a docked mower
   legitimately reports `(0, 0)`.

There is deliberately **no trail of where the mower has driven**: the official
app does not draw one either.

All four share one coordinate frame. The chain code's grid scale is read out
of the mower's own payload (`centerX`/`centerY` give the outline's bounding-box
centre in map units, which pins map units per grid cell and is cross-checked
across both axes), so gardens whose map uses a different grid still decode
correctly; `CHAIN_STEP` in `map_geometry.py` is only the fallback. Both the
derivation and the direction mapping are covered by tests against a real
capture.

If a mower or firmware never sends an outline, the integration falls back to
tracing the boundary of the accumulated coverage (`map_outline.py`); the
outline's provenance is exposed as `outline_source` (`mower` or `coverage`)
on the map sensor.

**Position updates** — the moving marker follows MQTT `onPos`. If `onPos` has
been quiet for about 60 seconds while mowing, a background task falls back to
polling `getPos`. The service `ecovacs_goat.request_live_position_stream`
asks ECOVACS for an app-style session so the mower streams positions the way
it does with the app open; the `auto_live_map` option (on by default) keeps
that session alive automatically while mowing, and the card refreshes it
while it is visible.

For technical protocol notes, see `docs/protocol-summary.md`.

## Debug Capture

If a model or feature does not work, use the debug capture services before opening an issue. Captures are disabled by default and stored locally under `/config/ecovacs_goat_debug/`.

Recommended workflow:

1. Call `ecovacs_goat.start_debug_capture` from **Developer Tools -> Services**.
2. Reproduce the problem.
3. Optionally call `ecovacs_goat.mark_debug_capture` at useful moments.
4. Call `ecovacs_goat.stop_debug_capture`.
5. Download Home Assistant diagnostics or call `ecovacs_goat.export_debug_capture`.

Do not share passwords, access tokens, device IDs, or private network details publicly.

## Troubleshooting

- If entities are unknown after setup, press **Refresh state** once the mower is online.
- If commands fail, check whether the official ECOVACS app can still control the mower.
- If both Home Assistant and the official app lose contact with the mower, stop testing and recover the mower first.
- **"Invalid ECOVACS credentials" although the password is right.** The account
  session is tied to the client device id the integration registered. Logging
  in elsewhere with that same id (a script reusing it, a second Home Assistant
  restored from a backup) invalidates the stored session. Reauthenticate from
  **Settings → Devices & services → Ecovacs GOAT O1200 → Configure →
  Re-authenticate account**, and do not reuse the integration's device id
  outside Home Assistant.
- **The map shows no plan while mowing** (a lone lane, no hatching). The plan
  is broadcast when the mower sees a fresh app-style session connect; it is
  re-requested automatically at the start of every job, but you can force one
  with `ecovacs_goat.request_live_position_stream` or simply by opening the
  official app once.
- When opening an issue, include the integration version, mower model, Home Assistant logs, and the action that failed.

## Safety

Only use mower commands when the mower is in a safe outdoor state. Keep people, pets, and objects away from the mower while testing. If behavior looks wrong, stop the mower with the official app or physical controls first.

This software is provided without any warranty. Compatibility is best effort and may change if ECOVACS changes its app, cloud service, firmware, or account behavior. You are responsible for deciding whether it is safe to use with your mower and property.

ECOVACS and GOAT names and trademarks belong to their respective owner. This project does not claim any ownership of ECOVACS branding or official assets.

## More Information

- `docs/protocol-summary.md` has sanitized protocol notes.
- `CONTRIBUTING.md` explains the development scope.
- `SECURITY.md` explains what not to share publicly.
- `ACKNOWLEDGEMENTS.md` credits related community work.

## License

MIT License. See `LICENSE`.
