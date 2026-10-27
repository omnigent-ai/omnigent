import { useEffect, useState } from "react";
import {
  AppWindowIcon,
  ChevronDownIcon,
  CodeIcon,
  FolderOpenIcon,
  TerminalIcon,
} from "lucide-react";
import { toast } from "sonner";
import { authenticatedFetch } from "@/lib/identity";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  getFileOpenApps,
  getFileOpenSshHost,
  openFileInApp,
  openRemoteFileInApp,
  supportsFileOpen,
  supportsFileOpenSshDiscovery,
  supportsFileReveal,
  supportsRemoteFileOpen,
  type FileOpenApp,
} from "@/lib/nativeBridge";
import {
  revealInFileManager,
  revealLabel,
  useHostFileTarget,
  useLocalFileTarget,
} from "./RevealInFileManager";

/** Shared actions for the folder header and the file viewer's responsive toolbar. */
export function useOpenInActions(path: string | null, directory = false) {
  const localTarget = useLocalFileTarget(path);
  const target = useHostFileTarget(path);
  const remote = !localTarget;
  const hostId = target?.hostId;
  const available =
    !!target &&
    (remote ? supportsRemoteFileOpen() && supportsFileOpenSshDiscovery() : supportsFileOpen());
  const [apps, setApps] = useState<FileOpenApp[]>([]);
  const [connection, setConnection] = useState<{ hostId: string; alias: string } | null>(null);
  const sshHost = connection && connection.hostId === hostId ? connection.alias : null;
  useEffect(() => {
    if (!available || !remote || !hostId) return;
    let cancelled = false;
    const discover = async () => {
      let alias = await getFileOpenSshHost(hostId);
      if (!alias) {
        try {
          const response = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}`, {
            signal: AbortSignal.timeout(3000),
          });
          if (response.ok) {
            const detail = await response.json();
            if (
              Array.isArray(detail.ssh_host_key_fingerprints) &&
              detail.ssh_host_key_fingerprints.length
            ) {
              alias = await getFileOpenSshHost(hostId, detail.ssh_host_key_fingerprints);
            }
          }
        } catch {
          // Unavailable metadata leaves remote opening hidden.
        }
      }
      if (!cancelled) setConnection(alias ? { hostId, alias } : null);
    };
    void discover();
    return () => {
      cancelled = true;
    };
  }, [available, remote, hostId]);
  useEffect(() => {
    if (!available) return;
    let cancelled = false;
    void getFileOpenApps().then((result) => {
      if (!cancelled) setApps(result);
    });
    return () => {
      cancelled = true;
    };
  }, [available]);

  const eligibleApps = remote
    ? apps.filter((app) => app.id === "vscode" || app.id === "cursor")
    : apps;
  const actions =
    !available || !target || (remote && !sshHost)
      ? []
      : eligibleApps.map((app) => ({
          key: app.id,
          label: app.label,
          icon: app.icon ? (
            <img src={app.icon} alt="" className="size-4 shrink-0 object-contain" />
          ) : app.directory ? (
            <TerminalIcon className="size-4" />
          ) : app.id === "default" ? (
            <AppWindowIcon className="size-4" />
          ) : (
            <CodeIcon className="size-4" />
          ),
          active: false,
          onSelect: () => {
            if (remote && sshHost) {
              void openRemoteFileInApp(target.hostId, target.path, app.id, sshHost, directory).then(
                (ok) => {
                  if (!ok)
                    toast.error(
                      `Couldn't open this remote ${directory ? "folder" : "file"} in ${app.label}`,
                    );
                },
              );
              return;
            }
            void openFileInApp(target.hostId, target.path, app.id).then((ok) => {
              if (!ok)
                toast.error(`Couldn't open this ${directory ? "folder" : "file"} in ${app.label}`);
            });
          },
        }));
  if (available && localTarget && supportsFileReveal()) {
    actions.push({
      key: "reveal",
      label: revealLabel(directory),
      icon: <FolderOpenIcon className="size-4" />,
      active: false,
      onSelect: () => revealInFileManager(localTarget),
    });
  }
  return actions;
}

export function OpenInMenu({ path, directory = false }: { path: string; directory?: boolean }) {
  const actions = useOpenInActions(path, directory);
  if (!actions.length) return null;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="ghost" size="sm" className="shrink-0" aria-label="Open in…">
          Open in…
          <ChevronDownIcon />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {actions.map((action) => (
          <DropdownMenuItem key={action.key} onSelect={action.onSelect}>
            {action.icon}
            {action.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
