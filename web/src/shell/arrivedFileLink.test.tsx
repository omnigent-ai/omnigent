import { StrictMode, Suspense, startTransition, useEffect, type ReactNode } from "react";
import { act, render, renderHook } from "@testing-library/react";
import { MemoryRouter, type NavigateFunction, useLocation, useNavigate } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { fileLinkOpenOptions, useArrivedFileLink } from "./arrivedFileLink";

describe("fileLinkOpenOptions", () => {
  it.each([
    ["file=a.ts", {}],
    ["file=a.ts&line=4&column=2&endLine=9", { line: 4, column: 2, endLine: 9 }],
    ["file=a.ts&line=0&column=2", {}],
    ["file=a.ts&line=4&endLine=3", { line: 4 }],
    ["file=a.ts&diff=1", { view: "diff" }],
    ["file=a.ts&diff=0", {}],
    ["file=a.ts&comment=c1", { commentId: "c1" }],
    ["file=a.ts&comment=", {}],
  ])("reads %s", (query, expected) => {
    expect(fileLinkOpenOptions(new URLSearchParams(query))).toEqual(expected);
  });
});

function arrivedLink(entry: string) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[entry]}>{children}</MemoryRouter>
  );
  return renderHook(() => ({ link: useArrivedFileLink(), navigate: useNavigate() }), { wrapper });
}

/** What a mounted shell sees of the hook, and its router's navigate. */
interface Shell {
  peek?: (path: string) => URLSearchParams | null;
  navigate?: NavigateFunction;
}

const neverResolves = new Promise<never>(() => {});

describe("useArrivedFileLink", () => {
  it("keeps the link a page arrived with across the app's own rewrites until it leaves the URL", () => {
    const { result } = arrivedLink("/c/s?file=a.ts&diff=1");
    act(() => result.current.navigate("/c/s?file=a.ts", { replace: true }));

    // Read as often as an effect runs, replays included, while the link stays in the URL.
    expect(result.current.link.peek("a.ts")?.toString()).toBe("file=a.ts&diff=1");
    expect(result.current.link.peek("a.ts")?.toString()).toBe("file=a.ts&diff=1");
    // Handed over: the app takes the link off the URL.
    act(() => result.current.navigate("/c/s", { replace: true }));
    expect(result.current.link.peek("a.ts")).toBeNull();
  });

  it("gives a replayed effect the same link, as StrictMode replays the shell's", () => {
    const shell: Shell = {};
    const seen: (string | null)[] = [];
    function Probe() {
      const { peek } = useArrivedFileLink();
      shell.navigate = useNavigate();
      useEffect(() => {
        seen.push(peek("a.ts")?.toString() ?? null);
      }, [peek]);
      return null;
    }
    render(
      <StrictMode>
        <MemoryRouter initialEntries={["/c/s?file=a.ts&diff=1"]}>
          <Probe />
        </MemoryRouter>
      </StrictMode>,
    );

    expect(seen.length).toBeGreaterThan(1);
    expect(new Set(seen)).toEqual(new Set(["file=a.ts&diff=1"]));
  });

  it("keeps the link through a navigation whose render never commits", () => {
    const shell: Shell = {};
    function Probe() {
      shell.peek = useArrivedFileLink().peek;
      shell.navigate = useNavigate();
      // The next page is still loading, so React keeps this one on screen.
      if (useLocation().search.includes("b.ts")) throw neverResolves;
      return null;
    }
    render(
      <MemoryRouter initialEntries={["/c/s?file=a.ts&diff=1"]}>
        <Suspense fallback={null}>
          <Probe />
        </Suspense>
      </MemoryRouter>,
    );
    const navigate = shell.navigate!;
    act(() => startTransition(() => navigate("/c/s?file=b.ts")));

    expect(shell.peek!("a.ts")?.toString()).toBe("file=a.ts&diff=1");
  });

  it("keeps the link for a shell that remounts after the app rewrote the URL", () => {
    const shell: Shell = {};
    function Probe() {
      shell.peek = useArrivedFileLink().peek;
      shell.navigate = useNavigate();
      return null;
    }
    const tree = (generation: number) => (
      <MemoryRouter initialEntries={["/c/s?file=a.ts&diff=1"]}>
        <Probe key={generation} />
      </MemoryRouter>
    );
    const { rerender } = render(tree(1));
    act(() => shell.navigate!("/c/s?file=a.ts", { replace: true }));
    rerender(tree(2));

    expect(shell.peek!("a.ts")?.toString()).toBe("file=a.ts&diff=1");
  });

  it("gives nothing for another file than the one the link names", () => {
    const { result } = arrivedLink("/c/s?file=a.ts&diff=1");

    expect(result.current.link.peek("b.ts")).toBeNull();
    expect(result.current.link.peek("a.ts")?.toString()).toBe("file=a.ts&diff=1");
  });

  it.each([
    ["another file, then back to a plain link", ["/c/s?file=b.ts", "/c/s?file=a.ts"]],
    ["no file, then back to a plain link", ["/c/s", "/c/s?file=a.ts"]],
    ["another session with the same file", ["/c/other?file=a.ts"]],
  ])("forgets the link once a replace moves the URL to %s", (_, rewrites) => {
    const { result } = arrivedLink("/c/s?file=a.ts&line=4&diff=1");
    for (const rewrite of rewrites) {
      act(() => result.current.navigate(rewrite, { replace: true }));
    }

    expect(result.current.link.peek("a.ts")).toBeNull();
  });

  it("follows each link a person follows, back and forward included", () => {
    const { result } = arrivedLink("/c/s?file=a.ts");
    act(() => result.current.navigate("/c/s?file=b.ts&line=2"));
    expect(result.current.link.peek("b.ts")?.get("line")).toBe("2");

    act(() => result.current.navigate(-1));
    expect(result.current.link.peek("a.ts")?.toString()).toBe("file=a.ts");

    act(() => result.current.navigate(1));
    expect(fileLinkOpenOptions(result.current.link.peek("b.ts")!)).toEqual({ line: 2 });
  });
});
