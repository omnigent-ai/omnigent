import { act, cleanup, render } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { basenamedRouting, RoutingProvider } from "@/lib/routing";

import { isOpenSession, usePublishOpenSession } from "./openSession";

const roots = new Map<string, string>();
vi.mock("@/hooks/useSession", () => ({
  useActiveRootSessionId: (id: string | null) => (id === null ? null : (roots.get(id) ?? id)),
}));

let goTo: (path: string) => void = () => {};
function Shell({ routed }: { routed: string | null }) {
  goTo = useNavigate();
  usePublishOpenSession(routed);
  return null;
}

function renderShell(path: string, routed: string | null) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Shell routed={routed} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  roots.clear();
});

describe("openSession", () => {
  it("counts the routed session and its sub-agent's root as open", () => {
    roots.set("child", "parent");
    renderShell("/c/child", "child");
    expect(isOpenSession("child")).toBe(true);
    expect(isOpenSession("parent")).toBe(true);
    expect(isOpenSession("other")).toBe(false);
  });

  it("counts the session a new sibling is drafted from, only on the new session page", () => {
    renderShell("/?sibling=source", null);
    expect(isOpenSession("source")).toBe(true);
    act(() => goTo("/settings?sibling=source"));
    expect(isOpenSession("source")).toBe(false);
  });

  it("counts a draft's source at the mount root, written with or without its slash", () => {
    for (const path of ["/mount?sibling=source", "/mount/?sibling=source"]) {
      const view = render(
        <RoutingProvider value={basenamedRouting("/mount")}>
          <MemoryRouter initialEntries={[path]}>
            <Shell routed={null} />
          </MemoryRouter>
        </RoutingProvider>,
      );
      expect(isOpenSession("source"), path).toBe(true);
      view.unmount();
    }
  });

  it("follows a switch to another session and clears once the shell unmounts", () => {
    const view = renderShell("/c/first", "first");
    act(() => goTo("/c/second"));
    view.rerender(
      <MemoryRouter initialEntries={["/c/first"]}>
        <Shell routed="second" />
      </MemoryRouter>,
    );
    expect(isOpenSession("first")).toBe(false);
    expect(isOpenSession("second")).toBe(true);
    view.unmount();
    expect(isOpenSession("second")).toBe(false);
  });

  it("keeps the ids a second publisher published when the first unmounts", () => {
    const first = renderShell("/c/first", "first");
    const second = renderShell("/c/second", "second");
    first.unmount();
    expect(isOpenSession("second")).toBe(true);
    second.unmount();
    expect(isOpenSession("second")).toBe(false);
  });
});
