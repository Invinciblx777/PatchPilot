// Talks only to the PatchPilot backend on this origin. The browser never sees
// an AgntID or GitHub credential and never talks to the AgntID runtime.

import type { ApiError, Run, RunEvent, Session } from "./types.ts";

const SESSION_HEADER = "X-PatchPilot-Session";
let token: string | null = null;

export type ApiResult<T> = { ok: true; data: T } | { ok: false; status: number; error: ApiError };

const UNREACHABLE: ApiError = {
  code: "network",
  message: "The PatchPilot server did not answer. Check that patchpilot-ui is still running, then reload.",
  problems: [],
};

async function send<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<ApiResult<T>> {
  const headers: Record<string, string> = {};
  if (token) headers[SESSION_HEADER] = token;
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
      headers: token ? { [SESSION_HEADER]: token } : {},
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
