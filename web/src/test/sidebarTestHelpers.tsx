/** Standard mounted Sidebar with a fresh query cache for each test; `setOpen` rerenders it. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { Sidebar } from "@/shell/Sidebar";

/**
 * `info` is the server info served; else `features` serves the fallback info with
 * those flags; with neither, info stays loading.
 */
export function renderSidebar(
  props: {
    open?: boolean;
    onClose?: () => void;
    route?: string;
    dragProgress?: number | null;
    features?: Record<string, boolean>;
    info?: ServerInfo;
  } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const info =
    props.info ??
    (props.features ? { ...FALLBACK_SERVER_INFO, features: props.features } : "loading");
  const onClose = props.onClose ?? vi.fn();
  const tree = (open: boolean) => (
    <QueryClientProvider client={qc}>
      <CapabilitiesProvider info={info}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={[props.route ?? "/"]}>
              <Sidebar open={open} onClose={onClose} dragProgress={props.dragProgress} />
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
  const view = render(tree(props.open ?? true));
  return { ...view, setOpen: (open: boolean) => view.rerender(tree(open)) };
}

/** Open the session filter funnel's Display submenu, which lists the filters. */
export function openSessionFilters() {
  fireEvent.pointerDown(screen.getByTestId("session-filter"), {
    button: 0,
    ctrlKey: false,
    pointerType: "mouse",
  });
  fireEvent.keyDown(screen.getByTestId("session-display-menu"), { key: "ArrowRight" });
}

/** Pick a session filter from the funnel. */
export function selectSessionFilter(value: "all" | "mine" | "shared" | "archived") {
  openSessionFilters();
  fireEvent.click(screen.getByTestId(`session-filter-${value}`));
}

/** Enter selection mode from the Projects header, which selects every list together. */
export function selectSessionsFromProjects() {
  const projects = screen.getByRole("button", { name: "Projects" }).closest("section")!;
  fireEvent.click(within(projects).getByTestId("toggle-selection-mode"));
}
