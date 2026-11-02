"""Compatibility of gateway authentication headers."""

from src.core.gateway_headers import gateway_headers


def test_gateway_headers_with_and_without_identity():
    assert gateway_headers("key", "id") == {"X-Api-Key": "key", "X-Gateway-Id": "id"}
    assert gateway_headers("key") == {"X-Api-Key": "key"}
    assert gateway_headers("key", "") == {"X-Api-Key": "key"}
