// Test helpers for the thingy UI: a thingy fixture and a render with the providers the app
// supplies (queries, server capabilities with thingies on, tooltips, a router).
import type { ReactElement, ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { type InitialEntry, MemoryRouter, Route, Routes } from "react-router-dom";

import { TooltipProvider } from "@/components/ui/tooltip";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { useLocation } from "@/lib/routing";
import type { Thingy } from "@/thingies/api/thingyApi";

export function thingyFixture(overrides: Partial<Thingy> = {}): Thingy {
  return {
    id: "thingy-1",
    name: "Launch board",
    owner_id: "owner@example.com",
    viewer_id: "owner@example.com",
    published_revision: "rev-1",
    version: "1",
    permissions: { read: true, write_data: true, publish: true, manage: true },
    ...overrides,
  };
}

export const THINGIES_INFO: ServerInfo = {
  ...SERVER_INFO_OFFLINE_FALLBACK,
  features: { thingies: true },
};

/** Shows the router's current location, for asserting where a click navigated. */
export function LocationProbe() {
  const location = useLocation();
  return (
    <output aria-label="location" data-state={JSON.stringify(location.state ?? null)}>
      {`${location.pathname}${location.search}`}
    </output>
  );
}

export function renderWithThingies(
  element: ReactElement,
  {
    info = THINGIES_INFO,
    path = "/",
    route,
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    }),
  }: {
    info?: ServerInfo | "loading";
    path?: InitialEntry;
    /** A route pattern for `element`, so it can read its params. */
    route?: string;
    queryClient?: QueryClient;
  } = {},
) {
  const wrap = (children: ReactNode) => (
    <QueryClientProvider client={queryClient}>
      <CapabilitiesProvider info={info}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[path]}>
            {route ? (
              <Routes>
                <Route path={route} element={children} />
                <Route path="*" element={null} />
              </Routes>
            ) : (
              children
            )}
            <LocationProbe />
          </MemoryRouter>
        </TooltipProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
  const result = render(wrap(element));
  return { ...result, queryClient, rerender: (next: ReactElement) => result.rerender(wrap(next)) };
}
