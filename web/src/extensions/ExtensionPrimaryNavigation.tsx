import {
  LayoutDashboardIcon,
  PanelsTopLeftIcon,
  PuzzleIcon,
  SearchIcon,
  type LucideIcon,
} from "lucide-react";
import type { MouseEvent } from "react";
import { PrimaryNavLink } from "@/shell/PrimaryNavLink";
import { useExtensions } from "./ExtensionProvider";

const ICONS: Record<string, LucideIcon> = {
  dashboard: LayoutDashboardIcon,
  "panels-top-left": PanelsTopLeftIcon,
  puzzle: PuzzleIcon,
  search: SearchIcon,
};

export function ExtensionPrimaryNavigation({
  activePageId,
  onNavigate,
}: {
  activePageId: string | null;
  onNavigate: (event: MouseEvent<HTMLAnchorElement>) => void;
}) {
  // V1 owns one slot between Inbox and Usage. `order` is deterministic within
  // that extension-owned slot; it does not reorder core navigation rows.
  const entries = useExtensions()
    .flatMap((extension) =>
      extension.primary_navigation.map((navigation) => ({ extension, navigation })),
    )
    .sort(
      (left, right) =>
        left.navigation.order - right.navigation.order ||
        left.navigation.id.localeCompare(right.navigation.id),
    );

  return entries.map(({ extension, navigation }) => {
    const page = extension.pages.find((item) => item.id === navigation.page);
    if (!page) return null;
    return (
      <PrimaryNavLink
        key={navigation.id}
        to={`/extensions/${extension.id}/${page.route}`}
        label={navigation.label}
        icon={ICONS[navigation.icon ?? ""] ?? PuzzleIcon}
        active={activePageId === page.id}
        onClick={onNavigate}
        componentId={`sidebar.extension.${navigation.id}`}
        testId={`extension-nav-${navigation.id}`}
      />
    );
  });
}
