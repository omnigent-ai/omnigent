import { afterEach, describe, expect, it } from "vitest";

import { sessionIdAtPath } from "./sessionRoutes";

afterEach(() => {
  delete window.__OMNIGENT_BASE_PATH__;
});

describe("sessionIdAtPath", () => {
  it.each([
    ["/c/abc", "abc"],
    ["/c/abc/", "abc"],
    ["/canvas/c/abc", "abc"],
    ["/", null],
    ["/inbox", null],
    ["/c/abc/files", null],
    ["/extensions/demo/c/abc", null],
  ])("reads %s as %s", (path, expected) => {
    expect(sessionIdAtPath(path)).toBe(expected);
  });

  it("strips the server's base path first", () => {
    window.__OMNIGENT_BASE_PATH__ = "/omnigent";
    expect(sessionIdAtPath("/omnigent/c/abc")).toBe("abc");
    expect(sessionIdAtPath("/omnigent/c/abc/")).toBe("abc");
    expect(sessionIdAtPath("/omnigent/canvas/c/abc")).toBe("abc");
  });

  it("matches under an embedding host's mount", () => {
    const rebase = (path: string) => `/mount${path}`;
    expect(sessionIdAtPath("/mount/c/abc", rebase)).toBe("abc");
    expect(sessionIdAtPath("/c/abc", rebase)).toBeNull();
  });
});
