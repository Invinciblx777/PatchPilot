# PatchPilot UI: Dark Editorial Comic

Date: 2026-10-10
Status: design approved in chat (task probe: "shortlist + one confirmed call")

## 1. Existing architecture (inspected before any change)

PatchPilot is a Python 3.11 package (`src/patchpilot`, setuptools, src layout)
with three CLIs and no frontend or web server.

| Module | Public surface used by the UI | Notes |
| --- | --- | --- |
| M1 `agntid_connector` | `load_config(env)`, `discover_tools(config) -> DiscoveryResult`, `redact_url`, `sdk_version`, typed `ConnectorError`s (`kind`, `phase`, token-free `message`) | One MCP session per discovery, bounded by `AGNTID_TIMEOUT_SECONDS` |
| M2 `task_session` | `load_identity(env)`, `open_task_session(client, config, identity, prompt)` (async context), `TaskSession.tools`, `.task_open`, `.call_tool -> ToolCallResult` (`success` / `denied` / `rejected` / `error`), `TaskOpenError.failure.stage` | A task exists only inside one `async with`; the wrapped client is never exposed |
| M3 `github_capabilities` | `load_capability_map`, `validate_mapping`, `execute_capability -> CapabilityResult` (9 statuses, provenance), `find_candidates`, `schema_fingerprint` | Read-only capabilities `get_issue`, `get_repository`, `read_file`; disabled until a reviewed map exists |
| M4 `patch_engine` | `run_patch_workflow(repo, base, patch, approve=callback, policy, tests, allow_uncommitted, check_only) -> WorkflowResult` | Synchronous, local only; real `git diff --cached`, real test exit codes, durations, output, cleanup report |
| CLIs | `discover_cli.merged_env`, `github_preflight_cli.PORTAL_STEPS`, `patch_verification.load_test_config` / `parse_test_command` | `.env` fills unset keys only |

Live state on 2026-10-10: the runtime at `http://localhost:8082/mcp` answers
`tools/list` with one business tool (`demo_get_ticket`) and the five AgntID
control tools. There is no `.env` and no capability map, so the GitHub path is
disabled. Baseline: 226 tests pass, 2 live tests skipped.

## 2. Integration points

| UI function | Backend operation | Modules |
| --- | --- | --- |
| Runtime connection, tool inventory, mapping checks | `GET /api/runtime` | M1 `discover_tools`, M3 `validate_mapping`, `find_candidates`, `schema_fingerprint` |
| Issue intake and optional file reads | `POST /api/runs` kind `investigation` | M1 discovery, M3 mapping check, M2 `open_task_session`, M3 `execute_capability(get_issue / read_file)` |
| Task probe (Security) | `POST /api/runs` kind `probe` | M1 discovery, M2 `open_task_session` and `TaskSession.call_tool` (at most once) |
| Patch check | `POST /api/runs` kind `patch_check` | M4 `run_patch_workflow(check_only=True)` |
| Patch apply and tests | `POST /api/runs` kind `patch_apply` | M4 `run_patch_workflow` with explicit approvals |
| Workspaces | `GET /api/repos` | M4 `open_repository` (read-only git) |
| Settings | `GET /api/config` | `load_config`, `load_identity`, `load_capability_map`, operator flags |

Nothing calls GitHub, the raw MCP client, or the AgntID runtime UI at
`localhost:8080`. Every business-tool call goes through `TaskSession.call_tool`.

## 3. Backend

### Layout

- `src/patchpilot/web/config.py`: `UISettings` (operator configuration from flags; env re-read per operation like the CLIs) and the read-only configuration view.
- `src/patchpilot/web/guard.py`: local-only request guard and security headers.
- `src/patchpilot/web/requests.py`: strict request validation (unknown fields rejected).
- `src/patchpilot/web/runs.py`: run registry, events, and the activity log.
- `src/patchpilot/web/operations.py`: runtime report, investigation, task probe, patch check and apply.
- `src/patchpilot/web/app.py`: Starlette routes, NDJSON streaming, static files.
- `src/patchpilot/ui_cli.py`: `patchpilot-ui` entry point (uvicorn).

Starlette and uvicorn are already installed through the SDK's `mcp`
dependency; they are declared because PatchPilot now imports them.

### Operator configuration (startup only, never from the browser)

```
patchpilot-ui [--host 127.0.0.1] [--port 8765] [--env-file .env] [--mapping FILE]
              [--repo PATH]... [--test-config FILE] [--test-command CMD]... [--test-timeout S]
              [--allow-sensitive-path PATTERN]...
```

- `--host` must be a loopback address. Anything else is refused at startup.
- Repositories and test commands come only from these flags. The browser picks a
  repository by ID from that list and can only approve or decline the configured
  commands. It cannot add a path, a command, or a sensitive-path exception.
- AgntID settings (`AGNTID_*`, `PATCHPILOT_AGENT_ID`, `PATCHPILOT_USER_ID`,
  `PATCHPILOT_CAPABILITY_MAP`) are read per operation with `merged_env`, so a
  configuration error is shown in the UI instead of preventing startup.

### Local-only guard

A local web server that runs repository tests is a drive-by code-execution
target, so every request passes these checks:

1. `Host` must be `127.0.0.1:PORT`, `localhost:PORT`, or `[::1]:PORT` (blocks DNS rebinding).
2. Every `/api/*` route except `GET /api/session` needs `X-PatchPilot-Session`
   equal to a random per-process token (constant-time compare). Cross-origin
   pages cannot read `/api/session` (no CORS) and cannot send the header without
   a preflight, which is never approved.
3. `POST` needs `Content-Type: application/json`; an `Origin` header, if present, must be the server's own origin. `Sec-Fetch-Site`, if present, must be `same-origin` or `none`.
4. Responses carry a strict CSP (`default-src 'self'`, no inline script, `frame-ancestors 'none'`), `nosniff`, `no-referrer`, COOP/CORP `same-origin`; API responses are `no-store`.
5. No secret value is ever serialized: config output reports `accessTokenSupplied: true/false` only.

### Runs and events

A run is one operation executed in the background (an asyncio task; M4 work in
a worker thread). The browser starts it with `POST /api/runs`, then follows
`GET /api/runs/{id}/events?after=N` (NDJSON stream, fetch with headers; it ends
when the run finishes). A disconnect does not stop the run, so a task is always
closed by its own `async with`.

```
Event { seq, runId, at (UTC ISO), source: "agntid" | "patchpilot",
        step, status: info|ok|warning|denied|error, title, detail }
```

`source` separates data returned by the AgntID runtime (tool list, task-open
result, shortlist, denial, tool result) from PatchPilot's own actions and
application checks (mapping and argument checks, patch parsing, git apply,
tests). The activity log is in memory, capped, and covers only this server
process. One AgntID run at a time (the SDK's `get_last_denial` is thread-local,
so concurrent tasks could swap denials); one patch run at a time. A busy
request gets `409`.

### Run kinds

**investigation** `{issue, prompt, files?: [{path, ref?}] (max 5)}`
1. Parse `issue` server-side: `https://github.com/OWNER/REPO/issues/N` or `OWNER/REPO#N`. Invalid input is a `400` before any run.
2. Load config, identity, capability map. No `get_issue` mapping: the run ends as `unavailable` without connecting.
3. M1 discovery; M3 `validate_mapping` for each needed capability (application check).
4. M2 task open; record task ID, intent engine, shortlist (or the failure stage and reason).
5. M3 `execute_capability(get_issue)`, then `read_file` per path. The run stops at the first result that is not `success`: no retry and no other tool after a denial or failure.
6. Task close is recorded; a close failure is an error event.

**probe** `{prompt, call?: {tool, arguments, confirmUnannotated}}`
1. Load config and identity; M1 discovery.
2. If `call` is present, check before opening the task: the tool is a listed business tool (not a control tool); it does not declare `readOnlyHint: false` or `destructiveHint: true` (Module 3's rule); `confirmUnannotated` is true when `readOnlyHint: true` is not declared; the arguments are an object, use no reserved names (`_task_id`, `_delegation_id`, `__agntid_*`) and no runtime-managed `agntid_*` names, and validate against the tool's `inputSchema`. A failed check ends the run with nothing sent and no task opened. (The `agntid_*` rule was added after the live runtime's schemas turned out to list `agntid_task_id`, `agntid_delegation_id`, and `agntid_approval`. Since the reserved-argument hardening pass, this check and the Module 2 dispatcher share one rule, `task_session.is_reserved_argument`, which also covers `_agntid_*`; run records show reserved names with their values withheld.)
3. M2 task open; shortlist recorded.
4. If `call` is present: one `session.call_tool` under `AGNTID_TIMEOUT_SECONDS`. A tool missing from the shortlist comes back `rejected` from the M2 dispatcher (nothing sent). A timeout is `outcome unknown`.

**patch_check** `{repoId, base, patch, allowUncommitted}`: M4 with `check_only=True`; result includes `patchSha256` and structured file changes (`parse_patch`, pure).

**patch_apply** `{repoId, base, patch, patchSha256, allowUncommitted, approveApply: true, approveTests}`: rejected with `409` when the SHA-256 of `patch` differs from `patchSha256` (the approval is bound to the reviewed patch). The M4 approval callback returns exactly the two flags and records an event when each stage is reached.

Request bodies with unknown fields are rejected, so a request cannot carry a
test command, a path policy, or a repository path.

## 4. Frontend

`frontend/`: Vite + Preact + TypeScript (strict), plain CSS with design tokens
and CSS modules, Biome for lint and format. The build writes to
`src/patchpilot/web/static/` (git-ignored, packaged as package data). Fonts are
self-hosted from Fontsource packages (SIL OFL): Archivo (one family for
display and UI; its width axis gives compact headlines), Commit Mono (code),
and Shantell Sans with its informal axis for a handful of margin notes. These
replaced the first draft's Instrument Sans, Geist, Geist Mono, and Caveat
during the design review, to avoid default choices. There are no CDN requests.

### Visual system

- Tokens: background `#0B0B0A`, surfaces `#121211` / `#191918` / hover `#222220`,
  ink `#F2F0E9`, secondary `#A6A39A`, line `#34332F`, accent `#D97745`, allow
  `#54C99A`, deny `#F07167`, warn `#E3B66A`. Most of the screen stays monochrome.
- Type: compact display headings (condensed width, tight tracking), legible
  15px UI text, sentence-case labels, monospace for tool names, hashes, diffs.
- Radius 2-4px, hairline rules instead of card stacks, no gradients or glows.
- Ink layer (original SVG, used sparingly): irregular ink outline on the
  evidence panel (decision ledger) and the expanded run only; printer's
  crop-mark corners on primary sections; the orange marker underline marks
  the current navigation item (not a decorative heading accent); hand-drawn
  timeline connectors; a rubber stamp for a decision the runtime returned;
  diagonal hatching that means "unavailable" (drafting convention); a line-art
  shield for runtime protection; a comic bug for the empty issue intake;
  small empty-state drawings for Workspaces and Activity.
- Motion: ink draw-on for illustrations and decision marks, 150-200ms state
  transitions, diff expand. Off under `prefers-reduced-motion` or the in-app
  "Reduce motion" setting. No looping decoration.

### Status vocabulary (shape + text, never color alone)

| State | Mark | Color |
| --- | --- | --- |
| Allowed by AgntID (real call returned a result) | check in circle | green |
| Denied by AgntID | cross | red |
| Waiting for AgntID review | pause | amber |
| Outcome unknown (sent, no definite answer) | `?` in dashed circle | amber |
| Not observed / not sent | dashed circle | grey |
| Not started | empty circle | grey |
| Unavailable | hatched circle | grey |
| Running | drawn arc | orange |
| PatchPilot check passed (application check) | outlined check | ivory, never green |
| Test passed / failed (real exit code) | check / cross | green / red |

### Pages

Only implemented destinations appear in the navigation.

- **Overview**: the journey. (1) runtime strip: connection, endpoint, tool
  counts, capability-map state, re-check; (2) issue intake (issue reference,
  editable task prompt, optional file paths) with the bug empty state;
  (3) task timeline; (4) patch review: repository selector, base, diff input,
  check, explicit approvals, real diff, test results; (5) security ledger and
  recent operations.
- **Workspaces**: configured repositories with a live read-only snapshot
  (branch, HEAD, clean or dirty) and this session's patch runs (outcome,
  tests, cleanup, original tree preserved).
- **Security**: shield header, runtime connection, tool inventory
  (control-tool flags, read-only declaration, schema hash), capability
  mappings (application checks), task probe.
- **Activity**: the event log grouped by run, filterable by source.
- **Settings**: read-only configuration and its sources, test commands,
  sensitive-path policy, and the motion preference (stored locally).

"Issues" is not a page: no backend operation lists issues.

### Timeline stages

| Stage | Source of truth | States |
| --- | --- | --- |
| Issue retrieval | investigation run, `get_issue` result | not started, unavailable (no mapping), running, allowed/done, denied, not sent, failed, unknown |
| Code investigation | `read_file` results for operator-listed paths | not started (no paths), unavailable, per-file outcomes; no automated investigation exists |
| Patch preparation | patch_check / patch_apply | waiting for a supplied diff (PatchPilot does not generate patches), checked, rejected, applied |
| Test execution | patch_apply tests | not started, unavailable (no commands configured), skipped, passed, failed, timed out, could not start |
| Security evaluation | AgntID decisions observed in this journey; PatchPilot checks listed separately | not observed, allowed (only real allows), denied, unknown |
| Pull request | none | unavailable: no write capability in this build |

### Security ledger (five separate facts)

Runtime connection; tool discovered (listed by the runtime); available for this
task (in the shortlist); policy decision actually observed; execution outcome.
PatchPilot's own checks (mapping schema, arguments) appear on a separate line
labelled "application check, not an AgntID decision". The denial reason,
policy name, intent mismatch, and step are shown exactly as returned.

## 5. Testing

- Backend tests run a real uvicorn server on a free port and use the existing
  fixture MCP servers (not AgntID) and real temporary Git repositories:
  guard behaviour, secret absence, runtime report, every investigation and
  probe outcome (including "nothing sent" paths verified against the fixture's
  call record), patch check/apply with approval binding, concurrency `409`s.
- Frontend: `tsc --noEmit`, `biome check`, `vite build`, and `node --test` for
  pure logic (diff parsing and counts, issue references, stage derivation).
- Bundle and source scan for secrets; screenshots of the running app with the
  cached Playwright Chromium at desktop and mobile widths.

## 6. Out of scope

AI patch generation, pull requests, GitHub writes, new capabilities, later
security modules, changes to the AgntID runtime or its UI, persistence across
restarts, editing configuration from the browser, keeping workspaces.
