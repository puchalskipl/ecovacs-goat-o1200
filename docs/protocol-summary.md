# Protocol Summary

This document intentionally contains only sanitized, high-level protocol notes
suitable for a public repository. Raw captures, device identifiers, account
details, and packet files are never included.

## Design

The integration is based on observed official-app behaviour for ECOVACS GOAT
mowers. Primary reference: live captures of a **GOAT O1200 LiDAR Pro**
(firmware 2.13.10), including full mowing sessions; the GOAT G1 line
(G1, G1-800 / G-800, G1-2000) was validated earlier against a G1-800 capture.

Transport:

- Device commands use the N-GIoT endpoint `/api/iot/endpoint/control` with
  `apn=<command>` and `fmt=j`; bodies use the app-style envelope with header
  version `0.0.22`.
- Live updates arrive over ECOVACS MQTT, topics shaped like
  `iot/atr/on.../<device>/<class>/<resource>/j`.
- Startup uses grouped `getInfo` readbacks to populate state; runtime changes
  come from MQTT pushes wherever possible, and readback before commands is
  stale-only to avoid unnecessary polling.
- Large payloads (map geometry, tracks) are base64-wrapped **compact LZMA**
  blobs: LZMA1 props, a 4-byte little-endian uncompressed size, then the raw
  stream — i.e. `FORMAT_ALONE` with the 8-byte size field shrunk to 4 bytes.
- Grouped `getInfo` replies that exceed the MQTT payload limit are split into
  `{d_id, d_seq, d_sum, d_val}` fragments carrying slices of the response
  JSON *as text*; only the concatenation parses.

## Model families and map dialects

The official app ships one H5 bundle for the whole GOAT range, but the mowers
speak **two dialects**. The integration models this with a per-device
*capability profile* (`mower_profiles.py`) selected from the cloud
`deviceName`:

- **GOAT G1 line** (`family = goat_g1`): UWB beacons with the `*_V2` dialect —
  `clean_V2`, `getCleanInfo_V2`, `getMapInfo_V2` / `getMapTrace_V2`, and
  `uwbPos` positions. Default for unknown models, so existing setups never
  regress.
- **GOAT O-series** (`family = goat_o_series`): O800 RTK, O1200,
  O1200 LiDAR Pro. Differences:
  - `clean` (not `clean_V2`); every act carries `content:{type:"auto"}` and
    the stop body uses `content.type = "auto"` (G1 uses `""`).
  - **Edge trimming** is its own job:
    `clean {act:"start", content:{type:"borderrotate", value:"reid:1;"}}` —
    without the `value` the mower answers "get border content error".
  - `getCleanInfo` (not `getCleanInfo_V2`) — same status fields (`state`,
    `cleanState.motionState`, `trigger`, nested `cleanState.cid`).
  - `getPos` returns `deebotPos` / `chargePos` / **`rtkPos`** (no `uwbPos`);
    position pushes report the placeholder map id `"0"`, which must not be
    treated as a map switch.
  - `getRTK` returns the single fixed base station (`rtks[0].x/y/sn`) plus
    GNSS `observations`.
  - Map dialect `getMapState` / `getMI` / `getMapTrack` / `getAreaSet`.

Dock (`charge {act:"go"}`), `appping`, `getLifeSpan`, battery, and error are
shared. The runtime profile in `mower_compat.py` still adapts on failures.

## O-series map geometry (decoded)

The mower's map is **vector geometry, not a bitmap**. Closed shapes are sent
as an anchor plus an 8-direction chain code, wrapped in the compact-LZMA blob:

| Push | Payload | Meaning |
| --- | --- | --- |
| `onMI` (`type: "-1"`) | `[["1", "s1;<seq>;<x>,<y>;<chain>"], ["2", ...]]` | **Lawn outline** — the shape the app fills green. Arrives reliably during a job; while docked, `getMI` is acknowledged but the push follows only sometimes, and mid-job replies carry an empty `s1;0;` placeholder — consumers must persist the last real outline. |
| `onArI` | `["1", "<layer>", "<count>", "<id>;<x>,<y>;<chain>", ...]` | Numbered layers; layer **3** holds the **obstacle shapes** the app paints as holes in the lawn. |
| `onMapTrack` | records whose third field is `...;x,y;x,y;...` | Window of recently **mowed coordinates** (the current job's cut path). |
| `onPos` / `getRTK` | `deebotPos` / `rtks[0]` | Mower marker and RTK base station. |

Chain-code decoding (calibrated against a live O1200 capture): `(n)` repeats
the previous digit n extra times, and the digits walk a square grid where
**even digits are the cardinal directions and odd digits the diagonals**
(`2` = +Y, `4` = +X, `6` = -Y, `8` = -X, `1` = -X+Y, `3` = +X+Y, `5` = +X-Y,
`7` = -X-Y). The Y axis matches the position frame — no mirroring.

**The grid scale is carried by the payload, not assumed:** `centerX`/`centerY`
in the `onMI` message are the centre of the outline's bounding box in map
units, so map units per cell = `(centerX - anchor_x) / cell_bbox_centre_x`,
cross-checked against the Y axis. On the reference mower both axes yield
exactly **50**, which is also the fallback for payloads that omit the centre.
Deriving it (rather than hard-coding) keeps the decode correct for other
gardens and firmware revisions.

With that, outline, obstacles, track, mower and dock all share one coordinate
frame — **the dock is the origin `(0, 0)`**, so a docked mower legitimately
reports position `(0, 0)`.

Chain codes emit one point per cell, so long straight edges arrive as hundreds
of collinear points; collapsing them (`map_geometry.drop_collinear`) reduces a
real outline from ~2200 points to ~220 without changing the shape.

Dead ends, so nobody retries them:

- `getSpecialContour` and `getMapInfo` look like contour requests but this
  firmware never answers them — each call times out after ~20 s.
- There is **no bitmap/piece mechanism** for mowers (unlike Deebot vacuums'
  `getMajorMap`/`getMinorMap`): the app renders the picture client-side from
  the vector geometry, which is why it can show the full lawn while the mower
  sits docked.
- There is **no video stream** in this protocol: `onFwBuryPoint-bd_camera` is
  bare telemetry and `videoMoveSupportChannel`/`videoMoveTask` are capability
  flags; the app's live view uses a private P2P/WebRTC channel.

## Other observed behaviours

- `getLifeSpan` reports **only the blade** on the O1200 (tested with `{}`,
  `{"type":"-1"}`, and list payloads); the app's extra consumables are cloud
  counters, not device data.
- `getTotalStats` reports area in **m²**, while per-session stats use cm².
- The session `duration` reported during a job is the mower's **estimate for
  the whole task**, fixed at start — not an elapsed-time counter.
- `getProtectState` pushes are **partial**: a payload may carry only some of
  `isAnimProtect` / `isRainProtect` / `isRainDelay` / `isEStop` / `isLocked`,
  so absent flags must not clear previously reported ones.
- `setChildLock` is accepted but ignored by the O1200 firmware.
- When a start is blocked (rain / animal protection), the mower accepts the
  command, drives for ~5 s, and returns on its own — that is not an error.
- The MQTT broker's certificate does not pass standard verification; the
  integration attempts verified TLS first and falls back explicitly with a
  warning.
