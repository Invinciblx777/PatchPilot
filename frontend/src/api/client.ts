// Talks only to the PatchPilot backend on this origin. The browser never sees
// an AgntID or GitHub credential and never talks to the AgntID runtime.

import type { ApiError, Run, RunEvent, Session } from "./types.ts";

const SESSION_HEADER = "X-PatchPilot-Session";
// Sent only to a server started in public mode (--public-origin); a local
// server ignores it. Kept for this tab only.
const ACCESS_HEADER = "X-PatchPilot-Access-Key";
const ACCESS_KEY_STORAGE = "patchpilot.accessKey";
let token: string | null = null;
let accessKey: string | null = readAccessKey();

function readAccessKey(): string | null {
  try {
    return sessionStorage.getItem(ACCESS_KEY_STORAGE);
  } catch {
    return null;
  }
}

export function setAccessKey(key: string | null): void {
  accessKey = key;
  try {
    if (key === null) sessionStorage.removeItem(ACCESS_KEY_STORAGE);
    else sessionStorage.setItem(ACCESS_KEY_STORAGE, key);
  } catch {
    /* storage unavailable: the key lasts for this page only */
  }
}

export function hasAccessKey(): boolean {
  return accessKey !== null;
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  if (token) headers[SESSION_HEADER] = token;
  if (accessKey) headers[ACCESS_HEADER] = accessKey;
  return headers;
}

export type ApiResult<T> = { ok: true; data: T } | { ok: false; status: number; error: ApiError };

const UNREACHABLE: ApiError = {
  code: "network",
  message: "The PatchPilot server did not answer. Check that patchpilot-ui is still running, then reload.",
  problems: [],
};

async function send<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<ApiResult<T>> {
  const headers = authHeaders();
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch {
    return { ok: false, status: 0, error: UNREACHABLE };
  }
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (response.ok) return { ok: true, data: payload as T };
  const error = (payload as { error?: Partial<ApiError> } | null)?.error;
  return {
    ok: false,
    status: response.status,
    error: {
      code: error?.code ?? `http_${response.status}`,
      message: error?.message ?? `The PatchPilot server answered HTTP ${response.status}.`,
      problems: error?.problems ?? [],
    },
  };
}

export async function startSession(): Promise<ApiResult<Session>> {
  const result = await send<Session>("GET", "/api/session");
  if (result.ok) token = result.data.token;
  return result;
}

export const api = {
  get: <T>(path: string) => send<T>("GET", path),
  post: <T>(path: string, body: unknown) => send<T>("POST", path, body),
};

export type StreamMessage = { type: "event"; event: RunEvent } | { type: "end"; run: Run } | { type: "heartbeat" };

/**
 * Read a run's NDJSON event stream from `after` until the run ends.
 * Returns true when the end message arrived, false if the stream broke first.
 */
export async function streamRun(runId: string, after: number, onMessage: (message: StreamMessage) => void): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch(`/api/runs/${encodeURIComponent(runId)}/events?after=${after}`, {
      headers: authHeaders(),
      cache: "no-store",
    });
  } catch {
    return false;
  }
  if (!response.ok || !response.body) return false;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) {
          const message = JSON.parse(line) as StreamMessage;
          onMessage(message);
          if (message.type === "end") {
            void reader.cancel();
            return true;
          }
        }
        newline = buffer.indexOf("\n");
      }
    }
  } catch {
    return false;
  }
  return false;
}
