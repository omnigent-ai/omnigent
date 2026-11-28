/** Standard mounted Sidebar with a fresh query cache for each test; `setOpen` rerenders it. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
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
    /** Match `route` against this path, as the app's routes do, so the sidebar reads its params. */
    routePattern?: string;
    dragProgress?: number | null;
    features?: Record<string, boolean>;
    info?: ServerInfo;
    /** Rendered inside the router after the sidebar, e.g. a location probe. */
    children?: ReactNode;
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
              {props.routePattern ? (
                <Routes>
                  <Route
                    path={props.routePattern}
                    element={
                      <Sidebar open={open} onClose={onClose} dragProgress={props.dragProgress} />
                    }
                  />
                </Routes>
              ) : (
                <Sidebar open={open} onClose={onClose} dragProgress={props.dragProgress} />
              )}
              {props.children}
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
  const view = render(tree(props.open ?? true));
  return { ...view, setOpen: (open: boolean) => view.rerender(tree(open)) };
}
