import { useEffect, useState } from "react";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { sandboxProfilesEnabled } from "@/lib/capabilities";
import { useQuery } from "@tanstack/react-query";
import { BoxIcon, ChevronDownIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  fetchSandboxProfiles,
  SANDBOX_PROFILES_QUERY_KEY,
  SANDBOX_PROFILE_RESULT_LIMIT,
  type SandboxProfile,
} from "@/lib/sandboxProfilesApi";

export type SandboxProfileSelection = Pick<SandboxProfile, "id" | "name" | "provider" | "revision">;

/** Find one profile by id: on the first page, else with a lookup that may reach beyond it. */
function useProfileById(provider: string | null, page: SandboxProfile[], profileId: string | null) {
  const inPage = page.find((p) => p.id === profileId);
  const looking = profileId !== null && inPage === undefined;
  const lookup = useQuery({
    queryKey: [...SANDBOX_PROFILES_QUERY_KEY, provider, "profile", profileId],
    queryFn: ({ signal }) =>
      fetchSandboxProfiles(provider ?? undefined, { profileId: profileId ?? undefined, signal }),
    enabled: looking,
    staleTime: 30_000,
    retry: false,
  });
  const profile =
    inPage ??
    (looking
      ? lookup.data?.profiles.find((p) => p.id === profileId && p.provider === provider)
      : undefined);
  return {
    profile,
    fetching: looking && lookup.isFetching,
    failed: looking && lookup.isError,
    // Looked up and not listed: the provider no longer offers this profile.
    missing: looking && lookup.isSuccess && profile === undefined,
  };
}

export function useSandboxProfileCatalog(
  provider: string | null,
  value: SandboxProfileSelection | null,
  rememberedProfileId: string | null = null,
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
  const explicit = required && value?.provider === provider ? value : null;
  const unpicked = required && explicit === null && provider !== null;
  // Prefer the last picked profile; unavailable or failed lookups fall back to the default.
  const remembered = useProfileById(provider, profiles, unpicked ? rememberedProfileId : null);
  const rememberedCheck = useQuery({
    queryKey: [
      ...SANDBOX_PROFILES_QUERY_KEY,
      provider,
      "selected",
      remembered.profile?.id,
      remembered.profile?.revision,
    ],
    queryFn: ({ signal }) =>
      fetchSandboxProfiles(provider ?? undefined, { profileId: remembered.profile?.id, signal }),
    enabled: remembered.profile !== undefined,
    staleTime: 30_000,
    retry: false,
  });
  const rememberedCurrent = rememberedCheck.data?.profiles.find(
    (p) => p.id === remembered.profile?.id && p.provider === provider,
  );
  const rememberedRejected =
    remembered.profile !== undefined &&
    (rememberedCheck.isError || (rememberedCheck.isSuccess && rememberedCurrent === undefined));
  const defaultProfileId =
    unpicked &&
    provider !== null &&
    (rememberedProfileId === null || remembered.missing || remembered.failed || rememberedRejected)
      ? (query.data?.default_profile_ids?.[provider] ?? null)
      : null;
  const fallback = useProfileById(provider, profiles, defaultProfileId);
  const preferred =
    (rememberedRejected ? undefined : (rememberedCurrent ?? remembered.profile)) ??
    fallback.profile;
  const selection: SandboxProfileSelection | null =
    explicit ??
    (unpicked && preferred !== undefined
      ? {
          id: preferred.id,
          name: preferred.name,
          provider: preferred.provider,
          revision: preferred.revision,
        }
      : null);
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
    // Picked from the first page at this revision: that page already checked it.
    ...(profiles.some((p) => p.id === selection?.id && p.revision === selection?.revision)
      ? { initialData: query.data, initialDataUpdatedAt: query.dataUpdatedAt }
      : {}),
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
          remembered.fetching ||
          fallback.fetching ||
          (selection && selectedQuery.isPending)
        ? "Loading sandbox profiles…"
        : query.isError || (selection && selectedQuery.isError) || fallback.failed
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
  rememberedProfileId = null,
}: {
  provider: string | null;
  value: SandboxProfileSelection | null;
  onChange: (profile: SandboxProfileSelection | null) => void;
  id?: string;
  /** Profile id to start on when nothing is picked, ahead of the provider default. */
  rememberedProfileId?: string | null;
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
  } = useSandboxProfileCatalog(provider, value, rememberedProfileId);
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
        <Command label="Search sandbox profiles" shouldFilter={false} defaultValue={selection?.id}>
          <CommandInput
            aria-label="Search sandbox profiles"
            placeholder="Search profiles…"
            value={search}
            onValueChange={setSearch}
            maxLength={200}
          />
          <CommandList aria-label="Sandbox profiles" className="max-h-64">
            {!loading && !active.isError && rows.length > 0 && (
              <CommandGroup>
                {rows.map((profile) => (
                  <CommandItem
                    key={`${profile.provider}:${profile.id}`}
                    value={profile.id}
                    data-checked={profile.id === selection?.id}
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
              </CommandGroup>
            )}
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
