"""Unit tests for EIOPA URL candidate generation and the version config store."""

from __future__ import annotations

from pathlib import Path

from dpm.config import add_version, load_versions, remove_version
from dpm.eiopa import candidate_urls


# ── URL candidates ──────────────────────────────────────────────────────────


def test_clean_version_candidate():
    urls = candidate_urls("2.10.0")
    assert (
        "https://dev.eiopa.europa.eu/Taxonomy/Full/2.10.0/S2/"
        "EIOPA_Solvency_II_DPM_Annotated_Templates_2.10.0_table_group_arrangement.xlsx"
        in urls
    )
    # clean release is probed first
    assert "2.10.0/S2" in urls[0]


def test_hotfix_casing_combo_present():
    # The real 2.8.2 hotfix URL: lowercase folder, capital-H filename label.
    urls = candidate_urls("2.8.2_hotfix")
    assert (
        "https://dev.eiopa.europa.eu/Taxonomy/Full/2.8.2_hotfix/S2/"
        "EIOPA_Solvency_II_DPM_Annotated_Templates_2.8.2_Hotfix_table_group_arrangement.xlsx"
        in urls
    )
    # explicit hotfix request → no clean-release candidate
    assert not any("/2.8.2/S2/" in u for u in urls)


def test_numbered_hotfix():
    urls = candidate_urls("2.7.0_Hotfix3")
    assert any("2.7.0_Hotfix3/S2/" in u and "_2.7.0_Hotfix3_" in u for u in urls)


# ── config store ────────────────────────────────────────────────────────────


def test_config_add_remove(tmp_path: Path):
    cfg = tmp_path / "dpm-tool.config.json"
    add_version("2.10.0", path=cfg)
    add_version("2.8.2", url="https://example/x.xlsx", path=cfg)
    entries = load_versions(cfg)
    assert [e.version for e in entries] == ["2.8.2", "2.10.0"]  # sorted by version_key
    assert next(e for e in entries if e.version == "2.8.2").url == "https://example/x.xlsx"
    assert next(e for e in entries if e.version == "2.10.0").url is None

    # adding the same version again updates rather than duplicates
    add_version("2.10.0", url="https://example/y.xlsx", path=cfg)
    entries = load_versions(cfg)
    assert len(entries) == 2
    assert next(e for e in entries if e.version == "2.10.0").url == "https://example/y.xlsx"

    remove_version("2.8.2", path=cfg)
    assert [e.version for e in load_versions(cfg)] == ["2.10.0"]
