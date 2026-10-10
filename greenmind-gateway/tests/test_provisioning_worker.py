from src.provisioning.ble_worker import ProvisioningWorker, _provisioning_urls


def test_provisioning_urls_preserve_api_prefix():
    http_url, ws_url = _provisioning_urls("https://example.invalid/api/v1")
    assert http_url == "https://example.invalid/api/v1/provisioning"
    assert ws_url == "wss://example.invalid/api/v1/provisioning/ws"


def test_provisioning_worker_authenticates_http_and_websocket():
    worker = ProvisioningWorker(
        {
            "api_key": "secret",
            "server_url": "https://example.invalid/api/v1",
        }
    )
    assert worker.headers == {"X-Api-Key": "secret"}


def test_legacy_http_port_443_uses_tls_for_both_transports():
    http_url, ws_url = _provisioning_urls("http://example.invalid:443/api/v1")
    assert http_url == "https://example.invalid:443/api/v1/provisioning"
    assert ws_url == "wss://example.invalid:443/api/v1/provisioning/ws"
