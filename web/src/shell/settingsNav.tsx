// Shared model + sidebar body for the Settings surface.
//
// Entering /settings doesn't swap out the conversations sidebar card — the
// SAME card just renders this nav in place of the conversation list, while
// the main area shows the selected section's content (SettingsPage). Section
// selection is URL-driven (/settings/<section>) so the nav (in the sidebar)
// and the content (in the outlet) stay in sync without shared state.

import {
  ArchiveIcon,
  ArrowLeftIcon,
  KeyboardIcon,
  PaletteIcon,
  PanelRightOpenIcon,
  ShieldCheckIcon,
  TerminalIcon,
  UserCogIcon,
  UsersIcon,
} from "lucide-react";
import { Link, useLocation } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useMe } from "@/hooks/useMe";
import { isElectronShell } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";

export type SettingsSectionId =
  | "appearance"
  | "shortcuts"
  | "account"
  | "members"
  | "policies"
  | "archived"
  | "cli";

const SECTION_IDS: readonly SettingsSectionId[] = [
  "appearance",
  "shortcuts",
  "account",
  "members",
  "policies",
  "archived",
  "cli",
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
 * Nav groups for the current deploy. The Account section is auth-gated; the
 * Admin group (Members / Policies) appears only for admins on accounts
 * deploys; the Desktop group (Local CLI) appears only in the Electron shell.
 */
export function settingsNavGroups(
  accountsEnabled: boolean,
  isDesktop: boolean,
  isAdmin = false,
): SettingsNavGroup[] {
  const general: SettingsNavItem[] = [
    { id: "appearance", label: "Appearance", icon: PaletteIcon },
    { id: "shortcuts", label: "Keyboard shortcuts", icon: KeyboardIcon, hideOnMobile: true },
  ];
  if (accountsEnabled) {
    // Account leads the group when present — it's the most-visited section
    // on accounts deploys.
    general.unshift({ id: "account", label: "Account", icon: UserCogIcon });
  }
  const groups: SettingsNavGroup[] = [];
  // Desktop (Local CLI) leads when present — it's the shell-specific section a
  // desktop user is most likely here to change.
  if (isDesktop) {
    groups.push({
      title: "Desktop",
      items: [{ id: "cli", label: "Local CLI", icon: TerminalIcon }],
    });
  }
  groups.push({ title: "General", items: general });
  // Admin: server-wide management, admin-only. Nested here as sub-categories
  // (rather than links out of the Account section) so entering them stays
  // inside /settings — the sidebar keeps the settings nav instead of snapping
  // back to the conversation list.
  if (accountsEnabled && isAdmin) {
    groups.push({
      title: "Admin",
      items: [
        { id: "members", label: "Members", icon: UsersIcon },
        { id: "policies", label: "Policies", icon: ShieldCheckIcon },
      ],
    });
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
 * section segment) defaults to Account when accounts auth is on — the most
 * relevant landing there — and Appearance otherwise. Basename-agnostic —
 * matches the `settings` segment wherever it lands, same approach as the
 * sidebar's top-level nav detection.
 */
export function useSettingsRoute(): { inSettings: boolean; section: SettingsSectionId } {
  const info = useServerInfo();
  const accountsEnabled = info !== "loading" && info.accounts_enabled;
  const defaultSection: SettingsSectionId = accountsEnabled ? "account" : "appearance";

  const segments = useLocation().pathname.split("/").filter(Boolean);
  const idx = segments.lastIndexOf("settings");
  if (idx === -1) return { inSettings: false, section: defaultSection };
  const next = segments[idx + 1];
  // Members / Policies are accounts-only sections. Off an accounts deploy
  // they aren't real destinations — fall back to the default section rather
  // than resolving to an admin section the page would render as an empty
  // panel (only reachable by manually typing the URL, but keep it clean).
  const accountsOnlySections = new Set<string>(["members", "policies"]);
  const isValidSection =
    (SECTION_IDS as readonly string[]).includes(next) &&
    (accountsEnabled || !accountsOnlySections.has(next));
  const section = isValidSection ? (next as SettingsSectionId) : defaultSection;
  return { inSettings: true, section };
}

/**
 * Settings nav rendered INSIDE the sidebar card (replacing the conversation
 * list on /settings). Keeps the card chrome — a top row with "Back to
 * Omnigent" and the same collapse control the conversations view uses.
 */
export function SettingsSidebarBody({
  onNavClick,
  onClose,
}: {
  onNavClick: (e: React.MouseEvent<HTMLAnchorElement>) => void;
  onClose: () => void;
}) {
  const info = useServerInfo();
  const accountsEnabled = info !== "loading" && info.accounts_enabled;
  // Admin gating for the Members / Policies sub-categories. Only probed on
  // accounts deploys; non-admins (and non-accounts deploys) never see them.
  const { data: me } = useMe(accountsEnabled);
  const { section } = useSettingsRoute();
  const groups = settingsNavGroups(accountsEnabled, isElectronShell(), me?.is_admin ?? false);

  return (
    <>
      <div className="flex items-center justify-between px-3 pt-3">
        <Button asChild variant="ghost" size="sm" className="gap-2 text-muted-foreground">
          {/* No onNavClick here: on mobile the sidebar is a full-screen
          overlay. Navigating to "/" exits /settings, so the sidebar swaps
          back to the conversation list — but we keep the overlay OPEN so
          mobile lands on that list rather than closing onto the homepage
          content behind it. On desktop onNavClick is a no-op (persistent
          card), so dropping it changes nothing there. */}
          <Link to="/">
            <ArrowLeftIcon className="size-4" />
            Back to Omnigent
          </Link>
        </Button>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Close sidebar"
              onClick={onClose}
              className="rounded-full"
            >
              <PanelRightOpenIcon className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Collapse sidebar</TooltipContent>
        </Tooltip>
      </div>
      <nav className="flex flex-1 flex-col gap-4 overflow-y-auto px-3 py-3">
        {groups.map((group) => (
          <div key={group.title} className="flex flex-col gap-0.5">
            <h2 className="px-2 py-1 text-muted-foreground text-xs font-medium uppercase tracking-wide">
              {group.title}
            </h2>
            {group.items.map((item) => {
              const Icon = item.icon;
              const selected = section === item.id;
              return (
                <Button
                  key={item.id}
                  asChild
                  variant="ghost"
                  className={cn(
                    "w-full justify-start gap-2 text-sm",
                    selected && "bg-muted font-semibold",
                    item.hideOnMobile && "max-md:hidden",
                  )}
                >
                  <Link
                    to={`/settings/${item.id}`}
                    onClick={onNavClick}
                    data-testid={`settings-nav-${item.id}`}
                    aria-current={selected ? "page" : undefined}
                  >
                    <Icon className="size-4 text-muted-foreground" />
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
