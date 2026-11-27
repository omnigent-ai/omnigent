import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import {
  createContext,
  createRef,
  memo,
  startTransition,
  Suspense,
  useCallback,
  useContext,
  useState,
} from "react";
import { MemoryRouter, Route, Routes, type To, useLocation, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  basenamedRouting,
  Link as RoutingLink,
  reactRouterRouting,
  RoutingProvider,
  ScopedStableNavigation,
  useNavigate as useAppNavigate,
  useReplaceSearchParams,
} from "./routing";
import { setOmnigentHostConfig } from "@/lib/host";

describe("scoped event navigation", () => {
  afterEach(cleanup);

  it.each(["", "/mount"])("keeps consumers idle and navigation current under '%s'", (basename) => {
    const renders = vi.fn();
    const anchor = createRef<HTMLAnchorElement>();
    const Consumer = memo(function Consumer() {
      renders();
      const navigate = useAppNavigate();
      return (
        <>
          <RoutingLink ref={anchor} to="/c/linked/view">
            Linked session
          </RoutingLink>
          <button type="button" onClick={() => navigate("../relative")}>
            Relative
          </button>
          <button type="button" onClick={() => navigate("../peer", { relative: "path" })}>
            Path relative
          </button>
          <button
            type="button"
            onClick={() => navigate({ search: "?latest=1" }, { replace: true, state: "kept" })}
          >
            Query
          </button>
          <button
            type="button"
            onClick={() => navigate({ pathname: "/c/object/view", hash: "#reply" })}
          >
            Absolute object
          </button>
          <button type="button" onClick={() => navigate(-1)}>
            Back
          </button>
          <button type="button" onClick={() => navigate(1)}>
            Forward
          </button>
        </>
      );
    });
    function LocationAndSwitch() {
      const location = useLocation();
      const navigate = useNavigate();
      return (
        <>
          <output>
            {location.pathname}
            {location.search}
            {location.hash}|{location.state}
          </output>
          <button type="button" onClick={() => navigate(`${basename}/c/second/view`)}>
            Switch
          </button>
        </>
      );
    }
    render(
      <MemoryRouter initialEntries={[`${basename}/c/first/view`]}>
        <LocationAndSwitch />
        <RoutingProvider value={basename ? basenamedRouting(basename) : reactRouterRouting}>
          <Routes>
            <Route path={`${basename}/c/:conversationId`}>
              <Route
                path="*"
                element={
                  <ScopedStableNavigation>
                    <Consumer />
                  </ScopedStableNavigation>
                }
              />
            </Route>
          </Routes>
        </RoutingProvider>
      </MemoryRouter>,
    );
    const expectLocation = (suffix: string) =>
      expect(screen.getByRole("status").textContent).toBe(`${basename}${suffix}`);
    const click = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
    expect(anchor.current).toBe(screen.getByRole("link", { name: "Linked session" }));
    expect(anchor.current).toHaveAttribute("href", `${basename}/c/linked/view`);
    click("Switch");
    expectLocation("/c/second/view|");
    click("Relative");
    expectLocation("/c/second/relative|");
    click("Path relative");
    expectLocation("/c/second/peer|");
    click("Query");
    expectLocation("/c/second/peer?latest=1|kept");
    click("Back");
    expectLocation("/c/second/relative|");
    click("Forward");
    expectLocation("/c/second/peer?latest=1|kept");
    click("Absolute object");
    expectLocation("/c/object/view#reply|");
    fireEvent.click(anchor.current!);
    expectLocation("/c/linked/view|");
    expect(renders).toHaveBeenCalledTimes(1);
  });

  it("uses the latest committed injected callback without leaking suspended renders", () => {
    const Version = createContext("committed");
    const calls = vi.fn();
    const suspended = vi.fn();
    const pending = new Promise<never>(() => {});
    const routing = {
      ...reactRouterRouting,
      useNavigate: () => {
        const version = useContext(Version);
        return useCallback(
          (to: To | number) => {
            calls(version, to);
          },
          [version],
        );
      },
    };
    let retainedNavigate: ReturnType<typeof useAppNavigate>;
    const Consumer = memo(function Consumer() {
      retainedNavigate = useAppNavigate();
      return null;
    });
    function SuspendPending() {
      if (useContext(Version) === "pending") {
        suspended();
        throw pending;
      }
      return null;
    }
    function Host() {
      const [version, setVersion] = useState("committed");
      return (
        <>
          <button type="button" onClick={() => startTransition(() => setVersion("pending"))}>
            Suspend
          </button>
          <button type="button" onClick={() => setVersion("latest")}>
            Commit
          </button>
          <Suspense fallback="Waiting">
            <Version.Provider value={version}>
              <RoutingProvider value={routing}>
                <ScopedStableNavigation>
                  <Consumer />
                  <SuspendPending />
                </ScopedStableNavigation>
              </RoutingProvider>
            </Version.Provider>
          </Suspense>
        </>
      );
    }
    render(<Host />);
    const navigate = retainedNavigate!;
    fireEvent.click(screen.getByRole("button", { name: "Suspend" }));
    expect(suspended).toHaveBeenCalled();
    act(() => {
      navigate("/c/first");
    });
    expect(calls).toHaveBeenLastCalledWith("committed", "/c/first");
    fireEvent.click(screen.getByRole("button", { name: "Commit" }));
    expect(retainedNavigate!).toBe(navigate);
    act(() => {
      navigate(-1);
    });
    expect(calls).toHaveBeenLastCalledWith("latest", -1);
  });
});

// `basenamedRouting` is the embed seam: it rebases web's absolute
// navigation targets under the host mount path so links land under
// `basename` instead of the host root. These tests render its `Link`
// inside a real `MemoryRouter` and read the resolved `href` — the value
// the browser would actually navigate to.
function renderRebasedLink(basename: string, to: To): string | null {
  const { Link } = basenamedRouting(basename);
  const { unmount } = render(
    <MemoryRouter initialEntries={[basename]}>
      <Link to={to}>go</Link>
    </MemoryRouter>,
  );
  const href = screen.getByRole("link", { name: "go" }).getAttribute("href");
  // Unmount so callers can render multiple links in one test without DOM collisions.
  unmount();
  return href;
}

describe("basenamedRouting Link rebasing", () => {
  it("rebases a string absolute path under the basename", () => {
    // String form already worked; this is the baseline the object form must match.
    expect(renderRebasedLink("/mount", "/c/abc")).toBe("/mount/c/abc");
  });

  it("rebases the pathname of an object `to` and preserves its search", () => {
    // Regression guard: object-form `to` (used by the subagents rail to carry
    // a preserved `?debug=1` search) previously bypassed rebasing entirely, so
    // the link landed at the host root `/c/abc` instead of `/mount/c/abc`.
    // Before the seam fix this returns "/c/abc?debug=1" and the assertion fails.
    expect(renderRebasedLink("/mount", { pathname: "/c/abc", search: "?debug=1" })).toBe(
      "/mount/c/abc?debug=1",
    );
  });

  it("does not double-prefix a path already under the basename", () => {
    // String already under the mount must pass through untouched.
    expect(renderRebasedLink("/mount", "/mount/c/abc")).toBe("/mount/c/abc");
    // Same invariant for the object form's pathname.
    expect(renderRebasedLink("/mount", { pathname: "/mount/c/abc" })).toBe("/mount/c/abc");
  });

  it("does not double-prefix the bare basename carrying a query", () => {
    // Regression guard: the settings Back link targets the
    // pre-settings location captured from `useLocation()`, which in the embed
    // already includes the basename. On the home page that's the bare basename
    // plus the host's `?o=<workspace>` search (e.g. `/mount?o=123`). The old
    // guard only treated `=== basename` / `${basename}/` as "already under",
    // so the `?`-boundary form fell through and was prefixed again, landing at
    // `/mount/mount?o=123` — a 404 (the reported double-basename bug).
    expect(renderRebasedLink("/mount", "/mount?o=123")).toBe("/mount?o=123");
    expect(renderRebasedLink("/mount", { pathname: "/mount", search: "?o=123" })).toBe(
      "/mount?o=123",
    );
  });

  it("still rebases a distinct sibling segment that only shares the basename prefix", () => {
    // The boundary check must not over-match: `/mounting` is NOT under `/mount`
    // (no `/`, `?`, or `#` at the boundary), so it gets rebased like any other
    // app-absolute path.
    expect(renderRebasedLink("/mount", "/mounting")).toBe("/mount/mounting");
  });
});

describe("basenamedRouting navigation", () => {
  it("keeps the rebased navigate callback stable across renders", async () => {
    const baseNavigate = vi.fn();
    const base = {
      ...reactRouterRouting,
      useNavigate: () => baseNavigate,
    };
    const { result, rerender } = renderHook(
      ({ basename }) => basenamedRouting(basename, base).useNavigate(),
      { initialProps: { basename: "/mount" } },
    );
    const navigate = result.current;

    rerender({ basename: "/next" });

    expect(result.current).toBe(navigate);
    await result.current("/c/abc");
    expect(baseNavigate).toHaveBeenCalledWith("/next/c/abc", undefined);
  });
});

describe("local query replacement", () => {
  it("preserves path, fragment and state and replaces history without host navigation", () => {
    const hostNavigate = vi.fn();
    function QueryEditor() {
      const replaceSearch = useReplaceSearchParams();
      const location = useLocation();
      const navigate = useNavigate();
      return (
        <>
          <output>{JSON.stringify({ ...location, key: undefined })}</output>
          <button type="button" onClick={() => replaceSearch(new URLSearchParams({ o: "123" }))}>
            Consume prompt
          </button>
          <button type="button" onClick={() => navigate(-1)}>
            Back
          </button>
        </>
      );
    }
    const view = render(
      <MemoryRouter
        initialEntries={[
          "/previous",
          {
            pathname: "/omnigent",
            search: "?o=123&prompt=hello",
            hash: "#composer",
            state: { context: "retained" },
          },
        ]}
      >
        <RoutingProvider value={{ ...reactRouterRouting, useNavigate: () => hostNavigate }}>
          <QueryEditor />
        </RoutingProvider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Consume prompt" }));
    expect(JSON.parse(view.getByRole("status").textContent!)).toEqual({
      pathname: "/omnigent",
      search: "?o=123",
      hash: "#composer",
      state: { context: "retained" },
    });
    expect(hostNavigate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(JSON.parse(view.getByRole("status").textContent!).pathname).toBe("/previous");
    view.unmount();
  });
});

describe("rebasePath primitive", () => {
  // `rebasePath` is the seam used to build absolute URLs (e.g. the share link
  // in PermissionsModal) so they respect the host mount path in the embed.

  it("is identity in standalone (no basename)", () => {
    // Standalone has no RoutingProvider, so consumers get reactRouterRouting.
    expect(reactRouterRouting.rebasePath("/c/abc")).toBe("/c/abc");
  });

  it("prepends the basename in the embed", () => {
    expect(basenamedRouting("/mount").rebasePath("/c/abc")).toBe("/mount/c/abc");
  });

  it("does not double-prefix a path already under the basename", () => {
    expect(basenamedRouting("/mount").rebasePath("/mount/c/abc")).toBe("/mount/c/abc");
    // The `?`/`#` boundary forms are equally "already under" the basename.
    expect(basenamedRouting("/mount").rebasePath("/mount?o=123")).toBe("/mount?o=123");
    expect(basenamedRouting("/mount").rebasePath("/mount#frag")).toBe("/mount#frag");
  });

  it("rebases a distinct sibling segment that only shares the basename prefix", () => {
    // `/mounting` merely shares the `/mount` text prefix; it's a different path
    // and must be rebased under the mount, not treated as already-under.
    expect(basenamedRouting("/mount").rebasePath("/mounting")).toBe("/mount/mounting");
  });
});

describe("Link analytics (componentId)", () => {
  afterEach(() => {
    cleanup();
    setOmnigentHostConfig({});
  });

  function renderLink(props: { componentId?: string; onClick?: () => void }) {
    return render(
      <MemoryRouter>
        <RoutingLink to="/tasks" {...props}>
          Tasks
        </RoutingLink>
      </MemoryRouter>,
    );
  }

  it("reports a click to the host sink and still calls the caller's onClick", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const onClick = vi.fn();
    renderLink({ componentId: "sidebar.tasks", onClick });
    fireEvent.click(screen.getByRole("link", { name: "Tasks" }));
    expect(analytics).toHaveBeenCalledExactlyOnceWith({
      type: "click",
      componentId: "sidebar.tasks",
      componentKind: "link",
    });
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("does not leak componentId onto the rendered <a>", () => {
    setOmnigentHostConfig({ analytics: vi.fn() });
    renderLink({ componentId: "sidebar.tasks" });
    // React would warn on an unknown DOM attribute; also assert it isn't present.
    expect(screen.getByRole("link", { name: "Tasks" })).not.toHaveAttribute("componentId");
  });

  it("emits nothing when componentId is absent", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    renderLink({});
    fireEvent.click(screen.getByRole("link", { name: "Tasks" }));
    expect(analytics).not.toHaveBeenCalled();
  });
});
