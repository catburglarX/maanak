"""Check that every permission the interface asks for actually exists.

`can('inspection.run_checks')` looks right and reads right, but the permission is
called `checks.run`. A name that does not exist is simply never held, so the control it
guards is hidden from everybody, including the roles that do hold the real permission.
Nothing fails loudly: the button is just absent.

This compares every string passed to `can(...)` and every `data-permission` attribute
against the Permission enum.

    docker run --rm -v "$PWD:/repo" -w /repo maanak-api:latest \
      python api/scripts/check_permission_names.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security.permissions import ROLE_PERMISSIONS, Permission

CAN_RE = re.compile(r"""\bcan\(\s*['"]([^'"]+)['"]""")
ATTR_RE = re.compile(r"""data-permission=['"]([^'"]+)['"]""")


def main() -> int:
    known = {permission.value for permission in Permission}
    usages: dict[str, list[str]] = {}

    for path in sorted(Path("web").rglob("*.js")) + sorted(Path("web").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for name in CAN_RE.findall(text) + ATTR_RE.findall(text):
            usages.setdefault(name, []).append(str(path))

    failures = 0
    for name in sorted(usages):
        where = ", ".join(sorted(set(usages[name])))
        if name in known:
            holders = sorted(
                role.value
                for role, perms in ROLE_PERMISSIONS.items()
                if any(p.value == name for p in perms)
            )
            print(f"  ok    {name}: held by {holders}")
        else:
            failures += 1
            close = sorted(k for k in known if name.split(".")[-1] in k or k.split(".")[-1] in name)
            print(f"  FAIL  {name} is not a permission. Used in {where}")
            if close:
                print(f"        did you mean: {close}")

    print()
    print(f"permission names used by the interface: {len(usages)}")
    print(f"names that do not exist: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
