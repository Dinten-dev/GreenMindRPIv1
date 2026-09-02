from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"


def test_gateway_restarts_without_rate_limit_and_waits_for_storage():
    service = (SYSTEMD / "greenmind-gateway.service").read_text(encoding="utf-8")

    assert "StartLimitIntervalSec=0" in service
    assert "Restart=always" in service
    assert "RequiresMountsFor=/opt/greenmind/current /opt/greenmind/data" in service
    assert "tailscaled.service" in service


def test_healthcheck_is_local_and_restarts_only_the_gateway():
    script = (SYSTEMD / "greenmind-healthcheck.sh").read_text(encoding="utf-8")

    assert "http://127.0.0.1/api/v1/health" in script
    assert "restart greenmind-gateway.service" in script


def test_persistent_journal_has_storage_bounds():
    config = (SYSTEMD / "greenmind-journald.conf").read_text(encoding="utf-8")

    assert "Storage=persistent" in config
    assert "SystemMaxUse=256M" in config
    assert "MaxRetentionSec=30day" in config
