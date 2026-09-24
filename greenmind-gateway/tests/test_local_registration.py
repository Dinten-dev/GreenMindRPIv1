import asyncio
from unittest.mock import Mock

import httpx
import pytest

from src.config import settings
from src.runtime import reset
from tools import register_sensor


def test_remote_reset_cannot_wipe_or_exit_with_default_guard(monkeypatch):
    monkeypatch.setattr(settings, "allow_remote_reset", False)
    store = Mock(side_effect=AssertionError("Must not access credentials"))
    exit_process = Mock(side_effect=AssertionError("Must not exit"))
    monkeypatch.setattr(reset, "SecretStore", store)
    monkeypatch.setattr(reset.sys, "exit", exit_process)
    asyncio.run(reset.trigger_remote_reset())
    store.assert_not_called()
    exit_process.assert_not_called()


@pytest.mark.parametrize("status", [200, 403])
def test_operator_registration_uses_gateway_without_cloud_key(monkeypatch, capsys, status):
    monkeypatch.setattr("sys.argv", ["register_sensor", "--gateway-ip", "192.168.1.10"])
    monkeypatch.setattr("builtins.input", lambda _: "aa-bb-cc-dd-ee-ff")
    monkeypatch.setattr(register_sensor.getpass, "getpass", lambda _: "ab1234")

    def post(url, **options):
        assert url == "http://192.168.1.10/api/v1/sensors/register"
        assert options["json"] == {"mac_address": "AA:BB:CC:DD:EE:FF", "code": "AB1234"}
        assert options["follow_redirects"] is False and options["trust_env"] is False
        assert "headers" not in options
        return httpx.Response(status, json={"status": "ok"})

    monkeypatch.setattr(register_sensor.httpx, "post", post)
    if status == 200:
        register_sensor.main()
        assert "Registration accepted" in capsys.readouterr().out
    else:
        with pytest.raises(SystemExit, match="Registration rejected"):
            register_sensor.main()
        assert "Registration accepted" not in capsys.readouterr().out
