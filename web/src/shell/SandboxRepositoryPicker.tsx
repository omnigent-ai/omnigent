import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronsUpDownIcon, GitBranchIcon, LockIcon, PlusIcon, XIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { fetchGithubBranches, fetchGithubRepos, type GithubRepo } from "@/lib/githubIntegration";
import type { LastSandboxRepo } from "@/lib/repoPreferences";
import {
  SANDBOX_WORKSPACE_LENGTH_MESSAGE,
  deriveRepoName,
  isValidSandboxBranch,
  isValidSandboxRepoUrl,
  isValidSandboxWorkspaceLength,
} from "@/lib/sandboxRepositories";

const INVALID_BRANCH_MESSAGE = "Enter a valid branch name, or leave blank for the default branch.";

const COMBOBOX_TRIGGER_CLASS =
  "flex w-full items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors hover:border-ring/60 focus-visible:border-ring";

function repoCloneUrl(repo: GithubRepo): string {
  return repo.clone_url ?? `https://github.com/${repo.full_name}.git`;
}

function repoIdentity(url: string): string {
  const normalized = url
    .trim()
    .replace(/^git@([^:]+):/, "https://$1/")
    .replace(/\/+$/, "");
  const identity = /^https:\/\/github\.com\//i.test(normalized)
    ? normalized.toLowerCase()
    : normalized;
  return identity.replace(/\.git$/, "");
}

function SandboxRepoCombobox({
  repos,
  onSelect,
  testIdPrefix,
}: {
  repos: GithubRepo[];
  onSelect: (repo: GithubRepo) => void;
  testIdPrefix: string;
}): ReactNode {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          role="combobox"
          aria-expanded={open}
          aria-label="GitHub repository"
          className={COMBOBOX_TRIGGER_CLASS}
          data-testid={`${testIdPrefix}-select`}
        >
          <span className="truncate">Choose a repository…</span>
          <ChevronsUpDownIcon className="ml-auto size-3.5 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-72 p-0">
        <Command>
          <CommandInput placeholder="Search repositories…" data-testid={`${testIdPrefix}-search`} />
          <CommandList>
            <CommandEmpty>No repositories found.</CommandEmpty>
            <CommandGroup>
              {repos.map((repo) => (
                <CommandItem
                  key={repo.full_name}
                  value={repo.full_name}
                  onSelect={() => {
                    onSelect(repo);
                    setOpen(false);
                  }}
                >
                  <span className="truncate">{repo.full_name}</span>
                  {repo.private && (
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

function SandboxRepoBranchSelect({
  repo,
  url,
  value,
  onChange,
  enabled,
  testIdPrefix,
  descriptionId,
  invalid,
}: {
  repo: GithubRepo;
  url: string;
  value: string;
  onChange: (branch: string) => void;
  enabled: boolean;
  testIdPrefix: string;
  descriptionId?: string;
  invalid: boolean;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const { data, isPending, isError } = useQuery({
    queryKey: ["github-branches", repo.full_name],
    queryFn: () => fetchGithubBranches(repo.full_name),
    enabled: enabled && open,
    staleTime: 5 * 60_000,
  });
  const branches = data?.connected ? data.branches : [];
  const defaultLabel = repo.default_branch ? `Default (${repo.default_branch})` : "Default branch";
  const options = branches.filter((branch) => branch !== repo.default_branch);
  if (value !== "" && !options.includes(value)) options.unshift(value);
  const customBranch = search.trim();
  const invalidSearch = !isValidSandboxBranch(search);
  const searchTooLong = !isValidSandboxWorkspaceLength(url, search);
  const showCustomBranch =
    customBranch !== "" &&
    !invalidSearch &&
    !searchTooLong &&
    customBranch !== repo.default_branch &&
    !options.includes(customBranch);
  const chooseBranch = (branch: string): void => {
    onChange(branch);
    setOpen(false);
  };
  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (nextOpen) setSearch("");
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          role="combobox"
          aria-expanded={open}
          aria-label={`Branch for ${repo.full_name}`}
          aria-describedby={descriptionId}
          aria-invalid={invalid || undefined}
          className={COMBOBOX_TRIGGER_CLASS}
          data-testid={`${testIdPrefix}-branch-select`}
          title={value || defaultLabel}
        >
          <GitBranchIcon className="size-3.5 shrink-0 opacity-60" />
          <span className="truncate">{value === "" ? defaultLabel : value}</span>
          <ChevronsUpDownIcon className="ml-auto size-3.5 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-72 p-0">
        <Command>
          <CommandInput
            placeholder="Search or enter a branch…"
            value={search}
            onValueChange={setSearch}
            data-testid={`${testIdPrefix}-branch-search`}
          />
          {invalidSearch && (
            <p className="px-3 py-2 text-xs text-destructive">{INVALID_BRANCH_MESSAGE}</p>
          )}
          {searchTooLong && (
            <p className="px-3 py-2 text-xs text-destructive">{SANDBOX_WORKSPACE_LENGTH_MESSAGE}</p>
          )}
          {isError && (
            <p className="px-3 py-2 text-xs text-muted-foreground">
              Couldn't load branches. Enter a branch name to use it.
            </p>
          )}
          <CommandList>
            <CommandEmpty>{isPending ? "Loading branches…" : "No branches found."}</CommandEmpty>
            <CommandGroup>
              <CommandItem
                value={defaultLabel}
                data-checked={value === ""}
                onSelect={() => chooseBranch("")}
              >
                {defaultLabel}
              </CommandItem>
              {options.map((branch) => (
                <CommandItem
                  key={branch}
                  value={branch}
                  data-checked={branch === value}
                  disabled={
                    !isValidSandboxBranch(branch) || !isValidSandboxWorkspaceLength(url, branch)
                  }
                  title={
                    !isValidSandboxWorkspaceLength(url, branch)
                      ? SANDBOX_WORKSPACE_LENGTH_MESSAGE
                      : undefined
                  }
                  onSelect={() => chooseBranch(branch)}
                >
                  {branch === repo.default_branch ? `${branch} (pinned)` : branch}
                </CommandItem>
              ))}
              {showCustomBranch && (
                <CommandItem
                  value={`Use ${customBranch}`}
                  onSelect={() => chooseBranch(customBranch)}
                >
                  Use “{customBranch}”
                </CommandItem>
              )}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

interface SandboxRepositoryPickerProps {
  selections: LastSandboxRepo[];
  onChange: (selections: LastSandboxRepo[], editedBranchUrl?: string) => void;
  branchNotes?: Readonly<Record<string, string>>;
  maxRepos: number;
  enabled?: boolean;
  testIdPrefix?: string;
  pendingUrl?: string;
  onPendingUrlChange?: (url: string) => void;
}

/** Shared repository and branch selection for starting and cloning sandbox sessions. */
export function SandboxRepositoryPicker({
  selections,
  onChange,
  branchNotes,
  maxRepos,
  enabled = true,
  testIdPrefix = "new-chat-landing-repo",
  pendingUrl,
  onPendingUrlChange,
}: SandboxRepositoryPickerProps): ReactNode {
  const info = useServerInfo();
  const [localPendingUrl, setLocalPendingUrl] = useState("");
  const inputUrl = pendingUrl ?? localPendingUrl;
  const updatePendingUrl = (url: string): void => {
    setLocalPendingUrl(url);
    onPendingUrlChange?.(url);
  };
  const githubEnabled =
    enabled && info !== "loading" && (info.enabled_connections ?? []).includes("github");
  const { data, isError } = useQuery({
    queryKey: ["github-repos"],
    queryFn: fetchGithubRepos,
    enabled: githubEnabled,
    staleTime: 5 * 60_000,
  });
  const connected = githubEnabled && data?.connected;
  const repos = connected ? data.repos : [];
  const selectedUrls = new Set(selections.map((selection) => repoIdentity(selection.url)));
  const unselectedRepos = repos.filter(
    (repo) => !selectedUrls.has(repoIdentity(repoCloneUrl(repo))),
  );
  const duplicateUrl = inputUrl.trim() !== "" && selectedUrls.has(repoIdentity(inputUrl));
  const urlError = (() => {
    if (inputUrl.trim() === "") return null;
    if (duplicateUrl) return "This repository is already selected. Clear this URL to continue.";
    if (!isValidSandboxWorkspaceLength(inputUrl, "")) return SANDBOX_WORKSPACE_LENGTH_MESSAGE;
    if (!isValidSandboxRepoUrl(inputUrl))
      return "Enter an HTTPS or SSH URL ending in a repository name.";
    return null;
  })();
  const canAdd = inputUrl.trim() !== "" && urlError === null && selections.length < maxRepos;
  const addRepo = (url: string): void => {
    if (
      selections.length >= maxRepos ||
      selectedUrls.has(repoIdentity(url)) ||
      !isValidSandboxRepoUrl(url) ||
      !isValidSandboxWorkspaceLength(url, "")
    )
      return;
    onChange([...selections, { url: url.trim(), branch: "" }]);
  };
  const addInput = (): void => {
    if (!canAdd) return;
    addRepo(inputUrl);
    updatePendingUrl("");
  };
  const setBranch = (index: number, branch: string): void => {
    onChange(
      selections.map((selection, row) => (row === index ? { ...selection, branch } : selection)),
      selections[index]?.url,
    );
  };
  const repositoryOccurrences = new Map<string, number>();
  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid={`${testIdPrefix}-picker`}>
      {selections.length > maxRepos && (
        <p className="text-sm text-warning" role="alert" data-testid={`${testIdPrefix}-overcap`}>
          This sandbox provider clones at most {maxRepos}{" "}
          {maxRepos === 1 ? "repository" : "repositories"}. Remove the extra repositories to
          continue.
        </p>
      )}
      {selections.map((selection, index) => {
        const occurrence = repositoryOccurrences.get(selection.url) ?? 0;
        repositoryOccurrences.set(selection.url, occurrence + 1);
        const repo = repos.find(
          (item) => repoIdentity(repoCloneUrl(item)) === repoIdentity(selection.url),
        );
        const name = repo?.full_name ?? deriveRepoName(selection.url) ?? selection.url;
        const invalidBranch = !isValidSandboxBranch(selection.branch);
        const workspaceTooLong = !isValidSandboxWorkspaceLength(selection.url, selection.branch);
        const invalid = invalidBranch || workspaceTooLong;
        const note = (() => {
          if (invalidBranch) return INVALID_BRANCH_MESSAGE;
          if (workspaceTooLong) return SANDBOX_WORKSPACE_LENGTH_MESSAGE;
          return branchNotes?.[selection.url];
        })();
        const descriptionId = note ? `${testIdPrefix}-branch-note-${index}` : undefined;
        return (
          <div
            key={`${selection.url}:${occurrence}`}
            className="min-w-0"
            data-testid={`${testIdPrefix}-row`}
          >
            <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 gap-y-1.5 py-1 sm:flex sm:gap-2 sm:py-0">
              <span
                className="col-start-1 row-start-1 min-w-0 flex-1 truncate text-sm"
                title={selection.url}
              >
                {name}
              </span>
              <div className="col-span-2 row-start-2 min-w-0 sm:w-40 sm:max-w-[55%] sm:shrink-0">
                {repo ? (
                  <SandboxRepoBranchSelect
                    repo={repo}
                    url={selection.url}
                    value={selection.branch}
                    onChange={(branch) => setBranch(index, branch)}
                    enabled={githubEnabled}
                    testIdPrefix={testIdPrefix}
                    descriptionId={descriptionId}
                    invalid={invalid}
                  />
                ) : (
                  <input
                    type="text"
                    value={selection.branch}
                    onChange={(event) => setBranch(index, event.target.value)}
                    placeholder="Default branch"
                    aria-label={`Branch for ${name}`}
                    aria-describedby={descriptionId}
                    aria-invalid={invalid || undefined}
                    className="w-full rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
                  />
                )}
              </div>
              <button
                type="button"
                onClick={() => onChange(selections.filter((_, row) => row !== index))}
                aria-label={`Remove ${name}`}
                className="col-start-2 row-start-1 shrink-0 justify-self-end rounded-sm p-1 text-muted-foreground transition-colors hover:text-foreground"
                data-testid={`${testIdPrefix}-remove`}
              >
                <XIcon className="size-3.5" />
              </button>
            </div>
            {note && (
              <p
                id={descriptionId}
                className={`mt-1 text-xs break-words ${invalid ? "text-destructive" : "text-muted-foreground"}`}
              >
                {note}
              </p>
            )}
          </div>
        );
      })}
      {(selections.length < maxRepos || inputUrl.trim() !== "") && (
        <>
          {selections.length > 0 && <div className="my-0.5 border-t border-border" />}
          {connected && selections.length < maxRepos && (
            <>
              <SandboxRepoCombobox
                repos={unselectedRepos}
                onSelect={(repo) => addRepo(repoCloneUrl(repo))}
                testIdPrefix={testIdPrefix}
              />
              {data.truncated && (
                <p
                  className="text-xs text-muted-foreground"
                  data-testid={`${testIdPrefix}-truncated`}
                >
                  Showing your most recently pushed repositories. Don't see one? Paste its URL
                  below.
                </p>
              )}
              <p className="text-xs text-muted-foreground">or paste a repository URL:</p>
            </>
          )}
          {githubEnabled && isError && !connected && (
            <p className="text-sm text-destructive" data-testid={`${testIdPrefix}-error`}>
              Couldn't load your GitHub repositories. Paste a repository URL below.
            </p>
          )}
          <div className="flex items-center gap-2">
            <input
              id={`${testIdPrefix}-input`}
              type="text"
              value={inputUrl}
              onChange={(event) => updatePendingUrl(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  addInput();
                }
              }}
              placeholder="https://github.com/org/repo"
              aria-label="Repository URL"
              aria-invalid={urlError !== null}
              aria-describedby={urlError ? `${testIdPrefix}-input-error` : undefined}
              className="min-w-0 flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus-visible:border-ring"
              data-testid={`${testIdPrefix}-input`}
            />
            <button
              type="button"
              disabled={!canAdd}
              onClick={addInput}
              className="flex shrink-0 items-center gap-1 rounded-md border border-input px-2.5 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground disabled:opacity-40"
              data-testid={`${testIdPrefix}-add`}
            >
              <PlusIcon className="size-3.5" />
              Add
            </button>
          </div>
          {urlError && (
            <p id={`${testIdPrefix}-input-error`} className="text-xs text-destructive">
              {urlError}
            </p>
          )}
        </>
      )}
    </div>
  );
}
