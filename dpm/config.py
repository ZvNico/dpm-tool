"""Small JSON-backed app config.

Holds the list of DPM versions the user tracks — each entry is
``{"version": "2.10.0", "url": <str|None>}`` (an optional explicit download URL
that overrides auto-resolution, needed for the odd hotfix builds) — plus the
selected UI ``theme``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from dpm._constants import CONFIG_PATH
from dpm.workflows import version_key

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class VersionEntry:
    version: str
    url: str | None = None


def _load_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("Could not read config %s: %s", path, exc)
        return {}


def _save_raw(raw: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2))


def load_versions(path: Path = CONFIG_PATH) -> list[VersionEntry]:
    raw = _load_raw(path)
    entries = [
        VersionEntry(version=str(e["version"]), url=(e.get("url") or None))
        for e in raw.get("versions", [])
        if e.get("version")
    ]
    return sorted(entries, key=lambda e: version_key(e.version))


def save_versions(entries: list[VersionEntry], path: Path = CONFIG_PATH) -> None:
    # Merge into the existing config so unrelated keys (e.g. ``theme``) survive.
    raw = _load_raw(path)
    raw["versions"] = [
        {"version": e.version, **({"url": e.url} if e.url else {})}
        for e in sorted(entries, key=lambda e: version_key(e.version))
    ]
    _save_raw(raw, path)


def load_theme(path: Path = CONFIG_PATH) -> str | None:
    """Return the persisted UI theme name, or ``None`` if unset."""
    return _load_raw(path).get("theme") or None


def save_theme(theme: str, path: Path = CONFIG_PATH) -> None:
    """Persist the selected UI theme, preserving the rest of the config."""
    raw = _load_raw(path)
    raw["theme"] = theme
    _save_raw(raw, path)


def add_version(
    version: str, url: str | None = None, path: Path = CONFIG_PATH
) -> list[VersionEntry]:
    """Add (or update the URL of) a version, returning the new list."""
    version = version.strip()
    url = (url or "").strip() or None
    entries = [e for e in load_versions(path) if e.version != version]
    entries.append(VersionEntry(version=version, url=url))
    save_versions(entries, path)
    return load_versions(path)


def remove_version(version: str, path: Path = CONFIG_PATH) -> list[VersionEntry]:
    entries = [e for e in load_versions(path) if e.version != version]
    save_versions(entries, path)
    return load_versions(path)
