/** Standard mounted Sidebar with a fresh query cache for each test; `setOpen` rerenders it. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { Sidebar } from "@/shell/Sidebar";

export function renderSidebar(
  props: {
    open?: boolean;
    onClose?: () => void;
    route?: string;
    dragProgress?: number | null;
  } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onClose = props.onClose ?? vi.fn();
  const tree = (open: boolean) => (
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={[props.route ?? "/"]}>
            <Sidebar open={open} onClose={onClose} dragProgress={props.dragProgress} />
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>
  );
  const view = render(tree(props.open ?? true));
  return { ...view, setOpen: (open: boolean) => view.rerender(tree(open)) };
}
