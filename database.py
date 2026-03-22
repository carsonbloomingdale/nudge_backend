import os
import re

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

# `.env` then `.env.local` (local overrides; same idea as Next.js / Vite)
load_dotenv()
load_dotenv(".env.local", override=True)


def _normalize_database_url(raw: str | None) -> str:
    """Strip whitespace, unwrap common .env quote mistakes, normalize postgres scheme."""
    if raw is None:
        return "sqlite:///./nudge.db"
    url = raw.strip()
    if not url:
        return "sqlite:///./nudge.db"
    # One layer of surrounding quotes (e.g. DATABASE_URL="..." with extra quotes in file)
    if len(url) >= 2 and url[0] == url[-1] and url[0] in "\"'":
        url = url[1:-1].strip()
    if not url:
        return "sqlite:///./nudge.db"
    # Remove accidental wrapping quotes inside value
    url = re.sub(r'^["\']+|["\']+$', "", url).strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def _postgresql_use_psycopg3(url: str) -> str:
    """Plain postgresql:// defaults to psycopg2 in SQLAlchemy; we ship psycopg (v3)."""
    if not url.startswith("postgresql"):
        return url
    # Already specifies a driver, e.g. postgresql+psycopg2://
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