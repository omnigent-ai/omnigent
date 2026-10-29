import { useEffect, useState } from "react";
import { CheckIcon, ChevronUpIcon, LogOutIcon, PlusIcon, SettingsIcon } from "lucide-react";
import { toast } from "sonner";
import { logout } from "@/lib/accountsApi";
import { withBasePath } from "@/lib/basePath";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  getServerPicker,
  openServerSetup,
  signOutOfServer,
  supportsNativeServerPicker,
  switchServer,
  type ServerPickerInfo,
} from "@/lib/nativeBridge";
import { Link } from "@/lib/routing";
import { userInitials } from "@/lib/userBadge";
import { cn } from "@/lib/utils";
import { useViewerId } from "@/hooks/useViewerId";
import { ownServerName } from "@/lib/serverNames";
import { SidebarSettingsButton } from "./SidebarHeaderActions";
import { useSettingsRoute } from "./settingsNav";

/** Short display label for a server URL — its host, e.g. "localhost:8000". */
function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

/** Origin of a server URL, for matching recents against the current origin. */
function originOf(url: string): string | null {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/**
 * A server's name over its host, or just the host. The host stays visible
 * because a server-supplied name is not proof of which server this is.
 */
function ServerLabel({
  name,
  host,
  className,
}: {
  name: string | null;
  host: string;
  className?: string;
}) {
  if (!name) return <span className={cn("min-w-0 truncate", className)}>{host}</span>;
  return (
    <span className="flex min-w-0 flex-col">
      <span className={cn("truncate", className)}>{name}</span>
      <span className="truncate text-xs text-muted-foreground">{host}</span>
    </span>
  );
}

/** Origin plus workspace selector, so two workspaces on one host stay apart. */
function serverKey(url: string): string | null {
  try {
    const parsed = new URL(url);
    return `${parsed.origin}?o=${parsed.searchParams.get("o") ?? ""}`;
  } catch {
    return null;
  }
}

/**
 * Profile control pinned to the sidebar's bottom in browsers and native shells.
 *
 * A profile trigger (avatar + identity + current server) opens the existing
 * server menu, with Settings above it and Sign out below it. The adjacent gear
 * links directly to Settings.
 *
 * This deliberately lives at the bottom of the sidebar rather than in the
 * chat surface's top strip. The macOS shell hides the native title bar
 * (titleBarStyle "hiddenInset"), and the previous picker filled that freed
 * strip with a centered "<thread> — <host>" label. But the chat header
 * occupies the same strip (`absolute top-0`, and taller at h-14), so on a
 * narrow window the centered label ran into the header's action cluster. The
 * iOS shell repeated the same mistake with its floating pill, which crowded
 * the chat header's title and floating controls on a notched iPhone. Docking
 * the picker here takes it out of that contested space on both shells.
 *
 * Native shells keep the existing server switcher. Browsers and older shells
 * show the current origin without offering unsupported switching actions.
 */
export function SidebarServerPicker() {
  const nativeServerPicker = supportsNativeServerPicker();
  const [info, setInfo] = useState<ServerPickerInfo | null>(() =>
    nativeServerPicker
      ? null
      : {
          currentOrigin: window.location.origin,
          recentServers: [],
        },
  );
  const viewerId = useViewerId();
  const serverInfo = useServerInfo();
  const { inSettings } = useSettingsRoute();

  useEffect(() => {
    if (!nativeServerPicker) {
      setInfo({ currentOrigin: window.location.origin, recentServers: [] });
      return;
    }
    let cancelled = false;
    void getServerPicker().then((result) => {
      if (!cancelled) setInfo(result);
    });
    return () => {
      cancelled = true;
    };
  }, [nativeServerPicker]);

  if (!info) return null;

  const managed = Array.isArray(info.managedServers) ? info.managedServers : [];
  const managedOrigins = new Set(managed.map(originOf).filter((origin) => origin !== null));
  // When sign-in moved hosts, the shell names the server the user picked; match
  // it by origin + workspace, since several workspaces can share that host.
  const currentServer = info.currentServer ?? null;
  const currentKey = currentServer === null ? null : serverKey(currentServer);
  const isCurrent = (url: string) =>
    originOf(url) === info.currentOrigin || (currentKey !== null && serverKey(url) === currentKey);
  const currentIsManaged = managed.some(isCurrent);
  const managedNames = new Map(Object.entries(info.managedServerNames ?? {}));
  const managedLabel = (url: string) => managedNames.get(url) ?? hostOf(url);
  // A server's own name is display only, so the host is always shown with it.
  const ownName = (url: string) => ownServerName(info.serverNames, url);
  const recentLabels = new Map(Object.entries(info.recentLabels ?? {}));
  const shownAs = (url: string) => recentLabels.get(url) ?? url;
  // A recent reached through a managed server's URL is that server: it's listed
  // once, as managed, which switches through the recent, where its sign-in is.
  const recentFor = (managedUrl: string) =>
    info.recentServers.find(
      (url) => recentLabels.has(url) && serverKey(shownAs(url)) === serverKey(managedUrl),
    );
  // The current server leads its section even when settings were edited out
  // from under us. Managed servers are not repeated under Recents.
  const recentOthers = info.recentServers.filter((url) => {
    const origin = originOf(url);
    return (
      !isCurrent(url) &&
      (origin === null || !managedOrigins.has(origin)) &&
      !managed.some((managedUrl) => recentFor(managedUrl) === url)
    );
  });
  const currentManaged = managed.find(isCurrent);
  // Name and host come from the same URL, so they always describe one server.
  const currentAddress = hostOf(currentServer ?? info.currentOrigin);
  const ownCurrent = ownName(currentServer ?? info.currentOrigin);
  // A name that only repeats the host adds nothing.
  const currentOwnName = ownCurrent === currentAddress ? null : ownCurrent;
  const currentManagedName =
    currentManaged !== undefined ? managedNames.get(currentManaged) : undefined;
  const currentHost = currentManagedName ?? currentOwnName ?? currentAddress;
  const currentDescription =
    currentManagedName === undefined && currentOwnName !== null
      ? `${currentOwnName} (${currentAddress})`
      : currentHost;
  const userName = viewerId === "local" ? "Local user" : (viewerId ?? "User");
  const browserServerInfo = serverInfo === "loading" ? null : serverInfo;
  const canSignOut = nativeServerPicker
    ? info.canSignOut === true
    : browserServerInfo !== null && browserServerInfo.login_url !== null;

  const handleSignOut = () => {
    if (nativeServerPicker) {
      void signOutOfServer().then((ok) => {
        if (!ok) toast.error(`Couldn't sign out of ${currentHost}`);
      });
      return;
    }
    if (browserServerInfo === null || browserServerInfo.login_url === null) return;
    if (browserServerInfo.accounts_enabled) {
      void logout().then(() => {
        window.location.href = withBasePath("/login");
      });
      return;
    }
    window.location.href = withBasePath("/auth/logout");
  };

  return (
    // shrink-0 keeps the row at its natural height so the scrolling session
    // list above (flex-1) gives up space instead of squashing it.
    <div
      className="flex shrink-0 items-stretch gap-1 px-2 pt-1 pb-2"
      data-testid="sidebar-server-picker-row"
    >
      <DropdownMenu
        onOpenChange={(open) => {
          // Re-read when opened so a newly applied/removed MDM profile appears
          // without restarting the server-served SPA.
          if (open && nativeServerPicker) void getServerPicker().then(setInfo);
        }}
      >
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            className={cn(
              "h-9 min-w-0 flex-1 justify-start gap-2 rounded-[var(--radius-otto-button)] border-0 px-2 py-1",
              "text-muted-foreground",
              "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50",
              "data-[state=open]:bg-muted data-[state=open]:text-foreground",
            )}
            aria-label={`${userName}. Server: ${currentDescription}. Open profile menu`}
            title={currentDescription}
            data-testid="sidebar-server-picker"
          >
            <Avatar className="size-6 after:border-0">
              <AvatarFallback className="bg-secondary font-medium text-sm text-secondary-foreground">
                {userInitials(userName.replace(/\s+/g, "."))}
              </AvatarFallback>
            </Avatar>
            <span className="flex min-w-0 flex-1 flex-col items-start text-left">
              <span className="w-full truncate text-left font-medium text-ui text-foreground leading-4">
                {userName}
              </span>
              <span className="w-full truncate text-left text-sm text-muted-foreground leading-4">
                {currentHost}
              </span>
            </span>
            {/* Points up: the menu opens upward from the sidebar's bottom. */}
            <ChevronUpIcon className="ui-icon ml-auto shrink-0 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        {/* side="top" — the trigger sits at the bottom of the window, so the
            menu must grow upward rather than off-screen. max-w caps the width so
            a long host (the default menu is w-max) truncates in place instead of
            overflowing the viewport on a narrow phone. */}
        <DropdownMenuContent
          side="top"
          align="start"
          className="min-w-[var(--radix-dropdown-menu-trigger-width)] max-w-[min(20rem,calc(100vw-1rem))]"
        >
          <DropdownMenuItem asChild className="gap-2">
            <Link
              to="/settings"
              componentId="sidebar.profile_menu.settings"
              data-testid="sidebar-profile-menu-settings"
            >
              <SettingsIcon className="size-4 shrink-0" />
              Settings
            </Link>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          {nativeServerPicker ? (
            <>
              {managed.length > 0 ? (
                <>
                  <DropdownMenuLabel className="text-muted-foreground">
                    Provided by your organization
                  </DropdownMenuLabel>
                  {managed.map((url) => {
                    const current = isCurrent(url);
                    return (
                      <DropdownMenuItem
                        key={url}
                        disabled={current}
                        className={cn("gap-2", current && "opacity-100")}
                        onSelect={
                          current ? undefined : () => void switchServer(recentFor(url) ?? url)
                        }
                      >
                        {current ? (
                          <CheckIcon className="size-4 shrink-0" />
                        ) : (
                          <span className="size-4 shrink-0" aria-hidden="true" />
                        )}
                        {managedNames.has(url) ? (
                          <span className={cn("min-w-0 truncate", current && "font-medium")}>
                            {managedLabel(url)}
                          </span>
                        ) : (
                          // No organization name: the server's own, beside its host.
                          <ServerLabel
                            name={ownName(url)}
                            host={hostOf(url)}
                            className={current ? "font-medium" : undefined}
                          />
                        )}
                      </DropdownMenuItem>
                    );
                  })}
                </>
              ) : null}
              {!currentIsManaged || recentOthers.length > 0 ? (
                <>
                  {managed.length > 0 ? <DropdownMenuSeparator /> : null}
                  <DropdownMenuLabel className="text-muted-foreground">Recents</DropdownMenuLabel>
                  {!currentIsManaged ? (
                    <DropdownMenuItem disabled className="gap-2 opacity-100">
                      <CheckIcon className="size-4 shrink-0" />
                      <ServerLabel
                        name={currentOwnName}
                        host={currentAddress}
                        className="font-medium"
                      />
                    </DropdownMenuItem>
                  ) : null}
                  {recentOthers.map((url) => (
                    <DropdownMenuItem
                      key={url}
                      className="gap-2"
                      onSelect={() => void switchServer(url)}
                    >
                      <span className="size-4 shrink-0" aria-hidden="true" />
                      <ServerLabel name={ownName(shownAs(url))} host={hostOf(shownAs(url))} />
                    </DropdownMenuItem>
                  ))}
                </>
              ) : null}
              <DropdownMenuSeparator />
              <DropdownMenuItem className="gap-2" onSelect={() => openServerSetup()}>
                <PlusIcon className="size-4 shrink-0" />
                Connect to new server…
              </DropdownMenuItem>
            </>
          ) : (
            <>
              <DropdownMenuLabel className="text-muted-foreground">Server</DropdownMenuLabel>
              <DropdownMenuItem disabled className="gap-2 opacity-100">
                <CheckIcon className="size-4 shrink-0" />
                <ServerLabel name={currentOwnName} host={currentAddress} className="font-medium" />
              </DropdownMenuItem>
            </>
          )}
          {canSignOut ? (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                className="gap-2"
                onSelect={handleSignOut}
                data-testid="sidebar-server-sign-out"
              >
                <LogOutIcon className="size-4 shrink-0" />
                <span className="min-w-0 truncate">
                  {nativeServerPicker ? `Sign out of ${currentHost}` : "Sign out"}
                </span>
              </DropdownMenuItem>
            </>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
      <SidebarSettingsButton
        testId="sidebar-profile-settings"
        className="size-9 shrink-0 rounded-[var(--radius-otto-button)]"
        to={inSettings ? "/" : "/settings"}
        label={inSettings ? "Home" : "Settings"}
      />
    </div>
  );
}
