import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class QueueFullError(Exception):
    pass


@dataclass(frozen=True)
class QueueItem:
    id: int
    request_id: str
    payload: str
    attempts: int



def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class QueueStore:
    def __init__(self, db_path: str, max_rows: int) -> None:
        self._db_path = db_path
        self._max_rows = max_rows
        self._lock = threading.Lock()

        db_parent = Path(db_path).parent
        db_parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=FULL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._initialize()

    def _initialize(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY,
                    request_id TEXT UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_retry TIMESTAMP,
                    last_error TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_status_next_retry ON queue(status, next_retry, id)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def enqueue(self, request_id: str, payload: str) -> bool:
        created_at = utc_now_iso()
        with self._lock, self._conn:
            total_rows = self._conn.execute("SELECT COUNT(1) AS c FROM queue").fetchone()["c"]
            if total_rows >= self._max_rows:
                raise QueueFullError(f"Queue size limit reached ({self._max_rows})")

            try:
                self._conn.execute(
                    """
                    INSERT INTO queue(request_id, payload, created_at, status, attempts, next_retry, last_error)
                    VALUES(?, ?, ?, 'queued', 0, ?, NULL)
                    """,
                    (request_id, payload, created_at, created_at),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def queue_depth(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(1) AS c FROM queue WHERE status = 'queued'"
            ).fetchone()
            return int(row["c"])

    def dead_letter_depth(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(1) AS c FROM queue WHERE status = 'dead_letter'"
            ).fetchone()
            return int(row["c"])

    def next_ready(self) -> QueueItem | None:
        now = utc_now_iso()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, request_id, payload, attempts
                FROM queue
                WHERE status = 'queued' AND (next_retry IS NULL OR next_retry <= ?)
                ORDER BY id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            return QueueItem(
                id=int(row["id"]),
                request_id=str(row["request_id"]),
                payload=str(row["payload"]),
                attempts=int(row["attempts"]),
            )

    def mark_sent(self, row_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM queue WHERE id = ?", (row_id,))

    def schedule_retry(self, row_id: int, attempts: int, next_retry: str, error: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE queue
                SET attempts = ?, next_retry = ?, last_error = ?, status = 'queued'
                WHERE id = ?
                """,
                (attempts, next_retry, error[:500], row_id),
            )

    def dead_letter(self, row_id: int, attempts: int, error: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE queue
                SET attempts = ?, next_retry = NULL, last_error = ?, status = 'dead_letter'
                WHERE id = ?
                """,
                (attempts, error[:500], row_id),
            )
