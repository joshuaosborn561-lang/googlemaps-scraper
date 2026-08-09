"""All MCP tools must expose fully-specified annotations (no None hints)."""

from __future__ import annotations

from mcp_server import server
from mcp_server.errors import tool_error_from_exception


def test_every_tool_has_all_four_hints() -> None:
    tools = {t.name: t for t in server._iter_registered_tools()}
    assert "classify_leads" in tools
    assert "enrich_waterfall" in tools
    assert "debug_echo" in tools
    for name, tool in sorted(tools.items()):
        ann = tool.annotations
        assert ann is not None, f"{name} missing annotations"
        dump = ann.model_dump()
        ro = dump.get("read_only_hint", dump.get("readOnlyHint"))
        dest = dump.get("destructive_hint", dump.get("destructiveHint"))
        idem = dump.get("idempotent_hint", dump.get("idempotentHint"))
        ow = dump.get("open_world_hint", dump.get("openWorldHint"))
        assert ro is not None, f"{name} readOnlyHint unset"
        assert dest is not None, f"{name} destructiveHint unset"
        assert idem is not None, f"{name} idempotentHint unset"
        assert ow is not None, f"{name} openWorldHint unset"
        assert dest is False, f"{name} destructiveHint must be False (got {dest})"


def test_classify_and_waterfall_match_spec() -> None:
    tools = {t.name: t for t in server._iter_registered_tools()}
    for name, expect in (
        ("classify_leads", (False, False, True, False)),
        ("enrich_waterfall", (False, False, True, True)),
        ("debug_echo", (False, False, True, True)),
    ):
        dump = tools[name].annotations.model_dump()
        got = (
            dump.get("read_only_hint"),
            dump.get("destructive_hint"),
            dump.get("idempotent_hint"),
            dump.get("open_world_hint"),
        )
        assert got == expect, f"{name}: {got} != {expect}"


def test_never_emit_bare_no_approval_received() -> None:
    err = tool_error_from_exception(RuntimeError("No approval received"))
    msg = err["error"]["message"]
    assert "No approval received" not in msg
    assert err["error"]["kind"] == "internal_error"
    assert "request_id" in err["error"]


def test_debug_echo_body() -> None:
    out = server.debug_echo("lane-probe")
    assert '"echo": "lane-probe"' in out
    assert "1.7.0" in out
