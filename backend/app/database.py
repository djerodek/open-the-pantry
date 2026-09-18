import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

DATA_DIR = os.environ.get("RECIPE_APP_DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "recipes.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
TMP_DIR = os.path.join(DATA_DIR, "tmp")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    # Enforce foreign keys (off by default in SQLite) so ON DELETE CASCADE works.
    cursor.execute("PRAGMA foreign_keys=ON")
    # WAL mode lets readers (list/search/get) proceed while a write is in
    # progress, instead of the default rollback-journal mode where a write
    # blocks every reader for its duration. Worth having given requests are
    # served concurrently through FastAPI's thread pool. This setting is
    # stored in the database file itself (not per-connection), so issuing
    # it here is idempotent -- harmless to repeat on every new connection.
    # Known caveat: WAL relies on shared-memory file locking that isn't
    # reliable on some network filesystems (notably NFS) -- fine for local
    # disk, most NAS setups (SMB/local block mounts), and typical Docker
    # bind mounts, which covers this app's documented deployment cases.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
