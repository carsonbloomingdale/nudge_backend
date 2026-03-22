# nudge_backend

## Environment variables

Copy `.env.example` to `.env` and set values. The app loads `.env` automatically via `python-dotenv` (see `database.py`).

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | SQLite (default) or Postgres URL (`postgres://` is normalized to `postgresql://`) |
| `CORS_ORIGINS` | Comma-separated allowed origins, or `*` for any (wildcard disables credentialed CORS) |
| `OPENAI_API_KEY` | Required for OpenAI-backed routes |
| `PORT` | Used when starting with `python main.py` |

**Run locally**

**Do not** run the global `uvicorn` from Homebrew (`/opt/homebrew/...`). It uses Homebrew Python and **will not see** `sqlalchemy` from this project → `ModuleNotFoundError`.

**Python 3.14:** many packages on PyPI still have **no matching distribution** (e.g. `click`). Use **3.12 or 3.11** for this project. On macOS: `brew install python@3.12`, then run `./scripts/dev.sh` (it prefers `python3.12` when creating `.venv`).

**Easiest:** from the repo root:

```bash
chmod +x scripts/dev.sh   # once
./scripts/dev.sh
```

That creates/uses `.venv`, installs `requirements.txt`, and runs `python -m uvicorn`.

**Manual (same idea):**

```bash
cd /path/to/nudge_backend
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# optional: .env.local overrides .env (gitignored)
# edit .env and/or .env.local

python -m uvicorn main:app --reload --host 0.0.0.0 --port "${PORT:-8000}"
```

**Cursor / VS Code:** Run and debug → **“FastAPI: uvicorn (project .venv)”** (`.vscode/launch.json`).

Or: `python main.py` (reads `PORT` from env) **after** `source .venv/bin/activate`.

**`No module named uvicorn`:** your active Python doesn’t have deps installed. Run:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Or delete and recreate the venv: `rm -rf .venv && ./scripts/dev.sh`.