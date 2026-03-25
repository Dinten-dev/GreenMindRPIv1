"""SQLite engine setup with WAL mode for concurrent read/write safety."""

import logging
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/opt/greenmind/data/queue.db")

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

# Engine and session are created lazily by init_db()
engine = None
SessionLocal = None


def init_db() -> None:
    """Create the database directory, engine, tables, and session factory."""
    global engine, SessionLocal

    # Ensure directory exists (only at runtime, not at import time)
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_dir, exist_ok=True)

    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        """Enable WAL mode for better concurrent access on the Pi."""
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    logger.info("Initializing local SQLite at %s", DB_PATH)
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency yielding a DB session."""
    if SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
