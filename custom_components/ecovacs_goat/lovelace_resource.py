"""Decide how the bundled card's Lovelace resource entry has to change.

Kept free of Home Assistant imports so the decision is unit-testable; the
frontend module applies the resulting plan to the resource collection.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

RESOURCE_TYPE = "module"


@dataclass(frozen=True)
class ResourcePlan:
    """Changes that leave exactly one resource entry loading the card."""

    create: bool = False
    update_id: str | None = None
    delete_ids: tuple[str, ...] = ()


def _path(url: object) -> str:
    return str(url or "").split("?", 1)[0]


def plan_card_resource(
    items: Iterable[Mapping[str, Any]], card_path: str, url: str
) -> ResourcePlan:
    """Return the changes that make the resources load ``url`` exactly once.

    Entries are matched by path with the query ignored, so an entry with a stale
    ``?v=`` token (or one added by hand for the same file) is rewritten instead
    of duplicated. An entry that is already current is preferred as the keeper.
    """
    ours = [item for item in items if _path(item.get("url")) == card_path]
    if not ours:
        return ResourcePlan(create=True)

    def is_current(item: Mapping[str, Any]) -> bool:
        return item.get("url") == url and item.get("res_type") == RESOURCE_TYPE

    keep = next((item for item in ours if is_current(item)), ours[0])
    return ResourcePlan(
        update_id=None if is_current(keep) else keep["id"],
        delete_ids=tuple(item["id"] for item in ours if item is not keep),
    )
