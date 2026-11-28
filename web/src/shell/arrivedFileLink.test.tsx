import type { ReactNode } from "react";
import { act, render, renderHook } from "@testing-library/react";
import { MemoryRouter, type NavigateFunction, useNavigate } from "react-router-dom";
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

describe("useArrivedFileLink", () => {
  it("keeps the link a page arrived with across the app's own rewrites, and hands it over once", () => {
    const { result } = arrivedLink("/c/s?file=a.ts&diff=1");
    act(() => result.current.navigate("/c/s?file=a.ts", { replace: true }));

    expect(result.current.link.take("a.ts")?.toString()).toBe("file=a.ts&diff=1");
    expect(result.current.link.take("a.ts")).toBeNull();
  });

  it("keeps the link for a shell that remounts after the app rewrote the URL", () => {
    const hooks: { take?: (path: string) => URLSearchParams | null; navigate?: NavigateFunction } =
      {};
    function Probe() {
      hooks.take = useArrivedFileLink().take;
      hooks.navigate = useNavigate();
      return null;
    }
    const shell = (generation: number) => (
      <MemoryRouter initialEntries={["/c/s?file=a.ts&diff=1"]}>
        <Probe key={generation} />
      </MemoryRouter>
    );
    const { rerender } = render(shell(1));
    act(() => hooks.navigate!("/c/s?file=a.ts", { replace: true }));
    rerender(shell(2));

    expect(hooks.take!("a.ts")?.toString()).toBe("file=a.ts&diff=1");
  });

  it("takes a link that names another file as nothing, and forgets it", () => {
    const { result } = arrivedLink("/c/s?file=a.ts&diff=1");
    act(() => result.current.navigate("/c/s?file=b.ts", { replace: true }));

    expect(result.current.link.take("b.ts")).toBeNull();
    expect(result.current.link.take("a.ts")).toBeNull();
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

    expect(result.current.link.take("a.ts")).toBeNull();
  });

  it("follows each link a person follows, back and forward included", () => {
    const { result } = arrivedLink("/c/s?file=a.ts");
    act(() => result.current.navigate("/c/s?file=b.ts&line=2"));
    expect(result.current.link.take("b.ts")?.get("line")).toBe("2");

    act(() => result.current.navigate(-1));
    expect(result.current.link.take("a.ts")?.toString()).toBe("file=a.ts");
  });
});
