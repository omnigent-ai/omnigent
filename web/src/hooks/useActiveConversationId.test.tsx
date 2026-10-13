import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { basenamedRouting, RoutingProvider, useNavigate } from "@/lib/routing";
import { useActiveConversationId } from "./useActiveConversationId";

afterEach(cleanup);

describe.each(["", "/embedded/omnigent"])("useActiveConversationId under %s", (basename) => {
  function wrapper(entry: string) {
    const routing = basenamedRouting(basename);
    return function Wrapper({ children }: { children: ReactNode }) {
      return (
        <MemoryRouter initialEntries={[`${basename}${entry}`]}>
          <RoutingProvider value={routing}>{children}</RoutingProvider>
        </MemoryRouter>
      );
    };
  }

  it.each([
    ["/c/session", "session"],
    ["/c/canvas", "canvas"],
    ["/canvas/c/session?canvas=project", "session"],
    ["/canvas/c/session/", "session"],
    ["/canvas/c/temp:123", "temp:123"],
    ["/c/temp%3A123", "temp:123"],
    ["/canvas/c/temp%3A123", "temp:123"],
    ["/canvas/c/session%20name", "session name"],
    ["/canvas/c/bad%E0%A4%A", "bad%E0%A4%A"],
    ["/", undefined],
    ["/canvas?canvas=project", undefined],
    ["/canvas/c/", undefined],
    ["/c/", undefined],
    ["/settings/c/session", undefined],
    ["/canvas/c/session/extra", undefined],
  ] as const)("reads the active session at %s", (entry, expected) => {
    const { result } = renderHook(useActiveConversationId, { wrapper: wrapper(entry) });
    expect(result.current).toBe(expected);
  });

  it("updates when switching cards and closing the conversation", () => {
    const { result } = renderHook(
      () => ({ id: useActiveConversationId(), navigate: useNavigate() }),
      { wrapper: wrapper("/c/first") },
    );
    expect(result.current.id).toBe("first");
    act(() => result.current.navigate("/canvas/c/second?canvas=project"));
    expect(result.current.id).toBe("second");
    act(() => result.current.navigate("/canvas?canvas=project"));
    expect(result.current.id).toBeUndefined();
  });
});
