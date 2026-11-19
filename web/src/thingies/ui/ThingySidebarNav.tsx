// Thingy in the sidebar's primary navigation, with the viewer's pinned thingies under it,
// indented like sessions under a project. A pinned row opens its thingy full screen and keeps the
// sidebar open.
import type { MouseEvent } from "react";
import { PanelsTopLeftIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Link, useLocation, useRebasePath } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { PrimaryNavLink } from "@/shell/PrimaryNavLink";
import { SIDEBAR_HOVER_HIGHLIGHT, SIDEBAR_ROW } from "@/shell/sidebarStyles";
import { thingyBoardPath } from "@/thingies/host/thingyPages";
import type { ThingyOpenState } from "./ThingyOpenPage";
import { useLinkedThingiesSync, useThingiesEnabled, useThingyShelf } from "./thingyQueries";

const PINNED_OPEN_STATE: ThingyOpenState = { keepSidebar: true };

export function ThingySidebarNav({
  active,
  onNavigate,
}: {
  /** Whether a Thingy page is open. */
  active: boolean;
  onNavigate?: (event: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const enabled = useThingiesEnabled();
  const shelf = useThingyShelf();
  const { pathname } = useLocation();
  const rebasePath = useRebasePath();
  useLinkedThingiesSync();
  if (!enabled) return null;
  const pinned = shelf.data?.pinned ?? [];
  return (
    <>
      <PrimaryNavLink
        to="/thingy"
        label="Thingy"
        icon={PanelsTopLeftIcon}
        active={active && pinned.every((pin) => rebasePath(thingyBoardPath(pin.id)) !== pathname)}
        onClick={onNavigate}
        componentId="sidebar.thingies"
        testId="thingies-nav"
      />
      {pinned.length > 0 && (
        <ul className="m-0 flex list-none flex-col p-0" aria-label="Pinned thingies">
          {pinned.map((pin) => {
            const current = rebasePath(thingyBoardPath(pin.id)) === pathname;
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
                    to={thingyBoardPath(pin.id)}
                    state={PINNED_OPEN_STATE}
                    onClick={onNavigate}
                    componentId="sidebar.thingies.pinned"
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
