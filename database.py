import os
import re
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

# Load from repo root (not cwd — fixes uvicorn/Cursor when cwd ≠ project dir)
_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")
load_dotenv(_ROOT / ".env.local", override=True)


def _normalize_database_url(raw: str | None) -> str:
    if raw is None:
        return "sqlite:///./nudge.db"
    url = raw.strip()
    if not url:
        return "sqlite:///./nudge.db"
    if len(url) >= 2 and url[0] == url[-1] and url[0] in "\"'":
        url = url[1:-1].strip()
    if not url:
        return "sqlite:///./nudge.db"
    url = re.sub(r'^["\']+|["\']+$', "", url).strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def _postgresql_use_psycopg3(url: str) -> str:
    if not url.startswith("postgresql"):
        return url
    if url.startswith("postgresql+"):
        return url
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


DATABASE_URL = _postgresql_use_psycopg3(_normalize_database_url(os.getenv("DATABASE_URL")))

try:
    make_url(DATABASE_URL)
except Exception as exc:
    scheme = DATABASE_URL.split(":", 1)[0] if ":" in DATABASE_URL else "(no scheme)"
    raise ValueError(
        "Invalid DATABASE_URL. Check .env / .env.local: one line, no stray quotes, "
        "use postgresql://... or sqlite:///./nudge.db. "
        f"(after cleanup: len={len(DATABASE_URL)}, leading scheme={scheme!r})"
    ) from exc

is_sqlite = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def ensure_auth_columns(engine) -> None:
    """Add password_hash to existing `person` tables (create_all does not alter columns)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("person"):
        return
    cols = {c["name"] for c in insp.get_columns("person")}
    if "password_hash" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE person ADD COLUMN password_hash VARCHAR"))
