"""Security-focused tests for the debug capture store."""

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

from custom_components.ecovacs_goat.debug_capture import DebugCaptureStore


def _store(tmp_path: Path) -> DebugCaptureStore:
    return DebugCaptureStore(tmp_path / "captures", tmp_path / "exports")


def test_session_path_accepts_only_generated_ids(tmp_path: Path) -> None:
    """Only internally generated timestamp ids resolve to a session path."""
    store = _store(tmp_path)
    session = tmp_path / "captures" / "20260827T093825Z"
    session.mkdir(parents=True)

    assert store._session_path("20260827T093825Z") == session.resolve()
    assert store._session_path(None) is None
    assert store._session_path("") is None


def test_session_path_rejects_traversal(tmp_path: Path) -> None:
    """Traversal and absolute paths must never resolve, even if they exist.

    The export service publishes the zipped directory under the
    unauthenticated /local/ path, so an unvalidated session id would let any
    logged-in user publish e.g. .storage (auth tokens) publicly.
    """
    store = _store(tmp_path)
    secret_dir = tmp_path / ".storage"
    secret_dir.mkdir()
    (tmp_path / "captures").mkdir()

    assert store._session_path("../.storage") is None
    assert store._session_path("..") is None
    assert store._session_path(str(secret_dir)) is None
    assert store._session_path("20260827T093825Z/../..") is None
    assert store._session_path("foo") is None


def test_export_zip_name_is_not_guessable(tmp_path: Path) -> None:
    """Exports served from /local/ carry a random token in the file name."""
    store = _store(tmp_path)
    session = tmp_path / "captures" / "20260827T093825Z"
    session.mkdir(parents=True)
    (session / "events.jsonl").write_text("{}\n", encoding="utf-8")

    result = store.export_zip("20260827T093825Z")
    name = Path(result["path"]).name
    assert name.startswith("20260827T093825Z-")
    assert name != "20260827T093825Z-.zip"
    assert len(name) > len("20260827T093825Z-.zip") + 10
    assert result["url"].endswith(name)
