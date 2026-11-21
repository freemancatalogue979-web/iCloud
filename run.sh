#!/usr/bin/env bash
# Launcher for the iCloud Browser Console.
#
#   ./run.sh            start the server
#   ./run.sh test       run the test suite
#   ./run.sh setup      create venv + install deps
#
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"

setup() {
  echo "==> Creating virtualenv"
  python3 -m venv "$VENV"
  echo "==> Installing Python dependencies"
  "$PY" -m pip install --upgrade pip >/dev/null
  "$PY" -m pip install -r requirements.txt
  echo
  echo "==> Next: download a browser binary"
  echo "    $PY -m playwright install --with-deps chromium"
  echo
  echo "==> Then: ./run.sh"
}

if [[ ! -x "$PY" ]]; then
  echo "No virtualenv found — running setup first."
  setup
  exit 0
fi

case "${1:-serve}" in
  setup)
    setup
    ;;
  test|tests)
    exec "$PY" -m pytest "${@:2}"
    ;;
  serve|"")
    exec "$PY" -m uvicorn app.main:app \
      --host "${ICLOUD_HOST:-0.0.0.0}" \
      --port "${ICLOUD_PORT:-8000}"
    ;;
  *)
    echo "usage: $0 [serve|test|setup]" >&2
    exit 2
    ;;
esac
