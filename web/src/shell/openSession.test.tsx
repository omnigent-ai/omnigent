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

  it("counts the session in a project board's quick view, only on the project's home", () => {
    renderShell("/projects/Payments?session=card", null);
    expect(isOpenSession("card")).toBe(true);
    act(() => goTo("/projects/Payments?session=other"));
    expect(isOpenSession("card")).toBe(false);
    expect(isOpenSession("other")).toBe(true);
    act(() => goTo("/settings?session=other"));
    expect(isOpenSession("other")).toBe(false);
    // An extension's page can end in /projects/<name>, but it shows no board.
    act(() => goTo("/extensions/demo/projects/Payments?session=other"));
    expect(isOpenSession("other")).toBe(false);
  });

  it("counts a quick view on the project's home under an embedding mount", () => {
    render(
      <MemoryRouter initialEntries={["/mount/projects/Payments?session=card"]}>
        <RoutingProvider value={basenamedRouting("/mount")}>
          <Shell routed={null} />
        </RoutingProvider>
      </MemoryRouter>,
    );
    expect(isOpenSession("card")).toBe(true);
  });

  it("follows a switch to another session and clears once the shell unmounts", () => {
    const view = renderShell("/c/first", "first");
    // The router keeps its first entries, so the switch navigates and the shell re-routes.
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
});
