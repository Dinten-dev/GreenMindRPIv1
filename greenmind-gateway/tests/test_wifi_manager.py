import asyncio
import logging
from unittest.mock import AsyncMock, patch

from src.network import wifi_manager
from src.network.wifi_manager import NetworkManager


def test_setup_ap_password_is_random_not_legacy_default():
    password = wifi_manager.get_setup_ap_password()
    assert password != "12345678"
    assert 16 <= len(password) <= 63


def test_command_redaction_covers_nmcli_password_forms():
    secret = "do-not-log-this"
    command = [
        "nmcli",
        "connection",
        "modify",
        "wifi",
        "wifi-sec.psk",
        secret,
    ]
    rendered = wifi_manager._redact_command(command)
    assert secret not in rendered
    assert "***REDACTED***" in rendered
    assert secret not in wifi_manager._redact_output(f"failure: {secret}", command)


def test_subprocess_failure_never_returns_or_logs_password(caplog):
    secret = "super-secret-password"

    class Process:
        returncode = 10

        async def communicate(self):
            return b"", f"nmcli rejected {secret}".encode()

    command = ["nmcli", "connection", "modify", "wifi", "wifi-sec.psk", secret]
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=Process())):
        with caplog.at_level(logging.ERROR):
            ok, output = asyncio.run(NetworkManager._run(command))

    assert not ok
    assert secret not in output
    assert secret not in caplog.text


def test_existing_ap_profile_is_refreshed_with_boot_password(monkeypatch):
    monkeypatch.setattr(wifi_manager, "_credentials_announced", True)
    run = AsyncMock(
        side_effect=[
            (True, ""),
            (True, wifi_manager.AP_CONNECTION_NAME),
            (True, ""),
            (True, ""),
        ]
    )
    monkeypatch.setattr(NetworkManager, "_run", run)

    assert asyncio.run(NetworkManager.start_ap(ssid="GreenMind-Gateway-ABCD"))
    modify_command = run.await_args_list[2].args[0]
    assert "wifi-sec.psk" in modify_command
    assert wifi_manager.get_setup_ap_password() in modify_command
