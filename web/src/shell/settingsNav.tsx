// Shared model + sidebar body for the Settings surface.
//
// Entering /settings doesn't swap out the conversations sidebar card — the
// SAME card just renders this nav in place of the conversation list, while
// the main area shows the selected section's content (SettingsPage). Section
// selection is URL-driven (/settings/<section>) so the nav (in the sidebar)
// and the content (in the outlet) stay in sync without shared state.

import { useEffect } from "react";
import {
  ArchiveIcon,
  ArrowLeftIcon,
  BlocksIcon,
  DownloadIcon,
  GitBranchIcon,
  KeyboardIcon,
  PaletteIcon,
  SettingsIcon,
  Share2Icon,
  ShieldCheckIcon,
  TerminalIcon,
  UserCogIcon,
  UsersIcon,
} from "lucide-react";
import { Link, useLocation } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode } from "@/lib/capabilities";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { isElectronShell } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";
import { SIDEBAR_ROW } from "./sidebarStyles";

export type SettingsSectionId =
  | "appearance"
  | "general"
  | "git"
  | "integrations"
  | "shortcuts"
  | "import"
  | "account"
  | "members"
  | "policies"
  | "sharing"
  | "archived"
  | "cli"
  | "updates";

const SECTION_IDS: readonly SettingsSectionId[] = [
  "appearance",
  "general",
  "git",
  "integrations",
  "shortcuts",
  "import",
  "account",
  "members",
  "policies",
  "sharing",
  "archived",
  "cli",
  "updates",
];

interface SettingsNavItem {
  id: SettingsSectionId;
  label: string;
  icon: typeof PaletteIcon;
  /** Hide this item on mobile (e.g. keyboard shortcuts on a touch device). */
  hideOnMobile?: boolean;
}

interface SettingsNavGroup {
  title: string;
  items: SettingsNavItem[];
}

/**
 * Nav groups for the current deploy. The Account section appears whenever the
 * deploy has a login session (accounts OR OIDC/SSO — i.e. a `login_url`
 * exists), so an SSO user can see who they're signed in as and sign out; it's
 * absent only in header single-user mode. The Admin group (Members / Policies)
 * appears for admins in ANY multi-user mode since both accounts and OIDC share
 * the `users.is_admin` flag and the server enforces admin on every route; the
 * Desktop group (Local CLI) appears only in the Electron shell.
 */
export function settingsNavGroups(
  hasAuthSession: boolean,
  isDesktop: boolean,
  isAdmin = false,
  isSingleUser = false,
  integrationsEnabled = false,
): SettingsNavGroup[] {
  const general: SettingsNavItem[] = [
    { id: "general", label: "General", icon: SettingsIcon },
    { id: "appearance", label: "Appearance", icon: PaletteIcon },
    { id: "git", label: "Git", icon: GitBranchIcon },
    { id: "shortcuts", label: "Keyboard shortcuts", icon: KeyboardIcon, hideOnMobile: true },
    { id: "import", label: "Import sessions", icon: DownloadIcon },
  ];
  // Sandbox Integrations appears once any connection provider is wired
  // (enabled_connections non-empty). Slots right after Git.
  if (integrationsEnabled) {
    general.splice(2, 0, {
      id: "integrations",
      label: "Sandbox Integrations",
      icon: BlocksIcon,
    });
  }
  if (hasAuthSession) {
    // Account leads the group when present — it's the most-visited section
    // on a deploy with sign-in.
    general.unshift({ id: "account", label: "Account", icon: UserCogIcon });
  }
  const groups: SettingsNavGroup[] = [];
  // Desktop (Local CLI) leads when present — it's the shell-specific section a
  // desktop user is most likely here to change.
  if (isDesktop) {
    groups.push({
      title: "Desktop",
      items: [
        { id: "cli", label: "Local CLI", icon: TerminalIcon },
        { id: "updates", label: "Updates", icon: DownloadIcon },
      ],
    });
  }
  groups.push({ title: "General", items: general });
  // Admin: server-wide management, admin-only. Nested here as sub-categories
  // (rather than links out of the Account section) so entering them stays
  // inside /settings — the sidebar keeps the settings nav instead of snapping
  // back to the conversation list. Gated on `isAdmin` alone (not
  // `accountsEnabled`) so the surface also appears under OIDC/SSO, the one
  // mode where there's otherwise no admin chrome at all. Members runs
  // read-only under OIDC (no password actions); Policies is identical.
  if (isAdmin) {
    // Members (manage other accounts) and Sharing (grant sessions to other
    // users) have no meaning in single-user mode — there are no other users —
    // so drop both from the nav there. Policies stays: global policies apply
    // to a solo user's own sessions too.
    const adminItems: SettingsNavItem[] = [];
    if (!isSingleUser) adminItems.push({ id: "members", label: "Members", icon: UsersIcon });
    adminItems.push({ id: "policies", label: "Policies", icon: ShieldCheckIcon });
    if (!isSingleUser) adminItems.push({ id: "sharing", label: "Sharing", icon: Share2Icon });
    groups.push({ title: "Admin", items: adminItems });
  }
  groups.push({
    title: "Archived",
    items: [{ id: "archived", label: "Archived sessions", icon: ArchiveIcon }],
  });
  return groups;
}

/**
 * Parse the active route into a settings descriptor. `inSettings` gates the
 * sidebar body swap; `section` drives the content. Bare `/settings` (no
 * section segment) and unknown sections default to General. Basename-agnostic
 * — matches the `settings` segment wherever it lands, same approach as the
 * sidebar's top-level nav detection.
 */
export function useSettingsRoute(): { inSettings: boolean; section: SettingsSectionId } {
  const info = useServerInfo();
  const defaultSection: SettingsSectionId = "general";

  const segments = useLocation().pathname.split("/").filter(Boolean);
  const idx = segments.lastIndexOf("settings");
  if (idx === -1) return { inSettings: false, section: defaultSection };
  const next = segments[idx + 1];
  // Members / Policies / Sharing are admin sections valid in ANY multi-user
  // mode (accounts AND OIDC). They're gated in the nav on `is_admin` and the
  // pages self-gate + the server 403s, so no accounts-mode carve-out here.
  // Members and Sharing are the exception: single-user mode has no other
  // users, so a direct hit to either falls back to the default section.
  const singleUser = isSingleUserMode(info);
  const isValidSection =
    (SECTION_IDS as readonly string[]).includes(next) &&
    !(singleUser && (next === "members" || next === "sharing"));
  const section = isValidSection ? (next as SettingsSectionId) : defaultSection;
  return { inSettings: true, section };
}

// Last location the user was on before entering /settings — path + search so
// the conversation (and its ?file= etc.) is preserved. The Back row
// returns here instead of the home page. Module-scoped: the sidebar stays
// mounted across the settings transition, so the value captured on the last
// non-settings render survives into settings.
let settingsReturnPath = "/";

/**
 * Record the current location as the settings return target whenever the user
 * is NOT in settings. Call from a component that stays mounted across the
 * transition into /settings (the Sidebar) so the last conversation is captured
 * before the swap.
 */
export function useTrackSettingsReturn(): void {
  const { pathname, search } = useLocation();
  const { inSettings } = useSettingsRoute();
  useEffect(() => {
    if (!inSettings) settingsReturnPath = `${pathname}${search}`;
  }, [inSettings, pathname, search]);
}

/**
 * Settings nav rendered inside the sidebar card, replacing the conversation
 * list on /settings. Its Back row matches the navigation rows below it.
 */
export function SettingsSidebarBody({
  onNavClick,
}: {
  onNavClick: (e: React.MouseEvent<HTMLAnchorElement>) => void;
}) {
  const info = useServerInfo();
  // Account section shows whenever there's a login session (accounts OR OIDC).
  const hasAuthSession = info !== "loading" && info.login_url !== null;
  // Admin gating for the Members / Policies sub-categories. Sourced from
  // `/v1/me` (mode-agnostic) so the group appears for admins under OIDC too,
  // not just accounts deploys. Non-admins never see it.
  const isAdmin = useIsAdmin();
  const integrationsEnabled = info !== "loading" && (info.enabled_connections ?? []).length > 0;
  const { section } = useSettingsRoute();
  const groups = settingsNavGroups(
    hasAuthSession,
    isElectronShell(),
    isAdmin,
    isSingleUserMode(info),
    integrationsEnabled,
  );

  return (
    <>
      {/* settings-sidebar-header carries the same traffic-light clearance the
      home sidebar's header row gets: with the sidebar now starting at the
      window's top edge on the macOS shell, this Back row would otherwise sit
      underneath the window controls (see index.css). Inert elsewhere. */}
      <div className="settings-sidebar-header px-3 pt-3">
        <Button
          asChild
          variant="ghost"
          className={cn(
            SIDEBAR_ROW,
            "w-fit justify-start border-0 font-normal text-muted-foreground",
          )}
        >
          {/* Returns to wherever the user was before entering settings (the
          conversation they were viewing, or home) — see settingsReturnPath.
          No onNavClick here: on mobile the sidebar is a full-screen overlay.
          Leaving /settings swaps the sidebar back to the conversation list —
          but we keep the overlay OPEN so mobile lands on that list rather than
          closing onto the content behind it. On desktop onNavClick is a no-op
          (persistent card), so dropping it changes nothing there. */}
          <Link to={settingsReturnPath} componentId="settings.back_to_omnigent">
            <ArrowLeftIcon className="ui-icon" />
            Back
          </Link>
        </Button>
      </div>
      <nav className="flex flex-1 flex-col gap-4 overflow-y-auto px-3 py-3">
        {groups.map((group) => (
          <div key={group.title} className="flex flex-col gap-0">
            <h2 className="px-2 py-1 text-sm font-normal text-muted-foreground">{group.title}</h2>
            {group.items.map((item) => {
              const Icon = item.icon;
              const selected = section === item.id;
              return (
                <Button
                  key={item.id}
                  asChild
                  variant="ghost"
                  className={cn(
                    SIDEBAR_ROW,
                    "w-full justify-start border-0 font-normal",
                    selected &&
                      "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)] hover:bg-[var(--sidebar-active)] hover:text-[var(--sidebar-active-foreground)] dark:hover:bg-[var(--sidebar-active)] dark:hover:text-[var(--sidebar-active-foreground)]",
                    item.hideOnMobile && "max-md:hidden",
                  )}
                >
                  <Link
                    to={`/settings/${item.id}`}
                    onClick={onNavClick}
                    data-testid={`settings-nav-${item.id}`}
                    componentId={`settings.nav.${item.id}`}
                    aria-current={selected ? "page" : undefined}
                  >
                    <Icon
                      className={cn(
                        "ui-icon",
                        selected
                          ? "text-[var(--sidebar-active-foreground)]"
                          : "text-muted-foreground",
                      )}
                    />
                    {item.label}
                  </Link>
                </Button>
              );
            })}
          </div>
        ))}
      </nav>
    </>
  );
}
