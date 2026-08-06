"""Trim page text down to the part that actually answers the question.

On a CPU-only machine, prompt *prefill* dominates: the model must read every
token before emitting one. A 12,000-character page is ~3,000 tokens, and at
the 20-60 tok/s prefill a laptop CPU manages that is one to three minutes per
business before generation even starts.

Most of those characters are navigation, opening hours and footer boilerplate.
`condense` keeps the top of the page (title, h1, hero copy -- usually the most
informative part) plus windows around the words that bear on the question, and
throws the rest away. Shorter prompts are also *more* accurate here: less
irrelevant text to distract a small model.
"""

from __future__ import annotations

import re

# What tells you whether a business fits an ICP.
ICP_HINTS = (
    "about", "we are", "we offer", "our services", "services", "specialize",
    "family owned", "since", "established", "founded", "locally owned",
    "our team", "welcome",
)

# What tells you who owns it.
OWNER_HINTS = (
    "owner", "owned", "founder", "founded", "president", "principal",
    "proprietor", "director", "our team", "meet the", "about us", "staff",
    "family", "generation", "started", "leadership", "manager",
)

WS = re.compile(r"[ \t]+")
BLANK = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    return BLANK.sub("\n\n", WS.sub(" ", text or "")).strip()


def condense(
    text: str,
    hints: tuple[str, ...] = ICP_HINTS,
    max_chars: int = 2500,
    head_chars: int = 700,
    window: int = 320,
) -> str:
    """Head of the page plus hint-centred windows, capped at `max_chars`."""
    text = _clean(text)
    if len(text) <= max_chars:
        return text

    head = text[:head_chars]
    rest = text[head_chars:]
    low = rest.lower()

    # Collect non-overlapping windows around each hint, in page order.
    spans: list[tuple[int, int]] = []
    for hint in hints:
        start = 0
        while True:
            i = low.find(hint, start)
            if i < 0:
                break
            spans.append((max(0, i - window // 2), min(len(rest), i + window)))
            start = i + len(hint)
            if len(spans) > 60:
                break

    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    out = [head]
    budget = max_chars - len(head)
    for s, e in merged:
        if budget <= 0:
            break
        chunk = rest[s:e].strip()
        if not chunk:
            continue
        chunk = chunk[:budget]
        out.append(chunk)
        budget -= len(chunk) + 5

    # Nothing matched: just take the head plus whatever follows it.
    if len(out) == 1:
        return text[:max_chars]
    return _clean("\n...\n".join(out))[:max_chars]
