#!/usr/bin/env bash
# Build the release archive.
#
#   bash scripts/package.sh
#
# Produces dist/maanak.zip and dist/maanak.zip.sha256.
#
# Excluded on purpose: the local .env (it holds real secrets), virtual environments,
# tool caches, build output, and the working database and object-storage volumes.
# .env.example is included so a fresh machine can create its own.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIST="$ROOT/dist"
STAGE="$DIST/stage/maanak"
ARCHIVE="$DIST/maanak.zip"

rm -rf "$DIST"
mkdir -p "$STAGE"

echo "staging files"

# An explicit include list rather than an exclude list: a new stray directory then
# cannot silently end up in a release.
INCLUDE=(
  "README.md"
  "compose.yaml"
  ".env.example"
  ".gitignore"
  "api"
  "web"
  "docs"
  "scripts"
  ".github"
)

for item in "${INCLUDE[@]}"; do
  if [ -e "$item" ]; then
    cp -r "$item" "$STAGE/"
  else
    echo "  note: $item not present, skipped"
  fi
done

echo "removing artefacts that must not ship"
find "$STAGE" \( \
     -name "__pycache__" -o \
     -name ".pytest_cache" -o \
     -name ".ruff_cache" -o \
     -name ".mypy_cache" -o \
     -name "*.pyc" -o \
     -name "*.pyo" -o \
     -name ".DS_Store" -o \
     -name "*.log" \
  \) -prune -exec rm -rf {} + 2>/dev/null || true

# A real .env must never be inside a distributed archive.
find "$STAGE" -name ".env" -delete 2>/dev/null || true

# Guard against committing a secret by accident. The example file legitimately
# contains the word, so it is excluded from the scan.
echo "scanning the staged tree for obvious secrets"
if grep -rIl --exclude=".env.example" --exclude-dir=".git" \
     -E "(BEGIN (RSA|OPENSSH|PRIVATE) KEY|AKIA[0-9A-Z]{16})" "$STAGE" 2>/dev/null | head -1; then
  echo "ERROR: the staged tree contains what looks like a private key or AWS key id"
  exit 1
fi

echo "writing the archive"
mkdir -p "$DIST"
(cd "$DIST/stage" && zip -q -r -X "$ARCHIVE" "maanak")
rm -rf "$DIST/stage"

echo "recording the checksum"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$DIST" && sha256sum "maanak.zip" > "maanak.zip.sha256")
else
  (cd "$DIST" && shasum -a 256 "maanak.zip" > "maanak.zip.sha256")
fi

SIZE="$(wc -c < "$ARCHIVE" | tr -d ' ')"
COUNT="$(unzip -Z1 "$ARCHIVE" | wc -l | tr -d ' ')"

echo
echo "archive: $ARCHIVE"
echo "size:    $SIZE bytes"
echo "entries: $COUNT"
echo "sha256:  $(cut -d' ' -f1 < "$DIST/maanak.zip.sha256")"
