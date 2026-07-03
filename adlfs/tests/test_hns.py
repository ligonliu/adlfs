"""
Tests for Hierarchical Namespace (HNS) features.

These tests require a real ADLS Gen2 storage account (Azurite does not
support HNS).  Set the following environment variables before running:

    ADLFS_TEST_ACCOUNT_NAME      # myaccount
    ADLFS_TEST_ACCOUNT_KEY       # shared key, or omit for DefaultAzureCredential
    ADLFS_TEST_CONTAINER         # container name to use (created if needed)

Alternatively, edit the defaults in ``_config()`` below.

Run isolated from the Azurite-based suite:

    pytest tests/test_hns.py -v
"""

import os

import pytest

from adlfs import AzureBlobFileSystem


def _config():
    """Return (account_name, account_key, container) from env or defaults."""
    name = os.getenv("ADLFS_TEST_ACCOUNT_NAME", "")
    key = os.getenv("ADLFS_TEST_ACCOUNT_KEY", "")
    container = os.getenv("ADLFS_TEST_CONTAINER", "adlfs-hns-test")
    return name, key, container


def _require_live_account():
    """Skip if no ADLS Gen2 account is configured."""
    name, _, _ = _config()
    if not name:
        pytest.skip("Set ADLFS_TEST_ACCOUNT_NAME to run HNS integration tests")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hns_fs():
    """An AzureBlobFileSystem connected to a real ADLS Gen2 account.

    Uses the auto-detection flow (DFS-first with Blob fallback).
    """
    _require_live_account()
    name, key, container = _config()
    kw = {"account_key": key} if key else {}
    fs = AzureBlobFileSystem(account_name=name, **kw)
    if container not in [c["name"] for c in fs.ls("")]:
        fs.mkdir(container)
    yield fs
    fs.rm(container, recursive=True)


@pytest.fixture
def hns_fs_blob_uri():
    """Force Blob endpoint via a blob.core.windows.net URI."""
    _require_live_account()
    name, key, _ = _config()
    kw = {"account_key": key} if key else {}
    uri = f"abfss://none@{name}.blob.core.windows.net/none"
    fs = AzureBlobFileSystem(uri, **kw)
    yield fs


@pytest.fixture
def hns_fs_account_host():
    """Force DFS endpoint via explicit account_host."""
    _require_live_account()
    name, key, _ = _config()
    kw = {"account_key": key} if key else {}
    fs = AzureBlobFileSystem(
        account_name=name,
        account_host=f"{name}.dfs.core.windows.net",
        **kw,
    )
    yield fs


# ---------------------------------------------------------------------------
# Phase 1 — Endpoint selection and HNS detection
# ---------------------------------------------------------------------------

class TestEndpointSelection:
    """Verify the do_connect() precedence chain."""

    def test_hns_detected_by_default(self, hns_fs):
        """Default auto-detect sets hns_enabled=True for Gen2 accounts."""
        assert hns_fs.hns_enabled is True
        assert hns_fs.account_url.endswith(".dfs.core.windows.net")

    def test_blob_uri_skips_hns_probe(self, hns_fs_blob_uri):
        """URI with blob.core.windows.net uses Blob endpoint, no probe."""
        assert hns_fs_blob_uri.hns_enabled is False
        assert ".blob.core." in hns_fs_blob_uri.account_url

    def test_account_host_wins(self, hns_fs_account_host):
        """account_host takes absolute precedence — no probing."""
        assert hns_fs_account_host.hns_enabled is False
        assert ".dfs.core." in hns_fs_account_host.account_url

    def test_short_uri_defaults_to_dfs_first(self):
        """Short URI (abfs://container/path) with account_name= probes DFS."""
        _require_live_account()
        name, key, _ = _config()
        kw = {"account_key": key} if key else {}
        fs = AzureBlobFileSystem(account_name=name, **kw)
        try:
            assert fs.hns_enabled is True
            assert fs.account_url.endswith(".dfs.core.windows.net")
        finally:
            pass  # no containers created


# ---------------------------------------------------------------------------
# Phase 2 — HNS-aware _dir_exists
# ---------------------------------------------------------------------------

class TestDirExists:
    """Verify optimized directory-existence checks on HNS."""

    def test_dir_exists_for_existing_directory(self, hns_fs):
        name, _, container = _config()
        dir_path = f"{container}/subdir"
        hns_fs.pipe(f"{dir_path}/afile.txt", b"hello")
        assert hns_fs.exists(dir_path)
        assert hns_fs.isdir(dir_path)

    def test_dir_exists_for_nonexistent_directory(self, hns_fs):
        name, _, container = _config()
        assert not hns_fs.exists(f"{container}/nonexistent")

    def test_dir_exists_for_file_is_not_dir(self, hns_fs):
        name, _, container = _config()
        path = f"{container}/plain_file.txt"
        hns_fs.pipe(path, b"hello")
        assert not hns_fs.isdir(path)
        assert hns_fs.isfile(path)


# ---------------------------------------------------------------------------
# Phase 3 — DFS-native rename
# ---------------------------------------------------------------------------

class TestRename:
    """Verify _mv_file behavior."""

    def test_rename_same_container(self, hns_fs):
        """Same-container rename should succeed."""
        name, _, container = _config()
        src = f"{container}/source.txt"
        dst = f"{container}/target.txt"
        content = b"rename-me"

        hns_fs.pipe(src, content)
        hns_fs.mv(src, dst)

        assert not hns_fs.exists(src)
        assert hns_fs.exists(dst)
        assert hns_fs.cat(dst) == content

    def test_rename_preserves_metadata(self, hns_fs):
        """Rename should preserve file size and content."""
        name, _, container = _config()
        src = f"{container}/meta_src.txt"
        dst = f"{container}/meta_dst.txt"
        content = b"metadata-test"

        hns_fs.pipe(src, content)
        src_info = hns_fs.info(src)
        hns_fs.mv(src, dst)
        dst_info = hns_fs.info(dst)

        assert dst_info["size"] == src_info["size"]
        assert hns_fs.cat(dst) == content

    def test_rename_cross_container(self, hns_fs):
        """Cross-container rename falls back to copy+delete."""
        name, _, container1 = _config()
        container2 = f"{container1}-cross"
        hns_fs.mkdir(container2)

        try:
            src = f"{container1}/cross_src.txt"
            dst = f"{container2}/cross_dst.txt"
            content = b"cross-container"

            hns_fs.pipe(src, content)
            hns_fs.mv(src, dst)

            assert not hns_fs.exists(src)
            assert hns_fs.exists(dst)
            assert hns_fs.cat(dst) == content
        finally:
            hns_fs.rm(container2, recursive=True)

    def test_rename_same_path_noop(self, hns_fs):
        """Moving a file to itself is a no-op."""
        name, _, container = _config()
        path = f"{container}/noop.txt"
        hns_fs.pipe(path, b"noop")
        hns_fs.mv(path, path)
        assert hns_fs.exists(path)
        assert hns_fs.cat(path) == b"noop"


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

class TestIntegration:
    """End-to-end workflow on HNS."""

    def test_full_workflow(self, hns_fs):
        """Create, list, read, rename, delete."""
        name, _, container = _config()

        hns_fs.pipe(f"{container}/a.txt", b"aaa")
        hns_fs.pipe(f"{container}/subdir/b.txt", b"bbb")

        listing = hns_fs.ls(container, detail=True)
        names = {e["name"] for e in listing}
        assert f"{container}/a.txt" in names
        assert f"{container}/subdir" in names

        sub = hns_fs.ls(f"{container}/subdir")
        assert sub == [f"{container}/subdir/b.txt"]

        hns_fs.mv(f"{container}/a.txt", f"{container}/renamed.txt")
        assert not hns_fs.exists(f"{container}/a.txt")
        assert hns_fs.cat(f"{container}/renamed.txt") == b"aaa"

        hns_fs.rm(f"{container}/subdir", recursive=True)
        assert not hns_fs.exists(f"{container}/subdir")

    def test_find_recursive(self, hns_fs):
        """find() traverses recursively under HNS."""
        name, _, container = _config()
        hns_fs.pipe(f"{container}/d1/f1.txt", b"1")
        hns_fs.pipe(f"{container}/d1/d2/f2.txt", b"2")

        found = hns_fs.find(container)
        assert f"{container}/d1/f1.txt" in found
        assert f"{container}/d1/d2/f2.txt" in found