import {
  useCallback,
  useId,
  useState,
  type Dispatch,
  type SetStateAction,
  type ReactNode,
} from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronsUpDownIcon,
  CircleHelpIcon,
  GitBranchIcon,
  LockIcon,
  PlusIcon,
  XIcon,
} from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { fetchGithubBranches, fetchGithubRepos, type GithubRepo } from "@/lib/githubIntegration";
import type { LastSandboxRepo } from "@/lib/repoPreferences";
import { cn } from "@/lib/utils";

/**
 * Session label recording the repository a managed session was created
 * with, as the raw ``<url>[#<branch>]`` request value (the server's
 * ``MANAGED_REPO_LABEL_KEY``). A sandbox relaunch re-clones from it, and
 * the fork dialog seeds its repository field from it so cloning a sandbox
 * session lands in the same checkout — which is why it lives beside the
 * workspace grammar below rather than with the server-capability probe.
 */
export const SANDBOX_REPO_LABEL_KEY = "omnigent.sandbox.repo";

export function sandboxReposFromLabels(labels: Record<string, string>): LastSandboxRepo[] {
  const indexed = Object.entries(labels)
    .filter(
      ([key]) =>
        key.startsWith(`${SANDBOX_REPO_LABEL_KEY}.`) &&
        /^\d+$/.test(key.slice(SANDBOX_REPO_LABEL_KEY.length + 1)),
    )
    .sort(([left], [right]) => Number(left.split(".").at(-1)) - Number(right.split(".").at(-1)));
  const values = indexed.length
    ? indexed.map(([, value]) => value)
    : (labels[SANDBOX_REPO_LABEL_KEY] ?? "").split(/\s+/).filter(Boolean);
  return values.map(splitSandboxWorkspace);
}

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
 * @returns true when ``url.trim()`` matches one of the two forms.
 */
export function isValidSandboxRepoUrl(url: string): boolean {
  const t = url.trim();
  return /^https:\/\/[^\s#/@]+\/+[^\s#@/][^\s#@]*$/.test(t) || /^git@[^\s#:@]+:[^\s#@]+$/.test(t);
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

/** Shared trigger styling for the repo/branch comboboxes. */
const COMBOBOX_TRIGGER_CLASS =
  "flex w-full items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors hover:border-ring/60 focus-visible:border-ring";

/**
 * Searchable combobox for picking one of the caller's GitHub repos.
 *
 * A trigger button opens a `cmdk` search list (repos are filtered as you
 * type on ``owner/name``), which scales to accounts with many repos far
 * better than a native ``<select>``. Selecting a repo fills the same
 * URL/branch state the free-text inputs drive; the empty value shows the
 * "Choose a repository…" placeholder.
 *
 * @param repos The caller's accessible repos (newest-first from the API).
 * @param value The selected repo's ``owner/name`` ("" = none).
 * @param onSelect Called with the chosen repo (or ``null`` to clear).
 */
function SandboxRepoCombobox({
  repos,
  value,
  onSelect,
}: {
  repos: GithubRepo[];
  value: string;
  onSelect: (repo: GithubRepo | null) => void;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const selected = repos.find((r) => r.full_name === value) ?? null;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          role="combobox"
          aria-expanded={open}
          aria-label="GitHub repository"
          className={COMBOBOX_TRIGGER_CLASS}
          data-testid="new-chat-landing-repo-select"
        >
          <span className="truncate">{selected ? selected.full_name : "Choose a repository…"}</span>
          <ChevronsUpDownIcon className="ml-auto size-3.5 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-72 p-0">
        <Command>
          <CommandInput
            placeholder="Search repositories…"
            data-testid="new-chat-landing-repo-search"
          />
          <CommandList>
            <CommandEmpty>No repositories found.</CommandEmpty>
            <CommandGroup>
              {repos.map((r) => (
                <CommandItem
                  key={r.full_name}
                  value={r.full_name}
                  data-checked={r.full_name === value}
                  onSelect={() => {
                    onSelect(r);
                    setOpen(false);
                  }}
                >
                  <span className="truncate">{r.full_name}</span>
                  {r.private && (
                    <LockIcon
                      className="size-3 shrink-0 opacity-60"
                      aria-label="Private repository"
                    />
                  )}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

/**
 * Searchable branch combobox for the connected-GitHub repo picker.
 *
 * Lazily fetches the chosen repo's branches and filters them as you type.
 * The empty value is the "default branch" sentinel: leaving it selected
 * appends no ``#branch`` fragment, so the server clones the repo's default
 * branch.
 *
 * @param fullName The chosen repo's ``owner/name``.
 * @param value The currently selected branch ("" = default).
 * @param defaultBranch The repo's default branch, for the sentinel label.
 * @param onChange Called with the newly selected branch.
 */
function SandboxRepoBranchSelect({
  fullName,
  value,
  defaultBranch,
  onChange,
  allowCustom = false,
  unselectedLabel,
}: {
  fullName: string;
  value: string;
  defaultBranch: string | null;
  onChange: (branch: string) => void;
  allowCustom?: boolean;
  unselectedLabel?: string;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const typedBranch = search.trim();
  const { data, isPending } = useQuery({
    queryKey: ["github-branches", fullName],
    queryFn: () => fetchGithubBranches(fullName),
    staleTime: 5 * 60_000,
  });
  const branches = data?.connected ? data.branches : [];
  const defaultLabel =
    unselectedLabel ?? (defaultBranch ? `Default (${defaultBranch})` : "Default branch");
  // Options: the fetched branches minus the default (represented by the
  // empty-value sentinel). Include the current value even before the list
  // loads (e.g. a draft-restored branch) so the trigger label always
  // resolves to a real option.
  const options = branches.filter((b) => allowCustom || b !== defaultBranch);
  if (value !== "" && !options.includes(value)) {
    options.unshift(value);
  }
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setSearch("");
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          role="combobox"
          aria-expanded={open}
          aria-label={`Branch for ${fullName}`}
          className={cn(COMBOBOX_TRIGGER_CLASS, "h-8 gap-1.5 px-2 text-xs")}
          data-testid="new-chat-landing-repo-branch-select"
        >
          <GitBranchIcon className="size-3.5 shrink-0 opacity-60" />
          <span className="truncate" title={value || defaultLabel}>
            {value === "" ? defaultLabel : value}
          </span>
          <ChevronsUpDownIcon className="ml-auto size-3.5 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-72 p-0">
        <Command>
          <CommandInput
            placeholder={allowCustom ? "Search or enter a branch…" : "Search branches…"}
            value={search}
            onValueChange={setSearch}
            data-testid="new-chat-landing-repo-branch-search"
          />
          <CommandList>
            <CommandEmpty>{isPending ? "Loading branches…" : "No branches found."}</CommandEmpty>
            <CommandGroup>
              <CommandItem
                value={defaultLabel}
                data-checked={value === ""}
                onSelect={() => {
                  onChange("");
                  setOpen(false);
                }}
              >
                {defaultLabel}
              </CommandItem>
              {options.map((b) => (
                <CommandItem
                  key={b}
                  value={b}
                  data-checked={b === value}
                  onSelect={() => {
                    onChange(b);
                    setOpen(false);
                  }}
                >
                  {b}
                </CommandItem>
              ))}
              {allowCustom && typedBranch && !options.includes(typedBranch) && (
                <CommandItem
                  value={`Use ${typedBranch}`}
                  onSelect={() => {
                    onChange(typedBranch);
                    setOpen(false);
                    setSearch("");
                  }}
                >
                  <PlusIcon className="size-3.5" />
                  Use “{typedBranch}”
                </CommandItem>
              )}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

export function SandboxRepositoryPicker({
  sandboxRepoSelections,
  setSandboxRepoSelections,
  sandboxCreateWorktrees,
  setSandboxCreateWorktrees,
  sandboxSessionWorkspaces,
  maxSandboxRepos,
  githubReposEnabled,
}: {
  sandboxRepoSelections: LastSandboxRepo[];
  setSandboxRepoSelections: Dispatch<SetStateAction<LastSandboxRepo[]>>;
  sandboxCreateWorktrees: boolean;
  setSandboxCreateWorktrees: (value: boolean) => void;
  sandboxSessionWorkspaces: boolean;
  maxSandboxRepos: number;
  githubReposEnabled: boolean;
}) {
  const worktreesId = useId();
  const sandboxRepoOverCap = sandboxRepoSelections.length > maxSandboxRepos;
  // Append a repo (deduped by URL — adding one already picked is a no-op),
  // remove one, or repoint its branch. Order is preserved so the list reads
  // the way the user built it.
  const addSandboxRepo = useCallback(
    (url: string, branch = ""): void => {
      const u = url.trim();
      if (u === "") return;
      setSandboxRepoSelections((prev) =>
        // Cap at the provider's limit so a selection can't only fail with a 422
        // at create; a duplicate URL is a no-op.
        prev.some((r) => r.url === u) || prev.length >= maxSandboxRepos
          ? prev
          : [...prev, { url: u, branch: branch.trim() }],
      );
    },
    [maxSandboxRepos, setSandboxRepoSelections],
  );
  const removeSandboxRepo = useCallback(
    (url: string): void => {
      setSandboxRepoSelections((prev) => prev.filter((r) => r.url !== url));
    },
    [setSandboxRepoSelections],
  );
  const setSandboxRepoBranch = useCallback(
    (url: string, branch: string): void => {
      setSandboxRepoSelections((prev) => prev.map((r) => (r.url === url ? { ...r, branch } : r)));
    },
    [setSandboxRepoSelections],
  );
  // Free-text URL being typed into the "paste a URL" adder (not yet added).
  const [pendingRepoUrl, setPendingRepoUrl] = useState<string>("");
  const { data: sandboxRepoData, isError: sandboxReposErrored } = useQuery({
    queryKey: ["github-repos"],
    queryFn: fetchGithubRepos,
    enabled: githubReposEnabled,
    staleTime: 5 * 60_000,
  });
  const sandboxRepoPickerConnected = sandboxRepoData?.connected ?? false;
  const sandboxRepos = sandboxRepoPickerConnected ? (sandboxRepoData?.repos ?? []) : [];
  const sandboxReposTruncated = sandboxRepoData?.truncated ?? false;
  const showGithubRepoPicker = githubReposEnabled && sandboxRepoPickerConnected;
  // The connected-GitHub repo (if any) a selection URL names, so its row can
  // offer that repo's branch list. Repos not in the picker (pasted URLs, or a
  // repo the account lost access to) resolve to undefined and fall back to a
  // free-text branch input.
  const repoForUrl = (url: string): GithubRepo | undefined =>
    sandboxRepos.find(
      (r) => (r.clone_url ?? `https://github.com/${r.full_name}.git`) === url.trim(),
    );
  // The clone URL of a connected repo, matching the server's derivation.
  const repoCloneUrl = (r: GithubRepo): string =>
    r.clone_url ?? `https://github.com/${r.full_name}.git`;
  // Repos not yet selected — the "Add repository" combobox offers only these.
  const unselectedRepos = sandboxRepos.filter(
    (r) => !sandboxRepoSelections.some((s) => s.url === repoCloneUrl(r)),
  );
  return (
    <div className="flex flex-col gap-2">
      {/* Stale over-cap selection (repos remembered/added under
                  a multi-repo provider, then switched to a single-repo one):
                  warn and block submit rather than 422 after the session row
                  is created. */}
      {sandboxRepoOverCap && (
        <p className="text-sm text-warning" data-testid="new-chat-landing-repo-overcap">
          This sandbox provider clones at most {maxSandboxRepos}{" "}
          {maxSandboxRepos === 1 ? "repository" : "repositories"}. Remove the extra{" "}
          {maxSandboxRepos === 1 ? "repositories" : "ones"} to continue.
        </p>
      )}
      {/* Add-repository controls, hidden once the provider's
                  repo cap is reached — so a single-repo provider shows one
                  slot and no multi-repo affordance. */}
      {sandboxRepoSelections.length < maxSandboxRepos && (
        <>
          {/* Add from the connected account's repos (only those
                      not already picked); the free-text URL below is the
                      fallback for a repo not in the list or no GitHub link. */}
          {showGithubRepoPicker && (
            <>
              <SandboxRepoCombobox
                repos={unselectedRepos}
                value=""
                onSelect={(repo) => {
                  if (repo) {
                    addSandboxRepo(repo.clone_url ?? `https://github.com/${repo.full_name}.git`);
                  }
                }}
              />
              {sandboxReposTruncated && (
                <p
                  className="text-sm text-muted-foreground"
                  data-testid="new-chat-landing-repo-truncated"
                >
                  Showing your most recently pushed repositories. Don't see one? Paste its URL
                  below.
                </p>
              )}
              <p className="text-sm text-muted-foreground">or paste a repository URL:</p>
            </>
          )}
          {/* Connected but the repo list failed to load: say so
                      explicitly, so a transient error isn't mistaken for
                      "GitHub not connected" (the picker just wouldn't render). */}
          {githubReposEnabled && sandboxReposErrored && !showGithubRepoPicker && (
            <p className="text-sm text-destructive" data-testid="new-chat-landing-repo-error">
              Couldn't load your GitHub repositories. Paste a repository URL below.
            </p>
          )}
          <div className="flex items-center gap-2">
            <input
              id="landing-repo-url"
              type="text"
              value={pendingRepoUrl}
              onChange={(e) => setPendingRepoUrl(e.target.value)}
              onKeyDown={(e) => {
                // Enter adds the repo (same as the Add button), so a
                // paste-then-Enter flow never needs the mouse.
                if (e.key === "Enter" && isValidSandboxRepoUrl(pendingRepoUrl)) {
                  e.preventDefault();
                  addSandboxRepo(pendingRepoUrl);
                  setPendingRepoUrl("");
                }
              }}
              placeholder="https://github.com/org/repo"
              aria-label="Repository URL"
              className="min-w-0 flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus-visible:border-ring"
              data-testid="new-chat-landing-repo-input"
            />
            <button
              type="button"
              disabled={!isValidSandboxRepoUrl(pendingRepoUrl)}
              onClick={() => {
                addSandboxRepo(pendingRepoUrl);
                setPendingRepoUrl("");
              }}
              className="flex shrink-0 items-center gap-1 rounded-md border border-input px-2.5 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground disabled:opacity-40"
              data-testid="new-chat-landing-repo-add"
            >
              <PlusIcon className="size-3.5" />
              Add
            </button>
          </div>
        </>
      )}
      {sandboxRepoSelections.length > 0 && sandboxRepoSelections.length < maxSandboxRepos && (
        <div className="my-0.5 border-t border-border" />
      )}
      {/* Selected repos: each clones into its own sibling dir.
                  A connected repo gets its branch combobox; a pasted URL a
                  free-text branch. The remove button drops it. */}
      {sandboxRepoSelections.map((sel) => {
        const repo = repoForUrl(sel.url);
        const name = repo?.full_name ?? deriveRepoName(sel.url) ?? sel.url;
        return (
          <div
            key={sel.url}
            className="flex items-center gap-2"
            data-testid="new-chat-landing-repo-row"
          >
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm" title={sel.url}>
                {name}
              </div>
              {sandboxSessionWorkspaces && (
                <div className="text-xs text-muted-foreground">
                  {sel.branch || sandboxCreateWorktrees ? "Separate worktree" : "Shared files"}
                </div>
              )}
            </div>
            {repo ? (
              <div className="w-36 shrink-0">
                <SandboxRepoBranchSelect
                  fullName={repo.full_name}
                  value={sel.branch}
                  defaultBranch={repo.default_branch}
                  allowCustom={sandboxSessionWorkspaces}
                  unselectedLabel={
                    sandboxSessionWorkspaces
                      ? sandboxCreateWorktrees
                        ? "New worktree"
                        : "Shared folder"
                      : undefined
                  }
                  onChange={(b) => setSandboxRepoBranch(sel.url, b)}
                />
              </div>
            ) : (
              <input
                type="text"
                value={sel.branch}
                onChange={(e) => setSandboxRepoBranch(sel.url, e.target.value)}
                placeholder={
                  sandboxSessionWorkspaces
                    ? sandboxCreateWorktrees
                      ? "New worktree"
                      : "Shared folder"
                    : "branch"
                }
                aria-label={`Branch for ${name}`}
                className="w-28 shrink-0 rounded-md border border-input bg-background px-2 py-1 text-xs outline-none transition-colors focus-visible:border-ring"
              />
            )}
            <button
              type="button"
              onClick={() => removeSandboxRepo(sel.url)}
              aria-label={`Remove ${name}`}
              className="shrink-0 rounded-sm p-1 text-muted-foreground transition-colors hover:text-foreground"
              data-testid="new-chat-landing-repo-remove"
            >
              <XIcon className="size-3.5" />
            </button>
          </div>
        );
      })}
      {sandboxSessionWorkspaces ? (
        <div className="mt-1 border-t border-border pt-3">
          <div className="flex items-center gap-2">
            <Checkbox
              id={worktreesId}
              checked={sandboxCreateWorktrees}
              onCheckedChange={(checked) => setSandboxCreateWorktrees(checked === true)}
            />
            <label htmlFor={worktreesId} className="cursor-pointer text-sm">
              Create worktrees
            </label>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  aria-label="About sandbox worktrees"
                  className="text-muted-foreground"
                >
                  <CircleHelpIcon className="size-3.5" />
                </button>
              </TooltipTrigger>
              <TooltipContent className="max-w-72">
                Off: reuse each repository's shared checkout. On: request a new branch and worktree.
                A chosen branch always uses a worktree. Existing clones are reused.
              </TooltipContent>
            </Tooltip>
          </div>
          <p className="mt-1 pl-6 text-xs text-muted-foreground">For repos without a branch</p>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          {maxSandboxRepos > 1
            ? "Cloned into the sandbox at startup. Several repos are cloned side by side and the agent starts in the parent that holds them; pick one and it starts directly inside it. Leave empty for a blank workspace."
            : "Cloned into the sandbox at startup as the working directory. Leave empty for a blank workspace."}
        </p>
      )}
    </div>
  );
}
