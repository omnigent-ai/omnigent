import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { basenamedRouting, Link, RoutingProvider, useLocation, useNavigate } from "./routing";
import {
  SessionNavigationProvider,
  type SessionHrefResolver,
  sessionPageHref,
  useNavigateToSession,
  useSessionHref,
} from "./sessionNavigation";
import { SessionNavigationTestHost } from "./sessionNavigation.test-utils";
import { canvasSessionHref } from "@/canvas/canvasNavigation";

afterEach(cleanup);

const MIXED_SEARCH =
  "?canvas=board&session=old&file=a&file=b&diff=1&comment=c1&view=terminal&debug=1&o=123";

function location() {
  return new URL(screen.getByTestId("location").textContent ?? "/", "https://example.test");
}

function NavigationProbe({ waitForCreate }: { waitForCreate?: () => Promise<void> }) {
  const current = useLocation();
  const navigate = useNavigate();
  const sessionHref = useSessionHref();
  const navigateToSession = useNavigateToSession();
  return (
    <>
      <output data-testid="location">{current.pathname + current.search}</output>
      <Link to={sessionHref("child", current.search)}>Child</Link>
      <Link to={sessionPageHref("child")}>Full page</Link>
      <Link to="/c/parent?file=report.txt&comment=comment-1">Comment link</Link>
      <button type="button" onClick={() => navigateToSession("child", { replace: true })}>
        Replace session
      </button>
      <button type="button" onClick={() => navigateToSession(null)}>
        Close session
      </button>
      <button
        type="button"
        onClick={async () => {
          await waitForCreate?.();
          await navigateToSession("created");
        }}
      >
        Create session
      </button>
      <button type="button" onClick={() => navigate({ search: "?canvas=other&session=old&o=456" })}>
        Change query
      </button>
      <button type="button" onClick={() => navigate(-1)}>
        Back
      </button>
      <button type="button" onClick={() => navigate(1)}>
        Forward
      </button>
    </>
  );
}

function renderNavigation({
  resolveHref,
  basename = "",
  initialEntries = ["/c/root?file=a&diff=1&view=terminal&debug=1"],
  waitForCreate,
}: {
  resolveHref?: SessionHrefResolver;
  basename?: string;
  initialEntries?: string[];
  waitForCreate?: () => Promise<void>;
} = {}) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <RoutingProvider value={basenamedRouting(basename)}>
        {resolveHref ? (
          <SessionNavigationTestHost resolveHref={resolveHref}>
            <NavigationProbe waitForCreate={waitForCreate} />
          </SessionNavigationTestHost>
        ) : (
          <NavigationProbe waitForCreate={waitForCreate} />
        )}
      </RoutingProvider>
    </MemoryRouter>,
  );
}

function reviewSessionHref(sessionId: string | null, search = ""): string {
  const params = new URLSearchParams(search);
  params.delete("thread");
  if (sessionId !== null) params.set("thread", sessionId);
  const query = params.toString();
  return query ? `/review?${query}` : "/review";
}

function ReplaceableHost({ waitForCreate }: { waitForCreate: () => Promise<void> }) {
  const [host, setHost] = useState("review");
  return (
    <>
      <button type="button" onClick={() => setHost("triage")}>
        Switch host policy
      </button>
      <SessionNavigationProvider
        resolveHref={(id) => (id === null ? `/${host}` : `/${host}?thread=${id}`)}
      >
        <NavigationProbe waitForCreate={waitForCreate} />
      </SessionNavigationProvider>
    </>
  );
}

describe("generic session navigation policies", () => {
  it.each(["", "/mount"])(
    "supports a non-Canvas host without imposing Canvas parameters (%s)",
    (basename) => {
      const search = "?workspace=team&view=review&file=notes.md";
      renderNavigation({
        resolveHref: reviewSessionHref,
        basename,
        initialEntries: [`${basename}/review${search}`],
      });
      expect(screen.getByRole("link", { name: "Child" })).toHaveAttribute(
        "href",
        `${basename}/review${search}&thread=child`,
      );
      fireEvent.click(screen.getByRole("link", { name: "Child" }));
      expect(location().pathname).toBe(`${basename}/review`);
      expect(location().search).toBe(`${search}&thread=child`);

      fireEvent.click(screen.getByRole("button", { name: "Close session" }));
      expect(location().pathname).toBe(`${basename}/review`);
      expect(location().search).toBe(search);
      fireEvent.click(screen.getByRole("button", { name: "Replace session" }));
      expect(location().search).toBe(`${search}&thread=child`);
    },
  );

  it.each(["", "/mount"])(
    "does not intercept canonical links or comment deep links (%s)",
    (basename) => {
      renderNavigation({
        resolveHref: reviewSessionHref,
        basename,
        initialEntries: [`${basename}/review?thread=source`],
      });
      const comment = screen.getByRole("link", { name: "Comment link" });
      expect(comment).toHaveAttribute(
        "href",
        `${basename}/c/parent?file=report.txt&comment=comment-1`,
      );
      fireEvent.click(comment);
      expect(location().pathname).toBe(`${basename}/c/parent`);
      expect(location().search).toBe("?file=report.txt&comment=comment-1");
      fireEvent.click(screen.getByRole("link", { name: "Full page" }));
      expect(location().pathname).toBe(`${basename}/c/child`);
      expect(location().search).toBe("");
    },
  );

  it.each(["", "/mount"])(
    "uses a replacement resolver after an asynchronous operation (%s)",
    async (basename) => {
      let finish = () => {};
      const pending = new Promise<void>((resolve) => {
        finish = resolve;
      });
      render(
        <MemoryRouter initialEntries={[`${basename}/review`]}>
          <RoutingProvider value={basenamedRouting(basename)}>
            <ReplaceableHost waitForCreate={() => pending} />
          </RoutingProvider>
        </MemoryRouter>,
      );
      fireEvent.click(screen.getByRole("button", { name: "Create session" }));
      fireEvent.click(screen.getByRole("button", { name: "Switch host policy" }));
      expect(screen.getByRole("link", { name: "Child" })).toHaveAttribute(
        "href",
        `${basename}/triage?thread=child`,
      );
      await act(async () => {
        finish();
        await pending;
      });
      expect(location().pathname).toBe(`${basename}/triage`);
      expect(location().search).toBe("?thread=created");
    },
  );

  it("allows a nested session surface to retain canonical navigation", () => {
    render(
      <MemoryRouter initialEntries={["/review"]}>
        <SessionNavigationProvider resolveHref={reviewSessionHref}>
          <SessionNavigationProvider resolveHref={sessionPageHref}>
            <NavigationProbe />
          </SessionNavigationProvider>
        </SessionNavigationProvider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Replace session" }));
    expect(location().pathname).toBe("/c/child");
    fireEvent.click(screen.getByRole("button", { name: "Close session" }));
    expect(location().pathname).toBe("/");
  });
});

describe("session destinations", () => {
  it("keeps canonical page destinations and optional global query state", () => {
    expect(sessionPageHref("child")).toBe("/c/child");
    expect(sessionPageHref("temp:local")).toBe("/c/temp:local");
    expect(sessionPageHref(null)).toBe("/");
    expect(
      sessionPageHref("child", "?file=a&file=b&diff=1&comment=c1&view=terminal&debug=1&o=123"),
    ).toBe("/c/child?debug=1&o=123");
  });
});

describe("session navigation host", () => {
  it("keeps ordinary rail navigation canonical with cleaned query state", () => {
    renderNavigation();
    expect(screen.getByRole("link", { name: "Child" })).toHaveAttribute("href", "/c/child?debug=1");
    fireEvent.click(screen.getByRole("link", { name: "Child" }));
    expect(location().pathname).toBe("/c/child");
    expect(location().search).toBe("?debug=1");
  });

  it("does not infer a Canvas host merely from the current URL", () => {
    renderNavigation({ initialEntries: ["/canvas?canvas=board&session=old"] });
    fireEvent.click(screen.getByRole("button", { name: "Replace session" }));
    expect(location().pathname).toBe("/c/child");
    expect(location().search).toBe("");
  });

  it.each(["", "/mount"])(
    "keeps contextual links and navigation inside the %s Canvas mount",
    (basename) => {
      renderNavigation({
        resolveHref: canvasSessionHref,
        basename,
        initialEntries: [`${basename}/canvas${MIXED_SEARCH}`],
      });
      expect(screen.getByRole("link", { name: "Child" })).toHaveAttribute(
        "href",
        `${basename}/canvas?canvas=board&debug=1&o=123&session=child&view=chat`,
      );
      expect(screen.getByRole("link", { name: "Full page" })).toHaveAttribute(
        "href",
        `${basename}/c/child`,
      );
      fireEvent.click(screen.getByRole("button", { name: "Replace session" }));
      expect(location().pathname).toBe(`${basename}/canvas`);
      expect(location().searchParams.get("session")).toBe("child");
      expect(location().searchParams.get("o")).toBe("123");
    },
  );

  it("uses the latest query state, not the search captured when the host mounted", () => {
    renderNavigation({
      resolveHref: canvasSessionHref,
      initialEntries: [`/canvas${MIXED_SEARCH}`],
    });
    fireEvent.click(screen.getByRole("button", { name: "Change query" }));
    fireEvent.click(screen.getByRole("link", { name: "Child" }));
    expect(location().search).toBe("?canvas=other&o=456&session=child&view=chat");
  });

  it.each(["", "/mount"])(
    "preserves updated query state after asynchronous creation (%s)",
    async (basename) => {
      let finish = () => {};
      const pending = new Promise<void>((resolve) => {
        finish = resolve;
      });
      renderNavigation({
        resolveHref: canvasSessionHref,
        basename,
        initialEntries: [`${basename}/canvas${MIXED_SEARCH}`],
        waitForCreate: () => pending,
      });
      fireEvent.click(screen.getByRole("button", { name: "Create session" }));
      fireEvent.click(screen.getByRole("button", { name: "Change query" }));
      await act(async () => {
        finish();
        await pending;
      });

      expect(location().pathname).toBe(`${basename}/canvas`);
      expect(location().search).toBe("?canvas=other&o=456&session=created&view=chat");
    },
  );

  it("closes back to the current board, preserving unrelated query parameters", () => {
    renderNavigation({
      resolveHref: canvasSessionHref,
      initialEntries: [`/canvas${MIXED_SEARCH}`],
    });
    fireEvent.click(screen.getByRole("button", { name: "Close session" }));
    expect(location().pathname).toBe("/canvas");
    expect(location().search).toBe("?canvas=board&debug=1&o=123");
  });

  it("preserves replace semantics so Back cannot reopen a superseded selection", () => {
    renderNavigation({
      resolveHref: canvasSessionHref,
      initialEntries: ["/before", "/canvas?canvas=board&session=old"],
    });
    fireEvent.click(screen.getByRole("button", { name: "Replace session" }));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(location().pathname).toBe("/before");
    fireEvent.click(screen.getByRole("button", { name: "Forward" }));
    expect(location().pathname).toBe("/canvas");
    expect(location().searchParams.get("session")).toBe("child");
  });
});
