"""Unit tests for EIOPA URL candidate generation and the version config store."""

from __future__ import annotations

from pathlib import Path

from dpm.config import add_version, load_versions, remove_version
from dpm.eiopa import candidate_urls


# ── URL candidates ──────────────────────────────────────────────────────────


def test_clean_version_candidate():
    urls = candidate_urls("2.10.0")
    # The real 2.10.0 URL uses the ``Solvency_II`` filename spelling.
    assert (
        "https://dev.eiopa.europa.eu/Taxonomy/Full/2.10.0/S2/"
        "EIOPA_Solvency_II_DPM_Database_2.10.0.zip"
        in urls
    )
    # Both filename spellings are probed.
    assert (
        "https://dev.eiopa.europa.eu/Taxonomy/Full/2.10.0/S2/"
        "EIOPA_SolvencyII_DPM_Database_2.10.0.zip"
        in urls
    )
    # clean release is probed first
    assert "2.10.0/S2" in urls[0]


def test_hotfix_casing_combo_present():
    # The real 2.8.2 hotfix URL: lowercase folder, capital-H filename label.
    urls = candidate_urls("2.8.2_hotfix")
    assert (
        "https://dev.eiopa.europa.eu/Taxonomy/Full/2.8.2_hotfix/S2/"
        "EIOPA_SolvencyII_DPM_Database_2.8.2_Hotfix.zip"
        in urls
    )
    # explicit hotfix request → no clean-release candidate
    assert not any("/2.8.2/S2/" in u for u in urls)


def test_numbered_hotfix():
    urls = candidate_urls("2.7.0_Hotfix3")
    assert any("2.7.0_Hotfix3/S2/" in u and "_2.7.0_Hotfix3.zip" in u for u in urls)


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


def test_proxy_config(tmp_path: Path):
    from dpm.config import load_proxy, save_proxy

    cfg = tmp_path / "dpm-tool.config.json"
    assert load_proxy(cfg) is None

    save_proxy("http://127.0.0.1:9000", path=cfg)
    assert load_proxy(cfg) == "http://127.0.0.1:9000"

    save_proxy("none", path=cfg)
    assert load_proxy(cfg) is None

    save_proxy("10.0.0.1:8080", path=cfg)
    assert load_proxy(cfg) == "10.0.0.1:8080"

    save_proxy(None, path=cfg)
    assert load_proxy(cfg) is None


def test_get_proxy_url_precedence(monkeypatch, tmp_path: Path):
    from dpm.config import save_proxy
    import dpm.eiopa as eiopa
    import dpm._constants as constants
    import dpm.config as config

    cfg = tmp_path / "dpm-tool.config.json"
    monkeypatch.setattr(constants, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)

    # Clean env vars
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
        monkeypatch.delenv(var, raising=False)

    # 1. No env, no config -> None (direct connection)
    assert eiopa.get_proxy_url() is None

    # 2. Config proxy is used when env is absent
    save_proxy("127.0.0.1:9000", path=cfg)
    assert eiopa.get_proxy_url() == "http://127.0.0.1:9000"

    # 3. Env var overrides config
    monkeypatch.setenv("DPM_PROXY", "proxy.corp:3128")
    assert eiopa.get_proxy_url() == "http://proxy.corp:3128"

    # 4. Env var explicitly setting direct connection overrides config
    monkeypatch.setenv("DPM_PROXY", "direct")
    assert eiopa.get_proxy_url() is None


def test_test_proxy_connection(monkeypatch):
    import urllib.error
    from unittest.mock import MagicMock
    import dpm.eiopa as eiopa

    # 1. Success case
    def mock_build_opener_ok(*handlers):
        opener = MagicMock()
        resp = MagicMock(status=200)
        opener.open.return_value.__enter__.return_value = resp
        return opener

    monkeypatch.setattr(eiopa.urllib.request, "build_opener", mock_build_opener_ok)
    ok, msg = eiopa.test_proxy_connection("127.0.0.1:9000")
    assert ok is True
    assert "200" in msg
    assert "127.0.0.1:9000" in msg

    # 2. Failure case
    def mock_build_opener_fail(*handlers):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("Connection refused")
        return opener

    monkeypatch.setattr(eiopa.urllib.request, "build_opener", mock_build_opener_fail)
    ok, msg = eiopa.test_proxy_connection("127.0.0.1:9000")
    assert ok is False
    assert "Connection refused" in msg


def test_ssl_fallback_memorization(monkeypatch):
    import ssl
    import urllib.error
    import urllib.request
    from unittest.mock import MagicMock
    import dpm.eiopa as eiopa

    monkeypatch.setattr(eiopa, "_ssl_fallback_active", False)

    verified_call_count = 0
    unverified_call_count = 0

    def mock_get_opener(verify_ssl=True):
        nonlocal verified_call_count, unverified_call_count
        opener = MagicMock()
        if verify_ssl:
            def fail_open(req, timeout=30.0):
                nonlocal verified_call_count
                verified_call_count += 1
                raise urllib.error.URLError(
                    ssl.SSLError("certificate verify failed: Basic Constraints of CA cert not marked critical")
                )
            opener.open = fail_open
        else:
            def ok_open(req, timeout=30.0):
                nonlocal unverified_call_count
                unverified_call_count += 1
                return MagicMock(status=200)
            opener.open = ok_open
        return opener

    monkeypatch.setattr(eiopa, "_get_opener", mock_get_opener)

    req1 = urllib.request.Request("https://example.com/file1")
    req2 = urllib.request.Request("https://example.com/file2")

    # First request triggers failure, switches flag, retries unverified
    res1 = eiopa._open_url(req1)
    assert res1.status == 200
    assert verified_call_count == 1
    assert unverified_call_count == 1
    assert eiopa._ssl_fallback_active is True

    # Second request directly uses unverified opener without trying verified
    res2 = eiopa._open_url(req2)
    assert res2.status == 200
    assert verified_call_count == 1  # Not incremented!
    assert unverified_call_count == 2

