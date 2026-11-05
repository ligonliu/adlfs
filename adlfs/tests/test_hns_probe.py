"""Unit tests for DFS endpoint probing and initialization behavior."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from adlfs import AzureBlobFileSystem


@pytest.mark.asyncio
async def test_check_dfs_reachability_success():
    """Verify reachability returns True when server responds (even with HTTP error status)."""
    fs = MagicMock(spec=AzureBlobFileSystem)
    
    with patch("azure.storage.filedatalake.aio.DataLakeServiceClient") as mock_cls:
        mock_client = MagicMock()
        mock_client._pipeline.run = AsyncMock(side_effect=HttpResponseError(message="Forbidden"))
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client
        
        reachable = await AzureBlobFileSystem._check_dfs_reachability(
            fs, "https://myaccount.dfs.core.windows.net", timeout=1.0
        )
        assert reachable is True
        mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_dfs_reachability_network_failure():
    """Verify reachability returns False on network timeout / drop."""
    fs = MagicMock(spec=AzureBlobFileSystem)
    
    with patch("azure.storage.filedatalake.aio.DataLakeServiceClient") as mock_cls:
        mock_client = MagicMock()
        mock_client._pipeline.run = AsyncMock(side_effect=ServiceRequestError("Connection timed out"))
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client
        
        reachable = await AzureBlobFileSystem._check_dfs_reachability(
            fs, "https://myaccount.dfs.core.windows.net", timeout=1.0
        )
        assert reachable is False
        mock_client.close.assert_awaited_once()


def test_init_datalake_client_probes_and_disables_on_failure():
    """If probe fails at init, datalake_service_client is set to None."""
    fs = MagicMock(spec=AzureBlobFileSystem)
    fs.hns_enabled = True
    fs._dfs_url = "https://myaccount.dfs.core.windows.net"
    fs.check_dfs = True
    fs.dfs_probe_timeout = 2.0
    fs.loop = MagicMock()

    with patch("adlfs.spec.sync", return_value=False):
        AzureBlobFileSystem._init_datalake_client(fs)

    assert fs.datalake_service_client is None


def test_init_datalake_client_probes_and_enables_on_success():
    """If probe succeeds at init, datalake_service_client is initialized."""
    fs = MagicMock(spec=AzureBlobFileSystem)
    fs.hns_enabled = True
    fs._dfs_url = "https://myaccount.dfs.core.windows.net"
    fs.check_dfs = True
    fs.dfs_probe_timeout = 2.0
    fs.credential = MagicMock()
    fs.account_key = None
    fs.sas_token = None
    fs._timeout_kwargs = {"connection_timeout": 10}
    fs.loop = MagicMock()

    with patch("adlfs.spec.sync", return_value=True):
        with patch("adlfs.spec.weakref"):
            with patch("azure.storage.filedatalake.aio.DataLakeServiceClient") as mock_cls:
                AzureBlobFileSystem._init_datalake_client(fs)
                mock_cls.assert_called_once()
                call_kw = mock_cls.call_args.kwargs
                assert call_kw["account_url"] == fs._dfs_url
                assert call_kw["connection_timeout"] == 10
                assert hasattr(fs, "datalake_service_client")


def test_init_datalake_client_skips_probe_when_check_dfs_false():
    """When check_dfs is False, probing is bypassed."""
    fs = MagicMock(spec=AzureBlobFileSystem)
    fs.hns_enabled = True
    fs._dfs_url = "https://myaccount.dfs.core.windows.net"
    fs.check_dfs = False
    fs.credential = MagicMock()
    fs.account_key = None
    fs.sas_token = None
    fs._timeout_kwargs = {}
    fs.loop = MagicMock()

    with patch("adlfs.spec.sync") as mock_sync:
        with patch("adlfs.spec.weakref"):
            with patch("azure.storage.filedatalake.aio.DataLakeServiceClient") as mock_cls:
                AzureBlobFileSystem._init_datalake_client(fs)
                mock_sync.assert_not_called()
                mock_cls.assert_called_once()


@pytest.mark.asyncio
async def test_mv_file_circuit_breaker():
    """If DFS rename fails, datalake_service_client is disabled (circuit breaker)."""
    fs = MagicMock(spec=AzureBlobFileSystem)
    fs.hns_enabled = True
    fs.datalake_service_client = MagicMock()
    fs._isdir = AsyncMock(return_value=False)
    fs._cp_file = AsyncMock()
    fs._rm_file = AsyncMock()
    fs.split_path = lambda path, **kw: (
        ("container", "src.txt", None) if "src" in path else ("container", "dst.txt", None)
    )

    # Simulate failure on DFS file client rename
    fc = MagicMock()
    fc.rename_file = AsyncMock(side_effect=ServiceRequestError("Firewall dropped connection"))
    fsc = MagicMock()
    fsc.get_file_client = MagicMock(return_value=fc)
    fs.datalake_service_client.get_file_system_client = MagicMock(return_value=fsc)

    await AzureBlobFileSystem._mv_file(fs, "container/src.txt", "container/dst.txt")

    # Circuit breaker tripped:
    assert fs.datalake_service_client is None
    # Fallback copy + delete executed:
    fs._cp_file.assert_awaited_once()
    fs._rm_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_mv_file_skips_dfs_when_client_is_none():
    """When datalake_service_client is None, DFS rename is skipped entirely with 0 delay."""
    fs = MagicMock(spec=AzureBlobFileSystem)
    fs.hns_enabled = True
    fs.datalake_service_client = None
    fs._cp_file = AsyncMock()
    fs._rm_file = AsyncMock()
    fs.split_path = lambda path, **kw: (
        ("container", "src.txt", None) if "src" in path else ("container", "dst.txt", None)
    )

    await AzureBlobFileSystem._mv_file(fs, "container/src.txt", "container/dst.txt")

    # Directly executed copy + delete without touching DFS:
    fs._cp_file.assert_awaited_once()
    fs._rm_file.assert_awaited_once()
