import { useQuery } from "@tanstack/react-query";
import { ChevronDownIcon, TriangleAlertIcon, XIcon } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { getCliServerUrl } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import { quoteShellArgument } from "@/lib/shell";
import { cn } from "@/lib/utils";

interface HostUpdate {
  needed: boolean;
  host_id?: string | null;
  host_version?: string | null;
  install_spec?: string | null;
}

/** Whether the viewer should update the computer hosting a session (only its owner ever should). */
export function useHostUpdate(sessionId: string | null | undefined) {
  return useQuery({
    queryKey: ["host-update", sessionId],
    enabled: Boolean(sessionId),
    staleTime: 60_000,
    queryFn: async (): Promise<HostUpdate | null> => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId as string)}/host-update`,
      );
      if (!response.ok) return null;
      return (await response.json()) as HostUpdate;
    },
  });
}

/**
 * Commands that install this server's build and restart the host for it.
 *
 * They call the uv-installed binary by path, because an older `omnigent` may come
 * first on PATH (OMNIGENT_WRAPPER_BYPASS lets it run where a wrapper requires
 * one), and they stop the old host
 * for this server first, because `host --background` would otherwise reuse it.
 */
export function hostUpdateCommand(installSpec: string, serverUrl: string): string {
  const server = quoteShellArgument(serverUrl);
  return [
    `uv tool install --force --python 3.12 ${quoteShellArgument(installSpec)}`,
    `export OMNIGENT_WRAPPER_BYPASS=1; omnigent="$(uv tool dir --bin)/omnigent"`,
    `"$omnigent" host stop --daemon-only --server ${server}`,
    `"$omnigent" host --background --server ${server}`,
    `"$omnigent" host status --all`,
  ].join("\n");
}

const DISMISSED_KEY_PREFIX = "omnigent.hostUpdate.dismissed.";

function dismissedVersion(hostId: string): string | null {
  try {
    return window.localStorage.getItem(DISMISSED_KEY_PREFIX + hostId);
  } catch {
    return null;
  }
}

/**
 * A compact notice, for the owner of the computer hosting a project session,
 * when that host doesn't relay the board tools: its ticket writes then count as
 * the owner, and its questions never reach them. Dismissed per host and
 * version, so it returns only when a different build connects without them.
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
  const update = useHostUpdate(sessionId).data;
  const [open, setOpen] = useState(false);
  const [dismissed, setDismissed] = useState<string | null>(null);
  if (!update?.needed) return null;
  const hostId = update.host_id ?? "";
  const version = update.host_version ?? "";
  if ((dismissed ?? dismissedVersion(hostId)) === version) return null;
  const dismiss = () => {
    try {
      window.localStorage.setItem(DISMISSED_KEY_PREFIX + hostId, version);
    } catch {
      // Private mode: the dismissal lasts until reload.
    }
    setDismissed(version);
  };
  const who = name.charAt(0).toUpperCase() + name.slice(1);
  return (
    <div
      className={cn("rounded-md border border-amber-500/40 bg-amber-500/5 text-sm", className)}
      data-testid="outdated-host-banner"
      role="status"
    >
      <div className="flex items-center gap-2 py-1 pr-1 pl-2.5">
        <TriangleAlertIcon className="size-4 shrink-0 text-amber-600" aria-hidden />
        <span className="min-w-0 flex-1 truncate">
          {who}'s computer needs an Omnigent update
          {version && <span className="text-muted-foreground"> ({version})</span>}
        </span>
        <Button
          variant="ghost"
          size="xs"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          How to fix
          <ChevronDownIcon className={cn("size-3.5 transition-transform", open && "rotate-180")} />
        </Button>
        <Button variant="ghost" size="icon-xs" aria-label="Dismiss update notice" onClick={dismiss}>
          <XIcon className="size-3.5" />
        </Button>
      </div>
      {open && (
        <div className="space-y-2 border-t border-amber-500/20 px-2.5 pt-2 pb-2.5 text-muted-foreground">
          <p>
            The Omnigent on your computer that runs {name} doesn't have the board tools. So{" "}
            {`${name}'s`} tickets and comments count as you, and its questions to you won't show as
            Needs you. On that computer, run:
          </p>
          <pre className="overflow-x-auto rounded-md bg-muted px-2 py-1.5 font-mono text-xs whitespace-pre text-foreground select-all">
            {hostUpdateCommand(update.install_spec ?? "omnigent", getCliServerUrl())}
          </pre>
          <p>
            This installs this server's build and restarts that computer's host for this server. Its
            sessions keep their history and pick up at their next message. If another tool started
            Omnigent on that computer for a different server, leave that host running: the last
            command should list both.
          </p>
        </div>
      )}
    </div>
  );
}
