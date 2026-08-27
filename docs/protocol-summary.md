# Protocol Summary

This document intentionally contains only sanitized, high-level protocol notes suitable for a public repository.

## Design

The integration is based on observed official app behavior for ECOVACS **GOAT G1-line** mowers (retail names such as **GOAT G1**, **G1-2000**, **G1-800** / **G-800**). The same H5 bundle and N-GIoT command set apply across these SKUs; differences are mainly **coverage, hardware bundles, and** `getRobotFeature` **flags** (e.g. 4G, GPS), not separate protocols.

Primary capture reference: **GOAT G1-800** (also written **G1-800**).

## Model families and map dialects

The official app ships one H5 bundle that supports the whole GOAT range, but the
mowers speak **two dialects**. The integration models this with a per-device
*capability profile* (`mower_profiles.py`) selected from the cloud `deviceName`:

- **GOAT G1 line** (`family = goat_g1`): UWB beacons with the `*_V2` dialect —
  `clean_V2`, `getCleanInfo_V2`, `getMapInfo_V2` / `getMapTrace_V2`, and
  `uwbPos` positions. This is the validated path and the default for unknown
  models, so existing setups never regress.
- **GOAT O-series** (`family = goat_o_series`, **experimental**): confirmed
  against a decrypted **GOAT O800 RTK** capture (class `9bts2s`, model
  `GOAT_O800_LC`, fw `1.9.10`):
  - `clean` (not `clean_V2`); every act carries `content:{type:"auto"}` and the
    stop body uses `content.type = "auto"` (G1 uses `""`).
  - `getCleanInfo` (not `getCleanInfo_V2`) — **same status fields**
    (`state`, `cleanState.motionState`, `trigger`, nested `cleanState.cid`).
  - `getPos` returns `deebotPos` / `chargePos` / **`rtkPos`** (no `uwbPos`).
  - `getRTK` returns the single fixed **base station** (`rtks[0].x/y/sn`) plus
    GNSS signal `observations`; the station is shown on the map where the G1
    shows UWB beacons.
  - Map dialect `getMapState` / `getMI` / `getMapTrack` / `getAreaSet` /
    `getSpecialContour`, plus RTK-specific reads (`getRTK`, `getMoveCtrlState`).

Dock (`charge {act:"go"}`), `appping`, `getLifeSpan`, battery, and error are
shared. The capability profile picks the command names / map dialect; the runtime
profile in `mower_compat.py` still adapts on failures.

### O-series map geometry (decoded)

The mower's map is **vector geometry, not a bitmap**. Closed shapes are sent as
an anchor plus an 8-direction chain code, wrapped in the shared base64 +
compact-LZMA blob:

| Push | Payload | Meaning |
| --- | --- | --- |
| `onMI` (`type: "-1"`) | `[["1", "s1;<seq>;<x>,<y>;<chain>"], ["2", ...]]` | **Lawn outline** — the shape the app fills green. Sent while docked; during a job the mower answers with an empty `s1;0;` placeholder, so consumers must persist the last real outline. |
| `onArI` | `["1", "<layer>", "<count>", "<id>;<x>,<y>;<chain>", ...]` | Layer **3** holds the **obstacle shapes** the app paints as holes in the lawn. |
| `onMapTrack` | records whose third field is `...;x,y;x,y;...` | Window of recently **mowed coordinates** (the current job's cut path). |
| `onPos` / `getRTK` | `deebotPos` / `rtks[0]` | Mower marker and RTK base station. |

Chain-code decoding (calibrated against a live capture, fw 2.13.10): `(n)`
repeats the previous digit n extra times, and the digits walk a square grid
where **even digits are the cardinal directions and odd digits the diagonals**
(`2` = +Y, `4` = +X, `6` = -Y, `8` = -X, `1` = -X+Y, `3` = +X+Y, `5` = +X-Y,
`7` = -X-Y). The Y axis matches the position frame — no mirroring.

**The grid scale is carried by the payload, not assumed:** `centerX`/`centerY`
in the `onMI` message are the centre of the outline's bounding box in map
units, so map units per cell = `(centerX - anchor_x) / cell_bbox_centre_x`,
cross-checked against the Y axis. On this mower both axes yield exactly
**50**, which is also the fallback for payloads that omit the centre. Deriving
it (rather than hard-coding) is what keeps the decode correct for other
gardens and firmware revisions.

With that, outline, obstacles, track, mower and dock all share one coordinate
frame — the dock is the origin `(0, 0)`.

Chain codes emit one point per step, so long straight edges arrive as hundreds
of collinear points; collapsing them (`map_geometry.drop_collinear`) reduces a
real outline from ~2200 points to ~220 without changing the shape.

`getSpecialContour` and `getMapInfo` look like contour requests but this
firmware never answers them (each call times out after ~20 s), so they are
not worth sending. `getMI` is acknowledged immediately but only triggers the
`onMI` push some of the time while docked; during a job the push arrives
repeatedly, which is when the outline is reliably captured.

There is **no bitmap/piece mechanism** for mowers (unlike Deebot vacuums'
`getMajorMap`/`getMinorMap`): the app renders the picture client-side from this
geometry, which is why it can show the full lawn while the mower sits docked.

- Device commands use the N-GIoT endpoint `/api/iot/endpoint/control` with `apn=<command>` and `fmt=j`.
- Command bodies use the app-style envelope with header version `0.0.22`.
- Live updates arrive over ECOVACS MQTT topics shaped like `iot/atr/on.../<device>/<class>/<resource>/j`.
- Startup uses grouped `getInfo` readbacks to populate state.
- Runtime state changes should come from MQTT pushes where possible.
- Readback before commands is stale-only and guarded to avoid unnecessary polling.

## Intentional Omissions

Raw captures, local lab commands, device identifiers, IP addresses, account details, and packet files are not included in this repository.
