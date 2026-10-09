import {
  ClockIcon,
  InboxIcon,
  LayoutDashboardIcon,
  MessageCirclePlusIcon,
  PanelLeftOpenIcon,
  WalletIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ExtensionPrimaryNavigation } from "@/extensions/ExtensionPrimaryNavigation";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useSearchParams } from "@/lib/routing";
import { PrimaryNavLink } from "@/shell/PrimaryNavLink";
import { SidebarSearchButton, SidebarSettingsButton } from "@/shell/SidebarHeaderActions";
import { MAIN_CANVAS_ID } from "./canvasLayout";
import { CANVAS_QUERY_PARAM, canvasLocation } from "./canvasNavigation";

/** The collapsed navigation stays available beside the Canvas workspace. */
export function CanvasSidebarRail({
  onExpand,
  onSearch,
}: {
  onExpand: () => void;
  onSearch: () => void;
}) {
  const [searchParams] = useSearchParams();
  const info = useServerInfo();
  const canvas = canvasLocation(searchParams.get(CANVAS_QUERY_PARAM) ?? MAIN_CANVAS_ID);
  const links = [
    { label: "New session", to: "/", Icon: MessageCirclePlusIcon, componentId: "sidebar.new_chat" },
    { label: "Automations", to: "/tasks", Icon: ClockIcon, componentId: "sidebar.tasks" },
    { label: "Inbox", to: "/inbox", Icon: InboxIcon, componentId: "sidebar.inbox" },
    {
      label: "Canvas",
      to: canvas.pathname + canvas.search,
      Icon: LayoutDashboardIcon,
      componentId: "sidebar.canvas",
    },
  ];
  return (
    <nav
      aria-label="Collapsed sidebar"
      data-testid="canvas-sidebar-rail"
      className="canvas-sidebar-rail flex w-12 shrink-0 flex-col items-center gap-2 border-r bg-sidebar px-1 pt-[calc(var(--omnigent-inset-top)+8px)] pb-3 md:w-14"
    >
      <Button
        variant="ghost"
        size="icon"
        aria-label="Expand sidebar"
        title="Expand sidebar"
        onClick={onExpand}
      >
        <PanelLeftOpenIcon className="size-4" />
      </Button>
      <SidebarSearchButton onOpenSearch={onSearch} className="size-8" />
      <SidebarSettingsButton className="size-8" />
      <div className="flex flex-col items-center gap-2" data-testid="canvas-sidebar-primary-nav">
        {links.map(({ label, to, Icon, componentId }) => (
          <PrimaryNavLink
            key={componentId}
            to={to}
            label={label}
            icon={Icon}
            active={componentId === "sidebar.canvas"}
            componentId={componentId}
            compact
          />
        ))}
        <ExtensionPrimaryNavigation activePageId={null} compact />
        {isFeatureEnabled(info, "usage_page") && (
          <PrimaryNavLink
            to="/usage"
            label="Usage"
            icon={WalletIcon}
            active={false}
            componentId="sidebar.usage"
            compact
          />
        )}
      </div>
    </nav>
  );
}
