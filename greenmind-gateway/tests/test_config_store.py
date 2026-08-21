import json
import stat
from unittest.mock import patch

import pytest

from src.core.config_store import SecretStore, SecretStoreError


def test_save_is_atomic_durable_and_mode_restricted(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(str(path))
    fsync_calls = []

    real_fsync = __import__("os").fsync

    def recording_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    with patch("src.core.config_store.os.fsync", side_effect=recording_fsync):
        store.save({"api_key": "never-log-this", "gateway_id": "gateway-id"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "api_key": "never-log-this",
        "gateway_id": "gateway-id",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert len(fsync_calls) == 2
    assert list(tmp_path.glob(".secrets-*.tmp")) == []


def test_replace_failure_keeps_previous_credentials_and_removes_temp(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(str(path))
    store.save({"api_key": "old", "gateway_id": "old-gateway"})

    with (
        patch("src.core.config_store.os.replace", side_effect=OSError("replace failed")),
        pytest.raises(SecretStoreError, match="Cannot persist"),
    ):
        store.save({"api_key": "new", "gateway_id": "new-gateway"})

    assert store.load() == {"api_key": "old", "gateway_id": "old-gateway"}
    assert list(tmp_path.glob(".secrets-*.tmp")) == []


@pytest.mark.parametrize("contents", ["not-json", "[]"])
def test_invalid_store_fails_closed(tmp_path, contents):
    path = tmp_path / "secrets.json"
    path.write_text(contents, encoding="utf-8")
    store = SecretStore(str(path))

    with pytest.raises(SecretStoreError, match="invalid JSON|JSON object"):
        store.load()


def test_permission_failure_does_not_replace_existing_store(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(str(path))
    store.save({"api_key": "old", "gateway_id": "old-gateway"})

    with (
        patch("src.core.config_store.os.fchmod", side_effect=OSError("denied")),
        pytest.raises(SecretStoreError, match="Cannot persist"),
    ):
        store.save({"api_key": "new", "gateway_id": "new-gateway"})

    assert store.load() == {"api_key": "old", "gateway_id": "old-gateway"}
    assert list(tmp_path.glob(".secrets-*.tmp")) == []


def test_credentials_require_https_and_rejection_keeps_existing_store(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(str(path))
    store.save({"existing": "value"})

    with pytest.raises(SecretStoreError, match="Cloud URL is invalid"):
        store.store_credentials(
            api_key="api-key",
            gateway_id="gateway-id",
            zone_id="zone-id",
            hardware_id="hardware-id",
            server_url="http://cloud.example/api/v1",
        )

    assert store.load() == {"existing": "value"}


def test_stored_insecure_cloud_url_fails_closed(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(str(path))
    store.save(
        {
            "api_key": "api-key",
            "gateway_id": "gateway-id",
            "server_url": "http://cloud.example/api/v1",
        }
    )

    with pytest.raises(SecretStoreError, match="Stored cloud URL is invalid"):
        store.get_credentials()


def test_loopback_http_credentials_require_explicit_opt_in(tmp_path):
    store = SecretStore(
        str(tmp_path / "secrets.json"),
        allow_insecure_cloud_http=True,
    )
    store.store_credentials(
        api_key="api-key",
        gateway_id="gateway-id",
        zone_id="zone-id",
        hardware_id="hardware-id",
        server_url="http://127.0.0.1:8000/api/v1/",
    )

    assert store.get_credentials()["server_url"] == "http://127.0.0.1:8000/api/v1"
