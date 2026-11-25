import { useQuery } from "@tanstack/react-query";
import { TriangleAlertIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { getCliServerUrl } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import { quoteShellArgument } from "@/lib/shell";
import { cn } from "@/lib/utils";

interface HostToolsHealth {
  session?: {
    host_project_tools?: boolean | null;
    host_version?: string | null;
    host_install_spec?: string;
  };
}

/** Whether a session's host relays the board tools; `false` only for a known-outdated host. */
export function useHostProjectTools(sessionId: string | null | undefined) {
  return useQuery({
    queryKey: ["host-project-tools", sessionId],
    enabled: Boolean(sessionId),
    staleTime: 60_000,
    queryFn: async () => {
      const response = await authenticatedFetch(
        `/health?session_id=${encodeURIComponent(sessionId as string)}`,
      );
      if (!response.ok) return null;
      return ((await response.json()) as HostToolsHealth).session ?? null;
    },
  });
}

/** The commands that bring a host up to this server's build and reconnect it. */
export function hostUpdateCommand(installSpec: string, serverUrl: string): string {
  return [
    `uv tool install --force --python 3.12 ${quoteShellArgument(installSpec)}`,
    `omnigent host --server ${quoteShellArgument(serverUrl)}`,
  ].join("\n");
}

/**
 * Warns when a project session's host runs an Omnigent without the board tools:
 * its ticket writes then count as the owner, and its questions never reach them.
 */
export function OutdatedHostBanner({
  sessionId,
  name,
  className,
}: {
  sessionId: string | null | undefined;
  /** Who runs on that host, as used mid-sentence, e.g. "the Lead" or "Reviewer". */
  name: string;
  className?: string;
}) {
  const health = useHostProjectTools(sessionId).data;
  if (health?.host_project_tools !== false) return null;
  const version = health.host_version ? ` (${health.host_version})` : "";
  return (
    <Alert className={cn("border-amber-500/40", className)} data-testid="outdated-host-banner">
      <TriangleAlertIcon className="text-amber-600" />
      <AlertTitle>
        {name.charAt(0).toUpperCase() + name.slice(1)}'s computer needs an Omnigent update
      </AlertTitle>
      <AlertDescription className="space-y-2">
        <p>
          The Omnigent on that computer{version} doesn't have the board tools. So {name}'s tickets
          and comments count as you, and its questions to you won't show as Needs you. On that
          computer, run:
        </p>
        <pre className="overflow-x-auto rounded-md bg-muted px-2 py-1.5 font-mono text-xs whitespace-pre select-all">
          {hostUpdateCommand(health.host_install_spec ?? "omnigent", getCliServerUrl())}
        </pre>
      </AlertDescription>
    </Alert>
  );
}
