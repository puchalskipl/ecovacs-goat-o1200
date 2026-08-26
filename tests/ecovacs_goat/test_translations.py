"""Translation files must cover every entity the platforms register."""

import json
from pathlib import Path
import re

PACKAGE_PATH = Path(__file__).parents[2] / "custom_components" / "ecovacs_goat"
TRANSLATIONS = PACKAGE_PATH / "translations"
PLATFORMS = ("button", "number", "select", "sensor", "switch", "time")

_TRANSLATION_KEY = re.compile(r'translation_key="([^"]+)"')


def _declared_keys() -> dict[str, set[str]]:
    """Return the translation keys each platform module registers."""
    return {
        platform: set(
            _TRANSLATION_KEY.findall(
                (PACKAGE_PATH / f"{platform}.py").read_text(encoding="utf-8")
            )
        )
        for platform in PLATFORMS
    }


def _load(path: Path) -> dict:
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} must not carry a BOM"
    return json.loads(raw.decode("utf-8"))


def test_every_entity_has_an_english_name() -> None:
    """strings.json must name every entity the platforms declare."""
    strings = _load(PACKAGE_PATH / "strings.json")
    for platform, keys in _declared_keys().items():
        named = set(strings["entity"].get(platform, {}))
        assert keys <= named, (
            f"{platform}: missing English names for {sorted(keys - named)}"
        )


def test_translation_files_match_strings() -> None:
    """Every shipped language covers exactly the keys strings.json defines."""
    strings = _load(PACKAGE_PATH / "strings.json")
    expected = {
        platform: set(items) for platform, items in strings["entity"].items()
    }

    for path in sorted(TRANSLATIONS.glob("*.json")):
        translation = _load(path)
        for platform, keys in expected.items():
            translated = set(translation.get("entity", {}).get(platform, {}))
            assert translated == keys, (
                f"{path.name} [{platform}]: missing {sorted(keys - translated)}, "
                f"unexpected {sorted(translated - keys)}"
            )


def test_select_options_are_translated() -> None:
    """Select options must be translated, not shown as raw protocol values."""
    for path in sorted(TRANSLATIONS.glob("*.json")):
        selects = _load(path)["entity"]["select"]
        assert set(selects["mowing_efficiency"]["state"]) == {"quick", "delicate"}
        assert set(selects["obstacle_avoidance"]["state"]) == {
            "short_grass",
            "general",
            "bumpy_tall_grass",
        }


def test_polish_translation_is_shipped() -> None:
    """Polish is a first-class language; English stays the fallback."""
    languages = {path.stem for path in TRANSLATIONS.glob("*.json")}
    assert {"en", "pl"} <= languages

    polish = _load(TRANSLATIONS / "pl.json")
    assert polish["entity"]["number"]["cutting_height"]["name"] == "Wysokość koszenia"
    assert polish["config"]["step"]["device_verification"]["title"]
