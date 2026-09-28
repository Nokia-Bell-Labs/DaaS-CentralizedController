#!/usr/bin/env bash
# Start the controller with the existing virtual environment.
# Install dependencies first with ./check_dependencies.sh.
# Extra arguments are passed to start_server.py, for example:
#   ./start_daas_cc.sh --config daas_cc/examples/poc-10G-config.json

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

venv_python="$ROOT/.venv/bin/python"
if [[ ! -x "$venv_python" ]]; then
  echo "No virtual environment in .venv. Run ./check_dependencies.sh first." >&2
  exit 1
fi

exec "$venv_python" "$ROOT/start_server.py" "$@"
