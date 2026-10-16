import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { basenamedRouting, RoutingProvider, useLocation, useNavigate } from "@/lib/routing";
import { handleSessionEvent, useChatStore } from "@/store/chatStore";
import { useActiveConversationId } from "./useActiveConversationId";
import { useConversationRedirect } from "./useConversationRedirect";

const initialState = useChatStore.getState();

afterEach(() => {
  cleanup();
  useChatStore.setState(initialState, true);
});

describe.each(["", "/embedded/omnigent"])("session replacement under %s", (basename) => {
  it.each([
    ["/c/conv_old", "/c/conv_new", ""],
    ["/canvas/c/conv_old?canvas=project&debug=1", "/canvas/c/conv_new", "?canvas=project&debug=1"],
  ])("keeps the conversation surface at %s", (entry, pathname, search) => {
    const routing = basenamedRouting(basename);
    useChatStore.setState({ conversationId: "conv_old", redirectToConversationId: null });
    function Wrapper({ children }: { children: ReactNode }) {
      return (
        <MemoryRouter initialEntries={[`${basename}/c/before`, `${basename}${entry}`]}>
          <RoutingProvider value={routing}>{children}</RoutingProvider>
        </MemoryRouter>
      );
    }
    const { result } = renderHook(
      () => {
        useConversationRedirect(useActiveConversationId());
        return { location: useLocation(), navigate: useNavigate() };
      },
      { wrapper: Wrapper },
    );
    act(() =>
      handleSessionEvent({
        type: "session_superseded",
        conversationId: "conv_old",
        targetConversationId: "conv_new",
        reason: "clear",
      }),
    );
    expect(result.current.location.pathname).toBe(`${basename}${pathname}`);
    expect(result.current.location.search).toBe(search);
    expect(useChatStore.getState().redirectToConversationId).toBeNull();
    act(() => result.current.navigate(-1));
    expect(result.current.location.pathname).toBe(`${basename}/c/before`);
  });
});
