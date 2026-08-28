#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
bad=$(grep -rIlU $'\r' --exclude-dir=.git --exclude-dir=data --exclude-dir=logs \
  --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=.pytest_cache \
  --exclude='bulk_report.txt' . || true)
if [[ -n "$bad" ]]; then
  echo "CRLF line endings found:"
  echo "$bad"
  exit 1
fi
echo "OK: all text files use LF line endings."
