"""MCP tool-name classification: ``mcp__<server>__<tool>`` (Hermes) and ``mcp_<server>_<tool>``.

Pins the prefix rule that feeds ``tool_schemas.mcp.<server>`` buckets. A rule that
only accepts single underscores silently charges every MCP server's schema to
``tool_schemas.builtin``, which is how the dashboard reported a 76% tool-schema
share against a 5.8% reality.
"""
import token_lens_core as core


RULES = core.DEFAULT_RULES


def test_hermes_double_underscore_names_resolve_to_server():
    assert core.mcp_server_for_tool("mcp__rea__xrefs", RULES) == "rea"
    assert core.mcp_server_for_tool("mcp__goodreads__goodreads_search_books", RULES) == "goodreads"
    assert core.mcp_server_for_tool("mcp__rea__binary_overview", RULES) == "rea"


def test_single_underscore_names_still_resolve():
    assert core.mcp_server_for_tool("mcp_github_create_issue", RULES) == "github"


def test_non_mcp_names_never_match():
    for name in ("terminal", "browser_exec", "rea_xrefs", "mcp__", "", "mcp__rea"):
        assert core.mcp_server_for_tool(name, RULES) is None


def test_hyphenated_servers_resolve():
    assert core.mcp_server_for_tool("mcp__my-server__tool", RULES) == "my-server"
