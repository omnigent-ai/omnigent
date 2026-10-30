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
import { deriveRepoName, isValidSandboxRepoUrl } from "@/lib/sandboxRepositories";

const COMBOBOX_TRIGGER_CLASS =
  "flex w-full items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors hover:border-ring/60 focus-visible:border-ring";

function repoCloneUrl(repo: GithubRepo): string {
  return repo.clone_url ?? `https://github.com/${repo.full_name}.git`;
}

function repoIdentity(url: string): string {
  return url
    .trim()
    .replace(/^git@([^:]+):/, "https://$1/")
    .replace(/\/+$/, "")
    .replace(/\.git$/, "");
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
  value,
  onChange,
  enabled,
  testIdPrefix,
}: {
  repo: GithubRepo;
  value: string;
  onChange: (branch: string) => void;
  enabled: boolean;
  testIdPrefix: string;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const { data, isPending, isError } = useQuery({
    queryKey: ["github-branches", repo.full_name],
    queryFn: () => fetchGithubBranches(repo.full_name),
    enabled,
    staleTime: 5 * 60_000,
  });
  const branches = data?.connected ? data.branches : [];
  const defaultLabel = repo.default_branch ? `Default (${repo.default_branch})` : "Default branch";
  const options = branches.filter((branch) => branch !== repo.default_branch);
  if (value !== "" && !options.includes(value)) options.unshift(value);
  const customBranch = search.trim();
  const showCustomBranch =
    customBranch !== "" && customBranch !== repo.default_branch && !options.includes(customBranch);
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
                  onSelect={() => chooseBranch(branch)}
                >
                  {branch}
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
  onChange: (selections: LastSandboxRepo[]) => void;
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
  const invalidUrl = inputUrl.trim() !== "" && !isValidSandboxRepoUrl(inputUrl);
  const canAdd = isValidSandboxRepoUrl(inputUrl) && !duplicateUrl && selections.length < maxRepos;
  const addRepo = (url: string): void => {
    if (selections.length >= maxRepos || selectedUrls.has(repoIdentity(url))) return;
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
    );
  };
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
        const repo = repos.find(
          (item) => repoIdentity(repoCloneUrl(item)) === repoIdentity(selection.url),
        );
        const name = repo?.full_name ?? deriveRepoName(selection.url) ?? selection.url;
        return (
          <div
            key={selection.url}
            className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 gap-y-1.5 py-1 sm:flex sm:gap-2 sm:py-0"
            data-testid={`${testIdPrefix}-row`}
          >
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
                  value={selection.branch}
                  onChange={(branch) => setBranch(index, branch)}
                  enabled={githubEnabled}
                  testIdPrefix={testIdPrefix}
                />
              ) : (
                <input
                  type="text"
                  value={selection.branch}
                  onChange={(event) => setBranch(index, event.target.value)}
                  placeholder="Default branch"
                  aria-label={`Branch for ${name}`}
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
              aria-invalid={invalidUrl || duplicateUrl}
              aria-describedby={
                invalidUrl || duplicateUrl ? `${testIdPrefix}-input-error` : undefined
              }
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
          {(invalidUrl || duplicateUrl) && (
            <p id={`${testIdPrefix}-input-error`} className="text-xs text-destructive">
              {duplicateUrl
                ? "This repository is already selected. Change its branch above."
                : "Enter an HTTPS or SSH repository URL."}
            </p>
          )}
        </>
      )}
    </div>
  );
}
