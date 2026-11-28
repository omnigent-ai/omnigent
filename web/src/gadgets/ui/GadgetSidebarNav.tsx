// Gadget in the sidebar's primary navigation, with the viewer's pinned gadgets under it,
// indented like sessions under a project. A pinned row opens its gadget full screen and keeps the
// sidebar open. While a gadget fills the screen, its pinned row (or Gadget, for one that isn't
// pinned) is the current item.
import type { MouseEvent } from "react";
import { PanelsTopLeftIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Link, useLocation, useRebasePath } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { useGadgetRailRequest } from "@/lib/gadgetRailTabs";
import { PrimaryNavLink } from "@/shell/PrimaryNavLink";
import { SIDEBAR_HOVER_HIGHLIGHT, SIDEBAR_ROW } from "@/shell/sidebarStyles";
import { gadgetBoardPath } from "@/gadgets/host/gadgetPages";
import { IN_APP_GADGET_OPEN } from "./gadgetNavigation";
import { useGadgetStartId } from "./GadgetRailTabs";
import { useLinkedGadgetsSync, useGadgetsEnabled, useGadgetShelf } from "./gadgetQueries";

/** The gadget filling the screen, in a session's side panel or beside New session, or null. */
export function useFullScreenGadgetId(): string | null {
  const inRail = useGadgetRailRequest((state) => state.fullScreenGadgetId);
  const newSessionMaximized = useGadgetRailRequest((state) => state.newSessionMaximized);
  const startId = useGadgetStartId();
  return inRail ?? (newSessionMaximized ? startId : null);
}

export function GadgetSidebarNav({
  active,
  onNavigate,
}: {
  /** Whether a Gadget page is open. */
  active: boolean;
  onNavigate?: (event: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const enabled = useGadgetsEnabled();
  const shelf = useGadgetShelf();
  const { pathname } = useLocation();
  const rebasePath = useRebasePath();
  const fullScreenId = useFullScreenGadgetId();
  useLinkedGadgetsSync();
  if (!enabled) return null;
  const pinned = shelf.data?.pinned ?? [];
  const isCurrent = (pinId: string) =>
    rebasePath(gadgetBoardPath(pinId)) === pathname || fullScreenId === pinId;
  const gadgetCurrent =
    (active || fullScreenId !== null) && pinned.every((pin) => !isCurrent(pin.id));
  return (
    <>
      <PrimaryNavLink
        to="/gadget"
        label="Gadget"
        icon={PanelsTopLeftIcon}
        active={gadgetCurrent}
        onClick={onNavigate}
        componentId="sidebar.gadgets"
        testId="gadgets-nav"
      />
      {pinned.length > 0 && (
        <ul className="m-0 flex list-none flex-col p-0" aria-label="Pinned gadgets">
          {pinned.map((pin) => {
            const current = isCurrent(pin.id);
            return (
              <li key={pin.id}>
                <Button
                  asChild
                  variant="ghost"
                  className={cn(
                    SIDEBAR_ROW,
                    "w-full justify-start border-0 pl-8 font-normal text-muted-foreground mobile-touch-target",
                    SIDEBAR_HOVER_HIGHLIGHT,
                    current &&
                      "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)] hover:bg-[var(--sidebar-active)] hover:text-[var(--sidebar-active-foreground)]",
                  )}
                >
                  <Link
                    to={gadgetBoardPath(pin.id)}
                    state={IN_APP_GADGET_OPEN}
                    onClick={onNavigate}
                    componentId="sidebar.gadgets.pinned"
                    aria-current={current ? "page" : undefined}
                    title={pin.name}
                  >
                    <span className="truncate">{pin.name}</span>
                  </Link>
                </Button>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
