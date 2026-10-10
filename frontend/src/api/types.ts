// Shapes returned by the PatchPilot backend (src/patchpilot/web). Field names
// match the JSON exactly; nothing here is computed in the browser.

export type ErrorView = { kind: string; phase: string; message: string; causeType?: string | null };

export type Session = { token: string; startedAt: string; version: string };

export type CapabilityName = "get_issue" | "get_repository" | "read_file";

export type MappingView = {
  tool: string;
  schemaSha256: string;
  arguments: Record<string, string>;
  fixedArguments: Record<string, unknown>;
};

export type CapabilityMapView =
  | { configured: false }
  | { configured: true; path: string; ok: true; capabilities: Partial<Record<CapabilityName, MappingView>> }
  | { configured: true; path: string; ok: false; error: string };

export type TestCommandView = {
  name: string;
  argv: string[];
  display: string;
  timeoutSeconds: number;
  passEnv: string[];
};

export type RepoConfig = { id: string; name: string; path: string };

export type ConfigView = {
  patchpilotVersion: string;
  sdkVersion: string;
  envFile: string | null;
  envFileFound: boolean;
  sources: string[];
  agntid: { ok: true; endpoint: string; accessTokenSupplied: boolean; timeoutSeconds: number } | { ok: false; error: ErrorView };
  identity: { ok: true; agentId: string; userId: string } | { ok: false; error: ErrorView };
  capabilityMap: CapabilityMapView;
  capabilityParameters: Record<CapabilityName, { name: string; type: string; required: boolean }[]>;
  repos: RepoConfig[];
  tests: TestCommandView[];
  patchPolicy: { sensitivePatterns: string[]; allowedSensitivePatterns: string[] };
};

export type ReadOnlyState = "declared" | "declares_write" | "not_declared";

export type ToolView = {
  name: string;
  title: string | null;
  description: string | null;
  inputSchema: Record<string, unknown>;
  outputSchema: Record<string, unknown> | null;
  annotations: Record<string, unknown> | null;
  isRuntimeControlTool: boolean;
  schemaSha256: string;
  readOnly: ReadOnlyState;
};

export type SchemaCheck = { ok: boolean; problems: string[]; warnings: string[] };

export type CapabilityEntry = {
  candidates: string[];
  mapping: { tool: string; schemaSha256: string; source: string } | null;
  listedByRuntime: boolean | null;
  schemaCheck: SchemaCheck | null;
};

export type RuntimeReport =
  | {
      ok: true;
      checkedAt: string;
      durationMs: number;
      sdkVersion: string;
      endpoint: string;
      accessTokenSupplied: boolean;
      toolCount: number;
      businessToolCount: number;
      tools: ToolView[];
      capabilities: Record<CapabilityName, CapabilityEntry>;
      capabilityMap: { configured: boolean; ok?: boolean; path?: string; error?: string };
      nextSteps: string[];
    }
  | {
      ok: false;
      checkedAt: string;
      durationMs: number;
      sdkVersion: string;
      endpoint: string | null;
      error: ErrorView;
    };

export type EventSource = "agntid" | "patchpilot";
export type EventStatus = "info" | "ok" | "warning" | "denied" | "error";

export type RunEvent = {
  seq: number;
  runId: string;
  at: string;
  source: EventSource;
  step: string;
  status: EventStatus;
  title: string;
  detail: Record<string, unknown>;
};

export type RunKind = "runtime_check" | "investigation" | "probe" | "patch_check" | "patch_apply";

export type RunSummary = {
  id: string;
  kind: RunKind;
  title: string;
  state: "running" | "finished";
  outcome: string | null;
  startedAt: string;
  finishedAt: string | null;
  eventCount: number;
};

export type Run = RunSummary & {
  request: Record<string, unknown>;
  events: RunEvent[];
  result: Record<string, unknown> | null;
};

// Decision and execution values computed by the backend (web/operations.py).
export type Decision = "allowed" | "denied" | "review" | "not_observed" | "unknown" | "not_determined";
export type Execution = "success" | "blocked" | "failed" | "unknown" | "not_sent";

export type TestStatus = "passed" | "failed" | "timed_out" | "not_started" | "skipped" | "not_run";

export type TestRun = {
  name: string;
  argv: string[];
  status: TestStatus;
  exitCode: number | null;
  durationSeconds: number | null;
  output: string;
  outputTruncated: boolean;
  note: string;
};

export type FileChange = {
  kind: "modify" | "create" | "delete" | "rename" | "copy";
  oldPath: string | null;
  newPath: string | null;
  added: number;
  removed: number;
  oldMode: string | null;
  newMode: string | null;
};

export type PatchResult = {
  outcome: string;
  patchApplied: boolean;
  repository: string | null;
  baseRevision: string;
  baseSha: string | null;
  originalHead: string | null;
  originalBranch: string | null;
  originalDirty: boolean | null;
  originalPreserved: boolean | null;
  workspacePath: string | null;
  workspaceBranch: string | null;
  files: string[];
  changedPaths: string[];
  problems: string[];
  diffStat: string;
  diff: string;
  tests: TestRun[];
  changedByTests: string[];
  cleanup: { status: "removed" | "kept" | "failed"; errors: string[] } | null;
  notes: string[];
  repoId: string;
  repoName: string;
  patchSha256: string;
  fileChanges: FileChange[] | null;
  testsConfigured: number;
};

export type RepoView =
  | { id: string; name: string; path: string; ok: true; branch: string | null; head: string | null; dirty: boolean; changedPaths: number }
  | { id: string; name: string; path: string; ok: false; error: string };

export type ApiError = { code: string; message: string; problems: string[] };
