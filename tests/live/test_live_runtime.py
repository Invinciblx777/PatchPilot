"""LIVE integration test against the real AgntID runtime in AGNTID_MCP_URL.

Opt-in only:  AGNTID_LIVE_TEST=1 pytest -m live -s

When opted in, an unreachable runtime FAILS this test; it is never turned into a
pass. No assertions are made about which tools exist, because the visible set
depends on the runtime's protection profile, review state, and role grants.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from patchpilot.agntid_connector import discover_tools, load_config
from patchpilot.discover_cli import format_report, merged_env

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AGNTID_LIVE_TEST") != "1",
        reason="live AgntID test is opt-in: set AGNTID_LIVE_TEST=1",
    ),
]


async def test_real_runtime_tool_discovery():
    env, sources = merged_env(os.environ, Path(".env"))
    config = load_config(env)
    result = await discover_tools(config)

    names = [t.name for t in result.tools]
    assert all(names), "every tool returned by the runtime must have a name"
    assert len(names) == len(set(names)), "runtime returned duplicate tool names"
    for tool in result.tools:
        assert isinstance(tool.input_schema, dict)

    print("\n" + format_report(result, sources))
