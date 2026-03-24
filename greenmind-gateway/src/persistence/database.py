from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.persistence.models import Base
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/opt/greenmind/data/queue.db")

# Ensure the directory exists
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    logger.info(f"Initializing local SQLite persistence at {DB_PATH}")
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
