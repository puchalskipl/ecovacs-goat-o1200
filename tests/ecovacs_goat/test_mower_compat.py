"""Tests for eco-ng protocol compatibility helpers."""

from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock

import pytest

PACKAGE_PATH = Path(__file__).parents[2] / "custom_components" / "ecovacs_goat"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(PACKAGE_PATH.parent)]
sys.modules.setdefault("custom_components", custom_components)

ecovacs_goat = types.ModuleType("custom_components.ecovacs_goat")
ecovacs_goat.__path__ = [str(PACKAGE_PATH)]
sys.modules.setdefault("custom_components.ecovacs_goat", ecovacs_goat)

from custom_components.ecovacs_goat.mower_api import EcovacsApiError
from custom_components.ecovacs_goat.mower_compat import (
    GETINFO_UNSUPPORTED_FAILURE_THRESHOLD,
    ProtocolProfile,
    apply_resilient_getinfo_group,
    refresh_live_position,
    refresh_rtk_map,
)
from custom_components.ecovacs_goat.mower_models import MowerMap, MowerState


def _make_subset(obj) -> str:
    """Build an O-series ``subsets`` blob (base64 + compact LZMA wrapper)."""
    import base64
    import json
    import lzma

    raw = json.dumps(obj, separators=(",", ":")).encode()
    comp = lzma.compress(
        raw,
        format=lzma.FORMAT_RAW,
        filters=[
            {"id": lzma.FILTER_LZMA1, "dict_size": 0x40000, "lc": 3, "lp": 0, "pb": 2}
        ],
    )
    header = (
        bytes([0x5D]) + (0x40000).to_bytes(4, "little") + len(raw).to_bytes(4, "little")
    )
    return base64.b64encode(header + comp).decode()


@pytest.mark.asyncio
async def test_resilient_getinfo_keeps_single_failures_retryable() -> None:
    """A single per-command failure should not permanently disable the readback."""
    api = AsyncMock()
    device = object()
    api.control.side_effect = [
        EcovacsApiError("batch failed"),
        {"body": {"data": {"getBattery": {"data": {"value": 88, "isLow": 0}}}}},
        EcovacsApiError("unsupported"),
    ]

    state, profile = await apply_resilient_getinfo_group(
        api,
        device,
        MowerState(),
        ("getBattery", "getOta"),
        ProtocolProfile(),
    )

    assert state.battery == 88
    assert "getOta" not in profile.unsupported_getinfo
    assert profile.getinfo_failures["getOta"] == 1
    assert api.control.call_count == 3


@pytest.mark.asyncio
async def test_resilient_getinfo_marks_repeated_failures_unsupported() -> None:
    """Repeated per-command failures are eventually cached as unsupported."""
    api = AsyncMock()
    device = object()
    profile = ProtocolProfile()

    for _ in range(GETINFO_UNSUPPORTED_FAILURE_THRESHOLD):
        api.control.side_effect = [
            EcovacsApiError("batch failed"),
            EcovacsApiError("unsupported"),
        ]
        _, profile = await apply_resilient_getinfo_group(
            api,
            device,
            MowerState(),
            ("getOta",),
            profile,
        )

    assert "getOta" in profile.unsupported_getinfo


@pytest.mark.asyncio
async def test_resilient_getinfo_uses_clean_info_fallback_after_v2_disabled() -> None:
    """If getCleanInfo_V2 is cached unsupported, request getCleanInfo instead."""
    api = AsyncMock()
    device = object()
    profile = ProtocolProfile(unsupported_getinfo=frozenset({"getCleanInfo_V2"}))
    api.control.return_value = {
        "body": {
            "data": {
                "getCleanInfo": {"data": {"trigger": "none", "state": "idle"}}
            }
        }
    }

    state, _ = await apply_resilient_getinfo_group(
        api,
        device,
        MowerState(),
        ("getCleanInfo_V2",),
        profile,
    )

    assert state.raw["getCleanInfo"]["state"] == "idle"
    assert api.control.call_args_list[0][0][2] == ["getCleanInfo"]


@pytest.mark.asyncio
async def test_refresh_live_position_drops_uwb_on_error() -> None:
    """If getPos with uwbPos fails, retry without UWB and persist the narrower field set."""
    api = AsyncMock()
    device = object()
    api.control.side_effect = [
        EcovacsApiError("bad uwb field"),
        {"body": {"data": {"deebotPos": {"x": 1, "y": 2}}}},
    ]
    profile = ProtocolProfile()

    state, profile_out = await refresh_live_position(
        api, device, MowerState(), profile
    )

    assert state.map.current_position is not None
    assert state.map.current_position.x == 1
    assert "uwbPos" not in profile_out.get_pos_fields
    assert api.control.call_args_list[0][0][2] == ["chargePos", "deebotPos", "uwbPos"]
    assert api.control.call_args_list[1][0][2] == ["chargePos", "deebotPos"]


@pytest.mark.asyncio
async def test_refresh_rtk_map_fetches_layers_and_decodes() -> None:
    """O-series map refresh reads station + decodes vw/ar map-set layers."""
    api = AsyncMock()
    device = object()
    api.control.side_effect = [
        {"body": {"data": {"state": "built", "expandState": "none"}}},  # getMapState
        {
            "body": {
                "data": {
                    "result": 0,
                    "rtks": [{"x": 578, "y": 1997, "sn": "S1", "invalid": 0}],
                }
            }
        },  # getRTK
        {"body": {"data": {"mid": "1"}}},  # getMI
        {
            "body": {
                "data": {"mid": "1", "type": "vw", "subsets": "XQAABAACAAAAAC2XPAAAAA=="}
            }
        },  # getMapTrack (empty virtual walls)
        {
            "body": {
                "data": {
                    "mid": "1",
                    "aid": "0",
                    "type": "ar",
                    "subsets": _make_subset([["1", "1", "Lawn", "", "100", "200", "0-0"]]),
                }
            }
        },  # getAreaSet
    ]

    # Pre-seed the map id as the live position stream (getPos) would.
    state = await refresh_rtk_map(
        api, device, MowerState(map=MowerMap(mid="1"))
    )

    assert state.map.rtk_station is not None and state.map.rtk_station.x == 578
    assert state.map.no_go_zones == ()
    assert [p.as_dict() for p in state.map.areas] == [{"x": 100, "y": 200}]
    # Commands and the getAreaSet body must match the captured protocol.
    commands = [call.args[1] for call in api.control.call_args_list]
    assert commands == ["getMapState", "getRTK", "getMI", "getMapTrack", "getAreaSet"]
    assert api.control.call_args_list[4].args[2] == {
        "mid": "1",
        "aid": "0",
        "type": "ar",
    }


@pytest.mark.asyncio
async def test_refresh_rtk_map_skips_area_set_without_a_real_map_id() -> None:
    """The placeholder map id "0" returns an empty blob, so skip that query.

    O-series position replies always carry ``mid: "0"``; the real id arrives on
    the ``onMI`` push that ``getMI`` triggers, so the area layer waits for it.
    """
    api = AsyncMock()
    events: list[tuple[str, dict]] = []
    api.control.return_value = {"body": {"data": {}}}

    state = await refresh_rtk_map(
        api,
        object(),
        MowerState(map=MowerMap(mid="0")),
        capture=lambda name, data: events.append((name, data)),
    )

    commands = [call.args[1] for call in api.control.call_args_list]
    assert "getAreaSet" not in commands
    assert commands == ["getMapState", "getRTK", "getMI", "getMapTrack"]
    assert ("rtk_map_refresh_skipped", {"command": "getAreaSet", "mid": "0"}) in events
    assert state.map.areas == ()


@pytest.mark.asyncio
async def test_map_info_probe_tries_variants_until_accepted() -> None:
    """getMI payload variants are probed; the accepted one is reported."""
    api = AsyncMock()
    events: list[tuple[str, dict]] = []
    api.control.side_effect = [
        {"body": {"data": {"state": "built"}}},  # getMapState
        {"body": {"data": {"result": 1, "rtks": []}}},  # getRTK
        EcovacsApiError("no type"),  # getMI variant 1 rejected
        {"body": {"data": {"mid": "1"}}},  # getMI variant 2 accepted
        {"body": {"data": {}}},  # getMapTrack
        {"body": {"data": {"mid": "1", "type": "ar", "subsets": ""}}},  # getAreaSet
    ]

    state = await refresh_rtk_map(
        api,
        object(),
        MowerState(),
        capture=lambda name, data: events.append((name, data)),
    )

    assert state.map.mid == "1"
    accepted = [data for name, data in events if name == "map_info_payload_accepted"]
    assert accepted == [{"payload": {"mid": "1", "type": "-1"}}]
    # The learned id then unlocks the area-set query.
    assert api.control.call_args_list[-1].args[1] == "getAreaSet"


@pytest.mark.asyncio
async def test_refresh_rtk_map_survives_command_failures() -> None:
    """A failing RTK map command is logged/captured and does not abort the rest."""
    api = AsyncMock()
    device = object()
    events: list[tuple[str, dict]] = []
    api.control.side_effect = [
        EcovacsApiError("getMapState fail"),
        EcovacsApiError("getRTK fail"),
        EcovacsApiError("getMI fail"),
        EcovacsApiError("getMI fail"),
        EcovacsApiError("getMI fail"),
        EcovacsApiError("getMapTrack fail"),
        {
            "body": {
                "data": {
                    "type": "ar",
                    "subsets": _make_subset([["1", "1", "", "", "5", "6", "0-0"]]),
                }
            }
        },
    ]

    state = await refresh_rtk_map(
        api,
        device,
        MowerState(map=MowerMap(mid="2")),
        capture=lambda event, data: events.append((event, data)),
    )

    # The final successful call still applied despite earlier failures.
    assert [p.as_dict() for p in state.map.areas] == [{"x": 5, "y": 6}]
    error_events = [event for event, _ in events if event == "rtk_map_refresh_error"]
    assert len(error_events) == 3  # getMapState, getRTK, getMapTrack
    assert any(event == "map_info_unavailable" for event, _ in events)
