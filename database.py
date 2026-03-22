import os
import re
from pathlib import Path

from typing import Annotated, Generator

from dotenv import load_dotenv
from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# Load from repo root (not cwd — fixes uvicorn/Cursor when cwd ≠ project dir)
_ROOT = Path(__file__).resolve().parent
if os.getenv("NUDGE_TESTING", "").lower() not in ("1", "true", "yes"):
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
if is_sqlite and ":memory:" in DATABASE_URL:
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    # Managed Postgres (Heroku and similar) may close idle connections or cycle during maintenance.
    # pool_pre_ping avoids reusing dead connections from the pool (e.g. AdminShutdown).
    if is_sqlite:
        engine = create_engine(DATABASE_URL, connect_args=connect_args)
    else:
        engine = create_engine(
            DATABASE_URL,
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_recycle=300,
        )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DbSession = Annotated[Session, Depends(get_db)]


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


def ensure_person_profile_columns(engine) -> None:
    """Add optional profile / SMS-prep columns (additive ALTERs only)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("person"):
        return
    cols = {c["name"] for c in insp.get_columns("person")}
    is_sqlite = engine.dialect.name == "sqlite"
    stmts: list[str] = []
    if "first_name" not in cols:
        stmts.append("ALTER TABLE person ADD COLUMN first_name VARCHAR(128)")
    if "last_name" not in cols:
        stmts.append("ALTER TABLE person ADD COLUMN last_name VARCHAR(128)")
    if "phone_e164" not in cols:
        stmts.append("ALTER TABLE person ADD COLUMN phone_e164 VARCHAR(20)")
    if "timezone" not in cols:
        stmts.append("ALTER TABLE person ADD COLUMN timezone VARCHAR(64)")
    if "sms_opt_in" not in cols:
        if is_sqlite:
            stmts.append("ALTER TABLE person ADD COLUMN sms_opt_in INTEGER NOT NULL DEFAULT 0")
        else:
            stmts.append("ALTER TABLE person ADD COLUMN sms_opt_in BOOLEAN NOT NULL DEFAULT false")
    if "phone_verified_at" not in cols:
        if is_sqlite:
            stmts.append("ALTER TABLE person ADD COLUMN phone_verified_at DATETIME")
        else:
            stmts.append("ALTER TABLE person ADD COLUMN phone_verified_at TIMESTAMPTZ")
    if not stmts:
        return
    with engine.begin() as conn:
        for sql in stmts:
            conn.execute(text(sql))
