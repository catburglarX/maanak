#!/bin/sh
# Generate an Alembic revision by comparing app.models against the live database.
#
# The generated file contains explicit DDL. Review it before committing: released
# migrations must never import app.models.
#
# Usage (from the repository root):
#   docker compose run --rm --no-deps -v "$PWD/api:/app" migrate sh scripts/makemigration.sh "message"
set -eu

MESSAGE=${1:-"schema change"}

# ruff is a dev-only dependency; the post-write hook needs it present.
python -c "import ruff" 2>/dev/null || pip install --quiet --no-cache-dir ruff==0.8.4

alembic revision --autogenerate -m "$MESSAGE"

echo "generated:"
ls -1t migrations/versions/*.py | head -1
