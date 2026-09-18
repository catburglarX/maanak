#!/bin/sh
# Run the quality gates the CI pipeline enforces: format, lint and types.
#
#   docker compose run --rm --no-deps --entrypoint sh api scripts/quality.sh
#
# Pass --fix to apply formatting and safe lint fixes rather than only reporting.
set -eu

PATH="$PATH:/home/maanak/.local/bin:$HOME/.local/bin"
export PATH

# The image runs as a non-root user and the source may be mounted read-only, so tool
# caches go to a writable location rather than beside the code.
export RUFF_CACHE_DIR="${RUFF_CACHE_DIR:-/tmp/ruff-cache}"
export MYPY_CACHE_DIR="${MYPY_CACHE_DIR:-/tmp/mypy-cache}"

python -c "import ruff" 2>/dev/null || command -v ruff >/dev/null 2>&1 || \
  pip install --quiet --no-cache-dir ruff==0.8.4
command -v mypy >/dev/null 2>&1 || pip install --quiet --no-cache-dir mypy==1.14.0

status=0

if [ "${1:-}" = "--fix" ]; then
  echo "== applying formatting and safe fixes =="
  ruff check --fix . || true
  ruff format .
fi

echo "== format =="
ruff format --check . || status=1

echo "== lint =="
ruff check . || status=1

echo "== types =="
# The stub-absence codes are excluded on the command line as well as in
# pyproject.toml: several pinned dependencies (reportlab, boto3, pytesseract,
# zxing-cpp, qrcode) ship no type information, and their call sites are covered by the
# verification suites instead. Everything else is checked.
mypy app \
  --disable-error-code=import-untyped \
  --disable-error-code=import-not-found \
  || status=1

if [ "$status" -eq 0 ]; then
  echo
  echo "all quality gates passed"
fi
exit "$status"
