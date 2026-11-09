import { useEffect, useState } from "react";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { sandboxProfilesEnabled } from "@/lib/capabilities";
import { useQuery } from "@tanstack/react-query";
import { BoxIcon, ChevronDownIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Command, CommandInput, CommandList, CommandItem } from "@/components/ui/command";
import {
  fetchSandboxProfiles,
  SANDBOX_PROFILES_QUERY_KEY,
  SANDBOX_PROFILE_RESULT_LIMIT,
  type SandboxProfile,
} from "@/lib/sandboxProfilesApi";

export type SandboxProfileSelection = Pick<SandboxProfile, "id" | "name" | "provider" | "revision">;

export function useSandboxProfileCatalog(
  provider: string | null,
  value: SandboxProfileSelection | null,
) {
  const enabled = sandboxProfilesEnabled(useServerInfo());
  const query = useQuery({
    queryKey: [...SANDBOX_PROFILES_QUERY_KEY, provider],
    queryFn: ({ signal }) => fetchSandboxProfiles(provider ?? undefined, { signal }),
    enabled: enabled && provider !== null,
    staleTime: 30_000,
    retry: false,
  });
  const profiles = (query.data?.profiles ?? []).filter((p) => enabled && p.provider === provider);
  const required =
    enabled && provider !== null && (query.data?.managed_providers ?? []).includes(provider);
  const selection = required && value?.provider === provider ? value : null;
  const selectedQuery = useQuery({
    queryKey: [
      ...SANDBOX_PROFILES_QUERY_KEY,
      provider,
      "selected",
      selection?.id,
      selection?.revision,
    ],
    queryFn: ({ signal }) =>
      fetchSandboxProfiles(provider ?? undefined, { profileId: selection?.id, signal }),
    enabled: selection !== null,
    staleTime: 30_000,
    retry: false,
  });
  const current = selectedQuery.data?.profiles.find(
    (p) => p.id === selection?.id && p.provider === provider,
  );
  const stale =
    selection !== null && selectedQuery.isSuccess && current?.revision !== selection.revision;
  const launchError =
    !enabled || provider === null || (query.data !== undefined && !required)
      ? null
      : query.isPending ||
          query.isFetching ||
          (selection && (selectedQuery.isPending || selectedQuery.isFetching))
        ? "Loading sandbox profiles…"
        : query.isError || (selection && selectedQuery.isError)
          ? "Could not load sandbox profiles. Try again."
          : stale
            ? "This profile changed or is unavailable. Choose a profile again before launching."
            : required && profiles.length === 0 && !selection
              ? "No sandbox profiles available for this provider."
              : required && selection === null
                ? "Choose a sandbox profile to continue."
                : null;
  return {
    enabled,
    query,
    selectedQuery,
    profiles,
    required,
    selection,
    current,
    stale,
    launchError,
  };
}

export function SandboxProfilePicker({
  provider,
  value,
  onChange,
  id,
}: {
  provider: string | null;
  value: SandboxProfileSelection | null;
  onChange: (profile: SandboxProfileSelection | null) => void;
  id?: string;
}) {
  const {
    enabled,
    query,
    selectedQuery,
    profiles,
    required,
    selection,
    current,
    stale,
    launchError,
  } = useSandboxProfileCatalog(provider, value);
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 200);
    return () => clearTimeout(timer);
  }, [search]);
  const results = useQuery({
    queryKey: [...SANDBOX_PROFILES_QUERY_KEY, provider, "search", debouncedSearch],
    queryFn: ({ signal }) =>
      fetchSandboxProfiles(provider ?? undefined, { search: debouncedSearch, signal }),
    enabled: open && required && debouncedSearch !== "",
    staleTime: 30_000,
    retry: false,
  });
  const searching = search.trim() !== "";
  const active = searching ? results : query;
  const rows = (searching ? (results.data?.profiles ?? []) : profiles)
    .filter((p) => p.provider === provider)
    .slice(0, SANDBOX_PROFILE_RESULT_LIMIT);
  const loading = searching && (search.trim() !== debouncedSearch || results.isFetching);
  if (!enabled || provider === null || (query.data !== undefined && !required)) return null;
  const label = query.isPending
    ? "Loading profiles…"
    : query.isError
      ? "Profiles unavailable"
      : (current?.name ??
        selection?.name ??
        (profiles.length === 0 ? "No profiles available" : "Choose profile"));
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
          id={id}
          type="button"
          aria-label={`Sandbox profile: ${label}`}
          className="inline-flex h-6 min-w-0 max-w-full items-center gap-1 rounded-md px-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <BoxIcon className="size-3.5 shrink-0" />
          <span className="max-w-64 truncate">{label}</span>
          {stale && <span className="text-warning">(changed)</span>}
          <ChevronDownIcon className="size-3 shrink-0 opacity-60" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 max-w-[calc(100vw-2rem)] p-0">
        <Command label="Search sandbox profiles" shouldFilter={false}>
          <CommandInput
            aria-label="Search sandbox profiles"
            placeholder="Search profiles…"
            value={search}
            onValueChange={setSearch}
            maxLength={200}
          />
          <CommandList aria-label="Sandbox profiles" className="max-h-64">
            {!loading &&
              !active.isError &&
              rows.map((profile) => (
                <CommandItem
                  key={`${profile.provider}:${profile.id}`}
                  value={profile.id}
                  onSelect={() => {
                    onChange(profile);
                    setOpen(false);
                    setSearch("");
                  }}
                >
                  <span className="flex min-w-0 flex-col gap-0.5">
                    <span className="truncate" title={profile.name}>
                      {profile.name}
                    </span>
                    {profile.description && (
                      <span
                        className="line-clamp-2 text-xs text-muted-foreground"
                        title={profile.description}
                      >
                        {profile.description}
                      </span>
                    )}
                  </span>
                </CommandItem>
              ))}
            {loading && (
              <p role="status" className="p-3 text-xs text-muted-foreground">
                Searching profiles…
              </p>
            )}
            {!loading && !active.isError && rows.length === 0 && (
              <p role="status" className="p-3 text-xs text-muted-foreground">
                {searching
                  ? "No matching profiles."
                  : (launchError ?? "No sandbox profiles available for this provider.")}
              </p>
            )}
          </CommandList>
          {!loading && active.data?.next_cursor && (
            <p role="status" className="border-t px-3 py-2 text-xs text-muted-foreground">
              Showing first {SANDBOX_PROFILE_RESULT_LIMIT} matches. Refine your search.
            </p>
          )}
          {(active.isError || selectedQuery.isError) && (
            <div className="p-3 text-xs">
              <p role="alert">Could not load sandbox profiles.</p>
              <button
                type="button"
                className="mt-1 underline"
                onClick={() => {
                  void active.refetch();
                  if (selection) void selectedQuery.refetch();
                }}
              >
                Try again
              </button>
            </div>
          )}
        </Command>
      </PopoverContent>
    </Popover>
  );
}
