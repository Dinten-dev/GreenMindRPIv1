import hashlib
import json
import logging
import math
import signal
import threading
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from gateway.config import ConfigError, load_config
from gateway.forwarder import Forwarder, ForwarderHealthState
from gateway.queue import QueueFullError, QueueStore

LOGGER = logging.getLogger("plantwiki.gateway")
MAX_BODY_BYTES = 1024 * 1024



def iso_to_utc_z(value: str) -> str:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")



def validate_payload(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Payload must be an object")

    device_id_raw = data.get("device_id")
    if not isinstance(device_id_raw, str) or not device_id_raw.strip():
        raise ValueError("device_id must be a non-empty string")
    try:
        device_id = str(uuid.UUID(device_id_raw.strip()))
    except ValueError as exc:
        raise ValueError("device_id must be a valid UUID") from exc

    samples_raw = data.get("samples")
    if not isinstance(samples_raw, list) or not samples_raw:
        raise ValueError("samples must be a non-empty array")

    normalized_samples: list[dict[str, Any]] = []
    for index, sample in enumerate(samples_raw):
        if not isinstance(sample, dict):
            raise ValueError(f"samples[{index}] must be an object")

        timestamp_raw = sample.get("timestamp")
        metric_key_raw = sample.get("metric_key")
        species_name_raw = sample.get("species_name")
        value_raw = sample.get("value")

        if not isinstance(timestamp_raw, str) or not timestamp_raw.strip():
            raise ValueError(f"samples[{index}].timestamp must be a non-empty string")
        if not isinstance(metric_key_raw, str) or not metric_key_raw.strip():
            raise ValueError(f"samples[{index}].metric_key must be a non-empty string")
        if not isinstance(species_name_raw, str) or not species_name_raw.strip():
            raise ValueError(f"samples[{index}].species_name must be a non-empty string")
        if not isinstance(value_raw, (int, float)) or isinstance(value_raw, bool):
            raise ValueError(f"samples[{index}].value must be a number")
        if not math.isfinite(float(value_raw)):
            raise ValueError(f"samples[{index}].value must be finite")

        try:
            timestamp = iso_to_utc_z(timestamp_raw.strip())
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"samples[{index}].timestamp must be an ISO-8601 timestamp") from exc

        normalized_samples.append(
            {
                "timestamp": timestamp,
                "metric_key": metric_key_raw.strip(),
                "species_name": species_name_raw.strip(),
                "value": float(value_raw),
            }
        )

    return {
        "device_id": device_id,
        "samples": normalized_samples,
    }



def canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)



def request_id_for_payload(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


class GatewayHandler(BaseHTTPRequestHandler):
    queue_store: QueueStore
    forward_state: ForwarderHealthState

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _bad_request(self, message: str) -> None:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": message})

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/gw/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        snapshot = self.forward_state.snapshot()
        self._send_json(
            HTTPStatus.OK,
            {
                "queue_depth": self.queue_store.queue_depth(),
                "dead_letter_depth": self.queue_store.dead_letter_depth(),
                "last_forward_success": snapshot["last_forward_success"],
                "backend_status": snapshot["backend_status"],
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/gw/ingest":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        length_header = self.headers.get("Content-Length", "")
        try:
            content_length = int(length_header)
        except ValueError:
            self._bad_request("Invalid Content-Length")
            return

        if content_length <= 0:
            self._bad_request("Empty request body")
            return

        if content_length > MAX_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Payload too large"})
            return

        raw = self.rfile.read(content_length)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            self._bad_request("Body must be valid JSON")
            return

        try:
            normalized = validate_payload(parsed)
        except ValueError as err:
            self._bad_request(str(err))
            return

        payload_json = canonical_payload(normalized)
        request_id = request_id_for_payload(payload_json)

        try:
            inserted = self.queue_store.enqueue(request_id=request_id, payload=payload_json)
        except QueueFullError as err:
            LOGGER.error("Queue full. rejecting request_id=%s error=%s", request_id, err)
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "queue is full"})
            return

        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "status": "accepted",
                "request_id": request_id,
                "deduplicated": not inserted,
            },
        )

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.info("%s - %s", self.client_address[0], fmt % args)



def run() -> None:
    try:
        config = load_config()
    except ConfigError as err:
        raise SystemExit(f"configuration error: {err}") from err

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    queue_store = QueueStore(config.db_path, config.max_queue_rows)
    forward_state = ForwarderHealthState()
    forwarder = Forwarder(config, queue_store, forward_state)

    GatewayHandler.queue_store = queue_store
    GatewayHandler.forward_state = forward_state

    server = ThreadingHTTPServer((config.listen_host, config.listen_port), GatewayHandler)
    server.daemon_threads = True

    stop_event = threading.Event()

    def shutdown_handler(signum: int, _: Any) -> None:
        LOGGER.info("Received signal=%s, shutting down", signum)
        stop_event.set()
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    LOGGER.info("Gateway listening on %s:%s", config.listen_host, config.listen_port)
    forwarder.start()

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        forwarder.stop()
        forwarder.join(timeout=5)
        server.server_close()
        queue_store.close()


if __name__ == "__main__":
    run()
