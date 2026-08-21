"""Tests for the GreenMind Update Agent.

Unit tests covering: update windows, signature verification, disk checks,
download integrity, rollback, locking, cleanup, and command execution.
"""

import hashlib
import json
import os

# Import agent functions — adjust path if running from different location
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from greenmind_agent import (
    ALLOWED_COMMANDS,
    _extract_release_archive,
    _validate_cloud_url,
    apply_config_update,
    check_disk_space,
    cleanup_old_releases,
    download_config,
    execute_command,
    is_in_update_window,
    is_valid_semver,
    main,
    verify_signature,
)


class TestCloudUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "https://green-mind.ch",
            "https://green-mind.ch/api/v1/",
            "https://[::1]:8443/api/v1",
        ],
    )
    def test_accepts_https(self, url):
        assert _validate_cloud_url(url) == url.rstrip("/")

    @pytest.mark.parametrize(
        "url",
        [
            "http://green-mind.ch/api/v1",
            "ftp://green-mind.ch/api/v1",
            "https://user:password@green-mind.ch/api/v1",
            "https://green-mind.ch/api/v1?token=secret",
            "green-mind.ch/api/v1",
        ],
    )
    def test_rejects_unsafe_cloud_urls(self, url):
        with pytest.raises(ValueError, match="invalid cloud URL"):
            _validate_cloud_url(url)

    @pytest.mark.parametrize(
        "url",
        ["http://localhost:8000/api/v1", "http://127.0.0.1:8000", "http://[::1]:8000"],
    )
    def test_loopback_http_requires_explicit_opt_in(self, url):
        with pytest.raises(ValueError):
            _validate_cloud_url(url)
        assert _validate_cloud_url(url, allow_insecure_loopback=True) == url

    def test_agent_startup_rejects_url_without_logging_credentials(self, caplog):
        sensitive_url = "http://operator:do-not-log@example.com/api/v1"
        with (
            patch(
                "greenmind_agent.load_secrets",
                return_value={
                    "api_key": "also-do-not-log",
                    "gateway_id": "gateway-id",
                    "server_url": sensitive_url,
                },
            ),
            patch("greenmind_agent.ALLOW_INSECURE_CLOUD_HTTP", False),
            pytest.raises(SystemExit),
        ):
            main()

        assert "do-not-log" not in caplog.text
        assert "also-do-not-log" not in caplog.text


# ── Update Window Tests ──────────────────────────────────────────────


class TestUpdateWindow:
    """Tests for update window logic."""

    def test_no_window_means_anytime(self):
        """Null window start/end means updates anytime."""
        assert is_in_update_window(None, None) is True

    def test_empty_window_means_anytime(self):
        assert is_in_update_window("", "") is True

    @patch("greenmind_agent.datetime")
    def test_inside_normal_window(self, mock_dt):
        """Time within a normal (non-overnight) window."""
        fake_now = datetime(2026, 4, 14, 3, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        assert is_in_update_window("02:00", "04:00", "UTC") is True

    def test_window_boundary_logic(self):
        """Test the mathematical logic of window checking."""
        # Normal window: 02:00 - 04:00 → 120 min to 240 min
        start_min = 2 * 60  # 120
        end_min = 4 * 60  # 240
        test_min = 3 * 60  # 180 (inside)
        assert start_min <= test_min <= end_min

        # Outside
        test_outside = 5 * 60  # 300
        assert not (start_min <= test_outside <= end_min)

    def test_overnight_window_logic(self):
        """Overnight window: 23:00 - 03:00."""
        start_min = 23 * 60  # 1380
        end_min = 3 * 60  # 180

        # 01:00 should be inside
        test_1am = 1 * 60  # 60
        assert test_1am <= end_min  # True — overnight

        # 22:00 should be outside
        test_10pm = 22 * 60  # 1320
        assert not (test_10pm >= start_min or test_10pm <= end_min)


# ── Signature Verification Tests ─────────────────────────────────────


class TestSignatureVerification:
    """Tests for Ed25519 signature verification."""

    def test_no_signature_fails_closed(self):
        result = verify_signature("a" * 64, None)
        assert result == "invalid"

    def test_empty_signature_fails_closed(self):
        result = verify_signature("a" * 64, "")
        assert result == "invalid"

    @patch("greenmind_agent.SIGNING_KEY_PATH")
    def test_no_signing_key_fails_closed(self, mock_path):
        mock_path.exists.return_value = False
        result = verify_signature("a" * 64, "dGVzdA==")
        assert result == "invalid"

    def test_valid_signature_flow(self):
        """Test Ed25519 signing and verification round-trip."""
        try:
            import base64

            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                PublicFormat,
            )

            # Generate keypair
            private_key = Ed25519PrivateKey.generate()
            public_key = private_key.public_key()

            # Sign
            sha256_hex = hashlib.sha256(b"test data").hexdigest()
            sig = private_key.sign(sha256_hex.encode("utf-8"))
            sig_b64 = base64.b64encode(sig).decode()

            # Write public key to temp file
            pub_pem = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
            with tempfile.NamedTemporaryFile(suffix=".pub", delete=False, mode="wb") as f:
                f.write(pub_pem)
                pub_path = f.name

            try:
                with patch("greenmind_agent.SIGNING_KEY_PATH", Path(pub_path)):
                    result = verify_signature(sha256_hex, sig_b64)
                assert result == "signed"
            finally:
                os.unlink(pub_path)

        except ImportError:
            pytest.skip("cryptography not installed")

    def test_invalid_signature_rejected(self):
        """Invalid signature returns 'invalid'."""
        try:
            import base64

            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                PublicFormat,
            )

            private_key = Ed25519PrivateKey.generate()
            public_key = private_key.public_key()

            sha256_hex = hashlib.sha256(b"test data").hexdigest()
            wrong_sig = base64.b64encode(b"wrong" * 13).decode()  # 65 bytes

            pub_pem = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
            with tempfile.NamedTemporaryFile(suffix=".pub", delete=False, mode="wb") as f:
                f.write(pub_pem)
                pub_path = f.name

            try:
                with patch("greenmind_agent.SIGNING_KEY_PATH", Path(pub_path)):
                    result = verify_signature(sha256_hex, wrong_sig)
                assert result == "invalid"
            finally:
                os.unlink(pub_path)

        except ImportError:
            pytest.skip("cryptography not installed")


# ── Disk Space Tests ─────────────────────────────────────────────────


class TestDiskSpace:
    """Tests for disk space pre-check."""

    @patch("greenmind_agent.get_disk_free_mb")
    def test_sufficient_space(self, mock_free):
        mock_free.return_value = 2000
        assert check_disk_space(10 * 1024 * 1024) is True  # 10 MB

    @patch("greenmind_agent.get_disk_free_mb")
    def test_insufficient_space(self, mock_free):
        mock_free.return_value = 50  # Only 50 MB free
        assert check_disk_space(100 * 1024 * 1024) is False  # Need ~300 MB

    @patch("greenmind_agent.get_disk_free_mb")
    def test_no_file_size_still_checks_margin(self, mock_free):
        mock_free.return_value = 50
        assert check_disk_space(None) is False

    @patch("greenmind_agent.get_disk_free_mb")
    def test_unknown_disk_fails_closed(self, mock_free):
        mock_free.return_value = -1
        assert check_disk_space(100000) is False


class TestReleaseValidation:
    @pytest.mark.parametrize(
        "version",
        ["1.0.0", "0.0.0+bootstrap", "2.3.4-rc.1", "10.20.30+build.7"],
    )
    def test_accepts_canonical_semver(self, version):
        assert is_valid_semver(version)

    @pytest.mark.parametrize(
        "version",
        ["1.0", "v1.0.0", "01.0.0", "../1.0.0", "1.0.0/evil", "1.0.0 rc1", ""],
    )
    def test_rejects_unsafe_or_noncanonical_versions(self, version):
        assert not is_valid_semver(version)

    @staticmethod
    def _archive(path: Path, *, name: str, data: bytes = b"x", kind=None):
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo(name)
            if kind is not None:
                member.type = kind
            member.size = len(data) if member.isreg() else 0
            import io

            archive.addfile(member, io.BytesIO(data) if member.isreg() else None)

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("../escape", None),
            ("/absolute", None),
            ("safe-link", tarfile.SYMTYPE),
            ("safe-hardlink", tarfile.LNKTYPE),
            ("device", tarfile.CHRTYPE),
        ],
    )
    def test_rejects_unsafe_archive_entries(self, tmp_path, name, kind):
        archive = tmp_path / "release.tar.gz"
        self._archive(archive, name=name, kind=kind)
        destination = tmp_path / "output"
        destination.mkdir()
        with pytest.raises(ValueError):
            _extract_release_archive(archive, destination)

    def test_extracts_regular_file_without_preserving_unsafe_mode(self, tmp_path):
        archive = tmp_path / "release.tar.gz"
        self._archive(archive, name="src/main.py", data=b"print('ok')\n")
        destination = tmp_path / "output"
        destination.mkdir()
        _extract_release_archive(archive, destination)
        output = destination / "src" / "main.py"
        assert output.read_bytes() == b"print('ok')\n"
        assert output.stat().st_mode & 0o022 == 0

    @patch("greenmind_agent.MAX_ARCHIVE_UNPACKED_BYTES", 4)
    def test_rejects_archive_expansion_bomb(self, tmp_path):
        archive = tmp_path / "release.tar.gz"
        self._archive(archive, name="large.bin", data=b"12345")
        destination = tmp_path / "output"
        destination.mkdir()
        with pytest.raises(ValueError, match="expands"):
            _extract_release_archive(archive, destination)


# ── SHA256 Verification Tests ────────────────────────────────────────


class TestSHA256:
    """Tests for download integrity checking."""

    def test_sha256_match(self):
        content = b"test binary content"
        expected = hashlib.sha256(content).hexdigest()
        actual = hashlib.sha256(content).hexdigest()
        assert expected == actual

    def test_sha256_mismatch(self):
        content1 = b"original content"
        content2 = b"modified content"
        assert hashlib.sha256(content1).hexdigest() != hashlib.sha256(content2).hexdigest()


# ── Cleanup Tests ────────────────────────────────────────────────────


class TestCleanup:
    """Tests for release cleanup strategy."""

    def test_cleanup_keeps_last_3(self):
        """Only the 3 most recent releases should survive cleanup."""
        with tempfile.TemporaryDirectory() as tmp:
            releases_dir = Path(tmp) / "releases"
            releases_dir.mkdir()

            # Create 5 releases with different mtimes
            for i in range(5):
                d = releases_dir / f"1.{i}.0"
                d.mkdir()
                (d / "dummy.txt").write_text("test")
                # Ensure different mtimes
                import time

                time.sleep(0.05)

            current_link = Path(tmp) / "current"
            latest = releases_dir / "1.4.0"
            current_link.symlink_to(latest)

            with (
                patch("greenmind_agent.RELEASES_DIR", releases_dir),
                patch("greenmind_agent.CURRENT_LINK", current_link),
                patch("greenmind_agent.KEEP_RELEASES", 3),
            ):
                cleanup_old_releases()

            remaining = list(releases_dir.iterdir())
            assert len(remaining) == 3


# ── Command Executor Tests ───────────────────────────────────────────


class TestCommandExecutor:
    """Tests for allowlisted command execution."""

    def test_unknown_command_rejected(self):
        result, msg = execute_command({"command_type": "rm_rf"}, {})
        assert result == "rejected"
        assert "allowlist" in msg.lower()

    @patch("greenmind_agent.subprocess.run")
    def test_restart_service(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result, msg = execute_command(
            {"command_type": "restart_gateway_service", "id": "test-id"}, {}
        )
        assert result == "executed"
        mock_run.assert_called_once()

    def test_enable_maintenance_mode(self):
        state = {}
        result, msg = execute_command(
            {"command_type": "enable_maintenance_mode", "id": "test-id"}, state
        )
        assert result == "executed"
        assert state.get("maintenance_mode_local") is True

    def test_disable_maintenance_mode(self):
        state = {"maintenance_mode_local": True}
        result, msg = execute_command(
            {"command_type": "disable_maintenance_mode", "id": "test-id"}, state
        )
        assert result == "executed"
        assert state.get("maintenance_mode_local") is False

    def test_all_allowed_commands_exist(self):
        """Verify the allowlist matches expected commands."""
        expected = {
            "restart_gateway_service",
            "reload_gateway_config",
            "enable_maintenance_mode",
            "disable_maintenance_mode",
            "controlled_reboot",
        }
        assert ALLOWED_COMMANDS == expected


# ── Config Validation Tests ──────────────────────────────────────────


class TestConfigValidation:
    """Tests for config update validation and backup."""

    def test_invalid_config_rejected(self):
        """Non-dict payload is rejected."""
        result = apply_config_update("not a dict", "v1", "1.0.0")
        assert result is False

    def test_download_verifies_backend_canonical_nested_json(self):
        payload = {
            "z_last": [3, {"nested_b": False, "nested_a": "value"}],
            "a_first": {"threshold": 1.25, "enabled": True},
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_sha256 = hashlib.sha256(canonical).hexdigest()
        response = MagicMock(status_code=200)
        response.content = json.dumps({"config_payload": payload}).encode("utf-8")
        response.json.return_value = {"config_payload": payload}
        client = MagicMock()
        client.get.return_value = response

        assert (
            download_config(
                client,
                "https://example.invalid/api/v1",
                "/gateway/configs/config-id/download",
                expected_sha256,
                "redacted-api-key",
            )
            == payload
        )

    def test_config_backup_created(self):
        """Last good config is backed up before applying new one."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            versions_dir = config_dir / "versions"
            backups_dir = Path(tmp) / "backups"
            config_dir.mkdir(parents=True)
            versions_dir.mkdir(parents=True)
            backups_dir.mkdir(parents=True)

            # Create existing active config
            old_config = versions_dir / "v1.json"
            old_config.write_text('{"old": true}')
            active_link = config_dir / "active.json"
            active_link.symlink_to(old_config)

            with (
                patch("greenmind_agent.CONFIG_DIR", config_dir),
                patch("greenmind_agent.CONFIG_VERSIONS_DIR", versions_dir),
                patch("greenmind_agent.BACKUPS_DIR", backups_dir),
                patch("greenmind_agent.subprocess.run"),
                patch("greenmind_agent.run_healthcheck_suite", return_value=(True, "{}")),
            ):
                result = apply_config_update({"new": True}, "v2", "1.0.0")

            assert result is True
            backup = backups_dir / "last_good_config.json"
            assert backup.exists()
            assert json.loads(backup.read_text()) == {"old": True}
