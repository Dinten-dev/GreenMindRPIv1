"""Authentication headers for gateway-to-cloud requests."""


def gateway_headers(api_key: str, gateway_id: str | None = None) -> dict[str, str]:
    headers = {"X-Api-Key": api_key}
    if isinstance(gateway_id, str) and gateway_id:
        headers["X-Gateway-Id"] = gateway_id
    return headers
