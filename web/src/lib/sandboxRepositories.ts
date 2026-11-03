import type { LastSandboxRepo } from "./repoPreferences";

/** Session response label containing space-joined `<url>[#<branch>]` repository workspaces. */
export const SANDBOX_REPO_LABEL_KEY = "omnigent.sandbox.repo";

/**
 * Return true when ``url`` is acceptable as a sandbox repository URL.
 *
 * Mirrors the server's accepted forms (``parse_repo_workspace``):
 * ``https://<host>/<path>`` or scp-style ``git@<host>:<path>``. The
 * server is the authority — this only gates the submit button so an
 * obviously unusable value gets inline feedback instead of a 422.
 *
 * The host may not carry an ``@``: that is embedded ``user:token@host``
 * userinfo, which the server refuses so a token never reaches a session
 * label or a sandbox Pod spec. Refusing it here keeps it out of the
 * request as well.
 *
 * @param url Value the user typed in the repository input.
 * @returns true when ``url.trim()`` has an accepted form and repository directory name.
 */
export function isValidSandboxRepoUrl(url: string): boolean {
  const t = url.trim();
  const validForm =
    /^https:\/\/[^\s#/@]+\/+[^\s#@/][^\s#@]*$/.test(t) || /^git@[^\s#:@]+:[^\s#@]+$/.test(t);
  const name = validForm ? deriveRepoName(t) : null;
  return name !== null && name !== "." && name !== ".." && /^[A-Za-z0-9._-]+$/.test(name);
}

// Each composed repository is stored in a label capped by server LABEL_VALUE_MAX_LEN.
export const MAX_SANDBOX_WORKSPACE_LENGTH = 256;
export const SANDBOX_WORKSPACE_LENGTH_MESSAGE = `Repository URL and branch must be at most ${MAX_SANDBOX_WORKSPACE_LENGTH} characters together.`;

/** Count Unicode code points to match the server's limit after input trimming. */
export function isValidSandboxWorkspaceLength(url: string, branch: string): boolean {
  return (
    Array.from(composeSandboxWorkspace(url, branch) ?? "").length <= MAX_SANDBOX_WORKSPACE_LENGTH
  );
}

/** Validate the trimmed branch sent to the server; blank selects the repository default. */
export function isValidSandboxBranch(branch: string): boolean {
  const value = branch.trim();
  if (value === "") return true;
  return (
    !/^[0-9a-f]{40}$/.test(value) &&
    !Array.from(value).some(
      (character) =>
        character.charCodeAt(0) < 0x20 || /\s/.test(character) || "~^:?*[\\#".includes(character),
    ) &&
    !value.startsWith("-") &&
    !value.startsWith("/") &&
    !value.endsWith("/") &&
    !value.endsWith(".") &&
    !value.includes("..") &&
    !value.includes("@{")
  );
}

/**
 * Compose the managed session's ``workspace`` string from the split
 * repository inputs.
 *
 * The API takes one Docker-build-context-style string —
 * ``<url>[#<branch>]`` — and the UI presents split fields, so this is
 * the reassembly step.
 *
 * @param url Repository URL input, e.g. ``"https://github.com/org/repo"``.
 * @param branch Branch input, e.g. ``"main"``; blank means the repo's
 *   default branch.
 * @returns The composed workspace string, or ``undefined`` when no
 *   repository was given (empty sandbox workspace).
 */
export function composeSandboxWorkspace(url: string, branch: string): string | undefined {
  const u = url.trim();
  if (u === "") return undefined;
  const b = branch.trim();
  return b === "" ? u : `${u}#${b}`;
}

/**
 * Split a composed sandbox workspace back into its two inputs.
 *
 * The inverse of {@link composeSandboxWorkspace}: the API carries one
 * ``<url>[#<branch>]`` string, the UI presents a URL field and a branch
 * field. Splits on the FIRST ``#``, matching the server's own parse.
 *
 * @param workspace Composed workspace, e.g.
 *   ``"https://github.com/org/repo#main"``, or ``null`` when unset.
 * @returns The url and branch, each ``""`` when absent.
 */
export function splitSandboxWorkspace(workspace: string | null): {
  url: string;
  branch: string;
} {
  if (workspace === null) return { url: "", branch: "" };
  const hash = workspace.indexOf("#");
  if (hash === -1) return { url: workspace, branch: "" };
  return { url: workspace.slice(0, hash), branch: workspace.slice(hash + 1) };
}

/**
 * Compose the managed session's ``workspaces`` list from the selected repos.
 *
 * Each ``{url, branch}`` becomes a ``<url>[#<branch>]`` string; blank-URL
 * entries are dropped. The API clones them in parallel and starts the agent in
 * the single repo (one entry) or the parent that holds them all (several).
 *
 * @param repos The selected repos, in the order the user added them.
 * @returns The composed workspace strings (may be empty for no repo).
 */
export function composeSandboxWorkspaces(repos: LastSandboxRepo[]): string[] {
  return repos
    .map((r) => composeSandboxWorkspace(r.url, r.branch))
    .filter((w): w is string => w !== undefined);
}

// Max repos a managed sandbox may clone — mirrors the server's
// `_MAX_MANAGED_WORKSPACES` so the picker stops adding before a create 422s.
export const MAX_SANDBOX_REPOS = 10;

/**
 * Derive a repository's display name from its URL.
 *
 * Last path segment with a trailing ``.git`` stripped — the same rule
 * the server uses for the clone directory, so the chip label matches
 * the workspace directory the session will get.
 *
 * @param url Repository URL, e.g. ``"https://github.com/org/repo.git"``.
 * @returns The name, e.g. ``"repo"``; ``null`` when underivable.
 */
export function deriveRepoName(url: string): string | null {
  const t = url.trim().replace(/\/+$/, "");
  if (t === "") return null;
  const last = t.split(/[/:]/).pop() ?? "";
  const name = last.endsWith(".git") ? last.slice(0, -4) : last;
  return name === "" ? null : name;
}
