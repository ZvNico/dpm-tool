"""Resolve, download and unpack the official EIOPA Solvency II DPM database.

The download URL cannot be derived from a version string alone: hotfix builds
live in suffixed folders with inconsistent casing (``2.8.2_hotfix`` vs
``2.8.1_Hotfix``), the zip filename spells the product either ``Solvency_II`` or
``SolvencyII`` depending on the build, and some builds carry non-mechanical
suffixes. So we generate a bounded set of candidate URLs and HEAD-probe them,
always allowing a caller to supply an explicit URL that bypasses resolution.

The download is a zip containing the DPM SQLite database; we extract that
database file and cache it as ``{version}.db``.
"""

from __future__ import annotations

import logging
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path

import lxml.html

LOG = logging.getLogger(__name__)

_BASE = "https://dev.eiopa.europa.eu/Taxonomy/Full"
# EIOPA's public listing pages. Every file a version publishes is linked from one
# of these; current releases live on the first, superseded ones on the second.
_LISTING_PAGES = (
    "https://www.eiopa.europa.eu/tools-and-data/supervisory-reporting-dpm-and-xbrl_en",
    "https://www.eiopa.europa.eu/deprecated-versions-data-point-model-and-xbrl_en",
)
# Marks the versioned-folder path segment that scoped download links share.
_TAXONOMY_MARKER = "/Taxonomy/Full/"
# The zip filename spells the product both ways across builds; probe both.
_FILENAMES = (
    "EIOPA_Solvency_II_DPM_Database_{label}.zip",
    "EIOPA_SolvencyII_DPM_Database_{label}.zip",
)
_UA = {"User-Agent": "Mozilla/5.0 (dpm-tool)"}
# Highest hotfix number to probe when auto-discovering the latest hotfix.
_MAX_HOTFIX = 4
_HOTFIX_RE = re.compile(r"^(?P<base>\d+(?:\.\d+)*)(?:[_-]?(?:hotfix)(?P<num>\d*))?$", re.I)
# SQLite database members inside the downloaded zip.
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
# Streaming chunk size for downloads/extraction, sized so progress updates land
# frequently on large (hundreds of MB) DPM databases without excessive callbacks.
_CHUNK = 1 << 20  # 1 MiB
_ssl_fallback_active: bool = False


def get_proxy_url(config_path: Path | None = None) -> str | None:
    """Return the configured proxy URL or None for direct connection.

    Precedence:
    1. Environment variables (DPM_PROXY, HTTPS_PROXY, HTTP_PROXY, etc.)
    2. Config file (CONFIG_PATH, key 'proxy')
    3. None (direct connection, no proxy)
    """
    for var in (
        "DPM_PROXY",
        "DPM_HTTP_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        val = os.environ.get(var)
        if val is not None:
            val = val.strip()
            if not val or val.lower() in ("none", "direct", "off", "no"):
                return None
            if "://" not in val:
                val = f"http://{val}"
            return val

    from dpm.config import load_proxy

    val = load_proxy(path=config_path)
    if val:
        val = val.strip()
        if not val or val.lower() in ("none", "direct", "off", "no"):
            return None
        if "://" not in val:
            val = f"http://{val}"
        return val

    return None


def _get_opener(verify_ssl: bool = True) -> urllib.request.OpenerDirector:
    """Construct a urllib opener configured with proxy and SSL context."""
    proxy = get_proxy_url()
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    if not verify_ssl or _ssl_fallback_active:
        ctx = ssl._create_unverified_context()
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    else:
        ctx = ssl.create_default_context()
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _open_url(req: urllib.request.Request, timeout: float = 30.0):
    """Open a URL request with automatic proxy and SSL fallback on certificate errors."""
    global _ssl_fallback_active
    if _ssl_fallback_active:
        return _get_opener(verify_ssl=False).open(req, timeout=timeout)

    opener = _get_opener(verify_ssl=True)
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
            LOG.warning(
                "SSL verification failed, retrying and memorizing unverified context: %s",
                exc,
            )
            _ssl_fallback_active = True
            fallback_opener = _get_opener(verify_ssl=False)
            return fallback_opener.open(req, timeout=timeout)
        raise


def test_proxy_connection(
    proxy: str | None = None, timeout: float = 10.0
) -> tuple[bool, str]:
    """Test connectivity to EIOPA via the given proxy (or configured proxy if None).

    Returns ``(success, message)``.
    """
    if proxy is not None:
        raw = proxy.strip()
        if not raw or raw.lower() in ("none", "direct", "off", "no"):
            proxy_url = None
        else:
            proxy_url = f"http://{raw}" if "://" not in raw else raw
    else:
        proxy_url = get_proxy_url()

    test_url = "https://www.eiopa.europa.eu/"
    req = urllib.request.Request(test_url, method="HEAD", headers=_UA)

    def _make_opener(verify: bool) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = []
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
        return urllib.request.build_opener(*handlers)

    target_desc = f"via {proxy_url}" if proxy_url else "directly"
    try:
        opener = _make_opener(verify=not _ssl_fallback_active)
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return True, f"Connection OK ({status}) to EIOPA {target_desc}"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
            try:
                fallback_opener = _make_opener(verify=False)
                with fallback_opener.open(req, timeout=timeout) as resp:
                    status = getattr(resp, "status", 200)
                    return True, f"Connection OK ({status}) to EIOPA {target_desc} (SSL unverified)"
            except Exception as sub_exc:
                return False, f"Fallback connection failed: {sub_exc}"
        reason = getattr(exc, "reason", exc)
        return False, f"Connection failed: {reason}"
    except Exception as exc:
        return False, f"Connection error: {exc}"


def _urls(folder: str, label: str) -> list[str]:
    """Candidate zip URLs for a (folder, filename-label), one per filename spelling."""
    return [f"{_BASE}/{folder}/S2/{name.format(label=label)}" for name in _FILENAMES]


def _hotfix_variants(base: str, num: str) -> list[tuple[str, str]]:
    """(folder, filename-label) pairs for a hotfix, spanning the observed casings."""
    pairs: list[tuple[str, str]] = []
    for folder_cas in ("hotfix", "Hotfix"):
        for label_cas in ("Hotfix", "hotfix"):
            pairs.append((f"{base}_{folder_cas}{num}", f"{base}_{label_cas}{num}"))
    return pairs


def candidate_urls(version: str) -> list[str]:
    """Ordered, de-duplicated candidate zip URLs for a version label.

    Ordering is clean release first, then ascending hotfix numbers, so a caller
    probing in order and keeping the *last* success lands on the latest hotfix.
    If ``version`` already names a hotfix, only that hotfix's casings are tried.
    """
    m = _HOTFIX_RE.match(version.strip())
    seen: set[str] = set()
    out: list[str] = []

    def add(folder: str, label: str) -> None:
        for url in _urls(folder, label):
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
        with _open_url(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def resolve_dpm_database_url(
    version: str, candidates: Iterable[str] | None = None
) -> str | None:
    """HEAD-probe candidates and return the last that resolves (latest hotfix)."""
    best: str | None = None
    for url in candidates if candidates is not None else candidate_urls(version):
        if _head_ok(url):
            LOG.info("Resolved candidate OK: %s", url)
            best = url
    return best


def download_file(
    url: str,
    dest: Path,
    timeout: float = 300.0,
    on_bytes: Callable[[int, int | None], None] | None = None,
    verify_zip: bool = True,
) -> Path:
    """Stream ``url`` to ``dest`` (atomic via a .part temp), returning ``dest``.

    ``on_bytes(done, total)`` is invoked as bytes arrive; ``total`` is the
    ``Content-Length`` when the server reports it, else ``None`` (unknown length).
    ``verify_zip`` fails fast (on the zip magic) when the download must be a zip;
    set it ``False`` for arbitrary files (xlsx, pdf, …).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=_UA)
    LOG.info("Downloading %s", url)
    with _open_url(req, timeout=timeout) as resp:
        if not (200 <= resp.status < 300):
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        total = int(resp.headers.get("Content-Length") or 0) or None
        done = 0
        if on_bytes:
            on_bytes(0, total)
        with open(tmp, "wb") as fh:
            while chunk := resp.read(_CHUNK):
                fh.write(chunk)
                done += len(chunk)
                if on_bytes:
                    on_bytes(done, total)
    # The download is a zip — cheap sanity check to fail fast on an HTML error page.
    if verify_zip:
        with open(tmp, "rb") as fh:
            if fh.read(2) != b"PK":
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"Downloaded file is not a zip (no zip magic): {url}")
    tmp.replace(dest)
    LOG.info("Saved %s", dest)
    return dest


def _extract_database(
    zip_path: Path,
    dest: Path,
    on_bytes: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Extract the SQLite database member of ``zip_path`` to ``dest``.

    Picks the largest member whose name ends in a database suffix — DPM zips ship a
    single ``.db`` alongside small readme/licence files. ``on_bytes(done, total)``
    reports extraction progress against the member's uncompressed size.
    """
    with zipfile.ZipFile(zip_path) as zf:
        db_members = [
            info
            for info in zf.infolist()
            if not info.is_dir()
            and info.filename.lower().endswith(_DB_SUFFIXES)
        ]
        if not db_members:
            raise RuntimeError(
                f"No SQLite database ({', '.join(_DB_SUFFIXES)}) found in {zip_path.name}"
            )
        member = max(db_members, key=lambda i: i.file_size)
        total = member.file_size or None
        done = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        if on_bytes:
            on_bytes(0, total)
        with zf.open(member) as src, open(dest, "wb") as out:
            while chunk := src.read(_CHUNK):
                out.write(chunk)
                done += len(chunk)
                if on_bytes:
                    on_bytes(done, total)
    LOG.info("Extracted %s → %s", member.filename, dest)
    return dest


def fetch_dpm_database(
    version: str,
    dest_dir: Path,
    url: str | None = None,
    on_progress: Callable[[int, int | None, str], None] | None = None,
) -> Path:
    """Return the local DPM SQLite database for ``version``, downloading it if absent.

    Cache-first: if ``dest_dir/{version}.db`` already exists it is returned
    untouched. Otherwise the URL is used as-is when given, else resolved by
    probing; the zip is downloaded and its database member extracted.

    ``on_progress(done, total, label)`` reports progress: unmeasurable steps
    (resolve, cached) emit ``total=None`` (indeterminate); the download and
    extraction report real byte counts.
    """
    def _label(label: str) -> None:
        if on_progress:
            on_progress(0, None, label)

    dest = dest_dir / f"{version}.db"
    if dest.exists():
        _label(f"Using cached database {dest.name}")
        return dest

    resolved = url
    if not resolved:
        _label("Resolving download URL…")
        resolved = resolve_dpm_database_url(version)
        if not resolved:
            raise RuntimeError(
                f"Could not resolve a download URL for version {version!r}. "
                "Set an explicit URL in Settings."
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        download_cb = extract_cb = None
        if on_progress:
            def download_cb(done: int, total: int | None) -> None:
                on_progress(done, total, "Downloading DPM database…")

            def extract_cb(done: int, total: int | None) -> None:
                on_progress(done, total, "Extracting database…")
        zip_path = download_file(
            resolved, Path(tmpdir) / f"{version}.zip", on_bytes=download_cb
        )
        _extract_database(zip_path, dest, on_bytes=extract_cb)
    return dest


def _folder_segment(url: str) -> str | None:
    """The versioned-folder path segment of a ``/Taxonomy/Full/{folder}/…`` URL."""
    path = urllib.parse.urlsplit(url).path
    marker = _TAXONOMY_MARKER.lower()
    low = path.lower()
    idx = low.find(marker)
    if idx < 0:
        return None
    rest = path[idx + len(_TAXONOMY_MARKER):]
    segment = rest.split("/", 1)[0]
    return segment or None


def list_version_files(version: str) -> list[tuple[str, str]]:
    """(filename, absolute-URL) pairs for every file EIOPA publishes for ``version``.

    Scrapes the public listing pages and keeps links hosted under the version's own
    ``/Taxonomy/Full/{folder}/`` folder, matching the folder segment case-insensitively
    (hotfix folders vary in casing). Shared cross-version links (generic licence,
    reports hosted elsewhere) are excluded. De-duplicated, preserving first-seen order.
    """
    wanted = version.strip().lower()
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for page in _LISTING_PAGES:
        req = urllib.request.Request(page, headers=_UA)
        try:
            with _open_url(req, timeout=30.0) as resp:
                html = resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            LOG.warning("Could not fetch listing page %s: %s", page, exc)
            continue
        doc = lxml.html.fromstring(html)
        doc.make_links_absolute(page)
        for href in doc.xpath("//a/@href"):
            segment = _folder_segment(href)
            if segment is None or segment.lower() != wanted:
                continue
            url = href.split("#", 1)[0]
            if url in seen:
                continue
            seen.add(url)
            name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            out.append((name, url))
    return out


def download_version_files(
    version: str,
    dest_dir: Path,
    on_progress: Callable[[int, int | None, str], None] | None = None,
) -> list[Path]:
    """Download every published file for ``version`` into ``dest_dir/{version}/``.

    Files already present are skipped. ``on_progress(done, total, label)`` reports
    per-file byte counts with a ``"Downloading i/N: <name>"`` label, matching the
    shape the ingest progress bar consumes.
    """
    files = list_version_files(version)
    if not files:
        raise RuntimeError(
            f"No published files found for version {version!r} on EIOPA's listing pages."
        )
    target = dest_dir / version
    target.mkdir(parents=True, exist_ok=True)
    total = len(files)
    saved: list[Path] = []
    for i, (name, url) in enumerate(files, start=1):
        dest = target / name
        label = f"Downloading {i}/{total}: {name}"
        if dest.exists():
            if on_progress:
                on_progress(0, None, f"Skipping {i}/{total} (exists): {name}")
            saved.append(dest)
            continue
        cb = (lambda done, tot, lbl=label: on_progress(done, tot, lbl)) if on_progress else None
        download_file(url, dest, on_bytes=cb, verify_zip=False)
        saved.append(dest)
    return saved
