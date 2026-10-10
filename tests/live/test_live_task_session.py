"""LIVE task-session test against the real AgntID runtime in AGNTID_MCP_URL.

Opt-in only:  AGNTID_LIVE_TEST=1 pytest -m live -s

Requires PATCHPILOT_AGENT_ID and PATCHPILOT_USER_ID. Optional:

  PATCHPILOT_LIVE_PROMPT     task prompt (default below)
  PATCHPILOT_LIVE_TOOL       one task-listed tool to call exactly once
  PATCHPILOT_LIVE_TOOL_ARGS  JSON object of arguments for that tool

A live tool call reaches the real backend behind the runtime. Pick a read-only
tool. When opted in, an unreachable runtime, a rejected task, or a missing
intent snapshot FAILS this test; none of them is turned into a pass. Nothing is
asserted about which tools are shortlisted or which calls are allowed, because
that depends on the runtime's profile, intent mode, and policies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import agntid
import pytest

from patchpilot.agntid_connector import load_config
from patchpilot.discover_cli import CONFIG_KEYS, merged_env
from patchpilot.task_session import ENV_AGENT_ID, ENV_USER_ID, TaskOpenError, load_identity, open_task_session

ENV_PROMPT = "PATCHPILOT_LIVE_PROMPT"
ENV_TOOL = "PATCHPILOT_LIVE_TOOL"
ENV_TOOL_ARGS = "PATCHPILOT_LIVE_TOOL_ARGS"
DEFAULT_PROMPT = "Add 11 and 22"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AGNTID_LIVE_TEST") != "1",
        reason="live AgntID test is opt-in: set AGNTID_LIVE_TEST=1",
    ),
]


async def test_real_runtime_task_session():
    keys = (*CONFIG_KEYS, ENV_AGENT_ID, ENV_USER_ID, ENV_PROMPT, ENV_TOOL, ENV_TOOL_ARGS)
    env, _ = merged_env(os.environ, Path(".env"), keys)
    config = load_config(env)
    identity = load_identity(env)
    prompt = env.get(ENV_PROMPT) or DEFAULT_PROMPT
    tool = env.get(ENV_TOOL)
    arguments = json.loads(env.get(ENV_TOOL_ARGS) or "{}")

    async with agntid.AgntidMCPClient(config.mcp_url, access_token=config.access_token) as client:
        try:
            async with open_task_session(client, config, identity, prompt) as session:
                intent = session.task_open
                print(f"\ntask {session.task_id} opened")
                print(f"  intent engine:  {intent.snapshot.engine_id if intent.snapshot else '(none)'}")
                print(f"  eligible/relevant: {intent.eligible_tool_count}/{intent.relevant_tool_count}")
                print(f"  task tools:     {sorted(session.tool_names) or '(empty)'}")
                print(f"  control tools excluded: {list(session.excluded_control_tools)}")
                if tool:
                    result = await session.call_tool(tool, arguments)
                    print(f"  call {tool}: {result.status.value} {result.message}")
                    if result.denial_explanation:
                        print(result.denial_explanation)
                    assert result.tool_name == tool
        except TaskOpenError as error:
            pytest.fail(f"{error.failure.stage}: {error.failure.reason}")
    assert not session.active
