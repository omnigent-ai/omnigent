// Shared model + sidebar body for the Settings surface.
//
// Entering /settings doesn't swap out the conversations sidebar card — the
// SAME card just renders this nav in place of the conversation list, while
// the main area shows the selected section's content (SettingsPage). Section
// selection is URL-driven (/settings/<section>) so the nav (in the sidebar)
// and the content (in the outlet) stay in sync without shared state.

import { useEffect, useState } from "react";
import {
  ArchiveIcon,
  ArrowLeftIcon,
  KeyboardIcon,
  PaletteIcon,
  PanelRightOpenIcon,
  ShieldCheckIcon,
  UserCogIcon,
} from "lucide-react";
import { Link, useLocation } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getControlPlaneMe } from "@/lib/controlPlaneApi";
import { cn } from "@/lib/utils";

export type SettingsSectionId = "appearance" | "shortcuts" | "account" | "archived" | "admin";

const SECTION_IDS: readonly SettingsSectionId[] = [
  "appearance",
  "shortcuts",
  "account",
  "archived",
  "admin",
];

/**
 * Whether to surface the control-plane Admin nav entry. Probes
 * ``GET /v1/control-plane/me`` once: shown only for admin / contributor
 * (consumers — and every non-control-plane deploy, where the probe 404s
 * — get ``false``, so the entry stays hidden). The Admin PAGE itself is
 * always routable and self-gates; this only governs the nav affordance.
 */
function useShowAdminNav(): boolean {
  const [show, setShow] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const result = await getControlPlaneMe();
      if (cancelled) return;
      setShow(result.ok && (result.me.role === "admin" || result.me.role === "contributor"));
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  return show;
}

interface SettingsNavItem {
  id: SettingsSectionId;
  label: string;
  icon: typeof PaletteIcon;
  /** Hide this item on mobile (e.g. keyboard shortcuts on a touch device). */
  hideOnMobile?: boolean;
  /**
   * Override the navigation target. Settings sections route to
   * ``/settings/<id>``; the Admin entry instead links to the top-level
   * ``/admin`` route (it's a full page outside the settings outlet).
   */
  to?: string;
}

interface SettingsNavGroup {
  title: string;
  items: SettingsNavItem[];
}

/**
 * Nav groups for the current deploy — the Account section is auth-gated,
 * and the Admin entry is gated on the control-plane probe (``showAdmin``).
 */
export function settingsNavGroups(
  accountsEnabled: boolean,
  showAdmin: boolean,
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
  const groups: SettingsNavGroup[] = [
    { title: "General", items: general },
    {
      title: "Archived",
      items: [{ id: "archived", label: "Archived sessions", icon: ArchiveIcon }],
    },
  ];
  if (showAdmin) {
    // Admin is its own group with a link out to the /admin page (the GTM
    // control-plane surface), shown only for admin / contributor.
    groups.push({
      title: "Admin",
      items: [{ id: "admin", label: "Admin", icon: ShieldCheckIcon, to: "/admin" }],
    });
  }
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
  const section = (SECTION_IDS as readonly string[]).includes(next)
    ? (next as SettingsSectionId)
    : defaultSection;
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
  const showAdmin = useShowAdminNav();
  const { section } = useSettingsRoute();
  const pathname = useLocation().pathname;
  const groups = settingsNavGroups(accountsEnabled, showAdmin);

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
              // Items with an explicit `to` (e.g. Admin → /admin) live outside
              // the /settings/<section> space, so match on the pathname; the
              // rest are section-driven.
              const selected = item.to ? pathname === item.to : section === item.id;
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
                    to={item.to ?? `/settings/${item.id}`}
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
