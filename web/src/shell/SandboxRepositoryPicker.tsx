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
import { cn } from "@/lib/utils";
import { fetchGithubBranches, fetchGithubRepos, type GithubRepo } from "@/lib/githubIntegration";
import type { LastSandboxRepo } from "@/lib/repoPreferences";
import {
  INVALID_BRANCH_MESSAGE,
  SANDBOX_WORKSPACE_LENGTH_MESSAGE,
  deriveRepoName,
  repositoryIdentity,
  repositorySelectionError,
  isValidSandboxBranch,
  isValidSandboxWorkspaceLength,
} from "@/lib/sandboxRepositories";

interface RepositorySuggestion {
  name: string;
  url: string;
  private?: boolean;
}
const EMPTY_CATALOG: RepositorySuggestion[] = [];

const COMBOBOX_TRIGGER_CLASS =
  "flex w-full items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors hover:border-ring/60 focus-visible:border-ring";

function repoCloneUrl(repo: GithubRepo): string {
  return repo.clone_url ?? `https://github.com/${repo.full_name}.git`;
}

function SandboxRepoCombobox({
  repos,
  onSelect,
  testIdPrefix,
}: {
  repos: RepositorySuggestion[];
  onSelect: (repo: RepositorySuggestion) => void;
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
          aria-label="Repository"
          className={cn("mobile-touch-target", COMBOBOX_TRIGGER_CLASS)}
          data-testid={`${testIdPrefix}-select`}
        >
          <span className="truncate">Choose a repository…</span>
          <ChevronsUpDownIcon className="ml-auto size-3.5 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-72 p-0">
        <Command className="mobile-app:[&_[data-slot=input-group]]:min-h-[44px]">
          <CommandInput
            placeholder="Search repositories…"
            className="mobile-touch-target"
            data-testid={`${testIdPrefix}-search`}
          />
          <CommandList>
            <CommandEmpty>No repositories found.</CommandEmpty>
            <CommandGroup>
              {repos.map((repo) => (
                <CommandItem
                  key={repositoryIdentity(repo.url)}
                  className="mobile-touch-target"
                  value={repositoryIdentity(repo.url)}
                  keywords={[repo.name]}
                  onSelect={() => {
                    onSelect(repo);
                    setOpen(false);
                  }}
                >
                  <span className="truncate">{repo.name}</span>
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
  single?: boolean;
  catalog?: { name: string; url: string }[];
  urlInput?: boolean;
  branchSelection?: boolean;
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
  single = false,
  catalog = EMPTY_CATALOG,
  urlInput = true,
  branchSelection = true,
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
    enabled &&
    urlInput &&
    info !== "loading" &&
    (info.enabled_connections ?? []).includes("github");
  const { data, isError } = useQuery({
    queryKey: ["github-repos"],
    queryFn: fetchGithubRepos,
    enabled: githubEnabled,
    staleTime: 5 * 60_000,
  });
  const connected = githubEnabled && data?.connected;
  const repos = connected ? data.repos : [];
  const suggestions = [
    ...new Map(
      [
        ...catalog,
        ...repos.map((repo) => ({
          name: repo.full_name,
          url: repoCloneUrl(repo),
          private: repo.private,
        })),
      ].map((repo) => [repositoryIdentity(repo.url), repo]),
    ).values(),
  ];
  const selectedUrls = new Set(selections.map((selection) => repositoryIdentity(selection.url)));
  const unselectedRepos = suggestions.filter(
    (repo) => !selectedUrls.has(repositoryIdentity(repo.url)),
  );
  const duplicateUrl = inputUrl.trim() !== "" && selectedUrls.has(repositoryIdentity(inputUrl));
  const urlError = (() => {
    if (inputUrl.trim() === "") return null;
    if (duplicateUrl) return "This repository is already selected. Clear this URL to continue.";
    return repositorySelectionError(
      { url: inputUrl, branch: "" },
      { catalog, urlInput, branchSelection },
    );
  })();
  const canAdd = inputUrl.trim() !== "" && urlError === null && selections.length < maxRepos;
  const addRepo = (url: string): void => {
    if (
      selections.length >= maxRepos ||
      selectedUrls.has(repositoryIdentity(url)) ||
      repositorySelectionError({ url, branch: "" }, { catalog, urlInput, branchSelection }) !== null
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
  if (single) {
    const selection = selections[0] ?? { url: "", branch: "" };
    const updateSelection = (next: LastSandboxRepo): void =>
      onChange(next.url.trim() === "" && next.branch.trim() === "" ? [] : [next]);
    const error = repositorySelectionError(selection, { catalog, urlInput, branchSelection });
    const branchError =
      error !== null &&
      selection.branch.trim() !== "" &&
      (!branchSelection ||
        (selection.url.trim() !== "" &&
          error !==
            repositorySelectionError(
              { ...selection, branch: "" },
              { catalog, urlInput, branchSelection },
            )));
    const selectedSuggestion = suggestions.find(
      (repo) => repositoryIdentity(repo.url) === repositoryIdentity(selection.url),
    );
    return (
      <div className="flex min-w-0 flex-col gap-2" data-testid={`${testIdPrefix}-picker`}>
        <label htmlFor={`${testIdPrefix}-input`} className="text-sm font-medium">
          Repository (optional)
        </label>
        {urlInput ? (
          <>
            <input
              id={`${testIdPrefix}-input`}
              list={`${testIdPrefix}-suggestions`}
              value={selection.url}
              onChange={(event) => updateSelection({ ...selection, url: event.target.value })}
              aria-label="Repository URL"
              aria-invalid={error !== null && !branchError}
              aria-describedby={error && !branchError ? `${testIdPrefix}-error` : undefined}
              placeholder="Select a repository or paste a URL"
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
            <datalist id={`${testIdPrefix}-suggestions`}>
              {suggestions.map((repo) => (
                <option key={repositoryIdentity(repo.url)} value={repo.url}>
                  {repo.name}
                </option>
              ))}
            </datalist>
          </>
        ) : (
          <select
            id={`${testIdPrefix}-input`}
            value={selectedSuggestion?.url ?? selection.url}
            onChange={(event) => updateSelection({ ...selection, url: event.target.value })}
            aria-invalid={error !== null && !branchError}
            aria-describedby={error && !branchError ? `${testIdPrefix}-error` : undefined}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
          >
            <option value="">No repository</option>
            {selection.url !== "" && !selectedSuggestion && (
              <option value={selection.url}>{selection.url} (unavailable)</option>
            )}
            {suggestions.map((repo) => (
              <option key={repositoryIdentity(repo.url)} value={repo.url}>
                {repo.name}
              </option>
            ))}
          </select>
        )}
        {githubEnabled && isError && (
          <p className="text-xs text-muted-foreground">
            Couldn't load repositories. You can still paste a URL.
          </p>
        )}
        {((selection.url.trim() !== "" && branchSelection) || selection.branch !== "") && (
          <>
            <label htmlFor={`${testIdPrefix}-branch`} className="text-sm font-medium">
              Branch (optional)
            </label>
            <input
              id={`${testIdPrefix}-branch`}
              value={selection.branch}
              onChange={(event) => updateSelection({ ...selection, branch: event.target.value })}
              aria-invalid={branchError || undefined}
              aria-describedby={branchError ? `${testIdPrefix}-error` : undefined}
              placeholder="Default branch"
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
          </>
        )}
        {error && (
          <p id={`${testIdPrefix}-error`} className="text-xs text-destructive" aria-live="polite">
            {error}
          </p>
        )}
      </div>
    );
  }
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
          (item) => repositoryIdentity(repoCloneUrl(item)) === repositoryIdentity(selection.url),
        );
        const suggestion = suggestions.find(
          (item) => repositoryIdentity(item.url) === repositoryIdentity(selection.url),
        );
        const name = suggestion?.name ?? deriveRepoName(selection.url) ?? selection.url;
        const error = repositorySelectionError(selection, { catalog, urlInput, branchSelection });
        const invalid = error !== null;
        const note = error ?? branchNotes?.[selection.url];
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
              {(branchSelection || selection.branch !== "") && (
                <div className="col-span-2 row-start-2 min-w-0 sm:w-40 sm:max-w-[55%] sm:shrink-0">
                  {repo && branchSelection ? (
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
              )}
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
          {(connected || catalog.length > 0) && selections.length < maxRepos && (
            <>
              <SandboxRepoCombobox
                repos={unselectedRepos}
                onSelect={(repo) => addRepo(repo.url)}
                testIdPrefix={testIdPrefix}
              />
              {connected && data.truncated && (
                <p
                  className="text-xs text-muted-foreground"
                  data-testid={`${testIdPrefix}-truncated`}
                >
                  Showing your most recently pushed repositories. Don't see one? Paste its URL
                  below.
                </p>
              )}
              {urlInput && (
                <p className="text-xs text-muted-foreground">or paste a repository URL:</p>
              )}
            </>
          )}
          {githubEnabled && isError && !connected && (
            <p className="text-sm text-destructive" data-testid={`${testIdPrefix}-error`}>
              Couldn't load your GitHub repositories. Paste a repository URL below.
            </p>
          )}
          {(urlInput || inputUrl.trim() !== "") && (
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
          )}
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
