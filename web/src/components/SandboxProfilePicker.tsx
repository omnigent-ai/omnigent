import { useQuery } from "@tanstack/react-query";
import { BoxIcon, ChevronDownIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  fetchSandboxProfiles,
  SANDBOX_PROFILES_QUERY_KEY,
  type SandboxProfile,
} from "@/lib/sandboxProfilesApi";

export type SandboxProfileSelection = Pick<SandboxProfile, "id" | "name" | "provider" | "revision">;

export function useSandboxProfileCatalog(
  provider: string | null,
  value: SandboxProfileSelection | null,
) {
  const query = useQuery({
    queryKey: SANDBOX_PROFILES_QUERY_KEY,
    queryFn: fetchSandboxProfiles,
    enabled: provider !== null,
    staleTime: 30_000,
    retry: false,
  });
  const profiles = (query.data?.profiles ?? []).filter(
    (profile) => profile.enabled && profile.provider === provider,
  );
  const required = provider !== null && (query.data?.managed_providers ?? []).includes(provider);
  const selection = value?.provider === provider ? value : null;
  const current = profiles.find((profile) => profile.id === selection?.id);
  const stale =
    selection !== null && query.data !== undefined && current?.revision !== selection.revision;
  const launchError =
    provider === null
      ? null
      : query.isPending || query.isFetching
        ? "Loading sandbox profiles…"
        : query.isError
          ? "Could not load sandbox profiles. Try again."
          : stale
            ? "This profile changed or is unavailable. Choose a profile again before launching."
            : required && profiles.length === 0
              ? "No sandbox profiles available for this provider."
              : required && selection === null
                ? "Choose a sandbox profile to continue."
                : null;
  return { query, profiles, required, selection, stale, launchError };
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
  const { query, profiles, required, selection, stale, launchError } = useSandboxProfileCatalog(
    provider,
    value,
  );
  if (provider === null) return null;
  const label =
    query.isPending || query.isFetching
      ? "Loading profiles…"
      : query.isError
        ? "Profiles unavailable"
        : (selection?.name ??
          (profiles.length === 0
            ? "No profiles available"
            : required
              ? "Choose profile"
              : "Default"));
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          id={id}
          type="button"
          aria-label={`Sandbox profile: ${label}`}
          className="inline-flex h-6 min-w-0 max-w-[50%] items-center gap-1 rounded-md px-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <BoxIcon className="size-3.5 shrink-0" />
          <span className="truncate">{label}</span>
          {stale && <span className="text-warning">(changed)</span>}
          <ChevronDownIcon className="size-3 shrink-0 opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-72 max-w-[calc(100vw-2rem)]">
        <DropdownMenuLabel>Sandbox profile</DropdownMenuLabel>
        {query.isSuccess && !required && (
          <>
            <DropdownMenuItem onSelect={() => onChange(null)}>
              <span className="flex flex-col gap-0.5">
                <span>Default</span>
                <span className="text-xs text-muted-foreground">Use the server configuration</span>
              </span>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
          </>
        )}
        {profiles.map((profile) => (
          <DropdownMenuItem
            key={`${profile.provider}:${profile.id}`}
            disabled={query.isError}
            onSelect={() =>
              onChange({
                id: profile.id,
                name: profile.name,
                provider: profile.provider,
                revision: profile.revision,
              })
            }
          >
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="break-words whitespace-normal">{profile.name}</span>
              {profile.description && (
                <span className="text-xs break-words whitespace-normal text-muted-foreground">
                  {profile.description}
                </span>
              )}
            </span>
          </DropdownMenuItem>
        ))}
        {(launchError || profiles.length === 0) && (
          <p
            role={query.isError ? "alert" : "status"}
            className="px-2 py-1 text-xs whitespace-normal text-muted-foreground"
          >
            {launchError ?? "No saved profiles for this provider."}
          </p>
        )}
        {query.isError && (
          <DropdownMenuItem onSelect={() => void query.refetch()}>Try again</DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
