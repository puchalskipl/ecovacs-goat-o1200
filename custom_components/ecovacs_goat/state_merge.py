"""Three-way merge protecting pushed state from stale refresh publishes.

The coordinator's background refreshers snapshot the current state, spend
seconds (sometimes minutes) awaiting HTTP calls, layer the responses onto
that snapshot, and publish the whole object. Every field a push updated in
the meantime silently reverts. Observed live: the session progress sawing
5→6→5→6→7 as refreshes republished pre-push values, the status tile
bouncing for a minute after a job ended, and stale ``activity`` publishes
reopening phantom jobs.

MQTT pushes apply synchronously — no awaits between reading the state and
publishing — so they are always fresh. That asymmetry decides the merge
rule: for every field, if the live state moved away from the refresher's
base while it was awaiting, a push moved it, and the push wins; otherwise
the refreshed value goes through, which is how a refresh still contributes
the fields only it polls (settings, lifespans, network, totals).

Kept free of Home Assistant imports so the behaviour is testable exactly as
it runs (same precedent as ``map_geometry``).
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import Any

from .mower_models import MowerState

# Nested dataclass fields merged one level deep, field by field. This is what
# lets getTotalStats land stats.total_* while onStats pushes are moving
# stats.progress in the same window. Anything below this level is a leaf
# compared whole — merging deeper could tear structures apart (for example
# combining a pushed x with a polled y inside one coordinate).
_NESTED_MERGE_FIELDS = frozenset(
    {"network", "settings", "stats", "protections", "telemetry", "map"}
)


def merge_refreshed_state(
    base: MowerState | None,
    refreshed: MowerState,
    current: MowerState | None,
) -> MowerState:
    """Merge a refresher's result onto the state published in the meantime.

    ``base`` is the snapshot the refresher started from, ``refreshed`` what it
    built by applying HTTP responses to that snapshot, ``current`` the live
    state at publish time. Per field: a value that moved between ``base`` and
    ``current`` was moved by a push and stays; everything else takes the
    refreshed value.

    Two deliberate exceptions:

    * ``available`` is always ``refreshed.available`` — the merge runs after a
      successful refresh, which is itself proof of reachability.
    * ``raw`` is a per-key union: the refresh contributes its response
      payloads, but keys a push updated during the awaits win. Diagnostics
      should describe what actually stands.
    """
    if base is None or current is None or current is base:
        return refreshed

    merged: dict[str, Any] = {}
    for field in fields(MowerState):
        name = field.name
        base_value = getattr(base, name)
        refreshed_value = getattr(refreshed, name)
        current_value = getattr(current, name)
        if name == "available":
            merged[name] = refreshed_value
        elif name == "raw":
            merged[name] = {
                **refreshed_value,
                **{
                    key: value
                    for key, value in current_value.items()
                    if base_value.get(key) != value
                },
            }
        elif name in _NESTED_MERGE_FIELDS and _same_dataclass(
            base_value, refreshed_value, current_value
        ):
            merged[name] = _merge_dataclass_fields(
                base_value, refreshed_value, current_value
            )
        else:
            merged[name] = (
                current_value if current_value != base_value else refreshed_value
            )
    return replace(refreshed, **merged)


def changed_field_names(a: MowerState, b: MowerState) -> tuple[str, ...]:
    """Name the top-level fields on which two states differ."""
    return tuple(
        field.name
        for field in fields(MowerState)
        if getattr(a, field.name) != getattr(b, field.name)
    )


def _same_dataclass(*values: Any) -> bool:
    """Return whether all values are instances of one dataclass type."""
    first = values[0]
    return is_dataclass(first) and all(type(v) is type(first) for v in values)


def _merge_dataclass_fields(base: Any, refreshed: Any, current: Any) -> Any:
    """Apply the leaf merge rule to each field of one nested dataclass."""
    merged: dict[str, Any] = {}
    for field in fields(base):
        base_value = getattr(base, field.name)
        refreshed_value = getattr(refreshed, field.name)
        current_value = getattr(current, field.name)
        merged[field.name] = (
            current_value if current_value != base_value else refreshed_value
        )
    return replace(refreshed, **merged)
