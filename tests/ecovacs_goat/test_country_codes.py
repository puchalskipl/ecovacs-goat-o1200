"""Tests for ECOVACS country-code/API-code mapping helpers."""

from pathlib import Path
import sys
import types

import pytest

PACKAGE_PATH = Path(__file__).parents[2] / "custom_components" / "ecovacs_goat"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(PACKAGE_PATH.parent)]
sys.modules.setdefault("custom_components", custom_components)

ecovacs_goat = types.ModuleType("custom_components.ecovacs_goat")
ecovacs_goat.__path__ = [str(PACKAGE_PATH)]
sys.modules.setdefault("custom_components.ecovacs_goat", ecovacs_goat)

from custom_components.ecovacs_goat.mower_api import country_api_code


@pytest.mark.parametrize(
    ("country", "expected"),
    [
        ("GB", "uk"),
        ("gb", "uk"),
        ("US", "us"),
        ("DE", "de"),
        ("CN", "cn"),
    ],
)
def test_country_api_code_maps_gb_to_uk(country: str, expected: str) -> None:
    """`GB` is rewritten to the `uk` code the API expects; others just lowercase."""
    assert country_api_code(country) == expected
