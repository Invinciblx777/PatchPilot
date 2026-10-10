# PatchPilot live demo (about 3 minutes)

Everything below runs against the real local AgntID runtime
(`achr-runtime` on `localhost:8082`). Nothing in this script uses fixture data.
The `demo_get_ticket` result is the runtime's own built-in demo server, which
labels its data `"simulated": true`; PatchPilot does not produce it.

## Who does what

| Step | Done by |
| --- | --- |
| Tool list, task open, task shortlist (intent engine) | AgntID runtime |
| Intent validation and policy decision on each business call | AgntID runtime |
| Attaching the task ID to each call (`agntid_task_id`) | AgntID SDK `wrap_client`, configured by PatchPilot |
| Refusing reserved arguments, unlisted tools, control tools, schema-invalid input | PatchPilot (application checks, never shown as AgntID decisions) |
| Patch validation, disposable worktree, test execution | PatchPilot (local; no AgntID involvement) |
| GitHub issue read, pull request | **Unavailable**: no GitHub MCP server is attached to the runtime |

## Setup (before recording)

```bash
cd "~/Projects/AgntID Hackathon"            # project root
# .env (no secrets): AGNTID_MCP_URL=http://localhost:8082/mcp,
#   PATCHPILOT_AGENT_ID=patchpilot:v1, PATCHPILOT_USER_ID=operator-local
podman ps                                   # achr-runtime and intent-parser healthy
demo/setup-demo-repo.sh                     # creates ~/patchpilot-demo-repo (one failing test)
.venv/bin/patchpilot-ui --repo ~/patchpilot-demo-repo --test-command "python3 -m unittest -v"
# open http://127.0.0.1:8765
```

Keep a second terminal open for runtime evidence:

```bash
podman logs -f agntid-runtime-achr-runtime-1 2>&1 \
  | grep -E "policy_check_with_task_context|Intent validation flow|VERDICT|Policy denied|first_number|intent_parsing"
```

## Script

**0:00 Connection (AgntID).** Show the runtime strip and Security screen: live
`tools/list` from `localhost:8082`. Point out the business tool and the five
control tools PatchPilot will never call as business tools. Optional terminal:
`.venv/bin/patchpilot-discover`.

**0:30 Task linked to the runtime (AgntID + SDK).** Security, Task probe:
prompt `Get the current status of ticket LAB-TKT-100`, tool `demo_get_ticket`,
arguments `{"ticket_id": "LAB-TKT-100"}`, confirm. Show the task ID and the
runtime's shortlist. In the log terminal show the same task ID:

```
policy_check_with_task_context task_id=<id> tool=demo.get_ticket prompt_len=44
Intent validation flow started for tool demo.get_ticket (prompt_length=44, ...)
VERDICT: allow | reason=ALLOWED
Step 2: intent_parsing (status=passed ...)
```

Say: before today's fix this line read `prompt_length=0` and the runtime logged
`No prompt available - skipping intent validation`. The SDK was sending
`_task_id`; this runtime build reads `agntid_task_id`. PatchPilot now passes
`task_argument="agntid_task_id"` to `wrap_client`.

**1:00 Permitted call (AgntID).** Same probe: the call card shows
`decision: allowed`, the real tool result, tagged `AgntID data`.

**1:20 Policy denial (AgntID).** See "Allow/deny with demo.add_numbers" below
for the current status. Only show this step if the tool is visible.

**1:50 Application safeguard (PatchPilot).** Probe the same tool with an extra
argument `agntid_task_id`. PatchPilot refuses before any task opens or call is
sent; the UI labels it a PatchPilot check, not an AgntID decision.

**2:10 Patch workspace (PatchPilot, local).** Overview, proposed change: paste
`demo/fix-discount.patch`, check, approve apply, approve tests. Show
`command-1: exit code 0`, cleanup `removed`, original tree `unchanged`. In a
terminal: `cd ~/patchpilot-demo-repo && python3 -m unittest -q` still fails,
proving the original repository was not touched.

**2:40 What is not live.** No GitHub MCP server is attached to this runtime, so
issue retrieval and pull requests are disabled and labelled unavailable. The
patch workflow is a local demonstration; it does not fix a GitHub issue.

## Allow/deny with demo.add_numbers

Status at 2026-10-10 15:29 IST: **blocked** (watched 15:17-15:29; runtime still on tool-scope snapshot `version=3`, `portal policies: 1`). The runtime's tool scope hides
`demo_add_numbers` (runtime log: `tool_scope_manager_applied profile=READ_WRITE
... hidden_tools=demo_add_numbers,demo_create_ticket,...`) and the portal holds
one policy (`demo.get_ticket`). An operator must enable `demo.add_numbers` and
add the Quick Start input limits in the portal (Quick Start sections 5 and 6).
PatchPilot does not change runtime configuration.

Once it is visible, run each as a separate task (UI Task probe, or CLI):

| Prompt | Tool and arguments | Expected if the policy is applied |
| --- | --- | --- |
| `Use demo.add_numbers to add 11 and 21.` | `demo_add_numbers` `{"first_number": 11, "second_number": 21}` | allowed, result 32 |
| `Use demo.add_numbers to add 50 and 21.` | `demo_add_numbers` `{"first_number": 50, "second_number": 21}` | AgntID policy denial on `first_number` |

Then open Reporting in the portal and show both Tool Execution events.

## Live evidence recorded so far

| Time (UTC) | Path | Task | Runtime result |
| --- | --- | --- | --- |
| 09:36 (before fix) | Module 2 | - | `prompt_length=0`, intent validation SKIPPED |
| 09:44 | Module 2 live test | `48788c80-...` | `prompt_len=44`, NLI entailment 0.996, VERDICT allow, policy allowed, tool ran |
| 09:48 | UI task probe | `bd36524e-...` | `prompt_len=44`, VERDICT allow, policy allowed |
| 09:48 | UI task probe, prompt names LAB-TKT-100, call asks LAB-TKT-200 | `f69f88a2-...` | intent ran (`prompt_len=44`) and **allowed** it; no denial observed |
