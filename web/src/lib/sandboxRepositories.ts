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
  if (
    t.includes("?") ||
    Array.from(t).some(
      (character) => character.charCodeAt(0) < 0x20 || character.charCodeAt(0) === 0x7f,
    )
  )
    return false;
  const sshMatch = /^git@(\[[^\s#@[\]]+\]|[^\s#:@/\\[\]]+):([^\s#@]+)$/.exec(t);
  const validForm = /^https:\/\/[^\s#/@]+\/+[^\s#@/][^\s#@]*$/.test(t) || sshMatch !== null;
  const name = validForm ? deriveRepoName(t) : null;
  if (name === null || name === "." || name === ".." || !/^[A-Za-z0-9._-]+$/.test(name))
    return false;
  let hostname: string;
  if (t.startsWith("https://")) {
    try {
      const parsed = new URL(t);
      if (parsed.hostname.startsWith("[")) return true;
    } catch {
      return false;
    }
    const authority = t.slice("https://".length).split("/")[0];
    hostname = authority.split(":")[0];
  } else {
    hostname = sshMatch![1];
    if (hostname.startsWith("[")) {
      try {
        return new URL(`https://${hostname}/`).hostname.startsWith("[");
      } catch {
        return false;
      }
    }
    return true;
  }
  return (
    hostname.length <= 253 &&
    hostname
      .replace(/\.$/, "")
      .split(".")
      .every((label) => /^[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?$/.test(label))
  );
}

// Each composed repository is stored in a label capped by server LABEL_VALUE_MAX_LEN.
export const MAX_SANDBOX_WORKSPACE_LENGTH = 256;
export const SANDBOX_WORKSPACE_LENGTH_MESSAGE = `Repository URL and branch must be at most ${MAX_SANDBOX_WORKSPACE_LENGTH} characters together.`;
export const INVALID_BRANCH_MESSAGE =
  "Enter a valid branch name, or leave blank for the default branch.";

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
        character.charCodeAt(0) < 0x20 ||
        character.charCodeAt(0) === 0x7f ||
        /\s/.test(character) ||
        "~^:?*[\\#".includes(character),
    ) &&
    !value.startsWith("-") &&
    !value.startsWith("/") &&
    !value.endsWith("/") &&
    !value.endsWith(".") &&
    !value.includes("..") &&
    !value.includes("@{") &&
    !value.includes("//") &&
    value !== "@" &&
    value.split("/").every((part) => !part.startsWith(".") && !part.endsWith(".lock"))
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

export function repositoryIdentity(url: string): string {
  const normalized = url
    .trim()
    .replace(/^git@(\[[^\]]+\]|[^:]+):/, "https://$1/")
    .replace(/\/+$/, "");
  const identity = /^https:\/\/github\.com\//i.test(normalized)
    ? normalized.toLowerCase()
    : normalized;
  return identity.replace(/\.git$/, "");
}

/** Validate a saved or drafted choice against the selected target, without dropping it. */
export function repositorySelectionError(
  selection: { url: string; branch?: string },
  capabilities: {
    catalog: { name: string; url: string }[];
    urlInput: boolean;
    branchSelection: boolean;
  },
): string | null {
  const branch = selection.branch ?? "";
  if (branch.trim() !== "" && !capabilities.branchSelection)
    return "This target uses the default branch. Clear the branch or choose another target.";
  if (selection.url.trim() === "")
    return branch.trim() === "" ? null : "Choose a repository for this branch.";
  if (!isValidSandboxRepoUrl(selection.url))
    return "Enter a valid HTTPS or SSH repository URL without credentials.";
  if (!isValidSandboxBranch(branch)) return INVALID_BRANCH_MESSAGE;
  if (!isValidSandboxWorkspaceLength(selection.url, branch))
    return SANDBOX_WORKSPACE_LENGTH_MESSAGE;
  if (
    !capabilities.urlInput &&
    !capabilities.catalog.some(
      (repo) => repositoryIdentity(repo.url) === repositoryIdentity(selection.url),
    )
  ) {
    return "This repository is unavailable on this target. Choose a listed repository or another target.";
  }
  return null;
}
