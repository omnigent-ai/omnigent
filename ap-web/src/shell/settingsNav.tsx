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
  UserCogIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { cn } from "@/lib/utils";

type TFn = ReturnType<typeof useTranslation<"nav">>["t"];

export type SettingsSectionId = "appearance" | "shortcuts" | "account" | "archived";

const SECTION_IDS: readonly SettingsSectionId[] = [
  "appearance",
  "shortcuts",
  "account",
  "archived",
];

interface SettingsNavItem {
  id: SettingsSectionId;
  label: string;
  icon: typeof PaletteIcon;
}

interface SettingsNavGroup {
  title: string;
  items: SettingsNavItem[];
}

/** Nav groups for the current deploy — the Account section is auth-gated. */
export function settingsNavGroups(t: TFn, accountsEnabled: boolean): SettingsNavGroup[] {
  const general: SettingsNavItem[] = [
    { id: "appearance", label: t("settingsAppearance"), icon: PaletteIcon },
    { id: "shortcuts", label: t("settingsShortcuts"), icon: KeyboardIcon },
  ];
  if (accountsEnabled) {
    // Account leads the group when present — it's the most-visited section
    // on accounts deploys.
    general.unshift({ id: "account", label: t("settingsAccount"), icon: UserCogIcon });
  }
  return [
    { title: t("settingsGroupGeneral"), items: general },
    {
      title: t("archived"),
      items: [{ id: "archived", label: t("settingsArchivedSessions"), icon: ArchiveIcon }],
    },
  ];
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
  const { t } = useTranslation("nav");
  const info = useServerInfo();
  const accountsEnabled = info !== "loading" && info.accounts_enabled;
  const { section } = useSettingsRoute();
  const groups = settingsNavGroups(t, accountsEnabled);

  return (
    <>
      <div className="flex items-center justify-between px-3 pt-3">
        <Button asChild variant="ghost" size="sm" className="gap-2 text-muted-foreground">
          <Link to="/" onClick={onNavClick}>
            <ArrowLeftIcon className="size-4" />
            {t("backToOmnigent")}
          </Link>
        </Button>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={t("closeSidebar")}
              onClick={onClose}
              className="rounded-full"
            >
              <PanelRightOpenIcon className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{t("collapseSidebar")}</TooltipContent>
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
