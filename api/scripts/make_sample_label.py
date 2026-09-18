"""Write the generated declaration panel to a file.

The seeder and the matters suite build a legible label in memory. The browser suite
needs the same image as a file to hand to a file input, and the browser image does not
carry the application dependencies, so it is produced here and read there.

    docker compose run --rm --no-deps -v "$PWD/api:/app" \
      --entrypoint python api scripts/make_sample_label.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_matters import make_label

#: make_label writes JPEG bytes, so the extension has to say so. The upload endpoint
#: sniffs the actual content and refuses a mismatch, which is the behaviour being relied
#: on elsewhere in this suite.
DESTINATION = Path(os.environ.get("SAMPLE_LABEL_PATH", "/app/screenshots/clean-label.jpg"))


def main() -> int:
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    data = make_label()
    DESTINATION.write_bytes(data)
    print(f"wrote {DESTINATION} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
