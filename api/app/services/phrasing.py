"""Counted nouns for messages an officer reads.

Several messages are assembled from a number and a noun, and the shortcut for that
is "(s)": *3 finding(s) written*, *1 test(s) returned non-compliant*. It is the
clearest possible sign that nobody read the sentence, and it appears in the place a
reviewer looks first.

So the count decides the form. Nouns in this codebase are regular apart from a
handful, and those pass their plural explicitly.
"""

from __future__ import annotations

#: Nouns whose plural is not formed by adding "s".
IRREGULAR = {
    "analysis": "analyses",
    "entry": "entries",
    "category": "categories",
    "index": "indexes",
}


def plural(noun: str, count: int) -> str:
    """The singular or plural form of ``noun`` for ``count``."""
    if count == 1:
        return noun
    if noun in IRREGULAR:
        return IRREGULAR[noun]
    if noun.endswith("y") and len(noun) > 1 and noun[-2] not in "aeiou":
        return f"{noun[:-1]}ies"
    if noun.endswith(("s", "x", "z", "ch", "sh")):
        return f"{noun}es"
    return f"{noun}s"


def counted(count: int, noun: str) -> str:
    """``count`` followed by the right form of ``noun``, for example "1 test"."""
    return f"{count} {plural(noun, count)}"


def verb(count: int, singular: str, plural_form: str) -> str:
    """Subject-verb agreement for a counted noun: ``verb(n, "is", "are")``."""
    return singular if count == 1 else plural_form
