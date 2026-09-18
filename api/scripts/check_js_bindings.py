"""Find identifiers a module uses but never imports or declares.

A missing import in an ES module is not a load error. It throws a ReferenceError only
when the line runs, so a helper referenced in a rarely-taken branch, such as the empty
value of one table column, can sit broken for a long time and pass every page load.

This is a static check: it parses the import lists and the declarations in each module
under web/js and reports any capitalised-or-known helper that is referenced without
being available.

    docker run --rm -v "$PWD:/repo" -w /repo maanak-api:latest \
      python api/scripts/check_js_bindings.py web/js
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Everything the browser provides. Anything not declared, not imported and not here is
#: a missing binding.
GLOBALS = {
    "window",
    "document",
    "location",
    "navigator",
    "console",
    "fetch",
    "FormData",
    "URLSearchParams",
    "URL",
    "Date",
    "Math",
    "JSON",
    "Object",
    "Array",
    "String",
    "Number",
    "Boolean",
    "Map",
    "Set",
    "Promise",
    "Error",
    "RegExp",
    "Intl",
    "CSS",
    "DataTransfer",
    "AbortController",
    "setTimeout",
    "clearTimeout",
    "setInterval",
    "clearInterval",
    "requestAnimationFrame",
    "isNaN",
    "parseInt",
    "parseFloat",
    "encodeURIComponent",
    "decodeURIComponent",
    "localStorage",
    "sessionStorage",
    "history",
    "alert",
    "Image",
    "Blob",
    "TextDecoder",
    "structuredClone",
    "queueMicrotask",
    "performance",
    "crypto",
}

IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from", re.DOTALL)
DEFAULT_IMPORT_RE = re.compile(r"import\s+([A-Za-z_$][\w$]*)\s+from")
DECL_RE = re.compile(
    r"(?:^|[\s;{(])(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", re.MULTILINE
)
# `const { openDialog } = await import('../dialog.js')` is how the heavier modules are
# loaded on demand, so a destructured declaration binds names too.
DESTRUCTURE_RE = re.compile(r"(?:const|let|var)\s*\{([^}]*)\}\s*=")
PARAM_RE = re.compile(r"(?:function\s*[\w$]*\s*\(([^)]*)\)|\(([^)]*)\)\s*=>)")
COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
# Template literals and both quote styles. An escaped quote inside a string would
# otherwise end it early and leak the rest of the line into the scan.
STRING_RE = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\]|\\.)*`", re.DOTALL)
# Only flag identifiers that look like a module-level binding: a helper name or a
# SCREAMING_CASE constant. Property access and local variables are out of scope.
CANDIDATE_RE = re.compile(
    r"(?<![.\w$'\"])([A-Z][A-Z0-9_]{2,}|[a-z][a-zA-Z0-9]*)(?=\s*[(\s,;)\]}:])"
)


def bindings(source: str) -> set[str]:
    names: set[str] = set()
    for block in IMPORT_RE.findall(source):
        for part in block.split(","):
            part = part.strip()
            if not part:
                continue
            names.add(part.split(" as ")[-1].strip())
    names.update(DEFAULT_IMPORT_RE.findall(source))
    names.update(DECL_RE.findall(source))
    for block in DESTRUCTURE_RE.findall(source):
        for part in block.split(","):
            cleaned = part.split(":")[-1].split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", cleaned):
                names.add(cleaned)
    for a, b in PARAM_RE.findall(source):
        for group in (a, b):
            for param in group.split(","):
                cleaned = re.sub(r"[{}\[\]:.=]", " ", param).split()
                names.update(token for token in cleaned if re.fullmatch(r"[A-Za-z_$][\w$]*", token))
    return names


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else "web/js")
    files = sorted(root.rglob("*.js"))
    if not files:
        print(f"no JavaScript under {root}")
        return 2

    # Every name any module exports, so a typo in an import is caught too.
    exported: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        exported.update(re.findall(r"export\s+(?:const|let|function|class)\s+([\w$]+)", text))

    failures = 0
    for path in files:
        raw = path.read_text(encoding="utf-8")
        source = STRING_RE.sub('""', COMMENT_RE.sub(" ", raw))
        available = bindings(raw) | GLOBALS
        missing = sorted(
            {
                name
                for name in CANDIDATE_RE.findall(source)
                # Only report names another module actually exports. Anything else is
                # far more likely to be a property or a local this crude parse missed.
                if name in exported and name not in available
            }
        )
        if missing:
            failures += len(missing)
            print(f"  FAIL  {path}: uses {missing} without importing")
        else:
            print(f"  ok    {path}")

    print()
    print(f"missing bindings: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
