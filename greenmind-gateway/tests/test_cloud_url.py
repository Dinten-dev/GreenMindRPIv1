import pytest
from pydantic import ValidationError

from src.cloud_url import validate_cloud_url
from src.config import Settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://green-mind.ch/api/v1", "https://green-mind.ch/api/v1"),
        ("https://green-mind.ch/api/v1/", "https://green-mind.ch/api/v1"),
        ("https://[::1]:8443/api/v1", "https://[::1]:8443/api/v1"),
    ],
)
def test_validate_cloud_url_accepts_and_normalizes_https(value, expected):
    assert validate_cloud_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://green-mind.ch/api/v1",
        "ftp://green-mind.ch/api/v1",
        "green-mind.ch/api/v1",
        "https://user:password@green-mind.ch/api/v1",
        "https://green-mind.ch/api/v1?token=secret",
        "https://green-mind.ch/api/v1#fragment",
        "https://green-mind.ch:invalid/api/v1",
        "",
    ],
)
def test_validate_cloud_url_rejects_insecure_or_malformed_values(value):
    with pytest.raises(ValueError):
        validate_cloud_url(value)


@pytest.mark.parametrize(
    "value",
    ["http://localhost:8000/api/v1", "http://127.0.0.2:8000", "http://[::1]:8000"],
)
def test_loopback_http_requires_explicit_opt_in(value):
    with pytest.raises(ValueError, match="HTTPS"):
        validate_cloud_url(value)
    assert validate_cloud_url(value, allow_insecure_loopback=True) == value


def test_opt_in_does_not_allow_http_to_non_loopback_host():
    with pytest.raises(ValueError, match="HTTPS"):
        validate_cloud_url(
            "http://gateway.internal/api/v1",
            allow_insecure_loopback=True,
        )


def test_settings_validate_both_cloud_urls_and_hide_invalid_input():
    sensitive_url = "https://operator:do-not-log@green-mind.ch/api/v1"
    with pytest.raises(ValidationError) as captured:
        Settings(
            _env_file=None,
            cloud_api_url=sensitive_url,
            firmware_api_url="https://green-mind.ch/api/v1",
        )

    assert "do-not-log" not in str(captured.value)


@pytest.mark.parametrize("field", ["cloud_api_url", "firmware_api_url"])
def test_settings_reject_public_http_for_every_cloud_base(field):
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            _env_file=None,
            **{field: "http://green-mind.ch/api/v1"},
        )


def test_settings_allow_opted_in_loopback_http():
    settings = Settings(
        _env_file=None,
        allow_insecure_cloud_http=True,
        cloud_api_url="http://127.0.0.1:8000/api/v1/",
        firmware_api_url="http://localhost:8000/api/v1/",
    )

    assert settings.cloud_api_url == "http://127.0.0.1:8000/api/v1"
    assert settings.firmware_api_url == "http://localhost:8000/api/v1"
