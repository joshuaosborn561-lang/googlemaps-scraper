"""ICP classification is no longer an LLM stage.

Website text is stored in public.site_pages. Qualify businesses in SQL
against that table instead of calling an LLM here.
"""

from __future__ import annotations

from typing import Any

REMOVED_MESSAGE = (
    "LLM classification removed, classify in SQL against site_pages"
)

# Kept so `gmscraper bench` can still time a dummy prompt. Not used in production.
SYSTEM = (
    "You qualify local businesses for a B2B prospect list. You are given an "
    "ICP definition and evidence about one business. Decide whether the "
    "business matches the ICP."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "in_icp": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["in_icp", "confidence", "reason"],
}

PROMPT = """ICP:
{icp}

BUSINESS
Name: {name}
Google Maps category: {category}
All Maps categories: {types}
Address: {address}
Website: {website}

WEBSITE TEXT:
---
{text}
---
"""


def run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """No-op: LLM classification is retired. Existing callers must not error."""
    return {
        "done": 0,
        "in_icp": 0,
        "errors": 0,
        "removed": True,
        "message": REMOVED_MESSAGE,
        "reason": REMOVED_MESSAGE,
    }
