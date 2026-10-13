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

export function SandboxProfilePicker({
  provider,
  value,
  onChange,
}: {
  provider: string | null;
  value: SandboxProfileSelection | null;
  onChange: (profile: SandboxProfileSelection | null) => void;
}) {
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
  // Default launches still work against servers without a profile catalog.
  if (profiles.length === 0 && value === null) return null;
  const current = profiles.find((profile) => profile.id === value?.id);
  const stale = value !== null && query.data !== undefined && current?.revision !== value.revision;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Sandbox profile: ${value?.name ?? "Default"}`}
          className="inline-flex h-6 min-w-0 max-w-[50%] items-center gap-1 rounded-md px-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <BoxIcon className="size-3.5 shrink-0" />
          <span className="truncate">{value?.name ?? "Default"}</span>
          {stale && <span className="text-warning">(changed)</span>}
          <ChevronDownIcon className="size-3 shrink-0 opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-72 max-w-[calc(100vw-2rem)]">
        <DropdownMenuLabel>Sandbox profile</DropdownMenuLabel>
        <DropdownMenuItem onSelect={() => onChange(null)}>
          <span className="flex flex-col gap-0.5">
            <span>Default</span>
            <span className="text-xs text-muted-foreground">Use the server configuration</span>
          </span>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {profiles.map((profile) => (
          <DropdownMenuItem
            key={profile.id}
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
              <span>{profile.name}</span>
              {profile.description && (
                <span className="text-xs text-muted-foreground">{profile.description}</span>
              )}
            </span>
          </DropdownMenuItem>
        ))}
        {stale && (
          <p role="status" className="px-2 py-1 text-xs text-warning">
            This profile changed or is unavailable. Choose a profile again before launching.
          </p>
        )}
        {query.isError && (
          <p role="alert" className="px-2 py-1 text-xs text-destructive">
            Could not refresh sandbox profiles.
          </p>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
