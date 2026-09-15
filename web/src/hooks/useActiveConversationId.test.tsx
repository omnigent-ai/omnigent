import { cleanup, renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import type { ReactNode } from "react";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { basenamedRouting, RoutingProvider } from "@/lib/routing";
import { useSessionRoute } from "./useActiveConversationId";

afterEach(cleanup);

describe("visible session route", () => {
  it.each(["", "/mount"])("matches only real session routes under %s", (basename) => {
    for (const [path, enabled, expected] of [
      ["/c/one", false, "one"],
      ["/C/UpperCaseId", false, "UpperCaseId"],
      ["/Canvas?session=UpperCaseId", true, "UpperCaseId"],
      ["/c/temp:local", false, "temp:local"],
      ["/c/encoded%20id/", false, "encoded id"],
      ["/c/bad%XX", false, "bad%XX"],
      ["/c/one/extra", true, undefined],
      ["/canvas?session=one", true, "one"],
      ["/canvas/?session=child&canvas=board", true, "child"],
      ["/canvas?session=one", false, undefined],
      ["/canvas?session=", true, undefined],
      ["/canvas", true, undefined],
      ["/canvas-other?session=one", true, undefined],
      ["/settings?session=one", true, undefined],
      ["/another/canvas?session=one", true, undefined],
    ] as const) {
      const { result, unmount } = renderHook(useSessionRoute, {
        wrapper: ({ children }: { children: ReactNode }) => (
          <MemoryRouter initialEntries={[`${basename}${path}`]}>
            <RoutingProvider value={basenamedRouting(basename)}>
              <CapabilitiesProvider
                info={{ ...FALLBACK_SERVER_INFO, features: { canvas: enabled } }}
              >
                {children}
              </CapabilitiesProvider>
            </RoutingProvider>
          </MemoryRouter>
        ),
      });
      expect(result.current.conversationId, path).toBe(expected);
      unmount();
    }
  });

  it("does not activate a Canvas session while server capabilities are loading", () => {
    const { result } = renderHook(useSessionRoute, {
      wrapper: ({ children }) => (
        <MemoryRouter initialEntries={["/canvas?session=one"]}>{children}</MemoryRouter>
      ),
    });
    expect(result.current).toEqual({ isCanvas: false, conversationId: undefined });
  });
});
