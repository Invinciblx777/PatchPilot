# PatchPilot

PatchPilot is an AI coding agent whose tool access goes through the
[AgntID](https://agntid.ai) runtime. It is built module by module; this
repository currently contains **Module 1: the AgntID runtime connector** and
**Module 2: the task lifecycle and fail-closed tool dispatch**, and
**Module 3: GitHub read capabilities (not yet verified live)**, and
**Module 4: the local patch workspace and test engine**, plus a local
**web UI** (`patchpilot-ui`) built on those four modules.

## Module 1: AgntID runtime connector

The connector loads configuration, opens the documented SDK client, lists the
tools the runtime exposes to this client, and closes the session:

```python
async with agntid.AgntidMCPClient(url, access_token=token) as client:
    tools = await agntid.get_tools_list(client)
```

It uses AgntID Python SDK **source version 0.2.0**, pinned to commit
`f4fcd2fe476f0c4dcf800e498530b7042b830f6b`. The published wheel is still
0.1.1, so the AgntID docs direct users to the source checkout.

| File | Purpose |
| --- | --- |
| `src/patchpilot/agntid_connector.py` | Config loading, discovery, tool normalization, error classification |
| `src/patchpilot/discover_cli.py` | `patchpilot-discover` live report (text or `--json`) |
| `tests/` | Automated tests (see [Tests](#tests)) |
| `tests/live/test_live_runtime.py` | Opt-in live test against the real runtime |
| `src/patchpilot/task_session.py` | Module 2: trusted identity, task lifecycle, fail-closed dispatch |
| `tests/test_task_session.py` | Module 2 tests |
| `tests/live/test_live_task_session.py` | Module 2 opt-in live task test |
| `.env.example` | Configuration template, placeholders only |

### What it reports

- The runtime endpoint, with any user-info, query string, and fragment removed.
- The installed SDK version.
- Whether discovery succeeded, and the error class if it failed.
- The number of tools returned. An empty list is reported as empty.
- For each tool: the exact name, title, description, annotations, and the full
  `inputSchema` (plus `outputSchema` if present), exactly as returned.
- The AgntID runtime control tools (`agntid_task_open` and similar) are flagged,
  never hidden.

A visible tool is not necessarily authorized. The runtime filters the list by
protection profile, review state, tool settings, and role grants, and it still
checks every call. Task-scoped shortlisting is Module 2.

## Setup

You need Python 3.11 or later and git.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env    # then edit .env; it is git-ignored
```

### Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `AGNTID_MCP_URL` | Yes | Runtime MCP address. For a local runtime this is usually `http://localhost:8082/mcp`. There is no silent default. |
| `AGNTID_ACCESS_TOKEN` | Only if Runtime Security requires OAuth | Raw OAuth access token, without the `Bearer` prefix. |
| `AGNTID_TIMEOUT_SECONDS` | No | Upper bound for connect, list, and close together. The default is 20. |

Values in the process environment take precedence. The `.env` file only fills
values that are unset. Use `--env-file PATH` to choose a different file.

The bootstrap token from the AgntID portal is **not** an MCP access token. Enter
it only in the local runtime UI.

## Run against the real runtime

First start and register a runtime with the AgntID
[Quick Start](agntid-docs/docs/quick-start.md). In short:

1. Run `docker compose up -d` with the AgntID compose file.
2. Open <http://localhost:8080> and enter the bootstrap token.
3. In the portal, review the protection profile.
4. Wait for **5 of 5** and **Runtime sync: Applied**.

Then run:

```bash
patchpilot-discover            # readable report
patchpilot-discover --json     # machine-readable report
AGNTID_LIVE_TEST=1 pytest -m live -s   # live test; fails if the runtime is unreachable
```

| Exit code | Meaning |
| --- | --- |
| 0 | Connected, and `tools/list` succeeded (the list may be empty) |
| 2 | Configuration error |
| 3 | Runtime unreachable (connection refused, DNS, network) |
| 4 | Authentication failed (HTTP 401/403) |
| 5 | Protocol error (wrong path, not an MCP endpoint, other HTTP status) |
| 6 | Connected, but `tools/list` failed or returned an unusable tool |
| 7 | Timed out |
| 8 | Failed while closing the session |

## Tests

```bash
pytest -q
```

The automated tests use no mocks. They run the real `agntid.AgntidMCPClient`
over real HTTP against these local fixtures:

- fixture MCP servers built with FastMCP, the library the SDK client is built on
- a server that uses FastMCP's real bearer-token verifier
- a closed port
- an unresolvable host
- a socket that accepts connections and never responds

**These fixtures are not AgntID.** Passing tests show that the connector
handles real MCP and HTTP behavior correctly. They do not show that an AgntID
runtime works. Only `patchpilot-discover` or `pytest -m live` against a running
runtime shows that. Fixture tool names all start with `fixture_`.

## Module 2: Task lifecycle and fail-closed tool dispatch

`src/patchpilot/task_session.py` opens one AgntID task per request and is the
only way PatchPilot calls a business tool. The caller still owns the MCP
client, as in Module 1:

```python
from patchpilot.agntid_connector import load_config
from patchpilot.task_session import CallStatus, TaskOpenError, load_identity, open_task_session

config = load_config(env)        # Module 1: URL, token, timeout
identity = load_identity(env)    # PATCHPILOT_AGENT_ID, PATCHPILOT_USER_ID

async with agntid.AgntidMCPClient(config.mcp_url, access_token=config.access_token) as client:
    try:
        async with open_task_session(client, config, identity, prompt) as session:
            session.tools                       # only this task's tools (ToolInfo)
            result = await session.call_tool(name, arguments)
            if result.status is CallStatus.DENIED:
                print(result.denial_explanation)
    except TaskOpenError as error:
        print(error.failure.stage, error.failure.reason)
```

### How a task is created and opened

`open_task_session` runs the documented SDK flow, in this order:

1. `agntid.get_tools_list(client)` gets the profile-visible tools.
2. `agntid.create_task(agent_id, user_id, prompt)` makes a task ID locally.
3. `agntid.wrap_client(client, task_id)` binds a wrapper to that task.
4. `agntid.task_context(client, task_id)` is entered. It sends
   `agntid_task_close` when the block exits, on success or on error.
5. `agntid.send_task_open_details(...)` sends the task to the runtime.
6. `agntid.filter_tools_for_task(visible, task_open)` builds the task's tool list.

The session is usable only inside the `async with` block. It is marked
inactive before task close is sent. Any later `call_tool` raises
`TaskSessionClosedError` and sends nothing. Await every call inside the block.

### Trusted identity

| Variable | Meaning |
| --- | --- |
| `PATCHPILOT_AGENT_ID` | Agent identity, from deployment configuration |
| `PATCHPILOT_USER_ID` | Stable opaque user ID (not an email address) |

These are PatchPilot settings, not AgntID-standard names. Both must be 1 to 128
characters: letters, digits, `.`, `_`, `:`, or `-`. Values that are missing, or
contain `@` or whitespace, are rejected before the runtime is contacted. The
prompt is never parsed for identity. This module is not an authentication
system. The application that sets these values must be trusted.

### Why the wrapped client is mandatory

`agntid.wrap_client` adds the task correlation argument that ties a call to the
opened task. PatchPilot passes `task_argument="agntid_task_id"`
(`task_session.TASK_ARGUMENT`), the name the running `achr-runtime` reads
(`tool_execution.py`, `_TASK_CORRELATION_ARG`). The SDK default `_task_id` is
read only by the runtime's `TaskMiddleware`, which this runtime build does not
register, so calls sent with it ran without their task's prompt. It also turns runtime denials into a `DenialResult` and
removes internal `__agntid_*` result fields. A call made on the raw client has
no task correlation. `TaskSession` therefore does not expose a client. Its
`call_tool` sends each request through `wrapped.call_tool_async` exactly once.

### Reserved arguments

A caller can never supply an argument that AgntID uses for task, delegation, or
approval context. One rule, `task_session.is_reserved_argument`, decides it.
A name is reserved when it is:

- `_task_id` or `_delegation_id`: the SDK wrapper's correlation arguments
  (`agntid/mcp_wrapper.py` at f4fcd2fe). The wrapper overwrites `_task_id`,
  but it forwards a caller's `_delegation_id` unchanged.
- in the AgntID namespace: it starts with `agntid_` after any leading
  underscores, in any case. This covers `agntid_task_id`,
  `agntid_delegation_id`, `agntid_approval`, and `__agntid_*`.

The local runtime (`achr-runtime`, `tool_prefix_handler.py`) adds
`agntid_task_id`, `agntid_delegation_id`, and `agntid_approval` to every
mounted tool's schema. `tool_execution.py` removes them before the policy
check. The first two choose which task or delegation a call belongs to.
`agntid_approval` is merged into the policy context as approval evidence.
None of them is a business input, so the rule has no exceptions.

The dispatcher checks the caller's arguments before the wrapper adds its own.
A call with a reserved name gets `CallStatus.REJECTED`, nothing is sent, and the
arguments are not changed. The message names the argument but never shows its
value. Module 3 mappings, the preflight CLI, and the web UI use the same rule.
The UI also checks first and hides reserved values in run records. These are
PatchPilot checks, not AgntID enforcement.

### Task-specific tool filtering

`session.tools` is the visible list intersected with the runtime's task
shortlist. It never contains a tool that the runtime did not list. The AgntID
control tools (`agntid_task_open`, `agntid_task_close`, `agntid_delegation_open`,
`agntid_delegation_close`, `agntid_action_update`) are removed by exact name,
using the SDK constants. The shortlist narrows what an agent can choose. It is
not authorization: the runtime still checks every call.

### Failure handling

| Situation | Result |
| --- | --- |
| Identity or prompt missing or invalid | `ConfigError`. Nothing is sent. |
| Runtime answers task open with `ok` not `true` | `TaskOpenError`, stage `task_open_rejected`. The runtime's reason is kept. |
| The task-open call fails (transport, tool error, timeout) | `TaskOpenError`, stage `task_open_error` |
| No intent snapshot (`filter_tools_for_task` raises `IntentPreparationError`) | `TaskOpenError`, stage `intent_unavailable`. There is no fallback to the visible list. |
| Shortlist is empty | The session opens with no tools. Every call is rejected. |
| Tool not in the task list, a control tool, or a reserved argument (`_task_id`, `_delegation_id`, any `agntid_*` / `__agntid_*`) | `CallStatus.REJECTED`, with `call_sent=False`. This is a PatchPilot check, not an AgntID decision. |
| Wrapped client returns a `DenialResult` | `CallStatus.DENIED`, with the message, `get_last_denial(clear=True)` data, and `format_denial` text. No retry, no other tool, no raw client. |
| Call sent but fails for another reason | `CallStatus.ERROR`, with a token-free message |
| Task close fails | `TaskCloseError`. If the block already raised, that exception is re-raised with a note instead. |

When `TaskOpenError` is raised, the block does not run. The task context still
exits and sends task close.

### Tests

```bash
pytest -q                               # everything except live tests
pytest -q tests/test_task_session.py    # Module 2 only
```

There are three kinds of test:

| Kind | What it shows |
| --- | --- |
| **Unit** (identity validation, `tests/test_task_session.py` top section) | Pure Python. No network. |
| **Local SDK/MCP transport** (rest of `tests/test_task_session.py`) | The real SDK calls run over real HTTP against a local FastMCP fixture server. The server answers `agntid_task_open` and `agntid_task_close` with payloads that each test sets, and records every call. Its denials use the error format that the SDK's `denial.py` parses. |
| **Live AgntID runtime** (`tests/live/`) | Only these talk to AgntID |

**The fixture server is not AgntID.** The transport tests show that
PatchPilot handles the SDK contract correctly. They do not show that AgntID
opened a task, built a shortlist, or denied a call.

### Live integration test

When a runtime is running and registered (see
[Run against the real runtime](#run-against-the-real-runtime)), set
`PATCHPILOT_AGENT_ID` and `PATCHPILOT_USER_ID`, then run:

```bash
AGNTID_LIVE_TEST=1 pytest -m live -s
# Optional: call one task-listed tool exactly once. Use a read-only tool.
AGNTID_LIVE_TEST=1 PATCHPILOT_LIVE_PROMPT="Add 11 and 22" \
  PATCHPILOT_LIVE_TOOL=demo_add_numbers \
  PATCHPILOT_LIVE_TOOL_ARGS='{"first_number": 11, "second_number": 22}' \
  pytest -m live -s
```

The task test fails if the task is rejected or no intent snapshot is returned.
The intent engine docs say only **LLM-based** intent shortlists tools. On a
runtime that uses the default **Standard** mode, the test may fail with
`intent_unavailable`. This has not been checked against a real runtime.
Do not treat any policy as enforced until a real allowed call and a real denial
have both been seen against the runtime and in Reporting.

### Known limitations

- `task_context` ignores the task-close result. A close that the runtime
  answers with `ok: false` is not detected. Only a close that raises is reported.
- The SDK classifies a tool error as a denial by text matching. Any error whose
  text contains words such as `denied`, `policy`, or `expired` becomes a
  `DenialResult`. This errs toward denial, never toward success.
- `get_last_denial` is thread-local, not per task. The dispatcher clears it
  before each call and reads it right after the call, with no await in between.
- The SDK has no per-call timeout. `AGNTID_TIMEOUT_SECONDS` bounds tools/list
  and task open only. Tool calls and task close are not bounded.

## Module 3: GitHub read capabilities

Module 3 lets PatchPilot read GitHub data (an issue, repository metadata, a
file) through tools that AgntID exposes. Every call goes through the Module 2
task session. PatchPilot has no GitHub credential and no other path to GitHub:
no REST, GraphQL, `gh`, PyGithub, or unwrapped MCP client.

**Status: not verified live.** No AgntID runtime has been reachable, so no real
GitHub tool list has been seen. No GitHub tool name or schema is built into the
code, and no mapping is shipped. The integration stays disabled until you
register a mapping from a real inventory.

| File | Purpose |
| --- | --- |
| `src/patchpilot/github_capabilities.py` | Candidate discovery, mapping validation, and dispatch through `TaskSession.call_tool` |
| `src/patchpilot/github_preflight_cli.py` | `patchpilot-github-preflight` report |
| `tests/test_github_capabilities.py`, `tests/test_github_preflight_cli.py` | Module 3 tests |

### Connect a GitHub MCP server to AgntID

PatchPilot can only use GitHub tools that the runtime provides. Follow
[Add an MCP Server](agntid-docs/docs/mcp/attach-server.md):

1. In the portal, open **Runtimes**, select the runtime, then open
   **Operations → MCP → Servers → Add MCP**. **MCP Support** must be enabled in
   the runtime's **Setup** tab.
2. Choose the connection style for your GitHub MCP server. Set a **Server ID**
   and a **Prefix**. The prefix becomes part of the tool names.
3. Store the GitHub credential as an environment variable **on the runtime
   host**, and refer to it by name in the form (the docs' example is
   `${GITHUB_TOKEN}`). Do not paste the secret into the form. Never give it to
   PatchPilot.
4. Select **Test Connection**, then **Save MCP**. Wait for **Apply: APPLIED**.
5. Review the actions under **Access Control → Tools**. Wait for runtime
   synchronization.

The docs do not name a specific GitHub MCP server, so this repository doesn't
either. The tool names and schemas depend on the server you attach and on its
prefix. The docs also use different name styles (`demo_add_numbers`,
`demo.add_numbers`, `github.create_issue`), so PatchPilot assumes none of them.

### Run the preflight

```bash
patchpilot-github-preflight                     # inventory, candidates, mapping checks
patchpilot-github-preflight --json
patchpilot-github-preflight --mapping capabilities.json --task-prompt "Read issue 42 in OWNER/REPO"
patchpilot-github-preflight --mapping capabilities.json --task-prompt "Read issue 42 in OWNER/REPO" \
  --run get_issue --arg owner=OWNER --arg repo=REPO --arg issue_number=42
```

Without `--run`, no business tool is called. `--task-prompt` opens one real
AgntID task (Module 2) and closes it. It needs `PATCHPILOT_AGENT_ID` and
`PATCHPILOT_USER_ID`. `--run` makes exactly one read inside that task and
prints the result exactly as returned, with its provenance: endpoint, task ID,
tool, arguments sent, and schema hash.

The report keeps four states apart. It never collapses them into one
"authorized" flag:

| State | Meaning | Decided by |
| --- | --- | --- |
| Listed by runtime | The tool is in the profile-visible list | AgntID visibility rules |
| Schema check: verified | A reviewed mapping matches the tool's real inputSchema | PatchPilot (application check) |
| In task tool list | The tool is in this task's shortlist | AgntID intent shortlist |
| Runtime authorization | One real `--run` call was allowed or denied | AgntID policy, for that call only |

A "candidate" is a guess from the tool's name, description, or `owner`/`repo`
inputs. It is only a suggestion and is never used without a mapping.

| Exit code | Meaning |
| --- | --- |
| 0 | At least one capability is schema-verified, and the task opened (if checked). With `--run`: the read succeeded. |
| 2-8 | As for `patchpilot-discover` (configuration, unreachable, authentication, protocol, discovery, timeout, close) |
| 9 | Connected, but no capability is schema-verified (no GitHub tools, no mapping, or an invalid mapping) |
| 10 | The task was not opened (rejected, task-open error, or no intent snapshot) |
| 11 | `--run` did not succeed (denied, rejected, invalid, failed, or outcome unknown) |

### Register a capability mapping

PatchPilot capabilities are application names, not AgntID tool names:

| Capability | Parameters |
| --- | --- |
| `get_issue` | `owner` (string), `repo` (string), `issue_number` (integer) |
| `get_repository` | `owner`, `repo` |
| `read_file` | `owner`, `repo`, `path`, optional `ref` |

After the preflight shows a real GitHub tool, write a JSON map that uses the
**exact** tool name and the `schema_sha256` printed for it:

```json
{
  "capabilities": {
    "get_issue": {
      "tool": "<exact name from the preflight>",
      "schema_sha256": "<schema_sha256 from the preflight>",
      "arguments": {"owner": "<tool arg>", "repo": "<tool arg>", "issue_number": "<tool arg>"},
      "fixed_arguments": {}
    }
  }
}
```

- `arguments` maps each PatchPilot parameter to the tool argument it fills.
  Renaming happens only here, by a person.
- `fixed_arguments` holds constant values that the tool requires, such as a
  `method` selector. They are checked against the schema.
- Pass the file with `--mapping`, or set `PATCHPILOT_CAPABILITY_MAP`.

A mapping is rejected when any of these is true:

- The tool name is not an exact match.
- The schema hash has changed.
- A required tool argument is neither mapped nor fixed.
- A type does not match, or a property declares no type.
- A target or fixed argument is reserved (`_task_id`, `_delegation_id`, any
  `agntid_*` or `__agntid_*`; see [Reserved arguments](#reserved-arguments)).
  The map is refused when it is loaded, and again before every call.
- The tool is a control tool.
- The tool declares `readOnlyHint: false` or `destructiveHint: true`.

A missing `readOnlyHint: true` is a warning. The mapping is checked again
against the task's tool metadata before every call. Arguments are checked
against the full inputSchema before anything is sent.

### Read results

`execute_capability(session, mappings, capability, params, timeout_seconds=...)`
returns one of these:

| Status | Call sent | Meaning |
| --- | --- | --- |
| `success` | yes | The tool result, unchanged |
| `denied` | yes | The SDK `DenialResult`, with its message and `format_denial` text |
| `missing_capability` | no | No mapping is registered |
| `not_in_task` | no | The mapped tool is not in this task's list |
| `invalid_mapping` | no | The mapping does not match the tool's schema |
| `invalid_arguments` | no | The request does not fit the mapping or the schema |
| `rejected` | no | The Module 2 dispatcher refused the call |
| `execution_failed` | yes | The server answered with a tool error |
| `outcome_unknown` | yes | Timeout, transport error, or an unrecognized result. The read may have run. |

No status triggers a retry, a change of arguments, or another tool.

### Tests

```bash
pytest -q tests/test_github_capabilities.py tests/test_github_preflight_cli.py
```

These tests are local:

- **Metadata unit tests.** Tool metadata dicts written for the tests. No network.
- **SDK transport tests.** The real SDK over real HTTP, against the task
  fixture server plus GitHub-shaped fixture tools. **These are not GitHub and
  not AgntID.** Their names start with `fixture_github_`, and their results
  say `fixture data, not from GitHub`.

Passing tests show that the discovery, validation, dispatch, and reporting
logic is correct. They show nothing about a real GitHub tool or a real AgntID
decision.

### Known limitations

- Repository, branch, and path scope are **not enforced** by PatchPilot or
  by any verified AgntID policy yet. Do not claim they are until a runtime
  policy is configured and a real denial is seen.
- Candidate detection is a heuristic on names and descriptions. It can miss
  tools or suggest wrong ones. Only a reviewed mapping is ever used.
- After `outcome_unknown`, the remote side may have run the read. PatchPilot
  does not retry it.

## Module 4: Local patch workspace and test engine

`patchpilot-patch` takes a unified diff that you supply. It checks the diff
against a local Git repository, applies it in a disposable worktree, runs the
test commands you configured, and reports what actually happened. It is local
only: it does not contact GitHub or AgntID, and it never commits, pushes, or
opens a pull request.

**This module does not generate fixes.** The patch is an input. A passing
test run shows only that your configured commands exited 0 against the
patched workspace. It does not show that a GitHub issue is resolved.

| File | Purpose |
| --- | --- |
| `src/patchpilot/patch_workspace.py` | Repository checks, strict diff parsing and path rules, the disposable worktree, atomic apply |
| `src/patchpilot/patch_verification.py` | Operator-configured test commands, minimal environment, timeouts |
| `src/patchpilot/patch_engine.py` | `run_patch_workflow(...) -> WorkflowResult` |
| `src/patchpilot/patch_cli.py` | `patchpilot-patch` |
| `tests/test_patch_*.py`, `tests/patch_helpers.py` | Module 4 tests |

### Usage

```bash
# Validate only: is the patch safe, and does it apply to the base?
patchpilot-patch --repo ~/src/project --base main --patch fix.patch --check-only

# Interactive: shows the change summary, then asks before applying and before testing.
patchpilot-patch --repo ~/src/project --base main --patch fix.patch \
  --test-command "python -m pytest -q"

# Non-interactive (automation), JSON output, commands from a trusted file.
patchpilot-patch --repo ~/src/project --base v1.2.0 --patch fix.patch \
  --test-config tests.json --yes --json
```

`tests.json` is operator configuration:

```json
{"commands": [{"name": "unit", "argv": ["python", "-m", "pytest", "-q"], "timeout_seconds": 600, "pass_env": []}]}
```

| Option | Meaning |
| --- | --- |
| `--repo` | Root of a non-bare Git working tree. A subdirectory or bare repository is rejected. |
| `--base` | A commit, branch, or tag. It is resolved to a full commit ID. A value starting with `-` is rejected. |
| `--patch` | A unified diff file in `git diff` format (`a/` and `b/` prefixes, `-p1`), at most 2 MiB |
| `--test-command` | A command line split with shell quoting rules (`shlex`) but never run by a shell. Repeatable. |
| `--test-config` | JSON file of commands. `argv` must be a list. |
| `--yes` | Approve applying and testing without prompts. Without it, an unanswered prompt means no. |
| `--keep-workspace` | Keep the worktree and its local branch. The report prints the commands that remove them. |
| `--allow-uncommitted` | Proceed although the original tree has local changes. They are still never touched. |
| `--allow-sensitive-path PATTERN` | Allow paths that match PATTERN despite the sensitive-path default deny |
| `--workspace-dir` | Parent directory for the workspace. It must be outside the repository. |

### What happens

1. **Repository.** The path must be the root of a non-bare working tree. Its
   HEAD, branch, `git status`, and the hashes of any locally changed files are
   recorded. If there are uncommitted or untracked changes, the run stops
   unless `--allow-uncommitted` is given.
2. **Patch.** The diff is parsed strictly. It is never repaired, and parts of
   it are never applied. These are rejected:
   - empty, truncated, or malformed diffs, and wrong hunk line counts
   - quoted or non-UTF-8 paths
   - absolute paths, `..` and empty path parts, and backslashes
   - anything under `.git`, plus `.gitmodules` and `.gitattributes`
   - symbolic links, submodules, and binary changes
   - a path changed twice
   - sensitive paths, by default: `.env`, `.env.*`, `*.pem`, `*.key`, SSH
     keys, `.netrc`, `.npmrc`, `.pypirc`, `credentials*`, `*.tfvars`,
     `.github/workflows/*`, `.github/actions/*`

   Only `--allow-sensitive-path` relaxes the sensitive list. Nothing in the
   patch or in an issue can change the policy. The other rules cannot be
   relaxed.
3. **Workspace.** `git worktree add -b patchpilot/workspace-<sha>-<random>`
   creates a worktree in a new temporary directory outside the repository.
4. **Check.** `git apply --numstat` must list the same files as PatchPilot's
   parser. Then `git apply --check --index` must pass against the base.
5. **Apply.** After approval, `git apply --index` applies the whole patch or
   nothing. It never uses `--reject`, `--3way`, or `--recount`. The changed
   paths must equal the patch's paths and stay inside the worktree. The
   report shows the real `git diff --cached`.
6. **Tests.** After approval, each configured command runs once, in the
   worktree, with `shell=False`, its own process group, and a hard timeout. A
   timeout kills the group and is reported as `timed_out`, which means
   incomplete. Nothing is retried. Files that the tests changed are listed.
7. **Cleanup.** The worktree, the local branch, and the temporary directory
   are removed, also when an exception is raised. The original repository's
   state is then compared with the recorded snapshot (`original tree:
   unchanged`).

The git helper can only run `rev-parse`, `status`, `symbolic-ref`,
`worktree`, `branch`, `apply`, `diff`, and `check-ref-format`. It always
disables repository hooks (`core.hooksPath=/dev/null`) and fsmonitor, and it
drops every `GIT_*` environment variable.

### Outcomes

| Outcome | Exit | Meaning |
| --- | --- | --- |
| `tests_passed` | 0 | Applied, and every configured command exited 0 |
| `check_passed` | 0 | `--check-only`: the patch would apply. Nothing was applied. |
| `invalid_repository`, `uncommitted_changes` | 10 | Nothing was done |
| `invalid_patch` | 11 | Empty, malformed, or unparseable |
| `unsafe_patch` | 12 | Rejected by a PatchPilot path or content rule |
| `patch_check_failed` | 13 | Well formed, but does not apply to the base |
| `not_applied` | 14 | Declined at the prompt |
| `patch_applied` | 15 | Applied. **No tests were run.** |
| `tests_failed` | 16 | Applied, and at least one command failed. **Not a successful fix.** |
| `tests_incomplete` | 17 | Applied, and a command timed out or could not start |
| `workspace_error` | 18 | The workspace could not be created, or the apply could not be verified |
| (cleanup failed) | 19 | Overrides the codes above. See `cleanup.errors`. |
| usage error | 2 | Unreadable patch or test config |

Each test reports one of `passed`, `failed`, `timed_out`, `not_started`,
`skipped` (declined), or `not_run` (the patch did not apply). It also reports
the real exit code, the duration, and up to the last 20,000 characters of
output.

### Test-command environment and risk

Running a repository's tests **runs repository code**, including code the
patch adds. Commands get only `PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`, and `TZ`,
plus a temporary `HOME` and `TMPDIR`, plus any names listed in `pass_env`.
`AGNTID_ACCESS_TOKEN`, `GITHUB_TOKEN`, and `GH_TOKEN` can never be passed.
Values of secret-looking variables (names containing `TOKEN`, `SECRET`,
`PASSWORD`, `API_KEY`, or similar) are replaced with `[REDACTED]` in the
captured output.

This is **not an operating-system sandbox**:

- Commands run with your user permissions.
- They have network access.
- They can read any file you can read.
- A process that leaves its process group (for example with `setsid`) can
  outlive a timeout.
- Background processes that a command leaves running after a normal exit are
  not killed.

Only run tests for patches whose code you are willing to execute.

### Tests

```bash
pytest -q tests/test_patch_workspace.py tests/test_patch_verification.py tests/test_patch_cli.py
```

The parser tests are pure unit tests on diff text. All other tests create real
temporary Git repositories with the real `git` binary and run real
subprocesses. Their patches come from real `git diff` output. There are no
mocks, no network, and no GitHub or AgntID.

### Integration boundary

`run_patch_workflow(repository, base_revision, patch, approve=..., tests=...)`
accepts the diff as text or bytes, so a later module can supply one. That
module would build the diff from files read through AgntID (Module 3).
Module 4 does not read issues or files from GitHub, does not generate
patches, and does not create commits or pull requests.

## Web UI (`patchpilot-ui`)

A local browser interface over Modules 1-4. It adds no capability of its own:
every screen shows what one of the modules really did, and every business-tool
call still goes through the Module 2 task session. Design notes:
[docs/superpowers/specs/2026-10-10-patchpilot-ui-design.md](docs/superpowers/specs/2026-10-10-patchpilot-ui-design.md).
Screenshots of the running app against the live local runtime and a real patch
run are in [docs/screenshots/](docs/screenshots/).

| File | Purpose |
| --- | --- |
| `src/patchpilot/ui_cli.py` | `patchpilot-ui`: operator flags, loopback-only listener |
| `src/patchpilot/web/app.py` | Starlette routes, NDJSON event streams, static files |
| `src/patchpilot/web/guard.py` | Local-only request guard and security headers |
| `src/patchpilot/web/config.py` | Operator settings and the read-only configuration view |
| `src/patchpilot/web/requests.py` | Strict request validation (unknown fields are rejected) |
| `src/patchpilot/web/runs.py` | Runs, events, and the in-memory activity log |
| `src/patchpilot/web/operations.py` | Runtime report, investigation, task probe, patch check and apply |
| `frontend/` | Vite + Preact + TypeScript source; builds into `src/patchpilot/web/static/` |
| `tests/test_web_*.py`, `tests/test_ui_cli.py` | Backend tests over real HTTP |

### Build and run

You need Node.js 22.18 or later to build the frontend. Python serves it.

```bash
python -m pip install -e ".[dev]"
npm --prefix frontend ci
npm --prefix frontend run build      # type-checks, then writes src/patchpilot/web/static/
patchpilot-ui --repo ~/src/project --test-command "python -m pytest -q"
# open http://127.0.0.1:8765
```

| Option | Meaning |
| --- | --- |
| `--host` | `127.0.0.1` (default), `localhost`, or `::1`. Any other address is refused. |
| `--port` | Default 8765; `0` picks a free port. The URL is printed once the socket listens. |
| `--repo PATH` | A local repository the UI may patch. Repeatable. The browser can only choose among these. |
| `--test-command`, `--test-config`, `--test-timeout` | Test commands, exactly as for `patchpilot-patch`. The browser can approve or decline them, never add one. |
| `--allow-sensitive-path PATTERN` | Sensitive-path exception, as for `patchpilot-patch`. Startup only. |
| `--mapping FILE` | Reviewed capability map (otherwise `PATCHPILOT_CAPABILITY_MAP`) |
| `--env-file` | dotenv file (default `./.env`). AgntID settings are read again for every operation. |

During frontend work, `npm --prefix frontend run dev` rebuilds on every change;
reload the page to see it.

### What the screens are connected to

| Screen | Real operation | Module |
| --- | --- | --- |
| Runtime strip, header, Security: connection and tools | one live `tools/list` per check | M1 `discover_tools` |
| Security: capability mappings | mapping checks against the live schemas (application checks) | M3 `validate_mapping` |
| Overview: issue intake and timeline | one AgntID task: `get_issue`, then `read_file` for listed paths | M2 + M3 `execute_capability` |
| Security: task probe | one AgntID task for a prompt; optionally one call to one shortlisted tool | M2 `TaskSession.call_tool` |
| Overview: proposed change | check, then apply and test in a disposable worktree after explicit approval | M4 `run_patch_workflow` |
| Workspaces | live read-only `git` snapshot of each `--repo`; this session's patch runs | M4 |
| Activity | every event this server process recorded, tagged `AgntID data` or `PatchPilot` | - |
| Settings | the configuration in effect; never a secret value | - |

What is not available, and says so on screen: AI patch generation (supply a
diff), automated code investigation (only the files you list are read),
pull-request creation (there is no write capability), and GitHub reads until a
reviewed capability map is registered. There is no "Issues" page because no
operation can list issues.

### Security model

The backend runs repository code, so a web page on another origin must not be
able to drive it:

- It listens on loopback only, and answers only `Host` values that name its
  loopback address (blocks DNS rebinding).
- Every API call except `GET /api/session` needs a random per-process session
  header. There is no CORS support, so a cross-origin page can neither read the
  token nor send the header. `POST` must be `application/json`; a foreign
  `Origin` or `Sec-Fetch-Site: cross-site` is refused.
- Responses carry a strict Content-Security-Policy (no inline script),
  `nosniff`, `no-referrer`, and `frame-ancestors 'none'`.
- Request bodies with unknown fields are rejected, so a request cannot carry a
  repository path, a test command, or a path-policy exception.
- `patch_apply` must carry the SHA-256 of the patch that was checked; a changed
  patch gets `409`. Applying and testing each need their own explicit approval.
- The task probe refuses control tools, tools that declare writes, tools that
  do not declare `readOnlyHint: true` unless the operator confirms, reserved
  correlation arguments, and runtime-managed `agntid_*` arguments. The live
  runtime's schemas list `agntid_task_id`, `agntid_delegation_id`, and
  `agntid_approval`; a browser must never fill them. Run records show a
  reserved argument's name but never its value. Arguments are checked against
  the tool's `inputSchema` before anything is sent. If this UI check ever
  fails, the Module 2 dispatcher still refuses the call.
- One AgntID task at a time (the SDK's `get_last_denial` is thread-local), one
  patch run at a time.
- The browser never receives `AGNTID_ACCESS_TOKEN` or any other secret; the
  configuration view reports only whether a token is supplied.

### Hosted frontend (public mode)

The frontend can be hosted on Vercel (`frontend/vercel.json`) and proxy
`/api/*` to a backend on the operator's machine through a tunnel:

```bash
PATCHPILOT_ACCESS_KEY="$(cat ~/.config/patchpilot/access-key)" \
  patchpilot-ui --port 8766 --repo ~/patchpilot-demo-repo \
  --test-command "python3 -m unittest -v" --public-origin https://tech.runs-on.dev
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8766 --http-host-header 127.0.0.1:8766
```

The tunnel URL goes into the `/api/:path*` rewrite in `frontend/vercel.json`.
A quick tunnel gets a new URL on every start, so the rewrite must be updated
and redeployed after a restart.

- The listener stays on loopback. The tunnel must rewrite `Host` to the
  loopback address, so the Host check still applies.
- `--public-origin` is added to the accepted `Origin` values. It must be a bare
  `https` origin.
- `--public-origin` refuses to start without `PATCHPILOT_ACCESS_KEY` (at least
  24 characters, environment only). Every request then needs it in the
  `X-PatchPilot-Access-Key` header. The frontend asks for it on its own sign-in
  screen and keeps it in `sessionStorage` for the tab. The session header is
  still required.
- The access key is the only barrier between the internet and test execution:
  anyone with it can apply a patch and run the configured tests, which runs
  repository code with the operator's permissions.

### Tests

```bash
pytest -q tests/test_web_*.py tests/test_ui_cli.py     # backend, real uvicorn over real HTTP
npm --prefix frontend test                              # pure frontend logic (node --test)
npm --prefix frontend run typecheck
npm --prefix frontend run lint
```

The backend tests use the same fixture MCP servers as Modules 1-3 (**not
AgntID, not GitHub**) and real temporary Git repositories. The frontend tests
cover diff parsing, issue references, timeline stages, and the decision ledger.

### Live check (2026-10-10)

Against the local AgntID runtime on `localhost:8082`, the UI listed 6 tools
(1 business tool, `demo_get_ticket`). One task probe opened a real task; the
runtime's `standard` intent engine shortlisted `demo_get_ticket`; the one call
was allowed and returned the tool's simulated ticket. No denial has been
observed live yet. GitHub reads remain unverified: no GitHub MCP server is
attached and no capability map exists.

Recheck after the reserved-argument fix (2026-10-10, about 09:36 UTC): a real
task opened and the runtime shortlisted `demo_get_ticket`. Three calls with
`agntid_task_id`, `agntid_delegation_id`, or `agntid_approval` were rejected
by PatchPilot. The runtime log shows they were never sent: it received only task
open, one ordinary `demo_get_ticket` call (allowed), and task close. Two UI
probes with reserved arguments were refused before a task opened. The runtime
received only tool listings.

**Resolved: calls are now linked to their task (2026-10-10, about 09:44 UTC).**
Before the fix the runtime logged, for an ordinary `demo_get_ticket` call:
`Intent validation flow started ... (prompt_length=0 ...)`, then
`No prompt available - skipping intent validation` and
`intent_parsing (status=skipped)`. Cause: the SDK wrapper sent `_task_id`, but
only the runtime's `TaskMiddleware` reads that name. That middleware is not
registered: `server/factory.py` registers it only when FastMCP has
`add_middleware`, and the runtime's `mcp==1.27.2` does not. Mounted tools read
`agntid_task_id` instead.

Fix: `open_task_session` now calls
`agntid.wrap_client(client, task_id, task_argument="agntid_task_id")`. The SDK's
`_inject_context` copies the caller's arguments and then sets that one key to
the task ID. Callers still cannot supply it: `agntid_task_id` is reserved, and
the dispatcher refuses it before anything is sent. After the fix the runtime
logged, for the same call through `TaskSession.call_tool`:
`policy_check_with_task_context task_id=48788c80-... prompt_len=44`,
`Intent v2 pipeline (parallel triad)`, NLI `entailment=0.996`,
`VERDICT: allow`, and `intent_parsing (status=passed)`. The same holds for a
call made from the UI task probe.

## Security

- The access token is passed only to the SDK transport. The SDK sends it as a
  bearer credential. The connector never prints, logs, returns, or stores it.
  It is also not in `repr()`, error messages, or JSON output.
- Errors are rebuilt as token-free messages. The original exception is not
  chained, because its httpx request object holds the `Authorization` header.
- URLs that contain credentials are rejected. Other URLs are shown without
  user-info, query string, or fragment.
- Terminal control characters in tool names and descriptions are escaped in text
  output.
- A token value of `Bearer …` is rejected. The literal value `oauth` is also
  rejected: the underlying fastmcp client would treat it as a request to start
  an interactive OAuth browser flow.
- PatchPilot holds no GitHub credential. GitHub credentials live only on the
  runtime host, referenced by name in the AgntID MCP server settings.
- Module 1 only lists tools. Modules 2 and 3 call business tools only through the
  task-bound wrapped client, and never put the token in task data, results,
  logs, or error messages.
