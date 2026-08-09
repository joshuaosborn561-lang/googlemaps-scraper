"""Structured MCP tool errors — never mask internals as approval failures."""

from __future__ import annotations

import traceback
import uuid
from typing import Any


class ToolError(Exception):
    """Typed failure returned to MCP clients as JSON, not a bare string."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "internal_error",
        details: dict[str, Any] | None = None,
        request_id: str = "",
    ):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details or {}
        self.request_id = request_id or uuid.uuid4().hex[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "kind": self.kind,
                "message": self.message,
                "request_id": self.request_id,
                "details": self.details,
            },
        }


KIND_CREDENTIAL = "missing_or_invalid_credential"
KIND_COST = "cost_ceiling_exceeded"
KIND_UPSTREAM = "upstream_vendor_error"
KIND_BAD_ARGS = "bad_arguments"
KIND_INTERNAL = "internal_error"

# Client connectors sometimes surface this bare string. Never emit it from us.
_FORBIDDEN_BARE = "no approval received"


def _scrub_message(text: str) -> str:
    """Rewrite legacy approval phrasing so it cannot leak as a bare response."""
    raw = text or ""
    if _FORBIDDEN_BARE in raw.lower():
        return (
            "Client/connector confirmation gate (not a server spend-approval). "
            "Server has no approval mechanism. Check tool annotations "
            "(destructiveHint must be explicitly false), bump server version, "
            "and reconnect the MCP connector. See error.request_id + details."
        )
    return raw


def classify_exception(exc: BaseException) -> str:
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    if "approval" in msg:
        # Never treat remnants as a live approval gate.
        return KIND_INTERNAL
    if "token" in msg or "api key" in msg or "credential" in msg or "unauthorized" in msg:
        return KIND_CREDENTIAL
    if "exceeds" in msg and ("cost" in msg or "max_cost" in msg or "ceiling" in msg):
        return KIND_COST
    if "apify" in msg or "rapidapi" in msg or "upstream" in msg or "http" in name:
        return KIND_UPSTREAM
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return KIND_BAD_ARGS
    return KIND_INTERNAL


def tool_error_from_exception(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ToolError):
        payload = exc.to_dict()
        payload["error"]["message"] = _scrub_message(payload["error"]["message"])
        return payload
    kind = classify_exception(exc)
    msg = _scrub_message(f"{type(exc).__name__}: {exc}")
    err = ToolError(
        msg,
        kind=kind,
        details={
            "exception_type": type(exc).__name__,
            "traceback": traceback.format_exc()[-2500:],
        },
    )
    return err.to_dict()
