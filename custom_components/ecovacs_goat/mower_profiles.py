"""Per-model capability profiles for GOAT mowers.

Two protocol families are supported: the **GOAT G1 line** (G1, G1-800,
G1-1600, G1-2000; UWB beacons, ``*_V2`` map dialect) and the **GOAT O-series**
(O800 RTK, O1200, O1200 LiDAR Pro; RTK/LiDAR positioning). The families
differ in a few concrete, important ways:

============  ===========================  ==============================
Aspect        GOAT G1 line                 GOAT O-series
============  ===========================  ==============================
Clean cmd     ``clean_V2``                 ``clean``
Stop body     ``content.type = ""``        ``content.type = "auto"``
Clean info    ``getCleanInfo_V2``          ``getCleanInfo`` (same fields)
Position      ``deebotPos/chargePos/uwb``  ``deebotPos/chargePos/rtkPos``
Map dialect   ``getMapInfo_V2`` /          ``getMapState`` / ``getMI`` /
              ``getMapTrace_V2``           ``getMapTrack`` / ``getAreaSet``
============  ===========================  ==============================

Dock (``charge {act:"go"}``), ``appping``, ``getLifeSpan``, battery, error, and
the ``getCleanInfo`` *status fields* (``state`` / ``cleanState.motionState`` /
``trigger`` / ``cid``) are shared, so the existing status parser handles both.

The O-series map geometry was validated against a live-mowing capture of a
**GOAT O1200 LiDAR Pro** (firmware 2.13.10, 2026-08): ``onMapTrack`` pushes
carry compact-LZMA windows of the mowed track, ``onArI`` carries chain-coded
zone boundaries, and ``onAreaParameter`` carries per-zone mowing parameters
(cutting height level) — all decoded in ``mower_messages``. One protocol
quirk matters: O-series position pushes report the placeholder map id ``"0"``
while map replies (``onMI``) carry the real id, so ``"0"`` must never be
treated as a map switch (see ``_reset_map_on_id_change``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .goat_models import (
    FAMILY_G1,
    FAMILY_O_SERIES,
    FAMILY_UNKNOWN,
    classify_goat_family,
)


class MowerFamily(StrEnum):
    """Coarse GOAT protocol family."""

    GOAT_G1 = FAMILY_G1
    GOAT_O_SERIES = FAMILY_O_SERIES
    UNKNOWN = FAMILY_UNKNOWN


class MapDialect(StrEnum):
    """Map command dialect a mower understands."""

    MAP_V2 = "map_v2"
    MAP_RTK = "map_rtk"


@dataclass(frozen=True)
class CapabilityProfile:
    """Static, per-model protocol capabilities.

    These values seed the coordinator's runtime
    :class:`~.mower_compat.ProtocolProfile` and pick the command dialect. They
    never *enable* a command a mower rejects; the runtime profile still adapts on
    failures. They only avoid issuing commands a family is known not to speak.
    """

    family: MowerFamily
    map_dialect: MapDialect
    map_uses_v2: bool
    # N-GIoT command used to start/pause/resume/stop mowing.
    clean_command: str
    # ``content.type`` sent with a clean "stop" (G1 uses "", O-series "auto").
    stop_content_type: str
    # Whether every clean act carries ``content:{type:"auto"}`` (O-series) or
    # only start/stop do (G1 line).
    clean_always_content: bool
    # Grouped getInfo key used to read mowing status.
    clean_info_command: str
    # Fields requested via getPos (UWB vs RTK reference points).
    position_fields: tuple[str, ...]
    # setCutEfficiency levels for this family, keyed by mowing-speed option.
    cut_efficiency_levels: dict[str, int]
    experimental: bool
    label: str

    def clean_body(self, act: str, job_type: str | None = None) -> dict[str, Any]:
        """Return the ``clean`` / ``clean_V2`` body for a mowing action.

        ``act`` is one of ``start`` / ``resume`` / ``pause`` / ``stop``.

        ``job_type`` is the type of the job currently open on the mower
        (``auto`` for a mow, ``borderrotate`` for an edge trim). The mower
        matches ``content.type`` against the running job and silently ignores
        a mismatch: a stop typed ``auto`` during an edge trim is answered
        ``ok`` and does nothing (observed live 2026-08-30 — the trim could
        not be stopped from HA, only ``charge`` worked).
        """
        if self.clean_always_content:
            return {"act": act, "content": {"type": job_type or "auto"}}
        if act == "start":
            return {"act": "start", "content": {"type": job_type or "auto"}}
        if act == "stop":
            return {
                "act": "stop",
                "content": {"type": job_type or self.stop_content_type},
            }
        return {"act": act}

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot for diagnostics."""
        return {
            "family": str(self.family),
            "map_dialect": str(self.map_dialect),
            "map_uses_v2": self.map_uses_v2,
            "clean_command": self.clean_command,
            "clean_info_command": self.clean_info_command,
            "position_fields": list(self.position_fields),
            "cut_efficiency_levels": dict(self.cut_efficiency_levels),
            "experimental": self.experimental,
            "label": self.label,
        }


# The G1 profile is the original, validated behaviour. UNKNOWN intentionally maps
# to the G1 profile so previously-working setups never regress: the runtime
# profile still adapts on failures, exactly as before this change.
_G1_PROFILE = CapabilityProfile(
    family=MowerFamily.GOAT_G1,
    map_dialect=MapDialect.MAP_V2,
    map_uses_v2=True,
    clean_command="clean_V2",
    stop_content_type="",
    clean_always_content=False,
    clean_info_command="getCleanInfo_V2",
    position_fields=("chargePos", "deebotPos", "uwbPos"),
    cut_efficiency_levels={"quick": 1, "delicate": 2},
    experimental=False,
    label="GOAT G1 line (UWB, V2 map)",
)

_O_SERIES_PROFILE = CapabilityProfile(
    family=MowerFamily.GOAT_O_SERIES,
    map_dialect=MapDialect.MAP_RTK,
    map_uses_v2=False,
    clean_command="clean",
    stop_content_type="auto",
    clean_always_content=True,
    clean_info_command="getCleanInfo",
    position_fields=("deebotPos", "chargePos"),
    # 4 = 0.5 m/s ("efficiency"), 7 = 0.35 m/s ("delicate").
    cut_efficiency_levels={"quick": 4, "delicate": 7},
    experimental=False,
    label="GOAT O-series (RTK/LiDAR, validated on O1200 LiDAR Pro)",
)

_UNKNOWN_PROFILE = CapabilityProfile(
    family=MowerFamily.UNKNOWN,
    map_dialect=MapDialect.MAP_V2,
    map_uses_v2=True,
    clean_command="clean_V2",
    stop_content_type="",
    clean_always_content=False,
    clean_info_command="getCleanInfo_V2",
    position_fields=("chargePos", "deebotPos", "uwbPos"),
    cut_efficiency_levels={"quick": 1, "delicate": 2},
    experimental=False,
    label="Unknown GOAT (assuming G1 / V2 map)",
)

_FAMILY_PROFILES: dict[MowerFamily, CapabilityProfile] = {
    MowerFamily.GOAT_G1: _G1_PROFILE,
    MowerFamily.GOAT_O_SERIES: _O_SERIES_PROFILE,
    MowerFamily.UNKNOWN: _UNKNOWN_PROFILE,
}


def profile_for_family(family: MowerFamily | str) -> CapabilityProfile:
    """Return the capability profile for a coarse family."""
    try:
        family = MowerFamily(family)
    except ValueError:
        family = MowerFamily.UNKNOWN
    return _FAMILY_PROFILES[family]


def profile_for_model(device_name: str | None) -> CapabilityProfile:
    """Return the capability profile for an ECOVACS ``deviceName``."""
    return profile_for_family(classify_goat_family(device_name))
