#!/bin/sh
# Run the Maanak test suite.
#
# Usage, from the repository root, with the stack already running:
#
#   docker compose run --rm --no-deps -v "$PWD/api:/w" -w /w \
#     --entrypoint sh api scripts/run_tests.sh
#
# Selection:
#   MAANAK_TEST_SCOPE=fast          unit tests only, no services needed
#   MAANAK_TEST_SCOPE=integration   the live-stack suites only
#   MAANAK_TEST_SCOPE=all           both (default)
set -eu

SCOPE=${MAANAK_TEST_SCOPE:-all}
export PYTHONPATH=${PYTHONPATH:-/w}
export API_URL=${API_URL:-http://api:8000}

python -c "import pytest" 2>/dev/null || pip install --quiet --no-cache-dir \
  pytest==8.3.4 pytest-asyncio==0.25.0

case "$SCOPE" in
  fast)
    echo "== fast unit tests =="
    exec python -m pytest tests -m "not integration and not browser" -q --no-header
    ;;
  integration)
    echo "== live-stack suites =="
    exec python -m pytest tests -m integration -q --no-header -p no:cacheprovider
    ;;
  all)
    echo "== fast unit tests =="
    python -m pytest tests -m "not integration and not browser" -q --no-header
    echo
    echo "== live-stack suites =="
    exec python -m pytest tests -m integration -q --no-header -p no:cacheprovider
    ;;
  *)
    echo "unknown MAANAK_TEST_SCOPE: $SCOPE" >&2
    exit 2
    ;;
esac
