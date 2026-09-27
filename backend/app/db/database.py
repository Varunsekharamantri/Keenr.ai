import logging
import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session
from ..config import settings
from ..models.schema import Base

logger = logging.getLogger(__name__)

# Create SQLite engine
# connect_args={"check_same_thread": False} is needed for SQLite with FastAPI multi-threading
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
    echo=False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Columns added to already-populated tables after their first release.
# create_all() only creates *missing tables* — it will never add a column to an
# existing one — and this project has no Alembic. SQLite does support
# ALTER TABLE ADD COLUMN, so this is the minimum honest migration step.
# Every entry must be nullable so existing rows stay valid without a backfill.
_ADDED_COLUMNS = {
    "companies": [
        ("country", "VARCHAR(80)"),
        ("region", "VARCHAR(40)"),
        # BFSI vertical and sub-industry, for the Companies solar system
        # (see app/db/bfsi_taxonomy.py).
        ("vertical", "VARCHAR(60)"),
        ("sub_industry", "VARCHAR(80)"),
    ],
    # Every profile a leader search returned, kept or not, so a change to the
    # verification rules can be replayed without spending another search.
    "leader_fetches": [
        ("candidates", "JSON"),
    ],
}


def ensure_columns():
    """Idempotently add post-release columns to existing tables."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all() will build it with the columns already present
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl_type in columns:
                if name in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))
                logger.info(f"Added column {table}.{name}")


def init_db():
    Base.metadata.create_all(bind=engine)
    ensure_columns()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
