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
| `onMI` (`type: "-1"`) | `[["1", "s1;<seq>;<x>,<y>;<chain>"], ["2", ...]]` | **Lawn outline** — the shape the app fills green, plus `centerX`/`centerY`. Arrives reliably during a job; while docked, `getMI` is acknowledged but the push follows only sometimes, and mid-job replies carry an empty `s1;0;` placeholder, so the last real outline must be persisted. |
| `onArI` | `["1", "<layer>", "<count>", "<id>;<x>,<y>;<chain>", ...]` | Numbered layers; layer **3** holds the **obstacle shapes** the app paints as holes in the lawn. |
| `onMapTrack` | `["1", "<kind>", "<field>", ...]` | **What is still to be cut** — see below. Not a record of where the mower has been. |
| `onPos` / `getRTK` | `deebotPos` / `rtks[0]` | Mower marker and RTK base station. |

### onMapTrack: the remaining plan, not a trail

The mower plans a job as numbered lanes and reports **what is left** on each,
re-sending a lane every couple of seconds as it shrinks. This is the layer the
app hatches over the lawn and rubs out piece by piece; the app draws no trail
of where the mower has driven, and neither should consumers of this push.

The record's **second element** says what the push is:

* `"1"` — a **full snapshot** of the remaining plan (75 lanes on the reference
  garden, shrinking to 66 and beyond as work proceeds). It is authoritative:
  finished lanes simply stop being listed rather than being reported empty, so
  a snapshot must **replace** the known set, not merge into it.
* `"2"` — an **update** to individual lanes.

**The snapshot is the answer to `getMapTrack`, and it arrives in chunks.**
`getMapTrack` returns a bare `code 0, msg ok` over HTTP — the plan follows as
an `onMapTrack` push, split when it outgrows one message: `serial` is the
number of parts, `index` the part number, `batid` ties them together. The
parts are **base64 fragments of one LZMA stream**: they must be concatenated
in `index` order *before* decoding — decoding a part on its own fails. A
mowing plan (~7 kB, 180+ lane fields) always ships as `serial: "2"`; the
much smaller edge-lap loop fits in one message, which is why an integration
that ignores multi-chunk pushes shows the plan for edge trims but never for
a mow (diagnosed 2026-08-31 after two sessions with zero visible plan).

Each field is `<type>;<subtype>;<id>;<data>`:

* subtype `"1"` — straight lanes as coordinates **in pairs**; each pair is one
  segment, and a lane interrupted by an obstacle simply carries several pairs.
  Joining lanes into one polyline draws lines across whatever lies between
  them (a terrace, the house).
* subtype `"2"` — a chain-coded shape: the **border lap** that follows the
  lawn edge (the standalone edge trim, and the edge finishing pass of a mow).
  It is announced **closed** (first and last cell meet) before the mower
  starts driving it; snapshots then carry only the **arc from the loop's
  fixed origin onward** and the stretch beyond the origin is never
  transmitted, so the remainder is composed from the announcement kept as a
  template (`map_geometry.compose_border`). Snapshot timing differs by job:
  the standalone trim shrinks its arc every few seconds, the in-mow pass can
  hold the same arc for **minutes**. The live progress signal in both is the
  **updates between snapshots**: each carries the handful of cells the mower
  just cut — the same signal the app whitens its ring with. They only
  **sample** the cut, though: ~5 cells every couple of seconds while the
  mower drives ~14 in between (a one-cell update arrives as an anchor with
  no chain), so the integration also cuts the lap between one update and
  the next (`map_geometry.trail_cells`: consecutive updates snapped to the
  announced ring, the short way round, never over more than 5 m) — eroding
  by the updates alone left a sliver between every two and the ring drew
  dashed all round. The accumulated cells erode every composed border
  (`map_geometry.erode_border`; a run under 1 m hemmed in by cut on both
  sides is dropped as sampling noise), plus a ratchet: any cell that ever
  left the published border stays cut. That also covers the mower
  re-announcing the full planned ring on reconnection mid-job — the
  announcement lands and the cut cells are rubbed straight back out. When
  the job closes, the whole layer (lanes, border, cut cells) is cleared.
* a field with an id but no coordinates means that lane is finished.

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
- `getCleanInfo`'s `trigger` says **why** the job is in its current state:
  `app` (app/HA), `screen` (the mower's own panel), `sched`, `lowBattery`
  (parked mid-job to recharge — the job stays `state: clean` with
  `motionState: pause`), `continue` (picking an interrupted job back up),
  `workComplete`, `alert`. The reference mower resumes at **80%** battery.
- `workComplete` + `state: idle` means the job finished AND the mower is about
  to drive itself home — it stays silent for the whole ride (measured 48 s
  after an edge trim) until `onChargeState` reports the dock, so map that
  push to "returning", not "idle".
- `getBreakPointStatus.continueLeftTime` reads 0 even mid-interruption; it is
  not a countdown to resuming.
- The mower re-broadcasts its map geometry (`onMI`, `onArI`) when it sees the
  **app-presence MQTT session connect** — the connect edge, not the connected
  state. Opening the official app produces that edge naturally; an integration
  holding a session open from before the job produces none. Cycling the
  presence session at job start reproduces it.
- `clean` acts (`start` / `resume` / `pause` / `stop`) are matched against the
  **currently open job type**: an act whose `content.type` does not match is
  answered `code 0, msg ok` and **silently ignored**. A stop typed `auto`
  cannot end a `borderrotate` job; the type of the running job must be sent.
- `setChildLock` is accepted but ignored by the O1200 firmware.
- When a start is blocked (rain / animal protection), the mower accepts the
  command, drives for ~5 s, and returns on its own — that is not an error.
- The MQTT broker's certificate does not pass standard verification; the
  integration attempts verified TLS first and falls back explicitly with a
  warning.
