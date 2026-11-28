// The sidebar's optional destinations, Canvas and Usage: one place decides
// when each is offered and where it goes, for the sidebar's nav links and ⌘K alike.

import { useMemo } from "react";
import { LayoutDashboardIcon, WalletIcon } from "lucide-react";

import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { MAIN_CANVAS_ID } from "@/canvas/canvasLayout";
import { CANVAS_QUERY_PARAM, canvasLocation } from "@/canvas/canvasNavigation";
import { useNavigate, useSearchParams } from "@/lib/routing";
import type { PaletteCommand } from "@/store/paletteCommands";

import { useQuickPickPaletteCommands } from "./screenCommands/useQuickPickPaletteCommands";

export interface SidebarNavDestinations {
  canvas: { enabled: boolean; to: string };
  usage: { enabled: boolean; to: string };
}

export function useSidebarNavDestinations(): SidebarNavDestinations {
  const serverInfo = useServerInfo();
  const [searchParams] = useSearchParams();
  const canvasId = searchParams.get(CANVAS_QUERY_PARAM) ?? MAIN_CANVAS_ID;
  const canvasEnabled = isFeatureEnabled(serverInfo, "canvas");
  const usageEnabled = isFeatureEnabled(serverInfo, "usage_page");
  return useMemo(() => {
    const canvas = canvasLocation(canvasId);
    return {
      canvas: { enabled: canvasEnabled, to: canvas.pathname + canvas.search },
      usage: { enabled: usageEnabled, to: "/usage" },
    };
  }, [canvasId, canvasEnabled, usageEnabled]);
}

/** "Go to Canvas" and "Go to Usage", while the sidebar offers them. */
export function sidebarNavCommands(
  destinations: SidebarNavDestinations,
  navigate: (to: string) => void,
): PaletteCommand[] {
  const commands: PaletteCommand[] = [];
  if (destinations.canvas.enabled) {
    commands.push({
      id: "go-canvas",
      label: "Go to Canvas",
      icon: LayoutDashboardIcon,
      keywords: ["canvas", "board", "grid", "overview"],
      run: () => navigate(destinations.canvas.to),
    });
  }
  if (destinations.usage.enabled) {
    commands.push({
      id: "go-usage",
      label: "Go to Usage",
      icon: WalletIcon,
      keywords: ["usage", "cost", "tokens", "spend", "billing"],
      run: () => navigate(destinations.usage.to),
    });
  }
  return commands;
}

export function useSidebarNavCommands(destinations: SidebarNavDestinations): void {
  const navigate = useNavigate();
  const commands = useMemo(
    () => sidebarNavCommands(destinations, navigate),
    [destinations, navigate],
  );
  useQuickPickPaletteCommands("sidebar-nav", "Go to", commands);
}
