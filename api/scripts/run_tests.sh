#!/bin/sh
# Run the Maanak test suite.
#
# Usage, from the repository root, with the stack already running. Mount the whole
# repository rather than api/ alone, so the prose and label checks can read web/ and
# docs/ as well:
#
#   docker compose run --rm --no-deps -v "$PWD:/repo" -w /repo/api \
#     -e PYTHONPATH=/repo/api --entrypoint sh api scripts/run_tests.sh
#
# Selection:
#   MAANAK_TEST_SCOPE=fast          unit tests only, no services needed
#   MAANAK_TEST_SCOPE=integration   the live-stack suites only
#   MAANAK_TEST_SCOPE=all           both (default)
set -eu

SCOPE=${MAANAK_TEST_SCOPE:-all}
export PYTHONPATH=${PYTHONPATH:-/w}
export API_URL=${API_URL:-http://api:8000}

# scripts/ sits inside api/, so two levels up is the repository root when the whole
# repository is mounted. When only api/ is mounted this resolves to / and web/ is
# absent, which the prose stage reports rather than silently skipping.
REPO_ROOT=$(cd "$(dirname "$0")/.." && cd .. && pwd)

python -c "import pytest" 2>/dev/null || pip install --quiet --no-cache-dir \
  pytest==8.3.4 pytest-asyncio==0.25.0

run_prose() {
  echo "== pages and documents =="
  if [ -d "$REPO_ROOT/web" ] && [ -d "$REPO_ROOT/docs" ]; then
    python "$(dirname "$0")/check_prose.py" \
      "$REPO_ROOT/web" "$REPO_ROOT/docs" "$REPO_ROOT/README.md"
  else
    echo "skipped: web/ and docs/ are not mounted."
    echo "Mount the repository root to include them:"
    echo '  docker compose run --rm --no-deps -v "$PWD:/repo" -w /repo/api \'
    echo '    -e PYTHONPATH=/repo/api --entrypoint sh api scripts/run_tests.sh'
    return 1
  fi
}

case "$SCOPE" in
  fast)
    echo "== fast unit tests =="
    python -m pytest tests -m "not integration and not browser" -q --no-header
    echo
    run_prose
    ;;
  integration)
    echo "== live-stack suites =="
    exec python -m pytest tests -m integration -q --no-header -p no:cacheprovider
    ;;
  all)
    echo "== fast unit tests =="
    python -m pytest tests -m "not integration and not browser" -q --no-header
    echo
    run_prose
    echo
    echo "== live-stack suites =="
    exec python -m pytest tests -m integration -q --no-header -p no:cacheprovider
    ;;
  *)
    echo "unknown MAANAK_TEST_SCOPE: $SCOPE" >&2
    exit 2
    ;;
esac
