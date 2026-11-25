import { act, render, renderHook } from "@testing-library/react";
import { createElement, useEffect } from "react";
import { describe, expect, it } from "vitest";

import {
  type ActivityRevealRequest,
  type ActivityRevealScope,
  useActivityRevealRequest,
} from "./activityReveal";

const SCOPE: ActivityRevealScope = {
  conversationId: "conv_1",
  prUrl: "https://github.com/acme/app/pull/7",
  account: "octocat",
};

function renderRequests(scope: ActivityRevealScope | null = SCOPE) {
  return renderHook((current: ActivityRevealScope | null) => useActivityRevealRequest(current), {
    initialProps: scope,
  });
}

describe("useActivityRevealRequest", () => {
  it("keeps the newest request, for the scope it was made in, until it's settled", () => {
    const { result } = renderRequests();
    act(() => result.current.reveal({ kind: "entry", key: "thread:a" }));
    act(() => result.current.reveal({ kind: "section" }));
    expect(result.current.request).toEqual({ id: 2, scope: SCOPE, target: { kind: "section" } });
    // Settling an older request leaves the newer one waiting.
    act(() => result.current.settle(1));
    expect(result.current.request?.id).toBe(2);
    act(() => result.current.settle(2));
    expect(result.current.request).toBeNull();
  });

  it.each([
    ["another pull request", { prUrl: "https://github.com/acme/app/pull/8" }],
    ["another reader", { account: "hubot" }],
    ["another pane", { conversationId: "conv_2" }],
  ])("drops a waiting request on %s", (_name, change) => {
    const { result, rerender } = renderRequests();
    act(() => result.current.reveal({ kind: "posted", key: "posted" }));
    rerender({ ...SCOPE, ...change });
    expect(result.current.request).toBeNull();
  });

  it("never hands a request to a child in the render where the reader changes", () => {
    const consumed: ActivityRevealRequest[] = [];
    let reveal: ReturnType<typeof useActivityRevealRequest>["reveal"] = () => {};
    // A child's effects run before its parent's, as Activity's do inside the panel; it acts on
    // a request once its reads are in, as Activity does for a posted comment.
    function Consumer({ request, read }: { request: ActivityRevealRequest | null; read: boolean }) {
      useEffect(() => {
        if (read && request) consumed.push(request);
      }, [read, request]);
      return null;
    }
    function Parent({ scope, read }: { scope: ActivityRevealScope; read: boolean }) {
      const requests = useActivityRevealRequest(scope);
      reveal = requests.reveal;
      return createElement(Consumer, { request: requests.request, read });
    }
    const view = render(createElement(Parent, { scope: SCOPE, read: false }));
    act(() => reveal({ kind: "posted", key: "posted" }));
    // The new reader's read arrives in the same render as the reader itself.
    view.rerender(createElement(Parent, { scope: { ...SCOPE, account: "hubot" }, read: true }));
    expect(consumed).toEqual([]);
  });

  it("keeps a request through a render with the same scope", () => {
    const { result, rerender } = renderRequests();
    act(() => result.current.reveal({ kind: "posted", key: "posted" }));
    rerender({ ...SCOPE });
    expect(result.current.request?.target).toEqual({ kind: "posted", key: "posted" });
  });

  it("makes no request where there's no Activity to show it", () => {
    const { result } = renderRequests(null);
    act(() => result.current.reveal({ kind: "section" }));
    expect(result.current.request).toBeNull();
  });
});
