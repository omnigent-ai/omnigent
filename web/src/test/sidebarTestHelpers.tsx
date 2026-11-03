/** Standard mounted Sidebar with a fresh query cache for each test. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { Sidebar } from "@/shell/Sidebar";

/** `features` serves the fallback server info with those flags; omitted, info stays loading. */
export function renderSidebar(
  props: {
    open?: boolean;
    onClose?: () => void;
    route?: string;
    features?: Record<string, boolean>;
  } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const info = props.features ? { ...FALLBACK_SERVER_INFO, features: props.features } : "loading";
  return render(
    <QueryClientProvider client={qc}>
      <CapabilitiesProvider info={info}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={[props.route ?? "/"]}>
              <Sidebar open={props.open ?? true} onClose={props.onClose ?? vi.fn()} />
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}
