/** Standard mounted Sidebar with a fresh query cache for each test; `setOpen` rerenders it. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { Sidebar } from "@/shell/Sidebar";

/**
 * `info` is the server info served; else `features` serves the fallback info with
 * those flags; with neither, info stays loading. `path` mounts the sidebar on that
 * route pattern, so its params (the open session) resolve from `route`.
 */
export function renderSidebar(
  props: {
    open?: boolean;
    onClose?: () => void;
    route?: string;
    path?: string;
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
  const tree = (open: boolean) => {
    const sidebar = <Sidebar open={open} onClose={onClose} dragProgress={props.dragProgress} />;
    return (
      <QueryClientProvider client={qc}>
        <CapabilitiesProvider info={info}>
          <SidebarDataProvider>
            <TooltipProvider>
              <MemoryRouter initialEntries={[props.route ?? "/"]}>
                {props.path ? (
                  <Routes>
                    <Route path={props.path} element={sidebar} />
                  </Routes>
                ) : (
                  sidebar
                )}
              </MemoryRouter>
            </TooltipProvider>
          </SidebarDataProvider>
        </CapabilitiesProvider>
      </QueryClientProvider>
    );
  };
  const view = render(tree(props.open ?? true));
  return { ...view, setOpen: (open: boolean) => view.rerender(tree(open)) };
}
