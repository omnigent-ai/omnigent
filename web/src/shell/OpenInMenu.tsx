import { useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import {
  AppWindowIcon,
  ChevronDownIcon,
  CheckIcon,
  CodeIcon,
  FolderOpenIcon,
  TerminalIcon,
} from "lucide-react";
import { toast } from "sonner";
import { authenticatedFetch } from "@/lib/identity";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
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

const APP_PREFERENCE_EVENT = "omnigent:open-in-app-changed";
const preferenceKey = (remote: boolean) => `omnigent:open-in-app:${remote ? "remote" : "local"}`;

function readPreferredApp(remote: boolean): string {
  try {
    return localStorage.getItem(preferenceKey(remote)) ?? "";
  } catch {
    return "";
  }
}

function rememberApp(remote: boolean, appId: string) {
  try {
    localStorage.setItem(preferenceKey(remote), appId);
  } catch {
    // Opening still works if this browser cannot save preferences.
  }
  window.dispatchEvent(new Event(APP_PREFERENCE_EVENT));
}

function subscribeToAppPreference(listener: () => void) {
  window.addEventListener(APP_PREFERENCE_EVENT, listener);
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener(APP_PREFERENCE_EVENT, listener);
    window.removeEventListener("storage", listener);
  };
}

interface OpenInAction {
  key: string;
  label: string;
  icon: ReactNode;
  active: boolean;
  onSelect: () => void;
}

/** Shared actions for the folder header and the file viewer's responsive toolbar. */
export function useOpenInActions(path: string | null, directory = false): OpenInAction[] {
  const localTarget = useLocalFileTarget(path);
  const target = useHostFileTarget(path);
  const remote = !localTarget;
  const preferredApp = useSyncExternalStore(
    subscribeToAppPreference,
    () => readPreferredApp(remote),
    () => "",
  );
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
            rememberApp(remote, app.id);
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
      onSelect: () => {
        rememberApp(false, "reveal");
        revealInFileManager(localTarget);
      },
    });
  }
  const defaultAction =
    actions.find((action) => action.key === preferredApp) ??
    actions.find((action) => action.key === "vscode" || action.key === "cursor") ??
    actions[0];
  for (const action of actions) action.active = action === defaultAction;
  return actions;
}

export function OpenInSplitButton({
  actions,
  interactive = true,
}: {
  actions: OpenInAction[];
  interactive?: boolean;
}) {
  const defaultAction = actions.find((action) => action.active) ?? actions[0];
  if (!defaultAction) return null;
  const label =
    defaultAction.key === "reveal" ? defaultAction.label : `Open in ${defaultAction.label}`;
  return (
    <DropdownMenu>
      <div role="group" aria-label="Open in" className="inline-flex shrink-0">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="icon-sm"
                className="rounded-r-none border-r-0"
                aria-label={label}
                tabIndex={interactive ? undefined : -1}
                onClick={interactive ? defaultAction.onSelect : undefined}
              >
                {defaultAction.icon}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{label}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="outline"
            size="icon-sm"
            className="w-6 rounded-l-none"
            aria-label="Choose app to open in"
            tabIndex={interactive ? undefined : -1}
          >
            <ChevronDownIcon />
          </Button>
        </DropdownMenuTrigger>
      </div>
      <DropdownMenuContent align="end">
        {actions.map((action) => (
          <DropdownMenuItem key={action.key} onSelect={interactive ? action.onSelect : undefined}>
            {action.icon}
            {action.label}
            {action.active && <CheckIcon className="ml-auto size-4" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function OpenInMenu({ path, directory = false }: { path: string; directory?: boolean }) {
  const actions = useOpenInActions(path, directory);
  return <OpenInSplitButton actions={actions} />;
}
