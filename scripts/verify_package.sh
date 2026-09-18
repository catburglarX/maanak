#!/usr/bin/env bash
# Verify the release archive.
#
#   bash scripts/verify_package.sh
#
# Extracts dist/maanak.zip into a scratch directory and confirms the archive, not the
# working tree, contains everything a clean installation needs. This is the check that
# catches a file which exists locally but was never added to the package.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="$ROOT/dist/maanak.zip"
CHECKSUM="$ROOT/dist/maanak.zip.sha256"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

failures=0
checks=0

check() {
  checks=$((checks + 1))
  if [ "$1" = "0" ]; then
    printf '  ok    %s\n' "$2"
  else
    printf '  FAIL  %s\n' "$2"
    failures=$((failures + 1))
  fi
}

echo "archive integrity"
[ -f "$ARCHIVE" ]; check $? "dist/maanak.zip exists"
[ -f "$CHECKSUM" ]; check $? "dist/maanak.zip.sha256 exists"

unzip -tq "$ARCHIVE" >/dev/null 2>&1; check $? "the archive is not corrupt"

(cd "$ROOT/dist" && if command -v sha256sum >/dev/null 2>&1; then
   sha256sum -c maanak.zip.sha256 >/dev/null 2>&1
 else
   shasum -a 256 -c maanak.zip.sha256 >/dev/null 2>&1
 fi)
check $? "the recorded checksum matches the archive"

echo "extraction into a clean directory"
unzip -q "$ARCHIVE" -d "$SCRATCH"
EXTRACTED="$SCRATCH/maanak"
[ -d "$EXTRACTED" ]; check $? "the archive extracts to a single maanak/ directory"

echo "required files are present in the archive"
REQUIRED=(
  "README.md"
  "compose.yaml"
  ".env.example"
  "api/Dockerfile"
  "api/requirements.txt"
  "api/requirements.lock.txt"
  "api/pyproject.toml"
  "api/alembic.ini"
  "api/app/main.py"
  "api/app/config.py"
  "api/migrations/env.py"
  "api/migrations/versions/0001_initial_schema.py"
  "api/migrations/versions/0002_audit_and_sequences.py"
  "api/scripts/run_tests.sh"
  "api/scripts/quality.sh"
  "api/tests/conftest.py"
  "web/Dockerfile"
  "web/nginx.conf"
  "web/index.html"
  "web/login.html"
  "web/app/overview.html"
  "web/app/inspection.html"
  "docs/ARCHITECTURE.md"
  "docs/DEPLOYMENT.md"
  "docs/LEGAL_SOURCES.md"
  "scripts/package.sh"
)
for path in "${REQUIRED[@]}"; do
  [ -e "$EXTRACTED/$path" ]; check $? "contains $path"
done

echo "artefacts that must not ship"
[ ! -f "$EXTRACTED/.env" ]; check $? "no .env with real secrets"
[ -z "$(find "$EXTRACTED" -name '__pycache__' -print -quit)" ]; check $? "no __pycache__ directories"
[ -z "$(find "$EXTRACTED" -name '*.pyc' -print -quit)" ]; check $? "no compiled Python files"
[ -z "$(find "$EXTRACTED" -name '.ruff_cache' -print -quit)" ]; check $? "no tool caches"
[ ! -d "$EXTRACTED/dist" ]; check $? "no nested dist directory"

echo "the archive is self-describing"
grep -q "docker compose" "$EXTRACTED/README.md"; check $? "README states how to start the stack"
grep -q "not a government" "$EXTRACTED/README.md"; check $? "README states it is not a government service"

echo "python files in the archive compile"
if command -v python3 >/dev/null 2>&1; then
  python3 -m compileall -q "$EXTRACTED/api/app" >/dev/null 2>&1
  check $? "every module under api/app compiles"
else
  echo "  note: python3 unavailable, skipped the compile check"
fi

echo
if [ "$failures" -gt 0 ]; then
  echo "$failures of $checks archive checks FAILED"
  exit 1
fi
echo "all $checks archive checks passed"
