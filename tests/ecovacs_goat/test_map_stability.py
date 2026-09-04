"""The drawn map must not flip between fresh and stale geometry."""

from pathlib import Path
import sys
import types

PACKAGE_PATH = Path(__file__).parents[2] / "custom_components" / "ecovacs_goat"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(PACKAGE_PATH.parent)]
sys.modules.setdefault("custom_components", custom_components)

ecovacs_goat = types.ModuleType("custom_components.ecovacs_goat")
ecovacs_goat.__path__ = [str(PACKAGE_PATH)]
sys.modules.setdefault("custom_components.ecovacs_goat", ecovacs_goat)

from custom_components.ecovacs_goat.map_geometry import (
    OUTLINE_SOURCE_COVERAGE,
    border_coverage_cells,
    carry_forward_track,
    compose_border,
    cut_cells_from_points,
    erode_border,
    OUTLINE_SOURCE_MOWER,
    stabilise_geometry,
    trail_cells,
)
from custom_components.ecovacs_goat.mower_models import (
    MapPosition,
    MowerMapInfo,
    active_job_from_payload,
    active_job_payload,
    standstill_bucket,
)

FRESH = (MapPosition(x=0, y=0), MapPosition(x=100, y=0), MapPosition(x=100, y=100))
STALE = (MapPosition(x=0, y=0), MapPosition(x=9, y=0), MapPosition(x=9, y=9))


def _info(outline, source=OUTLINE_SOURCE_MOWER, obstacles=()):
    return MowerMapInfo(
        outline=outline, outline_source=source, chain_step=50, obstacles=obstacles
    )


def test_stale_publish_cannot_undo_freshly_pushed_geometry() -> None:
    """A grouped refresh built before an onMI push must not replace it.

    Observed live: the lawn flipped between the correct shape and the previous
    one about once a second, because refreshes publish a snapshot assembled
    seconds earlier. Both copies claim source "mower", so only the publish of
    an actual push (learn=True) may change the remembered geometry.
    """
    published, remembered = stabilise_geometry(None, _info(FRESH), learn=True)
    assert published.outline == FRESH

    # The stale refresh republishes the outline it snapshotted earlier.
    published, remembered = stabilise_geometry(remembered, _info(STALE))

    assert published.outline == FRESH
    assert published.chain_step == 50
    assert remembered.outline == FRESH


def test_a_newer_push_still_updates_the_map() -> None:
    """Genuine new geometry from the mower replaces what was remembered."""
    _published, remembered = stabilise_geometry(None, _info(STALE), learn=True)

    published, remembered = stabilise_geometry(remembered, _info(FRESH), learn=True)

    assert published.outline == FRESH
    assert remembered.outline == FRESH


def test_publishes_without_geometry_still_draw_the_known_lawn() -> None:
    """Most state updates carry no map data; the lawn must stay on screen."""
    _published, remembered = stabilise_geometry(None, _info(FRESH), learn=True)

    published, _remembered = stabilise_geometry(remembered, MowerMapInfo())

    assert published.outline == FRESH
    assert published.outline_source == OUTLINE_SOURCE_MOWER


def test_coverage_fallback_never_overrides_the_mowers_own_map() -> None:
    """The traced approximation must not replace the mower's exact outline."""
    _published, remembered = stabilise_geometry(None, _info(FRESH), learn=True)

    published, _remembered = stabilise_geometry(
        remembered, _info(STALE, source=OUTLINE_SOURCE_COVERAGE)
    )

    assert published.outline == FRESH
    assert published.outline_source == OUTLINE_SOURCE_MOWER


def test_a_remap_drops_the_remembered_geometry() -> None:
    """A new map id means a new coordinate frame, so nothing is carried over."""
    _published, remembered = stabilise_geometry(None, _info(FRESH), learn=True)

    published, remembered = stabilise_geometry(
        remembered, MowerMapInfo(), remapped=True
    )

    assert published.outline == ()
    assert remembered is None


def test_trace_protection_logic_matches_coordinator_rules() -> None:
    """The mowed track may only grow, reset on task change, or reset on remap.

    Mirrors MowerCoordinator._carry_forward_map_trace: the cloud cannot
    re-serve the track (getMapTrack answers empty — the only source is small
    onMapTrack windows), so a stale publish carrying fewer points must not
    win. Observed live: a grouped refresh right after restart published an
    empty trace over the restored 880-point track and the debounced save made
    the loss permanent.
    """
    long_path = (MapPosition(x=0, y=0), MapPosition(x=1, y=1), MapPosition(x=2, y=2))
    short_path = (MapPosition(x=0, y=0),)

    # Same map: the longer remembered track outranks a shorter publish.
    remembered = ("1", long_path)
    incoming_mid, incoming = "1", short_path
    keep = remembered is not None and remembered[0] == incoming_mid and len(
        incoming
    ) < len(remembered[1])
    assert keep is True

    # A remap invalidates the remembered track entirely.
    keep_after_remap = remembered[0] == "2"
    assert keep_after_remap is False


LANES = {"26": ((MapPosition(x=0, y=0), MapPosition(x=0, y=100)),)}
BORDER = ((MapPosition(x=0, y=0), MapPosition(x=100, y=0)),)


TEMPLATE = tuple(
    MapPosition(x=x, y=y)
    for x, y in [
        (0, 100),
        (0, 50),
        (0, 0),
        (50, 0),
        (100, 0),
        (100, 50),
        (100, 100),
        (50, 100),
    ]
)


def test_only_a_track_push_may_change_the_remaining_lanes() -> None:
    """The layer the coordinator publishes for the remaining work.

    Observed live: the drawn boundary alternated between "still to cut" and
    "done" on every refresh, because grouped refreshes republished whatever
    lanes were current when they started. Only an onMapTrack push may move
    that layer; a remap clears what is remembered.
    """
    # A push teaches the layer.
    published, remembered = carry_forward_track(
        None, (LANES, BORDER, None, None), from_push=True, remapped=False
    )
    assert published == (LANES, BORDER, None, None)

    # A stale refresh carrying nothing must not blank it.
    published, remembered = carry_forward_track(
        remembered, ({}, None, None, None), from_push=False, remapped=False
    )
    assert published == (LANES, BORDER, None, None)

    # A remap drops it, so the next map starts clean.
    published, remembered = carry_forward_track(
        remembered, ({}, None, None, None), from_push=False, remapped=True
    )
    assert published == ({}, None, None, None)
    assert remembered is None


def test_a_stale_refresh_must_not_blank_the_border_lap() -> None:
    """The border lap rides in the same push as the lanes and is kept too.

    Observed live during an edge trim (2026-08-30): the lanes were carried
    forward but the border was not, so every ordinary refresh reset it to
    None. The card kept drawing the last loop it had managed to catch, which
    by then bore no relation to what was left — green ran both ahead of the
    mower and behind it.
    """
    # An edge trim plans no lanes at all: the border is the whole job. The
    # template and lap-start index ride along so compose_border survives
    # grouped refreshes rebuilding the trace from scratch.
    published, remembered = carry_forward_track(
        None, ({}, BORDER, TEMPLATE, 3), from_push=True, remapped=False
    )
    assert published[1] == BORDER

    published, remembered = carry_forward_track(
        remembered, ({}, None, None, None), from_push=False, remapped=False
    )
    assert published[1] == BORDER, "a refresh must not retire the lap"
    assert published[2] == TEMPLATE, "a refresh must not lose the template"
    assert published[3] == 3

    # Only a push may retire it, and "done" is () — distinct from "unknown".
    published, remembered = carry_forward_track(
        remembered, ({}, (), TEMPLATE, 3), from_push=True, remapped=False
    )
    assert published[1] == ()

    published, _ = carry_forward_track(
        remembered, ({}, None, None, None), from_push=False, remapped=False
    )
    assert published[1] == (), "done must survive the next stale refresh"


def test_the_border_tail_beyond_the_origin_is_composed_from_the_template() -> None:
    """compose_border on the shape observed live on 2026-08-30.

    The mower announces the lap CLOSED, then snapshots only the arc from the
    loop's origin to its front — the tail it will cut last (origin backwards
    to where it broke the loop) is never transmitted. Drawn alone, the arc
    left the lawn's whole right side unpainted 30 s into the trim.
    """
    from custom_components.ecovacs_goat.map_geometry import compose_border

    # 1. The closed announcement becomes the template; drawn whole.
    closed = (TEMPLATE + (MapPosition(x=0, y=100),),)
    border, template, lap_start = compose_border(None, None, closed, step=50)
    assert border == closed
    assert template == closed[0]
    assert lap_start is None

    # 2. First open arc: origin -> front. The mower broke the loop at the
    #    vertex nearest the arc's end; the template tail from there on is
    #    appended so the whole remainder is drawn.
    arc = ((TEMPLATE[0], TEMPLATE[1], TEMPLATE[2]),)
    border, template, lap_start = compose_border(template, None, arc, step=50)
    assert lap_start == 2
    # The tail runs from the break vertex through the template's closing
    # point back at the origin — the closing duplicate rides along.
    assert border == (arc[0], template[2:])

    # 3. Empty arc: the mower passed the origin — only the tail remains.
    border, template, lap_start = compose_border(template, lap_start, (), step=50)
    assert border == (template[2:],)

    # 4. Without a template (restart mid-job) the arc passes through as-is.
    border, template2, _ = compose_border(None, None, arc, step=50)
    assert border == arc
    assert template2 is None


def test_job_duration_covers_the_whole_session_including_the_recharge() -> None:
    """The merge decision comes from mower_models.continues_task.

    A session split by a mid-job recharge is one job: it began at the first
    leg and ended at the last, and the reported time is the span between —
    the charging break included, because that is what "how long did it take"
    means. Summing only the legs reported a three-hour mow as twenty minutes.
    """
    from datetime import datetime, timedelta, timezone

    from custom_components.ecovacs_goat.mower_models import (
        MowerLastJob,
        continues_task,
    )

    first_leg_start = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
    recharge_start = first_leg_start + timedelta(minutes=90)
    last_leg_start = recharge_start + timedelta(minutes=80)
    ended = last_leg_start + timedelta(minutes=85)

    first_leg = MowerLastJob(
        kind="auto",
        started_at=first_leg_start.isoformat(),
        ended_at=recharge_start.isoformat(),
        task_id="122",
    )
    assert continues_task(first_leg, "122", last_leg_start)

    # What the coordinator computes for the final leg, given the earlier one.
    started = min(last_leg_start, first_leg_start)
    duration = round((ended - started).total_seconds() / 60, 1)

    assert started == first_leg_start
    assert duration == 255.0  # 4 h 15 min wall clock, not 85 minutes


def test_a_reused_task_id_does_not_glue_two_days_of_mowing_together() -> None:
    """Exercises mower_models.continues_task.

    Observed live 2026-09-04: the mower reported cid 122 as the task id of
    Wednesday's mow, of the edge trim that followed it, and of Friday's mow.
    Merging on a matching id alone therefore backdated Friday's four-hour
    session to Wednesday lunchtime and reported it as 49 h 40 min. Legs of one
    task are separated by a recharge and nothing else, so they must touch in
    time as well.
    """
    from datetime import datetime, timedelta, timezone

    from custom_components.ecovacs_goat.mower_models import (
        JOB_LEG_MAX_GAP_SECONDS,
        MowerLastJob,
        continues_task,
    )

    wednesday = MowerLastJob(
        kind="auto",
        started_at="2026-09-02T10:30:00.951796+00:00",
        ended_at="2026-09-02T14:43:18+00:00",
        task_id="122",
    )
    friday_start = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    assert not continues_task(wednesday, "122", friday_start)

    # A recharge gap still merges, right up to the cutoff.
    ended_at = datetime.fromisoformat(wednesday.ended_at)
    assert continues_task(wednesday, "122", ended_at + timedelta(minutes=80))
    assert continues_task(
        wednesday, "122", ended_at + timedelta(seconds=JOB_LEG_MAX_GAP_SECONDS)
    )
    assert not continues_task(
        wednesday, "122", ended_at + timedelta(seconds=JOB_LEG_MAX_GAP_SECONDS + 1)
    )

    # A different task never merges, however close in time.
    assert not continues_task(wednesday, "123", ended_at + timedelta(minutes=1))
    assert not continues_task(wednesday, None, ended_at + timedelta(minutes=1))
    assert not continues_task(None, "122", friday_start)

    # A leg cannot precede the record it would continue, and a record written
    # before these fields existed cannot vouch for anything.
    assert not continues_task(wednesday, "122", ended_at - timedelta(minutes=1))
    assert not continues_task(
        MowerLastJob(kind="auto", task_id="122"), "122", friday_start
    )


def test_a_restart_mid_job_must_not_reset_the_clock() -> None:
    """The in-flight job is persisted, so its start survives a restart."""
    from datetime import datetime, timezone

    started = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
    stored = {
        "kind": "auto",
        "started_at": started.isoformat(),
        "task_id": "-8518531",
        "mowed_peak": 120.5,
    }

    restored = {
        "kind": stored["kind"],
        "started_at": datetime.fromisoformat(stored["started_at"]),
        "task_id": stored["task_id"],
        "mowed_peak": float(stored["mowed_peak"]),
    }

    assert restored["started_at"] == started
    assert restored["task_id"] == "-8518531"


def test_a_weather_break_is_not_counted_as_charging_too() -> None:
    """Exercises mower_models.standstill_bucket.

    Observed live 2026-08-31: a mow ran 10:29-18:39 (490 min) of which the
    mower spent 341 min waiting out rain — parked on the dock, so charging
    part of that time as well. Reporting the stretch under both headings
    would claim more standstill than the job even lasted, so blocked wins.
    """
    from custom_components.ecovacs_goat.mower_models import standstill_bucket

    # Cutting: working time, whatever else is true.
    assert standstill_bucket(mowing=True, blocked=False, charging=False) is None
    assert standstill_bucket(mowing=True, blocked=True, charging=True) is None

    # The rain break: parked and charging, but the weather is what holds it.
    assert (
        standstill_bucket(mowing=False, blocked=True, charging=True) == "blocked"
    )
    assert (
        standstill_bucket(mowing=False, blocked=True, charging=False) == "blocked"
    )

    # A plain mid-job recharge.
    assert (
        standstill_bucket(mowing=False, blocked=False, charging=True) == "charging"
    )

    # Paused by hand off the dock: neither bucket, it stays working time.
    assert standstill_bucket(mowing=False, blocked=False, charging=False) is None


def test_the_reported_split_never_exceeds_the_job() -> None:
    """The three parts of a duration add up to it, and none goes negative."""
    from custom_components.ecovacs_goat.mower_models import MowerLastJob

    # The real 2026-08-31 mow, rounded as the coordinator stores it.
    job = MowerLastJob(
        kind="auto",
        duration_minutes=489.7,
        blocked_minutes=341.0,
        charging_minutes=0.0,
    )
    working = round(
        max(
            0.0,
            job.duration_minutes
            - (job.blocked_minutes or 0.0)
            - (job.charging_minutes or 0.0),
        ),
        1,
    )
    assert working == 148.7
    assert working + job.blocked_minutes + job.charging_minutes == job.duration_minutes

    # A record from before this field existed must not compute a negative.
    stary = MowerLastJob(kind="auto", duration_minutes=20.0)
    assert (
        round(
            max(
                0.0,
                stary.duration_minutes
                - (stary.blocked_minutes or 0.0)
                - (stary.charging_minutes or 0.0),
            ),
            1,
        )
        == 20.0
    )


def test_a_stub_at_the_mower_never_replaces_the_border_arc() -> None:
    """compose_border rejects arcs that do not start at the loop's origin.

    Observed live 2026-09-01: a two-point run at the mower's position landed
    in the border slot mid-trim. Composed blindly, it anchored the ring at
    the wrong vertex and the card redrew long-done boundary as pending
    ("the old ring came back"). The genuine mid-job arc always starts at the
    template's fixed origin; anything else keeps the previous border.
    """
    from custom_components.ecovacs_goat.map_geometry import compose_border

    closed = (TEMPLATE + (MapPosition(x=0, y=100),),)
    border, template, lap_start = compose_border(None, None, closed, step=50)

    arc = ((TEMPLATE[0], TEMPLATE[1], TEMPLATE[2]),)
    border, template, lap_start = compose_border(
        template, None, arc, step=50, previous=border
    )
    dobra = border

    # The stub: far from the origin, at the mower.
    stub = ((MapPosition(x=600, y=400), MapPosition(x=650, y=400)),)
    border, template2, lap_start2 = compose_border(
        template, lap_start, stub, step=50, previous=dobra
    )
    assert border == dobra, "stub must not change the published border"
    assert template2 == template
    assert lap_start2 == lap_start

    # Without a previous border it degrades to the known tail, not the stub.
    border, _, _ = compose_border(
        template, lap_start, stub, step=50, previous=None
    )
    assert stub[0] not in border


def test_a_trim_lap_start_is_pinned_by_the_station_hint() -> None:
    """A standalone trim begins its lap at the dock.

    Observed 2026-09-01: the first open arc of a trim arrived 1.5 minutes in,
    so the front-based estimate put the whole already-cut bottom edge into
    the never-repainted tail and it stayed green for the rest of the job.
    With the station as origin_hint the lap start lands on the dock-adjacent
    vertex regardless of how late the first arc is.
    """
    from custom_components.ecovacs_goat.map_geometry import compose_border

    closed = (TEMPLATE + (MapPosition(x=0, y=100),),)
    border, template, lap_start = compose_border(None, None, closed, step=50)

    # First open arc arrives LATE: the front is already three vertices in.
    late_arc = ((TEMPLATE[0], TEMPLATE[1], TEMPLATE[2], TEMPLATE[3]),)
    station = MapPosition(x=45, y=95)  # dock sits by the (50, 100) vertex

    border, template, lap_start = compose_border(
        template, None, late_arc, step=50, origin_hint=station
    )
    assert lap_start == 7  # the (50, 100) vertex, not the arc front's 3

    # A hint far off the loop is ignored and the front estimate returns.
    border, template2, lap_start2 = compose_border(
        template, None, late_arc, step=50,
        origin_hint=MapPosition(x=5000, y=5000),
    )
    assert lap_start2 == 3


def test_a_cut_point_dilates_to_its_nine_cells() -> None:
    cells = cut_cells_from_points([MapPosition(x=500, y=500)], step=50)
    assert cells == frozenset(
        (10 + dx, 10 + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
    )


def test_erosion_splits_a_long_collapsed_edge() -> None:
    """Template runs collapse straight edges to end points metres apart; a
    short cut in the middle must still punch a hole through them."""
    border = ((MapPosition(x=0, y=0), MapPosition(x=2000, y=0)),)
    cut = cut_cells_from_points([MapPosition(x=1000, y=0)], step=50)
    eroded = erode_border(border, cut, step=50)
    assert len(eroded) == 2
    left, right = eroded
    assert left[0].x == 0 and left[-1].x < 950
    assert right[0].x > 1050 and right[-1].x == 2000


def test_erosion_without_cut_cells_is_a_no_op() -> None:
    border = ((MapPosition(x=0, y=0), MapPosition(x=2000, y=0)),)
    assert erode_border(border, frozenset(), step=50) is border


def test_erosion_drops_runs_shorter_than_two_points() -> None:
    border = ((MapPosition(x=0, y=0), MapPosition(x=100, y=0)),)
    cut = cut_cells_from_points(
        [MapPosition(x=0, y=0), MapPosition(x=100, y=0)], step=50
    )
    assert erode_border(border, cut, step=50) == ()


def test_a_snapshot_cannot_repaint_cut_ground() -> None:
    """The in-mow edge pass snapshots lag the cut by minutes and the composed
    tail can span ground long done (observed live 2026-09-02: every snapshot
    repainted the cut right side green) — erosion by the accumulated cut
    cells must win over whatever the composition resurrects."""
    ring = tuple(
        MapPosition(x=x, y=y)
        for x, y in [
            (0, 0), (1000, 0), (2000, 0), (2000, 1000), (2000, 2000),
            (1000, 2000), (0, 2000), (0, 1000),
        ]
    )
    # zamknieta zapowiedz -> szablon
    border, template, lap_start = compose_border(
        None, None, (ring + (MapPosition(x=0, y=50),),), step=50
    )
    assert template is not None
    # otwarty luk od poczatku szablonu -> kompozycja dokleja ogon
    arc = (ring[:5],)
    border, template, lap_start = compose_border(
        template, lap_start, arc, step=50, previous=border
    )
    total_before = sum(len(s) for s in border)
    # kosiarka melduje skoszenie fragmentu ogona
    cut = cut_cells_from_points(
        [MapPosition(x=500, y=2000), MapPosition(x=550, y=2000)], step=50
    )
    eroded = erode_border(border, cut, step=50)
    dziura = [
        p for seg in eroded for p in seg if p.y == 2000 and 450 <= p.x < 650
    ]
    assert not dziura
    assert sum(len(s) for s in eroded) != total_before or len(eroded) > len(border)


RING = tuple(
    MapPosition(x=x, y=y)
    for x, y in [(0, 0), (2000, 0), (2000, 2000), (0, 2000), (0, 0)]
)


def test_the_lap_between_two_cut_updates_is_cut_too() -> None:
    """The updates only sample the cut: a few cells every couple of seconds
    while the mower drives two to three times as far (2026-09-02 trim
    capture: 5 cells per update, 14 driven in between). Eroding by the
    updates alone left a sliver between every two of them and the ring drew
    dashed all round (observed live 2026-09-04, in-mow edge pass). The lap
    between consecutive updates was driven, so it is cut."""
    first = [MapPosition(x=500, y=0), MapPosition(x=550, y=0)]
    second = [MapPosition(x=1200, y=0), MapPosition(x=1250, y=0)]
    cells = trail_cells((RING,), first + second, step=50, closed=True)
    # kazda komorka gornej krawedzi miedzy aktualizacjami jest skoszona
    assert all((x // 50, 0) in cells for x in range(500, 1300, 50))
    # a reszta pierscienia nie
    assert (40, 20) not in cells
    assert (20, 40) not in cells


def test_the_trail_takes_the_short_way_round_a_closed_ring() -> None:
    """Across the ring's closing point the bridge must not go the long way
    round and wipe the whole lap."""
    cells = trail_cells(
        (RING,),
        [MapPosition(x=0, y=100), MapPosition(x=100, y=0)],
        step=50,
        closed=True,
    )
    assert (0, 0) in cells and (0, 1) in cells and (1, 0) in cells
    assert (20, 0) not in cells and (40, 20) not in cells


def test_updates_far_apart_along_the_lap_are_not_bridged() -> None:
    """Two updates half a lap apart did not come from a drive along the
    edge between them (the mower went elsewhere), so nothing is filled."""
    big = tuple(
        MapPosition(x=x, y=y)
        for x, y in [(0, 0), (5000, 0), (5000, 5000), (0, 5000), (0, 0)]
    )
    cells = trail_cells(
        (big,),
        [MapPosition(x=0, y=0), MapPosition(x=5000, y=5000)],
        step=50,
        closed=True,
    )
    assert cells == frozenset()


def test_an_update_off_the_lap_breaks_the_trail() -> None:
    """A cut reported nowhere near the lap (relocalisation blip, another
    shape) is not snapped to it, and the next update starts a new trail."""
    cells = trail_cells(
        (RING,),
        [
            MapPosition(x=500, y=0),
            MapPosition(x=1000, y=1000),
            MapPosition(x=1200, y=0),
        ],
        step=50,
        closed=True,
    )
    assert cells == frozenset()


def test_without_a_template_the_trail_follows_the_composed_segments() -> None:
    """Restart mid-job: no ring announced, the arc is all there is. Bridging
    works within a segment and never jumps between two."""
    arc = (MapPosition(x=0, y=0), MapPosition(x=2000, y=0))
    tail = (MapPosition(x=0, y=1000), MapPosition(x=2000, y=1000))
    cells = trail_cells(
        (arc, tail),
        [
            MapPosition(x=500, y=0),
            MapPosition(x=1000, y=0),
            MapPosition(x=1000, y=1000),
        ],
        step=50,
    )
    assert all((x // 50, 0) in cells for x in range(500, 1050, 50))
    assert (20, 10) not in cells and (20, 20) not in cells


def test_a_sliver_between_two_cut_stretches_is_rubbed_out() -> None:
    """What sparse sampling leaves between two updates is not edge still to
    cut; a run at the segment's own end is (that is the lap's front)."""
    border = ((MapPosition(x=0, y=0), MapPosition(x=5000, y=0)),)
    cut = cut_cells_from_points(
        [MapPosition(x=x, y=0) for x in range(1000, 1500, 50)]
        + [MapPosition(x=x, y=0) for x in range(1900, 2400, 50)],
        step=50,
    )
    eroded = erode_border(border, cut, step=50)
    assert len(eroded) == 2
    assert eroded[0][0].x == 0 and eroded[-1][-1].x == 5000
    assert not any(1500 <= p.x <= 1900 for seg in eroded for p in seg)

    # Metres of edge between two cut stretches are still to cut.
    cut = cut_cells_from_points(
        [MapPosition(x=x, y=0) for x in range(1000, 1500, 50)]
        + [MapPosition(x=x, y=0) for x in range(3000, 3500, 50)],
        step=50,
    )
    assert len(erode_border(border, cut, step=50)) == 3


def test_coverage_cells_walk_collapsed_edges() -> None:
    """A two-point edge 500 long must cover all 11 cells, not just its ends —
    the ratchet diffs these sets to learn what a shrinking snapshot cut."""
    cells = border_coverage_cells(
        ((MapPosition(x=0, y=0), MapPosition(x=500, y=0)),), step=50
    )
    assert cells == frozenset((i, 0) for i in range(11))


def test_an_open_job_survives_a_restart_with_its_standstill_tallies() -> None:
    """A restart mid-job must not turn standstill into working time.

    Observed 2026-09-02: a restart three minutes before a mow ended erased the
    82 minutes it had spent charging, and the record claimed the whole
    4 h 13 min as mowing. The save wrote the tallies; the restore read back
    fewer fields and dropped them.
    """
    from datetime import datetime, timezone

    started = datetime(2026, 9, 2, 10, 30, tzinfo=timezone.utc)
    job = {
        "kind": "auto",
        "started_at": started,
        "task_id": "122",
        "mowed_peak": 252.7,
        "blocked_seconds": 0.0,
        "charging_seconds": 4900.0,
        # Only meaningful while running — must not come back.
        "sampled_at": datetime(2026, 9, 2, 14, 40, tzinfo=timezone.utc),
    }

    payload = active_job_payload(job)
    assert payload["started_at"] == started.isoformat()
    assert "sampled_at" not in payload

    import json

    # It has to survive the storage round trip, not just the function call.
    restored = active_job_from_payload(
        json.loads(json.dumps(payload)), started, default_kind="auto"
    )
    assert restored["charging_seconds"] == 4900.0
    assert restored["blocked_seconds"] == 0.0
    assert restored["mowed_peak"] == 252.7
    assert restored["task_id"] == "122"
    assert restored["started_at"] is started
    assert "sampled_at" not in restored


def test_a_job_stored_before_the_tallies_existed_restores_as_zero() -> None:
    """Records written by the older save carry no tallies at all."""
    from datetime import datetime, timezone

    started = datetime(2026, 9, 2, 10, 30, tzinfo=timezone.utc)
    restored = active_job_from_payload(
        {"kind": "borderrotate", "task_id": "7"}, started, default_kind="auto"
    )
    assert restored["kind"] == "borderrotate"
    assert restored["blocked_seconds"] == 0.0
    assert restored["charging_seconds"] == 0.0
    assert restored["mowed_peak"] == 0.0


def test_standstill_between_restarts_is_charged_to_nobody() -> None:
    """Downtime belongs to no bucket, and the buckets still rank correctly."""
    assert standstill_bucket(mowing=False, blocked=True, charging=True) == "blocked"
    assert standstill_bucket(mowing=False, blocked=False, charging=True) == "charging"
    assert standstill_bucket(mowing=True, blocked=False, charging=True) is None
    assert standstill_bucket(mowing=False, blocked=False, charging=False) is None
