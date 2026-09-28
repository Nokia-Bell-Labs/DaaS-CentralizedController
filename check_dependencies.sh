#!/usr/bin/env bash
# Create .venv if needed and install the controller dependencies.
# Run this once, and again after pyproject.toml changes. Starting the
# controller does not install anything.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi

if ! python3 -c "import venv" >/dev/null 2>&1; then
  echo "The Python venv module is missing. Install it with:" >&2
  echo "  sudo apt install python3-venv python3-pip" >&2
  exit 1
fi

venv_python="$ROOT/.venv/bin/python"
if [[ ! -x "$venv_python" ]] || ! "$venv_python" -m pip --version >/dev/null 2>&1; then
  echo "Creating a virtual environment in .venv"
  rm -rf "$ROOT/.venv"
  python3 -m venv "$ROOT/.venv"
fi

if ! "$venv_python" -m pip --version >/dev/null 2>&1; then
  echo "This virtual environment has no pip. Install it with:" >&2
  echo "  sudo apt install python3-venv python3-pip" >&2
  exit 1
fi

"$venv_python" -m pip install --upgrade pip
"$venv_python" -m pip install -e ".[api,test]"
echo "Dependencies are installed. Start the controller with ./start_daas_cc.sh"
