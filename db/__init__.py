from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

from db import models  # noqa: F401  (registers tables on SQLModel.metadata)


def make_engine(db_path: Path) -> Engine:
    """Open the SQLite database, creating missing tables. Existing tables are never altered."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    SQLModel.metadata.create_all(engine)
    return engine
