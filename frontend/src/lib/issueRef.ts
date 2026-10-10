// Mirrors parse_issue_reference in src/patchpilot/web/requests.py, so the form
// can explain a bad reference before sending it. The backend still decides.

const OWNER = "[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})";
const REPO = "[A-Za-z0-9._-]{1,100}";
const SHORT_FORM = new RegExp(`^(${OWNER})/(${REPO})#([0-9]{1,10})$`);
const URL_FORM = new RegExp(`^https://github\\.com/(${OWNER})/(${REPO})/issues/([0-9]{1,10})/?(?:[?#].*)?$`);
const MAX_ISSUE_NUMBER = 2 ** 31 - 1;

export type IssueRef = { owner: string; repo: string; number: number; reference: string };

export function parseIssueRef(text: string): IssueRef | null {
  const value = text.trim();
  const match = URL_FORM.exec(value) ?? SHORT_FORM.exec(value);
  if (!match) return null;
  const [, owner = "", repo = "", digits = ""] = match;
  const number = Number(digits);
  if (repo === "." || repo === ".." || number < 1 || number > MAX_ISSUE_NUMBER) return null;
  return { owner, repo, number, reference: `${owner}/${repo}#${number}` };
}

export function defaultPrompt(ref: IssueRef, files: string[]): string {
  const what = files.length ? `the issue and the files ${files.join(", ")}` : "the issue";
  return `Investigate GitHub issue ${ref.reference}: read ${what} to understand the reported problem.`;
}
