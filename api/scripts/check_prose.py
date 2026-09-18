"""Scan the visible prose in HTML pages for machine-writing tells.

The point is not to score well on a regex. It is that the specific constructions a
language model reaches for by default are checkable, so there is no need to take
"this reads as human" on trust.

What it reports, in descending order of how damning each one is:

  fatal      tool leak markers, tracking parameters, chat pleasantries, placeholders
  structure  scaffold headings (Introduction, Challenges, Conclusion), title-case
             headings
  syntax     participial tails, negative parallelism, copula avoidance
  lexicon    the vocabulary that co-occurs in model output far more than in human
             writing

Usage:
    python scripts/check_prose.py web
    python scripts/check_prose.py web --verbose
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------- fatal

FATAL = {
    "tool leak marker": [
        r"contentReference",
        r"oai_citation",
        r"turn\d+search",
        r"turn\d+image",
        r"citeturn",
        r"\[cite:\s*\d",
        r"grok_render",
        r"ppl-ai-file-upload",
        r"\[attached_file:",
        r"\[web:\d+\]",
    ],
    "tracking parameter": [r"utm_source=", r"referrer=grok", r"utm_medium="],
    "chat pleasantry": [
        r"\bCertainly[!,]",
        r"\bOf course[!,]",
        r"I hope this helps",
        r"Let me know if",
        r"Would you like me to",
        r"\bAs an AI\b",
        r"In this article, we will",
        r"Here is a comprehensive",
    ],
    "knowledge-cutoff hedge": [
        r"as of my last update",
        r"up to my training data",
        r"while specific details are limited",
        r"based on available information",
    ],
    "placeholder": [
        r"\[Company Name\]",
        r"\[Insert\b",
        r"\[Your\b",
        r"\bTODO\b",
        r"\bTBD\b",
        r"20\d\d-xx-xx",
        r"\bLorem ipsum\b",
    ],
}

# ------------------------------------------------------------------------ structure

# Headings that are never legitimate, at any level. Each one is a container for
# content that should have been given its own descriptive name.
SCAFFOLD_ALWAYS = {
    "introduction",
    "conclusion",
    "in summary",
    "final thoughts",
    "key takeaways",
    "key features",
    "challenges and opportunities",
    "significance",
    "legacy",
    "future prospects",
    "future outlook",
    "awards and recognition",
}

# Headings that are only a problem as a section wrapper. "Overview" is a reasonable
# name for a landing page that summarises a workspace; it is not a reasonable name
# for a section inside a document, where it means the author had not decided what the
# section was about.
SCAFFOLD_WRAPPER_ONLY = {"overview", "summary", "impact", "challenges", "background"}

SMALL_WORDS = {
    "a",
    "an",
    "the",
    "of",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "with",
    "from",
    "by",
    "as",
    "is",
    "are",
    "not",
    "no",
    "it",
    "its",
    "this",
    "that",
    "you",
    "your",
    "we",
    "our",
    "be",
    "has",
    "have",
    "if",
    "than",
    "then",
    "what",
    "which",
    "who",
    "how",
    "when",
    "where",
    "why",
    "does",
    "do",
    "can",
    "must",
    "may",
    "will",
    "would",
    "should",
    "into",
    "over",
    "under",
    "up",
}

# --------------------------------------------------------------------------- syntax

SYNTAX = {
    "participial tail": [
        r",\s+(?:ensuring|highlighting|underscoring|showcasing|reflecting|cementing"
        r"|emphasi[sz]ing|demonstrating|solidifying|illustrating|signifying"
        r"|showcasing|paving the way|marking a)\b",
    ],
    "negative parallelism": [
        r"\bnot just\b",
        r"\bnot only\b(?=[^.]*\bbut\b)",
        r"\bisn't just\b",
        r"\bis not merely\b",
        r"\brather than merely\b",
        r"\bnot simply\b",
    ],
    "copula avoidance": [
        r"\bserves as\b",
        r"\bstands as\b",
        r"\bfunctions as\b",
        r"\bboasts\b",
        r"\bmarks a (?:significant|major|key)\b",
    ],
    "significance inflation": [
        r"\bplays a (?:key|vital|crucial|pivotal) role\b",
        r"\bstands as a testament\b",
        r"\bit is important to note\b",
        r"\bworth noting\b",
        r"\bvaluable insights\b",
        r"\bat its core\b",
        r"\bat the heart of\b",
        r"\bin today's (?:world|landscape)\b",
    ],
}

# -------------------------------------------------------------------------- lexicon

TIER_1 = [
    "delve",
    "showcase",
    "underscore",
    "tapestry",
    "testament",
    "realm",
    "interplay",
    "intricate",
    "pivotal",
    "meticulous",
    "garner",
    "foster",
    "bolster",
    "myriad",
    "plethora",
    "seamless",
    "holistic",
    "multifaceted",
    "profound",
    "vibrant",
    "groundbreaking",
    "revolutionary",
    "cutting-edge",
    "transformative",
    "unparalleled",
    "indelible",
    "ever-evolving",
    "resonate",
    "leverage",
    "utilize",
    "unpack",
    "shed light on",
    "deep dive",
    "nestled",
]

TIER_2 = [
    "robust",
    "comprehensive",
    "cornerstone",
    "hallmark",
    "ecosystem",
    "moreover",
    "furthermore",
    "additionally",
    "notably",
    "ultimately",
]

TAG_RE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
# Everything in <head> is metadata. A title separator is a typographic convention,
# not a sentence, and counting it as prose only produces noise.
HEAD_RE = re.compile(r"<head\b.*?</head>", re.DOTALL | re.IGNORECASE)
HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.DOTALL | re.IGNORECASE)
STRIP_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def visible_text(raw: str) -> str:
    body = HEAD_RE.sub(" ", raw)
    body = TAG_RE.sub(" ", body)
    body = STRIP_RE.sub(" ", body)
    return WS_RE.sub(" ", html.unescape(body)).strip()


def headings(raw: str) -> list[tuple[int, str]]:
    out = []
    for level, inner in HEADING_RE.findall(TAG_RE.sub(" ", raw)):
        text = WS_RE.sub(" ", html.unescape(STRIP_RE.sub(" ", inner))).strip()
        if text:
            out.append((int(level), text))
    return out


def is_title_case(text: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", text)
    if len(words) < 3:
        return False
    capitalised = [w for w in words[1:] if w[0].isupper() and w.lower() not in SMALL_WORDS]
    # Two or more mid-sentence capitals suggests title case rather than a proper noun.
    return len(capitalised) >= 2


def scan(path: Path, verbose: bool) -> tuple[int, int, list[str]]:
    raw = path.read_text(encoding="utf-8")
    text = visible_text(raw)
    lower = text.lower()
    words = len(re.findall(r"\b\w+\b", text))
    problems: list[str] = []
    review: list[str] = []

    for label, patterns in FATAL.items():
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                problems.append(f"FATAL {label}: {match.group(0)!r}")

    for level, head in headings(raw):
        name = head.strip().lower().rstrip(":")
        if name in SCAFFOLD_ALWAYS:
            problems.append(f"scaffold heading: {head!r}")
        elif name in SCAFFOLD_WRAPPER_ONLY and level >= 2:
            problems.append(f"scaffold section heading (h{level}): {head!r}")
        if is_title_case(head):
            review.append(f"possible title-case heading: {head!r}")

    for label, patterns in SYNTAX.items():
        for pattern in patterns:
            for match in re.finditer(pattern, lower):
                problems.append(f"{label}: {match.group(0).strip()!r}")

    for word in TIER_1:
        hits = len(re.findall(rf"\b{re.escape(word)}\w*\b", lower))
        if hits:
            problems.append(f"tier-1 vocabulary: {word!r} x{hits}")

    tier2_hits = sum(len(re.findall(rf"\b{re.escape(w)}\b", lower)) for w in TIER_2)
    if words and tier2_hits / max(words, 1) * 1000 >= 3:
        review.append(f"tier-2 density {tier2_hits} in {words} words")

    em = text.count("\u2014")
    if words and em > max(1, words // 500):
        review.append(f"em dashes: {em} in {words} words")

    lines = []
    if problems or (verbose and review):
        lines.append(f"  {path.name}  ({words} words)")
        for item in problems:
            lines.append(f"      {item}")
        if verbose:
            for item in review:
                lines.append(f"      review  {item}")
    return len(problems), words, lines


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    root = Path(argv[1])
    verbose = "--verbose" in argv
    files = sorted(root.rglob("*.html"))
    if not files:
        print(f"no HTML files under {root}")
        return 2

    total = 0
    words = 0
    output: list[str] = []
    for path in files:
        count, page_words, lines = scan(path, verbose)
        total += count
        words += page_words
        output.extend(lines)

    print(f"scanned {len(files)} pages, {words} words of visible prose")
    print()
    if output:
        print("\n".join(output))
        print()
    print(f"machine-writing tells found: {total}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
