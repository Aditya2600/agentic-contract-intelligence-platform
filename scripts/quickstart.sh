#!/usr/bin/env bash
# The one command: clone -> working system, offline, in a couple of minutes.
#
# Creates a virtual environment if there isn't one, installs the project, runs the
# offline test suite, then drives seven documents through the whole pipeline -- both
# human gates included -- with no model API key, no GPU and no model server.
#
# Deliberately does everything in one shell so a reviewer needs no prior state: `make`
# runs each recipe line in its own shell, which cannot keep a virtualenv activated
# between them.
set -euo pipefail

cd "$(dirname "$0")/.."

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m==> %s\033[0m\n' "$*" >&2; exit 1; }

# Find an interpreter that actually satisfies pyproject's `requires-python = ">=3.11"`.
# Bare `python3` is 3.9 on a stock macOS, and letting pip discover that produces a
# resolver error three minutes in that says nothing about which command to run instead.
usable() {
  command -v "$1" >/dev/null 2>&1 &&
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

PYTHON=""
for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3 python; do
  [ -n "$candidate" ] || continue
  if usable "$candidate"; then PYTHON="$candidate"; break; fi
done

if [ -z "$PYTHON" ]; then
  die "doctask needs Python 3.11 or newer, and none was found on PATH.
    Found: $(python3 -V 2>&1 || echo 'no python3')
    Install one (e.g. 'brew install python@3.12'), or point this at yours:
        PYTHON=/path/to/python3.12 make quickstart"
fi

if [ ! -d .venv ]; then
  say "Creating .venv with $PYTHON ($("$PYTHON" -V 2>&1))"
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -m pip --version >/dev/null 2>&1; then
  # A uv-created venv has no pip. Bootstrap one rather than failing with a module error
  # that says nothing about how to fix it.
  say "No pip in .venv; bootstrapping it"
  python -m ensurepip --upgrade >/dev/null
fi

say "Installing doctask and its development dependencies"
# An old pip cannot parse a very new macOS version and picks no wheel. Upgrading is the
# fix; there is no need to lie to it about which OS this is.
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e '.[dev]'

if [ ! -f .env ]; then
  say "Creating .env from .env.example"
  # Filled in place rather than appended: a duplicated key would still work (the last
  # occurrence wins) but would leave an empty-looking line above the real one, which is
  # exactly the sort of thing someone edits and then wonders why nothing changed.
  python - <<'PY'
import secrets
from pathlib import Path

tokens = {
    "DOCTASK_REVIEWER_TOKENS": f"{secrets.token_hex(24)}:alice",
    "DOCTASK_SERVICE_TOKENS": f"{secrets.token_hex(24)}:ingest-bot",
}
lines = Path(".env.example").read_text().splitlines(keepends=True)
Path(".env").write_text("".join(
    f"{key}={tokens[key]}\n"
    if (key := line.split("=", 1)[0]) in tokens and line.rstrip("\n").endswith("=")
    else line
    for line in lines
))
PY
  echo "    generated a reviewer token and a service token into .env (gitignored)"
fi

say "Running the offline test suite"
pytest

say "Running the demo: 7 documents, 4 formats, one obligations register"
python scripts/run_demo.py

cat <<'EOF'

==> Done. What you just watched, and where to look next:

    make demo         re-run the seven-document walkthrough above
    make demo-crash   SIGKILL a run mid-flight and prove it resumes exactly once
                      (needs Docker: it brings up Postgres itself)
    make api          serve the REST API on http://localhost:8000/docs
    make test-pg      the same suite plus the Postgres integration tests

EOF
