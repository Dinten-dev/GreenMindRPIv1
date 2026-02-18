import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    backend_url: str
    device_api_key: str
    listen_host: str
    listen_port: int
    db_path: str
    max_queue_rows: int
    log_level: str
    request_timeout_s: int
    max_attempts: int
    retry_base_s: int
    retry_cap_s: int
    poll_interval_s: float



def _parse_listen_addr(value: str) -> tuple[str, int]:
    if ":" not in value:
        raise ConfigError("LISTEN_ADDR must be in host:port format")
    host, port_text = value.rsplit(":", 1)
    if not host:
        raise ConfigError("LISTEN_ADDR host cannot be empty")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ConfigError("LISTEN_ADDR port must be an integer") from exc
    if port < 1 or port > 65535:
        raise ConfigError("LISTEN_ADDR port must be between 1 and 65535")
    return host, port



def _get_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ConfigError(f"{name} must be >= 0")
    return parsed



def _get_float(name: str, default: float) -> float:
    value = os.getenv(name, str(default)).strip()
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be > 0")
    return parsed



def _load_env_file(path: str) -> None:
    if not os.path.isfile(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            os.environ.setdefault(key, value)



def load_config() -> Config:
    explicit_env_file = os.getenv("PLANTWIKI_CONFIG_FILE", "").strip()
    if explicit_env_file:
        _load_env_file(explicit_env_file)
    else:
        default_env_file = "/etc/plantwiki-gateway/config.env"
        # Under systemd, EnvironmentFile already injects values; avoid hard-failing
        # when the service user cannot read a root-only config file.
        if os.access(default_env_file, os.R_OK):
            _load_env_file(default_env_file)

    backend_url = os.getenv("BACKEND_URL", "").strip()
    device_api_key = os.getenv("DEVICE_API_KEY", "").strip()
    listen_addr = os.getenv("LISTEN_ADDR", "0.0.0.0:8081").strip()

    if not backend_url:
        raise ConfigError("BACKEND_URL is required")
    if not device_api_key or device_api_key == "CHANGE_ME":
        raise ConfigError("DEVICE_API_KEY must be set")

    listen_host, listen_port = _parse_listen_addr(listen_addr)

    return Config(
        backend_url=backend_url,
        device_api_key=device_api_key,
        listen_host=listen_host,
        listen_port=listen_port,
        db_path=os.getenv("DB_PATH", "/var/lib/plantwiki-gateway/queue.db").strip(),
        max_queue_rows=_get_int("MAX_QUEUE_ROWS", 100000),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        request_timeout_s=_get_int("REQUEST_TIMEOUT_S", 10),
        max_attempts=_get_int("MAX_ATTEMPTS", 15),
        retry_base_s=_get_int("RETRY_BASE_S", 2),
        retry_cap_s=_get_int("RETRY_CAP_S", 300),
        poll_interval_s=_get_float("POLL_INTERVAL_S", 1.0),
    )
