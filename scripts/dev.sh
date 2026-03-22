#!/usr/bin/env bash
# Run the API with the project venv — avoids Homebrew/global `uvicorn` (wrong Python, missing deps).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

pick_venv_python() {
  local cmd ver major minor
  for cmd in python3.12 python3.11 python3.13 python3.10; do
    if command -v "$cmd" >/dev/null 2>&1; then
      ver=$("$cmd" -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>/dev/null || echo "0 0")
      read -r major minor <<<"$ver" || true
      if [[ "$major" -eq 3 ]] && [[ "$minor" -ge 10 ]] && [[ "$minor" -le 13 ]]; then
        echo "$cmd"
        return 0
      fi
    fi
  done
  if command -v python3 >/dev/null 2>&1; then
    minor=$(python3 -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 99)
    major=$(python3 -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo 0)
    if [[ "$major" -eq 3 ]] && [[ "$minor" -lt 14 ]]; then
      echo "python3"
      return 0
    fi
  fi
  return 1
}

if [[ -d .venv ]]; then
  vver=$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "")
  if [[ "$vver" == "3.14" ]] || [[ "$vver" == "3.15" ]]; then
    echo "Your .venv uses Python $vver — many packages don't support it yet. Removing .venv..."
    rm -rf .venv
  fi
fi

if [[ ! -d .venv ]]; then
  if ! PY="$(pick_venv_python)"; then
    echo "No usable Python 3.10–3.13 found on PATH." >&2
    echo "Install one of them, e.g. on macOS: brew install python@3.12" >&2
    exit 1
  fi
  echo "Creating .venv with: $PY ($($PY -V 2>&1))"
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Using: $(command -v python) ($(python -V 2>&1))"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -c "import uvicorn" || {
  echo "uvicorn still missing after pip install. Try: rm -rf .venv && ./scripts/dev.sh" >&2
  exit 1
}
exec python -m uvicorn main:app --reload --host 0.0.0.0 --port "${PORT:-8000}"
