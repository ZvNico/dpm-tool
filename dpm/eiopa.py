"""Resolve and download EIOPA Solvency II annotated-templates workbooks.

The download URL cannot be derived from a version string alone: hotfix builds
live in suffixed folders with inconsistent casing (``2.8.2_hotfix`` vs
``2.8.1_Hotfix``), the filename label casing differs from the folder within the
same URL, and some builds carry non-mechanical suffixes. So we generate a
bounded set of candidate URLs and HEAD-probe them, always allowing a caller to
supply an explicit URL that bypasses resolution entirely.
"""

from __future__ import annotations

import logging
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path

LOG = logging.getLogger(__name__)

_BASE = "https://dev.eiopa.europa.eu/Taxonomy/Full"
_FILENAME = "EIOPA_Solvency_II_DPM_Annotated_Templates_{label}_table_group_arrangement.xlsx"
_UA = {"User-Agent": "Mozilla/5.0 (dpm-tool)"}
# Highest hotfix number to probe when auto-discovering the latest hotfix.
_MAX_HOTFIX = 4
_HOTFIX_RE = re.compile(r"^(?P<base>\d+(?:\.\d+)*)(?:[_-]?(?:hotfix)(?P<num>\d*))?$", re.I)


def _url(folder: str, label: str) -> str:
    return f"{_BASE}/{folder}/S2/{_FILENAME.format(label=label)}"


def _hotfix_variants(base: str, num: str) -> list[tuple[str, str]]:
    """(folder, filename-label) pairs for a hotfix, spanning the observed casings."""
    pairs: list[tuple[str, str]] = []
    for folder_cas in ("hotfix", "Hotfix"):
        for label_cas in ("Hotfix", "hotfix"):
            pairs.append((f"{base}_{folder_cas}{num}", f"{base}_{label_cas}{num}"))
    return pairs


def candidate_urls(version: str) -> list[str]:
    """Ordered, de-duplicated candidate URLs for a version label.

    Ordering is clean release first, then ascending hotfix numbers, so a caller
    probing in order and keeping the *last* success lands on the latest hotfix.
    If ``version`` already names a hotfix, only that hotfix's casings are tried.
    """
    m = _HOTFIX_RE.match(version.strip())
    seen: set[str] = set()
    out: list[str] = []

    def add(folder: str, label: str) -> None:
        url = _url(folder, label)
        if url not in seen:
            seen.add(url)
            out.append(url)

    if m:
        base, num = m.group("base"), m.group("num")
        explicit_hotfix = "hotfix" in version.lower()
        # Clean release candidate (skip if the user explicitly asked for a hotfix).
        if not explicit_hotfix:
            add(base, base)
            for n in ("",) + tuple(str(i) for i in range(2, _MAX_HOTFIX + 1)):
                for folder, label in _hotfix_variants(base, n):
                    add(folder, label)
        else:
            for folder, label in _hotfix_variants(base, num or ""):
                add(folder, label)
    else:
        # Unparseable — try the version verbatim as both folder and label.
        add(version, version)
    return out


def _head_ok(url: str, timeout: float = 15.0) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def resolve_annotated_templates_url(
    version: str, candidates: Iterable[str] | None = None
) -> str | None:
    """HEAD-probe candidates and return the last that resolves (latest hotfix)."""
    best: str | None = None
    for url in candidates if candidates is not None else candidate_urls(version):
        if _head_ok(url):
            LOG.info("Resolved candidate OK: %s", url)
            best = url
    return best


def download_file(url: str, dest: Path, timeout: float = 120.0) -> Path:
    """Stream ``url`` to ``dest`` (atomic via a .part temp), returning ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=_UA)
    LOG.info("Downloading %s", url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if not (200 <= resp.status < 300):
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    # xlsx is a zip — cheap sanity check to fail fast on an HTML error page.
    with open(tmp, "rb") as fh:
        if fh.read(2) != b"PK":
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Downloaded file is not an .xlsx (no zip magic): {url}")
    tmp.replace(dest)
    LOG.info("Saved %s", dest)
    return dest


def fetch_annotated_templates(
    version: str,
    dest_dir: Path,
    url: str | None = None,
    on_step: "callable | None" = None,
) -> Path:
    """Return the local workbook for ``version``, downloading it if absent.

    Cache-first: if ``dest_dir/{version}.xlsx`` already exists it is returned
    untouched. Otherwise the URL is used as-is when given, else resolved by
    probing, then downloaded.
    """
    def _step(label: str) -> None:
        if on_step:
            on_step(label)

    dest = dest_dir / f"{version}.xlsx"
    if dest.exists():
        _step(f"Using cached workbook {dest.name}")
        return dest

    resolved = url
    if not resolved:
        _step("Resolving download URL…")
        resolved = resolve_annotated_templates_url(version)
        if not resolved:
            raise RuntimeError(
                f"Could not resolve a download URL for version {version!r}. "
                "Set an explicit URL in Settings."
            )
    _step("Downloading workbook…")
    return download_file(resolved, dest)
