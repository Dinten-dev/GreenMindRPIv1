import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from gateway.config import Config
from gateway.queue import QueueItem, QueueStore


@dataclass
class ForwarderHealthState:
    backend_status: str = "unknown"
    last_forward_success: str | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def set_backend_status(self, status: str) -> None:
        with self._lock:
            self.backend_status = status

    def set_last_success_now(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self._lock:
            self.last_forward_success = now
            self.backend_status = "online"

    def snapshot(self) -> dict[str, str | None]:
        with self._lock:
            return {
                "backend_status": self.backend_status,
                "last_forward_success": self.last_forward_success,
            }


class Forwarder(threading.Thread):
    def __init__(self, config: Config, queue: QueueStore, state: ForwarderHealthState) -> None:
        super().__init__(daemon=True, name="forwarder")
        self._config = config
        self._queue = queue
        self._state = state
        self._stop_event = threading.Event()
        self._logger = logging.getLogger("plantwiki.forwarder")

    def stop(self) -> None:
        self._stop_event.set()

    def _build_request(self, item: QueueItem) -> urllib.request.Request:
        return urllib.request.Request(
            self._config.backend_url,
            data=item.payload.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._config.device_api_key}",
                "X-Gateway-Request-Id": item.request_id,
            },
            method="POST",
        )

    @staticmethod
    def _is_permanent_http_error(status_code: int) -> bool:
        if status_code in (408, 409, 425, 429):
            return False
        return 400 <= status_code < 500

    def _compute_backoff(self, attempts: int) -> int:
        candidate = self._config.retry_base_s * (2 ** max(0, attempts - 1))
        return min(self._config.retry_cap_s, candidate)

    @staticmethod
    def _next_retry_iso(seconds: int) -> str:
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return next_retry.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _handle_transient_failure(self, item: QueueItem, attempts: int, message: str) -> None:
        if attempts >= self._config.max_attempts:
            self._queue.dead_letter(item.id, attempts, f"max attempts reached: {message}")
            self._state.set_backend_status("offline")
            self._logger.error("Dead-lettered request_id=%s after %s attempts", item.request_id, attempts)
            return

        delay = self._compute_backoff(attempts)
        next_retry = self._next_retry_iso(delay)
        self._queue.schedule_retry(item.id, attempts, next_retry, message)
        self._state.set_backend_status("offline")
        self._logger.warning(
            "Retry scheduled request_id=%s attempts=%s delay=%ss error=%s",
            item.request_id,
            attempts,
            delay,
            message,
        )

    def _forward_item(self, item: QueueItem) -> None:
        attempts = item.attempts + 1
        req = self._build_request(item)

        try:
            with urllib.request.urlopen(req, timeout=self._config.request_timeout_s) as response:
                status = response.getcode()
                if 200 <= status < 300:
                    self._queue.mark_sent(item.id)
                    self._state.set_last_success_now()
                    self._logger.info("Forwarded request_id=%s status=%s", item.request_id, status)
                    return

                message = f"unexpected backend status {status}"
                self._handle_transient_failure(item, attempts, message)
                return

        except urllib.error.HTTPError as err:
            details = err.read(512).decode("utf-8", errors="replace") if err.fp else ""
            message = f"http {err.code}: {details}".strip()
            if self._is_permanent_http_error(err.code):
                self._queue.dead_letter(item.id, attempts, message)
                self._state.set_backend_status("online")
                self._logger.error(
                    "Dead-lettered request_id=%s due to permanent backend error=%s",
                    item.request_id,
                    message,
                )
            else:
                self._handle_transient_failure(item, attempts, message)
            return

        except urllib.error.URLError as err:
            self._handle_transient_failure(item, attempts, f"network error: {err.reason}")
            return

        except TimeoutError:
            self._handle_transient_failure(item, attempts, "timeout")
            return

        except Exception as err:  # noqa: BLE001
            self._handle_transient_failure(item, attempts, f"unexpected error: {err}")

    def run(self) -> None:
        self._logger.info("Forwarder started")
        while not self._stop_event.is_set():
            item = self._queue.next_ready()
            if item is None:
                self._stop_event.wait(self._config.poll_interval_s)
                continue

            self._forward_item(item)

        self._logger.info("Forwarder stopped")
