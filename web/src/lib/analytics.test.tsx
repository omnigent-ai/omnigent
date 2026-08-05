import { afterEach, describe, expect, it, vi } from "vitest";
import { render, renderHook, act } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import type { ReactNode } from "react";

import { setOmnigentHostConfig, type OmnigentAnalyticsEvent } from "@/lib/host";
import { emitOmnigentAnalytics, useOmnigentAnalytics, useOmnigentPageView } from "@/lib/analytics";

afterEach(() => {
  // Reset the module-singleton host config between tests. The empty-config
  // guard only blocks clobbering a fetcher, so an analytics-only reset clears.
  setOmnigentHostConfig({});
});

describe("emitOmnigentAnalytics", () => {
  it("is a no-op when no host sink is configured", () => {
    // No throw, nothing to observe — standalone behavior.
    expect(() => emitOmnigentAnalytics({ type: "click", componentId: "x" })).not.toThrow();
  });

  it("forwards the event to the host sink when configured", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const event: OmnigentAnalyticsEvent = {
      type: "click",
      componentId: "x",
      componentKind: "button",
    };
    emitOmnigentAnalytics(event);
    expect(analytics).toHaveBeenCalledExactlyOnceWith(event);
  });
});

describe("useOmnigentAnalytics", () => {
  it("redacts field values by default and forwards only when declared PII-free", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const { result } = renderHook(() => useOmnigentAnalytics());

    result.current.trackValueChange("field", "input", "secret text");
    expect(analytics).toHaveBeenLastCalledWith({
      type: "value_change",
      componentId: "field",
      componentKind: "input",
      value: undefined,
    });

    result.current.trackValueChange("filter", "select", "active", { valueHasNoPii: true });
    expect(analytics).toHaveBeenLastCalledWith({
      type: "value_change",
      componentId: "filter",
      componentKind: "select",
      value: "active",
    });
  });
});

describe("useOmnigentPageView", () => {
  // useOmnigentPageView reads useLocation, so it needs a router. Render at a
  // fixed path and let a rerender's `path` prop drive navigation.
  const atPath = (path: string) => {
    const Wrapper = ({ children }: { children: ReactNode }) => (
      <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
    );
    return Wrapper;
  };

  it("fires exactly one page_view on mount", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    renderHook(() => useOmnigentPageView("chat"), { wrapper: atPath("/c/a") });
    expect(analytics).toHaveBeenCalledExactlyOnceWith({ type: "page_view", pageId: "chat" });
  });

  it("does not re-fire on re-render at the same path", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const { rerender } = renderHook(() => useOmnigentPageView("chat"), { wrapper: atPath("/c/a") });
    rerender();
    expect(analytics).toHaveBeenCalledTimes(1);
  });

  it("re-fires under the same pageId when the pathname changes (chat /c/a -> /c/b)", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    // One mounted component (like ChatPage) that navigates between conversations
    // in place. The hook must re-emit on the pathname change even though pageId
    // stays "chat" — the regression this guards against.
    let navigate: ReturnType<typeof useNavigate>;
    function ChatLike() {
      useOmnigentPageView("chat");
      navigate = useNavigate();
      return null;
    }
    render(
      <MemoryRouter initialEntries={["/c/a"]}>
        <ChatLike />
      </MemoryRouter>,
    );
    expect(analytics).toHaveBeenCalledTimes(1);
    act(() => navigate("/c/b"));
    expect(analytics).toHaveBeenCalledTimes(2);
    expect(analytics).toHaveBeenLastCalledWith({ type: "page_view", pageId: "chat" });
  });
});
