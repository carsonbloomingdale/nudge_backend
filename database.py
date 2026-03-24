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


def ensure_person_enrichment_summary_column(engine) -> None:
    """Add person.enrichment_summary for compact LLM context (replaces huge taskHistory in prompts)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("person"):
        return
    cols = {c["name"] for c in insp.get_columns("person")}
    if "enrichment_summary" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE person ADD COLUMN enrichment_summary TEXT"))


def ensure_person_admin_columns(engine) -> None:
    """Add role/account lock/admin note/mfa flags for admin console authz."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("person"):
        return
    cols = {c["name"] for c in insp.get_columns("person")}
    is_sqlite = engine.dialect.name == "sqlite"
    stmts: list[str] = []
    if "role" not in cols:
        stmts.append("ALTER TABLE person ADD COLUMN role VARCHAR(32)")
    if "account_locked" not in cols:
        stmts.append(
            "ALTER TABLE person ADD COLUMN account_locked "
            + ("INTEGER NOT NULL DEFAULT 0" if is_sqlite else "BOOLEAN NOT NULL DEFAULT false")
        )
    if "admin_note" not in cols:
        stmts.append("ALTER TABLE person ADD COLUMN admin_note TEXT")
    if "mfa_enabled" not in cols:
        stmts.append(
            "ALTER TABLE person ADD COLUMN mfa_enabled "
            + ("INTEGER NOT NULL DEFAULT 0" if is_sqlite else "BOOLEAN NOT NULL DEFAULT false")
        )
    if not stmts:
        return
    with engine.begin() as conn:
        for sql in stmts:
            conn.execute(text(sql))
        # Backfill role for old rows.
        conn.execute(text("UPDATE person SET role='user' WHERE role IS NULL OR role=''"))


def ensure_journals_note_column(engine) -> None:
    """Add journals.note if the table predates that column (create_all does not ALTER)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("journals"):
        return
    cols = {c["name"] for c in insp.get_columns("journals")}
    if "note" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE journals ADD COLUMN note TEXT"))


def ensure_journal_schema(engine) -> None:
    """Add tasks.journal_id for DBs created before journals (create_all does not ALTER existing tables)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    is_sqlite = engine.dialect.name == "sqlite"
    if not insp.has_table("tasks"):
        return
    cols = {c["name"] for c in insp.get_columns("tasks")}
    if "journal_id" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN journal_id INTEGER"))
        if not is_sqlite and insp.has_table("journals"):
            conn.execute(
                text(
                    "ALTER TABLE tasks ADD CONSTRAINT fk_tasks_journal_id "
                    "FOREIGN KEY (journal_id) REFERENCES journals(journal_id) ON DELETE CASCADE"
                )
            )
