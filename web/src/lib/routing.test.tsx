import { render, screen } from "@testing-library/react";
import { MemoryRouter, type To } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { basenamedRouting, reactRouterRouting } from "./routing";

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
    // Regression guard: the settings "Back to Omnigent" link targets the
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
