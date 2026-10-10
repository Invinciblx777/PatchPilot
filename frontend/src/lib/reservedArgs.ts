// Mirrors is_reserved_argument in src/patchpilot/task_session.py, so the form
// can explain a refusal before sending. The backend still decides.

const SDK_CORRELATION = new Set(["_task_id", "_delegation_id"]);
const AGNTID_NAMESPACE = /^_*agntid_/i;

export function isReservedArgument(name: string): boolean {
  return SDK_CORRELATION.has(name) || AGNTID_NAMESPACE.test(name);
}
