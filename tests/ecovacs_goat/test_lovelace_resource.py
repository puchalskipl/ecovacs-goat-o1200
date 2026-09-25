"""Tests for keeping the card's Lovelace resource entry current."""

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

from custom_components.ecovacs_goat.lovelace_resource import (
    ResourcePlan,
    plan_card_resource,
)

CARD = "/ecovacs_goat/ecovacs-goat-card.js"
URL = f"{CARD}?v=abc123"


def _item(item_id, url, res_type="module"):
    return {"id": item_id, "url": url, "res_type": res_type}


def test_creates_entry_when_missing():
    items = [_item("m", "/hacsfiles/lovelace-mushroom/mushroom.js")]
    assert plan_card_resource(items, CARD, URL) == ResourcePlan(create=True)


def test_current_entry_needs_no_change():
    items = [_item("a", URL)]
    assert plan_card_resource(items, CARD, URL) == ResourcePlan()


def test_stale_token_is_rewritten_not_duplicated():
    items = [_item("a", f"{CARD}?v=old")]
    assert plan_card_resource(items, CARD, URL) == ResourcePlan(update_id="a")


def test_wrong_type_is_rewritten():
    items = [_item("a", URL, res_type="js")]
    assert plan_card_resource(items, CARD, URL) == ResourcePlan(update_id="a")


def test_duplicates_collapse_onto_the_current_entry():
    items = [_item("a", f"{CARD}?v=zasob"), _item("b", URL), _item("c", CARD)]
    assert plan_card_resource(items, CARD, URL) == ResourcePlan(delete_ids=("a", "c"))


def test_duplicates_without_current_entry_keep_the_first():
    items = [_item("a", f"{CARD}?v=old"), _item("b", f"{CARD}?v=older")]
    assert plan_card_resource(items, CARD, URL) == ResourcePlan(
        update_id="a", delete_ids=("b",)
    )


def test_other_paths_are_never_touched():
    items = [
        _item("legacy", "/local/ecovacs_goat/ecovacs-goat-card.js"),
        _item("prefix", f"{CARD}.map"),
        {"id": "broken"},
    ]
    assert plan_card_resource(items, CARD, URL) == ResourcePlan(create=True)
